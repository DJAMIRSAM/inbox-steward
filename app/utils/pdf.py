from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from pypdf import PdfReader

from app.core.config import settings

logger = logging.getLogger(__name__)


def extract_text_from_pdf(path: Path) -> Optional[str]:
    try:
        reader = PdfReader(path)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        return text.strip() or None
    except Exception:  # noqa: BLE001
        logger.exception("Failed to read PDF %s", path)
        return None


def save_temp_pdf(filename: str, content: bytes) -> Path:
    settings.pdf_temp_dir.mkdir(parents=True, exist_ok=True)
    target = settings.pdf_temp_dir / filename
    target.write_bytes(content)
    return target
