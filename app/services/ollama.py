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
        timezone = context.get("timezone", settings.timezone)
        current_folder = context.get("current_folder") or context.get("folder") or settings.imap_mailbox
        existing_folders = context.get("existing_folders") or []
        existing_block = "\n".join(f"- {folder}" for folder in existing_folders) or "- (none)"
        hints = context.get("hints") or {}
        hints_block = "\n".join(f"- {hint}: {target}" for hint, target in hints.items()) or "- (none)"
        body = (context.get("body") or "").strip()
        if len(body) > 2000:
            body = body[:2000] + "…"
        email_context = dedent(
            f"""
            From: {context.get('sender', '')}
            To: {context.get('to', '')}
            Date: {context.get('received_at', '')}
            Subject: {context.get('subject', '')}

            Body (trimmed):
            {body}
            """
        ).strip()
        return dedent(
            f"""
            You are an email triage engine. Output ONE JSON object only. No prose, no markdown, no code fences.
            If you do not know a field, omit it. Do not include null, empty strings, or placeholder values.
            Use timezone {timezone}. All datetimes must be ISO 8601 with explicit offset (e.g., 2025-10-21T15:00:00-07:00).
            Prefer existing folders unless new_folder=true. Do NOT include any “think” fields inside the JSON.

            ############################
            # OUTPUT CONTRACT (STRICT) #
            ############################
            {{
              "email_actions": {{
                "lane": "quick" | "sticky" | "ignore",
                "folder_path": "Parent/Child[/Grandchild]",
                "new_folder": true | false,
                "create_folder": true | false,
                "move_now": true | false,
                "flag": true | false,
                "due_date": "YYYY-MM-DDTHH:MM:SS-07:00",
                "snooze_until": "YYYY-MM-DDTHH:MM:SS-07:00",
                "confidence": 0.0
              }},

              "calendar": {{
                "create": true,
                "title": "string",
                "start": "YYYY-MM-DDTHH:MM:SS-07:00",
                "end": "YYYY-MM-DDTHH:MM:SS-07:00",
                "timezone": "America/Vancouver",
                "location": "string",
                "url": "string",
                "notes": "string",
                "target_calendar_hint": "Family|Home",
                "confidence": 0.0
              }},

              "review": {{
                "needs_decision": true,
                "reason": "short explanation",
                "options": ["Parent/ChildA","Parent/ChildB"],
                "proposed_name": "NewChild"
              }},

              "archive": {{
                "forward_pdf": true,
                "target_email": "archive@example.com",
                "reason": "short label for why"
              }},

              "meta": {{
                "category": "Receipt|Newsletter|School|Finance|Action|Waiting|Calendar|Family|Work|Other",
                "subtopic": "free text short label",
                "message_hash": "stable-hash"
              }}
            }}

            ######################
            # ROUTING BEHAVIOR   #
            ######################
            - Never set folder_path to "Inbox" or "Archive".
            - If calendar.create=true → lane="quick", move_now=true, flag=false (file immediately to folder_path; create folders if needed).
            - If actionable but not a calendar → lane="sticky", flag=true, move_now=false. Include folder_path as the final destination after USER archives it.
            - Receipts/newsletters/promos → lane="quick", move_now=true, flag=false.
            - If no suitable existing folder and confidence ≥ 0.70 → set new_folder=true, create_folder=true, and propose a concise two-level folder_path. Do not ask for review in this case.
            - Use review.needs_decision=true only for ambiguous choices between two existing folders; provide exactly two options.

            ########################
            # TITLE QUALITY RULES  #
            ########################
            - Titles must be SPECIFIC enough to disambiguate at a glance.
              • Include WHO or WHAT the appointment/task is about (person, organization, subject).
              • Include the TYPE (appointment, service, delivery, exam, etc).
              • Optionally include a SHORT qualifier (e.g., location or provider) if it improves clarity.
            - Avoid generic titles like "Dentist appointment", "Service appointment", "Meeting".
            - Do NOT include sensitive identifiers (VINs, account numbers, tracking IDs) in the TITLE. Place those in calendar.notes if genuinely useful.
            - Keep titles short and human.

            #############################
            # CALENDAR SELECTION RULES  #
            #############################
            - target_calendar_hint must be "Family" or "Home".
            - If the event blocks real-world time (appointments, services, classes, travel) → prefer "Family".
            - If it’s a personal reminder/digital task OR implies privacy → use "Home".
            - If ambiguous, choose "Family" when it avoids double-booking; otherwise "Home".
            - Do not include attendees.

            #########################################
            # CALL CONTEXT                          #
            #########################################
            Timezone: {timezone}
            Current folder: {current_folder}
            Existing folders:
            {existing_block}
            Folder hints:
            {hints_block}

            #########################################
            # EMAIL INPUT                           #
            #########################################
            {email_context}

            #########################################
            # REQUIRED OUTPUT                       #
            #########################################
            Return ONE JSON object only, matching the contract above. No extra text.
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
        folder = None
        category = "Other"
        if any(keyword in subject for keyword in ["receipt", "invoice", "statement"]):
            folder = "Finance/Receipts"
            category = "Finance"
        elif "newsletter" in subject or "unsubscribe" in body:
            folder = "Newsletters"
            category = "Newsletter"
        elif any(keyword in subject for keyword in ["appointment", "meeting", "schedule"]):
            folder = "Home/Appointments"
            category = "Calendar"
        else:
            folder = "Home/Misc"
        lane = "quick" if folder in {"Finance/Receipts", "Newsletters"} else "sticky"
        move_now = lane == "quick"
        return {
            "email_actions": {
                "lane": lane,
                "folder_path": folder,
                "new_folder": False,
                "create_folder": False,
                "move_now": move_now,
                "flag": lane == "sticky",
                "confidence": 0.5,
            },
            "meta": {"category": category},
        }


classifier = OllamaClassifier()
