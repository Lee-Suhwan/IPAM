"""DB 무결성 검사(위변조 감지)와 복구.

검사
- 빠른 검사(quick_check): 대역 수·DB 크기와 무관하게 수 ms
  · 감시 장치(트리거) 훼손 여부
  · 기준값(anchor) 이후 새로 쌓인 이력만 체인 검증
  · 프로그램 밖에서 바뀐 행(dirty — 트리거가 위치를 기록)만 골라 해시로 검증
- 전체 검사(full_check): 이력 체인 전체 + 모든 행 해시. 화면에는 노출하지 않고
  ① 이전 버전 DB를 처음 열 때 1회(기준 검사) ② 정상 백업 검증 에만 쓴다.

복구(recover — 사용자 확인 1번, 방식은 자동 선택)
- 행만 변조된 경우(이력 체인은 정상): 이력의 마지막 스냅샷으로 현재 상태를 재구성 → 손실 없음
- 이력·대역·그룹·감시 장치가 변조된 경우: 검사를 통과한 '정상 백업'으로 원복한 뒤, 백업 이후 체인이
  이어지는 정상 이력을 다시 적용. 원복 전 DB는 '변조본_날짜.db'로 보관한다.
- 공유 드라이브에서 여러 명이 동시에 복구하지 않도록 복구 잠금 파일을 쓴다.
"""
from __future__ import annotations

import hmac
import json
import os
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from ipmgr.constants import (ACT_ALLOCATE, ACT_INTEGRITY_REPAIR, ACT_MODIFY, ACT_REALLOCATE, ACT_RECLAIM,
                             ACT_SUBNET_CREATE, GOOD_BACKUP_NAME, PREV_BACKUP_NAME, STATUS_UNUSED, STATUS_USED,
                             TAMPERED_PREFIX)
from ipmgr.db.connection import create_connection, transaction
from ipmgr.db.schema import (DIRTY_GROUP, DIRTY_HISTORY, DIRTY_IP, DIRTY_SUBNET, RANGE_LOOKBACK, TRIGGER_NAMES,
                             WATCH_TRIGGER_NAMES)
from ipmgr.logic import integrity as ig
from ipmgr.logic.subnet_calc import int_to_ip
from ipmgr.models.common import ValidationError, bump_revision, now_str
from ipmgr.models.history_model import HISTORY_COLUMNS, chain_head, insert_entries, make_entry, snapshot_entry

Progress = Callable[[float, str], None]
CHUNK = 20000            # 한 번에 읽는 행 수(짧게 끊어 읽어 다른 사용자의 쓰기를 막지 않음)
MAX_LIST = 300           # 화면에 보여줄 항목 수(건수 집계는 전부)

KIND_LABELS = {
    "anchor": "기준값 불일치", "gap": "이력 삭제", "chain": "이력 변조",
    "ip": "IP 행 변조", "missing": "IP 행 누락", "orphan": "비정상 IP 행", "count": "사용 통계 불일치",
    "subnet": "대역 행 변조", "group": "그룹 행 변조", "watch": "감시 장치 훼손",
}
LOCK_NAME = "복구중.lock"
LOCK_STALE_SEC = 30 * 60          # 이보다 오래된 잠금은 비정상 종료로 보고 무시
ROW_REPAIRABLE = {"ip", "missing", "orphan", "count"}          # 이력으로 복구 가능한 종류
SNAPSHOT_ACTIONS = {ACT_ALLOCATE, ACT_REALLOCATE, ACT_RECLAIM, ACT_MODIFY, ACT_INTEGRITY_REPAIR}
_HIST_COLS = ", ".join(HISTORY_COLUMNS)
_IP_COLS = ", ".join(ig.IP_FIELDS)
_I_ADDR, _I_ACTION, _I_TS = (HISTORY_COLUMNS.index(c) + 1 for c in ("ip_address", "action", "timestamp"))


@dataclass
class Issue:
    """발견된 위변조 1건."""
    kind: str
    ref: int | None
    where: str
    detail: str


@dataclass
class Report:
    """검사 결과."""
    scope: str                                    # "quick" | "full"
    issues: list[Issue] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)
    targets: dict[str, list[int]] = field(default_factory=dict)   # 복구 대상 ID(제한 없음)
    checked: dict[str, int] = field(default_factory=dict)
    head: tuple[int, bytes] = (0, ig.GENESIS)
    notes: list[str] = field(default_factory=list)
    needs_full: bool = False
    elapsed: float = 0.0
    finished_at: str = ""

    @property
    def ok(self) -> bool:
        """위변조가 하나도 없으면 True."""
        return not self.counts

    @property
    def repairable_rows(self) -> bool:
        """이력 기준 복구만으로 해결 가능한지(이력 체인·대역·그룹은 정상)."""
        return bool(self.counts) and set(self.counts) <= ROW_REPAIRABLE

    def add(self, kind: str, ref: int | None, where: str, detail: str) -> None:
        """이슈 추가(목록은 MAX_LIST까지, 건수와 복구 대상은 전부)."""
        self.counts[kind] += 1
        if ref is not None:
            self.targets.setdefault(kind, []).append(ref)
        if self.counts[kind] <= MAX_LIST:
            self.issues.append(Issue(kind, ref, where, detail))

    def summary(self) -> str:
        """한 줄 요약."""
        if self.ok:
            return "정상"
        return " · ".join(f"{KIND_LABELS[k]} {n:,}건" for k, n in self.counts.items())


