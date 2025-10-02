from __future__ import annotations

import re
from typing import Dict

ROOT_FOLDERS = {
    "School",
    "Finance",
    "Newsletters",
    "Vehicle",
    "Health",
    "Work",
    "Family",
    "Home",
}


class FolderNamer:
    pattern = re.compile(r"[^A-Za-z0-9/]+")

    def normalize(self, folder: str) -> str:
        folder = folder.strip()
        if not folder:
            return "Misc"
        parts = [self._title_case(part) for part in folder.split("/") if part]
        if not parts:
            return "Misc"
        if parts[0] not in ROOT_FOLDERS:
            parts.insert(0, "Misc")
        return "/".join(parts)

    def _title_case(self, value: str) -> str:
        value = self.pattern.sub(" ", value)
        return " ".join(word.capitalize() for word in value.split())


DEFAULT_FOLDERS: Dict[str, str] = {
    "receipt": "Finance/Receipts",
    "invoice": "Finance/Invoices",
    "statement": "Finance/Statements",
    "newsletter": "Newsletters",
    "promo": "Newsletters/Promotions",
    "school": "School",
    "tuition": "Finance/Tuition",
}
