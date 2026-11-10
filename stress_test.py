"""IP관리 부하(스트레스) 테스트 도구.

10.0.0.0/8 을 /16 그룹 256개 × /24 대역 256개 = 65,536개 대역(호스트 IP 16,646,144개)으로 생성하고,
UI가 실제로 호출하는 모델 함수들의 응답 시간과 다중 프로세스 동시 수정 충돌 처리를 측정한다.

사용 예:
    python stress_test.py seed                      # data_stress/IP관리.db 생성 (전체 /8)
    python stress_test.py seed --groups 16          # 빠른 확인용: 10.0~10.15 만 생성
    python stress_test.py bench                     # 조회/수정/검색/Excel 응답 시간 측정
    python stress_test.py concurrency --procs 8 --seconds 30
    python stress_test.py all                       # seed → bench → concurrency
    python -m ipmgr.app --data-dir data_stress       # 부하 DB로 GUI 실행
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import random
import statistics
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ipmgr.constants import (ACT_ALLOCATE, ACT_SUBNET_CREATE, DB_FILE_NAME, STATUS_UNUSED, STATUS_USED)
from ipmgr.db.connection import create_connection
from ipmgr.db.schema import create_triggers, drop_triggers, init_schema
from ipmgr.logic import excel_export
from ipmgr.logic import integrity as ig
from ipmgr.models import integrity_model
from ipmgr.logic.subnet_calc import int_to_ip, ip_to_int
from ipmgr.models import group_model, history_model, ip_model, subnet_model
from ipmgr.models.common import ConflictError, get_revisions
from ipmgr.models.history_model import HISTORY_COLUMNS

OPERATOR = "부하테스트(127.0.0.1)"
NAMES = ["김민준", "이서연", "박지호", "최수아", "정예준", "강하은", "조도윤", "윤지우", "장시우", "임서윤",
         "한주원", "오하린", "서건우", "신지안", "권우진", "황채원", "안현우", "송다은", "류승민", "전예린"]
SEARCH_INDEXES = ("idx_ips_hostname", "idx_ips_assignee", "idx_ips_request_no", "idx_ips_note")
HIST_INDEXES = ("idx_hist_ip", "idx_hist_subnet", "idx_hist_range")


def _fmt_s(sec: float) -> str:
    return f"{sec * 1000:,.1f} ms" if sec < 1 else f"{sec:,.2f} s"


def _usage_ratio(rng: random.Random) -> float:
    """안전/경고/위험 단계가 고루 나오도록 대역별 사용률 분포를 만든다."""
    r = rng.random()
    if r < 0.70:
        return rng.uniform(0.0, 0.60)
    if r < 0.85:
        return rng.uniform(0.60, 0.80)
    if r < 0.93:
        return rng.uniform(0.80, 0.90)
    return rng.uniform(0.90, 1.0)


# ════════════════════════════════════════════════════════════
def seed(data_dir: Path, groups: int, history: bool, force: bool, seed_value: int) -> Path:
    """부하 테스트 DB 생성."""
    data_dir.mkdir(parents=True, exist_ok=True)
    db = data_dir / DB_FILE_NAME
    if db.exists():
        if not force:
            raise SystemExit(f"{db} 가 이미 있습니다. 덮어쓰려면 --force")
        db.unlink()
    rng = random.Random(seed_value)
    conn = create_connection(db)
    init_schema(conn)
    drop_triggers(conn)
    for name in SEARCH_INDEXES + HIST_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute("PRAGMA journal_mode=MEMORY")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-262144")

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    today = date.today()
    ip_sql = ("INSERT INTO ips(id, subnet_id, status, hostname, allocated_date, assignee, request_no, note, "
              "version, updated_at, updated_by, row_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
    hist_sql = (f"INSERT INTO history_logs(id, {', '.join(HISTORY_COLUMNS)}, chain_hash) "
                f"VALUES (?, {', '.join('?' * len(HISTORY_COLUMNS))}, ?)")
    # 위변조 감지 해시도 앱과 똑같이 부여(이력 체인 + 사용중 IP/대역/그룹 행 해시)
    chain = {"id": 0, "prev": ig.GENESIS}

    def hist(row: tuple) -> tuple:
        chain["id"] += 1
        chain["prev"] = ig.chain_hash(chain["prev"], (chain["id"], *row))
        return (chain["id"], *row, chain["prev"])
    print(f"[seed] 10.0.0.0/8 → /16 그룹 {groups}개 × /24 대역 256개 = 대역 {groups * 256:,}개, "
          f"IP {groups * 256 * 254:,}개 생성 → {db}")
    t0 = time.perf_counter()
    conn.execute("BEGIN")
    root_id = conn.execute("INSERT INTO groups(parent_id, name, description, created_at, updated_at, updated_by) "
                           "VALUES (NULL, ?, ?, ?, ?, ?)",
                           ("10.0.0.0/8 (부하테스트)", "스트레스 테스트용 /8 전체", ts, ts, OPERATOR)).lastrowid
    ig.seal_groups(conn, [root_id])
    conn.execute("COMMIT")
    req_no = 0
    total_used = 0
    for x in range(groups):
        conn.execute("BEGIN")
        gid = conn.execute("INSERT INTO groups(parent_id, name, description, created_at, updated_at, updated_by) "
                           "VALUES (?, ?, ?, ?, ?, ?)",
                           (root_id, f"10.{x}.0.0/16", f"/16 블록 {x}", ts, ts, OPERATOR)).lastrowid
        ig.seal_groups(conn, [gid])
        ip_rows: list[tuple] = []
        hist_rows: list[tuple] = []
        for y in range(256):
            net = (10 << 24) | (x << 16) | (y << 8)
            ratio = _usage_ratio(rng)
            used_set = set(rng.sample(range(1, 255), round(254 * ratio)))
            vlan = f"VLAN{(x * 256 + y) % 4000 + 1}"
            sid = conn.execute(
                "INSERT INTO subnets(group_id, cidr, network_int, broadcast_int, prefix, description, total_count, "
                "used_count, created_at, updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (gid, f"10.{x}.{y}.0/24", net, net + 255, 24, vlan, 254,
                 len(used_set), ts, ts, OPERATOR)).lastrowid
            conn.execute("UPDATE subnets SET row_hash = ? WHERE id = ?", (ig.subnet_row_hash(
                {"id": sid, "group_id": gid, "cidr": f"10.{x}.{y}.0/24", "network_int": net,
                 "broadcast_int": net + 255, "prefix": 24, "description": vlan}), sid))
            if history:
                hist_rows.append(hist((sid, None, f"10.{x}.{y}.0/24", net, net + 255, ACT_SUBNET_CREATE, "",
                                       STATUS_UNUSED, "", "", "", "", "",
                                       f"대역 생성: 10.{x}.{y}.0/24 — IP 254개 미사용 일괄 생성", OPERATOR, ts)))
            for z in range(1, 255):
                ip = net + z
                if z in used_set:
                    req_no += 1
                    alloc = (today - timedelta(days=rng.randrange(0, 1095))).isoformat()
                    host = f"srv-{x}-{y}-{z}"
                    name = NAMES[(x + y + z) % len(NAMES)]
                    req = f"REQ-2026-{req_no:07d}"
                    row = (ip, sid, STATUS_USED, host, alloc, name, req, "", 2, ts, OPERATOR)
                    ip_rows.append((*row, ig.ip_row_hash(dict(zip(ig.IP_FIELDS, row)))))
                    if history:
                        hist_rows.append(hist((sid, ip, f"10.{x}.{y}.{z}", None, None, ACT_ALLOCATE, STATUS_UNUSED,
                                               STATUS_USED, host, name, req, "", alloc,
                                               f"상태: 미사용 → 사용중; 호스트네임: '' → '{host}'", OPERATOR, ts)))
                else:
                    ip_rows.append((ip, sid, STATUS_UNUSED, "", "", "", "", "", 1, "", "", None))
            total_used += len(used_set)
        conn.executemany(ip_sql, ip_rows)
        if hist_rows:
            conn.executemany(hist_sql, hist_rows)
        conn.execute("COMMIT")
        done = x + 1
        elapsed = time.perf_counter() - t0
        eta = elapsed / done * (groups - done)
        print(f"\r[seed] /16 {done:>3}/{groups}  IP {done * 65024:>12,}  경과 {elapsed:6.1f}s  남은 예상 {eta:6.1f}s",
              end="", flush=True)
    print()
    t1 = time.perf_counter()
    print("[seed] 인덱스/트리거 생성 중…")
    conn.execute("PRAGMA journal_mode=TRUNCATE")
    conn.execute("PRAGMA synchronous=FULL")
    init_schema(conn)         # IF NOT EXISTS → 삭제했던 인덱스 재생성
    create_triggers(conn)
    conn.execute("UPDATE meta SET value = value + 1 WHERE key IN ('data_rev', 'tree_rev')")
    conn.execute("ANALYZE")
    conn.close()
    size = db.stat().st_size
    print(f"[seed] 완료: 데이터 {t1 - t0:,.1f}s + 인덱스 {time.perf_counter() - t1:,.1f}s, "
          f"사용중 {total_used:,}개, DB 크기 {size / 1024 ** 3:,.2f} GB")
    return db


# ════════════════════════════════════════════════════════════
class Bench:
    """응답 시간 측정기."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, float, str, float]] = []

    def run(self, name: str, fn: Callable[[], Any], repeat: int = 1, budget: float = 0.5,
            note: Callable[[Any], str] | None = None) -> Any:
        times, result = [], None
        for _ in range(repeat):
            t = time.perf_counter()
            result = fn()
            times.append(time.perf_counter() - t)
        value = statistics.median(times)
        self.rows.append((name, value, note(result) if note else "", budget))
        flag = "OK " if value <= budget else "SLOW"
        print(f"  [{flag}] {name:<46} {_fmt_s(value):>12}   {note(result) if note else ''}")
        return result


