"""그룹(무한 중첩) CRUD, 이동(순환 참조 방어), 트리 지연 로딩용 조회."""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from ipmgr.constants import (ACT_GROUP_CREATE, ACT_GROUP_DELETE, ACT_GROUP_MOVE, ACT_GROUP_UPDATE,
                            ACT_SUBNET_DELETE, MAX_TEXT_LEN)
from ipmgr.db.connection import transaction
from ipmgr.logic.bulk_ops import delete_ip_ranges
from ipmgr.logic.integrity import seal_groups
from ipmgr.logic.subnet_calc import natural_key
from ipmgr.models.common import NotFoundError, ValidationError, bump_revision, now_str
from ipmgr.models.history_model import insert_entries, make_entry

_DESC_CTE = """
WITH RECURSIVE d(id, depth) AS (
    SELECT id, 0 FROM groups WHERE id = ?
    UNION ALL
    SELECT g.id, d.depth + 1 FROM groups g JOIN d ON g.parent_id = d.id WHERE d.depth < 1000
)
"""


# ── 조회 ───────────────────────────────────────────────────
def get_group(conn: sqlite3.Connection, group_id: int) -> dict[str, Any] | None:
    """그룹 1건 조회."""
    row = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    return dict(row) if row else None


def require_group(conn: sqlite3.Connection, group_id: int) -> dict[str, Any]:
    """그룹 조회(없으면 NotFoundError)."""
    group = get_group(conn, group_id)
    if group is None:
        raise NotFoundError("그룹이 존재하지 않습니다. 다른 사용자가 삭제했을 수 있습니다.")
    return group


def ancestor_chain(conn: sqlite3.Connection, group_id: int) -> list[dict[str, Any]]:
    """루트 → 자신 순서의 조상 그룹 목록."""
    rows = conn.execute("""
        WITH RECURSIVE a(id, parent_id, name, depth) AS (
            SELECT id, parent_id, name, 0 FROM groups WHERE id = ?
            UNION ALL
            SELECT g.id, g.parent_id, g.name, a.depth + 1
              FROM groups g JOIN a ON g.id = a.parent_id WHERE a.depth < 1000
        ) SELECT id, name FROM a ORDER BY depth DESC
    """, (group_id,)).fetchall()
    return [dict(r) for r in rows]


NO_GROUP_LABEL = "(그룹 없음)"


def group_path(conn: sqlite3.Connection, group_id: int | None) -> str:
    """'본사 > 서버팜 > DMZ' 형태의 경로. 그룹이 없으면 '(그룹 없음)'."""
    if group_id is None:
        return NO_GROUP_LABEL
    return " > ".join(r["name"] for r in ancestor_chain(conn, group_id))


def group_chain_ids(conn: sqlite3.Connection, group_id: int | None) -> list[int]:
    """루트 → 해당 그룹까지의 ID 목록(그룹 없음이면 빈 목록)."""
    return [] if group_id is None else [g["id"] for g in ancestor_chain(conn, group_id)]


def all_group_paths(conn: sqlite3.Connection) -> dict[int, str]:
    """전체 그룹의 경로 맵(Export/검색 결과 표시용)."""
    rows = conn.execute("SELECT id, parent_id, name FROM groups").fetchall()
    parent = {r["id"]: r["parent_id"] for r in rows}
    name = {r["id"]: r["name"] for r in rows}
    cache: dict[int, str] = {}

    def path(gid: int) -> str:
        if gid in cache:
            return cache[gid]
        chain, cur, seen = [], gid, set()
        while cur is not None and cur not in seen and cur in name:
            seen.add(cur)
            if cur in cache:
                chain.append(cache[cur])
                break
            chain.append(name[cur])
            cur = parent[cur]
        result = " > ".join(reversed(chain))
        cache[gid] = result
        return result

    return {gid: path(gid) for gid in name}


def descendant_ids(conn: sqlite3.Connection, group_id: int) -> list[int]:
    """자신을 포함한 모든 하위 그룹 ID."""
    return [r[0] for r in conn.execute(_DESC_CTE + "SELECT id FROM d", (group_id,))]


def group_stats(conn: sqlite3.Connection, group_id: int) -> dict[str, int]:
    """그룹 산하 전체(하위 그룹 포함) 대역 수/IP 수/사용중 수."""
    row = conn.execute(_DESC_CTE + """
        SELECT COUNT(s.id), COALESCE(SUM(s.total_count), 0), COALESCE(SUM(s.used_count), 0)
          FROM subnets s WHERE s.group_id IN (SELECT id FROM d)
    """, (group_id,)).fetchone()
    child_groups = conn.execute("SELECT COUNT(*) FROM groups WHERE parent_id = ?", (group_id,)).fetchone()[0]
    return {"subnet_count": row[0], "total": row[1], "used": row[2], "child_groups": child_groups}


