"""Procesele de fundal Tiger Data, pornite din lifespan-ul FastAPI (un singur worker uvicorn):
recuperare la pornire, scrierea tranzițiilor, outbox-ul de notificări și monitorul de alimentare."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from app.integrations.outbox import OutboxDispatcher
from app.integrations.persistence import TigerRecorder
from app.integrations.power import PowerMonitor
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
        power_interval_s: float = 30.0,
    ):
        self.db = db
        self._broker = broker
        self.recorder = TigerRecorder(
            db, recipients=recipients, response_timeout_s=response_timeout_s
        )
        self._outbox_interval = outbox_interval_s
        self._power_interval = power_interval_s
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

        def on_power(st) -> None:
            asyncio.run_coroutine_threadsafe(
                self._broker.publish(
                    "technical.power", data={"state": st.state, "detail": st.detail}
                ),
                loop,
            )

        power = PowerMonitor(self.db, on_change=on_power)
        self._tasks = [
            asyncio.create_task(
                self._loop(outbox.run_once, self._outbox_interval), name="vesta-outbox"
            ),
            asyncio.create_task(
                self._loop(power.run_once, self._power_interval), name="vesta-power"
            ),
        ]

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
