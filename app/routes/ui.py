from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app.core.config import settings
from app.core.database import get_session
from app.models import CalendarEvent, ConflictLog, EmailMessage
from app.services.actions import processor
from app.services.email_client import email_client
from app.services.notifications import notifier
from app.services.ollama import classifier

router = APIRouter()

templates = Jinja2Templates(directory="app/web/templates")
templates.env.globals.update(settings=settings, current_year=datetime.now().year)

logger = logging.getLogger(__name__)


def get_templates() -> Jinja2Templates:
    return templates


def _format_time(value: datetime | None) -> str:
    if not value:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M")


def _empty_debug_results() -> dict[str, dict[str, object] | None]:
    return {"email": None, "ollama": None, "home_assistant": None}


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, templates: Jinja2Templates = Depends(get_templates)) -> HTMLResponse:
    with get_session() as session:
        pending = session.exec(select(EmailMessage).where(EmailMessage.status == "pending")).all()
        needs_decision = session.exec(select(EmailMessage).where(EmailMessage.needs_decision == True)).all()  # noqa: E712
        recent_events = (
            session.exec(select(CalendarEvent).order_by(CalendarEvent.updated_at.desc()).limit(5)).all()
        )
        conflicts = session.exec(select(ConflictLog).where(ConflictLog.resolved == False)).all()  # noqa: E712
    stats = {
        "pending": len(pending),
        "needs_decision": len(needs_decision),
        "recent_events": recent_events,
        "conflicts": conflicts,
    }
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "stats": stats, "format_time": _format_time},
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, templates: Jinja2Templates = Depends(get_templates)) -> HTMLResponse:
    return templates.TemplateResponse("settings.html", {"request": request, "settings": settings})


@router.get("/what-if", response_class=HTMLResponse)
async def what_if_page(request: Request, templates: Jinja2Templates = Depends(get_templates)) -> HTMLResponse:
    plan = processor.what_if()
    return templates.TemplateResponse("what_if.html", {"request": request, "plan": plan, "flash": None})


@router.post("/what-if/full-sort", response_class=HTMLResponse)
async def run_full_sort(request: Request, templates: Jinja2Templates = Depends(get_templates)) -> HTMLResponse:
    result = processor.full_sort()
    moved = sum(len(uids) for uids in result.get("moves", {}).values())
    plan = processor.what_if()
    if moved:
        message = f"Filed {moved} message{'s' if moved != 1 else ''} using the latest plan."
        flash = {"status": "success", "message": message}
    else:
        flash = {"status": "info", "message": "No messages required moving. Inbox is already in harmony."}
    return templates.TemplateResponse("what_if.html", {"request": request, "plan": plan, "flash": flash})


@router.get("/debug", response_class=HTMLResponse)
async def debug_tools(request: Request, templates: Jinja2Templates = Depends(get_templates)) -> HTMLResponse:
    return templates.TemplateResponse(
        "debug.html",
        {"request": request, "results": _empty_debug_results(), "flash": None},
    )


@router.post("/debug", response_class=HTMLResponse)
async def debug_tools_run(
    request: Request,
    templates: Jinja2Templates = Depends(get_templates),
    action: str = Form(...),
) -> HTMLResponse:
    results = _empty_debug_results()
    flash = None

    if action == "test_email":
        try:
            message = await asyncio.to_thread(email_client.fetch_latest_message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Email connectivity check failed")
            message = None
            results["email"] = {"ok": False, "error": str(exc)}
        else:
            if message:
                snippet = (message.get("body") or "").strip().splitlines()
                preview = " ".join(line.strip() for line in snippet if line.strip())[:240]
                results["email"] = {
                    "ok": True,
                    "message": {
                        "uid": message.get("uid"),
                        "subject": message.get("subject"),
                        "sender": message.get("sender"),
                        "received_at": message.get("received_at"),
                        "snippet": preview,
                    },
                }
                flash = {
                    "status": "success",
                    "message": f"Fetched latest message from {message.get('sender', 'unknown sender')}",
                }
            else:
                results["email"] = {"ok": False, "error": "No messages found in the mailbox."}
                flash = {"status": "info", "message": "Mailbox is empty or inaccessible."}
    elif action == "test_ollama":
        result = await classifier.ping()
        results["ollama"] = result
        if result.get("ok"):
            flash = {"status": "success", "message": "Ollama responded successfully."}
        else:
            flash = {"status": "error", "message": result.get("error", "Unknown Ollama error")}
    elif action == "test_home_assistant":
        result = await notifier.send_test_notification()
        results["home_assistant"] = result
        if result.get("ok"):
            flash = {
                "status": "success",
                "message": "Test notification sent through Home Assistant.",
            }
        else:
            flash = {
                "status": "error",
                "message": result.get("error", "Home Assistant notification failed."),
            }
    else:
        flash = {"status": "error", "message": "Unknown debug action."}

    return templates.TemplateResponse(
        "debug.html",
        {"request": request, "results": results, "flash": flash},
    )
