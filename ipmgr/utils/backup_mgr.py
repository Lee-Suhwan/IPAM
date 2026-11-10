"""정상 백업 관리.

DB 안에 전체 이력이 누적되므로 날짜별 사본을 1년치 쌓지 않고, 아래 두 파일만 유지한다.
  Backup/IP관리_정상백업.db       무결성 검사를 '통과한' 최신 사본(하루 1회 갱신)
  Backup/IP관리_정상백업_이전.db   그 직전 정상 사본(정상 사본이 늦게 의심될 때 대비)
변조된 DB는 검사를 통과하지 못하므로 정상 사본을 덮어쓰지 못한다.
SQLite Online Backup API로 복사해 다른 사용자가 쓰는 중에도 일관된 사본을 만든다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from ipmgr.constants import (BACKUP_INFO_NAME, BACKUP_PREFIX, BACKUP_RETENTION_DAYS, DB_TIMEOUT, GOOD_BACKUP_NAME,
                             PREV_BACKUP_NAME)

log = logging.getLogger(__name__)
_LEGACY_RE = re.compile(rf"^(?:{re.escape(BACKUP_PREFIX)}|ipam_)(\d{{8}})\.db$")
LOCK_NAME = "백업중.lock"


def backup_info(backup_dir: Path) -> dict:
    """정상 백업 정보(없으면 빈 dict)."""
    try:
        return json.loads((backup_dir / BACKUP_INFO_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _copy_db(db_path: Path, target: Path) -> None:
    tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    src = sqlite3.connect(str(db_path), timeout=DB_TIMEOUT)
    dst = sqlite3.connect(str(tmp))
    try:
        src.backup(dst, pages=2048, sleep=0.002)     # 페이지 단위로 나눠 다른 사용자의 쓰기를 오래 막지 않음
    finally:
        dst.close()
        src.close()
    os.replace(tmp, target)


def refresh_good_backup(db_path: Path, backup_dir: Path, history_id: int, checked_by: str,
                        today: date | None = None, force: bool = False) -> Path | None:
    """무결성 검사를 통과한 직후 호출: 오늘 정상 백업이 없으면 '정상 → 이전'으로 넘기고 새 정상 백업을 만든다."""
    today = today or date.today()
    backup_dir.mkdir(parents=True, exist_ok=True)
    if not force and backup_info(backup_dir).get("date") == today.isoformat():
        return None
    lock = backup_dir / LOCK_NAME
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > 3600:
            lock.unlink()                        # 비정상 종료로 남은 잠금
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None                              # 다른 사용자가 지금 백업 중
    os.close(fd)
    try:
        if not force and backup_info(backup_dir).get("date") == today.isoformat():
            return None
        good, prev = backup_dir / GOOD_BACKUP_NAME, backup_dir / PREV_BACKUP_NAME
        new = backup_dir / f"{GOOD_BACKUP_NAME}.new"
        _copy_db(db_path, new)
        if good.exists():
            os.replace(good, prev)
        os.replace(new, good)
        info = {"date": today.isoformat(), "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "history_id": history_id, "checked_by": checked_by}
        (backup_dir / BACKUP_INFO_NAME).write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        return good
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def purge_old_backups(backup_dir: Path, retention_days: int = BACKUP_RETENTION_DAYS,
                      today: date | None = None) -> list[Path]:
    """이전 버전이 만들던 날짜별 백업(ipam_/IP관리_YYYYMMDD.db) 중 보존 기간이 지난 것과 남은 임시 파일을 정리한다."""
    today = today or date.today()
    cutoff = today - timedelta(days=retention_days)
    removed: list[Path] = []
    if not backup_dir.exists():
        return removed
    for path in backup_dir.iterdir():
        try:
            match = _LEGACY_RE.match(path.name)
            if match:
                if datetime.strptime(match.group(1), "%Y%m%d").date() < cutoff:
                    path.unlink()
                    removed.append(path)
            elif ".tmp-" in path.name and \
                    datetime.fromtimestamp(path.stat().st_mtime).date() < today - timedelta(days=1):
                path.unlink()
                removed.append(path)
        except (OSError, ValueError) as exc:
            log.warning("백업 정리 실패 %s: %s", path, exc)
    return removed
