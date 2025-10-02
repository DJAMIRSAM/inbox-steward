from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from typing import Any, Dict, Iterable, List, Tuple

from sqlmodel import select

from app.core.config import settings
from app.core.database import get_session
from app.models import ActionLog, EmailMessage, FolderHint, UndoToken
from app.services.calendar import CalendarService
from app.services.email_client import email_client
from app.services.notifications import notifier
from app.services.ollama import classifier
from app.services.rules import FolderNamer

logger = logging.getLogger(__name__)


class ActionProcessor:
    def __init__(self) -> None:
        self.calendar_service = CalendarService()
        self.folder_namer = FolderNamer()

    async def process_seen_messages(self) -> None:
        messages = email_client.fetch_seen_messages()
        if not messages:
            return
        logger.info("Processing %s seen messages", len(messages))
        for message in messages:
            await self._handle_message(message)
        logger.debug("Processed %s messages", len(messages))

    async def _handle_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        context = self._build_prompt_context(message)
        classification = await classifier.classify(context)
        session_id = self._session_id(message)
        self._persist_email(message, classification, session_id)
        await self._apply_actions(message, classification, session_id)
        token = self._ensure_undo_token(session_id)
        if token:
            logger.debug("Undo token ready for session %s", session_id)
        return classification

    def _build_prompt_context(self, message: Dict[str, Any]) -> Dict[str, Any]:
        with get_session() as session:
            hints = session.exec(
                select(FolderHint).where(FolderHint.hint == message.get("sender", ""))
            ).all()
        hint_payload = {hint.hint: hint.folder for hint in hints}
        return {**message, "hints": hint_payload}

    def _persist_email(self, message: Dict[str, Any], classification: Dict[str, Any], session_id: str) -> None:
        with get_session() as session:
            db_obj = session.get(EmailMessage, message["uid"])
            if not db_obj:
                received_at = datetime.fromisoformat(message["received_at"])
                if received_at.tzinfo:
                    received_at = received_at.astimezone(UTC)
                else:
                    received_at = received_at.replace(tzinfo=UTC)
                db_obj = EmailMessage(
                    uid=message["uid"],
                    message_id=message.get("message_id"),
                    thread_id=message.get("thread_id"),
                    subject=message.get("subject", ""),
                    sender=message.get("sender", ""),
                    to_recipients=message.get("to", ""),
                    cc_recipients=message.get("cc", ""),
                    received_at=received_at,
                    folder=message.get("folder", settings.imap_mailbox),
                )
            db_obj.classification = classification
            db_obj.updated_at = datetime.now(UTC)
            db_obj.folder = message.get("folder", settings.imap_mailbox)
            meta = classification.get("meta") or {}
            db_obj.needs_decision = bool(meta.get("needs_decision"))
            db_obj.session_id = session_id
            target_folder = None
            for action in classification.get("email_actions", []):
                if action.get("uid") == message.get("uid"):
                    target_folder = self.folder_namer.normalize(action.get("destination", ""))
                    break
            db_obj.target_folder = target_folder
            session.add(db_obj)
            session.commit()

    async def _apply_actions(
        self, message: Dict[str, Any], classification: Dict[str, Any], session_id: str
    ) -> None:
        actions = classification.get("email_actions", [])
        review = set(classification.get("review", []) or [])
        archive = set(classification.get("archive", []) or [])
        meta = classification.get("meta") or {}
        sticky_targets: List[Tuple[str, Dict[str, Any]]] = []
        for action in actions:
            destination = self.folder_namer.normalize(action.get("destination", ""))
            confidence = float(action.get("confidence", 0))
            uid = action.get("uid")
            if not uid:
                continue
            if confidence < 0.4:
                undo_token = self._ensure_undo_token(session_id)
                await notifier.send_decision_request(
                    message,
                    reason="Low confidence folder",
                    safe_default="Stick with Inbox",
                    undo_token=undo_token,
                )
                self._log_action(session_id, uid, "decision_request", action)
                continue
            if action.get("sticky"):
                sticky_targets.append((uid, action))
                email_client.flag(uid)
            else:
                email_client.unflag(uid)
                email_client.move(uid, destination)
                self._persist_folder_hint(message, destination, confidence)
                log_payload = {**action, "source": message.get("folder", settings.imap_mailbox)}
                self._log_action(session_id, uid, "move", log_payload)
            if uid in archive:
                email_client.move(uid, destination)
        if meta.get("needs_decision") and sticky_targets:
            undo_token = self._ensure_undo_token(session_id)
            await notifier.send_decision_request(
                message,
                reason=meta.get("reason", "Needs review"),
                safe_default="Keep flagged",
                undo_token=undo_token,
            )
        await self._handle_calendar(classification.get("calendar", []), session_id)
        if review:
            undo_token = self._ensure_undo_token(session_id)
            await notifier.send_digest(review, session_id, undo_token)

    async def _handle_calendar(self, calendar_actions: Iterable[Dict[str, Any]], session_id: str) -> None:
        for action in calendar_actions:
            result = self.calendar_service.apply(action)
            self._log_action(session_id, action.get("uid", ""), "calendar", result)
            if result.get("conflict"):
                await notifier.send_conflict(result)

    def _persist_folder_hint(self, message: Dict[str, Any], folder: str, confidence: float) -> None:
        hint_key = message.get("sender", "")
        if not hint_key:
            return
        with get_session() as session:
            hint = session.exec(
                select(FolderHint).where(FolderHint.hint == hint_key, FolderHint.folder == folder)
            ).first()
            if not hint:
                hint = FolderHint(hint=hint_key, folder=folder, weight=confidence)
            else:
                hint.weight = min(hint.weight + confidence, 5.0)
                hint.last_used_at = datetime.now(UTC)
            session.add(hint)
            session.commit()

    def _log_action(self, session_id: str, uid: str, action_type: str, payload: Dict[str, Any]) -> None:
        with get_session() as session:
            log_entry = ActionLog(session_id=session_id, email_uid=uid, action_type=action_type, payload=payload)
            session.add(log_entry)
            session.commit()

    def _session_id(self, message: Dict[str, Any]) -> str:
        digest = hashlib.sha256()
        digest.update(message.get("thread_id", "").encode())
        digest.update(message.get("uid", "").encode())
        digest.update(str(datetime.now(UTC).date()).encode())
        return digest.hexdigest()

    def full_sort(self) -> Dict[str, Any]:
        logger.info("Running full sort sweep")
        with get_session() as session:
            emails = session.exec(select(EmailMessage)).all()
        plan = defaultdict(list)
        for email_obj in emails:
            classification = email_obj.classification or {}
            for action in classification.get("email_actions", []):
                destination = self.folder_namer.normalize(action.get("destination", ""))
                plan[destination].append(email_obj.uid)
                email_client.move(email_obj.uid, destination)
        return {"moves": dict(plan)}

    def what_if(self) -> Dict[str, Any]:
        logger.info("Generating what-if plan")
        with get_session() as session:
            emails = session.exec(select(EmailMessage)).all()
        plan = []
        for email_obj in emails:
            classification = email_obj.classification or {}
            actions = classification.get("email_actions", [])
            for action in actions:
                plan.append(
                    {
                        "uid": email_obj.uid,
                        "subject": email_obj.subject,
                        "destination": self.folder_namer.normalize(action.get("destination", "")),
                        "confidence": action.get("confidence"),
                    }
                )
        return {"plan": plan, "count": len(plan)}

    def undo(self, token: str) -> bool:
        with get_session() as session:
            undo_token = session.exec(select(UndoToken).where(UndoToken.token == token)).first()
            if not undo_token:
                return False
            logs = session.exec(select(ActionLog).where(ActionLog.session_id == undo_token.session_id)).all()
            for log in logs:
                if log.action_type == "move" and log.payload:
                    source_folder = log.payload.get("source") or settings.imap_mailbox
                    email_client.move(log.email_uid, source_folder)
            session.delete(undo_token)
            session.commit()
        return True

    def _ensure_undo_token(self, session_id: str) -> str | None:
        now = datetime.now(UTC)
        expires_at = now + timedelta(days=1)
        with get_session() as session:
            token_obj = session.exec(
                select(UndoToken).where(UndoToken.session_id == session_id, UndoToken.expires_at > now)
            ).first()
            if token_obj:
                return token_obj.token
            token_value = token_urlsafe(16)
            undo_token = UndoToken(session_id=session_id, token=token_value, expires_at=expires_at)
            session.add(undo_token)
            session.commit()
            return token_value


processor = ActionProcessor()
