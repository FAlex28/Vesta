from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.api.routes import router
from app.config import Settings
from app.container import AppServices, build_services
from app.errors import AppError

LOGGER = logging.getLogger("vesta")


async def _watch_power(services: AppServices, stop_event: asyncio.Event) -> None:
    """Raspberry Pi power from `vcgencmd get_throttled`, published on every change.

    No battery percentage exists: a power bank does not report one to the Pi. A severe
    under-voltage can switch the Pi off before any notification leaves it.
    """

    from app.integrations.power import read_power_status

    last_state: str | None = None
    while not stop_event.is_set():
        try:
            status = await asyncio.to_thread(read_power_status)
            now = datetime.now(UTC)
            services.power = {"state": status.state, "detail": status.detail, "checked_at": now}
            if status.state != last_state:
                last_state = status.state
                await services.broker.publish(
                    "technical.power",
                    data={"state": status.state, "detail": status.detail},
                )
                if services.persistence is not None:
                    services.persistence.record_power(status)
        except Exception:
            LOGGER.exception("power status read failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                stop_event.wait(), timeout=services.settings.power_poll_seconds
            )


def _push_notifier(services: AppServices):
    from app.integrations.push import PushNotifier, webpush_sender

    if services.push_sender is not None:
        return PushNotifier(services.directory, services.push_sender, backoff_seconds=0.01)
    settings = services.settings

    def send(subscription: dict, payload: str) -> int:
        private = settings.vapid_private_key or services.directory.vapid()["private_key"]
        return webpush_sender(private, settings.vapid_subject)(subscription, payload)

    return PushNotifier(services.directory, send)


def _mount_frontend(app: FastAPI, dist_path: str) -> None:
    """Serve frontend/dist from this process: /, /companion and /contact share one origin."""

    dist = Path(dist_path).resolve()
    index = dist / "index.html"
    if not index.is_file():
        raise RuntimeError(f"VESTA_FRONTEND_DIST has no index.html: {dist}")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str) -> FileResponse:
        if path.startswith(("api/", "ws/")) or path in {"api", "ws", "health"}:
            raise HTTPException(status_code=404)
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(dist):
            headers = (
                {"Cache-Control": "public, max-age=31536000, immutable"}
                if path.startswith("assets/")
                else {"Cache-Control": "no-cache"}
            )
            return FileResponse(candidate, headers=headers)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


def create_app(
    settings: Settings | None = None,
    *,
    services: AppServices | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    resolved_services = services or build_services(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.services = resolved_services
        persistence = resolved_services.persistence
        if persistence is not None:
            # înainte de scheduler: recuperarea după restart rulează înaintea oricărei activări
            await persistence.start()
        stop_event = asyncio.Event()
        scheduler: asyncio.Task[None] | None = None
        hardware_task: asyncio.Task[None] | None = None
        power_task: asyncio.Task[None] | None = None
        push_task: asyncio.Task[None] | None = None
        try:
            await resolved_services.sessions.recover_unfinished()
            if resolved_services.hardware_monitor is not None:
                # The gpiozero callback only queues events. Session work is handled
                # asynchronously by this single event-loop coordinator.
                resolved_services.hardware_monitor.start()
                if resolved_services.hardware_coordinator is None:
                    raise RuntimeError("Hardware monitor has no activation coordinator")
                hardware_task = asyncio.create_task(
                    resolved_services.hardware_coordinator.run(stop_event),
                    name="vesta-gpio-activation-coordinator",
                )
            push_task = asyncio.create_task(
                _push_notifier(resolved_services).run(resolved_services.broker, stop_event),
                name="vesta-push",
            )
            if resolved_settings.environment in {"development", "test"} and not resolved_settings.device_pairing_code:
                code = resolved_services.directory.device_code(None)
                LOGGER.warning("Device pairing code (development only): %s-%s", code[:5], code[5:])
            if resolved_settings.gpio_mode == "gpio":
                power_task = asyncio.create_task(
                    _watch_power(resolved_services, stop_event), name="vesta-power"
                )
            if resolved_settings.scheduler_enabled:
                scheduler = asyncio.create_task(
                    resolved_services.sessions.run_scheduler(stop_event),
                    name="vesta-safety-scheduler",
                )
            if resolved_settings.control_secret_is_ephemeral:
                LOGGER.warning(
                    "VESTA_CONTROL_SIGNING_SECRET is unset; control tokens will not survive restart"
                )
            yield
        finally:
            stop_event.set()
            if resolved_services.hardware_monitor is not None:
                resolved_services.hardware_monitor.stop()
            tasks = [
                task for task in (scheduler, hardware_task, power_task, push_task) if task is not None
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await resolved_services.voice.aclose()
            if persistence is not None:
                await persistence.stop()

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        description=(
            "Backendul Vesta pentru sesiuni, conversație vocală și reguli deterministe "
            "de siguranță. Acesta este un prototip demonstrativ, nu un serviciu de urgență."
        ),
        lifespan=lifespan,
    )
    app.state.services = resolved_services
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved_settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Dashboard-Key",
            "X-Device-Key",
        ],
    )

    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    app.include_router(router)
    if resolved_settings.frontend_dist:
        _mount_frontend(app, resolved_settings.frontend_dist)
    return app


app = create_app()
