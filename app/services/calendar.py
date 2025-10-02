from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any, Dict, Optional

from sqlmodel import select

from app.core.config import settings
from app.core.database import get_session
from app.models import CalendarEvent, ConflictLog

logger = logging.getLogger(__name__)


class CalendarService:
    FAMILY = "Family"
    HOME = "Home"

    def apply(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        action = payload.get("action", "create")
        if action == "create":
            return self._create(payload)
        if action == "update":
            return self._update(payload)
        if action == "cancel":
            return self._cancel(payload)
        return {"status": "ignored", "reason": "Unknown action"}

    def _create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        uid = payload.get("uid") or self._deterministic_uid(payload)
        starts_at = self._parse_dt(payload.get("starts_at"))
        ends_at = self._parse_dt(payload.get("ends_at"))
        calendar = payload.get("calendar", self.HOME)
        conflict = self._detect_conflict(calendar, starts_at, ends_at, uid)
        if conflict:
            self._log_conflict(calendar, payload, conflict)
        with get_session() as session:
            existing = session.exec(select(CalendarEvent).where(CalendarEvent.uid == uid)).first()
            if existing:
                return self._update({**payload, "uid": uid})
            event = CalendarEvent(
                uid=uid,
                thread_id=payload.get("thread_id"),
                provider=payload.get("provider"),
                title=payload.get("title"),
                calendar=calendar,
                starts_at=starts_at,
                ends_at=ends_at,
                timezone=payload.get("timezone", settings.timezone),
                location=payload.get("location"),
                url=payload.get("url"),
                notes=payload.get("notes"),
                raw_payload=payload,
            )
            session.add(event)
            session.commit()
        return {"status": "created", "uid": uid, "conflict": conflict}

    def _update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        uid = payload.get("uid") or self._deterministic_uid(payload)
        starts_at = self._parse_dt(payload.get("starts_at"))
        ends_at = self._parse_dt(payload.get("ends_at"))
        calendar = payload.get("calendar", self.HOME)
        conflict = self._detect_conflict(calendar, starts_at, ends_at, uid)
        with get_session() as session:
            event = session.exec(select(CalendarEvent).where(CalendarEvent.uid == uid)).first()
            if not event:
                return self._create({**payload, "uid": uid})
            event.title = payload.get("title", event.title)
            event.calendar = calendar
            event.starts_at = starts_at or event.starts_at
            event.ends_at = ends_at or event.ends_at
            event.location = payload.get("location", event.location)
            event.url = payload.get("url", event.url)
            event.notes = payload.get("notes", event.notes)
            event.timezone = payload.get("timezone", event.timezone)
            event.raw_payload = payload
            event.updated_at = datetime.now(UTC)
            session.add(event)
            session.commit()
        return {"status": "updated", "uid": uid, "conflict": conflict}

    def _cancel(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        uid = payload.get("uid") or self._deterministic_uid(payload)
        with get_session() as session:
            event = session.exec(select(CalendarEvent).where(CalendarEvent.uid == uid)).first()
            if not event:
                return {"status": "missing", "uid": uid}
            session.delete(event)
            session.commit()
        return {"status": "cancelled", "uid": uid}

    def _deterministic_uid(self, payload: Dict[str, Any]) -> str:
        digest = hashlib.sha256()
        digest.update((payload.get("provider") or "").encode())
        digest.update((payload.get("title") or "").encode())
        digest.update((payload.get("starts_at") or "").encode())
        digest.update((payload.get("location") or "").encode())
        return digest.hexdigest()

    def _parse_dt(self, value: Optional[str]) -> datetime:
        if not value:
            return datetime.now(UTC)
        normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
        return datetime.fromisoformat(normalized).astimezone(UTC)

    def _detect_conflict(
        self, calendar: str, start: datetime, end: datetime, uid: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        with get_session() as session:
            events = session.exec(select(CalendarEvent).where(CalendarEvent.calendar == calendar)).all()
        for event in events:
            if uid and event.uid == uid:
                continue
            if event.starts_at < end and start < event.ends_at:
                return {
                    "existing_uid": event.uid,
                    "existing_title": event.title,
                    "existing_start": event.starts_at.isoformat(),
                }
        return None

    def _log_conflict(self, calendar: str, payload: Dict[str, Any], conflict: Dict[str, Any]) -> None:
        with get_session() as session:
            log = ConflictLog(calendar=calendar, conflict_type="calendar", details={"payload": payload, "conflict": conflict})
            session.add(log)
            session.commit()
