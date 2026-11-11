"""DB 스키마 정의 및 초기화.

설계 포인트(경량/고속):
- `ips.id` = IPv4 정수값(INTEGER PRIMARY KEY). 별도 인덱스 없이 rowid B-tree가
  IP 순 정렬 + 대역 범위 조회(`id BETWEEN 네트워크 AND 브로드캐스트`)를 모두 처리한다.
- 서브넷 사용 통계(total/used)는 트리거로 비정규화해 1,600만 IP에서도 집계가 O(서브넷 수).
- 검색용 인덱스는 값이 있는 행만 담는 부분 인덱스(빈 문자열 제외)라 DB가 거의 커지지 않는다.
- `meta.data_rev / tree_rev`는 매 쓰기 트랜잭션마다 증가 → 타 사용자 변경을 1행 조회로 감지.
"""
from __future__ import annotations

import sqlite3

from ipmgr.constants import MIN_SUBNET_PREFIX, SCHEMA_VERSION, STATUS_USED
from ipmgr.db.connection import transaction

# group_id가 NULL이면 그룹에 속하지 않은 최상위 대역
SUBNETS_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    id            INTEGER PRIMARY KEY,
    group_id      INTEGER REFERENCES groups(id),
    cidr          TEXT NOT NULL,
    network_int   INTEGER NOT NULL UNIQUE,
    broadcast_int INTEGER NOT NULL,
    prefix        INTEGER NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    total_count   INTEGER NOT NULL DEFAULT 0,
    used_count    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT '',
    updated_by    TEXT NOT NULL DEFAULT ''
);
"""

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY,
    parent_id   INTEGER REFERENCES groups(id),
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT '',
    updated_by  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_groups_parent ON groups(parent_id);

{SUBNETS_DDL}
CREATE INDEX IF NOT EXISTS idx_subnets_group ON subnets(group_id, network_int);

CREATE TABLE IF NOT EXISTS ips (
    id             INTEGER PRIMARY KEY,
    subnet_id      INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT '미사용' CHECK (status IN ('미사용', '사용중')),
    hostname       TEXT NOT NULL DEFAULT '',
    allocated_date TEXT NOT NULL DEFAULT '',
    assignee       TEXT NOT NULL DEFAULT '',
    request_no     TEXT NOT NULL DEFAULT '',
    note           TEXT NOT NULL DEFAULT '',
    version        INTEGER NOT NULL DEFAULT 1,
    updated_at     TEXT NOT NULL DEFAULT '',
    updated_by     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ips_hostname   ON ips(hostname)   WHERE hostname > '';
CREATE INDEX IF NOT EXISTS idx_ips_assignee   ON ips(assignee)   WHERE assignee > '';
CREATE INDEX IF NOT EXISTS idx_ips_request_no ON ips(request_no) WHERE request_no > '';
CREATE INDEX IF NOT EXISTS idx_ips_note       ON ips(note)       WHERE note > '';

CREATE TABLE IF NOT EXISTS history_logs (
    id             INTEGER PRIMARY KEY,
    subnet_id      INTEGER,
    ip_id          INTEGER,
    ip_address     TEXT NOT NULL DEFAULT '',
    range_start    INTEGER,
    range_end      INTEGER,
    action         TEXT NOT NULL,
    old_status     TEXT NOT NULL DEFAULT '',
    new_status     TEXT NOT NULL DEFAULT '',
    hostname       TEXT NOT NULL DEFAULT '',
    assignee       TEXT NOT NULL DEFAULT '',
    request_no     TEXT NOT NULL DEFAULT '',
    note           TEXT NOT NULL DEFAULT '',
    allocated_date TEXT NOT NULL DEFAULT '',
    diff_summary   TEXT NOT NULL DEFAULT '',
    operator       TEXT NOT NULL DEFAULT '',
    timestamp      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hist_ip     ON history_logs(ip_id) WHERE ip_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_hist_subnet ON history_logs(subnet_id);
CREATE INDEX IF NOT EXISTS idx_hist_range  ON history_logs(range_start) WHERE ip_id IS NULL;

-- 프로그램 밖(DB Browser 등)에서 바뀐 행의 위치. 트리거가 기록하고 빠른 검사가 그 행만 해시로 검증한다
CREATE TABLE IF NOT EXISTS dirty (
    kind INTEGER NOT NULL,
    ref  INTEGER NOT NULL,
    PRIMARY KEY (kind, ref)
) WITHOUT ROWID;
""".replace("{SUBNETS_DDL}", SUBNETS_DDL.replace("{table}", "subnets"))

