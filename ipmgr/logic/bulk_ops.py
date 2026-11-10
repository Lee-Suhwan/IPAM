"""대량 CUD 헬퍼 (`executemany` 기반 Bulk Insert / Bulk Delete)."""
from __future__ import annotations

import sqlite3
from typing import Iterable, Iterator, Sequence

from ipmgr.constants import STATUS_UNUSED

CHUNK = 500  # SQLite 변수 한도(32766) 대비 안전한 IN 절 크기


def iter_ip_rows(subnet_id: int, first: int, last: int, status: str = STATUS_UNUSED,
                 allocated_date: str = "") -> Iterator[tuple[int, int, str, str]]:
    """일괄 생성할 IP 행을 지연 생성한다(메모리 절약)."""
    for ip in range(first, last + 1):
        yield (ip, subnet_id, status, allocated_date)


def bulk_insert_ips(conn: sqlite3.Connection, subnet_id: int, first: int, last: int,
                    status: str = STATUS_UNUSED, allocated_date: str = "") -> int:
    """IP 범위를 한 번에 생성한다. 이미 있는 IP는 건너뛰며 실제 생성 수를 반환한다."""
    # total_changes에는 트리거의 subnets UPDATE도 포함되므로 범위 내 행 수 차이로 계산한다.
    count_sql = "SELECT COUNT(*) FROM ips WHERE id BETWEEN ? AND ?"
    before = conn.execute(count_sql, (first, last)).fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO ips(id, subnet_id, status, allocated_date) VALUES (?, ?, ?, ?)",
        iter_ip_rows(subnet_id, first, last, status, allocated_date))
    return conn.execute(count_sql, (first, last)).fetchone()[0] - before


def delete_ip_ranges(conn: sqlite3.Connection, ranges: Iterable[tuple[int, int]]) -> None:
    """(시작, 끝) 범위 목록의 IP를 일괄 삭제한다(서브넷/그룹 삭제용)."""
    conn.executemany("DELETE FROM ips WHERE id BETWEEN ? AND ?", list(ranges))


def chunked(values: Sequence[int], size: int = CHUNK) -> Iterator[Sequence[int]]:
    """IN 절용 청크 분할."""
    for i in range(0, len(values), size):
        yield values[i:i + size]
