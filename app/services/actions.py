from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from typing import Any, Dict, Optional

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
        if messages:
            logger.info("Processing %s seen messages", len(messages))
            for message in messages:
                await self._handle_message(message)
            logger.debug("Processed %s messages", len(messages))
        await self._process_archive_followups()

    async def _handle_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        context = self._build_prompt_context(message)
        classification = await classifier.classify(context)
        session_id = self._session_id(message)
        record = self._persist_email(message, classification, session_id)
        session_id = record.session_id or session_id
        await self._apply_actions(message, classification, session_id)
        token = self._ensure_undo_token(session_id)
        if token:
            logger.debug("Undo token ready for session %s", session_id)
        return classification

    async def _process_archive_followups(self) -> None:
        archive_folder = settings.imap_archive_mailbox
        if not archive_folder:
            return
        try:
            messages = email_client.fetch_flagged_messages(archive_folder)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to fetch flagged messages from %s", archive_folder)
            return
        if not messages:
            return
        logger.info("Processing %s flagged messages from %s", len(messages), archive_folder)
        for message in messages:
            snapshot = self._load_email_snapshot(message)
            existing_session = snapshot.get("session_id") if snapshot else None
            classification = snapshot.get("classification") if snapshot else None
            session_id = existing_session or self._session_id(message)
            if not classification:
                context = self._build_prompt_context(message)
                classification = await classifier.classify(context)
            record = self._persist_email(message, classification or {}, session_id)
            session_id = record.session_id or session_id
            await self._apply_actions(message, classification or {}, session_id)

    def _build_prompt_context(self, message: Dict[str, Any]) -> Dict[str, Any]:
        with get_session() as session:
            hints = session.exec(
                select(FolderHint).where(FolderHint.hint == message.get("sender", ""))
            ).all()
        hint_payload = {hint.hint: hint.folder for hint in hints}
        try:
            existing_folders = email_client.list_folders()
        except Exception:  # noqa: BLE001
            logger.exception("Unable to fetch folder list from IMAP")
            existing_folders = []
        return {
            **message,
            "hints": hint_payload,
            "existing_folders": existing_folders,
            "timezone": settings.timezone,
            "current_folder": message.get("folder", settings.imap_mailbox),
        }

    def _persist_email(
        self, message: Dict[str, Any], classification: Dict[str, Any], session_id: str
    ) -> EmailMessage:
        uid = str(message.get("uid")) if message.get("uid") is not None else None
        message_id = message.get("message_id")
        now = datetime.now(UTC)
        with get_session() as session:
            db_obj = session.get(EmailMessage, uid) if uid else None
            if not db_obj and message_id:
                db_obj = session.exec(
                    select(EmailMessage).where(EmailMessage.message_id == message_id)
                ).first()
                if db_obj and uid:
                    db_obj.uid = uid
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
            db_obj.updated_at = now
            db_obj.last_seen_at = now
            db_obj.folder = message.get("folder", settings.imap_mailbox)
            meta = classification.get("meta") or {}
            review = classification.get("review") or {}
            action = self._extract_email_action(classification)
            low_confidence = bool(action) and (action.get("confidence") or 0.0) < 0.4
            db_obj.needs_decision = bool(review.get("needs_decision")) or low_confidence
            db_obj.session_id = session_id
            target_folder = None
            if action and action.get("folder_path"):
                target_folder = action.get("folder_path")
            db_obj.target_folder = target_folder
            session.add(db_obj)
            session.commit()
            session.refresh(db_obj)
            return db_obj

    def _load_email_snapshot(self, message: Dict[str, Any]) -> Dict[str, Any] | None:
        uid = str(message.get("uid")) if message.get("uid") is not None else None
        message_id = message.get("message_id")
        with get_session() as session:
            db_obj = session.get(EmailMessage, uid) if uid else None
            if not db_obj and message_id:
                db_obj = session.exec(
                    select(EmailMessage).where(EmailMessage.message_id == message_id)
                ).first()
            if not db_obj:
                return None
            return {
                "uid": db_obj.uid,
                "message_id": db_obj.message_id,
                "classification": db_obj.classification,
                "session_id": db_obj.session_id,
                "sender": db_obj.sender,
                "folder": db_obj.folder,
            }

    async def _apply_actions(
        self, message: Dict[str, Any], classification: Dict[str, Any], session_id: str
    ) -> None:
        action = self._extract_email_action(classification)
        review = classification.get("review") or {}
        archive = classification.get("archive") or {}
        meta = classification.get("meta") or {}
        if action:
            await self._execute_email_action(message, action, session_id, meta)
        if review.get("needs_decision"):
            undo_token = self._ensure_undo_token(session_id)
            options = review.get("options") or []
            safe_default = options[0] if options else "Keep in Inbox"
            reason = review.get("reason") or "Needs your decision"
            await notifier.send_decision_request(
                message,
                reason=reason,
                safe_default=safe_default,
                undo_token=undo_token,
            )
            self._log_action(
                session_id,
                message.get("uid", ""),
                "decision_request",
                {"reason": reason, "options": options, "proposed_name": review.get("proposed_name")},
            )
        if archive:
            self._log_action(session_id, message.get("uid", ""), "archive", archive)
        if meta:
            self._log_action(session_id, message.get("uid", ""), "meta", meta)
        await self._handle_calendar(classification.get("calendar"), message, session_id)

    async def _handle_calendar(
        self, calendar_payload: Dict[str, Any] | None, message: Dict[str, Any], session_id: str
    ) -> None:
        if not isinstance(calendar_payload, dict) or not calendar_payload:
            return
        confidence = float(calendar_payload.get("confidence") or 0.0)
        if confidence and confidence < 0.4:
            undo_token = self._ensure_undo_token(session_id)
            await notifier.send_decision_request(
                message,
                reason="Calendar action below confidence threshold",
                safe_default="Skip calendar update",
                undo_token=undo_token,
            )
            self._log_action(
                session_id,
                message.get("uid", ""),
                "calendar_pending",
                {"confidence": confidence, "payload": calendar_payload},
            )
            return
        action_type = calendar_payload.get("action")
        if not action_type:
            if calendar_payload.get("cancel"):
                action_type = "cancel"
            elif calendar_payload.get("create", True):
                action_type = "create"
            else:
                action_type = "update"
        payload = {
            "action": action_type,
            "thread_id": message.get("thread_id"),
            "provider": message.get("sender"),
            "title": calendar_payload.get("title"),
            "calendar": calendar_payload.get("target_calendar_hint", CalendarService.HOME),
            "starts_at": calendar_payload.get("start"),
            "ends_at": calendar_payload.get("end"),
            "timezone": calendar_payload.get("timezone", settings.timezone),
            "location": calendar_payload.get("location"),
            "url": calendar_payload.get("url"),
            "notes": calendar_payload.get("notes"),
            "uid": calendar_payload.get("uid"),
        }
        result = self.calendar_service.apply(payload)
        combined_payload = {**payload, **result}
        self._log_action(session_id, message.get("uid", ""), "calendar", combined_payload)
        conflict = result.get("conflict")
        if conflict:
            await notifier.send_conflict(conflict)

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
        plan = defaultdict(list)
        self._apply_archive_followups(plan)
        with get_session() as session:
            emails = session.exec(select(EmailMessage)).all()
        for email_obj in emails:
            classification = email_obj.classification or {}
            action = self._extract_email_action(classification)
            if not action:
                continue
            destination = action.get("folder_path")
            if not destination or action.get("lane") == "ignore":
                continue
            if action.get("lane") == "sticky" and not action.get("move_now"):
                continue
            email_client.ensure_folder(destination)
            email_client.move(email_obj.uid, destination)
            email_client.unflag(email_obj.uid)
            self._persist_folder_hint(
                {"sender": email_obj.sender, "folder": email_obj.folder},
                destination,
                action.get("confidence") or 0.0,
            )
            plan[destination].append(email_obj.uid)
        return {"moves": dict(plan)}

    def _apply_archive_followups(self, plan: defaultdict[str, list[str]]) -> None:
        archive_folder = settings.imap_archive_mailbox
        if not archive_folder:
            return
        try:
            messages = email_client.fetch_flagged_messages(archive_folder)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to sweep flagged messages from %s", archive_folder)
            return
        for message in messages:
            snapshot = self._load_email_snapshot(message)
            if not snapshot:
                continue
            classification = snapshot.get("classification") or {}
            action = self._extract_email_action(classification)
            if not action:
                continue
            destination = action.get("folder_path")
            if not destination:
                continue
            try:
                email_client.ensure_folder(destination)
                email_client.move(message["uid"], destination)
                email_client.unflag(message["uid"])
            except Exception:  # noqa: BLE001
                logger.exception("Failed to move %s to %s during archive sweep", message.get("uid"), destination)
                continue
            self._persist_folder_hint(
                {"sender": snapshot.get("sender", ""), "folder": message.get("folder", archive_folder)},
                destination,
                action.get("confidence") or 0.0,
            )
            plan[destination].append(str(message.get("uid")))
            session_id = snapshot.get("session_id") or self._session_id(message)
            self._persist_email(
                {**message, "folder": destination},
                classification,
                session_id,
            )

    def what_if(self) -> Dict[str, Any]:
        logger.info("Generating what-if plan")
        with get_session() as session:
            emails = session.exec(select(EmailMessage)).all()
        plan = []
        for email_obj in emails:
            classification = email_obj.classification or {}
            action = self._extract_email_action(classification)
            if not action or not action.get("folder_path"):
                continue
            plan.append(
                {
                    "uid": email_obj.uid,
                    "subject": email_obj.subject,
                    "destination": action.get("folder_path"),
                    "lane": action.get("lane") or "unknown",
                    "move_now": action.get("move_now"),
                    "flag": action.get("flag"),
                    "confidence": action.get("confidence"),
                }
            )
        plan.sort(key=lambda item: (item["lane"] or "", item["uid"]))
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

    def _extract_email_action(self, classification: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        action = classification.get("email_actions")
        if isinstance(action, list):
            action = action[0] if action else None
        if not isinstance(action, dict) or not action:
            return None
        folder_path_raw = action.get("folder_path") or ""
        folder_path = self.folder_namer.normalize(folder_path_raw) if folder_path_raw else None
        confidence = action.get("confidence")
        try:
            confidence_value = float(confidence) if confidence is not None else 0.0
        except (TypeError, ValueError):
            confidence_value = 0.0
        return {
            "lane": action.get("lane", "ignore"),
            "folder_path": folder_path,
            "raw_folder_path": folder_path_raw,
            "new_folder": bool(action.get("new_folder")),
            "create_folder": bool(action.get("create_folder")),
            "move_now": bool(action.get("move_now")),
            "flag": bool(action.get("flag")),
            "confidence": confidence_value,
            "due_date": action.get("due_date"),
            "snooze_until": action.get("snooze_until"),
        }

    async def _execute_email_action(
        self, message: Dict[str, Any], action: Dict[str, Any], session_id: str, meta: Dict[str, Any]
    ) -> None:
        uid = message.get("uid")
        if not uid:
            return
        lane = action.get("lane") or "ignore"
        destination = action.get("folder_path")
        confidence = action.get("confidence") or 0.0
        current_folder = message.get("folder", settings.imap_mailbox)
        move_now_raw = action.get("move_now")
        if move_now_raw is None:
            move_now = lane == "quick"
        else:
            move_now = bool(move_now_raw)
        flag_raw = action.get("flag")
        if flag_raw is None:
            flag = lane == "sticky"
        else:
            flag = bool(flag_raw)
        if lane == "sticky" and current_folder not in {settings.imap_mailbox, None}:
            move_now = True
            flag = False
        if lane == "ignore":
            logger.debug("Skipping ignore lane for %s", uid)
            return
        if confidence < 0.4:
            undo_token = self._ensure_undo_token(session_id)
            await notifier.send_decision_request(
                message,
                reason="Low confidence folder selection",
                safe_default="Leave in Inbox",
                undo_token=undo_token,
            )
            self._log_action(
                session_id,
                uid,
                "decision_request",
                {"reason": "low_confidence", "confidence": confidence, "destination": destination},
            )
            return
        if not destination:
            logger.warning("No destination folder provided for %s", uid)
            return
        if action.get("new_folder") or action.get("create_folder"):
            email_client.ensure_folder(destination)
        moved = False
        if lane == "sticky":
            if flag:
                email_client.flag(uid)
            else:
                email_client.unflag(uid)
            if move_now:
                email_client.ensure_folder(destination)
                email_client.move(uid, destination)
                moved = True
        else:
            if flag:
                email_client.flag(uid)
            else:
                email_client.unflag(uid)
            if move_now:
                email_client.ensure_folder(destination)
                email_client.move(uid, destination)
                moved = True
        log_payload = {
            "lane": lane,
            "destination": destination,
            "move_now": move_now,
            "flag": flag,
            "confidence": confidence,
            "meta": meta,
            "source": message.get("folder", settings.imap_mailbox),
        }
        if action.get("due_date"):
            log_payload["due_date"] = action.get("due_date")
        if action.get("snooze_until"):
            log_payload["snooze_until"] = action.get("snooze_until")
        action_type = "move" if moved else "plan"
        self._log_action(session_id, uid, action_type, log_payload)
        if moved:
            self._persist_folder_hint(message, destination, confidence)


processor = ActionProcessor()