# dirty.kind
DIRTY_IP, DIRTY_SUBNET, DIRTY_GROUP, DIRTY_HISTORY = 0, 1, 2, 3
# 프로그램의 쓰기 트랜잭션은 meta.app_write = 1 로 표시하고 커밋 직전 0으로 되돌린다(다른 연결에는 항상 0).
# 그 밖의 쓰기만 dirty에 남는다.
_EXTERNAL = "WHEN (SELECT value FROM meta WHERE key = 'app_write') IS NOT 1"
_WATCH = (("ips", DIRTY_IP), ("subnets", DIRTY_SUBNET), ("groups", DIRTY_GROUP), ("history_logs", DIRTY_HISTORY))
WATCH_TRIGGERS_SQL = "".join(
    f"CREATE TRIGGER IF NOT EXISTS trg_w_{table}_{op[:3].lower()} AFTER {op} ON {table} {_EXTERNAL} BEGIN\n"
    f"    INSERT OR IGNORE INTO dirty(kind, ref) VALUES ({kind}, {'OLD' if op == 'DELETE' else 'NEW'}.id);\nEND;\n"
    for table, kind in _WATCH for op in ("INSERT", "UPDATE", "DELETE"))
WATCH_TRIGGER_NAMES = tuple(f"trg_w_{t}_{op}" for t, _ in _WATCH for op in ("ins", "upd", "del"))

TRIGGERS_SQL = f"""
CREATE TRIGGER IF NOT EXISTS trg_ips_insert AFTER INSERT ON ips BEGIN
    UPDATE subnets SET total_count = total_count + 1,
                       used_count  = used_count + (NEW.status = '{STATUS_USED}')
     WHERE id = NEW.subnet_id;
END;
CREATE TRIGGER IF NOT EXISTS trg_ips_delete AFTER DELETE ON ips BEGIN
    UPDATE subnets SET total_count = total_count - 1,
                       used_count  = used_count - (OLD.status = '{STATUS_USED}')
     WHERE id = OLD.subnet_id;
END;
CREATE TRIGGER IF NOT EXISTS trg_ips_status AFTER UPDATE OF status ON ips
WHEN OLD.status <> NEW.status BEGIN
    UPDATE subnets SET used_count = used_count + (NEW.status = '{STATUS_USED}')
                                              - (OLD.status = '{STATUS_USED}')
     WHERE id = NEW.subnet_id;
END;
"""

TRIGGER_NAMES = ("trg_ips_insert", "trg_ips_delete", "trg_ips_status") + WATCH_TRIGGER_NAMES

# 이력 범위 이벤트 검색 시 하한(서브넷 최대 크기)
RANGE_LOOKBACK = 2 ** (32 - MIN_SUBNET_PREFIX)


class SchemaTooNewError(Exception):
    """DB가 이 프로그램보다 새 버전 프로그램으로 이미 변환됨 → 구버전 프로그램은 DB를 건드리지 않는다."""

    def __init__(self, db_version: int) -> None:
        super().__init__(f"이 DB는 더 새로운 버전의 IP관리 프로그램으로 업데이트되었습니다 "
                         f"(DB 구조 v{db_version}, 이 프로그램 v{SCHEMA_VERSION}).\n\n"
                         "공유 폴더의 최신 IP관리.exe로 다시 실행해 주세요.\n"
                         "(구버전으로 계속 사용하면 새 구조를 '변조'로 잘못 판단할 수 있어 차단합니다)")
        self.db_version = db_version


