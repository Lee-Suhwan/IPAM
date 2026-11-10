"""실행 위치 기준 경로 계산.

EXE(PyInstaller onefile)로 실행되면 EXE가 놓인 폴더(네트워크 드라이브)를,
소스로 실행되면 프로젝트 루트를 기준 경로로 사용한다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from ipmgr.constants import (ANCHOR_FILE_NAME, APP_NAME, BACKUP_DIR_NAME, DATA_DIR_NAME, DB_FILE_NAME,
                             LEGACY_DB_FILE_NAME)

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


def get_db_path() -> Path:
    """SQLite DB 파일 경로. v1.0 파일명(ipam.db)만 있으면 새 이름으로 바꿔 이어서 사용한다."""
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