def list_children(conn: sqlite3.Connection, parent_id: int | None) -> dict[str, list[dict[str, Any]]]:
    """트리 지연 로딩: 직속 하위 그룹(산하 사용률 포함)과 직속 서브넷 목록."""
    groups = [dict(r) for r in conn.execute("""
        SELECT g.id, g.name, g.description, g.sort_order,
               EXISTS(SELECT 1 FROM groups c WHERE c.parent_id = g.id) AS has_groups,
               EXISTS(SELECT 1 FROM subnets s WHERE s.group_id = g.id) AS has_subnets
          FROM groups g WHERE g.parent_id IS ?
    """, (parent_id,))]
    if groups:
        stats = {r[0]: (r[1], r[2], r[3]) for r in conn.execute("""
            WITH RECURSIVE d(root, id, depth) AS (
                SELECT id, id, 0 FROM groups WHERE parent_id IS ?
                UNION ALL
                SELECT d.root, g.id, d.depth + 1 FROM groups g JOIN d ON g.parent_id = d.id
                 WHERE d.depth < 1000
            )
            SELECT d.root, COUNT(s.id), COALESCE(SUM(s.total_count), 0), COALESCE(SUM(s.used_count), 0)
              FROM d LEFT JOIN subnets s ON s.group_id = d.id GROUP BY d.root
        """, (parent_id,))}
        for g in groups:
            g["subnet_count"], g["total"], g["used"] = stats.get(g["id"], (0, 0, 0))
            has_groups, has_subnets = g.pop("has_groups"), g.pop("has_subnets")
            g["has_children"] = bool(has_groups or has_subnets)
    # parent_id가 None이면 그룹에 속하지 않은 최상위 대역
    subnets = [dict(r) for r in conn.execute("""
        SELECT id, group_id, cidr, network_int, description, total_count, used_count, sort_order
          FROM subnets WHERE group_id IS ?
    """, (parent_id,))]
    # 표시 순서: 사용자가 정한 sort_order → (기본) 그룹 먼저 이름순, 대역은 IP 순
    items = [("group", g) for g in groups] + [("subnet", s) for s in subnets]
    items.sort(key=_display_key)
    groups = [d for k, d in items if k == "group"]
    subnets = [d for k, d in items if k == "subnet"]
    return {"groups": groups, "subnets": subnets, "items": items}


def _display_key(item: tuple[str, dict[str, Any]]) -> tuple:
    kind, data = item
    if kind == "group":
        return (data["sort_order"], 0, natural_key(data["name"]), 0)
    return (data["sort_order"], 1, [], data["network_int"])


def ordered_children(conn: sqlite3.Connection, parent_id: int | None) -> list[tuple[str, int]]:
    """트리에 보이는 순서대로 직속 자식 (종류, ID) 목록."""
    return [(k, d["id"]) for k, d in list_children(conn, parent_id)["items"]]


def next_sort_order(conn: sqlite3.Connection, parent_id: int | None) -> int:
    """새로 들어오는 항목의 표시 순서: 순서를 직접 정한 위치면 맨 끝, 아니면 기본 정렬(0)."""
    top = conn.execute("""SELECT MAX(m) FROM (SELECT MAX(sort_order) AS m FROM groups WHERE parent_id IS ?
                          UNION ALL SELECT MAX(sort_order) FROM subnets WHERE group_id IS ?)""",
                       (parent_id, parent_id)).fetchone()[0] or 0
    return top + 1 if top > 0 else 0


def tree_snapshot(conn: sqlite3.Connection, parent_ids: Iterable[int | None]) -> dict[int | None, Any]:
    """여러 부모의 자식 목록을 한 번의 Worker 작업으로 조회한다(트리 갱신용)."""
    result: dict[int | None, Any] = {}
    for pid in dict.fromkeys(parent_ids):
        if pid is None or get_group(conn, pid) is not None:
            result[pid] = list_children(conn, pid)
    return result


# ── 쓰기 ───────────────────────────────────────────────────
def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValidationError("그룹 이름을 입력하세요.")
    if len(name) > MAX_TEXT_LEN:
        raise ValidationError(f"그룹 이름은 {MAX_TEXT_LEN}자 이하로 입력하세요.")
    return name


def _check_sibling(conn: sqlite3.Connection, parent_id: int | None, name: str, exclude_id: int | None) -> None:
    row = conn.execute("SELECT id FROM groups WHERE parent_id IS ? AND name = ? AND id IS NOT ?",
                       (parent_id, name, exclude_id)).fetchone()
    if row:
        raise ValidationError(f"같은 위치에 '{name}' 그룹이 이미 있습니다.")


