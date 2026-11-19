"""실행 위치 기준 경로 계산.

EXE(PyInstaller onefile)로 실행되면 EXE가 놓인 폴더(네트워크 드라이브)를,
소스로 실행되면 프로젝트 루트를 기준 경로로 사용한다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from ipmgr.constants import (ANCHOR_FILE_NAME, APP_NAME, BACKUP_DIR_NAME, DATA_DIR_NAME, DB_FILE_NAME,
                             GOOD_BACKUP_NAME, LEGACY_DB_FILE_NAME, OLD_FILE_PREFIX, PREV_BACKUP_NAME)

_data_dir_override: Path | None = None


def get_base_path() -> Path:
    """EXE 또는 프로젝트 루트 경로를 반환한다.

    `Path.resolve()`는 매핑 드라이브(Z:)를 UNC 경로로 바꿀 수 있으므로 abspath를 사용한다.
    """
    if getattr(sys, "frozen", False):
        return Path(os.path.abspath(os.path.dirname(sys.executable)))
    return Path(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


def set_data_dir(path: str | os.PathLike[str] | None) -> None:
    """데이터 폴더를 강제로 지정한다(부하 테스트/개발용). None이면 기본값으로 복귀."""
    global _data_dir_override
    _data_dir_override = Path(os.path.abspath(path)) if path else None


def get_data_dir() -> Path:
    """`data_삭제금지` 폴더 경로를 반환하고 없으면 생성한다."""
    path = _data_dir_override or (get_base_path() / DATA_DIR_NAME)
    path.mkdir(parents=True, exist_ok=True)
    return path


BLOCKER_NOTE = "이전 버전 실행 차단용 - 지우지 마세요.txt"


class OldVersionRunning(Exception):
    """이전 버전(IP관리.exe)이 아직 DB를 열고 있어 파일 이름을 바꿀 수 없다."""


def migrate_old_names(data_dir: Path) -> bool:
    """v1.1~v1.2의 'IP관리.*' 데이터 파일을 'IPAM.*' 으로 바꾼다(처음 한 번). 바꿨으면 True.

    DB 파일 이름을 바꾼 뒤에는 같은 이름의 '폴더'를 만들어 둔다. 공유 폴더에 남은 이전 버전 EXE를
    누가 실행해도 그 자리에 빈 DB를 새로 만들지 못하게(데이터가 두 군데로 갈라지지 않게) 막기 위해서다.
    """
    old_db, new_db = data_dir / f"{OLD_FILE_PREFIX}.db", data_dir / DB_FILE_NAME
    if not old_db.is_file() or new_db.exists():
        return False
    try:
        os.replace(old_db, new_db)
    except OSError as exc:
        raise OldVersionRunning(
            "데이터 파일 이름을 'IPAM'으로 바꾸려면 모든 사용자가 이전 버전(IP관리.exe)을 종료해야 합니다.\n"
            "다른 사용자가 아직 이전 버전을 쓰고 있습니다. 모두 종료한 뒤 다시 실행해 주세요.") from exc
    moves = [(f"{OLD_FILE_PREFIX}.db-journal", f"{DB_FILE_NAME}-journal"),
             (f"{OLD_FILE_PREFIX}.anchor", ANCHOR_FILE_NAME)]
    backup = data_dir / BACKUP_DIR_NAME
    for src, dst in moves:
        _move_if_free(data_dir / src, data_dir / dst)
    for src, dst in ((f"{OLD_FILE_PREFIX}.anchor", ANCHOR_FILE_NAME),
                     (f"{OLD_FILE_PREFIX}_정상백업.db", GOOD_BACKUP_NAME),
                     (f"{OLD_FILE_PREFIX}_정상백업_이전.db", PREV_BACKUP_NAME)):
        _move_if_free(backup / src, backup / dst)
    try:
        old_db.mkdir()
        (old_db / BLOCKER_NOTE).write_text(
            "데이터 파일 이름이 IPAM.db 로 바뀌었습니다.\n"
            "이 폴더는 이전 버전(IP관리.exe)이 빈 DB를 새로 만들지 못하게 막는 용도입니다. 지우지 마세요.\n"
            "공유 폴더의 IP관리.exe 는 지우고 IPAM.exe 를 사용하세요.\n", encoding="utf-8")
    except OSError:
        pass
    return True


def _move_if_free(src: Path, dst: Path) -> None:
    if src.is_file() and not dst.exists():
        try:
            os.replace(src, dst)
        except OSError:
            pass


def get_db_path() -> Path:
    """SQLite DB 파일 경로. 예전 이름(IP관리.db, v1.0의 ipam.db)이면 새 이름으로 바꿔 이어서 사용한다."""
    migrate_old_names(get_data_dir())
    path = get_data_dir() / DB_FILE_NAME
    legacy = get_data_dir() / LEGACY_DB_FILE_NAME
    if not path.exists() and legacy.exists():
        try:
            os.replace(legacy, path)
            journal = legacy.with_name(legacy.name + "-journal")
            if journal.exists():
                os.replace(journal, path.with_name(path.name + "-journal"))
        except OSError:
            return legacy        # 다른 사용자가 열고 있으면 기존 파일을 그대로 사용
    return path


def get_anchor_path() -> Path:
    """무결성 기준값 파일 경로(DB와 같은 폴더)."""
    return get_data_dir() / ANCHOR_FILE_NAME


def get_backup_dir() -> Path:
    """일일 백업 폴더 경로(없으면 생성)."""
    path = get_data_dir() / BACKUP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_user_config_dir() -> Path:
    """사용자별 로컬 설정/로그 폴더(%LOCALAPPDATA%\\IP관리). 공유 드라이브에 쓰지 않는다."""
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    path = Path(root) / APP_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        path = Path.home()
    return path