def bench(data_dir: Path) -> bool:
    """UI 경로별 응답 시간 측정 + 정합성 검증."""
    db = data_dir / DB_FILE_NAME
    conn = create_connection(db)
    init_schema(conn)
    b = Bench()
    print(f"[bench] {db} ({db.stat().st_size / 1024 ** 3:,.2f} GB)")
    roots = group_model.list_children(conn, None)["groups"]
    root = next(g for g in roots if g["name"].startswith("10.0.0.0/8"))
    print(f"  전체 대역 {root['subnet_count']:,}개 · IP {root['total']:,}개 · 사용중 {root['used']:,}개")

    print("── 트리/대시보드")
    b.run("폴링: get_revisions (1회)", lambda: get_revisions(conn), repeat=200, budget=0.005)
    b.run("트리: 최상위 노드(산하 통계 포함)", lambda: group_model.list_children(conn, None), repeat=3, budget=0.5)
    kids = b.run("트리: /8 펼침 → /16 그룹 256개(+통계)", lambda: group_model.list_children(conn, root["id"]),
                 repeat=3, budget=0.5, note=lambda r: f"{len(r['groups'])}개")
    g16 = kids["groups"][len(kids["groups"]) // 2]
    b.run("트리: /16 펼침 → /24 대역 256개", lambda: group_model.list_children(conn, g16["id"]), repeat=5,
          budget=0.2, note=lambda r: f"{len(r['subnets'])}개")
    b.run("대시보드: /8 그룹 (65,536 대역 집계, 256개 표시)", lambda: subnet_model.group_dashboard(conn, root["id"]),
          repeat=3, budget=1.0, note=lambda r: f"집계 {r['stats']['subnet_count']:,}개")
    b.run("대시보드: /16 그룹 (256 대역)", lambda: subnet_model.group_dashboard(conn, g16["id"]), repeat=5,
          budget=0.2)

    print("── 서브넷/IP")
    subnet = group_model.list_children(conn, g16["id"])["subnets"][100]
    detail = b.run("서브넷 뷰: /24 IP 254개 조회", lambda: ip_model.subnet_detail(conn, subnet["id"]), repeat=10,
                   budget=0.1)
    used_ip = next((r for r in detail["ips"] if r["status"] == STATUS_USED), detail["ips"][0])
    b.run("IP 상세 + 생애주기 타임라인", lambda: ip_model.ip_detail(conn, used_ip["id"]), repeat=20, budget=0.05)

    rng = random.Random(7)
    targets = rng.sample(range(ip_to_int("10.0.0.1"), ip_to_int(f"10.{len(kids['groups']) - 1}.255.254")), 200)
    targets = [t for t in targets if t & 255 not in (0, 255)]

    def save_many() -> int:
        n = 0
        for ip in targets:
            row = ip_model.get_ip(conn, ip)
            if row is None:
                continue
            new = STATUS_UNUSED if row["status"] == STATUS_USED else STATUS_USED
            ip_model.update_ip(conn, ip, row["version"], {**row, "status": new, "note": "bench"}, OPERATOR)
            n += 1
        return n
    t = time.perf_counter()
    n = save_many()
    per = (time.perf_counter() - t) / max(n, 1)
    b.rows.append(("IP 저장(낙관적 잠금+이력) 1건 평균", per, f"{n}건", 0.05))
    print(f"  [{'OK ' if per <= 0.05 else 'SLOW'}] {'IP 저장(낙관적 잠금+이력) 1건 평균':<46} {_fmt_s(per):>12}   {n}건 (fsync 포함)")

    row = ip_model.get_ip(conn, targets[0])
    stale = row["version"]
    ip_model.update_ip(conn, row["id"], stale, {**row, "note": "first"}, "사용자A(1.1.1.1)")
    try:
        ip_model.update_ip(conn, row["id"], stale, {**row, "note": "second"}, "사용자B(1.1.1.2)")
        conflict_ok = False
    except ConflictError as exc:
        conflict_ok = exc.latest["updated_by"] == "사용자A(1.1.1.1)"
    print(f"  [{'OK ' if conflict_ok else 'FAIL'}] 버전 충돌 감지 및 최신 작업자 안내")

    print("── 검색")
    b.run("검색: IP 접두어 '10.1.2'", lambda: ip_model.search(conn, "10.1.2"), repeat=3, budget=0.3,
          note=lambda r: f"{len(r['rows'])}건")
    b.run(f"검색: 호스트네임 'srv-{len(kids['groups']) - 1}-200-1'",
          lambda: ip_model.search(conn, f"srv-{len(kids['groups']) - 1}-200-1"), repeat=1, budget=10,
          note=lambda r: f"{len(r['rows'])}건")
    b.run("검색: 담당자 '김민준' (결과 상한 1,000)", lambda: ip_model.search(conn, "김민준"), repeat=1, budget=1,
          note=lambda r: f"{len(r['rows'])}건{' (상한)' if r['truncated'] else ''}")
    b.run("검색: 없는 값 (부분 인덱스 전체 스캔 최악)", lambda: ip_model.search(conn, "zz-no-match"), repeat=1,
          budget=15, note=lambda r: f"{len(r['rows'])}건")

    print("── 일괄 처리")
    sid = subnet["id"]
    free = {r["id"]: r["version"] for r in ip_model.subnet_detail(conn, sid)["ips"]
            if r["status"] == STATUS_UNUSED}
    free = dict(list(free.items())[:100])
    b.run(f"IP 일괄 등록 {len(free)}개 (공통정보+IP별 호스트네임, 이력)",
          lambda: ip_model.assign_ips(conn, free, {i: f"bulk-{i & 255}" for i in free}, "부하", "REQ-BULK", "",
                                      OPERATOR), budget=0.5, note=lambda r: f"등록 {r['count']}")
    after = ip_model.get_ips(conn, free)
    b.run(f"IP 일괄 회수 {len(free)}개",
          lambda: ip_model.reclaim_ips(conn, {i: after[i]["version"] for i in free}, "부하", "RET-BULK", "bench",
                                       OPERATOR), budget=0.5, note=lambda r: f"회수 {r['count']}")
    bulk_group = b.run("그룹 생성", lambda: group_model.create_group(conn, f"bench-{time.time():.0f}", None, "", OPERATOR),
                       budget=0.2)
    b.run("분할 생성 172.31.0.0/16 → /24 256개 (IP 65,024)",
          lambda: subnet_model.create_split_subnets(conn, bulk_group, "172.31.0.0/16", 24, "bench", True, OPERATOR),
          budget=5, note=lambda r: f"대역 {r['count']}")
    b.run("그룹 삭제 (대역 256 / IP 65,024)", lambda: group_model.delete_group(conn, bulk_group, OPERATOR), budget=5)

    print("── 무결성(위변조 감지)")
    anchor = Path(tempfile.mkdtemp(prefix="anchor_")) / "anchor"
    full = b.run("최초 기준 검사 (이전 버전 DB 1회만: 이력 체인 + 모든 행 해시)",
                 lambda: integrity_model.full_check(None, anchor, conn=conn), budget=300,
                 note=lambda r: f"{'정상' if r.ok else r.summary()} · 이력 {r.checked.get('history', 0):,} · "
                                f"IP해시 {r.checked.get('ips', 0):,}")
    ip_model.update_ip(conn, targets[1], ip_model.get_ip(conn, targets[1])["version"],
                       {**ip_model.get_ip(conn, targets[1]), "note": "quick"}, OPERATOR)
    b.run("빠른 검사 (감시 장치 + 새 이력 + 외부 수정 행)", lambda: integrity_model.quick_check(conn, anchor),
          budget=0.05, note=lambda r: "정상" if r.ok else r.summary())
    conn.execute("UPDATE ips SET hostname = 'tampered' WHERE id = ?", (targets[1],))     # 외부 프로그램 변조 흉내
    caught = b.run("빠른 검사 (외부 변조 1건 있음)", lambda: integrity_model.quick_check(conn, anchor), budget=0.05,
                   note=lambda r: r.summary())
    integrity_model.recover(conn, anchor, Path(tempfile.mkdtemp(prefix="bk_")), OPERATOR)
    fixed = integrity_model.quick_check(conn, anchor)
    integ_ok = full.ok and caught.counts.get("ip") == 1 and fixed.ok
    print(f"  [{'OK ' if integ_ok else 'FAIL'}] 외부 변조 1건 감지({caught.summary()}) → 원클릭 복구(이력 기준) → 재검사 정상")
    b.run("통합검색 기본 화면: 최근 이력 200건", lambda: history_model.recent_history(conn, None, 200), budget=0.05)

    print("── Excel")
    out = Path(tempfile.mkdtemp(prefix="bench_"))
    b.run("Excel 모드 B: /24 (IP현황 + 전체이력)",
          lambda: excel_export.write_report(excel_export.collect_subnet_report(conn, sid, OPERATOR), out / "b.xlsx"),
          budget=3)
    b.run("Excel 모드 A: /16 그룹 (256행)",
          lambda: excel_export.write_report(excel_export.collect_group_report(conn, g16["id"], OPERATOR),
                                            out / "a16.xlsx"), budget=3)
    b.run(f"Excel 모드 A: /8 그룹 ({root['subnet_count']:,}행)",
          lambda: excel_export.write_report(excel_export.collect_group_report(conn, root["id"], OPERATOR),
                                            out / "a8.xlsx"), budget=60)
    print(f"  출력 폴더: {out}")

    print("── 정합성")
    sample = conn.execute("SELECT id, network_int, broadcast_int, total_count, used_count FROM subnets "
                          "ORDER BY random() LIMIT 300").fetchall()
    bad = 0
    for s in sample:
        total, used = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(status = '{STATUS_USED}'), 0) FROM ips "
                                   "WHERE id BETWEEN ? AND ?", (s["network_int"], s["broadcast_int"])).fetchone()
        bad += (total, used) != (s["total_count"], s["used_count"])
    print(f"  [{'OK ' if not bad else 'FAIL'}] 서브넷 통계 카운터 표본 300개 일치 (불일치 {bad})")
    conn.close()
    slow = [r for r in b.rows if r[1] > r[3]]
    print(f"[bench] 완료 — 기준 초과 {len(slow)}건")
    return conflict_ok and not bad and integ_ok


