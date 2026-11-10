"""모델 공통: 예외, 시간 문자열, 변경 리비전."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any


class AppError(Exception):
    """사용자에게 그대로 보여줄 수 있는 비즈니스 오류."""


class ValidationError(AppError):
    """입력값/정책 위반."""


class NotFoundError(AppError):
    """대상이 이미 삭제되었거나 존재하지 않음."""


class ConflictError(AppError):
    """낙관적 잠금(version) 충돌: 다른 사용자가 먼저 수정/삭제했다."""

    def __init__(self, message: str, latest: dict[str, Any] | None = None, deleted: bool = False) -> None:
        super().__init__(message)
        self.latest = latest
        self.deleted = deleted


def now_str() -> str:
    """현재 일시 'YYYY-MM-DD HH:MM:SS'."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def bump_revision(conn: sqlite3.Connection, tree: bool = False) -> None:
    """쓰기 트랜잭션 안에서 호출: 다른 클라이언트가 폴링으로 변경을 감지하게 한다."""
    keys = ("data_rev", "tree_rev") if tree else ("data_rev",)
    conn.executemany("UPDATE meta SET value = value + 1 WHERE key = ?", [(k,) for k in keys])


def get_revisions(conn: sqlite3.Connection) -> dict[str, int]:
    """현재 data_rev / tree_rev 값과 외부 수정 흔적(dirty) 여부를 조회한다(폴링용 초경량 쿼리)."""
    revs = {row[0]: row[1] for row in conn.execute(
        "SELECT key, value FROM meta WHERE key IN ('data_rev', 'tree_rev')")}
    try:
        revs["dirty"] = conn.execute("SELECT EXISTS(SELECT 1 FROM dirty)").fetchone()[0]
    except sqlite3.OperationalError:
        revs["dirty"] = 0
    return revs


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """sqlite3.Row → dict (None 안전)."""
    return dict(row) if row is not None else None
