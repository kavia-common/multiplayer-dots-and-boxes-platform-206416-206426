import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import JSON, Column, DateTime, String, Text, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, declarative_base


Base = declarative_base()


class GameRoomState(Base):
    """SQLAlchemy model storing authoritative room + game state snapshots."""

    __tablename__ = "game_room_states"

    room_code = Column(String(16), primary_key=True)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    version = Column(String(64), nullable=False, default="v1")
    notes = Column(Text, nullable=True)


@dataclass(frozen=True)
class DbConfig:
    """Resolved DB connection config."""

    dsn: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_db_connection_file() -> Path:
    """
    Find db_connection.txt.

    The database container writes it under its own workspace. In this monorepo layout,
    the backend container depends on it, so we read it via a relative path.

    If you move containers, set DOTS_DB_CONNECTION_FILE env var to the new location.
    """
    # Allow override if orchestration prefers mounting/redirecting the file.
    override = os.getenv("DOTS_DB_CONNECTION_FILE")
    if override:
        return Path(override)

    # Repository layout per work item:
    # multiplayer-dots-and-boxes-platform-206416-206425/dots_and_boxes_db/db_connection.txt
    here = Path(__file__).resolve()
    # .../dots_and_boxes_api/src/api/db.py => go up to .../multiplayer-dots-and-boxes-platform-*/dots_and_boxes_api
    api_root = here.parents[3]
    repo_root = api_root.parent
    return repo_root.parent / "multiplayer-dots-and-boxes-platform-206416-206425" / "dots_and_boxes_db" / "db_connection.txt"


def _parse_db_connection_txt(content: str) -> str:
    """
    Parse db_connection.txt content which typically looks like:
      'psql postgresql://user:pass@host:port/db'

    Returns the DSN portion usable by SQLAlchemy/psycopg.
    """
    content = content.strip()
    if not content:
        raise ValueError("db_connection.txt is empty")

    if content.startswith("psql "):
        content = content[len("psql ") :].strip()

    if not (content.startswith("postgresql://") or content.startswith("postgres://")):
        raise ValueError(f"Unexpected db_connection.txt format: {content}")

    return content


def load_db_config() -> DbConfig:
    """Load database configuration from db_connection.txt."""
    path = _default_db_connection_file()
    if not path.exists():
        raise FileNotFoundError(
            f"db_connection.txt not found at {path}. "
            "Set DOTS_DB_CONNECTION_FILE to the correct path or ensure the DB container created it."
        )
    dsn = _parse_db_connection_txt(path.read_text(encoding="utf-8"))
    return DbConfig(dsn=dsn)


def create_db_engine() -> Engine:
    """Create a synchronous SQLAlchemy engine."""
    cfg = load_db_config()
    # psycopg3 driver is used automatically by SQLAlchemy for postgresql:// DSNs.
    return create_engine(cfg.dsn, pool_pre_ping=True, future=True)


def init_db(engine: Engine) -> None:
    """Initialize database schema (idempotent)."""
    Base.metadata.create_all(engine)


def save_room_state(engine: Engine, room_code: str, payload: dict[str, Any]) -> None:
    """Persist authoritative room state snapshot."""
    now = _utcnow()
    try:
        with Session(engine) as session:
            existing = session.get(GameRoomState, room_code)
            if existing:
                existing.payload = payload
                existing.updated_at = now
            else:
                session.add(
                    GameRoomState(
                        room_code=room_code,
                        payload=payload,
                        created_at=now,
                        updated_at=now,
                        version="v1",
                        notes=None,
                    )
                )
            session.commit()
    except SQLAlchemyError as e:
        raise RuntimeError(f"Failed to save room state: {e}") from e


def load_room_state(engine: Engine, room_code: str) -> Optional[dict[str, Any]]:
    """Load last persisted authoritative room state snapshot."""
    try:
        with Session(engine) as session:
            row = session.get(GameRoomState, room_code)
            if not row:
                return None
            # JSON field should already be decoded; but keep safe fallback.
            if isinstance(row.payload, str):
                return json.loads(row.payload)
            return row.payload
    except SQLAlchemyError as e:
        raise RuntimeError(f"Failed to load room state: {e}") from e


def list_rooms(engine: Engine, limit: int = 50) -> list[str]:
    """List persisted room codes (best-effort utility)."""
    try:
        with Session(engine) as session:
            stmt = select(GameRoomState.room_code).order_by(GameRoomState.updated_at.desc()).limit(limit)
            return [r[0] for r in session.execute(stmt).all()]
    except SQLAlchemyError as e:
        raise RuntimeError(f"Failed to list rooms: {e}") from e
