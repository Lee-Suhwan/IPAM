"""F3 / Ctrl+F 통합 검색 모달 (IP · 호스트네임 · 담당자 · 요청번호 · 비고).

검색 전에는 전체 최근 이력을 보여 준다. 처음에 RECENT_PAGE건만 읽고, 아래로 스크롤해 끝에 가까워질 때마다
다음 RECENT_PAGE건을 이어 붙인다(이력이 수백만 건이어도 화면에 필요한 만큼만 조회).
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from ipmgr.constants import SEARCH_LIMIT, STATUS_USED
from ipmgr.db.worker import DBWorker
from ipmgr.models import history_model, ip_model
from ipmgr.ui.theme_mgr import Palette, ThemeManager
from ipmgr.ui.widgets import center_over, one_line, scrolled_tree

RECENT_PAGE = 200
COLUMNS = (
    ("ip", "IP 주소", 120, "w", False),
    ("status", "상태", 70, "center", False),
    ("hostname", "호스트네임", 150, "w", True),
    ("assignee", "담당자", 90, "w", False),
    ("request_no", "요청번호", 110, "w", False),
    ("note", "비고", 140, "w", True),
    ("cidr", "대역", 120, "w", False),
    ("group", "그룹 경로", 180, "w", True),
)
RECENT_COLUMNS = (
    ("ts", "일시", 140, "w", False),
    ("action", "작업", 90, "center", False),
    ("target", "대상", 130, "w", False),
    ("change", "상태 변경", 120, "center", False),
    ("hostname", "호스트네임", 130, "w", True),
    ("assignee", "담당자", 80, "w", False),
    ("request_no", "요청번호", 100, "w", False),
    ("operator", "작업자", 150, "w", False),
    ("detail", "내용", 220, "w", True),
)


def _vscroll(frame: ttk.Frame) -> ttk.Scrollbar:
    return next(w for w in frame.winfo_children()
                if isinstance(w, ttk.Scrollbar) and str(w.cget("orient")) == "vertical")


class SearchDialog(tk.Toplevel):
    """통합 검색 창. 결과 더블클릭/Enter → 해당 IP로 이동."""

    def __init__(self, parent: tk.Misc, theme: ThemeManager, worker: DBWorker,
                 on_navigate: Callable[[int], None], on_error: Callable[[BaseException], None],
                 initial: str = "") -> None:
        super().__init__(parent)
        self.withdraw()
        self.theme = theme
        self.worker = worker
        self.on_navigate = on_navigate
        self.on_error = on_error
        self._seq = 0
        self._recent_last: int | None = None      # 마지막으로 불러온 이력 id(다음 페이지 기준)
        self._recent_more = False
        self._recent_loading = False
        self._showing_recent = True
        self._nav: dict[str, int] = {}
        px = theme.px
        self.title("통합 검색")
        self.transient(parent.winfo_toplevel())
        self.geometry(f"{px(1080)}x{px(600)}")
        self.minsize(px(700), px(380))
        theme.register_toplevel(self)

        outer = ttk.Frame(self, style="Surface.TFrame", padding=px(14))
        outer.pack(fill="both", expand=True)
        outer.grid_rowconfigure(2, weight=1)
        outer.grid_columnconfigure(0, weight=1)
        bar = ttk.Frame(outer, style="Surface.TFrame")
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        self.query = tk.StringVar(self, value=initial)
        self.entry = ttk.Entry(bar, textvariable=self.query, font=theme.font)
        self.entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="🔍 검색", style="Accent.TButton", command=self.search).grid(
            row=0, column=1, padx=(px(8), 0))
        self.info = ttk.Label(outer, style="SurfaceMuted.TLabel", text="")
        self.info.grid(row=1, column=0, sticky="w", pady=(px(8), px(6)))

        holder = ttk.Frame(outer, style="Surface.TFrame")
        holder.grid(row=2, column=0, sticky="nsew")
        holder.grid_rowconfigure(0, weight=1)
        holder.grid_columnconfigure(0, weight=1)
        self.result_frame, self.tree = scrolled_tree(holder, theme, COLUMNS, selectmode="browse")
        self.recent_frame, self.recent = scrolled_tree(holder, theme, RECENT_COLUMNS, selectmode="browse")
        for frame in (self.result_frame, self.recent_frame):
            frame.grid(row=0, column=0, sticky="nsew")
        vsb = _vscroll(self.recent_frame)
        self.recent.configure(yscrollcommand=lambda first, last: (vsb.set(first, last),
                                                                  self._near_end(first, last)))

        for tree in (self.tree, self.recent):
            tree.bind("<Double-Button-1>", self._go)
            tree.bind("<Return>", self._go)
        self.entry.bind("<Return>", lambda e: self.search())
        self.entry.bind("<Down>", lambda e: self._focus_results())
        self.bind("<Escape>", lambda e: self.destroy())
        theme.register(self._apply_theme)
        center_over(self, parent)
        self.deiconify()
        self.grab_set()
        self.entry.focus_set()
        self.entry.select_range(0, "end")
        if initial.strip():
            self.search()
        else:
            self.show_recent()

    def _apply_theme(self, p: Palette) -> None:
        self.tree.tag_configure("used", foreground=p["used"])
        self.recent.tag_configure("used", foreground=p["used"])

    @property
    def _active(self) -> ttk.Treeview:
        return self.recent if self._showing_recent else self.tree

    # ── 최근 이력(검색 전 기본 화면) ─────────────────────────
    def show_recent(self) -> None:
        """검색어가 없을 때: 최근 이력 첫 페이지."""
        self._seq += 1
        self._showing_recent = True
        self.recent_frame.tkraise()
        self.recent.delete(*self.recent.get_children())
        self._nav.clear()
        self._recent_last, self._recent_more, self._recent_loading = None, False, False
        self._load_recent()

    def _near_end(self, first: str, last: str) -> None:
        # first > 0: 사용자가 실제로 스크롤했을 때만(첫 배치 직후 레이아웃 계산 중의 호출은 무시)
        if self._recent_more and not self._recent_loading and float(first) > 0 and float(last) > 0.92:
            self._load_recent()

    def _load_recent(self) -> None:
        self._recent_loading = True
        seq = self._seq
        self.info.configure(text="최근 이력 불러오는 중…")

        def ok(page: dict[str, Any]) -> None:
            if seq != self._seq or not self.winfo_exists():
                return
            self._recent_loading = False
            self._append_recent(page)

        def failed(exc: BaseException) -> None:
            self._recent_loading = False
            self.on_error(exc)

        self.worker.submit(history_model.recent_history, self._recent_last, RECENT_PAGE, on_success=ok,
                           on_error=failed)

    def _append_recent(self, page: dict[str, Any]) -> None:
        t = self.recent
        for r in page["rows"]:
            iid = f"h{r['id']}"
            change = (f"{r['old_status']} → {r['new_status']}" if r["old_status"] and r["new_status"]
                      and r["old_status"] != r["new_status"] else r["new_status"] or "")
            t.insert("", "end", iid=iid, tags=("used",) if r["new_status"] == STATUS_USED else (),
                     values=(r["timestamp"], r["action"], r["ip_address"] or r["cidr"] or "", change,
                             r["hostname"], r["assignee"], r["request_no"], r["operator"],
                             one_line(r["diff_summary"], 200)))
            if r["nav"] is not None:
                self._nav[iid] = r["nav"]
        if page["rows"]:
            self._recent_last = page["rows"][-1]["id"]
        self._recent_more = page["has_more"]
        count = len(t.get_children())
        tail = " · 아래로 스크롤하면 이전 이력을 더 불러옵니다" if self._recent_more else " · 처음 이력까지 모두 표시"
        self.info.configure(text=f"최근 이력 {count:,}건{tail if count else ''}   —   "
                                 "IP(10.1.2.3 · 10.1.2 · 10.1.0.0/16), 호스트네임, 담당자, 요청번호, 비고로 검색")
        if not count:
            self.info.configure(text="아직 이력이 없습니다. IP, 호스트네임, 담당자, 요청번호, 비고로 검색할 수 있습니다.")

    # ── 검색 ─────────────────────────────────────────────
    def search(self) -> None:
        """Worker에서 검색 실행(검색어를 지우고 검색하면 최근 이력으로 돌아간다)."""
        text = self.query.get().strip()
        if not text:
            self.show_recent()
            return
        self._seq += 1
        seq = self._seq
        self.info.configure(text=f"'{text}' 검색 중…")

        def ok(result: dict[str, Any]) -> None:
            if seq != self._seq or not self.winfo_exists():
                return
            self._show(result, text)

        self.worker.submit(ip_model.search, text, on_success=ok, on_error=self.on_error)

    def _show(self, result: dict[str, Any], text: str) -> None:
        self._showing_recent = False
        self.result_frame.tkraise()
        t = self.tree
        t.delete(*t.get_children())
        for r in result["rows"]:
            t.insert("", "end", iid=str(r["id"]), tags=("used",) if r["status"] == STATUS_USED else (),
                     values=(r["ip_address"], r["status"], r["hostname"], r["assignee"], r["request_no"],
                             one_line(r["note"]), r["cidr"], r["group_path"]))
        count = len(result["rows"])
        more = f" (상위 {SEARCH_LIMIT:,}건만 표시 — 검색어를 구체적으로 입력하세요)" if result["truncated"] else ""
        self.info.configure(text=f"'{text}' 검색 결과 {count:,}건{more}   ·   검색어를 지우고 Enter → 최근 이력")
        if count:
            first = t.get_children()[0]
            t.selection_set(first)
            t.focus(first)

    def _focus_results(self) -> None:
        tree = self._active
        children = tree.get_children()
        if children:
            tree.focus_set()
            tree.selection_set(children[0])
            tree.focus(children[0])

    def _go(self, event: tk.Event | None = None) -> None:
        tree = event.widget if event is not None and isinstance(event.widget, ttk.Treeview) else self._active
        sel = tree.selection()
        if not sel:
            return
        ip_id = self._nav.get(sel[0]) if tree is self.recent else int(sel[0])
        if ip_id is None:
            return
        self.destroy()
        self.on_navigate(ip_id)
