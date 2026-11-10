"""Single Worker Thread + Queue 패턴.

- DB 커넥션은 오직 이 스레드만 소유한다(UI 스레드는 SQLite를 직접 만지지 않음 → 프리징 없음).
- UI는 `submit(fn, ...)`으로 작업을 넣고, 결과 콜백은 `drain()`을 통해 UI 스레드에서 실행된다.
- 네트워크 드라이브 일시 단절(disk I/O error 등) 시 커넥션을 폐기·재생성하고 재시도한다.
- 잠금 대기(busy timeout 15초) 초과 시 짧게 쉬었다가 재시도한 뒤 사용자 오류로 보고한다.
"""
from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ipmgr.db.connection import create_connection
from ipmgr.db.schema import init_schema

log = logging.getLogger(__name__)

Callback = Callable[[Any], None]

_RECONNECT_HINTS = ("disk i/o", "unable to open", "readonly database", "not a database",
                    "database disk image", "network", "file is not")
_BUSY_HINTS = ("locked", "busy")


class DBBusyError(Exception):
    """다른 사용자의 작업으로 DB 잠금을 획득하지 못했다."""


class DBConnectionError(Exception):
    """DB 파일(네트워크 드라이브)에 연결할 수 없다."""


class DBWorker:
    """DB 커넥션을 독점하는 단일 작업 스레드."""

    def __init__(self, db_path: str | Path, max_retries: int = 2) -> None:
        self.db_path = Path(db_path)
        self.max_retries = max_retries
        self._tasks: queue.Queue[tuple | None] = queue.Queue()
        self._results: queue.Queue[tuple[Callback, Any]] = queue.Queue()
        self._conn: sqlite3.Connection | None = None
        self._thread = threading.Thread(target=self._run, name="db-worker", daemon=True)
        self._pending = 0
        self._busy_pending = 0
        self._lock = threading.Lock()
        self.connected = False
        self.default_error_handler: Callback | None = None

    # ── 수명 주기 ──────────────────────────────────────────
    def start(self) -> None:
        """작업 스레드를 시작한다."""
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """대기 작업을 처리한 뒤 스레드를 종료한다."""
        self._tasks.put(None)
        self._thread.join(timeout)

    # ── 작업 제출 ──────────────────────────────────────────
    @property
    def pending(self) -> int:
        """완료되지 않은 사용자 작업 수(폴링 등 silent 작업 제외)."""
        return self._busy_pending

    def submit(self, fn: Callable[..., Any], *args: Any, on_success: Callback | None = None,
               on_error: Callback | None = None, silent: bool = False, **kwargs: Any) -> None:
        """`fn(conn, *args, **kwargs)`를 Worker에서 실행하고 결과를 콜백으로 전달한다."""
        with self._lock:
            self._pending += 1
            if not silent:
                self._busy_pending += 1
        self._tasks.put((fn, args, kwargs, on_success, on_error, silent))

    def run_sync(self, fn: Callable[..., Any], *args: Any, timeout: float | None = 60.0, **kwargs: Any) -> Any:
        """비-UI 코드(테스트/스크립트)용 동기 실행. 예외는 호출자에게 다시 발생한다."""
        done = threading.Event()
        box: dict[str, Any] = {}

        def ok(value: Any) -> None:
            box["value"] = value
            done.set()

        def err(exc: Any) -> None:
            box["error"] = exc
            done.set()

        self._tasks.put((fn, args, kwargs, ok, err, True, True))
        with self._lock:
            self._pending += 1
        if not done.wait(timeout):
            raise TimeoutError("DB 작업 시간 초과")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def drain(self, max_items: int = 200) -> int:
        """완료된 작업의 콜백을 호출 스레드(UI)에서 실행한다. 처리 개수 반환."""
        handled = 0
        while handled < max_items:
            try:
                callback, value = self._results.get_nowait()
            except queue.Empty:
                break
            handled += 1
            try:
                callback(value)
            except Exception:  # noqa: BLE001 - 콜백 오류가 펌프를 멈추지 않게
                log.exception("콜백 실행 오류")
        return handled

    # ── 내부 ──────────────────────────────────────────────
    def _run(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                break
            fn, args, kwargs, on_success, on_error, silent, *direct = task
            try:
                result = self._execute(fn, args, kwargs)
            except BaseException as exc:  # noqa: BLE001
                handler = on_error or self.default_error_handler
                if not isinstance(exc, (DBBusyError, DBConnectionError)) and not _is_business_error(exc):
                    log.exception("DB 작업 오류: %s", getattr(fn, "__name__", fn))
                self._finish(handler, exc, silent, bool(direct))
            else:
                self._finish(on_success, result, silent, bool(direct))
        self._close()

    def _finish(self, callback: Callback | None, value: Any, silent: bool, direct: bool) -> None:
        with self._lock:
            self._pending -= 1
            if not silent:
                self._busy_pending -= 1
        if callback is None:
            return
        if direct:              # run_sync: 대기 중인 스레드에 바로 전달
            callback(value)
        else:
            self._results.put((callback, value))

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            try:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = create_connection(self.db_path)
                init_schema(conn)
            except (sqlite3.Error, OSError) as exc:
                self.connected = False
                raise DBConnectionError(f"DB에 연결할 수 없습니다: {self.db_path}\n{exc}") from exc
            self._conn = conn
            self.connected = True
        return self._conn

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
        self._conn = None
        self.connected = False

    def _execute(self, fn: Callable[..., Any], args: tuple, kwargs: dict) -> Any:
        attempt = 0
        while True:
            conn = self._ensure_conn()
            try:
                return fn(conn, *args, **kwargs)
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if attempt >= self.max_retries:
                    if any(h in msg for h in _BUSY_HINTS):
                        raise DBBusyError("다른 사용자가 작업 중이라 DB가 잠겨 있습니다.\n"
                                          "잠시 후 다시 시도해 주세요.") from exc
                    if any(h in msg for h in _RECONNECT_HINTS):
                        self._close()
                        raise DBConnectionError(f"네트워크 드라이브의 DB에 접근할 수 없습니다.\n{exc}") from exc
                    raise
                attempt += 1
                if any(h in msg for h in _RECONNECT_HINTS):
                    log.warning("DB 연결 재생성(%s): %s", attempt, exc)
                    self._close()
                    time.sleep(0.5 * attempt)
                elif any(h in msg for h in _BUSY_HINTS):
                    time.sleep(0.3 * attempt)
                else:
                    raise
            except sqlite3.DatabaseError as exc:
                if attempt >= self.max_retries or "malformed" in str(exc).lower():
                    raise
                attempt += 1
                self._close()
                time.sleep(0.5 * attempt)


def _is_business_error(exc: BaseException) -> bool:
    from ipmgr.models.common import AppError
    return isinstance(exc, AppError)