def ensure_not_newer(conn: sqlite3.Connection) -> None:
    """DB 구조 버전이 이 프로그램보다 높으면 SchemaTooNewError(연결 시·무결성 검사 시마다 확인)."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return                                      # 아직 스키마가 없는 새 DB
    if row is not None and row[0] > SCHEMA_VERSION:
        raise SchemaTooNewError(row[0])


def init_schema(conn: sqlite3.Connection) -> None:
    """테이블/인덱스/트리거를 생성하고 meta 기본값을 채운다(멱등).

    프로그램을 수정·재빌드해도 DB는 그대로 쓴다. 구조가 바뀐 버전(SCHEMA_VERSION 증가)이 처음 열 때만
    컬럼 추가·트리거 교체 같은 변환을 1회 수행하고, 더 새 버전이 변환한 DB는 열지 않는다.
    """
    ensure_not_newer(conn)
    with transaction(conn):
        ensure_not_newer(conn)                      # 잠금을 잡은 뒤 한 번 더(동시 실행 대비)
        fresh = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'meta'").fetchone() is None
        for statement in _split(TABLES_SQL):
            conn.execute(statement)
        conn.executemany("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                         [("schema_version", SCHEMA_VERSION), ("data_rev", 0), ("tree_rev", 0), ("app_write", 0),
                          ("baseline", 1 if fresh else 0)])
        conn.execute("UPDATE meta SET value = 1 WHERE key = 'app_write'")     # 이 트랜잭션의 쓰기는 프로그램 작업
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        _migrate_nullable_group(conn)
        _ensure_hash_columns(conn)
        if not fresh and version < SCHEMA_VERSION:
            # 구조가 바뀐 새 버전이 처음 열 때: 예전 버전의 트리거를 이 버전의 정의로 교체
            # (IF NOT EXISTS만으로는 예전 정의가 남아 '감시 장치 훼손'으로 오탐된다)
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
                conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        create_triggers(conn)
        if version < 3 and not fresh:
            # v2 이하 → 기존 데이터 전체에 해시 체인/행 해시를 최초 1회 부여
            from ipmgr.models.integrity_model import seal_all
            seal_all(conn)
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version' AND value < ?",
                     (SCHEMA_VERSION, SCHEMA_VERSION))


EXTRA_COLUMNS = (("history_logs", "chain_hash", "BLOB"), ("ips", "row_hash", "BLOB"),
                 ("subnets", "row_hash", "BLOB"), ("groups", "row_hash", "BLOB"),
                 # 트리 표시 순서(드래그로 변경). 위변조 해시 대상이 아님
                 ("groups", "sort_order", "INTEGER NOT NULL DEFAULT 0"),
                 ("subnets", "sort_order", "INTEGER NOT NULL DEFAULT 0"))


def _ensure_hash_columns(conn: sqlite3.Connection) -> None:
    """위변조 감지용 해시 컬럼과 트리 순서 컬럼을 추가한다(없을 때만, 기존 DB 자동 확장)."""
    for table, column, decl in EXTRA_COLUMNS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate_nullable_group(conn: sqlite3.Connection) -> None:
    """v1 DB(subnets.group_id NOT NULL)를 그룹 없는 대역을 허용하는 구조로 변환한다."""
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(subnets)")}
    if not cols or not cols["group_id"][3]:      # [3] = notnull
        return
    drop_triggers(conn)
    conn.execute(SUBNETS_DDL.replace("{table}", "subnets_v2"))
    conn.execute("INSERT INTO subnets_v2 SELECT id, group_id, cidr, network_int, broadcast_int, prefix, "
                 "description, total_count, used_count, created_at, updated_at, updated_by FROM subnets")
    conn.execute("DROP TABLE subnets")
    conn.execute("ALTER TABLE subnets_v2 RENAME TO subnets")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subnets_group ON subnets(group_id, network_int)")
    conn.execute("UPDATE meta SET value = value + 1 WHERE key IN ('data_rev', 'tree_rev')")


def create_triggers(conn: sqlite3.Connection) -> None:
    """서브넷 통계 유지 트리거와 외부 수정 감시 트리거 생성."""
    for statement in (TRIGGERS_SQL + WATCH_TRIGGERS_SQL).split("END;"):
        if statement.strip():
            conn.execute(statement + "END;")


def drop_triggers(conn: sqlite3.Connection) -> None:
    """대량 시드 시 트리거를 임시 제거한다(이후 recalc_counters + create_triggers 필수)."""
    for name in TRIGGER_NAMES:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def recalc_counters(conn: sqlite3.Connection) -> None:
    """모든 서브넷의 total/used 카운터를 실제 IP 행으로부터 재계산한다."""
    with transaction(conn):
        conn.execute(f"""
            UPDATE subnets SET
              total_count = (SELECT COUNT(*) FROM ips
                              WHERE ips.id BETWEEN subnets.network_int AND subnets.broadcast_int),
              used_count  = (SELECT COUNT(*) FROM ips
                              WHERE ips.id BETWEEN subnets.network_int AND subnets.broadcast_int
                                AND ips.status = '{STATUS_USED}')
        """)


def _split(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]