def _hist_where(row: tuple) -> str:
    return f"이력 #{row[0]}  {row[_I_TS]}  {row[_I_ACTION]}  {row[_I_ADDR]}".rstrip()


# ── 체인/기준값 ─────────────────────────────────────────────
def verify_chain(conn: sqlite3.Connection, report: Report, start_id: int = 0, start_hash: bytes = ig.GENESIS,
                 progress: Progress | None = None, total: int = 0) -> tuple[int, bytes]:
    """start 이후 이력 체인을 검증하고 (마지막 id, 해시)를 반환한다."""
    prev_id, prev = start_id, start_hash
    checked = 0
    while True:
        rows = conn.execute(f"SELECT id, {_HIST_COLS}, chain_hash FROM history_logs WHERE id > ? "
                            f"ORDER BY id LIMIT ?", (prev_id, CHUNK)).fetchall()
        if not rows:
            break
        for row in rows:
            rid, stored = row[0], row[-1]
            if rid != prev_id + 1:
                report.add("gap", prev_id + 1, f"이력 #{prev_id + 1} ~ #{rid - 1}",
                           f"이력 {rid - prev_id - 1:,}건이 삭제되었습니다")
                prev_id, prev = rid, stored or ig.GENESIS      # 지워진 직전 해시를 모르므로 이 행은 건너뜀
                continue
            expected = ig.chain_hash(prev, tuple(row[:-1]))
            if stored is None or not hmac.compare_digest(expected, bytes(stored)):
                report.add("chain", rid, _hist_where(row), "이력 내용이 프로그램 밖에서 수정되었습니다")
                prev = bytes(stored) if stored is not None else expected   # 이후 독립 변조도 각각 찾아냄
            else:
                prev = expected
            prev_id = rid
        checked += len(rows)
        if progress and total:
            progress(min(checked / total, 1.0), f"이력 체인 검사 {checked:,}/{total:,}")
    report.checked["history"] = report.checked.get("history", 0) + checked
    return prev_id, prev


def check_anchor(conn: sqlite3.Connection, report: Report, anchor_path: Path | None) -> ig.Anchor | None:
    """DB 밖 기준값과 대조: 최근 이력 삭제·과거 사본으로 교체를 감지한다."""
    if anchor_path is None:
        return None
    try:
        anchor = ig.read_anchor(anchor_path)
    except ig.AnchorInvalid as exc:
        report.add("anchor", None, "기준값 파일", str(exc))
        return None
    if anchor is None:
        report.notes.append("기준값 파일이 없어 전체 검사 후 새로 만듭니다.")
        report.needs_full = True
        return None
    row = conn.execute("SELECT chain_hash FROM history_logs WHERE id = ?", (anchor.id,)).fetchone()
    if row is None:
        head_id = chain_head(conn)[0]
        report.add("anchor", anchor.id, f"이력 #{anchor.id}",
                   f"검증된 기준 시점(#{anchor.id})의 이력이 없습니다 — 최근 이력이 삭제되었거나 DB가 과거 "
                   f"사본으로 교체되었습니다 (현재 마지막 이력 #{head_id})")
        return None
    if row[0] is None or not hmac.compare_digest(bytes(row[0]), anchor.hash):
        report.add("anchor", anchor.id, f"이력 #{anchor.id}", "기준 시점 이력의 해시가 다릅니다 — 이력이 재작성되었습니다")
        return None
    return anchor


def _quick_report(conn: sqlite3.Connection, anchor_path: Path | None) -> tuple[Report, ig.Anchor | None,
                                                                             list[tuple[int, int]]]:
    """빠른 검사 본체(부수 효과 없음): (결과, 기준값, 검증한 dirty 항목)."""
    report = Report("quick")
    check_watch(conn, report)
    anchor = check_anchor(conn, report, anchor_path)
    if anchor is not None:
        report.head = verify_chain(conn, report, anchor.id, anchor.hash)
    else:
        report.head = chain_head(conn)
    keys = verify_dirty(conn, report)
    row = conn.execute("SELECT value FROM meta WHERE key = 'baseline'").fetchone()
    if row is not None and not row[0]:
        report.needs_full = True                  # 이전 버전 DB: 감시 이전 변경은 기준 검사(1회)로 확인
    return report, anchor, keys


def quick_check(conn: sqlite3.Connection, anchor_path: Path | None) -> Report:
    """빠른 검사. 정상이면 기준값을 앞으로 옮기고 검증을 마친 dirty 기록을 지운다."""
    started = time.perf_counter()
    report, anchor, keys = _quick_report(conn, anchor_path)
    if report.ok:
        if anchor is not None and report.head[0] > anchor.id:
            _write_anchor_safe(anchor_path, report)
        if keys:
            _clear_dirty(conn, keys)
    report.elapsed = time.perf_counter() - started
    report.finished_at = now_str()
    return report


