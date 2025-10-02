from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List

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
    return {
        "email": None,
        "ollama": None,
        "home_assistant": None,
        "folders": None,
        "imap": None,
        "audit": None,
    }


def _service_overview() -> Dict[str, Dict[str, Any]]:
    return {
        "imap": {
            "configured": all([settings.imap_host, settings.imap_username, settings.imap_password]),
            "host": settings.imap_host,
            "mailbox": settings.imap_mailbox,
            "poll": settings.poll_interval_seconds,
            "username": settings.imap_username,
        },
        "ollama": {
            "configured": bool(settings.ollama_endpoint and settings.ollama_model),
            "endpoint": settings.ollama_endpoint,
            "model": settings.ollama_model,
        },
        "home_assistant": {
            "configured": bool(settings.ha_base_url and settings.ha_token and settings.ha_mobile_target),
            "base_url": settings.ha_base_url,
            "target": settings.ha_mobile_target,
        },
        "database": {
            "configured": bool(settings.database_url),
            "url": settings.database_url,
        },
    }


def _mask_secret(value: str | None) -> str:
    if not value:
        return "—"
    cleaned = value.strip()
    if len(cleaned) <= 8:
        return cleaned
    return f"{cleaned[:4]}…{cleaned[-4:]}"


def _environment_snapshot() -> Dict[str, Any]:
    return {
        "home_assistant": {
            "base_url": settings.ha_base_url or "",
            "token": _mask_secret(settings.ha_token),
            "target": settings.ha_mobile_target or "",
            "configured": bool(settings.ha_base_url and settings.ha_token and settings.ha_mobile_target),
        },
        "ollama": {
            "endpoint": settings.ollama_endpoint,
            "model": settings.ollama_model,
        },
    }


def _friendly_imap_error(exc: Exception) -> str:
    message = str(exc)
    if "NONAUTH" in message or "LOGIN failed" in message:
        return "Authentication failed. Verify IMAP username, password, and app password permissions."
    if "Timed out" in message or "timeout" in message.lower():
        return f"Connection to {settings.imap_host}:{settings.imap_port} timed out."
    return message


def _summarize_body(body: str | None) -> str:
    if not body:
        return ""
    snippet = " ".join(line.strip() for line in body.strip().splitlines() if line.strip())
    return snippet[:240]


async def _check_latest_email() -> dict[str, Any]:
    try:
        message = await asyncio.to_thread(email_client.fetch_latest_message)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Email connectivity check failed")
        return {"ok": False, "error": _friendly_imap_error(exc)}
    if not message:
        return {"ok": False, "error": "No messages found in the mailbox."}
    preview = _summarize_body(message.get("body"))
    return {
        "ok": True,
        "message": {
            "uid": message.get("uid"),
            "subject": message.get("subject"),
            "sender": message.get("sender"),
            "received_at": message.get("received_at"),
            "snippet": preview or "(no body content)",
            "folder": message.get("folder"),
        },
    }


async def _list_imap_folders() -> dict[str, Any]:
    try:
        folders: List[str] = await asyncio.to_thread(email_client.list_folders, True)
        return {"ok": True, "folders": folders}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Folder listing failed")
        return {"ok": False, "error": _friendly_imap_error(exc)}


async def _check_ollama_ping() -> dict[str, Any]:
    result = await classifier.ping()
    if not result.get("ok"):
        error = result.get("error", "Unknown Ollama error")
        result = {
            "ok": False,
            "error": f"{error} (endpoint: {settings.ollama_endpoint})",
        }
    return result


async def _check_home_assistant(send_notification: bool = False) -> dict[str, Any]:
    if send_notification:
        return await notifier.send_test_notification()
    return await notifier.check_status()


async def _imap_diagnostics() -> dict[str, Any]:
    try:
        details = await asyncio.to_thread(email_client.diagnostics)
        if not details.get("ok") and "error" not in details:
            details["error"] = details.get("mailbox_error") or details.get("capabilities_error") or "Unknown IMAP error"
        return details
    except Exception as exc:  # noqa: BLE001
        logger.exception("IMAP diagnostics failed")
        return {"ok": False, "error": _friendly_imap_error(exc)}


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
        {
            "request": request,
            "results": _empty_debug_results(),
            "flash": None,
            "overview": _service_overview(),
            "environment": _environment_snapshot(),
        },
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
        result = await _check_latest_email()
        results["email"] = result
        if result.get("ok"):
            message_info: Dict[str, Any] = result.get("message") or {}
            sender = message_info.get("sender", "the latest sender")
            flash = {
                "status": "success",
                "message": f"Fetched latest message from {sender}",
            }
        else:
            flash = {
                "status": "error",
                "message": result.get("error", "Unable to fetch email."),
            }
    elif action == "list_folders":
        result = await _list_imap_folders()
        results["folders"] = result
        if result.get("ok"):
            folders: List[str] = result.get("folders") or []
            label = "folder" if len(folders) == 1 else "folders"
            flash = {
                "status": "success",
                "message": f"Discovered {len(folders)} IMAP {label}.",
            }
        else:
            flash = {
                "status": "error",
                "message": result.get("error", "Unable to list folders."),
            }
    elif action == "test_ollama":
        result = await _check_ollama_ping()
        results["ollama"] = result
        if result.get("ok"):
            flash = {"status": "success", "message": "Ollama responded successfully."}
        else:
            flash = {"status": "error", "message": result.get("error", "Unknown Ollama error")}
    elif action == "test_home_assistant":
        result = await _check_home_assistant(send_notification=True)
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
    elif action == "imap_diagnostics":
        result = await _imap_diagnostics()
        results["imap"] = result
        if result.get("ok"):
            flash = {
                "status": "success",
                "message": "IMAP session is authenticated and mailbox statistics are available.",
            }
        else:
            flash = {
                "status": "error",
                "message": result.get("error", "Unable to inspect IMAP session."),
            }
    elif action == "reset_imap":
        await asyncio.to_thread(email_client.reset_connection)
        results["imap"] = {"ok": True, "reset": True}
        flash = {
            "status": "info",
            "message": "IMAP connection reset. Run a diagnostic to establish a fresh session.",
        }
    elif action == "run_audit":
        email_result = await _check_latest_email()
        folder_result = await _list_imap_folders()
        ollama_result = await _check_ollama_ping()
        ha_result = await _check_home_assistant()
        imap_result = await _imap_diagnostics()
        results.update(
            {
                "email": email_result,
                "folders": folder_result,
                "ollama": ollama_result,
                "home_assistant": ha_result,
                "imap": imap_result,
                "audit": {
                    "imap": imap_result.get("ok", False),
                    "folders": folder_result.get("ok", False),
                    "ollama": ollama_result.get("ok", False),
                    "home_assistant": ha_result.get("ok", False),
                },
            }
        )
        if all(item.get("ok") for item in [email_result, folder_result, ollama_result, ha_result, imap_result]):
            flash = {
                "status": "success",
                "message": "All connectivity checks passed. Inbox Steward is ready to run.",
            }
        else:
            flash = {
                "status": "error",
                "message": "Connectivity audit completed with failures. See details below.",
            }
    else:
        flash = {"status": "error", "message": "Unknown debug action."}

    return templates.TemplateResponse(
        "debug.html",
        {
            "request": request,
            "results": results,
            "flash": flash,
            "overview": _service_overview(),
            "environment": _environment_snapshot(),
        },
    )
