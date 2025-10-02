from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.actions import processor

router = APIRouter(prefix="/api")


@router.post("/full-sort")
def api_full_sort() -> dict:
    return processor.full_sort()


@router.get("/what-if")
def api_what_if() -> dict:
    return processor.what_if()


@router.post("/process")
async def api_process_seen() -> dict:
    await processor.process_seen_messages()
    return {"status": "ok"}


@router.post("/undo/{token}")
def api_undo(token: str) -> dict:
    if not processor.undo(token):
        raise HTTPException(status_code=404, detail="Undo token not found")
    return {"status": "undone"}
