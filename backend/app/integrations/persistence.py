"""Legătura dintre SessionManager (stare autoritativă, în memorie) și Tiger Data.

SessionManager trimite fiecare tranziție publicată către un `recorder`. TigerRecorder o pune
într-o coadă și o scrie în baza de date în fundal, în ordine, fără să blocheze bucla asyncio.
Dacă Tiger Data e indisponibil, logica de siguranță (verificări, SOS, anulare) continuă
neafectată; doar istoricul rămâne în urmă, iar erorile apar în log.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

log = logging.getLogger("vesta.persistence")


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    session_id: str
    device_id: str
    kind: str  # TransitionKind.value
    from_state: str
    to_state: str
    occurred_at: datetime
    details: dict[str, Any] = field(default_factory=dict)
    source: str | None = None  # android | voice_agent | dashboard | voice_gateway | backend ...
    command_event_id: str | None = None
    reason: str | None = None


class SessionRecorder(Protocol):
    def submit(self, record: TransitionRecord) -> None: ...


class NullRecorder:
    """Implicit: fără persistență (teste, mod memory)."""

    def submit(self, record: TransitionRecord) -> None:
        return None


# issue → (componentă, eveniment la apariție, eveniment la rezolvare)
_ISSUES: dict[str, tuple[str, str, str]] = {
    "network_disconnected": ("PHONE", "PHONE_DISCONNECTED", "PHONE_RECONNECTED"),
    "voice_unavailable": ("VOICE_SERVICE", "SERVICE_UNAVAILABLE", "SERVICE_RECONNECTED"),
    "microphone_denied": ("AUDIO", "AUDIO_ERROR", "AUDIO_CONNECTED"),
    "audio_interrupted": ("AUDIO", "AUDIO_DISCONNECTED", "AUDIO_CONNECTED"),
    "audio_disconnected": ("AUDIO", "AUDIO_DISCONNECTED", "AUDIO_CONNECTED"),
    "voice_timeout": ("VOICE_SERVICE", "SERVICE_TIMEOUT", "SERVICE_RECONNECTED"),
    "gps_stale": ("GPS", "GPS_STALE", "GPS_AVAILABLE"),
    "gps_unavailable": ("GPS", "GPS_UNAVAILABLE", "GPS_AVAILABLE"),
    "page_hidden": ("PHONE", "PAGE_HIDDEN", "PAGE_VISIBLE"),
    "low_voltage": ("POWER", "LOW_VOLTAGE", "POWER_OK"),
    "power_unstable": ("POWER", "POWER_UNSTABLE", "POWER_OK"),
    "process_restarted": ("SERVER", "PROCESS_RESTARTED", "PROCESS_RECOVERED"),
}
_SOURCES = {"android": "COMPANION", "voice_agent": "VOICE", "dashboard": "DASHBOARD"}


class TigerRecorder:
    def __init__(
        self,
        db,
        *,
        recipients: tuple[str, ...] | list[str],
        response_timeout_s: float,
        queue_size: int = 2000,
        max_retries: int = 3,
        retry_base_s: float = 0.5,
    ):
        self._db = db
        self._recipients = list(recipients)
        self._timeout = response_timeout_s
        self._queue: asyncio.Queue[TransitionRecord] | None = None
        self._queue_size = queue_size
        self._max_retries = max_retries
        self._retry_base = retry_base_s
        self._task: asyncio.Task | None = None
        self._rounds: dict[str, int] = {}
        self.dropped = 0
        self.failed = 0

    # ---------- coadă ----------

    def submit(self, record: TransitionRecord) -> None:
        if self._queue is None:
            self._queue = asyncio.Queue(self._queue_size)
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped += 1
            log.error(
                "persistence queue full; dropped %s for session %s", record.kind, record.session_id
            )

    async def start(self) -> None:
        if self._queue is None:
            self._queue = asyncio.Queue(self._queue_size)
        self._task = asyncio.create_task(self._worker(), name="vesta-tiger-recorder")

    async def stop(self, drain_timeout_s: float = 5.0) -> None:
        if self._task is None:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._queue.join(), timeout=drain_timeout_s)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def flush(self) -> None:
        """Pentru teste: așteaptă scrierea tuturor înregistrărilor din coadă."""
        if self._queue is not None:
            await self._queue.join()

    async def _worker(self) -> None:
        from app.db.errors import VestaDbError

        assert self._queue is not None
        while True:
            rec = await self._queue.get()
            try:
                for attempt in range(1, self._max_retries + 1):
                    try:
                        await asyncio.to_thread(self.apply, rec)
                        break
                    except VestaDbError as e:
                        # stare din DB nesincronizată (ex. sesiune de dinainte de restart): nu reîncercăm
                        self.failed += 1
                        log.warning("persist %s for %s rejected: %s", rec.kind, rec.session_id, e)
                        break
                    except Exception:
                        if attempt == self._max_retries:
                            self.failed += 1
                            log.exception(
                                "persist %s for %s failed after %s attempts",
                                rec.kind,
                                rec.session_id,
                                attempt,
                            )
                        else:
                            await asyncio.sleep(self._retry_base * 2 ** (attempt - 1))
            finally:
                self._queue.task_done()

    # ---------- mapare tranziție → Tiger Data (sincron, rulează în thread) ----------

    def _event_id(self, rec: TransitionRecord, *parts: Any):
        from app.db import derived_event_id

        if rec.command_event_id:
            return derived_event_id("cmd", rec.kind, rec.command_event_id, *parts)
        return derived_event_id(rec.session_id, rec.kind, rec.occurred_at.isoformat(), *parts)

    def apply(self, rec: TransitionRecord) -> None:
        db, sid, at, k = self._db, rec.session_id, rec.occurred_at, rec.kind
        source = _SOURCES.get(rec.source or "", "SAFETY_ENGINE")

        if k == "activated":
            res = db.sessions.activate(
                rec.device_id, self._event_id(rec), session_id=sid, occurred_at=at
            )
            if res.status == "ALREADY_ACTIVE":
                log.warning(
                    "DB already has open session %s for %s; new session %s not persisted",
                    res.session["session_id"],
                    rec.device_id,
                    sid,
                )

        elif k == "check_in_started":
            attempt = int(rec.details.get("attempt", 1))
            if attempt == 1 or sid not in self._rounds:
                self._rounds[sid] = self._rounds.get(sid, 0) + 1
            db.sessions.transition(
                sid,
                "CHECK_IN",
                event_id=self._event_id(rec, "state"),
                occurred_at=at,
                metadata={"attempt": attempt},
            )
            db.checkins.request(
                sid,
                round_no=self._rounds[sid],
                attempt_no=attempt,
                checkin_id=rec.details.get("checkin_id"),
                timeout_s=self._timeout,
                at=at,
            )

        elif k == "check_in_delivered":
            channel = str(rec.details.get("channel") or "voice")
            db.checkins.mark_delivered(
                rec.details.get("checkin_id"),
                "AUDIO" if channel == "voice" else "COMPANION_UI",
                at=at,
            )

        elif k == "check_in_undelivered":
            if rec.to_state == "CHECK_IN":
                db.checkins.mark_audio_unavailable(rec.details.get("checkin_id"), at=at)
            else:
                # Fereastra de răspuns a expirat fără confirmare de livrare.
                # Rămâne TIMED_OUT cu delivered_at NULL, distinct de un timeout livrat.
                db.checkins.time_out_open(sid, at=at)
                db.sessions.transition(
                    sid,
                    "UNCONFIRMED",
                    event_id=self._event_id(rec, "state"),
                    occurred_at=at,
                    metadata={"undelivered_attempts": rec.details.get("undelivered_attempts")},
                )

        elif k == "check_in_missed":
            db.checkins.time_out_open(sid, at=at)
            db.sessions.transition(
                sid,
                "UNCONFIRMED",
                event_id=self._event_id(rec, "state"),
                occurred_at=at,
                metadata={"missed_attempts": rec.details.get("missed_attempts")},
            )

        elif k == "check_in_confirmed":
            via = "VOICE_TOOL" if rec.source == "voice_agent" else "UI_BUTTON"
            db.checkins.confirm(
                sid,
                via=via,
                event_id=self._event_id(rec, "confirm"),
                checkin_id=rec.details.get("checkin_id"),
                at=at,
            )
            db.sessions.transition(
                sid, "ACTIVE", event_id=self._event_id(rec, "state"), occurred_at=at, source=source
            )

        elif k == "check_in_deferred":
            db.events.log_session_event(
                event_id=self._event_id(rec),
                session_id=sid,
                event_type="CHECKIN_DEFERRED",
                source="SAFETY_ENGINE",
                occurred_at=at,
                metadata=rec.details,
            )

        elif k == "alerted":
            reason = rec.details.get("reason")
            explicit_sos = reason == "SOS_EXPLICIT"
            if not explicit_sos:
                db.checkins.time_out_open(sid, at=at)
            db.alerts.raise_alert(
                sid,
                "SOS_EXPLICIT" if explicit_sos else "CHECKIN_TIMEOUT",
                event_id=self._event_id(rec),
                alert_id=rec.details.get("alert_id"),
                source=source,
                recipients=self._recipients,
                occurred_at=at,
            )

        elif k == "alert_resolved":
            self._rounds.pop(sid, None)
            alert = db.alerts.active_for_session(sid)
            if alert is not None:
                db.alerts.resolve(
                    alert["alert_id"],
                    event_id=self._event_id(rec),
                    resolved_by="USER",
                    reason=str(
                        rec.details.get("resolution_action") or rec.reason or "ALERT_RESOLVED"
                    ),
                    source=source,
                )

        elif k == "cancelled":
            self._rounds.pop(sid, None)
            alert = db.alerts.active_for_session(sid) if rec.from_state == "ALERT" else None
            if alert is not None:
                # anularea unei alerte deja transmise = rezolvare explicită, nu ștergere
                db.alerts.resolve(
                    alert["alert_id"],
                    event_id=self._event_id(rec),
                    resolved_by="USER",
                    reason=rec.reason or "CANCELLED_BY_USER",
                    source="COMPANION",
                )
            else:
                db.sessions.cancel(
                    sid, event_id=self._event_id(rec), source="COMPANION", reason=rec.reason
                )

        elif k in ("technical_issue_reported", "technical_issue_resolved"):
            issue = str(rec.details.get("issue"))
            component, on, off = _ISSUES.get(
                issue, ("SERVER", "TECHNICAL_ISSUE", "TECHNICAL_ISSUE_RESOLVED")
            )
            db.events.log_technical_event(
                component=component,
                event_type=on if k == "technical_issue_reported" else off,
                event_id=self._event_id(rec, issue),
                session_id=sid,
                occurred_at=at,
                metadata={"issue": issue, "source": rec.source},
            )

        else:
            log.debug("transition %s not persisted", k)
