from __future__ import annotations

import base64
import email
import logging
import re
import threading
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError
from msal import (
    ConfidentialClientApplication,
    PublicClientApplication,
    SerializableTokenCache,
)

from app.core.config import settings

logger = logging.getLogger(__name__)


def _ensure_directory(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to create directory for %s", path)


class IMAPEmailBackend:
    """Wrapper around IMAPClient to simplify message retrieval and actions."""

    def __init__(self) -> None:
        self._client: Optional[IMAPClient] = None
        self._folder_cache: Optional[tuple[float, List[str]]] = None

    def connect(self) -> IMAPClient:
        if self._client is None:
            logger.info(
                "Connecting to IMAP %s using %s (%s auth)",
                settings.imap_host,
                settings.imap_encryption,
                settings.imap_auth_type,
            )
            use_ssl = settings.imap_encryption == "SSL"
            client = IMAPClient(
                settings.imap_host,
                port=settings.imap_port,
                ssl=use_ssl,
            )
            if settings.imap_encryption == "STARTTLS":
                try:
                    client.starttls()
                except Exception:  # noqa: BLE001
                    logger.exception("IMAP STARTTLS negotiation failed")
                    try:
                        client.shutdown()
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "IMAP client shutdown after STARTTLS failure raised but was ignored",
                            exc_info=True,
                        )
                    self._client = None
                    raise
            try:
                if settings.imap_auth_type == "XOAUTH2":
                    token = settings.imap_oauth2_token
                    if not token:
                        raise ValueError(
                            "IMAP_OAUTH2_TOKEN must be configured when IMAP_AUTH_TYPE=XOAUTH2"
                        )
                    client.oauth2_login(settings.imap_username, token)
                else:
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
            "encryption": settings.imap_encryption,
            "auth_type": settings.imap_auth_type,
            "ssl": settings.imap_encryption == "SSL",
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


