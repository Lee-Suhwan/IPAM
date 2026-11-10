"""서브넷(IP 대역) CRUD, 중복 대역 방지, 그룹 대시보드(최대 256개 대역 통계)."""
from __future__ import annotations

import sqlite3
from typing import Any

from ipmgr.constants import (ACT_SUBNET_CREATE, ACT_SUBNET_DELETE, ACT_SUBNET_MOVE, ACT_SUBNET_UPDATE,
                            MAX_SPLIT_SUBNETS, MAX_SUBNET_DISPLAY, MAX_TEXT_LEN)
from ipmgr.db.connection import transaction
from ipmgr.logic.bulk_ops import bulk_insert_ips, delete_ip_ranges
from ipmgr.logic.integrity import seal_subnets
from ipmgr.logic.subnet_calc import SubnetInfo, int_to_ip, parse_cidr, split_cidr
from ipmgr.models import group_model
from ipmgr.models.common import NotFoundError, ValidationError, bump_revision, now_str
from ipmgr.models.history_model import insert_entries, make_entry


# ── 조회 ───────────────────────────────────────────────────
def get_subnet(conn: sqlite3.Connection, subnet_id: int) -> dict[str, Any] | None:
    """서브넷 1건 조회."""
    row = conn.execute("SELECT * FROM subnets WHERE id = ?", (subnet_id,)).fetchone()
    return dict(row) if row else None


def require_subnet(conn: sqlite3.Connection, subnet_id: int) -> dict[str, Any]:
    """서브넷 조회(없으면 NotFoundError)."""
    subnet = get_subnet(conn, subnet_id)
    if subnet is None:
        raise NotFoundError("대역이 존재하지 않습니다. 다른 사용자가 삭제했을 수 있습니다.")
    return subnet


def find_overlap(conn: sqlite3.Connection, start: int, end: int) -> dict[str, Any] | None:
    """[start, end]와 겹치는 기존 서브넷을 반환한다(서브넷은 서로 겹치지 않으므로 1회 조회로 충분)."""
    row = conn.execute("SELECT * FROM subnets WHERE network_int <= ? ORDER BY network_int DESC LIMIT 1",
                       (end,)).fetchone()
    if row and row["broadcast_int"] >= start:
        return dict(row)
    return None


def subnet_containing(conn: sqlite3.Connection, ip_int: int) -> dict[str, Any] | None:
    """IP를 포함하는 서브넷."""
    return find_overlap(conn, ip_int, ip_int)


def _scope(group_ids: list[int], subnet_ids: list[int]) -> tuple[str, str, list[int]]:
    """선택 범위(그룹들의 산하 전체 + 개별 대역들)를 SQL로 만든다. (CTE, WHERE, 파라미터)"""
    g_marks = ",".join("?" * len(group_ids)) or "NULL"
    s_marks = ",".join("?" * len(subnet_ids)) or "NULL"
    cte = f"""
        WITH RECURSIVE d(id) AS (
            SELECT id FROM groups WHERE id IN ({g_marks})
            UNION
            SELECT g.id FROM groups g JOIN d ON g.parent_id = d.id
        )
    """
    where = f"(s.group_id IN (SELECT id FROM d) OR s.id IN ({s_marks}))"
    return cte, where, [*group_ids, *subnet_ids]


def scope_page(conn: sqlite3.Connection, group_ids: list[int], subnet_ids: list[int],
               offset: int = 0, limit: int = MAX_SUBNET_DISPLAY) -> dict[str, Any]:
    """선택 범위의 통계 + 대역 목록 한 페이지(offset부터 limit개, IP 순)."""
    cte, where, params = _scope(group_ids, subnet_ids)
    count, total, used = conn.execute(
        cte + f"SELECT COUNT(*), COALESCE(SUM(total_count), 0), COALESCE(SUM(used_count), 0) "
              f"FROM subnets s WHERE {where}", params).fetchone()
    rows = conn.execute(cte + f"""
        SELECT s.id, s.group_id, s.cidr, s.prefix, s.network_int, s.description, s.total_count, s.used_count
          FROM subnets s WHERE {where} ORDER BY s.network_int LIMIT ? OFFSET ?
    """, [*params, limit, offset]).fetchall()
    return {"stats": {"subnet_count": count, "total": total, "used": used},
            "subnets": [dict(r) for r in rows], "offset": offset, "limit": limit}