def create_group(conn: sqlite3.Connection, name: str, parent_id: int | None, description: str,
                 operator: str) -> int:
    """그룹을 생성하고 ID를 반환한다."""
    name = _validate_name(name)
    description = (description or "").strip()
    with transaction(conn):
        if parent_id is not None:
            require_group(conn, parent_id)
        _check_sibling(conn, parent_id, name, None)
        ts = now_str()
        gid = conn.execute(
            "INSERT INTO groups(parent_id, name, description, created_at, updated_at, updated_by, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (parent_id, name, description, ts, ts, operator, next_sort_order(conn, parent_id))).lastrowid
        seal_groups(conn, [gid])
        insert_entries(conn, [make_entry(action=ACT_GROUP_CREATE, diff_summary=f"그룹 생성: {name}",
                                         operator=operator, timestamp=ts)])
        bump_revision(conn, tree=True)
    return int(gid)


def update_group(conn: sqlite3.Connection, group_id: int, name: str, description: str, operator: str) -> None:
    """그룹 이름/설명 변경."""
    name = _validate_name(name)
    description = (description or "").strip()
    with transaction(conn):
        group = require_group(conn, group_id)
        _check_sibling(conn, group["parent_id"], name, group_id)
        ts = now_str()
        conn.execute("UPDATE groups SET name = ?, description = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                     (name, description, ts, operator, group_id))
        seal_groups(conn, [group_id])
        diff = []
        if group["name"] != name:
            diff.append(f"이름: '{group['name']}' → '{name}'")
        if group["description"] != description:
            diff.append(f"설명: '{group['description']}' → '{description}'")
        insert_entries(conn, [make_entry(action=ACT_GROUP_UPDATE, operator=operator, timestamp=ts,
                                         diff_summary=f"그룹 '{name}' " + ("; ".join(diff) or "변경 없음"))])
        bump_revision(conn, tree=True)


def move_group(conn: sqlite3.Connection, group_id: int, new_parent_id: int | None, operator: str) -> None:
    """그룹을 다른 부모 아래로 이동한다. 자기 자신/하위 그룹으로의 이동은 차단."""
    with transaction(conn):
        group = require_group(conn, group_id)
        if new_parent_id is not None:
            require_group(conn, new_parent_id)
            if new_parent_id == group_id or new_parent_id in set(descendant_ids(conn, group_id)):
                raise ValidationError("순환 참조 방지: 그룹을 자기 자신 또는 자신의 하위 그룹으로 이동할 수 없습니다.")
        if group["parent_id"] == new_parent_id:
            return
        _check_sibling(conn, new_parent_id, group["name"], group_id)
        old_path = group_path(conn, group["parent_id"]) if group["parent_id"] is not None else "(최상위)"
        new_path = group_path(conn, new_parent_id) if new_parent_id is not None else "(최상위)"
        ts = now_str()
        conn.execute("UPDATE groups SET parent_id = ?, updated_at = ?, updated_by = ?, sort_order = ? WHERE id = ?",
                     (new_parent_id, ts, operator, next_sort_order(conn, new_parent_id), group_id))
        seal_groups(conn, [group_id])
        insert_entries(conn, [make_entry(action=ACT_GROUP_MOVE, operator=operator, timestamp=ts,
                                         diff_summary=f"그룹 '{group['name']}' 이동: {old_path} → {new_path}")])
        bump_revision(conn, tree=True)


def delete_group(conn: sqlite3.Connection, group_id: int, operator: str) -> dict[str, int]:
    """그룹과 모든 하위 그룹/서브넷/IP를 삭제한다. '사용중' IP가 하나라도 있으면 차단."""
    with transaction(conn):
        group = require_group(conn, group_id)
        stats = group_stats(conn, group_id)
        if stats["used"] > 0:
            raise ValidationError(f"하위에 '사용중' IP가 {stats['used']:,}개 있어 삭제할 수 없습니다.\n"
                                  "먼저 해당 IP를 회수(미사용)하세요.")
        groups = conn.execute(_DESC_CTE + "SELECT id, depth FROM d", (group_id,)).fetchall()
        ids = [r["id"] for r in groups]
        ts = now_str()
        subnets = conn.execute(_DESC_CTE + """
            SELECT id, cidr, network_int, broadcast_int, total_count FROM subnets
             WHERE group_id IN (SELECT id FROM d)
        """, (group_id,)).fetchall()
        path = group_path(conn, group_id)
        entries = [make_entry(subnet_id=s["id"], ip_address=s["cidr"], range_start=s["network_int"],
                              range_end=s["broadcast_int"], action=ACT_SUBNET_DELETE, operator=operator,
                              timestamp=ts, diff_summary=f"그룹 '{path}' 삭제로 대역 삭제 "
                                                         f"(IP {s['total_count']:,}개 제거)")
                   for s in subnets]
        entries.append(make_entry(action=ACT_GROUP_DELETE, operator=operator, timestamp=ts,
                                  diff_summary=f"그룹 삭제: {path} (하위 그룹 {len(ids) - 1}개, "
                                               f"대역 {len(subnets)}개)"))
        insert_entries(conn, entries)
        # 서브넷을 먼저 지우면 IP 삭제 트리거가 갱신할 대상이 없어 대량 삭제가 빨라진다.
        conn.executemany("DELETE FROM subnets WHERE id = ?", [(s["id"],) for s in subnets])
        delete_ip_ranges(conn, [(s["network_int"], s["broadcast_int"]) for s in subnets])
        for row in sorted(groups, key=lambda r: -r["depth"]):
            conn.execute("DELETE FROM groups WHERE id = ?", (row["id"],))
        bump_revision(conn, tree=True)
    return {"groups": len(ids), "subnets": len(subnets), "ips": stats["total"]}
