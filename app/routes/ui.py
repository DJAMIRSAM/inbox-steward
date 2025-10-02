from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
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


DEBUG_PROGRESS_STEPS: Dict[str, Dict[str, Any]] = {
    "run_audit": {
        "title": "Run connectivity audit",
        "steps": [
            {"key": "audit_email", "label": "Fetch latest mailbox message", "status": "pending"},
            {"key": "audit_folders", "label": "List IMAP folders", "status": "pending"},
            {"key": "audit_ollama", "label": "Ping Ollama endpoint", "status": "pending"},
            {"key": "audit_home_assistant", "label": "Check Home Assistant status", "status": "pending"},
            {"key": "audit_imap", "label": "Inspect IMAP session", "status": "pending"},
            {"key": "audit_summary", "label": "Summarize audit results", "status": "pending"},
        ],
    },
    "test_email": {
        "title": "Fetch latest mailbox message",
        "steps": [
            {"key": "email_connect", "label": "Connect to IMAP", "status": "pending"},
            {"key": "email_fetch", "label": "Retrieve latest message", "status": "pending"},
        ],
    },
    "list_folders": {
        "title": "Refresh IMAP folder tree",
        "steps": [
            {"key": "folders_connect", "label": "Connect to IMAP", "status": "pending"},
            {"key": "folders_fetch", "label": "Fetch folder listing", "status": "pending"},
        ],
    },
    "imap_diagnostics": {
        "title": "Inspect IMAP diagnostics",
        "steps": [
            {"key": "imap_connect", "label": "Connect to IMAP", "status": "pending"},
            {"key": "imap_inspect", "label": "Collect session details", "status": "pending"},
        ],
    },
    "reset_imap": {
        "title": "Reset IMAP connection",
        "steps": [
            {"key": "imap_reset", "label": "Reset cached IMAP session", "status": "pending"},
        ],
    },
    "test_ollama": {
        "title": "Ping Ollama model",
        "steps": [
            {"key": "ollama_request", "label": "Contact Ollama endpoint", "status": "pending"},
            {"key": "ollama_response", "label": "Process model reply", "status": "pending"},
        ],
    },
    "test_home_assistant": {
        "title": "Send Home Assistant notification",
        "steps": [
            {"key": "ha_prepare", "label": "Prepare notification payload", "status": "pending"},
            {"key": "ha_send", "label": "Send request to Home Assistant", "status": "pending"},
            {"key": "ha_confirm", "label": "Await confirmation", "status": "pending"},
        ],
    },
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


@dataclass
class DebugStepState:
    key: str
    label: str
    status: str = "pending"
    detail: Optional[str] = None


@dataclass
class DebugJobState:
    id: str
    action: str
    status: str = "pending"
    steps: List[DebugStepState] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    flash: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "status": self.status,
            "steps": [
                {
                    "key": step.key,
                    "label": step.label,
                    "status": step.status,
                    "detail": step.detail,
                }
                for step in self.steps
            ],
            "error": self.error,
        }


class ProgressTracker:
    def __init__(self, job: DebugJobState):
        self.job = job
        self.lookup = {step.key: step for step in job.steps}

    def start(self, key: str, detail: Optional[str] = None) -> None:
        step = self.lookup.get(key)
        if not step:
            return
        step.status = "running"
        if detail:
            step.detail = detail

    def complete(self, key: str, detail: Optional[str] = None) -> None:
        step = self.lookup.get(key)
        if not step:
            return
        step.status = "complete"
        if detail:
            step.detail = detail

    def fail(self, key: str, detail: Optional[str] = None) -> None:
        step = self.lookup.get(key)
        if not step:
            return
        step.status = "error"
        if detail:
            step.detail = detail


_debug_jobs: Dict[str, DebugJobState] = {}
_debug_job_lock = asyncio.Lock()


def _build_step_states(action: str) -> List[DebugStepState]:
    definition = DEBUG_PROGRESS_STEPS.get(action, {})
    steps = definition.get("steps", [])
    return [DebugStepState(step["key"], step["label"]) for step in steps]


async def _register_job(action: str) -> DebugJobState:
    job = DebugJobState(id=str(uuid4()), action=action, steps=_build_step_states(action))
    async with _debug_job_lock:
        now = datetime.utcnow()
        expired = [job_id for job_id, details in _debug_jobs.items() if now - details.created_at > timedelta(minutes=10)]
        for job_id in expired:
            _debug_jobs.pop(job_id, None)
        _debug_jobs[job.id] = job
    return job