def group_dashboard(conn: sqlite3.Connection, group_id: int, offset: int = 0,
                    limit: int = MAX_SUBNET_DISPLAY) -> dict[str, Any]:
    """그룹 선택 시 대시보드 데이터: 경로, 산하 통계, 하위 대역 한 페이지."""
    group = group_model.require_group(conn, group_id)
    page = scope_page(conn, [group_id], [], offset, limit)
    page["stats"]["child_groups"] = conn.execute(
        "SELECT COUNT(*) FROM groups WHERE parent_id = ?", (group_id,)).fetchone()[0]
    return {"group": group, "path": group_model.group_path(conn, group_id), **page}


def list_scope_subnets(conn: sqlite3.Connection, group_ids: list[int],
                       subnet_ids: list[int]) -> list[dict[str, Any]]:
    """선택 범위의 전체 서브넷(제한 없음, Export용)."""
    cte, where, params = _scope(group_ids, subnet_ids)
    rows = conn.execute(cte + f"""
        SELECT s.id, s.group_id, s.cidr, s.description, s.total_count, s.used_count
          FROM subnets s WHERE {where} ORDER BY s.network_int
    """, params).fetchall()
    return [dict(r) for r in rows]


def list_descendant_subnets(conn: sqlite3.Connection, group_id: int) -> list[dict[str, Any]]:
    """그룹 산하 전체 서브넷(제한 없음, Export용)."""
    return list_scope_subnets(conn, [group_id], [])


# ── 쓰기 ───────────────────────────────────────────────────
def _clean_desc(text: str) -> str:
    text = (text or "").strip()
    if len(text) > MAX_TEXT_LEN:
        raise ValidationError(f"대역 설명은 {MAX_TEXT_LEN}자 이하로 입력하세요.")
    return text


