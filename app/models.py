from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Column
from sqlmodel import Field, JSON, SQLModel, UniqueConstraint


class EmailMessage(SQLModel, table=True):
    __tablename__ = "emails"

    uid: str = Field(primary_key=True, description="IMAP UID")
    message_id: Optional[str] = Field(default=None, index=True)
    thread_id: Optional[str] = Field(default=None, index=True)
    subject: str = Field(index=True)
    sender: str = Field(index=True)
    to_recipients: Optional[str] = None
    cc_recipients: Optional[str] = None
    received_at: datetime = Field(index=True)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)
    folder: str
    target_folder: Optional[str] = Field(default=None, index=True)
    classification: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    status: str = Field(default="pending", index=True)
    needs_decision: bool = Field(default=False, index=True)
    digest_batch: Optional[str] = Field(default=None, index=True)
    session_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FolderHint(SQLModel, table=True):
    __tablename__ = "folder_hints"
    __table_args__ = (UniqueConstraint("hint", "folder", name="uq_hint_folder"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    hint: str = Field(index=True)
    folder: str = Field(index=True)
    weight: float = Field(default=1.0)
    last_used_at: datetime = Field(default_factory=datetime.utcnow)


class CalendarEvent(SQLModel, table=True):
    __tablename__ = "calendar_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    uid: str = Field(index=True, unique=True)
    thread_id: Optional[str] = Field(default=None, index=True)
    provider: Optional[str] = Field(default=None, index=True)
    title: str
    calendar: str = Field(index=True)
    starts_at: datetime = Field(index=True)
    ends_at: datetime = Field(index=True)
    timezone: str = Field(default="America/Vancouver")
    location: Optional[str] = None
    url: Optional[str] = None
    notes: Optional[str] = None
    raw_payload: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ActionLog(SQLModel, table=True):
    __tablename__ = "action_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)
    email_uid: Optional[str] = Field(default=None, index=True)
    action_type: str = Field(index=True)
    payload: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ConflictLog(SQLModel, table=True):
    __tablename__ = "conflict_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    calendar: str = Field(index=True)
    conflict_type: str = Field(index=True)
    details: dict[str, Any] = Field(sa_column=Column(JSON))
    resolved: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UndoToken(SQLModel, table=True):
    __tablename__ = "undo_tokens"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)
    token: str = Field(unique=True, index=True)
    expires_at: datetime = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
