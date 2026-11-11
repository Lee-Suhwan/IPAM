"""메인 윈도우: 반응형 레이아웃, 테마(시스템 연동), 맥락 기반 Excel Export, 화면 간 오케스트레이션.

우측 화면
- 그룹 선택 / 트리 다중 선택: 대역 사용률 표(256개씩, [더 보기])
- 대역 선택: 상단 IP 목록 + 하단 전체 이력(선택 IP 이력, 선택이 없으면 대역 전체 이력)
"""
from __future__ import annotations

import logging
import os
import queue
import re
import sqlite3
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from ipmgr.constants import HISTORY_PAGE, LEVEL_LABELS, MAX_SUBNET_DISPLAY, STATUS_USED
from ipmgr.db.worker import DBBusyError, DBConnectionError, DBWorker
from ipmgr.logic import excel_export
from ipmgr.logic.subnet_calc import natural_key, usage_level, usage_percent
from ipmgr.models import group_model, integrity_model, ip_model, subnet_model
from ipmgr.db.schema import SchemaTooNewError
from ipmgr.models.common import AppError, ConflictError
from ipmgr.ui.bulk_form import AssignDialog, GroupDialog, ReclaimDialog, SubnetDialog, SubnetEditDialog
from ipmgr.ui.crud_form import IPEditDialog
from ipmgr.ui.history_view import HistoryTimeline
from ipmgr.utils import backup_mgr, path_helper
from ipmgr.ui.search_view import SearchDialog
from ipmgr.ui.subnet_dashboard import SubnetDashboard
from ipmgr.ui.theme_mgr import Palette, ThemeManager
from ipmgr.ui.tree_view import Node, NetworkTree, node_iid
from ipmgr.ui.widgets import Tooltip, one_line, scrolled_tree, tsv_cell

log = logging.getLogger(__name__)

IP_COLUMNS = (
    ("status", "상태", 90, "w", False),
    ("ip", "IP 주소", 125, "w", False),
    ("hostname", "호스트네임", 160, "w", True),
    ("allocated", "변경일", 100, "center", False),
    ("assignee", "담당자", 100, "w", False),
    ("request_no", "요청번호", 160, "w", False),
    ("note", "비고", 200, "w", True),
)
FILTER_ALL = "전체 상태"
EXTERNAL_UPDATE_MSG = "다른 사용자가 방금 이 IP 정보를 업데이트하여 최신 정보로 갱신합니다."
MAX_BULK_SELECT = 1024    # 한 번에 등록/회수할 수 있는 최대 IP 수


def _subnet_chain(conn: sqlite3.Connection, subnet_id: int) -> list[int]:
    """서브넷의 조상 그룹 ID 체인(루트 → 소속 그룹)."""
    subnet = subnet_model.require_subnet(conn, subnet_id)
    return group_model.group_chain_ids(conn, subnet["group_id"])


def _group_chain(conn: sqlite3.Connection, group_id: int | None) -> list[int]:
    return group_model.group_chain_ids(conn, group_id)