async def _get_job(job_id: str) -> Optional[DebugJobState]:
    async with _debug_job_lock:
        return _debug_jobs.get(job_id)


async def _update_job(job: DebugJobState) -> None:
    async with _debug_job_lock:
        _debug_jobs[job.id] = job


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


async def _perform_debug_action(
    action: str, progress: Optional[ProgressTracker] = None
) -> tuple[dict[str, dict[str, object] | None], Optional[Dict[str, Any]]]:
    results = _empty_debug_results()
    flash: Optional[Dict[str, Any]] = None

    def start(key: str, detail: Optional[str] = None) -> None:
        if progress:
            progress.start(key, detail)

    def complete(key: str, detail: Optional[str] = None) -> None:
        if progress:
            progress.complete(key, detail)

    def fail(key: str, detail: Optional[str] = None) -> None:
        if progress:
            progress.fail(key, detail)

    if action == "test_email":
        start("email_connect", "Connecting to mailbox…")
        result = await _check_latest_email()
        if result.get("ok"):
            complete("email_connect", "Connection succeeded")
            start("email_fetch", "Fetching latest message…")
            complete("email_fetch", "Message retrieved")
            message_info: Dict[str, Any] = result.get("message") or {}
            sender = message_info.get("sender", "the latest sender")
            flash = {"status": "success", "message": f"Fetched latest message from {sender}"}
        else:
            fail("email_connect", result.get("error"))
            flash = {"status": "error", "message": result.get("error", "Unable to fetch email.")}
        results["email"] = result
    elif action == "list_folders":
        start("folders_connect", "Connecting to mailbox…")
        result = await _list_imap_folders()
        if result.get("ok"):
            complete("folders_connect", "Connection succeeded")
            start("folders_fetch", "Retrieving folders…")
            complete("folders_fetch", f"Found {len(result.get('folders') or [])} folders")
            folders: List[str] = result.get("folders") or []
            label = "folder" if len(folders) == 1 else "folders"
            flash = {"status": "success", "message": f"Discovered {len(folders)} IMAP {label}."}
        else:
            fail("folders_connect", result.get("error"))
            flash = {"status": "error", "message": result.get("error", "Unable to list folders.")}
        results["folders"] = result
    elif action == "test_ollama":
        start("ollama_request", "Contacting Ollama…")
        result = await _check_ollama_ping()
        if result.get("ok"):
            complete("ollama_request", "Endpoint reachable")
            start("ollama_response", "Processing response…")
            complete("ollama_response", "Received response")
            flash = {"status": "success", "message": "Ollama responded successfully."}
        else:
            fail("ollama_request", result.get("error"))
            flash = {"status": "error", "message": result.get("error", "Unknown Ollama error")}
        results["ollama"] = result
    elif action == "test_home_assistant":
        start("ha_prepare", "Preparing notification payload…")
        complete("ha_prepare", "Payload ready")
        start("ha_send", "Sending request to Home Assistant…")
        result = await _check_home_assistant(send_notification=True)
        if result.get("ok"):
            complete("ha_send", "Request accepted")
            start("ha_confirm", "Confirming delivery…")
            complete("ha_confirm", f"HTTP {result.get('status', '200')}")
            flash = {"status": "success", "message": "Test notification sent through Home Assistant."}
        else:
            fail("ha_send", result.get("error"))
            fail("ha_confirm", "Awaiting confirmation failed")
            flash = {"status": "error", "message": result.get("error", "Home Assistant notification failed.")}
        results["home_assistant"] = result
    elif action == "imap_diagnostics":
        start("imap_connect", "Connecting to mailbox…")
        result = await _imap_diagnostics()
        if result.get("ok"):
            complete("imap_connect", "Authenticated")
            start("imap_inspect", "Collecting diagnostics…")
            complete("imap_inspect", "Session details collected")
            flash = {"status": "success", "message": "IMAP session is authenticated and mailbox statistics are available."}
        elif result.get("reset"):
            complete("imap_connect", "Connection reset")
            flash = {"status": "info", "message": "IMAP connection reset. Run a diagnostic to establish a fresh session."}
        else:
            fail("imap_connect", result.get("error"))
            fail("imap_inspect", result.get("error"))
            flash = {"status": "error", "message": result.get("error", "Unable to inspect IMAP session.")}
        results["imap"] = result
    elif action == "reset_imap":
        start("imap_reset", "Clearing cached session…")
        await asyncio.to_thread(email_client.reset_connection)
        complete("imap_reset", "Connection reset")
        results["imap"] = {"ok": True, "reset": True}
        flash = {"status": "info", "message": "IMAP connection reset. Run a diagnostic to establish a fresh session."}
    elif action == "run_audit":
        start("audit_email", "Fetching latest message…")
        email_result = await _check_latest_email()
        if email_result.get("ok"):
            complete("audit_email", "Latest message retrieved")
        else:
            fail("audit_email", email_result.get("error"))

        start("audit_folders", "Listing mailbox folders…")
        folder_result = await _list_imap_folders()
        if folder_result.get("ok"):
            complete("audit_folders", f"Found {len(folder_result.get('folders') or [])} folders")
        else:
            fail("audit_folders", folder_result.get("error"))

        start("audit_ollama", "Pinging Ollama…")
        ollama_result = await _check_ollama_ping()
        if ollama_result.get("ok"):
            complete("audit_ollama", "Ollama reachable")
        else:
            fail("audit_ollama", ollama_result.get("error"))

        start("audit_home_assistant", "Checking Home Assistant…")
        ha_result = await _check_home_assistant()
        if ha_result.get("ok"):
            complete("audit_home_assistant", f"HTTP {ha_result.get('status', '200')}")
        else:
            fail("audit_home_assistant", ha_result.get("error"))

        start("audit_imap", "Gathering IMAP diagnostics…")
        imap_result = await _imap_diagnostics()
        if imap_result.get("ok"):
            complete("audit_imap", "IMAP diagnostics ready")
        else:
            fail("audit_imap", imap_result.get("error"))

        start("audit_summary", "Summarizing results…")
        audit_summary = {
            "imap": imap_result.get("ok", False),
            "folders": folder_result.get("ok", False),
            "ollama": ollama_result.get("ok", False),
            "home_assistant": ha_result.get("ok", False),
        }
        complete("audit_summary", "Summary ready")

        results.update(
            {
                "email": email_result,
                "folders": folder_result,
                "ollama": ollama_result,
                "home_assistant": ha_result,
                "imap": imap_result,
                "audit": audit_summary,
            }
        )

        if all(item.get("ok") for item in [email_result, folder_result, ollama_result, ha_result, imap_result]):
            flash = {"status": "success", "message": "All connectivity checks passed. Inbox Steward is ready to run."}
        else:
            flash = {"status": "error", "message": "Connectivity audit completed with failures. See details below."}
    else:
        flash = {"status": "error", "message": "Unknown debug action."}

    return results, flash


