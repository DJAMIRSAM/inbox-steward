from __future__ import annotations

import email
import logging
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from typing import Any, Dict, List, Optional

from imapclient import IMAPClient

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmailClient:
    """Wrapper around IMAPClient to simplify message retrieval and actions."""

    def __init__(self) -> None:
        self._client: Optional[IMAPClient] = None

    def connect(self) -> IMAPClient:
        if self._client is None:
            logger.info("Connecting to IMAP %s", settings.imap_host)
            self._client = IMAPClient(
                settings.imap_host,
                port=settings.imap_port,
                ssl=settings.imap_use_ssl,
            )
            self._client.login(settings.imap_username, settings.imap_password)
        return self._client

    def fetch_seen_messages(self) -> List[Dict[str, Any]]:
        client = self.connect()
        client.select_folder(settings.imap_mailbox)
        uids = client.search(["SEEN"])
        if not uids:
            return []
        response = client.fetch(uids, ["RFC822", "FLAGS", "ENVELOPE", "BODYSTRUCTURE"])
        messages: List[Dict[str, Any]] = []
        for uid, data in response.items():
            raw_message: bytes = data.get(b"RFC822", b"")
            msg = email.message_from_bytes(raw_message)
            parsed = self._parse_message(uid, msg, data)
            messages.append(parsed)
        return messages

    def move(self, uid: int | str, destination: str) -> None:
        client = self.connect()
        logger.info("Moving message %s -> %s", uid, destination)
        self.ensure_folder(destination)
        client.move(uid, destination)

    def flag(self, uid: int | str) -> None:
        client = self.connect()
        client.add_flags(uid, ["\\Flagged"])

    def unflag(self, uid: int | str) -> None:
        client = self.connect()
        client.remove_flags(uid, ["\\Flagged"])

    def ensure_folder(self, folder: str) -> None:
        client = self.connect()
        existing = {item[2] for item in client.list_folders()}
        if folder in existing:
            return
        parts = folder.split("/")
        for i in range(1, len(parts) + 1):
            subfolder = "/".join(parts[:i])
            if subfolder not in existing:
                logger.info("Creating folder %s", subfolder)
                client.create_folder(subfolder)
                existing.add(subfolder)

    def _parse_message(self, uid: int, message: Message, metadata: Dict[bytes, Any]) -> Dict[str, Any]:
        subject = self._decode(message.get("Subject", ""))
        sender = self._decode(message.get("From", ""))
        to_recipients = self._decode(message.get("To", ""))
        cc_recipients = self._decode(message.get("Cc", ""))
        message_id = message.get("Message-Id")
        thread_id = self._thread_id(message)
        body_text = self._extract_text(message)
        received = metadata.get(b"INTERNALDATE")
        if isinstance(received, datetime):
            received_at = received.astimezone(timezone.utc)
        else:
            received_at = datetime.now(timezone.utc)
        return {
            "uid": str(uid),
            "subject": subject,
            "sender": sender,
            "to": to_recipients,
            "cc": cc_recipients,
            "message_id": message_id,
            "thread_id": thread_id,
            "body": body_text,
            "received_at": received_at.isoformat(),
            "raw": message.as_string(),
            "folder": settings.imap_mailbox,
        }

    def _decode(self, value: str) -> str:
        if not value:
            return ""
        return str(make_header(decode_header(value)))

    def _thread_id(self, message: Message) -> str:
        references = message.get_all("References", [])
        in_reply_to = message.get("In-Reply-To")
        raw = " ".join(references + ([in_reply_to] if in_reply_to else []))
        return re.sub(r"\s+", " ", raw.strip()) or (message.get("Message-Id") or "")

    def _extract_text(self, message: Message) -> str:
        if message.is_multipart():
            parts: Iterable[Message] = message.walk()
        else:
            parts = [message]
        chunks: List[str] = []
        for part in parts:
            content_type = part.get_content_type()
            if content_type in {"text/plain", "text/html"}:
                try:
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="ignore")
                    if content_type == "text/html":
                        text = self._strip_html(text)
                    chunks.append(text)
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to decode message part")
        return "\n".join(chunks)

    def _strip_html(self, html: str) -> str:
        return re.sub(r"<[^>]+>", " ", html)


email_client = EmailClient()