def _group_choices(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """대역 등록 대화상자용 (그룹 ID, 경로) 목록(경로 순)."""
    paths = group_model.all_group_paths(conn)
    return sorted(paths.items(), key=lambda kv: natural_key(kv[1]))


def _safe_filename(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text).strip("_")[:80] or "export"


# ════════════════════════════════════════════════════════════
class SubnetView(ttk.Frame):
    """대역 선택 시: IP 목록(상단) + 전체 이력(하단)."""

    def __init__(self, master: tk.Misc, theme: ThemeManager, win: "MainWindow") -> None:
        super().__init__(master, style="TFrame")
        self.theme = theme
        self.win = win
        self.subnet: dict[str, Any] | None = None
        self.path = ""
        self.rows: dict[int, dict[str, Any]] = {}
        self._order: list[int] = []
        self._shown: dict[str, tuple] = {}      # 그리드에 표시 중인 값(변경 행만 다시 그리기 위함)
        px = theme.px

        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        card = ttk.Frame(self, style="Surface.TFrame", padding=(px(16), px(12)))
        card.grid(row=0, column=0, sticky="ew")
        card.grid_columnconfigure(0, weight=1)
        self.title = ttk.Label(card, text="", style="SurfaceTitle.TLabel")
        self.title.grid(row=0, column=0, sticky="w")
        self.badge = ttk.Label(card, text="", style="Safe.Badge.TLabel")
        self.badge.grid(row=0, column=1, sticky="e", padx=(px(8), px(8)))
        actions = ttk.Frame(card, style="Surface.TFrame")
        actions.grid(row=0, column=2, sticky="e")
        ttk.Button(actions, text="✔ IP 등록", style="Accent.TButton", width=0,
                   command=win.assign_selected).pack(side="left")
        ttk.Button(actions, text="↩ IP 회수", width=0, command=win.reclaim_selected).pack(side="left", padx=(px(6), 0))
        ttk.Button(actions, text="✎ 대역 수정", width=0, command=win.edit_node).pack(side="left", padx=(px(6), 0))
        ttk.Button(actions, text="📊 Excel 내보내기", width=0, command=win.export_excel).pack(side="left",
                                                                                         padx=(px(6), 0))
        self.subtitle = ttk.Label(card, text="", style="SurfaceMuted.TLabel")
        self.subtitle.grid(row=1, column=0, columnspan=3, sticky="w", pady=(px(2), px(6)))
        self.progress = ttk.Progressbar(card, maximum=100, style="Safe.Horizontal.TProgressbar")
        self.progress.grid(row=2, column=0, columnspan=3, sticky="ew")

        bar = ttk.Frame(self, padding=(px(16), px(8), px(16), px(6)))
        bar.grid(row=1, column=0, sticky="ew")
        bar.grid_columnconfigure(3, weight=1)
        ttk.Label(bar, text="필터").grid(row=0, column=0, sticky="w", padx=(0, px(6)))
        self.filter_var = tk.StringVar(self)
        entry = ttk.Entry(bar, textvariable=self.filter_var, width=28, font=theme.font)
        entry.grid(row=0, column=1, sticky="w")
        self.status_filter = tk.StringVar(self, value=FILTER_ALL)
        combo = ttk.Combobox(bar, textvariable=self.status_filter, state="readonly", width=10,
                             values=(FILTER_ALL, "사용중", "미사용"), font=theme.font)
        theme.track_combobox(combo)
        combo.grid(row=0, column=2, sticky="w", padx=(px(8), 0))
        self.count_label = ttk.Label(bar, text="", style="Muted.TLabel")
        self.count_label.grid(row=0, column=3, sticky="e")
        self.filter_var.trace_add("write", lambda *_: self._render(preserve=True))
        self.status_filter.trace_add("write", lambda *_: self._render(preserve=True))

        paned = ttk.PanedWindow(self, orient="vertical")
        paned.grid(row=2, column=0, sticky="nsew")
        grid_frame, self.grid_tree = scrolled_tree(paned, theme, IP_COLUMNS)
        paned.add(grid_frame, weight=3)
        self.history = HistoryTimeline(paned, theme, on_order_change=lambda _n: win.load_history(reset=True),
                                       on_more=win.more_history)
        paned.add(self.history, weight=2)

        g = self.grid_tree
        g.bind("<<TreeviewSelect>>", lambda e: win.on_ip_selection())
        g.bind("<Double-Button-1>", self._on_double_click)
        g.bind("<Return>", lambda e: win.open_action_for_selection())
        g.bind("<F2>", lambda e: win.edit_selected_ip())
        g.bind("<Button-3>", self._on_right_click)
        g.bind("<Control-a>", self._select_all)
        g.bind("<Control-c>", lambda e: (win.copy_selected_rows(), "break")[1])
        g.bind("<Control-C>", lambda e: (win.copy_selected_rows(), "break")[1])
        # 여러 줄이거나 잘린 비고는 마우스를 올리면 전체 내용을 툴팁으로 보여 준다
        self._tip = Tooltip(self, theme)
        self._tip_key: tuple[str, str] | None = None
        g.bind("<Motion>", self._on_motion)
        g.bind("<Leave>", lambda e: self._hide_tip())
        theme.register(self._apply_theme)

    def _hide_tip(self) -> None:
        self._tip_key = None
        self._tip.hide()

    def _on_motion(self, event: tk.Event) -> None:
        g = self.grid_tree
        iid, col = g.identify_row(event.y), g.identify_column(event.x)
        note = self.rows.get(int(iid), {}).get("note", "") if iid and col == "#7" else ""
        if not note or (note == one_line(note) and self.theme.font.measure(note) < g.column("note", "width") - 12):
            self._hide_tip()
            return
        if self._tip_key != (iid, col):
            self._tip_key = (iid, col)
            lines = note.splitlines()
            text = "\n".join(lines[:25]) + (f"\n… 외 {len(lines) - 25}줄" if len(lines) > 25 else "")
            self._tip.show(event.x_root, event.y_root, text)

    def _apply_theme(self, p: Palette) -> None:
        self.grid_tree.tag_configure("used", foreground=p["used"], background=p["used_row"])
        self.grid_tree.tag_configure("unused", foreground=p["text"])
        if self.subnet is not None:
            self._update_header()

    # ── 데이터 ────────────────────────────────────────────
    def load(self, detail: dict[str, Any], preserve: bool) -> None:
        """subnet_detail() 결과 바인딩."""
        same = self.subnet is not None and self.subnet["id"] == detail["subnet"]["id"]
        self.subnet = detail["subnet"]
        self.path = detail["path"]
        self.rows = {r["id"]: r for r in detail["ips"]}
        self._order = [r["id"] for r in detail["ips"]]
        if not same:
            self.filter_var.set("")
            self.status_filter.set(FILTER_ALL)
        self._update_header()
        self._render(preserve=preserve and same)

    def _update_header(self) -> None:
        s = self.subnet
        assert s is not None
        pct = usage_percent(s["used_count"], s["total_count"])
        level = usage_level(pct)
        self.title.configure(text=f"🌐  {s['cidr']}")
        self.subtitle.configure(text=f"{self.path}" + (f"   ·   {s['description']}" if s["description"] else "") +
                                f"   ·   전체 {s['total_count']:,}  ·  사용중 {s['used_count']:,}  ·  "
                                f"잔여 {s['total_count'] - s['used_count']:,}")
        self.badge.configure(text=f"{LEVEL_LABELS[level]} {pct:.1f}%", style=f"{level.title()}.Badge.TLabel")
        self.progress.configure(value=pct, style=f"{level.title()}.Horizontal.TProgressbar")

    def _visible_ids(self) -> list[int]:
        text = self.filter_var.get().strip().lower()
        status = self.status_filter.get()
        result = []
        for ip_id in self._order:
            r = self.rows[ip_id]
            if status != FILTER_ALL and r["status"] != status:
                continue
            if text and not any(text in str(r[k]).lower() for k in
                                ("ip_address", "hostname", "assignee", "request_no", "note")):
                continue
            result.append(ip_id)
        return result

    @staticmethod
    def _values(r: dict[str, Any]) -> tuple:
        used = r["status"] == STATUS_USED
        return ("● 사용중" if used else "○ 미사용", r["ip_address"], r["hostname"], r["allocated_date"],
                r["assignee"], r["request_no"], one_line(r["note"]))

    def _render(self, preserve: bool) -> None:
        t = self.grid_tree
        visible = [str(i) for i in self._visible_ids()]
        current = list(t.get_children())
        if preserve and current == visible:
            for iid in visible:            # 같은 행 구성 → 바뀐 행만 갱신(선택/스크롤 유지, /16도 가볍게)
                r = self.rows[int(iid)]
                values = self._values(r)
                if self._shown.get(iid) != values:
                    t.item(iid, values=values, tags=("used" if r["status"] == STATUS_USED else "unused",))
                    self._shown[iid] = values
        else:
            selected = t.selection() if preserve else ()
            yview = t.yview()[0] if preserve else 0.0
            t.delete(*current)
            self._shown = {}
            for iid in visible:
                r = self.rows[int(iid)]
                values = self._values(r)
                t.insert("", "end", iid=iid, values=values,
                         tags=("used" if r["status"] == STATUS_USED else "unused",))
                self._shown[iid] = values
            keep = [i for i in selected if t.exists(i)]
            if keep:
                t.selection_set(keep)
            t.yview_moveto(yview)
        self.count_label.configure(text=f"표시 {len(visible):,} / 전체 {len(self.rows):,}   ·   Esc: 선택 해제")

    def update_row(self, row: dict[str, Any]) -> None:
        """단일 행 최신값 반영."""
        self.rows[row["id"]] = row
        iid = str(row["id"])
        if self.grid_tree.exists(iid):
            values = self._values(row)
            self.grid_tree.item(iid, values=values, tags=("used" if row["status"] == STATUS_USED else "unused",))
            self._shown[iid] = values

    def selected_ids(self) -> list[int]:
        """선택된 IP ID 목록."""
        return [int(i) for i in self.grid_tree.selection()]

    def clear_selection(self) -> bool:
        """IP 선택 해제(해제할 것이 있었으면 True)."""
        if self.grid_tree.selection():
            self.grid_tree.selection_set(())
            return True
        return False

    def select_ip(self, ip_id: int) -> None:
        """IP 행 선택(필터에 가려져 있으면 필터 해제)."""
        iid = str(ip_id)
        if not self.grid_tree.exists(iid) and ip_id in self.rows:
            self.filter_var.set("")
            self.status_filter.set(FILTER_ALL)
        if self.grid_tree.exists(iid):
            self.grid_tree.selection_set(iid)
            self.grid_tree.focus(iid)
            self.grid_tree.see(iid)
            self.grid_tree.focus_set()

    def _select_all(self, _event: tk.Event) -> str:
        self.grid_tree.selection_set(self.grid_tree.get_children())
        return "break"

    def _on_double_click(self, event: tk.Event) -> str | None:
        """더블클릭: 미사용 → IP 등록, 사용중 → IP 회수."""
        if self.grid_tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            return None
        iid = self.grid_tree.identify_row(event.y)
        if iid:
            self.grid_tree.selection_set(iid)
            self.win.open_action_for_selection([int(iid)])
        return "break"

    def _on_right_click(self, event: tk.Event) -> None:
        iid = self.grid_tree.identify_row(event.y)
        if iid and iid not in self.grid_tree.selection():
            self.grid_tree.selection_set(iid)
        ids = self.selected_ids()
        unused = sum(1 for i in ids if self.rows.get(i, {}).get("status") != STATUS_USED)
        used = len(ids) - unused
        menu = tk.Menu(self, tearoff=False)
        self.theme.style_menu(menu)
        menu.add_command(label=f"✔ IP 등록 (미사용 {unused}개)…", command=self.win.assign_selected,
                         state="normal" if unused else "disabled")
        menu.add_command(label=f"↩ IP 회수 (사용중 {used}개)…", command=self.win.reclaim_selected,
                         state="normal" if used else "disabled")
        menu.add_command(label="✎ 정보 수정…   F2", command=self.win.edit_selected_ip,
                         state="normal" if len(ids) == 1 else "disabled")
        menu.add_separator()
        menu.add_command(label="행 복사 (엑셀 붙여넣기용)   Ctrl+C", command=self.win.copy_selected_rows,
                         state="normal" if ids else "disabled")
        menu.add_command(label="IP 주소만 복사", command=self.win.copy_selected_ips,
                         state="normal" if ids else "disabled")
        menu.add_separator()
        menu.add_command(label="선택 해제   Esc", command=self.clear_selection, state="normal" if ids else "disabled")
        menu.add_command(label="새로고침   F5", command=self.win.refresh_all)
        menu.tk_popup(event.x_root, event.y_root)


# ════════════════════════════════════════════════════════════
class MainWindow:
    """메인 화면 구성과 사용자 동작 처리."""

    def __init__(self, root: tk.Tk, theme: ThemeManager, worker: DBWorker, operator: str, db_path: Path,
                 request_poll: Callable[[], None]) -> None:
        self.root = root
        self.theme = theme
        self.worker = worker
        self.operator = operator
        self.db_path = db_path
        self.request_poll = request_poll
        self.view_kind: str | None = None          # "group" | "multi" | "subnet" | None
        self.view_id: int | None = None
        self.view_nodes: list[Node] = []
        self.revs: dict[str, int] | None = None
        self._view_seq = 0
        self._hist_seq = 0
        self._hist_limit = HISTORY_PAGE
        self._hist_key: tuple | None = None
        self._pending_ip: int | None = None
        self._ip_select_job: str | None = None
        self._bg_results: queue.Queue[tuple[Callable[[Any], None], Any]] = queue.Queue()
        self._last_conn_error = 0.0
        self._closing_for_upgrade = False
        self.integrity_report: integrity_model.Report | None = None
        self._integrity_running = False
        self._integrity_alerted = False
        self._quick_inflight = False
        self._recovering = False
        self.auto_backup = True
        self.anchor_path = path_helper.get_anchor_path()
        self.backup_dir = path_helper.get_backup_dir()
        self.worker.default_error_handler = self.show_error
        self._build()

    # ── 레이아웃 ──────────────────────────────────────────
    def _build(self) -> None:
        px = self.theme.px
        root = self.root
        root.grid_rowconfigure(1, weight=1)
        root.grid_columnconfigure(0, weight=1)

        toolbar = ttk.Frame(root, style="Toolbar.TFrame", padding=(px(8), px(8)))
        toolbar.grid(row=0, column=0, sticky="ew")
        buttons = (("＋ 그룹", self.add_group), ("＋ 대역", self.add_subnet), None,
                   ("✔ IP 등록", self.assign_selected), ("↩ IP 회수", self.reclaim_selected), None,
                   ("✎ 수정", self.edit_action), ("🗑 삭제", self.delete_node), None,
                   ("🔍 검색 (F3)", self.open_search), ("📊 Excel 내보내기", self.export_excel),
                   ("⟳ 새로고침 (F5)", self.refresh_all))
        for spec in buttons:
            if spec is None:
                ttk.Frame(toolbar, style="Divider.TFrame", width=1).pack(side="left", fill="y", padx=px(8),
                                                                        pady=px(4))
                continue
            # width=0: 글자 길이에 맞춘 자연 폭 → 최소 창 크기에서도 툴바가 넘치지 않음
            ttk.Button(toolbar, text=spec[0], style="Toolbar.TButton", width=0, command=spec[1]).pack(side="left")
        self.theme_btn = ttk.Button(toolbar, style="Toolbar.TButton", width=0, command=self._theme_menu)
        self.theme_btn.pack(side="right")
        ttk.Frame(root, style="Divider.TFrame", height=1).grid(row=0, column=0, sticky="sew")

        paned = ttk.PanedWindow(root, orient="horizontal")
        paned.grid(row=1, column=0, sticky="nsew")
        self.tree = NetworkTree(paned, self.theme, self.worker, on_select=self.on_tree_select,
                                on_move=self.on_tree_move, on_context=self.on_tree_context,
                                on_error=self.show_error)
        paned.add(self.tree, weight=1)
        self.right = ttk.Frame(paned)
        self.right.grid_rowconfigure(0, weight=1)
        self.right.grid_columnconfigure(0, weight=1)
        paned.add(self.right, weight=4)

        self.empty_view = ttk.Frame(self.right, style="Surface.TFrame")
        self.empty_view.grid_rowconfigure(0, weight=1)
        self.empty_view.grid_columnconfigure(0, weight=1)
        inner = ttk.Frame(self.empty_view, style="Surface.TFrame")
        inner.grid(row=0, column=0)
        ttk.Label(inner, text="좌측 트리에서 그룹 또는 대역을 선택하세요", style="SurfaceTitle.TLabel").pack(
            pady=(0, px(4)))
        self.empty_hint = ttk.Label(inner, text="그룹 선택 → 대역 사용률   ·   대역 선택 → IP 목록 / 등록 / 회수 / 이력"
                                               "   ·   Ctrl/Shift로 여러 개 선택", style="SurfaceMuted.TLabel")
        self.empty_hint.pack()
        starters = ttk.Frame(inner, style="Surface.TFrame")
        starters.pack(pady=(px(16), 0))
        ttk.Button(starters, text="＋ 대역 추가", style="Accent.TButton", width=0,
                   command=lambda: self.add_subnet(top_level=True)).pack(side="left")
        ttk.Button(starters, text="＋ 그룹 만들기", width=0,
                   command=lambda: self.add_group(top_level=True)).pack(side="left", padx=(px(8), 0))
        self.dashboard = SubnetDashboard(self.right, self.theme, on_open_subnet=self.open_subnet,
                                         on_export=self.export_excel, on_more=self.more_dashboard)
        self.subnet_view = SubnetView(self.right, self.theme, self)
        for view in (self.empty_view, self.dashboard, self.subnet_view):
            view.grid(row=0, column=0, sticky="nsew")
        self.empty_view.tkraise()

        ttk.Frame(root, style="Divider.TFrame", height=1).grid(row=2, column=0, sticky="new")
        status = ttk.Frame(root, style="Status.TFrame", padding=(px(12), px(4)))
        status.grid(row=2, column=0, sticky="ew")
        status.grid_columnconfigure(0, weight=1)
        self.status_msg = ttk.Label(status, text="준비", style="Status.TLabel")
        self.status_msg.grid(row=0, column=0, sticky="w")
        self.busy_label = ttk.Label(status, text="", style="Status.TLabel")
        self.busy_label.grid(row=0, column=1, sticky="e", padx=(0, px(16)))
        self.integrity_label = ttk.Label(status, text="🛡 무결성 확인 전", style="Status.TLabel", cursor="hand2")
        self.integrity_label.grid(row=0, column=2, sticky="e", padx=(0, px(16)))
        self.integrity_label.bind("<Button-1>", lambda e: self.integrity_clicked())
        self.conn_label = ttk.Label(status, text="", style="Status.TLabel")
        self.conn_label.grid(row=0, column=3, sticky="e", padx=(0, px(16)))
        db_label = f"…\\{self.db_path.parent.name}\\{self.db_path.name}"
        ttk.Label(status, text=f"👤 {self.operator}   ·   DB: {db_label}",
                  style="Status.TLabel").grid(row=0, column=4, sticky="e")
        self.theme.register(self._apply_theme)

    def _apply_theme(self, p: Palette) -> None:
        pref = self.theme.preference
        mode = "다크" if self.theme.is_dark else "라이트"
        self.theme_btn.configure(text=f"🖥 테마: 시스템({mode}) ▾" if pref == "system" else
                                 f"{'🌙' if self.theme.is_dark else '☀'} 테마: {mode} ▾")

    # ── 공용 ─────────────────────────────────────────────
    def set_status(self, text: str) -> None:
        """상태 표시줄 메시지."""
        self.status_msg.configure(text=f"{datetime.now():%H:%M:%S}  {text}")

    def set_busy(self, busy: bool) -> None:
        """작업 중 표시."""
        self.busy_label.configure(text="⏳ 처리 중…" if busy else "")
        self.root.configure(cursor="watch" if busy else "")

    def set_connection(self, ok: bool, detail: str = "") -> None:
        """DB 연결 상태 표시."""
        if ok:
            self.conn_label.configure(text=f"● 동기화 {datetime.now():%H:%M:%S}",
                                      foreground=self.theme.palette["safe"])
        else:
            self.conn_label.configure(text=f"● DB 연결 끊김 — 재시도 중 {detail}",
                                      foreground=self.theme.palette["critical"])

    def show_error(self, exc: BaseException) -> None:
        """Worker 오류를 사용자에게 안내한다."""
        self.set_busy(False)
        if isinstance(exc, SchemaTooNewError):
            self._schema_too_new(exc)
        elif isinstance(exc, ConflictError):
            messagebox.showwarning("충돌 감지", str(exc), parent=self.root)
            self.refresh_all()
        elif isinstance(exc, AppError):
            messagebox.showwarning("알림", str(exc), parent=self.root)
        elif isinstance(exc, DBBusyError):
            messagebox.showwarning("DB 사용 중", str(exc), parent=self.root)
        elif isinstance(exc, DBConnectionError):
            self.set_connection(False)
            if time.monotonic() - self._last_conn_error > 30:
                self._last_conn_error = time.monotonic()
                messagebox.showerror("DB 연결 오류", str(exc), parent=self.root)
        else:
            log.error("처리 오류", exc_info=exc)
            messagebox.showerror("오류", f"작업 중 오류가 발생했습니다.\n{type(exc).__name__}: {exc}",
                                 parent=self.root)

    def run_background(self, fn: Callable[[], Any], on_done: Callable[[Any], None],
                       on_error: Callable[[BaseException], None]) -> None:
        """DB와 무관한 무거운 작업(Excel 파일 생성)을 별도 스레드에서 실행한다."""
        def job() -> None:
            try:
                self._bg_results.put((on_done, fn()))
            except BaseException as exc:  # noqa: BLE001
                self._bg_results.put((on_error, exc))
        threading.Thread(target=job, name="bg", daemon=True).start()

    def drain_background(self) -> None:
        """백그라운드 작업 결과를 UI 스레드에서 처리."""
        while True:
            try:
                callback, value = self._bg_results.get_nowait()
            except queue.Empty:
                return
            callback(value)

    def escape(self) -> None:
        """Esc: IP 선택 해제 → 하단 이력이 대역 전체 이력으로 바뀐다."""
        if self.view_kind == "subnet" and self.subnet_view.clear_selection():
            self.set_status("선택 해제 — 대역 전체 이력 표시")

    # ── 폴링 / 갱신 ──────────────────────────────────────
    def on_revisions(self, revs: dict[str, int]) -> None:
        """폴링 결과: 다른 사용자(또는 내) 변경이 있으면 트리/현재 화면을 갱신."""
        self.set_connection(True)
        if self.revs is None:
            self.revs = revs
            return
        if revs != self.revs:
            changed = {k: v for k, v in revs.items() if k != "dirty"} != {
                k: v for k, v in self.revs.items() if k != "dirty"}
            self.revs = revs
            if changed:
                self.tree.refresh()
                self.reload_view()
            self.run_quick_check()
        elif revs.get("dirty") and not self._integrity_alerted:
            self.run_quick_check()          # 프로그램 밖 수정 흔적 → 검증(이미 경고 중이면 생략)

    def refresh_all(self) -> None:
        """수동 새로고침(F5)."""
        self.tree.refresh()
        self.reload_view()
        self.set_status("새로고침 완료")

    def reload_view(self) -> None:
        """현재 우측 화면을 같은 상태(선택·스크롤·불러온 개수) 그대로 다시 조회."""
        if self.view_kind in ("group", "multi"):
            self._load_dashboard(append=False, limit=max(MAX_SUBNET_DISPLAY, self.dashboard.loaded), silent=True)
        elif self.view_kind == "subnet" and self.view_id is not None:
            self._load_subnet(self.view_id, preserve=True)

    # ── 트리 이벤트 ──────────────────────────────────────
    def on_tree_select(self, nodes: list[Node]) -> None:
        """트리 선택에 따라 우측 화면 모드 전환(다중 선택이면 선택 항목 요약)."""
        self.view_nodes = nodes
        if not nodes:
            self.view_kind = self.view_id = None
            self.empty_view.tkraise()
            return
        if len(nodes) == 1:
            kind, node_id = nodes[0]
            if (kind, node_id) == (self.view_kind, self.view_id):
                return
            self.view_kind, self.view_id = kind, node_id
        else:
            self.view_kind, self.view_id = "multi", None
        if self.view_kind == "subnet":
            self.subnet_view.history.clear()
            self._hist_key = None
            self._load_subnet(self.view_id, preserve=False)    # type: ignore[arg-type]
        else:
            self._load_dashboard(append=False, limit=MAX_SUBNET_DISPLAY, silent=False)

    def _scope(self) -> tuple[list[int], list[int]]:
        groups = [n for k, n in self.view_nodes if k == "group"]
        subnets = [n for k, n in self.view_nodes if k == "subnet"]
        return groups, subnets

    def _load_dashboard(self, append: bool, limit: int, silent: bool) -> None:
        self._view_seq += 1
        seq = self._view_seq
        kind = self.view_kind
        offset = self.dashboard.loaded if append else 0

        def ok(data: dict[str, Any]) -> None:
            if seq != self._view_seq or self.view_kind != kind:
                return
            if kind == "group":
                data["title"] = f"📁  {data['path']}"
                data["subtitle"] = f"하위 그룹 {data['stats']['child_groups']:,}개"
            else:
                names = [self.tree.label_of(k, n) for k, n in self.view_nodes]
                data["path"] = f"선택 항목 {len(names)}개"
                data["title"] = f"선택한 항목 {len(names):,}개"
                data["subtitle"] = ", ".join(names[:6]) + (f" 외 {len(names) - 6}개" if len(names) > 6 else "")
            self.dashboard.load(data, append=append)
            self.dashboard.tkraise()
            if not silent:
                self.set_status(f"{data['title'].strip('📁 ')} — 대역 {data['stats']['subnet_count']:,}개")

        if kind == "group":
            self.worker.submit(subnet_model.group_dashboard, self.view_id, offset, limit, on_success=ok,
                               on_error=self._view_error, silent=silent)
        else:
            groups, subnets = self._scope()
            self.worker.submit(subnet_model.scope_page, groups, subnets, offset, limit, on_success=ok,
                               on_error=self._view_error, silent=silent)

    def more_dashboard(self, all_rows: bool) -> None:
        """[더 보기] 다음 256개 / [모두 보기] 나머지 전부."""
        data = self.dashboard._data
        if not data:
            return
        remaining = data["stats"]["subnet_count"] - self.dashboard.loaded
        if all_rows and remaining > 5000 and not messagebox.askyesno(
                "모두 보기", f"남은 대역 {remaining:,}개를 모두 불러옵니다.\n화면에 표시하는 데 몇 초 걸릴 수 있습니다. "
                            "계속할까요?", parent=self.root):
            return
        self._load_dashboard(append=True, limit=remaining if all_rows else MAX_SUBNET_DISPLAY, silent=False)

    def _load_subnet(self, subnet_id: int, preserve: bool) -> None:
        self._view_seq += 1
        seq = self._view_seq

        def ok(detail: dict[str, Any]) -> None:
            if seq != self._view_seq or self.view_id != subnet_id:
                return
            view = self.subnet_view
            view.load(detail, preserve=preserve)
            view.tkraise()
            if self._pending_ip is not None:
                ip_id, self._pending_ip = self._pending_ip, None
                view.select_ip(ip_id)         # 선택 이벤트가 이력을 불러온다
            else:
                self.load_history(reset=not preserve)
            if not preserve:
                self.set_status(f"대역 {detail['subnet']['cidr']} — IP {len(detail['ips']):,}개")

        self.worker.submit(ip_model.subnet_detail, subnet_id, on_success=ok, on_error=self._view_error,
                           silent=preserve)

    def _view_error(self, exc: BaseException) -> None:
        if isinstance(exc, AppError):        # 다른 사용자가 삭제한 노드
            self.view_kind = self.view_id = None
            self.empty_view.tkraise()
            self.set_status(str(exc))
            self.tree.refresh()
        else:
            self.show_error(exc)

    def open_subnet(self, subnet_id: int, focus_ip: int | None = None) -> None:
        """대시보드/검색에서 서브넷 진입: 트리 경로를 펼치고 선택한다."""
        self._pending_ip = focus_ip

        def ok(chain: list[int]) -> None:
            self.tree.reveal(chain, f"s{subnet_id}")
            if self.view_kind == "subnet" and self.view_id == subnet_id and focus_ip is not None:
                self._pending_ip = None
                self.subnet_view.select_ip(focus_ip)

        self.worker.submit(_subnet_chain, subnet_id, on_success=ok, on_error=self.show_error)

    def on_tree_move(self, nodes: list[Node], new_parent: int | None, src: str, dst: str,
                     ref: Node | None = None, after: bool = False, reorder_only: bool = False) -> None:
        """Drag & Drop: 같은 위치 안이면 순서만 바꾸고(확인 없음), 다른 그룹으로 가면 확인 후 이동한다."""
        if not reorder_only and not messagebox.askyesno(
                "이동 확인", f"'{src}'\n→ '{dst}'(으)로 이동하시겠습니까?", parent=self.root):
            return

        def ok(_count: int) -> None:
            self.set_status(f"'{src}' 순서 변경 완료" if reorder_only else f"'{src}' 이동 완료 → {dst}")
            first = node_iid(*nodes[0])
            self.worker.submit(_group_chain, new_parent, on_success=lambda ch: self.tree.reveal(ch, first),
                               on_error=self.show_error, silent=True)
            self.request_poll()

        self.worker.submit(subnet_model.place_nodes, nodes, new_parent, ref, after, self.operator, on_success=ok,
                           on_error=self.show_error)

    def on_tree_context(self, event: tk.Event, nodes: list[Node]) -> None:
        """트리 우클릭 메뉴."""
        menu = tk.Menu(self.root, tearoff=False)
        self.theme.style_menu(menu)
        kind = nodes[0][0] if len(nodes) == 1 else None
        if len(nodes) > 1:
            menu.add_command(label=f"선택 항목 {len(nodes)}개 삭제…", command=self.delete_node)
            menu.add_separator()
            menu.add_command(label="Excel 내보내기 (선택 항목 사용현황)", command=self.export_excel)
        elif kind == "group":
            menu.add_command(label="하위 그룹 추가…", command=self.add_group)
            menu.add_command(label="대역(서브넷) 추가…", command=self.add_subnet)
            menu.add_separator()
            menu.add_command(label="그룹 수정…", command=self.edit_node)
            menu.add_command(label="그룹 삭제…", command=self.delete_node)
            menu.add_separator()
            menu.add_command(label="Excel 내보내기 (대역별 사용현황)", command=self.export_excel)
        elif kind == "subnet":
            menu.add_command(label="대역 수정…", command=self.edit_node)
            menu.add_command(label="대역 삭제…", command=self.delete_node)
            menu.add_separator()
            menu.add_command(label="Excel 내보내기 (IP 현황 + 전체 이력)", command=self.export_excel)
        else:
            menu.add_command(label="최상위 그룹 추가…", command=lambda: self.add_group(top_level=True))
            menu.add_command(label="대역 추가 (그룹 없이)…", command=lambda: self.add_subnet(top_level=True))
        menu.add_separator()
        menu.add_command(label="새로고침   F5", command=self.refresh_all)
        menu.tk_popup(event.x_root, event.y_root)

    # ── IP 선택 / 이력 ───────────────────────────────────
    def on_ip_selection(self) -> None:
        """그리드 선택 변경 → (디바운스) 하단 이력 갱신."""
        if self._ip_select_job:
            self.root.after_cancel(self._ip_select_job)
        self._ip_select_job = self.root.after(150, lambda: self.load_history(reset=True))

    def load_history(self, reset: bool = False) -> None:
        """하단 전체 이력: 선택 IP들의 이력, 선택이 없으면 대역 전체 이력."""
        self._ip_select_job = None
        view = self.subnet_view
        if self.view_kind != "subnet" or view.subnet is None:
            return
        ids = view.selected_ids()
        key = (view.subnet["id"], tuple(ids), view.history.newest_first)
        if reset or key != self._hist_key:
            self._hist_limit = HISTORY_PAGE
        else:
            view.history.keep_scroll_once()
        self._hist_key = key
        if not ids:
            scope = f"대역 {view.subnet['cidr']} 전체"
        elif len(ids) == 1:
            scope = view.rows[ids[0]]["ip_address"] if ids[0] in view.rows else "선택 IP"
        else:
            scope = f"선택 IP {len(ids):,}개"
        view.history.show_loading(scope)
        self._hist_seq += 1
        seq = self._hist_seq

        def ok(page: dict[str, Any]) -> None:
            if seq != self._hist_seq:
                return
            view.history.render(page["rows"], scope, page["has_more"], show_ip=len(ids) != 1)

        self.worker.submit(ip_model.history_for, view.subnet["id"], ids or None, view.history.newest_first,
                           self._hist_limit, on_success=ok, on_error=self.show_error, silent=True)

    def more_history(self) -> None:
        """이력 [더 보기]."""
        self._hist_limit += HISTORY_PAGE
        self.subnet_view.history.keep_scroll_once()
        self.load_history()

    # ── IP 정보 수정 (Pre-edit Fetch → 낙관적 잠금 저장) ──
    def edit_action(self) -> None:
        """툴바 [✎ 수정]: IP 하나가 선택돼 있으면 IP 정보, 아니면 트리의 그룹/대역."""
        if self.view_kind == "subnet" and len(self.subnet_view.selected_ids()) == 1:
            self.edit_selected_ip()
        else:
            self.edit_node()

    def edit_selected_ip(self) -> None:
        """선택한 IP 1개의 정보 수정."""
        ids = self.subnet_view.selected_ids()
        if len(ids) != 1:
            messagebox.showinfo("정보 수정", "수정할 IP를 하나만 선택하세요.", parent=self.root)
            return
        ip_id = ids[0]
        view = self.subnet_view
        cached = view.rows.get(ip_id)

        def fetched(rec: dict[str, Any] | None) -> None:
            if rec is None:
                messagebox.showwarning("정보 수정", "다른 사용자가 이 IP를 삭제했습니다. 화면을 새로고침합니다.",
                                       parent=self.root)
                self.refresh_all()
                return
            notice = ""
            if cached is not None and cached["version"] != rec["version"]:
                notice = EXTERNAL_UPDATE_MSG
                view.update_row(rec)
                self.request_poll()
            result = IPEditDialog(self.root, self.theme, rec, notice).show()
            if not result:
                return

            def saved(res: dict[str, Any]) -> None:
                if res["changed"]:
                    view.update_row(res["ip"])
                    self.set_status(f"{res['ip']['ip_address']} 저장 완료 ({res['action']})")
                    self.load_history()
                    self.request_poll()

            def failed(exc: BaseException) -> None:
                if isinstance(exc, ConflictError):
                    self._show_conflict(exc)
                else:
                    self.show_error(exc)

            self.worker.submit(ip_model.update_ip, ip_id, rec["version"], result, self.operator,
                               on_success=saved, on_error=failed)

        self.worker.submit(ip_model.get_ip, ip_id, on_success=fetched, on_error=self.show_error)

    def _show_conflict(self, exc: ConflictError) -> None:
        if exc.deleted or exc.latest is None:
            messagebox.showwarning("충돌 감지", "[충돌 감지] 다른 사용자가 이 IP를 삭제했습니다.\n"
                                            "화면을 새로고침하여 최신 변경사항을 먼저 반영합니다.", parent=self.root)
        else:
            latest = exc.latest
            messagebox.showwarning(
                "충돌 감지",
                f"[충돌 감지] 다른 사용자가 먼저 수정했습니다.\n"
                f"- 최근 작업자: {latest['updated_by'] or '-'}\n"
                f"- 현재 DB 상태: {latest['status']} (담당자: {latest['assignee'] or '-'}, "
                f"호스트: {latest['hostname'] or '-'})\n"
                f"화면을 새로고침하여 최신 변경사항을 먼저 반영합니다.", parent=self.root)
            self.subnet_view.update_row(latest)
        self.reload_view()
        self.request_poll()

    # ── 그룹/대역 ────────────────────────────────────────
    def _context_group(self) -> tuple[int | None, str]:
        """새 항목을 만들 그룹(선택된 그룹 또는 선택된 대역의 그룹)."""
        kind, node_id = self.tree.selected()
        if kind == "group" and node_id is not None:
            return node_id, self.tree.label_of(kind, node_id)
        if kind == "subnet" and node_id is not None:
            gid = self.tree.parent_group_of(node_iid(kind, node_id))
            if gid is not None:
                return gid, self.tree.label_of("group", gid)
        return None, ""

    def add_group(self, top_level: bool = False) -> None:
        """그룹 추가(선택 그룹 하위 또는 최상위)."""
        parent_id, parent_name = (None, "") if top_level else self._context_group()
        location = f"위치: {parent_name} 하위" if parent_id is not None else "위치: 최상위"
        result = GroupDialog(self.root, self.theme, "그룹 추가", location).show()
        if not result:
            return

        def ok(gid: int) -> None:
            self.set_status(f"그룹 '{result['name']}' 생성")
            self.worker.submit(_group_chain, gid, on_success=lambda ch: self.tree.reveal(ch, f"g{gid}"),
                               on_error=self.show_error, silent=True)
            self.request_poll()

        self.worker.submit(group_model.create_group, result["name"], parent_id, "",
                           self.operator, on_success=ok, on_error=self.show_error)

    def add_subnet(self, top_level: bool = False) -> None:
        """대역(서브넷) 생성 – 단일 또는 분할 일괄. 그룹은 선택 사항(없으면 최상위)."""
        default_group = None if top_level else self._context_group()[0]

        def got_groups(groups: list[tuple[int, str]]) -> None:
            result = SubnetDialog(self.root, self.theme, groups, default_group).show()
            if not result:
                return
            group_id = result["group_id"]

            def ok(res: dict[str, Any]) -> None:
                if result["mode"] == "split":
                    self.set_status(f"대역 {res['count']:,}개 생성 (IP {res['ip_count']:,}개)")
                    if group_id is not None:
                        self.worker.submit(_group_chain, group_id,
                                           on_success=lambda ch: self.tree.reveal(ch, f"g{group_id}"),
                                           on_error=self.show_error, silent=True)
                else:
                    msg = f"대역 {res['cidr']} 생성 (IP {res['ip_count']:,}개)"
                    if res["normalized"]:
                        msg += " — 입력값을 네트워크 주소로 보정했습니다"
                    self.set_status(msg)
                    self.open_subnet(res["id"])
                self.request_poll()

            if result["mode"] == "split":
                self.worker.submit(subnet_model.create_split_subnets, group_id, result["cidr"],
                                   result["split_prefix"], result["description"], result["populate"],
                                   self.operator, on_success=ok, on_error=self.show_error)
            else:
                self.worker.submit(subnet_model.create_subnet, group_id, result["cidr"], result["description"],
                                   result["populate"], self.operator, on_success=ok, on_error=self.show_error)

        self.worker.submit(_group_choices, on_success=got_groups, on_error=self.show_error)

    def edit_node(self) -> None:
        """선택 그룹/대역 수정."""
        kind, node_id = self.tree.selected()
        if kind == "group" and node_id is not None:
            def got(group: dict[str, Any] | None) -> None:
                if group is None:
                    return
                result = GroupDialog(self.root, self.theme, "그룹 수정", "그룹 이름 변경", group["name"]).show()
                if result:
                    # 그룹 설명은 더 이상 쓰지 않음(예전 값은 건드리지 않고 그대로 둔다)
                    self.worker.submit(group_model.update_group, node_id, result["name"], group["description"],
                                       self.operator, on_success=lambda _r: (self.set_status("그룹 수정 완료"),
                                                                             self.request_poll()),
                                       on_error=self.show_error)
            self.worker.submit(group_model.get_group, node_id, on_success=got, on_error=self.show_error)
        elif kind == "subnet" and node_id is not None:
            def got_s(subnet: dict[str, Any] | None) -> None:
                if subnet is None:
                    return
                result = SubnetEditDialog(self.root, self.theme, subnet["cidr"], subnet["description"]).show()
                if result:
                    self.worker.submit(subnet_model.update_subnet, node_id, result["description"], self.operator,
                                       on_success=lambda _r: (self.set_status("대역 수정 완료"), self.request_poll()),
                                       on_error=self.show_error)
            self.worker.submit(subnet_model.get_subnet, node_id, on_success=got_s, on_error=self.show_error)
        else:
            messagebox.showinfo("수정", "수정할 그룹 또는 대역을 좌측 트리에서 하나만 선택하세요.", parent=self.root)

    def delete_node(self) -> None:
        """선택한 그룹/대역(다중 가능) 삭제. '사용중' IP가 하나라도 있으면 전체 취소."""
        nodes = self.tree.selected_nodes()
        if not nodes:
            messagebox.showinfo("삭제", "삭제할 그룹 또는 대역을 좌측 트리에서 선택하세요.", parent=self.root)
            return
        names = [("그룹 " if k == "group" else "대역 ") + self.tree.label_of(k, n) for k, n in nodes]
        listing = "\n".join(f"• {n}" for n in names[:12]) + (f"\n…외 {len(names) - 12}개" if len(names) > 12 else "")
        if not messagebox.askyesno("삭제 확인", f"다음 {len(nodes)}개 항목과 그 안의 모든 하위 그룹·대역·IP를 "
                                            f"삭제하시겠습니까?\n\n{listing}\n\n"
                                            "※ '사용중' IP가 있으면 전체 삭제가 취소됩니다.\n※ 삭제 이력은 보존됩니다.",
                                   icon="warning", parent=self.root):
            return

        def ok(res: dict[str, int]) -> None:
            self.set_status(f"삭제 완료 — 대역 {res['subnets']:,}개 · IP {res['ips']:,}개 제거")
            self.view_kind = self.view_id = None
            self.view_nodes = []
            self.empty_view.tkraise()
            self.request_poll()

        self.worker.submit(subnet_model.delete_nodes, nodes, self.operator, on_success=ok, on_error=self.show_error)

    # ── IP 등록(할당) / 회수 ─────────────────────────────
    def open_action_for_selection(self, ids: list[int] | None = None) -> None:
        """Enter/더블클릭: 미사용이면 등록, 사용중이면 회수."""
        ids = ids or self.subnet_view.selected_ids()
        if len(ids) != 1:
            return
        row = self.subnet_view.rows.get(ids[0])
        if row is None:
            return
        if row["status"] == STATUS_USED:
            self.reclaim_selected(ids)
        else:
            self.assign_selected(ids)

    def assign_selected(self, ids: list[int] | None = None) -> None:
        """선택한 미사용 IP 일괄 등록(Ctrl/Shift로 다중 선택)."""
        self._bulk_action(ids, assign=True)

    def reclaim_selected(self, ids: list[int] | None = None) -> None:
        """선택한 사용중 IP 일괄 회수."""
        self._bulk_action(ids, assign=False)

    def _bulk_action(self, ids: list[int] | None, assign: bool) -> None:
        verb, target = ("등록", "미사용") if assign else ("회수", "사용중")
        view = self.subnet_view
        if self.view_kind != "subnet" or view.subnet is None:
            messagebox.showinfo(f"IP {verb}", "먼저 좌측 트리에서 대역을 선택하세요.", parent=self.root)
            return
        ids = ids or view.selected_ids()
        if not ids:
            messagebox.showinfo(f"IP {verb}", f"{verb}할 IP를 선택하세요.\n(Ctrl / Shift + 클릭으로 여러 개 선택)",
                                parent=self.root)
            return
        candidates = [i for i in ids if (view.rows.get(i, {}).get("status") == STATUS_USED) == (not assign)]
        skipped = len(ids) - len(candidates)
        if not candidates:
            messagebox.showwarning(f"{verb} 불가", f"선택한 IP 중 '{target}' 상태인 IP가 없습니다.", parent=self.root)
            return
        if len(candidates) > MAX_BULK_SELECT:
            messagebox.showwarning(f"IP {verb}", f"한 번에 최대 {MAX_BULK_SELECT:,}개까지 {verb}할 수 있습니다.",
                                   parent=self.root)
            return
        if skipped and not messagebox.askyesno(
                "혼합 선택", f"선택한 IP 중 '{target}' {len(candidates):,}개만 {verb} 대상입니다.\n"
                            f"나머지 {skipped:,}개는 건너뜁니다. 계속할까요?", parent=self.root):
            return

        def fresh(rows: dict[int, dict[str, Any]]) -> None:
            # 대화상자를 열기 전에 DB 최신 상태로 재검증(타 사용자 선행 변경 감지)
            changed = [rows.get(i) or {"ip_address": view.rows[i]["ip_address"], "status": "삭제됨",
                                       "updated_by": "-"} for i in candidates
                       if i not in rows or rows[i]["version"] != view.rows[i]["version"]]
            if changed:
                lines = "\n".join(f"• {r['ip_address']}: {r['status']} (작업자 {r['updated_by'] or '-'})"
                                  for r in changed[:10])
                messagebox.showwarning("최신 정보로 갱신", f"다른 사용자가 방금 아래 IP를 변경했습니다.\n\n{lines}\n\n"
                                                    "화면을 새로고침합니다. 다시 선택해 주세요.", parent=self.root)
                self.reload_view()
                self.request_poll()
                return
            items = [(i, view.rows[i]["ip_address"]) for i in candidates]
            if assign:
                result = AssignDialog(self.root, self.theme, items).show()
            else:
                result = ReclaimDialog(self.root, self.theme, items).show()
            if not result:
                return
            versions = {i: rows[i]["version"] for i in candidates}
            if assign:
                self.worker.submit(ip_model.assign_ips, versions, result["hostnames"], result["assignee"],
                                   result["request_no"], result["note"], self.operator,
                                   on_success=done, on_error=self.show_error)
            else:
                self.worker.submit(ip_model.reclaim_ips, versions, result["requester"], result["request_no"],
                                   result["reason"], self.operator, on_success=done, on_error=self.show_error)

        def done(res: dict[str, Any]) -> None:
            if not res["applied"]:
                lines = "\n".join(f"• {ip}: {why}" for ip, why in res["conflicts"][:10])
                more = f"\n…외 {len(res['conflicts']) - 10}건" if len(res["conflicts"]) > 10 else ""
                messagebox.showwarning("충돌 감지", f"[충돌 감지] 다른 사용자가 먼저 변경하여 {verb}를 취소했습니다.\n\n"
                                                f"{lines}{more}\n\n화면을 새로고침하여 최신 변경사항을 먼저 반영합니다.",
                                       parent=self.root)
            else:
                self.set_status(f"IP {res['count']:,}개 {verb} 완료")
            self.reload_view()
            self.request_poll()

        self.worker.submit(ip_model.get_ips, candidates, on_success=fresh, on_error=self.show_error)

    def copy_selected_rows(self) -> None:
        """선택 행을 탭 구분으로 복사(엑셀에 바로 붙여넣기)."""
        view = self.subnet_view
        rows = [view.rows[i] for i in view.selected_ids() if i in view.rows]
        if rows:
            keys = ("ip_address", "status", "hostname", "allocated_date", "assignee", "request_no", "note")
            text = "\n".join("\t".join(tsv_cell(r[k]) for k in keys) for r in rows)   # 여러 줄 비고도 한 셀로
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.set_status(f"{len(rows):,}행 복사됨 (엑셀에 붙여넣기 가능)")

    def copy_selected_ips(self) -> None:
        """선택 IP 주소를 클립보드로 복사."""
        view = self.subnet_view
        ips = [view.rows[i]["ip_address"] for i in view.selected_ids() if i in view.rows]
        if ips:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(ips))
            self.set_status(f"IP {len(ips):,}개 복사됨")

    # ── 검색 / 테마 ──────────────────────────────────────
    def open_search(self, _event: tk.Event | None = None) -> str:
        """F3 / Ctrl+F 통합 검색."""
        SearchDialog(self.root, self.theme, self.worker, on_navigate=self.navigate_to_ip,
                     on_error=self.show_error)
        return "break"

    def navigate_to_ip(self, ip_id: int) -> None:
        """검색 결과 → 트리 경로 펼침 → 대역 진입 → IP 선택."""
        def ok(loc: dict[str, Any] | None) -> None:
            if loc is None:
                messagebox.showinfo("이동", "해당 IP가 삭제되었습니다.", parent=self.root)
                return
            if self.view_kind == "subnet" and self.view_id == loc["subnet_id"]:
                self.subnet_view.select_ip(ip_id)
            else:
                self._pending_ip = ip_id
                self.tree.reveal(loc["group_chain"], f"s{loc['subnet_id']}")

        self.worker.submit(ip_model.locate_ip, ip_id, on_success=ok, on_error=self.show_error)

    THEME_CHOICES = (("system", "🖥 시스템 설정 따르기 (Windows)"), ("light", "☀ 라이트 모드"), ("dark", "🌙 다크 모드"))

    def _theme_menu(self, _event: tk.Event | None = None) -> None:
        """테마 버튼: 시스템 / 라이트 / 다크 중 선택(현재 선택에 ✓)."""
        menu = tk.Menu(self.root, tearoff=False)
        self.theme.style_menu(menu)
        for pref, label in self.THEME_CHOICES:
            mark = "✓  " if self.theme.preference == pref else "     "
            menu.add_command(label=mark + label, command=lambda p=pref: self._set_theme(p))
        btn = self.theme_btn
        menu.tk_popup(btn.winfo_rootx(), btn.winfo_rooty() + btn.winfo_height())

    def _set_theme(self, pref: str) -> None:
        self.theme.set_preference(pref)
        self._apply_theme(self.theme.palette)
        label = dict(self.THEME_CHOICES)[pref].split(" ", 1)[1]
        self.set_status(f"테마: {label}" + ("  — Windows 설정을 바꾸면 자동으로 따라갑니다" if pref == "system" else ""))

    # ── Excel ────────────────────────────────────────────
    def export_excel(self, _event: tk.Event | None = None) -> str:
        """그룹/다중 선택 → 대역별 사용현황, 대역 → IP 현황 + 전체 이력."""
        kind = self.view_kind
        stamp = f"{datetime.now():%Y%m%d_%H%M}"
        if kind == "group" and self.view_id is not None:
            default = f"IP관리_그룹_{_safe_filename(self.dashboard.path or '그룹')}_{stamp}.xlsx"
            job: tuple = (excel_export.collect_group_report, self.view_id, self.operator)
        elif kind == "multi":
            groups, subnets = self._scope()
            default = f"IP관리_선택항목_{len(self.view_nodes)}개_{stamp}.xlsx"
            job = (excel_export.collect_scope_report, groups, subnets, self.operator,
                   f"선택 항목 {len(self.view_nodes)}개")
        elif kind == "subnet" and self.subnet_view.subnet is not None:
            default = f"IP관리_대역_{_safe_filename(self.subnet_view.subnet['cidr'])}_{stamp}.xlsx"
            job = (excel_export.collect_subnet_report, self.view_id, self.operator)
        else:
            messagebox.showinfo("Excel 내보내기", "내보낼 그룹 또는 대역을 좌측 트리에서 선택하세요.\n\n"
                                               "· 그룹(여러 개 가능): 대역별 사용현황(1개 시트)\n"
                                               "· 대역: IP 할당현황 + 대역 전체 이력(2개 시트)", parent=self.root)
            return "break"
        path = filedialog.asksaveasfilename(parent=self.root, title="Excel 내보내기", defaultextension=".xlsx",
                                            initialfile=default, filetypes=[("Excel 통합 문서", "*.xlsx")])
        if not path:
            return "break"
        self.set_status("Excel 데이터 수집 중…")
        self.set_busy(True)

        def failed(exc: BaseException) -> None:
            self.set_busy(False)
            if isinstance(exc, PermissionError):
                messagebox.showerror("Excel 내보내기", f"파일을 저장할 수 없습니다. 파일이 열려 있는지 확인하세요.\n{path}",
                                     parent=self.root)
            else:
                self.show_error(exc)

        def written(target: Path) -> None:
            self.set_busy(False)
            self.set_status(f"Excel 저장 완료: {target}")
            if messagebox.askyesno("Excel 내보내기", f"저장되었습니다.\n{target}\n\n파일을 여시겠습니까?",
                                   parent=self.root):
                try:
                    os.startfile(target)  # type: ignore[attr-defined]
                except OSError as exc:
                    messagebox.showerror("열기 실패", str(exc), parent=self.root)

        def collected(report: excel_export.Report) -> None:
            self.set_status("Excel 파일 작성 중…")
            self.run_background(lambda: excel_export.write_report(report, path), written, failed)

        self.worker.submit(*job, on_success=collected, on_error=failed)
        return "break"

    # ── 무결성(위변조 감지) ──────────────────────────────
    def _set_integrity_label(self, text: str, level: str) -> None:
        self.integrity_label.configure(text=text, foreground=self.theme.palette[level])

    def run_quick_check(self, startup: bool = False, manual: bool = False) -> None:
        """빠른 검사(수 ms): 감시 장치 + 기준값 이후 새 이력 + 프로그램 밖에서 바뀐 행만 검증.

        startup이면 통과 시 하루 1회 정상 백업을 갱신한다."""
        if self._quick_inflight or self._integrity_running:
            return
        self._quick_inflight = True

        def done(report: integrity_model.Report) -> None:
            self._quick_inflight = False
            if not report.ok:
                self._on_integrity_problem(report)
            elif report.needs_full:
                self._start_baseline_check(backup=startup and self.auto_backup)
            else:
                self.integrity_report = report
                self._integrity_alerted = False
                self._set_integrity_label(f"🛡 무결성 정상 · {report.finished_at[11:16]}", "safe")
                if startup and self.auto_backup:
                    self._refresh_backup(report.head[0])
                if manual:
                    self.set_status(f"무결성 검사 정상 ({report.elapsed * 1000:.0f}ms)")

        def failed(exc: BaseException) -> None:
            self._quick_inflight = False
            if isinstance(exc, SchemaTooNewError):
                self._schema_too_new(exc)
                return
            log.warning("빠른 무결성 검사 실패: %s", exc)

        self.worker.submit(integrity_model.quick_check, self.anchor_path, on_success=done, on_error=failed,
                           silent=True)

    def _refresh_backup(self, history_id: int) -> None:
        """검사를 통과한 상태의 DB를 정상 백업으로 보관(하루 1회, 백그라운드)."""
        def job() -> Path | None:
            path = backup_mgr.refresh_good_backup(self.db_path, self.backup_dir, history_id, self.operator)
            backup_mgr.purge_old_backups(self.backup_dir)
            return path

        def done(path: Path | None) -> None:
            if path is not None:
                self.set_status(f"무결성 정상 → 정상 백업 갱신 ({path.name})")

        self.run_background(job, done, lambda exc: self.set_status(f"정상 백업 실패: {exc}"))

    def _start_baseline_check(self, backup: bool) -> None:
        """이전 버전 DB(또는 기준값 파일 없음)를 처음 열 때 1회: 전체 검사로 기준을 세운다(백그라운드)."""
        if self._integrity_running:
            return
        self._integrity_running = True
        self._set_integrity_label("🛡 최초 기준 검사 중…", "text_muted")
        last = [0.0]

        def progress(frac: float, text: str) -> None:
            now = time.monotonic()
            if now - last[0] > 0.3:
                last[0] = now
                self._bg_results.put((lambda _v: self._set_integrity_label(
                    f"🛡 최초 기준 검사 {frac * 100:.0f}%", "text_muted"), None))

        def done(report: integrity_model.Report) -> None:
            self._integrity_running = False
            if report.ok:
                self.integrity_report = report
                self._set_integrity_label(f"🛡 무결성 정상 · {report.finished_at[11:16]}", "safe")
                if backup:
                    self._refresh_backup(report.head[0])
            else:
                self._on_integrity_problem(report)

        def failed(exc: BaseException) -> None:
            self._integrity_running = False
            if isinstance(exc, SchemaTooNewError):
                self._schema_too_new(exc)
                return
            self._set_integrity_label("🛡 무결성 검사 실패", "warning")
            log.error("무결성 기준 검사 오류", exc_info=exc)

        self.run_background(lambda: integrity_model.full_check(self.db_path, self.anchor_path, progress),
                            done, failed)

    def _schema_too_new(self, exc: SchemaTooNewError) -> None:
        """다른 사용자가 새 버전 프로그램으로 DB를 업데이트함 → 안내 후 종료(구버전은 DB를 건드리지 않음)."""
        if self._closing_for_upgrade:
            return
        self._closing_for_upgrade = True
        log.warning("DB가 더 새 버전으로 업데이트됨: v%s", exc.db_version)
        messagebox.showwarning("새 버전 필요", str(exc), parent=self.root)
        self.root.destroy()

    def _on_integrity_problem(self, report: integrity_model.Report) -> None:
        self.integrity_report = report
        self._set_integrity_label("⚠ DB 변조 감지 — 클릭하여 복구", "critical")
        self.set_status(f"무결성 경고: {report.summary()}")
        if not self._integrity_alerted:
            self._integrity_alerted = True
            self.confirm_recover()

    def integrity_clicked(self) -> None:
        """상태 표시줄 🛡 클릭: 문제가 있으면 복구 확인, 없으면 빠른 검사."""
        if self.integrity_report is not None and not self.integrity_report.ok:
            self.confirm_recover()
        else:
            self.run_quick_check(manual=True)

    def confirm_recover(self) -> None:
        """변조 확인 → 사용자 확인 1번 → 최종 정상 버전으로 자동 복구(방식은 프로그램이 선택)."""
        report = self.integrity_report
        if report is None or report.ok or self._recovering:
            return
        lines = [f"• {integrity_model.KIND_LABELS[i.kind]} — {i.where}" for i in report.issues[:6]]
        if len(report.issues) > 6:
            lines.append(f"• …외 {sum(report.counts.values()) - 6:,}건")
        if not messagebox.askyesno(
                "DB 변조 확인",
                "프로그램 밖에서 DB가 수정된 것이 확인되었습니다.\n\n" + "\n".join(lines) +
                "\n\n최종 정상 버전으로 복구하겠습니다.\n"
                "(복구 방식은 자동으로 선택되며, 백업으로 되돌리는 경우 변조된 DB는\n"
                " Backup 폴더에 '변조본_날짜.db'로 보관됩니다)\n\n지금 복구할까요?",
                icon="warning", parent=self.root):
            return
        self._recovering = True
        self._set_integrity_label("🛡 복구 중…", "text_muted")
        self.set_busy(True)

        def ok(res: dict[str, Any]) -> None:
            self._recovering = False
            self.set_busy(False)
            self._integrity_alerted = False
            if res["method"] == "rows":
                text = (f"변조된 항목을 마지막 정상 이력 기준으로 되돌렸습니다(데이터 손실 없음).\n\n"
                        f"• 복원 {res.get('restored', 0):,}건 · 초기화 {res.get('pristine', 0):,}건 · "
                        f"제거 {res.get('deleted', 0):,}건")
            elif res["method"] == "backup":
                manual = res["manual"]
                text = (f"정상 백업({res['backup']})으로 되돌린 뒤 이후 정상 작업 {res['replayed']:,}건을 "
                        f"다시 적용했습니다(IP 반영 {res['applied']:,}건).\n\n"
                        f"• 변조된 DB 보관: {Path(res['tampered_copy']).name}")
                if manual:
                    text += ("\n\n아래 구조 변경(대역·그룹)은 백업 이후에 있었던 작업이라 다시 확인해 주세요:\n"
                             + "\n".join(f"• {m}" for m in manual[:8])
                             + (f"\n…외 {len(manual) - 8}건" if len(manual) > 8 else ""))
            else:
                text = "다른 사용자가 이미 복구를 마쳐 DB가 정상 상태입니다."
            messagebox.showinfo("복구 완료", text + "\n\n복구 내역은 이력에 '무결성 복구'로 남았습니다."
                                if res["method"] != "none" else text, parent=self.root)
            self.refresh_all()
            self.run_quick_check()

        def failed(exc: BaseException) -> None:
            self._recovering = False
            self._set_integrity_label("⚠ DB 변조 감지 — 클릭하여 복구", "critical")
            self.show_error(exc)

        self.worker.submit(integrity_model.recover, self.anchor_path, self.backup_dir, self.operator, report.scope,
                           on_success=ok, on_error=failed)

    # ── 시작 ─────────────────────────────────────────────
    def start(self) -> None:
        """초기 트리 로딩."""
        self.tree.refresh(on_done=self._after_first_load)

    def _after_first_load(self) -> None:
        if not self.tree.tree.get_children(""):
            self.empty_hint.configure(text="등록된 대역이 없습니다. [＋ 대역 추가]로 시작하세요.")
        self.set_status(f"준비 완료 — 작업자 {self.operator}")
