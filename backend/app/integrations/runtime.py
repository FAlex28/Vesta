"""Procesele de fundal Tiger Data, pornite din lifespan-ul FastAPI (un singur worker uvicorn):
recuperare la pornire, scrierea tranzițiilor, outbox-ul de notificări și monitorul de alimentare."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from app.integrations.heartbeat import HeartbeatMonitor
from app.integrations.outbox import OutboxDispatcher
from app.integrations.persistence import TigerRecorder
from app.integrations.power import PowerStatus
from app.integrations.startup import recover_on_startup

log = logging.getLogger("vesta.persistence")


class TigerPersistence:
    def __init__(
        self,
        db,
        broker,
        *,
        recipients: tuple[str, ...],
        response_timeout_s: float,
        outbox_interval_s: float = 1.0,
        heartbeat_sweep_s: float = 5.0,
    ):
        self.db = db
        self._broker = broker
        self.recorder = TigerRecorder(
            db, recipients=recipients, response_timeout_s=response_timeout_s
        )
        self._outbox_interval = outbox_interval_s
        self._heartbeat_sweep = heartbeat_sweep_s
        self._side_tasks: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self.recovery: dict | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            self.recovery = await asyncio.to_thread(recover_on_startup, self.db)
            await self._broker.publish(
                "system.recovered",
                data={
                    "sessions_interrupted": len(self.recovery["interrupted"]),
                    "alerts_still_active": [
                        str(a["alert_id"]) for a in self.recovery["active_alerts"]
                    ],
                },
            )
        except Exception:
            # fără DB, sesiunile și SOS continuă în memorie; doar istoricul lipsește
            log.exception("Tiger Data startup recovery failed; continuing without history")
        await self.recorder.start()

        def send(notification: dict) -> None:
            # Fluxul /ws/events este autentificat; din minimizarea datelor publicăm doar
            # starea locației, nu coordonatele contactului în payload-ul outbox.
            p = notification.get("payload") or {}
            fut = asyncio.run_coroutine_threadsafe(
                self._broker.publish(
                    "contact.notification",
                    session_id=p.get("session_id"),
                    data={
                        "notification_id": str(notification["notification_id"]),
                        "alert_id": str(notification["alert_id"]),
                        "kind": notification["kind"],
                        "recipient_id": notification["recipient_id"],
                        "incident_type": p.get("incident_type"),
                        "location_status": (p.get("location") or {}).get("status"),
                    },
                ),
                loop,
            )
            fut.result(timeout=5)

        outbox = OutboxDispatcher(self.db, send)
        # Server-side sweep of companion_clients: CONNECTED -> RECONNECTING -> DISCONNECTED
        # history (PHONE_HEARTBEAT_MISSED / PHONE_DISCONNECTED). Safety decisions stay in
        # SessionManager's lease; this only records the timeline. It never raises SOS.
        heartbeat = HeartbeatMonitor(self.db)
        # Power is read by app.main for every persistence mode; see record_power().
        self._tasks = [
            asyncio.create_task(
                self._loop(outbox.run_once, self._outbox_interval), name="vesta-outbox"
            ),
            asyncio.create_task(
                self._loop(heartbeat.run_once, self._heartbeat_sweep), name="vesta-heartbeat-sweep"
            ),
        ]

    # -- fire-and-forget history writes (a database outage never blocks the API) --------

    def _background(self, fn, *args, **kwargs) -> None:
        async def run() -> None:
            try:
                await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=10)
            except Exception:
                log.exception("Tiger Data write failed: %s", getattr(fn, "__qualname__", fn))

        try:
            task = asyncio.get_running_loop().create_task(run())
        except RuntimeError:
            return
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)

    def record_companion_heartbeat(self, *, device_id: str, session_id: str, visibility: str) -> None:
        self._background(
            self.db.companions.heartbeat,
            f"phone:{device_id}",
            device_id,
            session_id=session_id,
            visibility=visibility if visibility in {"VISIBLE", "HIDDEN"} else "UNKNOWN",
        )

    def record_alert_viewed(self, alert_id: str) -> None:
        def mark() -> None:
            for row in self.db.notifications.for_alert(alert_id):
                if row.get("kind") in (None, "ALERT_RAISED"):
                    self.db.notifications.mark_viewed(row["notification_id"])

        self._background(mark)

    def record_power(self, status: PowerStatus) -> None:
        self._background(
            self.db.events.log_technical_event,
            component="POWER",
            event_type=status.state,
            metadata={
                "raw": hex(status.raw) if status.raw is not None else None,
                "thermal_throttling": status.thermal_throttling,
                "detail": status.detail,
            },
        )

    async def _loop(self, fn, interval: float) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(fn)
            except Exception:
                log.exception("%s failed", getattr(fn, "__qualname__", fn))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=interval)

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            with contextlib.suppress(Exception):
                await t
        await self.recorder.stop()
        await asyncio.to_thread(self.db.close)
