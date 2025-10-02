from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.database import init_db
from app.core.logging import configure_logging
from app.routes import api, ui
from app.services.actions import processor

configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN001
    init_db()
    task = asyncio.create_task(_background_poll())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(Exception):
            await task


async def _background_poll() -> None:
    while True:
        await processor.process_seen_messages()
        await asyncio.sleep(settings.poll_interval_seconds)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
app.include_router(ui.router)
app.include_router(api.router)


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
