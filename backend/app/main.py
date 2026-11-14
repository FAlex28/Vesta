from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import Settings
from app.container import AppServices, build_services
from app.errors import AppError

LOGGER = logging.getLogger("vesta")


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
        stop_event = asyncio.Event()
        scheduler: asyncio.Task[None] | None = None
        hardware_task: asyncio.Task[None] | None = None
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
            tasks = [task for task in (scheduler, hardware_task) if task is not None]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await resolved_services.voice.aclose()

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
    return app


app = create_app()