def _insert_subnet(conn: sqlite3.Connection, group_id: int, info: SubnetInfo, description: str,
                   populate: bool, operator: str, ts: str) -> tuple[int, int]:
    sid = conn.execute("""
        INSERT INTO subnets(group_id, cidr, network_int, broadcast_int, prefix, description,
                            created_at, updated_at, updated_by, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (group_id, info.cidr, info.network_int, info.broadcast_int, info.prefix, description,
          ts, ts, operator, group_model.next_sort_order(conn, group_id))).lastrowid
    seal_subnets(conn, [sid])
    count = bulk_insert_ips(conn, sid, info.host_first, info.host_last) if populate else 0
    detail = (f"IP {int_to_ip(info.host_first)} ~ {int_to_ip(info.host_last)} ({count:,}개) 미사용 일괄 생성"
              if populate else "IP 미생성")
    insert_entries(conn, [make_entry(
        subnet_id=sid, ip_address=info.cidr, range_start=info.network_int, range_end=info.broadcast_int,
        action=ACT_SUBNET_CREATE, new_status="미사용" if populate else "", operator=operator, timestamp=ts,
        diff_summary=f"대역 생성: {info.cidr} — {detail}")])
    return int(sid), count


def create_subnet(conn: sqlite3.Connection, group_id: int | None, cidr: str, description: str,
                  populate: bool, operator: str) -> dict[str, Any]:
    """서브넷 1개 생성(+호스트 IP 미사용 상태 Bulk Insert). group_id=None이면 그룹 없는 최상위 대역."""
    try:
        info = parse_cidr(cidr)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    description = _clean_desc(description)
    with transaction(conn):
        if group_id is not None:
            group_model.require_group(conn, group_id)
        overlap = find_overlap(conn, info.network_int, info.broadcast_int)
        if overlap:
            raise ValidationError(f"{info.cidr} 대역이 기존 대역 {overlap['cidr']}와(과) 겹칩니다.")
        sid, count = _insert_subnet(conn, group_id, info, description, populate, operator, now_str())
        bump_revision(conn, tree=True)
    return {"id": sid, "cidr": info.cidr, "ip_count": count, "normalized": info.normalized}


def create_split_subnets(conn: sqlite3.Connection, group_id: int | None, cidr: str, new_prefix: int,
                         description: str, populate: bool, operator: str) -> dict[str, Any]:
    """큰 대역을 /new_prefix 단위로 분할해 여러 서브넷을 한 번에 생성한다(전체 성공/전체 실패)."""
    try:
        infos = split_cidr(cidr, new_prefix, MAX_SPLIT_SUBNETS)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    description = _clean_desc(description)
    with transaction(conn):
        if group_id is not None:
            group_model.require_group(conn, group_id)
        first, last = infos[0].network_int, infos[-1].broadcast_int
        conflicts = [r["cidr"] for r in conn.execute(
            "SELECT cidr FROM subnets WHERE network_int BETWEEN ? AND ? ORDER BY network_int LIMIT 5",
            (first, last))]
        container = find_overlap(conn, first, first)
        if container and container["cidr"] not in conflicts:
            conflicts.insert(0, container["cidr"])
        if conflicts:
            raise ValidationError("다음 기존 대역과 겹쳐 분할 생성할 수 없습니다:\n" + "\n".join(conflicts[:5]))
        ts = now_str()
        total = 0
        for info in infos:
            total += _insert_subnet(conn, group_id, info, description, populate, operator, ts)[1]
        bump_revision(conn, tree=True)
    return {"count": len(infos), "ip_count": total}


def update_subnet(conn: sqlite3.Connection, subnet_id: int, description: str, operator: str) -> None:
    """대역 설명 변경(CIDR은 변경 불가)."""
    description = _clean_desc(description)
    with transaction(conn):
        subnet = require_subnet(conn, subnet_id)
        if subnet["description"] == description:
            return
        ts = now_str()
        conn.execute("UPDATE subnets SET description = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                     (description, ts, operator, subnet_id))
        seal_subnets(conn, [subnet_id])
        insert_entries(conn, [make_entry(
            subnet_id=subnet_id, ip_address=subnet["cidr"], action=ACT_SUBNET_UPDATE, operator=operator,
            timestamp=ts, diff_summary=f"대역 설명: '{subnet['description']}' → '{description}'")])
        bump_revision(conn, tree=True)


def move_subnet(conn: sqlite3.Connection, subnet_id: int, new_group_id: int | None, operator: str) -> None:
    """서브넷을 다른 그룹(None이면 그룹 밖 최상위)으로 이동한다."""
    with transaction(conn):
        subnet = require_subnet(conn, subnet_id)
        if new_group_id is not None:
            group_model.require_group(conn, new_group_id)
        if subnet["group_id"] == new_group_id:
            return
        old_path = group_model.group_path(conn, subnet["group_id"])
        new_path = group_model.group_path(conn, new_group_id)
        ts = now_str()
        conn.execute("UPDATE subnets SET group_id = ?, updated_at = ?, updated_by = ?, sort_order = ? WHERE id = ?",
                     (new_group_id, ts, operator, group_model.next_sort_order(conn, new_group_id), subnet_id))
        seal_subnets(conn, [subnet_id])
        insert_entries(conn, [make_entry(
            subnet_id=subnet_id, ip_address=subnet["cidr"], action=ACT_SUBNET_MOVE, operator=operator,
            timestamp=ts, diff_summary=f"대역 이동: {old_path} → {new_path}")])
        bump_revision(conn, tree=True)


def delete_subnet(conn: sqlite3.Connection, subnet_id: int, operator: str) -> dict[str, int]:
    """서브넷과 IP 전체 삭제. '사용중' IP가 있으면 차단."""
    with transaction(conn):
        subnet = require_subnet(conn, subnet_id)
        if subnet["used_count"] > 0:
            raise ValidationError(f"{subnet['cidr']} 대역에 '사용중' IP가 {subnet['used_count']:,}개 있어 "
                                  "삭제할 수 없습니다.\n먼저 해당 IP를 회수(미사용)하세요.")
        ts = now_str()
        insert_entries(conn, [make_entry(
            subnet_id=subnet_id, ip_address=subnet["cidr"], range_start=subnet["network_int"],
            range_end=subnet["broadcast_int"], action=ACT_SUBNET_DELETE, operator=operator, timestamp=ts,
            diff_summary=f"대역 삭제: {subnet['cidr']} (IP {subnet['total_count']:,}개 제거, "
                         f"그룹: {group_model.group_path(conn, subnet['group_id'])})")])
        conn.execute("DELETE FROM subnets WHERE id = ?", (subnet_id,))
        delete_ip_ranges(conn, [(subnet["network_int"], subnet["broadcast_int"])])
        bump_revision(conn, tree=True)
    return {"ips": subnet["total_count"]}


# ── 트리 다중 선택 일괄 처리 ───────────────────────────────
Node = tuple[str, int]      # ("group" | "subnet", id)


def _node_label(conn: sqlite3.Connection, kind: str, node_id: int) -> str:
    if kind == "group":
        g = group_model.get_group(conn, node_id)
        return f"그룹 '{g['name']}'" if g else "그룹"
    s = get_subnet(conn, node_id)
    return f"대역 {s['cidr']}" if s else "대역"


def move_nodes(conn: sqlite3.Connection, nodes: list[Node], new_parent_id: int | None, operator: str) -> int:
    """여러 그룹/대역을 한 그룹(None이면 최상위)으로 이동. 하나라도 불가하면 전체 취소."""
    moved = 0
    with transaction(conn):
        for kind, node_id in nodes:
            try:
                if kind == "group":
                    group_model.move_group(conn, node_id, new_parent_id, operator)
                else:
                    move_subnet(conn, node_id, new_parent_id, operator)
            except ValidationError as exc:
                raise ValidationError(f"{_node_label(conn, kind, node_id)}: {exc}") from exc
            moved += 1
    return moved


def place_nodes(conn: sqlite3.Connection, nodes: list[Node], new_parent_id: int | None,
                ref: Node | None, after: bool, operator: str) -> int:
    """트리 Drag & Drop: nodes를 new_parent_id 아래 ref 항목의 앞(또는 뒤)에 놓는다.

    - 부모가 다른 항목은 먼저 이동(순환 참조 차단·이력 기록은 move_* 가 담당)
    - 그 부모의 자식 순서를 1..n 으로 다시 매긴다(다른 사용자가 방금 추가한 항목도 빠짐없이 포함)
    - 순서(sort_order)는 위변조 해시 대상이 아니므로 순서만 바꾸면 이력/해시는 그대로다
    """
    with transaction(conn):
        moved = move_nodes(conn, [n for n in nodes if _parent_of(conn, n) != new_parent_id], new_parent_id,
                           operator) if nodes else 0
        children = group_model.ordered_children(conn, new_parent_id)
        rest = [c for c in children if c not in nodes]
        if ref is not None and ref in rest:
            index = rest.index(ref) + (1 if after else 0)
        else:
            index = len(rest)
        ordered = rest[:index] + [n for n in nodes if n in children] + rest[index:]
        conn.executemany("UPDATE groups SET sort_order = ? WHERE id = ?",
                         [(i + 1, nid) for i, (k, nid) in enumerate(ordered) if k == "group"])
        conn.executemany("UPDATE subnets SET sort_order = ? WHERE id = ?",
                         [(i + 1, nid) for i, (k, nid) in enumerate(ordered) if k == "subnet"])
        bump_revision(conn, tree=True)
    return moved


def _parent_of(conn: sqlite3.Connection, node: Node) -> int | None:
    kind, nid = node
    if kind == "group":
        row = conn.execute("SELECT parent_id FROM groups WHERE id = ?", (nid,)).fetchone()
    else:
        row = conn.execute("SELECT group_id FROM subnets WHERE id = ?", (nid,)).fetchone()
    if row is None:
        raise NotFoundError("이동할 항목이 존재하지 않습니다. 다른 사용자가 삭제했을 수 있습니다.")
    return row[0]


def delete_nodes(conn: sqlite3.Connection, nodes: list[Node], operator: str) -> dict[str, int]:
    """여러 그룹/대역 일괄 삭제. 하나라도 '사용중' IP가 있으면 전체 취소."""
    totals = {"groups": 0, "subnets": 0, "ips": 0}
    with transaction(conn):
        subnets = [n for k, n in nodes if k == "subnet"]
        groups = [n for k, n in nodes if k == "group"]
        for sid in subnets:
            if get_subnet(conn, sid) is None:
                continue
            try:
                totals["ips"] += delete_subnet(conn, sid, operator)["ips"]
            except ValidationError as exc:
                raise ValidationError(f"{exc}\n\n삭제를 모두 취소했습니다.") from exc
            totals["subnets"] += 1
        # 안쪽 그룹부터 삭제해 이미 지워진 하위 그룹을 다시 지우지 않게 한다
        depth = {gid: len(group_model.ancestor_chain(conn, gid)) for gid in groups}
        for gid in sorted(groups, key=lambda g: -depth[g]):
            if group_model.get_group(conn, gid) is None:
                continue
            try:
                res = group_model.delete_group(conn, gid, operator)
            except ValidationError as exc:
                raise ValidationError(f"{_node_label(conn, 'group', gid)}: {exc}\n\n삭제를 모두 취소했습니다.") from exc
            totals["groups"] += res["groups"]
            totals["subnets"] += res["subnets"]
            totals["ips"] += res["ips"]
    return totals
