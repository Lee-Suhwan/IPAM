"""핵심 로직/모델 단위 테스트: python -m unittest discover -s tests"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ipmgr.constants import (ACT_ALLOCATE, ACT_REALLOCATE, ACT_RECLAIM, ACT_SUBNET_CREATE, STATUS_UNUSED,
                            STATUS_USED)
import tkinter as tk
from ipmgr.db.connection import create_connection
from ipmgr.db.schema import init_schema, recalc_counters
from ipmgr.db.worker import DBWorker
from ipmgr.logic import excel_export
from ipmgr.logic import state_machine as sm
from ipmgr.logic.subnet_calc import ip_prefix_range, ip_to_int, parse_cidr, split_cidr, usage_level
from ipmgr.models import group_model, ip_model, subnet_model
from ipmgr.models.common import ConflictError, ValidationError, get_revisions
from ipmgr.models.history_model import ip_timeline, subnet_history
from ipmgr.logic import integrity as integrity_ig
from ipmgr.models import integrity_model
from ipmgr.utils import backup_mgr
from ipmgr.utils.user_tracker import extract_korean_name, get_operator_info

OP = "홍길동(10.0.0.5)"
OP2 = "김철수(10.0.0.6)"


class BaseDB(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "data.db"
        self.conn = create_connection(self.db_path)
        init_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def make_subnet(self, cidr: str = "10.1.1.0/24") -> tuple[int, int]:
        gid = group_model.create_group(self.conn, "본사", None, "", OP)
        sid = subnet_model.create_subnet(self.conn, gid, cidr, "서버팜", True, OP)["id"]
        return gid, sid


class TestCalc(unittest.TestCase):
    def test_parse_cidr(self) -> None:
        info = parse_cidr("10.1.2.7/24")
        self.assertEqual(info.cidr, "10.1.2.0/24")
        self.assertTrue(info.normalized)
        self.assertEqual(info.host_count, 254)
        self.assertEqual(parse_cidr("10.0.0.0/32").host_count, 1)
        with self.assertRaises(ValueError):
            parse_cidr("10.0.0.0/12")

    def test_split_and_prefix(self) -> None:
        self.assertEqual(len(split_cidr("10.5.0.0/16", 24, 1024)), 256)
        # 유연한 분할 단위: /24 → /25, /16 → /20, /22 → /26
        self.assertEqual([s.cidr for s in split_cidr("10.0.1.0/24", 25, 1024)], ["10.0.1.0/25", "10.0.1.128/25"])
        self.assertEqual([s.host_count for s in split_cidr("10.0.1.0/24", 25, 1024)], [126, 126])
        sixteen = split_cidr("10.0.1.0/16", 20, 1024)
        self.assertEqual((len(sixteen), sixteen[0].cidr, sixteen[-1].cidr), (16, "10.0.0.0/20", "10.0.240.0/20"))
        self.assertEqual(len(split_cidr("10.9.0.0/22", 26, 1024)), 16)
        with self.assertRaises(ValueError):             # 원본보다 큰 단위는 불가
            split_cidr("10.0.1.0/24", 23, 1024)
        # /16까지는 분할 없이 그대로 등록, 그보다 크면 분할 필요
        self.assertEqual(parse_cidr("10.0.0.0/16").host_count, 65534)
        self.assertEqual(parse_cidr("10.0.0.0/22").host_count, 1022)
        with self.assertRaises(ValueError):
            parse_cidr("10.0.0.0/15")
        # 한 번에 만드는 IP 총수 제한(262,144): /14 → /16 4개는 가능, /13 → /16 8개는 초과
        self.assertEqual(len(split_cidr("10.0.0.0/14", 16, 16384)), 4)
        with self.assertRaises(ValueError):
            split_cidr("10.0.0.0/13", 16, 16384)
        self.assertEqual(len(split_cidr("10.0.0.0/16", 30, 16384)), 16384)    # /30 16,384개 = IP 32,768
        self.assertEqual(ip_prefix_range("10.1"), (ip_to_int("10.1.0.0"), ip_to_int("10.1.255.255")))
        self.assertEqual(ip_prefix_range("10.1.2.3"), (ip_to_int("10.1.2.3"),) * 2)
        self.assertIsNone(ip_prefix_range("web01"))
        self.assertIsNone(ip_prefix_range("2024"))

    def test_levels(self) -> None:
        # 경고 > 80%, 위험 > 90%
        self.assertEqual(usage_level(80), "safe")
        self.assertEqual(usage_level(80.1), "warning")
        self.assertEqual(usage_level(90), "warning")
        self.assertEqual(usage_level(90.1), "critical")

    def test_state_machine(self) -> None:
        d = date(2026, 1, 2)
        # 변경일 = 할당 또는 회수가 일어난 날(회수 시 지우지 않음), 정보 수정은 유지
        self.assertEqual(sm.resolve_changed_date(STATUS_UNUSED, STATUS_USED, "", d), "2026-01-02")
        self.assertEqual(sm.resolve_changed_date(STATUS_USED, STATUS_USED, "2025-05-05", d), "2025-05-05")
        self.assertEqual(sm.resolve_changed_date(STATUS_USED, STATUS_UNUSED, "2025-05-05", d), "2026-01-02")
        self.assertEqual(sm.resolve_changed_date(None, STATUS_USED, "", d), "2026-01-02")
        with self.assertRaises(ValueError):
            sm.validate_status("예약")
        self.assertEqual(sm.determine_action(STATUS_UNUSED, STATUS_USED, True), ACT_REALLOCATE)

    def test_korean_name(self) -> None:
        self.assertEqual(extract_korean_name("홍길동/부장/IT팀"), "홍길동")
        self.assertEqual(extract_korean_name("Hong Gildong (홍길동)"), "홍길동")
        self.assertEqual(extract_korean_name("john"), "")
        self.assertRegex(get_operator_info(), r".+\(\d+\.\d+\.\d+\.\d+\)$")


class TestModels(BaseDB):
    def test_subnet_create_and_counts(self) -> None:
        gid, sid = self.make_subnet()
        s = subnet_model.get_subnet(self.conn, sid)
        self.assertEqual((s["total_count"], s["used_count"]), (254, 0))
        with self.assertRaises(ValidationError):
            subnet_model.create_subnet(self.conn, gid, "10.1.1.128/25", "", True, OP)
        res = subnet_model.create_split_subnets(self.conn, gid, "10.2.0.0/22", 24, "", True, OP)
        self.assertEqual(res, {"count": 4, "ip_count": 1016})
        with self.assertRaises(ValidationError):
            subnet_model.create_split_subnets(self.conn, gid, "10.1.0.0/23", 24, "", True, OP)

    def test_lifecycle_and_conflict(self) -> None:
        _, sid = self.make_subnet()
        ip = ip_to_int("10.1.1.10")
        row = ip_model.get_ip(self.conn, ip)
        values = {"status": STATUS_USED, "hostname": "web01", "assignee": "김철수", "request_no": "REQ-1", "note": ""}
        r1 = ip_model.update_ip(self.conn, ip, row["version"], values, OP)
        self.assertEqual(r1["action"], ACT_ALLOCATE)
        self.assertEqual(r1["ip"]["allocated_date"], date.today().isoformat())
        # 오래된 버전으로 저장 → 충돌
        with self.assertRaises(ConflictError) as ctx:
            ip_model.update_ip(self.conn, ip, row["version"], {**values, "hostname": "x"}, OP2)
        self.assertEqual(ctx.exception.latest["hostname"], "web01")
        self.assertEqual(ctx.exception.latest["updated_by"], OP)
        v = r1["ip"]["version"]
        r2 = ip_model.update_ip(self.conn, ip, v, {**values, "status": STATUS_UNUSED}, OP2)
        self.assertEqual(r2["action"], ACT_RECLAIM)
        self.assertEqual(r2["ip"]["allocated_date"], date.today().isoformat())     # 회수일
        r3 = ip_model.update_ip(self.conn, ip, r2["ip"]["version"], values, OP)
        self.assertEqual(r3["action"], ACT_REALLOCATE)
        self.assertEqual(subnet_model.get_subnet(self.conn, sid)["used_count"], 1)
        unchanged = ip_model.update_ip(self.conn, ip, r3["ip"]["version"], values, OP)
        self.assertFalse(unchanged["changed"])
        tl = ip_timeline(self.conn, ip, newest_first=False)
        self.assertEqual([t["action"] for t in tl], [ACT_SUBNET_CREATE, ACT_ALLOCATE, ACT_RECLAIM, ACT_REALLOCATE])

    def test_concurrent_connections_conflict(self) -> None:
        """두 개의 독립 커넥션(=두 사용자)이 같은 버전으로 저장 → 한 명만 성공."""
        _, _ = self.make_subnet()
        ip = ip_to_int("10.1.1.20")
        version = ip_model.get_ip(self.conn, ip)["version"]
        results: list[str] = []

        def user(op: str, host: str) -> None:
            conn = create_connection(self.db_path)
            try:
                ip_model.update_ip(conn, ip, version, {"status": STATUS_USED, "hostname": host}, op)
                results.append("ok")
            except ConflictError:
                results.append("conflict")
            finally:
                conn.close()

        threads = [threading.Thread(target=user, args=(f"사용자{i}(1.1.1.{i})", f"h{i}")) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("conflict"), 5)

    def test_bulk_assign_and_reclaim(self) -> None:
        _, sid = self.make_subnet()
        ips = [ip_to_int(f"10.1.1.{n}") for n in (10, 11, 12)]
        versions = {i: 1 for i in ips}
        hosts = {ips[0]: "web01", ips[1]: "web02", ips[2]: ""}
        with self.assertRaises(ValidationError):          # 담당자/요청번호 필수
            ip_model.assign_ips(self.conn, versions, hosts, "", "REQ-1", "", OP)
        res = ip_model.assign_ips(self.conn, versions, hosts, "김철수", "REQ-1", "웹서버", OP)
        self.assertEqual((res["applied"], res["count"]), (True, 3))
        rows = ip_model.get_ips(self.conn, ips)
        self.assertEqual([rows[i]["hostname"] for i in ips], ["web01", "web02", ""])
        self.assertTrue(all(r["status"] == STATUS_USED and r["assignee"] == "김철수" and r["request_no"] == "REQ-1"
                            and r["note"] == "웹서버" and r["allocated_date"] == date.today().isoformat()
                            for r in rows.values()))
        self.assertEqual(subnet_model.get_subnet(self.conn, sid)["used_count"], 3)
        # 이미 사용중인 IP를 오래된 버전으로 다시 등록 → 전체 취소 + 충돌 사유(작업자 포함)
        res = ip_model.assign_ips(self.conn, {ips[0]: 1, ip_to_int("10.1.1.20"): 1}, {}, "박", "REQ-2", "", OP2)
        self.assertFalse(res["applied"])
        self.assertIn(OP, res["conflicts"][0][1])
        self.assertEqual(ip_model.get_ip(self.conn, ip_to_int("10.1.1.20"))["status"], STATUS_UNUSED)
        # 회수: 호스트네임 초기화, 변경일=회수일, 회수 정보로 대체, 이전 값은 이력에 보존
        res = ip_model.reclaim_ips(self.conn, {i: rows[i]["version"] for i in ips[:2]}, "이영희", "RET-9",
                                   "서버 폐기", OP2)
        self.assertTrue(res["applied"])
        r = ip_model.get_ip(self.conn, ips[0])
        self.assertEqual((r["status"], r["hostname"], r["allocated_date"], r["assignee"], r["request_no"], r["note"]),
                         (STATUS_UNUSED, "", date.today().isoformat(), "이영희", "RET-9", "서버 폐기"))
        self.assertEqual(subnet_model.get_subnet(self.conn, sid)["used_count"], 1)
        res = ip_model.assign_ips(self.conn, {ips[0]: r["version"]}, {ips[0]: "web01b"}, "최", "REQ-3", "", OP)
        tl = ip_timeline(self.conn, ips[0], newest_first=False)
        self.assertEqual([t["action"] for t in tl], [ACT_SUBNET_CREATE, ACT_ALLOCATE, ACT_RECLAIM, ACT_REALLOCATE])
        self.assertIn("'web01' → ''", tl[2]["diff_summary"])

    def test_delete_rules(self) -> None:
        gid, sid = self.make_subnet()
        ip = ip_to_int("10.1.1.30")
        ip_model.update_ip(self.conn, ip, 1, {"status": STATUS_USED, "hostname": "db"}, OP)
        with self.assertRaises(ValidationError):
            subnet_model.delete_subnet(self.conn, sid, OP)
        with self.assertRaises(ValidationError):
            group_model.delete_group(self.conn, gid, OP)
        ip_model.update_ip(self.conn, ip, 2, {"status": STATUS_UNUSED, "hostname": "db"}, OP)
        summary = group_model.delete_group(self.conn, gid, OP)
        self.assertEqual(summary["subnets"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ips").fetchone()[0], 0)
        # 삭제 후에도 이력은 남는다
        self.assertGreater(len(ip_timeline(self.conn, ip)), 0)

    def test_big_subnet_without_split(self) -> None:
        """/16 대역을 분할 없이 그대로 등록하고, 끝 IP의 할당·이력·무결성까지 정상 동작."""
        res = subnet_model.create_subnet(self.conn, None, "172.20.0.0/16", "큰 대역", True, OP)
        self.assertEqual(res["ip_count"], 65534)
        sid = res["id"]
        last = ip_to_int("172.20.255.254")
        ip_model.assign_ips(self.conn, {last: 1}, {last: "edge"}, "김", "R-1", "", OP)
        detail = ip_model.subnet_detail(self.conn, sid)
        self.assertEqual((len(detail["ips"]), detail["subnet"]["used_count"]), (65534, 1))
        actions = [e["action"] for e in ip_timeline(self.conn, last, newest_first=False)]
        self.assertEqual(actions, [ACT_SUBNET_CREATE, ACT_ALLOCATE])    # 범위 이벤트 조회 범위(/16)도 정상
        self.assertTrue(integrity_model.full_check(None, None, conn=self.conn).ok)

    def test_subnet_without_group(self) -> None:
        sid = subnet_model.create_subnet(self.conn, None, "192.168.0.0/24", "그룹 없음", True, OP)["id"]
        root = group_model.list_children(self.conn, None)
        self.assertEqual([s["id"] for s in root["subnets"]], [sid])
        self.assertEqual(ip_model.subnet_detail(self.conn, sid)["path"], group_model.NO_GROUP_LABEL)
        gid = group_model.create_group(self.conn, "본사", None, "", OP)
        subnet_model.move_subnet(self.conn, sid, gid, OP)
        self.assertEqual(group_model.list_children(self.conn, None)["subnets"], [])
        subnet_model.move_subnet(self.conn, sid, None, OP)
        self.assertEqual(group_model.list_children(self.conn, None)["subnets"][0]["id"], sid)
        self.assertEqual(ip_model.locate_ip(self.conn, ip_to_int("192.168.0.5"))["group_chain"], [])
        out = Path(self.tmp.name) / "s.xlsx"
        excel_export.write_report(excel_export.collect_subnet_report(self.conn, sid, OP), out)
        subnet_model.create_split_subnets(self.conn, None, "192.168.4.0/23", 24, "", True, OP)
        self.assertEqual(len(group_model.list_children(self.conn, None)["subnets"]), 3)
        subnet_model.delete_subnet(self.conn, sid, OP)

    def test_scope_paging_and_multi_ops(self) -> None:
        a = group_model.create_group(self.conn, "A", None, "", OP)
        b = group_model.create_group(self.conn, "B", a, "", OP)
        subnet_model.create_split_subnets(self.conn, a, "10.0.0.0/22", 24, "", True, OP)    # 4개
        subnet_model.create_split_subnets(self.conn, b, "10.0.4.0/23", 24, "", True, OP)    # 2개
        loose = subnet_model.create_subnet(self.conn, None, "10.9.0.0/24", "", True, OP)["id"]
        page = subnet_model.group_dashboard(self.conn, a, 0, 4)
        self.assertEqual((page["stats"]["subnet_count"], len(page["subnets"])), (6, 4))
        nxt = subnet_model.group_dashboard(self.conn, a, 4, 4)
        self.assertEqual([s["cidr"] for s in nxt["subnets"]], ["10.0.4.0/24", "10.0.5.0/24"])
        # 그룹 + 중첩 하위 그룹 + 그룹 밖 대역 동시 선택 → 중복 없이 합산
        scope = subnet_model.scope_page(self.conn, [a, b], [loose], 0, 100)
        self.assertEqual(scope["stats"]["subnet_count"], 7)
        # 다중 이동: 대역 2개를 그룹 밖으로
        ids = [s["id"] for s in nxt["subnets"]]
        subnet_model.move_nodes(self.conn, [("subnet", i) for i in ids], None, OP)
        self.assertEqual(len(group_model.list_children(self.conn, None)["subnets"]), 3)
        with self.assertRaises(ValidationError):      # 순환 이동은 전체 취소
            subnet_model.move_nodes(self.conn, [("subnet", loose), ("group", a)], b, OP)
        self.assertIsNone(subnet_model.get_subnet(self.conn, loose)["group_id"])
        # 다중 삭제: 사용중 IP가 하나라도 있으면 전체 취소
        ip_model.assign_ips(self.conn, {ip_to_int("10.9.0.1"): 1}, {}, "김", "R-1", "", OP)
        with self.assertRaises(ValidationError):
            subnet_model.delete_nodes(self.conn, [("group", a), ("subnet", loose)], OP)
        self.assertIsNotNone(group_model.get_group(self.conn, a))
        res = subnet_model.delete_nodes(self.conn, [("group", b), ("group", a), ("subnet", ids[0])], OP)
        self.assertEqual((res["groups"], res["subnets"]), (2, 5))

    def test_tree_order_drag_and_drop(self) -> None:
        """트리 순서: 기본(그룹 이름순 → 대역 IP순) → 드래그로 순서 변경 → 이후 추가 항목은 맨 끝."""
        b = group_model.create_group(self.conn, "B", None, "", OP)
        a = group_model.create_group(self.conn, "A", None, "", OP)
        s2 = subnet_model.create_subnet(self.conn, None, "10.0.2.0/24", "", True, OP)["id"]
        s1 = subnet_model.create_subnet(self.conn, None, "10.0.1.0/24", "", True, OP)["id"]
        order = lambda pid=None: group_model.ordered_children(self.conn, pid)  # noqa: E731
        self.assertEqual(order(), [("group", a), ("group", b), ("subnet", s1), ("subnet", s2)])
        # 대역 s2를 그룹 A 앞으로(종류가 달라도 자유롭게)
        subnet_model.place_nodes(self.conn, [("subnet", s2)], None, ("group", a), False, OP)
        self.assertEqual(order(), [("subnet", s2), ("group", a), ("group", b), ("subnet", s1)])
        # 그룹 B를 맨 앞(s2 앞)으로, 여러 개 동시에(s1, B)를 A 뒤로
        subnet_model.place_nodes(self.conn, [("subnet", s1), ("group", b)], None, ("group", a), True, OP)
        self.assertEqual(order(), [("subnet", s2), ("group", a), ("subnet", s1), ("group", b)])
        c = group_model.create_group(self.conn, "C-새그룹", None, "", OP)          # 순서를 정한 위치 → 맨 끝
        self.assertEqual(order()[-1], ("group", c))
        # 다른 그룹 안의 특정 위치로 이동(이동 이력 기록) + 순환 참조 차단
        inner = subnet_model.create_subnet(self.conn, a, "10.0.9.0/24", "", True, OP)["id"]
        subnet_model.place_nodes(self.conn, [("subnet", s2)], a, ("subnet", inner), False, OP)
        self.assertEqual(order(a), [("subnet", s2), ("subnet", inner)])
        self.assertEqual(ip_timeline(self.conn, ip_to_int("10.0.2.1"))[0]["action"], ACT_SUBNET_CREATE)
        child = group_model.create_group(self.conn, "child", a, "", OP)
        with self.assertRaises(ValidationError):
            subnet_model.place_nodes(self.conn, [("group", a)], a, ("group", child), False, OP)
        # 순서 변경은 위변조 해시 대상이 아니다
        self.assertTrue(integrity_model.full_check(None, None, conn=self.conn).ok)

    def test_history_page(self) -> None:
        _, sid = self.make_subnet()
        subnet = subnet_model.get_subnet(self.conn, sid)
        ips = [ip_to_int(f"10.1.1.{n}") for n in range(1, 6)]
        ip_model.assign_ips(self.conn, {i: 1 for i in ips}, {i: f"h{i & 255}" for i in ips}, "김", "R-1", "", OP)
        whole = ip_model.history_for(self.conn, sid, None, True, 3)
        self.assertEqual((len(whole["rows"]), whole["has_more"]), (3, True))
        whole = ip_model.history_for(self.conn, sid, None, True, 100)
        self.assertEqual(len(whole["rows"]), 6)                     # 대역 생성 1 + 할당 5
        two = ip_model.history_for(self.conn, sid, ips[:2], False, 100)["rows"]
        self.assertEqual([r["action"] for r in two], [ACT_SUBNET_CREATE, ACT_ALLOCATE, ACT_ALLOCATE])
        self.assertEqual(subnet["id"], sid)
        from ipmgr.ui.history_view import parse_diff
        diff = parse_diff(two[1]["diff_summary"])
        self.assertEqual(diff[0], ("상태", "미사용", "사용중"))
        self.assertIn(("호스트네임", "", "h1"), diff)
        self.assertIsNone(parse_diff("대역 생성: 10.1.1.0/24 — 기타"))

    def test_migrate_v1_schema(self) -> None:
        """group_id NOT NULL 이던 v1 DB가 데이터 보존한 채 자동 변환되는지."""
        path = Path(self.tmp.name) / "v1.db"
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE groups (id INTEGER PRIMARY KEY, parent_id INTEGER, name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '', updated_by TEXT NOT NULL DEFAULT '');
            CREATE TABLE subnets (id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES groups(id),
                cidr TEXT NOT NULL, network_int INTEGER NOT NULL UNIQUE, broadcast_int INTEGER NOT NULL,
                prefix INTEGER NOT NULL, description TEXT NOT NULL DEFAULT '', total_count INTEGER NOT NULL DEFAULT 0,
                used_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '', updated_by TEXT NOT NULL DEFAULT '');
            INSERT INTO groups(id, name) VALUES (1, 'G');
            INSERT INTO subnets(id, group_id, cidr, network_int, broadcast_int, prefix, total_count)
                VALUES (7, 1, '10.0.0.0/24', 167772160, 167772415, 24, 0);
        """)
        old.commit()
        old.close()
        conn = create_connection(path)
        init_schema(conn)
        notnull = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(subnets)")}["group_id"]
        self.assertEqual(notnull, 0)
        self.assertEqual(subnet_model.get_subnet(conn, 7)["cidr"], "10.0.0.0/24")
        subnet_model.move_subnet(conn, 7, None, OP)
        self.assertIsNone(subnet_model.get_subnet(conn, 7)["group_id"])
        conn.close()

    def test_move_cycle(self) -> None:
        a = group_model.create_group(self.conn, "A", None, "", OP)
        b = group_model.create_group(self.conn, "B", a, "", OP)
        c = group_model.create_group(self.conn, "C", b, "", OP)
        for target in (a, b, c):
            with self.assertRaises(ValidationError):
                group_model.move_group(self.conn, a, target, OP)
        group_model.move_group(self.conn, c, None, OP)
        self.assertIsNone(group_model.get_group(self.conn, c)["parent_id"])
        sid = subnet_model.create_subnet(self.conn, b, "10.9.9.0/24", "", True, OP)["id"]
        subnet_model.move_subnet(self.conn, sid, c, OP)
        self.assertEqual(group_model.list_children(self.conn, c)["subnets"][0]["id"], sid)
        self.assertEqual(group_model.group_path(self.conn, b), "A > B")

    def test_tree_stats_and_revisions(self) -> None:
        gid, sid = self.make_subnet()
        child = group_model.create_group(self.conn, "하위", gid, "", OP)
        subnet_model.create_subnet(self.conn, child, "10.1.2.0/24", "", True, OP)
        roots = group_model.list_children(self.conn, None)["groups"]
        self.assertEqual(roots[0]["total"], 508)
        self.assertTrue(roots[0]["has_children"])
        rev = get_revisions(self.conn)
        ip_model.update_ip(self.conn, ip_to_int("10.1.2.1"), 1, {"status": STATUS_USED}, OP)
        self.assertEqual(get_revisions(self.conn)["data_rev"], rev["data_rev"] + 1)
        dash = subnet_model.group_dashboard(self.conn, gid)
        self.assertEqual(len(dash["subnets"]), 2)
        self.assertEqual(dash["stats"]["used"], 1)
        recalc_counters(self.conn)
        self.assertEqual(group_model.group_stats(self.conn, gid)["used"], 1)

    def test_search(self) -> None:
        _, sid = self.make_subnet()
        ip_model.update_ip(self.conn, ip_to_int("10.1.1.7"), 1,
                           {"status": STATUS_USED, "hostname": "WEB-PROD_01", "request_no": "REQ%9"}, OP)
        self.assertEqual(len(ip_model.search(self.conn, "10.1.1")["rows"]), 254)
        self.assertEqual(ip_model.search(self.conn, "web-prod")["rows"][0]["ip_address"], "10.1.1.7")
        self.assertEqual(len(ip_model.search(self.conn, "REQ%")["rows"]), 1)
        self.assertEqual(len(ip_model.search(self.conn, "prod_0")["rows"]), 1)
        plan = " ".join(r[3] for r in self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM ips WHERE hostname > '' AND hostname LIKE '%a%'"))
        self.assertIn("idx_ips_hostname", plan)

    def test_excel(self) -> None:
        gid, sid = self.make_subnet()
        ip_model.update_ip(self.conn, ip_to_int("10.1.1.9"), 1, {"status": STATUS_USED, "hostname": "h"}, OP)
        out = Path(self.tmp.name)
        a = excel_export.write_report(excel_export.collect_group_report(self.conn, gid, OP), out / "a.xlsx")
        b = excel_export.write_report(excel_export.collect_subnet_report(self.conn, sid, OP), out / "b.xlsx")
        from openpyxl import load_workbook
        wa = load_workbook(a)
        self.assertEqual(wa.sheetnames, ["대역별_사용현황"])
        self.assertEqual(wa.active["B5"].value, "10.1.1.0/24")
        wb = load_workbook(b)
        self.assertEqual(wb.sheetnames, ["IP_할당현황", "대역_전체이력"])
        self.assertEqual(wb["IP_할당현황"].max_row, 4 + 254)
        hist = wb["대역_전체이력"]
        self.assertEqual(hist["C5"].value, ACT_SUBNET_CREATE)
        self.assertEqual(hist["C6"].value, ACT_ALLOCATE)
        self.assertEqual(len(subnet_history(self.conn, sid, ip_to_int("10.1.1.0"), ip_to_int("10.1.1.255"))), 2)

    def test_worker(self) -> None:
        worker = DBWorker(self.db_path)
        worker.start()
        try:
            gid = worker.run_sync(group_model.create_group, "W", None, "", OP)
            self.assertEqual(worker.run_sync(group_model.get_group, gid)["name"], "W")
            with self.assertRaises(ValidationError):
                worker.run_sync(group_model.create_group, "", None, "", OP)
        finally:
            worker.stop()


class TestDialogs(unittest.TestCase):
    def test_group_dialog_starts_empty(self) -> None:
        """그룹 생성 창에 '!groupdialog' 같은 내부 위젯 이름이 들어가지 않아야 한다."""
        from ipmgr.ui.bulk_form import GroupDialog
        from ipmgr.ui.theme_mgr import ThemeManager
        root = tk.Tk()
        root.withdraw()
        try:
            theme = ThemeManager(root)
            captured: dict[str, str] = {}
            original = GroupDialog.show

            def fake_show(self: GroupDialog) -> None:
                captured["name"] = self.name_var.get()
                captured["has_desc"] = hasattr(self, "desc_var")      # 그룹 설명 입력란은 없어야 함
                self.destroy()
            GroupDialog.show = fake_show  # type: ignore[method-assign]
            try:
                GroupDialog(root, theme, "그룹 추가", "위치: 최상위").show()
            finally:
                GroupDialog.show = original  # type: ignore[method-assign]
            self.assertEqual(captured, {"name": "", "has_desc": False})
        finally:
            root.destroy()


class TestMultilineNote(BaseDB):
    """메모장처럼 여러 줄로 쓰는 비고."""

    def test_multiline_note_saved_searched_and_rendered(self) -> None:
        from ipmgr.ui.history_view import parse_diff
        _, sid = self.make_subnet()
        ip = ip_to_int("10.1.1.20")
        note = "1층 서버실 랙 A-3\n전원: UPS 2번; 이중화\n담당 변경 예정"
        ip_model.assign_ips(self.conn, {ip: 1}, {ip: "web20"}, "김", "R-1", note, OP)
        self.assertEqual(ip_model.get_ip(self.conn, ip)["note"], note)
        self.assertEqual([r["id"] for r in ip_model.search(self.conn, "UPS 2번")["rows"]], [ip])  # 둘째 줄도 검색
        diff = parse_diff(ip_timeline(self.conn, ip)[0]["diff_summary"])
        self.assertEqual(dict((label, after) for label, _b, after in diff)["비고"], note)  # '; ' 포함해도 안 깨짐

    def test_too_long_note_is_friendly_validation_error(self) -> None:
        _, sid = self.make_subnet()
        ip = ip_to_int("10.1.1.21")
        with self.assertRaises(ValidationError):
            ip_model.assign_ips(self.conn, {ip: 1}, {ip: "x"}, "김", "R-1", "가" * 5000, OP)

    def test_display_helpers(self) -> None:
        from ipmgr.ui.widgets import one_line, tsv_cell
        self.assertEqual(one_line("첫 줄\n\n 둘째 줄 "), "첫 줄 ⏎ 둘째 줄")
        self.assertTrue(one_line("가" * 300).endswith("…"))
        self.assertEqual(tsv_cell("a\nb"), '"a\nb"')
        self.assertEqual(tsv_cell('say "hi"'), '"say ""hi"""')
        self.assertEqual(tsv_cell("plain"), "plain")


class TestIntegrity(BaseDB):
    """위변조 감지와 복구."""

    def setUp(self) -> None:
        super().setUp()
        self.anchor = Path(self.tmp.name) / "anchor"
        _, self.sid = self.make_subnet()
        self.ips = [ip_to_int(f"10.1.1.{n}") for n in (10, 11, 12)]
        ip_model.assign_ips(self.conn, {i: 1 for i in self.ips}, {i: f"web{i & 255}" for i in self.ips},
                            "김철수", "REQ-1", "", OP)

    def check(self) -> integrity_model.Report:
        return integrity_model.full_check(None, self.anchor, conn=self.conn)

    def quick(self) -> integrity_model.Report:
        return integrity_model.quick_check(self.conn, self.anchor)

    def raw(self, sql: str, *params: object) -> None:
        """다른 프로그램(DB Browser 등)으로 직접 수정하는 상황."""
        self.conn.execute(sql, params)

    def test_clean_db_passes_and_anchor_advances(self) -> None:
        report = self.check()
        self.assertTrue(report.ok, report.summary())
        self.assertTrue(self.anchor.exists())
        ip_model.reclaim_ips(self.conn, {self.ips[0]: 2}, "이", "R-2", "", OP)
        quick = integrity_model.quick_check(self.conn, self.anchor)
        self.assertTrue(quick.ok)
        self.assertEqual(integrity_ig.read_anchor(self.anchor).id, quick.head[0])

    def test_detects_row_tampering_and_repairs_from_history(self) -> None:
        self.check()
        a, b, c = self.ips
        pristine = ip_to_int("10.1.1.50")
        gone = ip_to_int("10.1.1.60")
        self.raw("UPDATE ips SET hostname = 'hacked' WHERE id = ?", a)                       # 값 변조
        self.raw("UPDATE ips SET note = '몰래 메모' WHERE id = ?", pristine)                  # 해시 없는 행 변조
        self.raw("UPDATE ips SET status = '미사용', hostname = '', allocated_date = '', assignee = '', "
                 "request_no = '', note = '', version = 1, updated_at = '', updated_by = '', row_hash = NULL "
                 "WHERE id = ?", b)                                                         # 초기 상태로 위장
        self.raw("DELETE FROM ips WHERE id = ?", gone)                                       # 행 삭제
        self.raw("UPDATE subnets SET used_count = 200 WHERE id = ?", self.sid)               # 통계 변조
        for report in (self.check(), self.quick()):       # 빠른 검사(외부 수정 행만)도 전체 검사와 같은 결과
            self.assertFalse(report.ok)
            self.assertEqual(set(report.counts), {"ip", "missing", "count"})
            self.assertEqual(sorted(report.targets["ip"]), sorted([a, b, pristine]))
            self.assertTrue(report.repairable_rows)
        result = integrity_model.repair_rows(self.conn, OP, self.anchor)
        self.assertEqual(result, {"restored": 2, "pristine": 2})
        self.assertTrue(self.quick().ok)
        self.assertTrue(self.check().ok)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM dirty").fetchone()[0], 0)
        rows = ip_model.get_ips(self.conn, [a, b, pristine, gone])
        self.assertEqual((rows[a]["hostname"], rows[b]["hostname"], rows[b]["status"]), ("web10", "web11", STATUS_USED))
        self.assertEqual((rows[pristine]["note"], rows[gone]["status"]), ("", STATUS_UNUSED))
        self.assertEqual(subnet_model.get_subnet(self.conn, self.sid)["used_count"], 3)
        self.assertEqual(ip_timeline(self.conn, a)[0]["action"], "무결성 복구")

    def test_detects_history_and_structure_tampering(self) -> None:
        self.check()
        head = self.conn.execute("SELECT MAX(id) FROM history_logs").fetchone()[0]
        self.raw("UPDATE history_logs SET hostname = 'x' WHERE id = 2")
        self.raw("DELETE FROM history_logs WHERE id = 3")
        self.raw("UPDATE subnets SET description = '변조' WHERE id = ?", self.sid)
        for report in (self.check(), self.quick()):
            self.assertEqual(report.targets["chain"], [2])
            self.assertEqual(report.targets["gap"], [3])
            self.assertIn("subnet", report.counts)
            self.assertFalse(report.repairable_rows)
        with self.assertRaises(ValidationError):
            integrity_model.repair_rows(self.conn, OP, self.anchor)
        # 최근 이력을 통째로 지우면 기준값(anchor)이 잡아낸다
        self.raw("DELETE FROM history_logs WHERE id = ?", head)
        self.assertIn("anchor", integrity_model.quick_check(self.conn, self.anchor).counts)

    def test_restore_from_good_backup_and_replay(self) -> None:
        bdir = Path(self.tmp.name) / "Backup"
        report = self.check()
        good = backup_mgr.refresh_good_backup(self.db_path, bdir, report.head[0], OP, force=True)
        self.assertTrue(good.exists())
        self.assertIsNone(backup_mgr.refresh_good_backup(self.db_path, bdir, report.head[0], OP))   # 하루 1회
        late = ip_to_int("10.1.1.30")
        ip_model.assign_ips(self.conn, {late: 1}, {late: "late"}, "박", "REQ-9", "", OP)            # 백업 이후 정상 작업
        self.raw("UPDATE history_logs SET assignee = '조작' WHERE id = 2")                          # 백업 이전 이력 변조
        self.raw("UPDATE ips SET hostname = 'hacked' WHERE id = ?", self.ips[0])
        self.assertFalse(self.check().ok)
        res = integrity_model.restore_from_backup(self.conn, good, bdir, self.anchor, OP)
        self.assertEqual(res["applied"], 1)
        self.assertTrue(Path(res["tampered_copy"]).exists())
        self.assertTrue(self.check().ok)
        self.assertEqual(ip_model.get_ip(self.conn, late)["hostname"], "late")
        self.assertEqual(ip_model.get_ip(self.conn, self.ips[0])["hostname"], "web10")

    def test_upgrade_v2_db_seals_existing_data(self) -> None:
        """해시가 없던 v2 DB를 열면 기존 이력/행 전체에 해시가 부여되고 검사를 통과한다."""
        self.raw("UPDATE history_logs SET chain_hash = NULL")
        for table in ("ips", "subnets", "groups"):
            self.raw(f"UPDATE {table} SET row_hash = NULL")
        self.raw("UPDATE meta SET value = 2 WHERE key = 'schema_version'")
        self.assertFalse(self.check().ok)
        init_schema(self.conn)
        self.assertTrue(self.check().ok)
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], 4)

    def test_program_writes_leave_no_trace_but_external_edits_do(self) -> None:
        """프로그램의 쓰기는 감시 기록을 남기지 않고, 밖에서 고친 행만 기록되어 빠른 검사로 잡힌다."""
        self.check()
        dirty = lambda: self.conn.execute("SELECT kind, ref FROM dirty").fetchall()   # noqa: E731
        ip_model.reclaim_ips(self.conn, {self.ips[0]: 2}, "이", "R-2", "", OP)
        group_model.create_group(self.conn, "새 그룹", None, "", OP)
        self.assertEqual(dirty(), [])
        self.raw("UPDATE ips SET assignee = '몰래' WHERE id = ?", self.ips[1])
        self.assertEqual([tuple(r) for r in dirty()][:1], [(0, self.ips[1])])
        self.assertEqual(self.quick().targets["ip"], [self.ips[1]])
        # 값만 바꿨다가 원래대로 되돌린 경우: 해시가 맞으므로 정상 처리되고 기록은 지워진다
        self.raw("UPDATE ips SET assignee = '김철수' WHERE id = ?", self.ips[1])
        self.assertTrue(self.quick().ok)
        self.assertEqual(dirty(), [])

    def test_tampered_ip_cannot_be_saved_until_recovered(self) -> None:
        """밖에서 고친 IP를 프로그램이 저장해 변조 흔적을 덮어쓰지 못하게 막는다."""
        self.check()
        a = self.ips[0]
        self.raw("UPDATE ips SET hostname = 'hacked' WHERE id = ?", a)
        cur = ip_model.get_ip(self.conn, a)
        with self.assertRaises(ValidationError):
            ip_model.update_ip(self.conn, a, cur["version"], {**cur, "note": "덮어쓰기"}, OP)
        with self.assertRaises(ValidationError):
            ip_model.reclaim_ips(self.conn, {a: cur["version"]}, "이", "R-2", "", OP)
        self.assertEqual(self.quick().targets["ip"], [a])                 # 흔적이 그대로 남아 있음
        integrity_model.recover(self.conn, self.anchor, Path(self.tmp.name) / "Backup", OP)
        cur = ip_model.get_ip(self.conn, a)
        self.assertTrue(ip_model.update_ip(self.conn, a, cur["version"], {**cur, "note": "복구 후"}, OP)["changed"])

    def test_dropping_watch_triggers_is_detected(self) -> None:
        self.check()
        self.raw("DROP TRIGGER trg_w_ips_upd")
        self.raw("UPDATE ips SET hostname = 'quiet' WHERE id = ?", self.ips[0])
        report = self.quick()
        self.assertIn("watch", report.counts)
        self.assertFalse(report.repairable_rows)

    def test_recover_picks_method_automatically(self) -> None:
        bdir = Path(self.tmp.name) / "Backup"
        backup_mgr.refresh_good_backup(self.db_path, bdir, self.check().head[0], OP, force=True)
        # 1) 행만 변조 → 이력 기준 복구(백업 사용 안 함)
        self.raw("UPDATE ips SET hostname = 'hacked' WHERE id = ?", self.ips[0])
        res = integrity_model.recover(self.conn, self.anchor, bdir, OP)
        self.assertEqual((res["method"], res["restored"]), ("rows", 1))
        self.assertTrue(self.quick().ok)
        # 2) 감시 장치 훼손 + 이력 변조 → 정상 백업으로 원복 + 이후 정상 이력 재적용
        late = ip_to_int("10.1.1.30")
        ip_model.assign_ips(self.conn, {late: 1}, {late: "late"}, "박", "REQ-9", "", OP)
        self.raw("DROP TRIGGER trg_w_history_logs_upd")
        self.raw("UPDATE history_logs SET assignee = '조작' WHERE id = 2")
        res = integrity_model.recover(self.conn, self.anchor, bdir, OP)
        self.assertEqual(res["method"], "backup")
        self.assertTrue(self.quick().ok)
        self.assertTrue(self.check().ok)
        self.assertEqual(ip_model.get_ip(self.conn, late)["hostname"], "late")
        # 3) 정상 상태에서는 아무것도 하지 않음
        self.assertEqual(integrity_model.recover(self.conn, self.anchor, bdir, OP)["method"], "none")

    def test_recent_history_pages_newest_first(self) -> None:
        from ipmgr.models.history_model import recent_history
        total = self.conn.execute("SELECT COUNT(*) FROM history_logs").fetchone()[0]
        first = recent_history(self.conn, None, 2)
        self.assertEqual(len(first["rows"]), 2)
        self.assertTrue(first["has_more"])
        self.assertGreater(first["rows"][0]["id"], first["rows"][1]["id"])
        self.assertEqual(first["rows"][0]["nav"], self.ips[-1])           # IP 이벤트 → 그 IP로 이동
        seen = [r["id"] for r in first["rows"]]
        page = first
        while page["has_more"]:
            page = recent_history(self.conn, seen[-1], 2)
            seen += [r["id"] for r in page["rows"]]
        self.assertEqual(len(seen), total)
        self.assertEqual(len(set(seen)), total)

    def test_recovery_lock_blocks_second_user(self) -> None:
        bdir = Path(self.tmp.name) / "Backup"
        with integrity_model.recovery_lock(bdir, "홍길동(10.0.0.1)"):
            with self.assertRaises(ValidationError) as ctx:
                integrity_model.recover(self.conn, self.anchor, bdir, OP)
            self.assertIn("홍길동", str(ctx.exception))
        self.assertFalse((bdir / integrity_model.LOCK_NAME).exists())

    def test_backup_rotation_keeps_previous(self) -> None:
        bdir = Path(self.tmp.name) / "Backup"
        backup_mgr.refresh_good_backup(self.db_path, bdir, 1, OP, today=date(2026, 11, 1))
        backup_mgr.refresh_good_backup(self.db_path, bdir, 2, OP, today=date(2026, 11, 2))
        self.assertEqual(sorted(p.name for p in bdir.glob("*.db")), ["IP관리_정상백업.db", "IP관리_정상백업_이전.db"])
        self.assertEqual(backup_mgr.backup_info(bdir)["history_id"], 2)
        legacy = bdir / "IP관리_20241231.db"
        legacy.write_bytes(b"x")
        self.assertEqual(backup_mgr.purge_old_backups(bdir, 365, date(2026, 11, 2)), [legacy])


if __name__ == "__main__":
    unittest.main()