class ExchangeAuthManager:
    """Handles delegated Exchange authentication flows for Microsoft accounts."""

    def __init__(self) -> None:
        self._cache = SerializableTokenCache()
        self._cache_path = Path(settings.exchange_token_cache)
        self._lock = threading.Lock()
        self._load_cache()

    def _authority(self) -> str:
        tenant = settings.exchange_tenant_id
        if settings.exchange_login_mode == "DELEGATED":
            tenant = tenant or "consumers"
        if not tenant:
            raise ValueError("EXCHANGE_TENANT_ID must be configured for Exchange client credential auth")
        return f"{settings.exchange_authority}/{tenant}"

    def _public_client(self) -> PublicClientApplication:
        if not settings.exchange_client_id:
            raise ValueError("EXCHANGE_CLIENT_ID must be configured for Exchange access")
        return PublicClientApplication(
            client_id=settings.exchange_client_id,
            authority=self._authority(),
            token_cache=self._cache,
        )

    def _scopes(self) -> List[str]:
        scopes = settings.exchange_scopes
        if scopes:
            return scopes
        return ["offline_access", "Mail.ReadWrite"]

    def _load_cache(self) -> None:
        try:
            if self._cache_path.exists():
                data = self._cache_path.read_text(encoding="utf-8")
                if data:
                    self._cache.deserialize(data)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to load Exchange token cache from %s", self._cache_path)

    def _persist_cache(self) -> None:
        if not self._cache.has_state_changed:
            return
        try:
            _ensure_directory(self._cache_path)
            payload = self._cache.serialize()
            self._cache_path.write_text(payload, encoding="utf-8")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist Exchange token cache to %s", self._cache_path)

    def current_account(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            app = self._public_client()
            accounts = app.get_accounts()
            return accounts[0] if accounts else None

    def acquire_delegated_token(self) -> tuple[str, int, Dict[str, Any]]:
        with self._lock:
            app = self._public_client()
            accounts = app.get_accounts()
            if not accounts:
                raise RuntimeError(
                    "No Microsoft account is signed in. Run the Microsoft sign-in flow from the Diagnostics tab."
                )
            result: Optional[Dict[str, Any]] = None
            account_info: Optional[Dict[str, Any]] = None
            for account in accounts:
                result = app.acquire_token_silent(self._scopes(), account=account)
                if result and result.get("access_token"):
                    account_info = account
                    break
            else:  # noqa: PLW0120
                raise RuntimeError("Unable to refresh Exchange delegated token. Re-authorize the mailbox.")

            token = result.get("access_token") if result else None
            if not token:
                raise RuntimeError("Failed to acquire Exchange delegated access token.")
            expires_in = int(result.get("expires_in", 3599)) if result else 3599
            self._persist_cache()
            return token, expires_in, account_info or {}

    def initiate_device_flow(self) -> Dict[str, Any]:
        if settings.exchange_login_mode != "DELEGATED":
            raise RuntimeError("Device login is only available when EXCHANGE_LOGIN_MODE=DELEGATED")
        with self._lock:
            app = self._public_client()
            flow = app.initiate_device_flow(scopes=self._scopes())
            if "user_code" not in flow:
                raise RuntimeError(flow.get("error_description") or "Unable to start device login flow")
            return flow

    def complete_device_flow(self, flow: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            app = self._public_client()
            result = app.acquire_token_by_device_flow(flow)  # type: ignore[arg-type]
            if not result or "access_token" not in result:
                error = result.get("error_description") if isinstance(result, dict) else "Authorization failed"
                raise RuntimeError(error or "Authorization failed")
            self._persist_cache()
            return result


exchange_auth_manager = ExchangeAuthManager()


class ExchangeGraphBackend:
    """Microsoft Graph-based mailbox backend for Exchange Online."""

    GRAPH_BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self) -> None:
        self._token: Optional[tuple[str, float]] = None
        self._http = httpx.Client(timeout=settings.exchange_timeout)
        self._folder_cache: Optional[tuple[float, Dict[str, Dict[str, Optional[str]]]]] = None
        self._user_cache: Optional[Dict[str, Any]] = None
        self._auth = exchange_auth_manager
        self._account: Optional[Dict[str, Any]] = None

    def connect(self) -> bool:
        self._ensure_token()
        return True

    def fetch_seen_messages(self) -> List[Dict[str, Any]]:
        messages = self._get_messages(filter="isRead eq true", top=50)
        return [self._parse_message(item) for item in messages]

    def fetch_latest_message(self) -> Optional[Dict[str, Any]]:
        items = self._get_messages(top=1)
        if not items:
            return None
        return self._parse_message(items[0])

    def move(self, uid: int | str, destination: str) -> None:
        folder = self._ensure_folder(destination)
        path = f"{self._user_prefix}/messages/{uid}/move"
        payload = {"destinationId": folder["id"]}
        self._request("POST", path, json=payload)

    def flag(self, uid: int | str) -> None:
        path = f"{self._user_prefix}/messages/{uid}"
        self._request("PATCH", path, json={"flag": {"flagStatus": "flagged"}})

    def unflag(self, uid: int | str) -> None:
        path = f"{self._user_prefix}/messages/{uid}"
        self._request("PATCH", path, json={"flag": {"flagStatus": "notFlagged"}})

    def ensure_folder(self, folder: str) -> Dict[str, Any]:
        parts = [part.strip() for part in folder.split("/") if part.strip()]
        if not parts:
            raise ValueError("Folder path must not be empty")

        folders = self._load_folders()
        current_path = ""
        parent_id: Optional[str] = None
        for part in parts:
            current_path = f"{current_path}/{part}".strip("/")
            existing = folders.get(current_path)
            if existing:
                parent_id = existing["id"]
                continue
            if parent_id:
                path = f"{self._user_prefix}/mailFolders/{parent_id}/childFolders"
            else:
                path = f"{self._user_prefix}/mailFolders"
            created = self._request("POST", path, json={"displayName": part}) or {}
            folder_id = created.get("id")
            if not folder_id:
                raise RuntimeError("Failed to create Exchange folder")
            folders[current_path] = {"id": folder_id, "name": part, "parent": parent_id}
            parent_id = folder_id
        self._folder_cache = (time.monotonic(), folders)
        return folders[current_path]

    def list_folders(self, refresh: bool = False) -> List[str]:
        folders = self._load_folders(force=refresh)
        return sorted(folders.keys())

    def diagnostics(self) -> Dict[str, Any]:
        try:
            self._ensure_token()
            user = self._get_user()
            inbox = self._request(
                "GET",
                f"{self._user_prefix}/mailFolders/inbox",
                params={"$select": "displayName,totalItemCount,unreadItemCount"},
            )
            folders = self._load_folders()
            mailbox_identity = self._mailbox_identity(user)
            return {
                "ok": True,
                "state": "connected",
                "selected_folder": inbox.get("displayName", "Inbox") if isinstance(inbox, dict) else "Inbox",
                "server": "graph.microsoft.com",
                "encryption": "TLS",
                "auth_type": "OAUTH2",
                "mailbox": mailbox_identity,
                "mailbox_status": {
                    "messages": inbox.get("totalItemCount", 0) if isinstance(inbox, dict) else 0,
                    "unseen": inbox.get("unreadItemCount", 0) if isinstance(inbox, dict) else 0,
                },
                "mailbox_error": None,
                "capabilities": ["Graph", "OAuth2", "Folders"],
                "capabilities_error": None,
                "backend": "EXCHANGE",
                "folder_count": len(folders),
                "login_mode": settings.exchange_login_mode,
                "account": mailbox_identity,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("Exchange diagnostics failed")
            return {
                "ok": False,
                "state": "error",
                "server": "graph.microsoft.com",
                "auth_type": "OAUTH2",
                "mailbox": self._account_email() or settings.exchange_user_id or settings.imap_username,
                "mailbox_error": str(exc),
                "capabilities": [],
                "capabilities_error": str(exc),
                "backend": "EXCHANGE",
                "login_mode": settings.exchange_login_mode,
            }

    def reset_connection(self) -> None:
        self._token = None
        self._folder_cache = None
        self._user_cache = None
        self._account = None
        try:
            self._http.close()
        finally:
            self._http = httpx.Client(timeout=settings.exchange_timeout)

    def _ensure_token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._token[1]:
            return self._token[0]

        if settings.exchange_login_mode == "CLIENT":
            if not (
                settings.exchange_client_id
                and settings.exchange_client_secret
                and settings.exchange_tenant_id
            ):
                raise ValueError(
                    "Exchange OAuth credentials are not fully configured. "
                    "Set EXCHANGE_CLIENT_ID, EXCHANGE_CLIENT_SECRET, and EXCHANGE_TENANT_ID."
                )
            authority = f"{settings.exchange_authority}/{settings.exchange_tenant_id}"
            app = ConfidentialClientApplication(
                client_id=settings.exchange_client_id,
                authority=authority,
                client_credential=settings.exchange_client_secret,
            )
            result = app.acquire_token_for_client(scopes=settings.exchange_scopes)
            token = result.get("access_token")
            if not token:
                error = result.get("error_description") or result.get("error") or "unknown error"
                raise RuntimeError(f"Failed to acquire Exchange token: {error}")
            expires_in = int(result.get("expires_in", 3599))
            self._token = (token, now + max(expires_in - 60, 60))
            return token

        token, expires_in, account = self._auth.acquire_delegated_token()
        self._account = account
        self._token = (token, now + max(expires_in - 60, 60))
        return token

    def _headers(self) -> Dict[str, str]:
        token = self._ensure_token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Prefer": 'outlook.body-content-type="text"',
        }

    @property
    def _user_prefix(self) -> str:
        if settings.exchange_login_mode == "DELEGATED" and not settings.exchange_user_id:
            return "/me"
        user = settings.exchange_user_id or settings.imap_username
        if not user:
            raise ValueError("EXCHANGE_USER_ID or IMAP_USERNAME must be configured for Exchange access")
        return f"/users/{user}"

    def _account_email(self) -> Optional[str]:
        if self._account and self._account.get("username"):
            return self._account.get("username")
        account = self._auth.current_account()
        if account:
            return account.get("username")
        return settings.exchange_user_id or settings.imap_username

    def _mailbox_identity(self, user: Optional[Dict[str, Any]]) -> str:
        if settings.exchange_login_mode == "DELEGATED":
            return (self._account_email() or (user or {}).get("mail") or (user or {}).get("userPrincipalName") or "")
        if user:
            return user.get("mail") or user.get("userPrincipalName") or (
                settings.exchange_user_id or settings.imap_username or ""
            )
        return settings.exchange_user_id or settings.imap_username or ""

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        absolute: bool = False,
    ) -> Dict[str, Any]:
        url = path if absolute else f"{self.GRAPH_BASE}{path}"
        response = self._http.request(method, url, params=params, json=json, headers=self._headers())
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:  # noqa: BLE001
            detail = None
            try:
                payload = response.json()
                detail = payload.get("error", {}).get("message")
            except Exception:  # noqa: BLE001
                detail = response.text
            raise RuntimeError(f"Exchange request failed: {detail or exc}") from exc
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def _paginate(
        self, path: str, *, params: Optional[Dict[str, Any]] = None, top: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        if top:
            data = self._request("GET", path, params=params)
            return data.get("value", [])

        items: List[Dict[str, Any]] = []
        next_path: Optional[str] = path
        next_params = params
        while next_path:
            data = self._request("GET", next_path, params=next_params, absolute=next_path.startswith("http"))
            items.extend(data.get("value", []))
            next_link = data.get("@odata.nextLink")
            if not next_link:
                break
            next_path = next_link
            next_params = None
        return items

    def _get_messages(
        self,
        *,
        filter: Optional[str] = None,
        top: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "$select": ",".join(
                [
                    "id",
                    "subject",
                    "from",
                    "toRecipients",
                    "ccRecipients",
                    "body",
                    "bodyPreview",
                    "receivedDateTime",
                    "isRead",
                    "flag",
                    "conversationId",
                    "internetMessageId",
                    "parentFolderId",
                    "webLink",
                    "mimeContent",
                ]
            ),
            "$orderby": "receivedDateTime desc",
        }
        if filter:
            params["$filter"] = filter
        if top:
            params["$top"] = top
        return self._paginate(f"{self._user_prefix}/messages", params=params, top=top)

    def _load_folders(self, force: bool = False) -> Dict[str, Dict[str, Optional[str]]]:
        if self._folder_cache and not force:
            timestamp, cache = self._folder_cache
            if time.monotonic() - timestamp < 300:
                return cache

        data = self._paginate(
            f"{self._user_prefix}/mailFolders",
            params={"$select": "id,displayName,parentFolderId"},
        )
        id_map: Dict[str, Dict[str, Optional[str]]] = {}
        for item in data:
            folder_id = item.get("id")
            if not folder_id:
                continue
            id_map[folder_id] = {
                "id": folder_id,
                "name": item.get("displayName") or "",
                "parent": item.get("parentFolderId"),
            }

        path_map: Dict[str, Dict[str, Optional[str]]] = {}
        for folder_id, info in id_map.items():
            path = self._build_folder_path(folder_id, id_map)
            if path:
                path_map[path] = info

        self._folder_cache = (time.monotonic(), path_map)
        return path_map

    def _build_folder_path(
        self, folder_id: str, id_map: Dict[str, Dict[str, Optional[str]]]
    ) -> Optional[str]:
        names: List[str] = []
        current = folder_id
        visited: set[str] = set()
        while current and current not in visited:
            visited.add(current)
            info = id_map.get(current)
            if not info:
                break
            name = info.get("name")
            if not name:
                break
            names.append(name)
            parent = info.get("parent")
            if not parent or parent not in id_map:
                break
            current = parent
        if not names:
            return None
        return "/".join(reversed(names))

    def _folder_path_for_id(self, folder_id: Optional[str]) -> str:
        if not folder_id:
            return ""
        folders = self._load_folders()
        for path, info in folders.items():
            if info.get("id") == folder_id:
                return path
        folders = self._load_folders(force=True)
        for path, info in folders.items():
            if info.get("id") == folder_id:
                return path
        return ""

    def _parse_message(self, item: Dict[str, Any]) -> Dict[str, Any]:
        subject = item.get("subject") or ""
        sender = self._format_address(item.get("from"))
        to_recipients = self._format_addresses(item.get("toRecipients"))
        cc_recipients = self._format_addresses(item.get("ccRecipients"))
        message_id = item.get("internetMessageId")
        thread_id = item.get("conversationId") or ""
        body_obj = item.get("body") or {}
        body_content = body_obj.get("content") or ""
        body = body_content if body_content else item.get("bodyPreview") or ""
        if isinstance(body_obj, dict) and body_obj.get("contentType", "").lower() == "html":
            body = self._strip_html(body)
        received = item.get("receivedDateTime")
        try:
            if received:
                received_at = datetime.fromisoformat(received.replace("Z", "+00:00")).astimezone(timezone.utc)
            else:
                received_at = datetime.now(timezone.utc)
        except ValueError:
            received_at = datetime.now(timezone.utc)

        raw_b64 = item.get("mimeContent")
        raw = ""
        if raw_b64:
            try:
                raw = base64.b64decode(raw_b64).decode("utf-8", errors="ignore")
            except Exception:  # noqa: BLE001
                raw = ""

        folder_path = self._folder_path_for_id(item.get("parentFolderId"))
        return {
            "uid": str(item.get("id")),
            "subject": subject,
            "sender": sender,
            "to": to_recipients,
            "cc": cc_recipients,
            "message_id": message_id,
            "thread_id": thread_id,
            "body": body,
            "received_at": received_at.isoformat(),
            "raw": raw,
            "folder": folder_path or "",
        }

    def _strip_html(self, value: str) -> str:
        return re.sub(r"<[^>]+>", " ", value)

    def _format_address(self, entry: Optional[Dict[str, Any]]) -> str:
        if not entry or "emailAddress" not in entry:
            return ""
        email_info = entry["emailAddress"]
        address = email_info.get("address") or ""
        name = email_info.get("name") or ""
        if name and address:
            return f"{name} <{address}>"
        return address or name

    def _format_addresses(self, entries: Optional[List[Dict[str, Any]]]) -> str:
        if not entries:
            return ""
        return ", ".join(filter(None, (self._format_address(entry) for entry in entries)))

    def _get_user(self) -> Dict[str, Any]:
        if self._user_cache:
            return self._user_cache
        data = self._request(
            "GET",
            self._user_prefix,
            params={"$select": "id,displayName,mail,userPrincipalName"},
        )
        self._user_cache = data
        return data


class EmailClient:
    """Facade that routes calls to the configured mail backend."""

    def __init__(self) -> None:
        if settings.mail_backend == "EXCHANGE":
            self._backend = ExchangeGraphBackend()
        else:
            self._backend = IMAPEmailBackend()

    @property
    def backend_name(self) -> str:
        return settings.mail_backend

    def connect(self) -> Any:
        return self._backend.connect()

    def fetch_seen_messages(self) -> List[Dict[str, Any]]:
        return self._backend.fetch_seen_messages()

    def fetch_latest_message(self) -> Optional[Dict[str, Any]]:
        return self._backend.fetch_latest_message()

    def move(self, uid: int | str, destination: str) -> None:
        self._backend.move(uid, destination)

    def flag(self, uid: int | str) -> None:
        self._backend.flag(uid)

    def unflag(self, uid: int | str) -> None:
        self._backend.unflag(uid)

    def ensure_folder(self, folder: str) -> Any:
        return self._backend.ensure_folder(folder)

    def list_folders(self, refresh: bool = False) -> List[str]:
        return self._backend.list_folders(refresh)

    def diagnostics(self) -> Dict[str, Any]:
        return self._backend.diagnostics()

    def reset_connection(self) -> None:
        self._backend.reset_connection()


email_client = EmailClient()
