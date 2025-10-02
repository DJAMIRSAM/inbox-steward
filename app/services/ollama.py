from __future__ import annotations

import json
import logging
from textwrap import dedent
from typing import Any, Dict

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class OllamaClassifier:
    """Client responsible for prompting the local Ollama model."""

    def __init__(self, endpoint: str | None = None, model: str | None = None) -> None:
        self.endpoint = endpoint or settings.ollama_endpoint
        self.model = model or settings.ollama_model
        self._client = httpx.AsyncClient(timeout=60)

    async def classify(self, prompt_context: Dict[str, Any]) -> Dict[str, Any]:
        prompt = self._build_prompt(prompt_context)
        logger.debug("Sending prompt to Ollama: %s", prompt[:500])
        try:
            response = await self._client.post(
                f"{self.endpoint}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
            )
            response.raise_for_status()
            content = response.json().get("response", "")
            return self._parse_json(content)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Falling back to heuristic classification: %s", exc)
            return self._fallback(prompt_context)

    async def close(self) -> None:
        await self._client.aclose()

    def _build_prompt(self, context: Dict[str, Any]) -> str:
        return dedent(
            f"""
            You are an elite email sorting assistant. You must respond with a single JSON
            object using the schema described below. Do not include markdown fencing.
            Schema:
            {{
              "email_actions": [
                {{
                  "uid": "<message uid>",
                  "destination": "Folder/Path",
                  "sticky": <true|false>,
                  "confidence": 0-1,
                  "reason": "Short explanation"
                }}
              ],
              "calendar": [
                {{
                  "action": "create|update|cancel",
                  "thread_id": "id",
                  "provider": "sender domain or source",
                  "title": "Specific title",
                  "calendar": "Family|Home",
                  "starts_at": "ISO8601",
                  "ends_at": "ISO8601",
                  "timezone": "America/Vancouver",
                  "location": "",
                  "url": "",
                  "notes": "",
                  "uid": "deterministic unique id"
                }}
              ],
              "review": ["uid"],
              "archive": ["uid"],
              "meta": {{"needs_decision": true|false, "reason": ""}}
            }}

            Always respect folder naming rules: root categories like Finance or School and
            concise Title Case leaf folders. Never suggest Inbox or Archive as destinations.

            Email context:
            {json.dumps(context, ensure_ascii=False)}
            """
        )

    def _parse_json(self, payload: str) -> Dict[str, Any]:
        payload = payload.strip()
        if not payload:
            return {}
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("Repairing JSON from model output")
            start = payload.find("{")
            end = payload.rfind("}")
            if start != -1 and end != -1:
                repaired = payload[start : end + 1]
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    logger.error("Unable to repair model JSON: %s", payload)
            return {}

    def _fallback(self, context: Dict[str, Any]) -> Dict[str, Any]:
        subject = context.get("subject", "").lower()
        body = context.get("body", "").lower()
        uid = context.get("uid")
        folder = "Misc"
        if any(keyword in subject for keyword in ["receipt", "invoice", "statement"]):
            folder = "Finance/Receipts"
        elif "newsletter" in subject or "unsubscribe" in body:
            folder = "Newsletters"
        elif any(keyword in subject for keyword in ["appointment", "meeting", "schedule"]):
            folder = "Home/Appointments"
        sticky = folder not in {"Finance/Receipts", "Newsletters"}
        return {
            "email_actions": [
                {
                    "uid": uid,
                    "destination": folder,
                    "sticky": sticky,
                    "confidence": 0.5,
                    "reason": "Fallback heuristic",
                }
            ],
            "meta": {"needs_decision": sticky},
        }


classifier = OllamaClassifier()
