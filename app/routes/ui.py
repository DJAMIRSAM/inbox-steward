from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app.core.config import settings
from app.core.database import get_session
from app.models import CalendarEvent, ConflictLog, EmailMessage
from app.services.actions import processor

router = APIRouter()

templates = Jinja2Templates(directory="app/web/templates")
templates.env.globals.update(settings=settings, current_year=datetime.now().year)


def get_templates() -> Jinja2Templates:
    return templates


def _format_time(value: datetime | None) -> str:
    if not value:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M")


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
    return templates.TemplateResponse("what_if.html", {"request": request, "plan": plan})
