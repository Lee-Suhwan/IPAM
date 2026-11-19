"""DB 위변조 감지용 해시(HMAC) 기본 기능.

- 이력 해시 체인: 각 이력 행의 chain_hash = HMAC(직전 행 해시 ‖ 이 행의 id·내용).
  중간 행을 고치거나 지우면 그 지점부터 체인이 끊어져 '어느 이력'이 변조됐는지 정확히 알 수 있다.
- 행 해시: IP·대역·그룹의 현재 상태 행마다 row_hash = HMAC(행 내용).
  한 번도 손대지 않은 IP(미사용·빈 값·version 1)는 해시를 저장하지 않고(용량 절약),
  대신 '손대지 않은 상태'인지 SQL로 검사한다.
- 기준값(anchor) 파일: 검증을 마친 마지막 이력의 (id, 해시)를 DB 밖에 HMAC과 함께 보관해,
  최근 이력을 통째로 지우거나 DB 파일을 과거 사본으로 바꿔치기한 것도 감지한다.
  기준값 파일까지 지우는 경우에 대비해 보조 사본(Backup 폴더)과 정상 백업 DB의 마지막 이력 지점도
  함께 대조한다. 밖에서 지운 이력은 감시 기록(dirty)으로도 잡는다(프로그램은 이력을 지우지 않음).

키는 프로그램에 내장되어 있으므로, 다른 프로그램(DB Browser 등)으로 값을 고친 경우는 확실히 잡지만
EXE를 역분석해 키를 알아낸 작정한 공격까지 막지는 못한다(= '변조 방지'가 아니라 '변조 감지').
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ipmgr.constants import BACKUP_DIR_NAME, GOOD_BACKUP_NAME, PREV_BACKUP_NAME, STATUS_UNUSED

_KEY = bytes.fromhex("6b2f9c41d07e3a58b1c4e92f7d630a18e5f2b97c4d8a061e3b9f72c5a014d8e6")
CHAIN_BYTES = 16
ROW_BYTES = 12
GENESIS = b"\x00" * CHAIN_BYTES

IP_FIELDS = ("id", "subnet_id", "status", "hostname", "allocated_date", "assignee", "request_no", "note",
             "version", "updated_at", "updated_by")
SUBNET_FIELDS = ("id", "group_id", "cidr", "network_int", "broadcast_int", "prefix", "description")
GROUP_FIELDS = ("id", "parent_id", "name", "description")

# 한 번도 손대지 않은 IP 행 조건(해시 없이 허용) — Python/SQL 정의를 반드시 일치시킨다.
PRISTINE_SQL = (f"status = '{STATUS_UNUSED}' AND hostname = '' AND allocated_date = '' AND assignee = '' "
                "AND request_no = '' AND note = '' AND version = 1 AND updated_at = '' AND updated_by = ''")


_BASE = hmac.new(_KEY, digestmod=hashlib.sha256)     # 키 준비를 한 번만(복사해서 쓰면 전체 검사가 빨라짐)


def _mac(parts: Iterable[Any], size: int) -> bytes:
    msg = "\x1f".join("" if p is None else str(p) for p in parts).encode("utf-8")
    h = _BASE.copy()
    h.update(msg)
    return h.digest()[:size]


def chain_hash(prev: bytes, row: Sequence[Any]) -> bytes:
    """이력 체인 해시: (직전 해시, id, 이력 컬럼 값...)."""
    return _mac([prev.hex(), *row], CHAIN_BYTES)


def is_pristine_ip(row: Mapping[str, Any]) -> bool:
    """대역 생성 직후 그대로인 IP인지."""
    return (row["status"] == STATUS_UNUSED and row["version"] == 1 and not any(
        row[k] for k in ("hostname", "allocated_date", "assignee", "request_no", "note", "updated_at", "updated_by")))


def ip_row_hash(row: Mapping[str, Any]) -> bytes | None:
    """IP 행 해시(손대지 않은 행은 None)."""
    return None if is_pristine_ip(row) else _mac([row[f] for f in IP_FIELDS], ROW_BYTES)


def ip_values_hash(values: Sequence[Any]) -> bytes:
    """IP 행 해시(IP_FIELDS 순서의 값 튜플, 손대지 않은 행 여부는 호출자가 판단) — 전체 검사 고속 경로."""
    return _mac(values, ROW_BYTES)


def subnet_row_hash(row: Mapping[str, Any]) -> bytes:
    """대역 행 해시(사용 통계 카운터는 제외 — 카운터는 실제 IP 수와 대조해 검사)."""
    return _mac([row[f] for f in SUBNET_FIELDS], ROW_BYTES)


def group_row_hash(row: Mapping[str, Any]) -> bytes:
    """그룹 행 해시."""
    return _mac([row[f] for f in GROUP_FIELDS], ROW_BYTES)


# ── 봉인(해시 갱신): 프로그램이 데이터를 바꿀 때마다 같은 트랜잭션 안에서 호출 ──
def _seal(conn: sqlite3.Connection, table: str, fields: Sequence[str], ids: Iterable[int], fn: Any) -> None:
    ids = sorted({int(i) for i in ids})
    cols = ", ".join(fields)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(f"SELECT {cols} FROM {table} WHERE id IN ({marks})", chunk).fetchall()
        conn.executemany(f"UPDATE {table} SET row_hash = ? WHERE id = ?",
                         [(fn(dict(zip(fields, r))), r[0]) for r in rows])


def seal_ips(conn: sqlite3.Connection, ip_ids: Iterable[int]) -> None:
    """IP 행 해시 갱신."""
    _seal(conn, "ips", IP_FIELDS, ip_ids, ip_row_hash)


def seal_subnets(conn: sqlite3.Connection, subnet_ids: Iterable[int]) -> None:
    """대역 행 해시 갱신."""
    _seal(conn, "subnets", SUBNET_FIELDS, subnet_ids, subnet_row_hash)


def seal_groups(conn: sqlite3.Connection, group_ids: Iterable[int]) -> None:
    """그룹 행 해시 갱신."""
    _seal(conn, "groups", GROUP_FIELDS, group_ids, group_row_hash)


def tampered_ips(conn: sqlite3.Connection, ip_ids: Iterable[int]) -> list[int]:
    """프로그램 밖에서 바뀌었고(감시 기록 있음) 해시도 맞지 않는 IP.

    이런 행을 프로그램이 그대로 저장하면 새 해시로 덮여 변조 흔적이 사라지므로 저장 전에 막는다.
    """
    ids = sorted({int(i) for i in ip_ids})
    found: list[int] = []
    try:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            dirty = [r[0] for r in conn.execute(f"SELECT ref FROM dirty WHERE kind = 0 AND ref IN ({marks})", chunk)]
            if not dirty:
                continue
            marks = ",".join("?" * len(dirty))
            for row in conn.execute(f"SELECT {', '.join(IP_FIELDS)}, row_hash FROM ips WHERE id IN ({marks})", dirty):
                expected, stored = ip_row_hash(dict(zip(IP_FIELDS, row))), row[-1]
                if (expected is None) != (stored is None) or (
                        expected is not None and not hmac.compare_digest(expected, bytes(stored))):
                    found.append(row[0])
    except sqlite3.OperationalError:
        return []          # 감시 구조가 없는 DB
    return found


# ── 기준값(anchor) 파일 ───────────────────────────────────
@dataclass(frozen=True)
class Anchor:
    """검증을 마친 이력 체인의 마지막 지점."""
    id: int
    hash: bytes


class AnchorInvalid(Exception):
    """기준값 파일 자체가 변조되었거나 읽을 수 없다."""


def _anchor_mac(anchor_id: int, digest: bytes) -> str:
    return _mac(["anchor", anchor_id, digest.hex()], 16).hex()


def read_anchor(path: Path) -> Anchor | None:
    """기준값 읽기. 파일이 없으면 None, 변조되었으면 AnchorInvalid."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise AnchorInvalid(f"기준값 파일을 읽을 수 없습니다: {exc}") from exc
    try:
        anchor = Anchor(int(data["id"]), bytes.fromhex(data["hash"]))
        ok = hmac.compare_digest(_anchor_mac(anchor.id, anchor.hash), data["mac"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AnchorInvalid("기준값 파일 형식이 올바르지 않습니다.") from exc
    if not ok:
        raise AnchorInvalid("기준값 파일의 서명이 맞지 않습니다(파일 변조).")
    return anchor


def _write_one(path: Path, anchor_id: int, digest: bytes, force: bool = False) -> None:
    path = Path(path)
    if not force:
        try:
            current = read_anchor(path)
            if current is not None and current.id >= anchor_id:
                return
        except AnchorInvalid:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps({"id": anchor_id, "hash": digest.hex(), "mac": _anchor_mac(anchor_id, digest)}),
                   encoding="utf-8")
    os.replace(tmp, path)


def write_anchor(path: Path, anchor_id: int, digest: bytes, force: bool = False) -> None:
    """기준값 저장(임시 파일 → 원자적 교체). 다른 사용자가 더 앞선 지점을 저장했으면 덮지 않는다.

    공유 폴더의 기준값 파일과 함께 보조 사본(Backup 폴더)에도 저장한다.
    force=True는 정상 백업 원복처럼 체인이 짧아진 경우에만 쓴다.
    """
    _write_one(path, anchor_id, digest, force)
    for mirror in anchor_mirrors(path):
        try:
            _write_one(mirror, anchor_id, digest, force)
        except OSError:
            pass                    # 보조 사본은 실패해도 본 기능에 영향 없음


# ── 보조 기준값: 기준값 파일을 지우고 최근 이력까지 지우는 경우 대비 ──
def anchor_mirrors(primary: Path) -> list[Path]:
    """보조 기준값 위치: 공유 폴더의 Backup 폴더(사용자 PC에는 파일을 남기지 않는다)."""
    primary = Path(primary)
    return [primary.parent / BACKUP_DIR_NAME / primary.name]


def read_mirror_anchors(primary: Path) -> list[Anchor]:
    """읽을 수 있는 보조 기준값 목록(손상된 사본은 무시)."""
    found = []
    for mirror in anchor_mirrors(primary):
        try:
            anchor = read_anchor(mirror)
        except AnchorInvalid:
            continue
        if anchor is not None:
            found.append(anchor)
    return found


def backup_heads(primary: Path) -> list[Anchor]:
    """정상 백업 DB들의 마지막 이력 지점(현재 DB 이력이 이보다 짧으면 최근 이력이 삭제된 것)."""
    heads = []
    for name in (GOOD_BACKUP_NAME, PREV_BACKUP_NAME):
        path = Path(primary).parent / BACKUP_DIR_NAME / name
        if not path.exists():
            continue
        try:
            conn = sqlite3.connect(str(path), timeout=2)
            try:
                row = conn.execute("SELECT id, chain_hash FROM history_logs ORDER BY id DESC LIMIT 1").fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        if row and row[1] is not None:
            heads.append(Anchor(int(row[0]), bytes(row[1])))
    return heads
