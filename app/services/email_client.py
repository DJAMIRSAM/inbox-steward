from __future__ import annotations

import email
import logging
import re
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from typing import Any, Dict, List, Optional

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmailClient:
    """Wrapper around IMAPClient to simplify message retrieval and actions."""

    def __init__(self) -> None:
        self._client: Optional[IMAPClient] = None
        self._folder_cache: Optional[tuple[float, List[str]]] = None

    def connect(self) -> IMAPClient:
        if self._client is None:
            logger.info("Connecting to IMAP %s", settings.imap_host)
            client = IMAPClient(
                settings.imap_host,
                port=settings.imap_port,
                ssl=settings.imap_use_ssl,
            )
            try:
                client.login(settings.imap_username, settings.imap_password)
            except Exception:  # noqa: BLE001
                logger.exception("IMAP login failed")
                try:
                    client.shutdown()
                except Exception:  # noqa: BLE001
                    logger.debug("IMAP client shutdown raised but was ignored", exc_info=True)
                self._client = None
                raise
            self._client = client
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

    def fetch_latest_message(self) -> Optional[Dict[str, Any]]:
        client = self.connect()
        client.select_folder(settings.imap_mailbox)
        uids = client.search(["ALL"])
        if not uids:
            return None
        latest_uid = max(uids)
        response = client.fetch([latest_uid], ["RFC822", "FLAGS", "ENVELOPE", "BODYSTRUCTURE"])
        data = response.get(latest_uid)
        if not data:
            return None
        raw_message: bytes = data.get(b"RFC822", b"")
        msg = email.message_from_bytes(raw_message)
        return self._parse_message(latest_uid, msg, data)

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
        self._folder_cache = None

    def list_folders(self, refresh: bool = False) -> List[str]:
        if self._folder_cache and not refresh:
            timestamp, cached = self._folder_cache
            if time.monotonic() - timestamp < 300:
                return cached
        client = self.connect()
        folders = sorted(item[2] for item in client.list_folders())
        self._folder_cache = (time.monotonic(), folders)
        return folders

    def diagnostics(self) -> Dict[str, Any]:
        client = self.connect()
        state = getattr(getattr(client, "_imap", None), "state", None)
        if hasattr(state, "name"):
            state_name = state.name  # type: ignore[attr-defined]
        else:
            state_name = str(state) if state else "unknown"

        capabilities: List[str] = []
        capabilities_error: Optional[str] = None
        try:
            raw_caps = client.capabilities() or []
            capabilities = sorted(
                cap.decode() if isinstance(cap, bytes) else str(cap)
                for cap in raw_caps
            )
        except Exception as exc:  # noqa: BLE001
            capabilities_error = str(exc)

        mailbox_status: Dict[str, Any] = {}
        mailbox_error: Optional[str] = None
        try:
            status = client.folder_status(
                settings.imap_mailbox,
                what=("MESSAGES", "RECENT", "UNSEEN"),
            )
            mailbox_status = {
                (key.decode() if isinstance(key, bytes) else str(key)).lower(): int(value)
                for key, value in status.items()
            }
        except IMAPClientError as exc:
            mailbox_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            mailbox_error = str(exc)

        try:
            selected = client.get_selected_folder()
            if isinstance(selected, bytes):
                selected_folder = selected.decode()
            else:
                selected_folder = selected
        except Exception:  # noqa: BLE001
            selected_folder = None

        return {
            "ok": mailbox_error is None,
            "state": state_name,
            "selected_folder": selected_folder,
            "server": f"{settings.imap_host}:{settings.imap_port}",
            "ssl": settings.imap_use_ssl,
            "mailbox": settings.imap_mailbox,
            "mailbox_status": mailbox_status,
            "mailbox_error": mailbox_error,
            "capabilities": capabilities,
            "capabilities_error": capabilities_error,
        }

    def reset_connection(self) -> None:
        if self._client is not None:
            try:
                self._client.logout()
            except Exception:  # noqa: BLE001
                try:
                    self._client.shutdown()
                except Exception:  # noqa: BLE001
                    logger.debug("IMAP client shutdown during reset raised", exc_info=True)
            finally:
                self._client = None
        self._folder_cache = None

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
