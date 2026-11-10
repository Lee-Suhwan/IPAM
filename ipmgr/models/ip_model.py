"""개별 IP 수정, 다중 IP 일괄 등록(할당)/회수, 낙관적 잠금(version) 충돌 감지, 통합 검색."""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Iterable, Mapping

from ipmgr.constants import (ACT_ALLOCATE, ACT_REALLOCATE, ACT_RECLAIM, HISTORY_PAGE, MAX_TEXT_LEN,
                            SEARCH_LIMIT, STATUS_UNUSED, STATUS_USED)
from ipmgr.db.connection import transaction
from ipmgr.logic import state_machine as sm
from ipmgr.logic.bulk_ops import chunked
from ipmgr.logic.integrity import seal_ips, tampered_ips
from ipmgr.logic.subnet_calc import int_to_ip, ip_prefix_range
from ipmgr.models import group_model, subnet_model
from ipmgr.models.common import ConflictError, ValidationError, bump_revision, now_str
from ipmgr.models.history_model import history_page, insert_entries, ip_timeline, snapshot_entry

IP_COLUMNS = ("id, subnet_id, status, hostname, allocated_date, assignee, request_no, note, "
              "version, updated_at, updated_by")


def _with_address(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["ip_address"] = int_to_ip(data["id"])
    return data


# ── 조회 ───────────────────────────────────────────────────
def get_ip(conn: sqlite3.Connection, ip_id: int) -> dict[str, Any] | None:
    """IP 1건 최신 상태(Pre-edit / Pre-save 재조회용)."""
    row = conn.execute(f"SELECT {IP_COLUMNS} FROM ips WHERE id = ?", (ip_id,)).fetchone()
    return _with_address(row) if row else None


def list_ips(conn: sqlite3.Connection, subnet: Mapping[str, Any]) -> list[dict[str, Any]]:
    """대역 내 전체 IP(IP 순). PK 범위 스캔이라 1,600만 행 DB에서도 즉시 반환된다."""
    rows = conn.execute(f"SELECT {IP_COLUMNS} FROM ips WHERE id BETWEEN ? AND ? ORDER BY id",
                        (subnet["network_int"], subnet["broadcast_int"])).fetchall()
    return [_with_address(r) for r in rows]


def subnet_detail(conn: sqlite3.Connection, subnet_id: int) -> dict[str, Any]:
    """서브넷 뷰 데이터: 서브넷 정보, 그룹 경로, IP 목록(한 번의 읽기 트랜잭션)."""
    with transaction(conn, immediate=False):
        subnet = subnet_model.require_subnet(conn, subnet_id)
        return {"subnet": subnet, "path": group_model.group_path(conn, subnet["group_id"]),
                "ips": list_ips(conn, subnet)}


def history_for(conn: sqlite3.Connection, subnet_id: int, ip_ids: list[int] | None, newest_first: bool = True,
                limit: int = HISTORY_PAGE) -> dict[str, Any]:
    """이력 패널: 선택 IP들(없으면 대역 전체)의 이력 한 페이지."""
    subnet = subnet_model.require_subnet(conn, subnet_id)
    return history_page(conn, subnet, ip_ids, newest_first, limit)


def ip_detail(conn: sqlite3.Connection, ip_id: int, newest_first: bool = True) -> dict[str, Any]:
    """수정 폼 로드 시 DB 최신 레코드 + 전체 생애주기 타임라인."""
    with transaction(conn, immediate=False):
        return {"ip": get_ip(conn, ip_id), "timeline": ip_timeline(conn, ip_id, newest_first)}


# ── 수정(낙관적 잠금) ──────────────────────────────────────
def update_ip(conn: sqlite3.Connection, ip_id: int, expected_version: int, values: Mapping[str, str],
              operator: str, today: date | None = None) -> dict[str, Any]:
    """IP 상태/속성을 저장한다.

    1) 저장 직전 DB 최신 레코드를 재조회해 폼을 연 시점의 version과 비교(다르면 ConflictError)
    2) `UPDATE ... WHERE id = ? AND version = ?`로 한 번 더 원자적으로 보장
    3) 변경일(할당·회수 일자) 자동 기록, 이력 스냅샷(해시 체인) 기록, 행 해시 갱신, 변경 리비전 증가
    """
    status = sm.validate_status(values.get("status", ""))
    fields = _normalize(values)
    with transaction(conn):
        current = get_ip(conn, ip_id)
        if current is None:
            raise ConflictError("다른 사용자가 이 IP를 삭제했습니다.", deleted=True)
        if current["version"] != expected_version:
            raise ConflictError("다른 사용자가 먼저 수정했습니다.", latest=current)
        _guard_tampered(conn, [ip_id])
        allocated =sm.resolve_changed_date(current["status"], status, current["allocated_date"], today)
        new = {"status": status, "allocated_date": allocated, **fields}
        diff = sm.build_diff_summary(current, new)
        if not diff:
            return {"changed": False, "ip": current}
        allocated_before = False
        if status == STATUS_USED and current["status"] != STATUS_USED:
            allocated_before = conn.execute(
                "SELECT 1 FROM history_logs WHERE ip_id = ? AND action IN (?, ?) LIMIT 1",
                (ip_id, ACT_ALLOCATE, ACT_REALLOCATE)).fetchone() is not None
        action = sm.determine_action(current["status"], status, allocated_before)
        ts = now_str()
        cur = conn.execute("""
            UPDATE ips SET status = ?, hostname = ?, allocated_date = ?, assignee = ?, request_no = ?,
                           note = ?, version = version + 1, updated_at = ?, updated_by = ?
             WHERE id = ? AND version = ?
        """, (status, fields["hostname"], allocated, fields["assignee"], fields["request_no"],
              fields["note"], ts, operator, ip_id, expected_version))
        if cur.rowcount != 1:
            raise ConflictError("다른 사용자가 먼저 수정했습니다.", latest=get_ip(conn, ip_id))
        seal_ips(conn, [ip_id])
        updated = get_ip(conn, ip_id)
        insert_entries(conn, [snapshot_entry(
            updated, action=action, old_status=current["status"], new_status=status,
            ip_address=updated["ip_address"], diff_summary=diff, operator=operator, timestamp=ts)])
        bump_revision(conn)
    return {"changed": True, "ip": updated, "action": action}


# ── 다중 IP 일괄 등록(할당) / 회수 ─────────────────────────
def get_ips(conn: sqlite3.Connection, ip_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """여러 IP의 DB 최신 레코드(대화상자 열기 전 최신성 검증용)."""
    ids = sorted({int(i) for i in ip_ids})
    rows: dict[int, dict[str, Any]] = {}
    for chunk in chunked(ids):
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(f"SELECT {IP_COLUMNS} FROM ips WHERE id IN ({marks})", tuple(chunk)):
            rows[r["id"]] = _with_address(r)
    return rows


def _check_targets(rows: Mapping[int, dict[str, Any]], versions: Mapping[int, int],
                   required: str) -> list[tuple[str, str]]:
    """상태/버전 재검증. (IP, 사유) 충돌 목록을 반환한다."""
    conflicts: list[tuple[str, str]] = []
    for ip_id, version in sorted(versions.items()):
        r = rows.get(ip_id)
        if r is None:
            conflicts.append((int_to_ip(ip_id), "IP가 존재하지 않습니다(대역이 삭제되었을 수 있음)"))
        elif r["status"] != required:
            conflicts.append((r["ip_address"], f"다른 사용자({r['updated_by'] or '-'})가 먼저 '{r['status']}'(으)로 "
                                               f"변경했습니다 (담당자: {r['assignee'] or '-'})"))
        elif r["version"] != version:
            conflicts.append((r["ip_address"], f"조회 이후 다른 사용자({r['updated_by'] or '-'})가 수정했습니다"))
    return conflicts


def _normalize(values: Mapping[str, str]) -> dict[str, str]:
    """입력값 정리·길이 검증(초과 시 사용자에게 보여줄 ValidationError)."""
    try:
        return sm.normalize_fields(values)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _guard_tampered(conn: sqlite3.Connection, ip_ids: Iterable[int]) -> None:
    """프로그램 밖에서 변조된 IP는 복구 전까지 저장 금지(변조 흔적이 새 해시로 덮이지 않도록)."""
    bad = tampered_ips(conn, ip_ids)
    if bad:
        shown = ", ".join(int_to_ip(i) for i in bad[:5]) + (f" 외 {len(bad) - 5}개" if len(bad) > 5 else "")
        raise ValidationError(f"DB 변조가 감지된 IP입니다: {shown}\n"
                              "상태 표시줄의 '⚠ DB 변조 감지'를 눌러 복구한 뒤 다시 시도하세요.")


def _require(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValidationError(f"{label}은(는) 필수 입력 항목입니다.")
    if len(value) > MAX_TEXT_LEN:
        raise ValidationError(f"{label}은(는) 최대 {MAX_TEXT_LEN}자까지 입력할 수 있습니다.")
    return value


def _apply_bulk(conn: sqlite3.Connection, versions: Mapping[int, int], required: str,
                build: Any, operator: str, action_of: Any) -> dict[str, Any]:
    """일괄 상태 전이 공통 처리(전체 성공 또는 전체 취소)."""
    if not versions:
        raise ValidationError("처리할 IP가 없습니다.")
    with transaction(conn):
        rows = get_ips(conn, versions)
        conflicts = _check_targets(rows, versions, required)
        if conflicts:
            return {"applied": False, "count": 0, "conflicts": conflicts}
        _guard_tampered(conn, versions)
        ts = now_str()
        updates, entries = [], []
        for ip_id in sorted(versions):
            old = rows[ip_id]
            new = build(old)
            action = action_of(old)
            updates.append((new["status"], new["hostname"], new["allocated_date"], new["assignee"],
                            new["request_no"], new["note"], ts, operator, ip_id, versions[ip_id]))
            entries.append(snapshot_entry({**old, **new}, action=action, old_status=old["status"],
                                          new_status=new["status"], ip_address=old["ip_address"],
                                          diff_summary=sm.build_diff_summary(old, new), operator=operator,
                                          timestamp=ts))
        cur = conn.executemany("""
            UPDATE ips SET status = ?, hostname = ?, allocated_date = ?, assignee = ?, request_no = ?, note = ?,
                           version = version + 1, updated_at = ?, updated_by = ?
             WHERE id = ? AND version = ?
        """, updates)
        if cur.rowcount != len(updates):
            raise ConflictError("다른 사용자가 동시에 수정했습니다. 새로고침 후 다시 시도하세요.")
        seal_ips(conn, list(versions))
        insert_entries(conn, entries)
        bump_revision(conn)
    return {"applied": True, "count": len(updates), "conflicts": []}


def assign_ips(conn: sqlite3.Connection, versions: Mapping[int, int], hostnames: Mapping[int, str],
               assignee: str, request_no: str, note: str, operator: str,
               today: date | None = None) -> dict[str, Any]:
    """선택한 미사용 IP들을 한 번에 '사용중'으로 등록한다.

    담당자/요청번호/비고는 공통, 호스트네임은 IP별. 변경일은 오늘 날짜(할당일)로 자동 기록.
    한 IP라도 다른 사용자가 먼저 바꿨으면 전체를 취소하고 충돌 목록을 반환한다.
    """
    assignee = _require(assignee, "담당자")
    request_no = _require(request_no, "요청번호")
    common = _normalize({"assignee": assignee, "request_no": request_no, "note": note})
    hosts = {int(k): _normalize({"hostname": v})["hostname"] for k, v in hostnames.items()}
    allocated = sm.resolve_changed_date(STATUS_UNUSED, STATUS_USED, "", today)
    prior: set[int] = set()
    ids = sorted(versions)
    for chunk in chunked(ids):
        marks = ",".join("?" * len(chunk))
        prior.update(r[0] for r in conn.execute(
            f"SELECT DISTINCT ip_id FROM history_logs WHERE ip_id IN ({marks}) AND action IN (?, ?)",
            (*chunk, ACT_ALLOCATE, ACT_REALLOCATE)))

    def build(old: dict[str, Any]) -> dict[str, str]:
        return {"status": STATUS_USED, "hostname": hosts.get(old["id"], ""), "allocated_date": allocated,
                "assignee": common["assignee"], "request_no": common["request_no"], "note": common["note"]}

    return _apply_bulk(conn, versions, STATUS_UNUSED, build, operator,
                       lambda old: ACT_REALLOCATE if old["id"] in prior else ACT_ALLOCATE)


def reclaim_ips(conn: sqlite3.Connection, versions: Mapping[int, int], requester: str, request_no: str,
                reason: str, operator: str, today: date | None = None) -> dict[str, Any]:
    """선택한 사용중 IP들을 한 번에 '미사용'으로 회수한다.

    호스트네임은 초기화되고 변경일은 오늘(회수일)로 기록된다. 담당자/요청번호/비고는
    회수 요청자/회수 요청번호/회수 사유로 대체된다(이전 값은 이력의 변경 요약에 그대로 보존).
    """
    requester = _require(requester, "회수 요청자")
    request_no = _require(request_no, "회수 요청번호")
    common = _normalize({"assignee": requester, "request_no": request_no, "note": reason})
    reclaimed = sm.resolve_changed_date(STATUS_USED, STATUS_UNUSED, "", today)

    def build(_old: dict[str, Any]) -> dict[str, str]:
        return {"status": STATUS_UNUSED, "hostname": "", "allocated_date": reclaimed,
                "assignee": common["assignee"], "request_no": common["request_no"], "note": common["note"]}

    return _apply_bulk(conn, versions, STATUS_USED, build, operator, lambda _old: ACT_RECLAIM)


# ── 통합 검색 ──────────────────────────────────────────────
def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search(conn: sqlite3.Connection, text: str, limit: int = SEARCH_LIMIT) -> dict[str, Any]:
    """IP(접두어/CIDR) · 호스트네임 · 담당자 · 요청번호 · 비고 통합 검색.

    텍스트 컬럼은 '값이 있는 행'만 담은 부분 인덱스를 범위 스캔하므로 빈 IP 수천만 개는 읽지 않는다.
    """
    query = (text or "").strip()
    if not query:
        return {"rows": [], "truncated": False}
    found: dict[int, dict[str, Any]] = {}
    rng = ip_prefix_range(query)
    if rng:
        for r in conn.execute(f"SELECT {IP_COLUMNS} FROM ips WHERE id BETWEEN ? AND ? ORDER BY id LIMIT ?",
                              (rng[0], rng[1], limit + 1)):
            found.setdefault(r["id"], dict(r))
    pattern = f"%{_escape_like(query)}%"
    # IP 형식 검색어(예: 10.1.2, 10.1.0.0/16)는 PK 범위 조회만 수행 → 대용량에서도 즉시 응답
    for col in (() if rng else ("hostname", "assignee", "request_no", "note")):
        if len(found) > limit:
            break
        for r in conn.execute(f"SELECT {IP_COLUMNS} FROM ips WHERE {col} > '' AND {col} LIKE ? ESCAPE '\\' "
                              f"LIMIT ?", (pattern, limit + 1)):
            found.setdefault(r["id"], dict(r))
    truncated = len(found) > limit
    rows = sorted(found.values(), key=lambda r: r["id"])[:limit]
    subnet_ids = sorted({r["subnet_id"] for r in rows})
    subnets: dict[int, dict[str, Any]] = {}
    for chunk in chunked(subnet_ids):
        marks = ",".join("?" * len(chunk))
        for s in conn.execute(f"SELECT id, group_id, cidr FROM subnets WHERE id IN ({marks})", tuple(chunk)):
            subnets[s["id"]] = dict(s)
    paths = group_model.all_group_paths(conn) if rows else {}
    for r in rows:
        r["ip_address"] = int_to_ip(r["id"])
        s = subnets.get(r["subnet_id"], {})
        r["cidr"] = s.get("cidr", "")
        r["group_id"] = s.get("group_id")
        r["group_path"] = paths.get(s.get("group_id"), "")
    return {"rows": rows, "truncated": truncated}


def locate_ip(conn: sqlite3.Connection, ip_id: int) -> dict[str, Any] | None:
    """검색 결과 이동용: IP가 속한 서브넷과 조상 그룹 체인."""
    ip = get_ip(conn, ip_id)
    if ip is None:
        return None
    subnet = subnet_model.get_subnet(conn, ip["subnet_id"])
    if subnet is None:
        return None
    return {"ip_id": ip_id, "subnet_id": subnet["id"],
            "group_chain": group_model.group_chain_ids(conn, subnet["group_id"])}