# ════════════════════════════════════════════════════════════
def _worker_proc(db: str, hot: list[int], seconds: float, idx: int, out: mp.Queue) -> None:
    """한 프로세스 = 한 사용자. 핫스팟 IP를 무작위로 읽고-생각하고-저장한다."""
    conn = create_connection(db)
    rng = random.Random(idx)
    op = f"사용자{idx:02d}(10.99.0.{idx})"
    ok = conflict = errors = 0
    lat: list[float] = []
    end = time.time() + seconds
    while time.time() < end:
        ip = rng.choice(hot)
        try:
            row = ip_model.get_ip(conn, ip)
            time.sleep(rng.uniform(0, 0.02))     # 사용자가 폼을 보고 수정하는 시간
            new = STATUS_UNUSED if row["status"] == STATUS_USED else STATUS_USED
            t = time.perf_counter()
            ip_model.update_ip(conn, ip, row["version"], {**row, "status": new, "assignee": op[:3]}, op)
            lat.append(time.perf_counter() - t)
            ok += 1
        except ConflictError:
            conflict += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"  ! 프로세스 {idx}: {type(exc).__name__}: {exc}")
    conn.close()
    out.put((ok, conflict, errors, lat))


def concurrency(data_dir: Path, procs: int, seconds: float) -> bool:
    """다중 프로세스 동시 수정: 충돌 감지, 잠금 대기, 카운터/이력 정합성 검증."""
    db = data_dir / DB_FILE_NAME
    conn = create_connection(db)
    subnets = conn.execute("SELECT id, network_int FROM subnets ORDER BY id LIMIT 2").fetchall()
    hot = [s["network_int"] + z for s in subnets for z in range(1, 41)]   # 80개 IP에 집중 → 의도적 경합
    hist_before = conn.execute("SELECT COUNT(*) FROM history_logs").fetchone()[0]
    start = conn.execute("SELECT id, chain_hash FROM history_logs ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    print(f"[concurrency] 프로세스 {procs}개 × {seconds:g}초, 핫스팟 IP {len(hot)}개 (journal_mode=TRUNCATE)")
    q: mp.Queue = mp.Queue()
    ps = [mp.Process(target=_worker_proc, args=(str(db), hot, seconds, i + 1, q)) for i in range(procs)]
    for p in ps:
        p.start()
    results = [q.get() for _ in ps]
    for p in ps:
        p.join()
    ok = sum(r[0] for r in results)
    conflict = sum(r[1] for r in results)
    errors = sum(r[2] for r in results)
    lat = sorted(x for r in results for x in r[3])
    p95 = lat[int(len(lat) * 0.95)] if lat else 0
    print(f"  저장 성공 {ok:,} · 충돌 감지 {conflict:,} · 오류 {errors} · 처리량 {ok / seconds:,.1f} 건/s · "
          f"저장 지연 p50 {_fmt_s(statistics.median(lat) if lat else 0)} / p95 {_fmt_s(p95)}")
    conn = create_connection(db)
    hist_after = conn.execute("SELECT COUNT(*) FROM history_logs").fetchone()[0]
    bad = 0
    for s in conn.execute("SELECT * FROM subnets WHERE id IN (?, ?)", (subnets[0]["id"], subnets[1]["id"])):
        total, used = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(status = '{STATUS_USED}'), 0) FROM ips "
                                   "WHERE id BETWEEN ? AND ?", (s["network_int"], s["broadcast_int"])).fetchone()
        bad += (total, used) != (s["total_count"], s["used_count"])
    check = conn.execute("PRAGMA quick_check(1)").fetchone()[0] if os.environ.get("QUICK_CHECK") else "skip"
    conn.close()
    hist_ok = hist_after - hist_before == ok
    print(f"  [{'OK ' if hist_ok else 'FAIL'}] 이력 증가 {hist_after - hist_before:,} = 성공 저장 {ok:,}")
    # 여러 프로세스가 동시에 이어 붙인 이력도 해시 체인이 끊기지 않아야 한다
    chain_report = integrity_model.Report("quick")
    conn = create_connection(db)
    integrity_model.verify_chain(conn, chain_report, start[0], bytes(start[1]))
    conn.close()
    hist_ok = hist_ok and chain_report.ok
    print(f"  [{'OK ' if chain_report.ok else 'FAIL'}] 동시 기록된 이력 {chain_report.checked.get('history', 0):,}건 "
          f"해시 체인 무결성 ({chain_report.summary()})")
    print(f"  [{'OK ' if not bad else 'FAIL'}] 경합 대역 통계 카운터 일치")
    print(f"  [{'OK ' if errors == 0 else 'FAIL'}] 잠금 타임아웃/손상 오류 0건   (quick_check: {check})")
    return hist_ok and not bad and errors == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="IP관리 부하 테스트")
    parser.add_argument("command", choices=("seed", "bench", "concurrency", "all"))
    parser.add_argument("--data-dir", default="data_stress", help="DB 폴더 (기본: data_stress)")
    parser.add_argument("--groups", type=int, default=256, help="생성할 /16 그룹 수 (1~256, 기본 256 = 10.0.0.0/8 전체)")
    parser.add_argument("--no-history", action="store_true", help="시드 시 이력 생성 생략")
    parser.add_argument("--force", action="store_true", help="기존 부하 DB 덮어쓰기")
    parser.add_argument("--procs", type=int, default=8)
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--seed", type=int, default=20261101)
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve()
    groups = max(1, min(256, args.groups))
    passed = True
    if args.command in ("seed", "all"):
        seed(data_dir, groups, not args.no_history, args.force or args.command == "all", args.seed)
    if args.command in ("bench", "all"):
        passed &= bench(data_dir)
    if args.command in ("concurrency", "all"):
        passed &= concurrency(data_dir, args.procs, args.seconds)
    print("\n결과:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
