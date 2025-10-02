from __future__ import annotations

import logging
from typing import Iterable

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class HomeAssistantNotifier:
    def __init__(self) -> None:
        self.base_url = (settings.ha_base_url or "").rstrip("/") or None
        self.token = (settings.ha_token or "").strip() or None
        self.mobile_target = (settings.ha_mobile_target or "").strip() or None
        self._client = httpx.AsyncClient(timeout=30)

    async def send_decision_request(
        self, message: dict, reason: str, safe_default: str, undo_token: str | None = None
    ) -> None:
        if not self._enabled:
            return
        data = {
            "title": "Email needs decision",
            "message": f"{message.get('subject')}\n{reason}\nDefault: {safe_default}",
            "data": {
                "actions": [
                    {"action": "DEFAULT", "title": safe_default},
                    {"action": "UNDO", "title": "Undo last 24h"},
                ]
            },
        }
        if undo_token:
            data["data"]["url"] = f"/api/undo/{undo_token}"
        await self._send("Decision request", data)

    async def send_conflict(self, conflict: dict) -> None:
        if not self._enabled:
            return
        data = {
            "title": "Calendar conflict detected",
            "message": (
                "Conflicting with {existing_title} at {existing_start}".format(**conflict)
            ),
        }
        await self._send("Calendar conflict", data)

    async def send_digest(self, review_uids: Iterable[str], session_id: str, undo_token: str | None = None) -> None:
        if not self._enabled:
            return
        uids = ", ".join(review_uids)
        data = {
            "title": "Emails waiting for review",
            "message": f"Session {session_id}: {uids}",
        }
        if undo_token:
            data.setdefault("data", {})
            data["data"]["url"] = f"/api/undo/{undo_token}"
        await self._send("Review digest", data)

    async def send_test_notification(self) -> dict[str, str | int | bool]:
        if not self._enabled:
            return {
                "ok": False,
                "error": self._missing_credentials_message(),
            }
        payload = {
            "title": "Inbox Steward connectivity test",
            "message": "This is a debug notification from Inbox Steward.",
            "data": {"tag": "inbox-steward-debug"},
        }
        try:
            response = await self._client.post(
                f"{self.base_url}/api/services/notify/{self.mobile_target}",
                headers={"Authorization": f"Bearer {self.token}"},
                json=payload,
            )
            response.raise_for_status()
            return {"ok": True, "status": response.status_code}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Home Assistant test notification failed")
            return {
                "ok": False,
                "error": f"{exc} (target: {self.mobile_target}, base: {self.base_url})",
            }

    async def check_status(self) -> dict[str, str | int | bool]:
        if not self._enabled:
            return {
                "ok": False,
                "error": self._missing_credentials_message(),
            }
        try:
            response = await self._client.get(
                f"{self.base_url}/api/",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            response.raise_for_status()
            return {"ok": True, "status": response.status_code}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Home Assistant status check failed")
            return {
                "ok": False,
                "error": f"{exc} (base: {self.base_url})",
            }

    async def _send(self, event: str, payload: dict) -> None:
        try:
            response = await self._client.post(
                f"{self.base_url}/api/services/notify/{self.mobile_target}",
                headers={"Authorization": f"Bearer {self.token}"},
                json=payload,
            )
            response.raise_for_status()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send notification %s", event)

    @property
    def _enabled(self) -> bool:
        return bool(self.base_url and self.token and self.mobile_target)

    def _missing_credentials_message(self) -> str:
        missing = []
        if not self.base_url:
            missing.append("HOME_ASSISTANT_BASE_URL")
        if not self.token:
            missing.append("HOME_ASSISTANT_TOKEN")
        if not self.mobile_target:
            missing.append("HOME_ASSISTANT_MOBILE_TARGET")
        joined = ", ".join(missing) if missing else "required Home Assistant settings"
        return f"Missing {joined}."


notifier = HomeAssistantNotifier()
