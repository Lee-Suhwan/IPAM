"""history_logs 기록 및 조회 (개별 IP 생애주기 / 대역 전체 이력)."""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Mapping

from ipmgr.db.schema import RANGE_LOOKBACK
from ipmgr.logic.integrity import GENESIS, chain_hash

HISTORY_COLUMNS: tuple[str, ...] = (
    "subnet_id", "ip_id", "ip_address", "range_start", "range_end", "action",
    "old_status", "new_status", "hostname", "assignee", "request_no", "note",
    "allocated_date", "diff_summary", "operator", "timestamp",
)
_INSERT_SQL = (f"INSERT INTO history_logs(id, {', '.join(HISTORY_COLUMNS)}, chain_hash) "
               f"VALUES (?, {', '.join('?' * len(HISTORY_COLUMNS))}, ?)")
_TEXT_DEFAULTS = {"ip_address", "old_status", "new_status", "hostname", "assignee", "request_no",
                  "note", "allocated_date", "diff_summary", "operator"}


def make_entry(**fields: Any) -> tuple:
    """키워드 인자로 이력 행 튜플을 만든다. 누락된 텍스트 필드는 ''로 채운다."""
    unknown = set(fields) - set(HISTORY_COLUMNS)
    if unknown:
        raise KeyError(f"알 수 없는 이력 컬럼: {unknown}")
    return tuple(fields.get(c, "" if c in _TEXT_DEFAULTS else None) for c in HISTORY_COLUMNS)


def snapshot_entry(ip_row: Mapping[str, Any], *, action: str, old_status: str, new_status: str,
                   ip_address: str, diff_summary: str, operator: str, timestamp: str) -> tuple:
    """IP 행의 현재 속성을 스냅샷으로 담은 이력 행을 만든다."""
    return make_entry(
        subnet_id=ip_row["subnet_id"], ip_id=ip_row["id"], ip_address=ip_address, action=action,
        old_status=old_status, new_status=new_status, hostname=ip_row["hostname"],
        assignee=ip_row["assignee"], request_no=ip_row["request_no"], note=ip_row["note"],
        allocated_date=ip_row["allocated_date"], diff_summary=diff_summary,
        operator=operator, timestamp=timestamp)


def chain_head(conn: sqlite3.Connection) -> tuple[int, bytes]:
    """이력 체인의 마지막 (id, 해시). 이력이 없으면 (0, GENESIS)."""
    row = conn.execute("SELECT id, chain_hash FROM history_logs ORDER BY id DESC LIMIT 1").fetchone()
    return (row[0], row[1] or GENESIS) if row else (0, GENESIS)


def insert_entries(conn: sqlite3.Connection, entries: Iterable[tuple]) -> None:
    """이력 행을 해시 체인으로 이어 일괄 기록한다.

    반드시 쓰기 트랜잭션(BEGIN IMMEDIATE) 안에서 호출된다 → 마지막 행 조회와 추가가 다른 사용자와 직렬화된다.
    """
    entries = list(entries)
    if not entries:
        return
    last_id, prev = chain_head(conn)
    rows = []
    for entry in entries:
        last_id += 1
        prev = chain_hash(prev, (last_id, *entry))
        rows.append((last_id, *entry, prev))
    conn.executemany(_INSERT_SQL, rows)


def ip_timeline(conn: sqlite3.Connection, ip_id: int, newest_first: bool = True) -> list[dict[str, Any]]:
    """특정 IP의 전체 생애주기 이력.

    IP 단위 이벤트(할당/회수/수정/삭제) + 해당 IP를 포함하는 범위 이벤트(대역 생성/삭제 등).
    삭제 후 재생성된 IP도 IP 정수값이 같으므로 과거 이력이 이어진다.
    """
    order = "DESC" if newest_first else "ASC"
    rows = conn.execute(f"""
        SELECT * FROM (
            SELECT * FROM history_logs WHERE ip_id = ?
            UNION ALL
            SELECT * FROM history_logs
             WHERE ip_id IS NULL AND range_start BETWEEN ? AND ? AND range_end >= ?
        ) ORDER BY timestamp {order}, id {order}
    """, (ip_id, ip_id - RANGE_LOOKBACK, ip_id, ip_id)).fetchall()
    return [dict(r) for r in rows]