async def _run_debug_job(job: DebugJobState) -> None:
    tracker = ProgressTracker(job)
    job.status = "running"
    await _update_job(job)
    try:
        results, flash = await _perform_debug_action(job.action, tracker)
        job.result = {"results": results, "flash": flash}
        job.flash = flash
        job.status = "completed"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Debug job failed")
        job.status = "failed"
        job.error = str(exc)
    finally:
        await _update_job(job)


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
            "progress_steps": DEBUG_PROGRESS_STEPS,
        },
    )


@router.post("/debug", response_class=HTMLResponse)
async def debug_tools_run(
    request: Request,
    templates: Jinja2Templates = Depends(get_templates),
    action: str = Form(...),
) -> HTMLResponse:
    results, flash = await _perform_debug_action(action)

    return templates.TemplateResponse(
        "debug.html",
        {
            "request": request,
            "results": results,
            "flash": flash,
            "overview": _service_overview(),
            "environment": _environment_snapshot(),
            "progress_steps": DEBUG_PROGRESS_STEPS,
        },
    )

@router.post("/debug/run")
async def debug_run(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    action = payload.get("action")
    if not action:
        raise HTTPException(status_code=400, detail="Missing debug action")
    job = await _register_job(action)
    asyncio.create_task(_run_debug_job(job))
    return {"job_id": job.id}


@router.get("/debug/status/{job_id}")
async def debug_status(job_id: str, request: Request) -> JSONResponse:
    job = await _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    payload = job.to_dict()
    if job.status in {"completed", "failed"}:
        context_data = job.result or {"results": _empty_debug_results(), "flash": job.flash}
        html = templates.get_template("partials/debug_content.html").render(
            {
                "request": request,
                "results": context_data.get("results", _empty_debug_results()),
                "flash": context_data.get("flash"),
                "overview": _service_overview(),
                "environment": _environment_snapshot(),
            }
        )
        payload["html"] = html
    return JSONResponse(payload)
