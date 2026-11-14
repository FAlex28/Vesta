"""Web Push to the emergency contacts' browsers when an alert is raised or resolved.

* Listens to the event broker: ``alert.notification_requested`` -> ALERT_RAISED and
  ``alert.resolved`` -> ALERT_RESOLVED. The safety engine is never blocked by push.
* One push per (alert, kind, subscription): a replayed event or a restart does not resend
  what was already accepted. At most three attempts, then FAILED (no endless resends).
* "SENT" means the browser's push service accepted it, not that the contact saw it; seeing is
  recorded separately when the contact page opens the alert (``viewed_at``).
* The payload has no coordinates: tapping the notification opens /contact, which requires
  the contact's grant.
* A subscription answered with 404/410 is gone and is removed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.realtime.broker import EventBroker
from app.services.directory import DeviceDirectory

LOGGER = logging.getLogger("vesta.push")

Sender = Callable[[dict, str], int]
"""(subscription_info, json_payload) -> HTTP status from the push service."""


def webpush_sender(private_key: str, subject: str) -> Sender:
    def send(subscription: dict, payload: str) -> int:
        from pywebpush import WebPushException, webpush

        try:
            response = webpush(
                subscription_info=subscription,
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": subject},
                ttl=3600,
                timeout=10,
            )
            return int(getattr(response, "status_code", 201))
        except WebPushException as exc:
            response = getattr(exc, "response", None)
            return int(getattr(response, "status_code", 0) or 0)

    return send


class PushNotifier:
    def __init__(
        self,
        directory: DeviceDirectory,
        sender: Sender,
        *,
        attempts: int = 3,
        backoff_seconds: float = 2.0,
    ) -> None:
        self._directory = directory
        self._send = sender
        self._attempts = attempts
        self._backoff = backoff_seconds

    async def run(self, broker: EventBroker, stop_event: asyncio.Event) -> None:
        async with broker.subscribe() as queue:
            while not stop_event.is_set():
                getter = asyncio.create_task(queue.get())
                stopper = asyncio.create_task(stop_event.wait())
                done, _ = await asyncio.wait({getter, stopper}, return_when=asyncio.FIRST_COMPLETED)
                for task in (getter, stopper):
                    if not task.done():
                        task.cancel()
                if getter not in done:
                    return
                event = getter.result()
                kind = {
                    "alert.notification_requested": "ALERT_RAISED",
                    "alert.resolved": "ALERT_RESOLVED",
                }.get(event.type)
                alert_id = event.data.get("alert_id")
                if kind and isinstance(alert_id, str):
                    try:
                        await self.notify(alert_id, kind, event.data)
                    except Exception:
                        LOGGER.exception("push notification failed for %s", alert_id)

    def payload(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        name = self._directory.owner_name() or "Persoana pe care o susții"
        if kind == "ALERT_RAISED":
            reason = (
                "a cerut ajutor (SOS)"
                if data.get("incident_type") == "EXPLICIT_SOS"
                else "nu a răspuns la verificările de siguranță"
            )
            return {
                "title": "Alertă Vesta",
                "body": f"{name} {reason}. Deschide pagina pentru detalii și locație.",
                "url": "/contact",
                "tag": f"vesta-{data.get('alert_id')}",
                "kind": kind,
                "requireInteraction": True,
            }
        return {
            "title": "Alerta Vesta a fost închisă",
            "body": f"{name} a închis alerta.",
            "url": "/contact",
            "tag": f"vesta-{data.get('alert_id')}",
            "kind": kind,
            "requireInteraction": False,
        }

    async def notify(self, alert_id: str, kind: str, data: dict[str, Any]) -> list[str]:
        message = json.dumps(self.payload(kind, {**data, "alert_id": alert_id}), ensure_ascii=False)
        outcomes = []
        for contact_id, key, subscription in self._directory.push_targets():
            record_key = f"{alert_id}:{kind}:{key}"
            previous = self._directory.push_record(record_key)
            if previous and previous["status"] in {"SENT", "FAILED", "GONE"}:
                continue
            status = "FAILED"
            for attempt in range(self._attempts):
                code = await asyncio.to_thread(self._send, subscription, message)
                if 200 <= code < 300:
                    status = "SENT"
                    break
                if code in {404, 410}:
                    status = "GONE"
                    self._directory.remove_subscription(subscription["endpoint"])
                    break
                if attempt + 1 < self._attempts:
                    await asyncio.sleep(self._backoff * (2**attempt))
            self._directory.set_push_record(
                record_key,
                {
                    "alert_id": alert_id,
                    "kind": kind,
                    "contact_id": contact_id,
                    "status": status,
                    "at": datetime.now(UTC).isoformat(),
                },
            )
            outcomes.append(status)
        return outcomes