def history_page(conn: sqlite3.Connection, subnet: Mapping[str, Any], ip_ids: list[int] | None,
                 newest_first: bool = True, limit: int = 300) -> dict[str, Any]:
    """이력 패널용: 선택 IP들(없으면 대역 전체)의 이력을 최대 limit건 반환한다.

    선택 IP가 있으면 해당 IP 이벤트 + 그 IP들을 포함하는 범위 이벤트(대역 생성/삭제)만,
    없으면 대역 전체 이력(subnet_id 이벤트 + 대역 내 모든 IP 이벤트).
    """
    order = "DESC" if newest_first else "ASC"
    if ip_ids:
        ids = sorted(set(ip_ids))[:1000]
        marks = ",".join("?" * len(ids))
        sql = f"""
            SELECT * FROM (
                SELECT * FROM history_logs WHERE ip_id IN ({marks})
                UNION ALL
                SELECT * FROM history_logs
                 WHERE ip_id IS NULL AND range_start BETWEEN ? AND ? AND range_end >= ?
            ) ORDER BY timestamp {order}, id {order} LIMIT ?
        """
        params = [*ids, ids[0] - RANGE_LOOKBACK, ids[-1], ids[0], limit + 1]
    else:
        sql = f"""
            SELECT * FROM (
                SELECT * FROM history_logs WHERE subnet_id = ?
                UNION
                SELECT * FROM history_logs WHERE ip_id BETWEEN ? AND ?
            ) ORDER BY timestamp {order}, id {order} LIMIT ?
        """
        params = [subnet["id"], subnet["network_int"], subnet["broadcast_int"], limit + 1]
    rows = [dict(r) for r in conn.execute(sql, params)]
    return {"rows": rows[:limit], "has_more": len(rows) > limit}


def recent_history(conn: sqlite3.Connection, before_id: int | None = None, limit: int = 200) -> dict[str, Any]:
    """통합 검색 기본 화면: 전체 이력을 최신순으로 limit건씩(before_id 이전) 반환한다.

    id(PK) 역순 범위 조회라 이력이 수백만 건이어도 페이지마다 limit건만 읽는다.
    nav는 더블클릭 시 이동할 IP(IP 이벤트는 그 IP, 대역 이벤트는 대역 첫 호스트).
    """
    rows = conn.execute("""
        SELECT h.id, h.timestamp, h.action, h.ip_address, h.old_status, h.new_status, h.hostname, h.assignee,
               h.request_no, h.operator, h.diff_summary, s.cidr,
               COALESCE(h.ip_id, s.network_int + 1, h.range_start + 1) AS nav
          FROM history_logs h LEFT JOIN subnets s ON s.id = h.subnet_id
         WHERE h.id < ? ORDER BY h.id DESC LIMIT ?
    """, (before_id if before_id is not None else 1 << 62, limit + 1)).fetchall()
    rows = [dict(r) for r in rows]
    return {"rows": rows[:limit], "has_more": len(rows) > limit}


def subnet_history(conn: sqlite3.Connection, subnet_id: int, network_int: int, broadcast_int: int,
                   newest_first: bool = False) -> list[dict[str, Any]]:
    """대역 전체 이력: 해당 subnet_id 이벤트 + 대역 범위 내 IP들의 모든 이벤트."""
    order = "DESC" if newest_first else "ASC"
    rows = conn.execute(f"""
        SELECT * FROM (
            SELECT * FROM history_logs WHERE subnet_id = ?
            UNION
            SELECT * FROM history_logs WHERE ip_id BETWEEN ? AND ?
        ) ORDER BY timestamp {order}, id {order}
    """, (subnet_id, network_int, broadcast_int)).fetchall()
    return [dict(r) for r in rows]