def _clear_dirty(conn: sqlite3.Connection, keys: list[tuple[int, int]] | None = None) -> None:
    """검증을 마친 외부 수정 기록 삭제(keys가 없으면 전체)."""
    with transaction(conn):
        if keys is None:
            conn.execute("DELETE FROM dirty")
        else:
            conn.executemany("DELETE FROM dirty WHERE kind = ? AND ref = ?", keys)


# ── 감시 장치 / 외부 수정 행 ────────────────────────────────
def has_watch(conn: sqlite3.Connection) -> bool:
    """외부 수정 감시(v4) 구조가 있는 DB인지."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'dirty'").fetchone() is not None


def _normalize_sql(sql: str) -> str:
    return " ".join(sql.replace("IF NOT EXISTS ", "").split())


def check_watch(conn: sqlite3.Connection, report: Report) -> None:
    """감시 트리거·통계 트리거·dirty 테이블이 원래 정의 그대로인지 확인한다."""
    from ipmgr.db.schema import TRIGGERS_SQL, WATCH_TRIGGERS_SQL
    expected = {}
    for statement in (TRIGGERS_SQL + WATCH_TRIGGERS_SQL).split("END;"):
        if statement.strip():
            sql = _normalize_sql(statement + "END")
            expected[sql.split()[2]] = sql
    actual = {r[0]: _normalize_sql(r[1] or "").rstrip(";")
              for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")}
    changed = [n for n in TRIGGER_NAMES if actual.get(n) != expected[n]]
    if not has_watch(conn):
        report.add("watch", None, "감시 장치", "외부 수정 기록 테이블이 삭제되었습니다")
    if changed:
        gone = sum(1 for n in changed if n not in actual)
        report.add("watch", None, "감시 장치",
                   f"트리거 {len(changed)}개가 삭제·변경되었습니다" + (f"(삭제 {gone}개)" if gone else ""))


def _dirty_refs(conn: sqlite3.Connection, kind: int) -> Iterator[list[int]]:
    last = -1
    while True:
        refs = [r[0] for r in conn.execute("SELECT ref FROM dirty WHERE kind = ? AND ref > ? ORDER BY ref LIMIT ?",
                                           (kind, last, CHUNK))]
        if not refs:
            return
        yield refs
        last = refs[-1]


def _by_id(conn: sqlite3.Connection, table: str, cols: str, ids: list[int]) -> dict[int, tuple]:
    found: dict[int, tuple] = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for row in conn.execute(f"SELECT {cols} FROM {table} WHERE id IN ({','.join('?' * len(part))})", part):
            found[row[0]] = tuple(row)
    return found


_REVERTED_SQL = f"""
    SELECT 1 FROM history_logs h WHERE h.ip_id = :ip AND h.action <> '{ACT_INTEGRITY_REPAIR}'
       AND h.id > COALESCE((SELECT MAX(r.id) FROM history_logs r
                             WHERE r.ip_id IS NULL AND r.action = '{ACT_SUBNET_CREATE}'
                               AND r.range_start BETWEEN :ip - {RANGE_LOOKBACK} AND :ip
                               AND r.range_end >= :ip), 0)
     LIMIT 1"""


def verify_dirty(conn: sqlite3.Connection, report: Report) -> list[tuple[int, int]]:
    """프로그램 밖에서 바뀐 행만 해시로 검증하고, 검증한 (kind, ref) 목록을 반환한다."""
    if not has_watch(conn):
        return []
    keys: list[tuple[int, int]] = []
    missing_subnets: set[int] = set()
    for ids in _dirty_refs(conn, DIRTY_IP):
        keys += [(DIRTY_IP, i) for i in ids]
        rows = _by_id(conn, "ips", f"{_IP_COLS}, row_hash", ids)
        for ip_id in ids:
            subnet = _containing_subnet(conn, ip_id)
            row = rows.get(ip_id)
            if row is None:
                if subnet is not None and _is_host(subnet, ip_id) and subnet["id"] not in missing_subnets:
                    missing_subnets.add(subnet["id"])
                    report.add("missing", subnet["id"], subnet["cidr"], "IP 행이 삭제되었습니다(직접 삭제)")
                continue
            data = dict(zip(ig.IP_FIELDS, row))
            if subnet is None or data["subnet_id"] != subnet["id"]:
                report.add("orphan", ip_id, int_to_ip(ip_id), "어느 대역에도 속하지 않는 IP 행입니다(직접 추가)")
                continue
            expected, stored = ig.ip_row_hash(data), row[-1]
            if expected is None:
                if stored is not None:
                    report.add("ip", ip_id, int_to_ip(ip_id), "IP 정보가 프로그램 밖에서 수정되었습니다")
                elif conn.execute(_REVERTED_SQL, {"ip": ip_id}).fetchone():
                    report.add("ip", ip_id, int_to_ip(ip_id), "사용 이력이 있는데 초기 상태로 되돌려졌습니다(직접 수정)")
            elif stored is None or not hmac.compare_digest(expected, bytes(stored)):
                report.add("ip", ip_id, int_to_ip(ip_id), "IP 정보가 프로그램 밖에서 수정되었습니다")

    for kind, table, fields, fn, label in ((DIRTY_SUBNET, "subnets", ig.SUBNET_FIELDS, ig.subnet_row_hash, "대역"),
                                           (DIRTY_GROUP, "groups", ig.GROUP_FIELDS, ig.group_row_hash, "그룹")):
        issue = "subnet" if kind == DIRTY_SUBNET else "group"
        for ids in _dirty_refs(conn, kind):
            keys += [(kind, i) for i in ids]
            extra = ", total_count, used_count, prefix" if kind == DIRTY_SUBNET else ""
            rows = _by_id(conn, table, f"{', '.join(fields)}{extra}, row_hash", ids)
            for ref in ids:
                row = rows.get(ref)
                if row is None:
                    report.add(issue, ref, f"{label} #{ref}", "프로그램 밖에서 삭제되었습니다")
                    continue
                data = dict(zip(fields, row))
                name = data.get("cidr") or data.get("name") or str(ref)
                if row[-1] is None or not hmac.compare_digest(fn(data), bytes(row[-1])):
                    report.add(issue, ref, name, "프로그램 밖에서 수정되었거나 추가되었습니다")
                if kind == DIRTY_SUBNET:
                    total_c, used_c = row[len(fields)], row[len(fields) + 1]
                    actual, used = conn.execute(
                        f"SELECT COUNT(*), COALESCE(SUM(status = '{STATUS_USED}'), 0) FROM ips "
                        f"WHERE id BETWEEN ? AND ?", (data["network_int"], data["broadcast_int"])).fetchone()
                    if (total_c, used_c) != (actual, used):
                        report.add("count", ref, name, f"통계 {used_c}/{total_c} ≠ 실제 {used}/{actual}")

    head = chain_head(conn)[0]
    for ids in _dirty_refs(conn, DIRTY_HISTORY):
        keys += [(DIRTY_HISTORY, i) for i in ids]
        for hid in ids:
            row = conn.execute(f"SELECT id, {_HIST_COLS}, chain_hash FROM history_logs WHERE id = ?",
                               (hid,)).fetchone()
            if row is None:
                if hid <= head:
                    report.add("gap", hid, f"이력 #{hid}", "이력이 삭제되었습니다")
                continue
            if hid == 1:
                prev = ig.GENESIS
            else:
                before = conn.execute("SELECT chain_hash FROM history_logs WHERE id = ?", (hid - 1,)).fetchone()
                if before is None or before[0] is None:
                    continue                                   # 직전 이력도 삭제됨 → 그쪽에서 gap으로 보고
                prev = bytes(before[0])
            stored = row[-1]
            if stored is None or not hmac.compare_digest(ig.chain_hash(prev, tuple(row[:-1])), bytes(stored)):
                report.add("chain", hid, _hist_where(tuple(row)), "이력 내용이 프로그램 밖에서 수정되었습니다")
    report.checked["dirty"] = len(keys)
    return keys


def _is_host(subnet: sqlite3.Row, ip_id: int) -> bool:
    if subnet["prefix"] >= 31:
        return subnet["network_int"] <= ip_id <= subnet["broadcast_int"]
    return subnet["network_int"] < ip_id < subnet["broadcast_int"]


def _write_anchor_safe(anchor_path: Path | None, report: Report) -> None:
    if anchor_path is None or report.head[0] == 0:
        return
    try:
        ig.write_anchor(anchor_path, *report.head)
    except OSError as exc:
        report.notes.append(f"기준값 파일 저장 실패: {exc}")


# ── 행 검사 ────────────────────────────────────────────────
def _scan(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    """id 순으로 CHUNK씩 끊어 읽는다(sql은 'id > ?'와 ORDER BY id LIMIT ? 를 포함)."""
    last = -1
    while True:
        rows = conn.execute(sql, (*params, last, CHUNK)).fetchall()
        if not rows:
            return
        yield from rows
        last = rows[-1][0]


def verify_rows(conn: sqlite3.Connection, report: Report, progress: Progress | None = None) -> None:
    """IP/대역/그룹 행 해시, 손대지 않은 IP, 통계 카운터, 누락/비정상 IP 행 검사."""
    n = 0
    total_rows =conn.execute("SELECT COUNT(*) FROM ips WHERE row_hash IS NOT NULL").fetchone()[0] or 1
    for row in _scan(conn, f"SELECT {_IP_COLS}, row_hash FROM ips WHERE row_hash IS NOT NULL AND id > ? "
                           f"ORDER BY id LIMIT ?"):
        data = dict(zip(ig.IP_FIELDS, row))
        expected = ig.ip_row_hash(data)
        if expected is None or not hmac.compare_digest(expected, bytes(row[-1])):
            report.add("ip", row[0], int_to_ip(row[0]), "IP 정보가 프로그램 밖에서 수정되었습니다")
        n += 1
        if progress and n % CHUNK == 0:
            progress(min(n / total_rows, 1.0), f"IP 행 검사 {n:,}/{total_rows:,}")
    # 해시 없이 값이 바뀐 행(손대지 않은 상태가 아님)
    for row in _scan(conn, f"SELECT id FROM ips WHERE row_hash IS NULL AND NOT ({ig.PRISTINE_SQL}) "
                           f"AND id > ? ORDER BY id LIMIT ?"):
        report.add("ip", row[0], int_to_ip(row[0]), "해시 없이 값이 바뀌었습니다(직접 수정)")
    # 손대지 않은 상태로 '되돌려 놓은' 행: 이력에는 사용 기록이 있는데 행은 초기 상태
    for row in _scan(conn, f"""
        SELECT i.id FROM ips i WHERE i.row_hash IS NULL AND i.id > ?
           AND EXISTS (SELECT 1 FROM history_logs h WHERE h.ip_id = i.id AND h.action <> '{ACT_INTEGRITY_REPAIR}'
                          AND h.id > COALESCE((SELECT MAX(r.id) FROM history_logs r
                                                WHERE r.ip_id IS NULL AND r.action = '{ACT_SUBNET_CREATE}'
                                                  AND r.range_start BETWEEN i.id - {RANGE_LOOKBACK} AND i.id
                                                  AND r.range_end >= i.id), 0))
         ORDER BY i.id LIMIT ?"""):
        report.add("ip", row[0], int_to_ip(row[0]), "사용 이력이 있는데 초기 상태로 되돌려졌습니다(직접 수정)")
    report.checked["ips"] = n

    # 대역/그룹 행 해시
    for table, fields, fn, kind in (("subnets", ig.SUBNET_FIELDS, ig.subnet_row_hash, "subnet"),
                                    ("groups", ig.GROUP_FIELDS, ig.group_row_hash, "group")):
        cnt = 0
        for row in _scan(conn, f"SELECT {', '.join(fields)}, row_hash FROM {table} WHERE id > ? ORDER BY id LIMIT ?"):
            data = dict(zip(fields, row))
            cnt += 1
            if row[-1] is None or not hmac.compare_digest(fn(data), bytes(row[-1])):
                label = data.get("cidr") or data.get("name") or str(row[0])
                report.add(kind, row[0], label, "프로그램 밖에서 수정되었거나 추가되었습니다")
        report.checked[table] = cnt
    if progress:
        progress(1.0, "사용 통계·누락 행 검사")

    # 사용 통계 카운터 + IP 누락(대역 호스트 수보다 적음)
    for row in _scan(conn, f"""
        SELECT s.id, s.cidr, s.prefix, s.total_count, s.used_count,
               (SELECT COUNT(*) FROM ips WHERE ips.id BETWEEN s.network_int AND s.broadcast_int),
               (SELECT COUNT(*) FROM ips WHERE ips.id BETWEEN s.network_int AND s.broadcast_int
                                          AND ips.status = '{STATUS_USED}')
          FROM subnets s WHERE s.id > ? ORDER BY s.id LIMIT ?"""):
        sid, cidr, prefix, total_c, used_c, actual, used = row
        hosts = 2 ** (32 - prefix) - (0 if prefix >= 31 else 2)
        if actual < hosts:
            report.add("missing", sid, cidr, f"IP 행 {hosts - actual:,}개가 없습니다(직접 삭제)")
        if (total_c, used_c) != (actual, used):
            report.add("count", sid, cidr, f"통계 {used_c}/{total_c} ≠ 실제 {used}/{actual}")
    # 어느 대역에도 속하지 않거나 소속이 맞지 않는 IP 행
    for row in _scan(conn, """
        SELECT i.id FROM ips i LEFT JOIN subnets s ON s.id = i.subnet_id
         WHERE i.id > ? AND (s.id IS NULL OR i.id < s.network_int OR i.id > s.broadcast_int)
         ORDER BY i.id LIMIT ?"""):
        report.add("orphan", row[0], int_to_ip(row[0]), "어느 대역에도 속하지 않는 IP 행입니다(직접 추가)")


def full_check(db_path: Path | None, anchor_path: Path | None, progress: Progress | None = None,
               conn: sqlite3.Connection | None = None) -> Report:
    """전체 검사. conn이 없으면 db_path로 별도 연결을 열어(백그라운드 스레드용) 검사한다."""
    started = time.perf_counter()
    own = conn is None
    if own:
        conn = create_connection(db_path)       # type: ignore[arg-type]
    try:
        report = _full_report(conn, anchor_path, progress)
        if report.ok:
            _write_anchor_safe(anchor_path, report)
            if anchor_path is not None and has_watch(conn):     # 기준 검사 완료 → 이후는 빠른 검사만
                with transaction(conn):
                    conn.execute("UPDATE meta SET value = 1 WHERE key = 'baseline'")
    finally:
        if own:
            conn.close()
    report.elapsed = time.perf_counter() - started
    report.finished_at = now_str()
    return report


def _full_report(conn: sqlite3.Connection, anchor_path: Path | None, progress: Progress | None = None) -> Report:
    report = Report("full")
    if has_watch(conn):
        check_watch(conn, report)
    check_anchor(conn, report, anchor_path)
    total = conn.execute("SELECT MAX(id) FROM history_logs").fetchone()[0] or 0
    report.head = verify_chain(conn, report, 0, ig.GENESIS, progress, total)
    verify_rows(conn, report, progress)
    return report


# ── 최초 봉인(v2 → v3 마이그레이션, 부하 DB 생성) ──────────
def seal_all(conn: sqlite3.Connection) -> None:
    """기존 데이터 전체에 해시 체인/행 해시를 부여한다(트랜잭션 안에서 호출)."""
    prev_id, prev = 0, ig.GENESIS
    while True:
        rows = conn.execute(f"SELECT id, {_HIST_COLS} FROM history_logs WHERE id > ? ORDER BY id LIMIT ?",
                            (prev_id, CHUNK)).fetchall()
        if not rows:
            break
        updates = []
        for row in rows:
            prev = ig.chain_hash(prev, tuple(row))
            updates.append((prev, row[0]))
        conn.executemany("UPDATE history_logs SET chain_hash = ? WHERE id = ?", updates)
        prev_id = rows[-1][0]
    for row_ids in _id_chunks(conn, f"SELECT id FROM ips WHERE NOT ({ig.PRISTINE_SQL}) AND id > ? ORDER BY id LIMIT ?"):
        ig.seal_ips(conn, row_ids)
    ig.seal_subnets(conn, [r[0] for r in conn.execute("SELECT id FROM subnets")])
    ig.seal_groups(conn, [r[0] for r in conn.execute("SELECT id FROM groups")])


def _id_chunks(conn: sqlite3.Connection, sql: str) -> Any:
    last = -1
    while True:
        ids = [r[0] for r in conn.execute(sql, (last, CHUNK)).fetchall()]
        if not ids:
            return
        yield ids
        last = ids[-1]


# ── 복구 1: 이력 기준 행 복구 ──────────────────────────────
def _containing_subnet(conn: sqlite3.Connection, ip_id: int) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM subnets WHERE network_int <= ? ORDER BY network_int DESC LIMIT 1",
                       (ip_id,)).fetchone()
    return row if row and row["broadcast_int"] >= ip_id else None


def _rebuild_ip(conn: sqlite3.Connection, ip_id: int, operator: str, ts: str) -> str:
    """이력의 마지막 정상 스냅샷으로 IP 1개를 재구성. 결과: 'restored' | 'pristine' | 'deleted'."""
    subnet = _containing_subnet(conn, ip_id)
    current = conn.execute(f"SELECT {_IP_COLS} FROM ips WHERE id = ?", (ip_id,)).fetchone()
    if subnet is None:
        conn.execute("DELETE FROM ips WHERE id = ?", (ip_id,))
        return "deleted"
    last_ev = conn.execute("SELECT * FROM history_logs WHERE ip_id = ? ORDER BY id DESC LIMIT 1",
                           (ip_id,)).fetchone()
    reset_at = conn.execute(f"""SELECT MAX(id) FROM history_logs WHERE ip_id IS NULL AND action = ?
                                AND range_start BETWEEN ? AND ? AND range_end >= ?""",
                            (ACT_SUBNET_CREATE, ip_id - RANGE_LOOKBACK, ip_id, ip_id)).fetchone()[0] or 0
    if last_ev is not None and last_ev["id"] > reset_at and last_ev["action"] not in SNAPSHOT_ACTIONS:
        conn.execute("DELETE FROM ips WHERE id = ?", (ip_id,))        # 분할 등으로 제외된 주소가 되살아난 경우
        return "deleted"
    if last_ev is not None and last_ev["id"] > reset_at:
        new = {"status": last_ev["new_status"] or STATUS_UNUSED, "hostname": last_ev["hostname"],
               "allocated_date": last_ev["allocated_date"], "assignee": last_ev["assignee"],
               "request_no": last_ev["request_no"], "note": last_ev["note"],
               "updated_at": last_ev["timestamp"], "updated_by": last_ev["operator"]}
        version = (current["version"] if current else 1) + 1
        result = "restored"
    else:
        new = {"status": STATUS_UNUSED, "hostname": "", "allocated_date": "", "assignee": "", "request_no": "",
               "note": "", "updated_at": "", "updated_by": ""}
        version = 1
        result = "pristine"
    if current is None:
        conn.execute("INSERT INTO ips(id, subnet_id) VALUES (?, ?)", (ip_id, subnet["id"]))
    conn.execute("""UPDATE ips SET subnet_id = ?, status = ?, hostname = ?, allocated_date = ?, assignee = ?,
                    request_no = ?, note = ?, version = ?, updated_at = ?, updated_by = ? WHERE id = ?""",
                 (subnet["id"], new["status"], new["hostname"], new["allocated_date"], new["assignee"],
                  new["request_no"], new["note"], version, new["updated_at"], new["updated_by"], ip_id))
    ig.seal_ips(conn, [ip_id])
    if result == "restored":
        before = dict(zip(ig.IP_FIELDS, current)) if current else {}
        row = dict(zip(ig.IP_FIELDS, conn.execute(f"SELECT {_IP_COLS} FROM ips WHERE id = ?", (ip_id,)).fetchone()))
        changed = [k for k in ("status", "hostname", "assignee", "request_no", "note", "allocated_date")
                   if before.get(k) != row[k]]
        insert_entries(conn, [snapshot_entry(
            row, action=ACT_INTEGRITY_REPAIR, old_status=before.get("status", ""), new_status=row["status"],
            ip_address=int_to_ip(ip_id), operator=operator, timestamp=ts,
            diff_summary="DB 직접 수정 감지 → 마지막 정상 이력 기준으로 복구"
                         + (f" (변조 항목: {', '.join(changed)})" if changed else ""))])
    return result


def repair_rows(conn: sqlite3.Connection, operator: str, anchor_path: Path | None = None,
                scope: str = "quick") -> dict[str, int]:
    """이력 체인이 정상일 때: 변조/누락/비정상 IP 행을 이력 기준으로 되돌리고 통계를 재계산한다.

    scope="quick"이면 외부 수정 기록(dirty) 행만, "full"이면 모든 행을 다시 검사해 대상을 정한다.
    """
    with transaction(conn):
        if scope == "full":
            report, verified = _full_report(conn, anchor_path), True
        else:
            report, anchor, _ = _quick_report(conn, anchor_path)
            verified = anchor is not None                 # 기준값 이후 체인까지 검증된 경우에만 기준값 갱신
        if set(report.counts) - ROW_REPAIRABLE:
            raise ValidationError("이력·대역·그룹 정보가 변조되어 이력 기준 복구를 할 수 없습니다. 정상 백업으로 원복하세요.")
        ts = now_str()
        result = Counter()
        targets = set(report.targets.get("ip", [])) | set(report.targets.get("orphan", []))
        for sid in report.targets.get("missing", []):
            s = conn.execute("SELECT network_int, broadcast_int, prefix FROM subnets WHERE id = ?", (sid,)).fetchone()
            first, last = (s[0], s[1]) if s[2] >= 31 else (s[0] + 1, s[1] - 1)
            have = {r[0] for r in conn.execute("SELECT id FROM ips WHERE id BETWEEN ? AND ?", (first, last))}
            targets.update(i for i in range(first, last + 1) if i not in have)
        for ip_id in sorted(targets):
            result[_rebuild_ip(conn, ip_id, operator, ts)] += 1
        conn.execute(f"""UPDATE subnets SET
              total_count = (SELECT COUNT(*) FROM ips WHERE ips.id BETWEEN subnets.network_int AND subnets.broadcast_int),
              used_count  = (SELECT COUNT(*) FROM ips WHERE ips.id BETWEEN subnets.network_int AND subnets.broadcast_int
                                                       AND ips.status = '{STATUS_USED}')""")
        insert_entries(conn, [make_entry(action=ACT_INTEGRITY_REPAIR, operator=operator, timestamp=ts,
                                         diff_summary=f"이력 기준 복구: 복원 {result['restored']}건, 초기화 "
                                                      f"{result['pristine']}건, 제거 {result['deleted']}건, "
                                                      f"통계 재계산")])
        if has_watch(conn):
            conn.execute("DELETE FROM dirty")
        bump_revision(conn, tree=True)
    if verified:
        _write_anchor_safe(anchor_path, Report("quick", head=chain_head(conn)))
    return dict(result)


# ── 복구 2: 정상 백업으로 원복 + 이후 정상 이력 재적용 ─────
def restore_from_backup(conn: sqlite3.Connection, backup_path: Path, tampered_dir: Path,
                        anchor_path: Path | None, operator: str) -> dict[str, Any]:
    """정상 백업으로 원복한다. 원복 전 DB는 변조본으로 보관하고, 백업 이후 정상 이력은 다시 적용한다."""
    if not Path(backup_path).exists():
        raise ValidationError("정상 백업 파일이 없습니다.")
    bconn = create_connection(backup_path)
    try:
        backup_report = full_check(None, None, conn=bconn)
        if not backup_report.ok:
            raise ValidationError(f"백업 파일도 무결성 검사를 통과하지 못했습니다({backup_report.summary()}).\n"
                                  "직전 정상 백업을 사용하세요.")
        b_id, b_hash = backup_report.head
        # 1) 현재 DB에서 백업 시점 이후로 체인이 이어지는 정상 이력 수집
        replay: list[tuple] = []
        row = conn.execute("SELECT chain_hash FROM history_logs WHERE id = ?", (b_id,)).fetchone()
        if b_id == 0 or (row is not None and row[0] is not None and hmac.compare_digest(bytes(row[0]), b_hash)):
            prev_id, prev = b_id, b_hash
            stop = False
            while not stop:
                rows = conn.execute(f"SELECT id, {_HIST_COLS}, chain_hash FROM history_logs WHERE id > ? "
                                    f"ORDER BY id LIMIT ?", (prev_id, CHUNK)).fetchall()
                if not rows:
                    break
                for r in rows:
                    if r[0] != prev_id + 1 or r[-1] is None or not hmac.compare_digest(
                            ig.chain_hash(prev, tuple(r[:-1])), bytes(r[-1])):
                        stop = True
                        break
                    replay.append(tuple(r))
                    prev_id, prev = r[0], bytes(r[-1])
        # 2) 현재(변조된) DB 보관
        tampered_dir.mkdir(parents=True, exist_ok=True)
        saved = tampered_dir / f"{TAMPERED_PREFIX}{datetime.now():%Y%m%d_%H%M%S}.db"
        dst = sqlite3.connect(str(saved))
        try:
            conn.backup(dst)
        finally:
            dst.close()
        # 3) 정상 백업 → 현재 DB 덮어쓰기
        bconn.backup(conn)
    finally:
        bconn.close()
    # 4) 백업 이후 정상 이력 재적용
    manual: list[str] = []
    applied = 0
    with transaction(conn):
        if replay:
            conn.executemany(f"INSERT INTO history_logs(id, {_HIST_COLS}, chain_hash) "
                             f"VALUES ({', '.join('?' * (len(HISTORY_COLUMNS) + 2))})", replay)
        for r in replay:
            ev = dict(zip(("id", *HISTORY_COLUMNS), r[:-1]))
            if ev["ip_id"] is not None and ev["action"] in SNAPSHOT_ACTIONS:
                cur = conn.execute("UPDATE ips SET status = ?, hostname = ?, allocated_date = ?, assignee = ?, "
                                   "request_no = ?, note = ?, version = version + 1, updated_at = ?, updated_by = ? "
                                   "WHERE id = ?",
                                   (ev["new_status"] or STATUS_UNUSED, ev["hostname"], ev["allocated_date"],
                                    ev["assignee"], ev["request_no"], ev["note"], ev["timestamp"], ev["operator"],
                                    ev["ip_id"]))
                if cur.rowcount:
                    ig.seal_ips(conn, [ev["ip_id"]])
                    applied += 1
                else:
                    manual.append(f"{ev['timestamp']} {ev['action']} {ev['ip_address']} (대상 IP 없음)")
            else:
                manual.append(f"{ev['timestamp']} {ev['action']} {ev['ip_address'] or ''} — {ev['diff_summary']}")
        conn.execute(f"""UPDATE subnets SET
              total_count = (SELECT COUNT(*) FROM ips WHERE ips.id BETWEEN subnets.network_int AND subnets.broadcast_int),
              used_count  = (SELECT COUNT(*) FROM ips WHERE ips.id BETWEEN subnets.network_int AND subnets.broadcast_int
                                                       AND ips.status = '{STATUS_USED}')""")
        insert_entries(conn, [make_entry(
            action=ACT_INTEGRITY_REPAIR, operator=operator, timestamp=now_str(),
            diff_summary=f"정상 백업({Path(backup_path).name}, 이력 #{b_id})으로 원복 · 이후 정상 이력 "
                         f"{len(replay):,}건 재적용(IP 반영 {applied:,}건) · 원복 전 DB 보관: {saved.name}")])
        conn.execute("UPDATE meta SET value = value + 1000 WHERE key IN ('data_rev', 'tree_rev')")
    head = chain_head(conn)
    if anchor_path is not None:
        anchor_path.unlink(missing_ok=True)      # 원복으로 체인이 짧아졌을 수 있으므로 새 기준값으로 교체
        ig.write_anchor(anchor_path, *head)
    return {"backup_head": b_id, "replayed": len(replay), "applied": applied, "manual": manual,
            "tampered_copy": str(saved)}


# ── 원클릭 복구 ────────────────────────────────────────────
@contextmanager
def recovery_lock(lock_dir: Path, operator: str) -> Iterator[None]:
    """공유 드라이브에서 한 명만 복구하도록 잠금 파일을 만든다(비정상 종료로 남은 오래된 잠금은 무시)."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / LOCK_NAME
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                info = {}
            try:
                stale = time.time() - path.stat().st_mtime > LOCK_STALE_SEC
            except OSError:
                stale = True
            if not stale:
                raise ValidationError(f"{info.get('operator', '다른 사용자')}님이 {info.get('started', '')} 부터 "
                                      f"복구를 진행하고 있습니다.\n잠시 후 자동으로 정상 상태로 돌아옵니다.")
            path.unlink(missing_ok=True)
    else:
        raise ValidationError("복구 잠금을 만들 수 없습니다. 잠시 후 다시 시도하세요.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"operator": operator, "started": now_str()}, f, ensure_ascii=False)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def recover(conn: sqlite3.Connection, anchor_path: Path | None, backup_dir: Path, operator: str,
            scope: str = "quick") -> dict[str, Any]:
    """변조 확인 후 '최종 정상 버전'으로 복구한다. 방식은 자동 선택:
    행만 변조 → 이력 기준 복구(손실 없음), 그 밖 → 정상 백업 → 직전 정상 백업 순으로 원복 + 이후 이력 재적용."""
    with recovery_lock(backup_dir, operator):
        report = _full_report(conn, anchor_path) if scope == "full" else _quick_report(conn, anchor_path)[0]
        if report.ok:
            return {"method": "none"}
        if report.repairable_rows:
            return {"method": "rows", "found": report.summary(), **repair_rows(conn, operator, anchor_path, scope)}
        errors: list[str] = []
        for name in (GOOD_BACKUP_NAME, PREV_BACKUP_NAME):
            path = backup_dir / name
            if not path.exists():
                continue
            try:
                res = restore_from_backup(conn, path, backup_dir, anchor_path, operator)
            except ValidationError as exc:
                errors.append(f"{name}: {exc}")
                continue
            return {"method": "backup", "found": report.summary(), "backup": name, **res}
        detail = "\n".join(errors) if errors else "정상 백업 파일이 없습니다."
        raise ValidationError(f"복구에 사용할 정상 백업이 없습니다.\n{detail}\n\n"
                              f"Backup 폴더를 확인하거나 관리자에게 문의하세요.")
