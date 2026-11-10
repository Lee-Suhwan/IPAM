"""SMB 공유 환경에 맞춘 SQLite 연결 생성 및 트랜잭션 헬퍼.

- 파일 락 직접 사용 금지: SQLite 자체 잠금 + busy timeout(15초)에 맡긴다.
- WAL은 공유 메모리(-shm)를 쓰므로 네트워크 드라이브에서 손상 위험 → `journal_mode=TRUNCATE`.
- 쓰기는 `BEGIN IMMEDIATE`로 시작해 잠금 획득 실패를 트랜잭션 초기에 감지한다.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ipmgr.constants import DB_TIMEOUT


def create_connection(db_path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """PRAGMA가 적용된 autocommit 모드 연결을 만든다(트랜잭션은 명시적으로 제어)."""
    conn = sqlite3.connect(str(db_path), timeout=DB_TIMEOUT, isolation_level=None,
                           check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=TRUNCATE").fetchone()
    except sqlite3.OperationalError:
        pass  # 다른 연결이 잡고 있으면 기존 모드 유지(다음 연결에서 재시도)
    conn.execute(f"PRAGMA busy_timeout={int(DB_TIMEOUT * 1000)}")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-16384")      # 16MB 페이지 캐시
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """재진입 가능한 트랜잭션 컨텍스트.

    이미 트랜잭션 안이면 바깥 트랜잭션에 합류한다. 예외 시 ROLLBACK, 정상 종료 시 COMMIT.
    쓰기 트랜잭션(immediate)은 meta.app_write로 '프로그램의 쓰기'임을 표시해 외부 수정 감시 트리거를 건너뛴다.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        if immediate:
            _mark_app_write(conn, 1)
        yield conn
        if immediate:
            _mark_app_write(conn, 0)
    except BaseException:
        _safe_rollback(conn)
        raise
    try:
        conn.execute("COMMIT")
    except BaseException:
        _safe_rollback(conn)
        raise


def _mark_app_write(conn: sqlite3.Connection, value: int) -> None:
    try:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'app_write'", (value,))
    except sqlite3.OperationalError:
        pass  # 스키마 생성 전(meta 없음)


def _safe_rollback(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass
