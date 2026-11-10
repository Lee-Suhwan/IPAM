"""좌측 그룹/대역 계층 트리뷰.

- 지연 로딩 + 상태(펼침/선택/스크롤) 보존 갱신
- Ctrl/Shift 다중 선택
- Drag & Drop 이동(다중 선택 항목 함께 이동, 빈 곳에 놓으면 그룹 밖 최상위, 순환 참조 차단)
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable

from ipmgr.db.worker import DBWorker
from ipmgr.logic.subnet_calc import usage_level, usage_percent
from ipmgr.models import group_model
from ipmgr.ui.theme_mgr import Palette, ThemeManager

DUMMY = "::dummy"
Node = tuple[str, int]
SelectCallback = Callable[[list[Node]], None]
# (옮길 노드들, 새 부모, 원본 이름, 대상 이름, 기준 노드|None, 기준 뒤에?, 같은 부모 안 순서 변경만?)
MoveCallback = Callable[[list[Node], int | None, str, str, Node | None, bool, bool], None]
ContextCallback = Callable[[tk.Event, list[Node]], None]


def parse_iid(iid: str) -> tuple[str | None, int | None]:
    """'g12' → ('group', 12), 's5' → ('subnet', 5)."""
    if not iid or iid.endswith(DUMMY):
        return None, None
    kind = {"g": "group", "s": "subnet"}.get(iid[0])
    try:
        return kind, int(iid[1:])
    except ValueError:
        return None, None


def node_iid(kind: str, node_id: int) -> str:
    """('group', 3) → 'g3'."""
    return ("g" if kind == "group" else "s") + str(node_id)


class NetworkTree(ttk.Frame):
    """네트워크 계층 트리."""

    def __init__(self, master: tk.Misc, theme: ThemeManager, worker: DBWorker, *,
                 on_select: SelectCallback, on_move: MoveCallback, on_context: ContextCallback,
                 on_error: Callable[[BaseException], None]) -> None:
        super().__init__(master, style="Surface.TFrame")
        self.theme = theme
        self.worker = worker
        self.on_select = on_select
        self.on_move = on_move
        self.on_context = on_context
        self.on_error = on_error
        self._loaded: set[str] = set()
        self._press: tuple[str, int, int, tuple[str, ...]] | None = None
        self._sources: list[str] = []
        self._dragging = False
        self._drop_iid: str | None = None
        self._hover_job: str | None = None
        self._line: tk.Frame | None = None          # 순서 변경 시 들어갈 자리를 보여주는 가로선
        self._suppress_select = False
        self._last_selection: tuple[str, ...] = ()
        px = theme.px

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        head = ttk.Frame(self, style="Surface.TFrame", padding=(px(12), px(10), px(8), px(6)))
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(0, weight=1)
        ttk.Label(head, text="네트워크 계층", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(head, text="다중 선택 · 드래그로 이동/순서", style="SurfaceMuted.TLabel",
                  font=theme.font_small).grid(row=0, column=1, sticky="e")

        body = ttk.Frame(self, style="Surface.TFrame")
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(body, columns=("usage",), show="tree headings", selectmode="extended")
        vsb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(body, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.tree.heading("#0", text="그룹 / 대역", anchor="w")
        self.tree.heading("usage", text="사용률", anchor="e")
        usage_w = theme.font_bold.measure("사용률") + px(36)
        self.tree.column("#0", width=px(250), minwidth=px(160), stretch=True)
        self.tree.column("usage", width=usage_w, minwidth=usage_w, anchor="e", stretch=False)

        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<ButtonPress-1>", self._on_press, add="+")
        self.tree.bind("<B1-Motion>", self._on_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self._on_release, add="+")
        self.tree.bind("<Escape>", lambda e: self._cancel_drag())
        self.tree.bind("<Button-3>", self._on_right_click)
        theme.register(self._apply_theme)

    # ── 테마 ──────────────────────────────────────────────
    def _apply_theme(self, p: Palette) -> None:
        t = self.tree
        t.tag_configure("group", font=self.theme.font_bold)
        t.tag_configure("subnet", font=self.theme.font)
        t.tag_configure("empty", foreground=p["text_muted"])
        for level in ("safe", "warning", "critical"):
            t.tag_configure(level, foreground=p[level])
        # 드롭 하이라이트는 가장 나중에 설정 → 우선순위 최상
        t.tag_configure("drop_ok", background=p["drop_ok"])
        t.tag_configure("drop_bad", background=p["drop_bad"])
        if self._line is not None:
            self._line.configure(background=p["accent"])

    # ── 공개 API ──────────────────────────────────────────
    def refresh(self, on_done: Callable[[], None] | None = None) -> None:
        """최상위 + 펼쳐 둔 모든 그룹의 자식을 다시 조회해 상태를 보존하며 갱신한다."""
        parents: list[int | None] = [None]
        for iid in list(self._loaded):
            kind, gid = parse_iid(iid)
            if kind == "group" and self.tree.exists(iid):
                parents.append(gid)
        self._request(parents, on_done)

    def reveal(self, group_chain: list[int], target_iid: str, on_done: Callable[[], None] | None = None) -> None:
        """조상 그룹 체인을 펼치고 대상 노드를 선택한다(검색 결과/대시보드 이동용)."""
        def done() -> None:
            for gid in group_chain:
                iid = f"g{gid}"
                if self.tree.exists(iid) and iid != target_iid:
                    self.tree.item(iid, open=True)
            if self.tree.exists(target_iid):
                self.select(target_iid)
            if on_done:
                on_done()
        self._request([None, *group_chain], done)

    def select(self, iid: str) -> None:
        """노드 하나만 선택 + 화면에 보이도록 스크롤."""
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)

    def selected_nodes(self) -> list[Node]:
        """선택된 노드 목록(화면 순서)."""
        nodes = []
        for iid in self.tree.selection():
            kind, node_id = parse_iid(iid)
            if kind and node_id is not None:
                nodes.append((kind, node_id))
        return nodes

    def selected(self) -> tuple[str | None, int | None]:
        """단일 선택일 때 (종류, ID), 아니면 (None, None)."""
        nodes = self.selected_nodes()
        return nodes[0] if len(nodes) == 1 else (None, None)

    def label_of(self, kind: str, node_id: int) -> str:
        """노드 표시 이름(아이콘 제외)."""
        iid = node_iid(kind, node_id)
        if not self.tree.exists(iid):
            return ""
        return self.tree.item(iid, "text").replace("📁", "").replace("🌐", "").strip()

    def parent_group_of(self, iid: str) -> int | None:
        """노드의 부모 그룹 ID(최상위면 None)."""
        parent = self.tree.parent(iid)
        return parse_iid(parent)[1] if parent else None

    # ── 데이터 로딩 ───────────────────────────────────────
    def _request(self, parents: list[int | None], on_done: Callable[[], None] | None) -> None:
        def ok(snapshot: dict[int | None, Any]) -> None:
            self._apply_snapshot(snapshot)
            if on_done:
                on_done()
        self.worker.submit(group_model.tree_snapshot, parents, on_success=ok, on_error=self.on_error, silent=True)

    def _apply_snapshot(self, snapshot: dict[int | None, Any]) -> None:
        before = self.tree.selection()
        self._suppress_select = True
        try:
            for pid in sorted(snapshot, key=lambda k: -1 if k is None else 0):   # 루트 먼저
                parent_iid = "" if pid is None else f"g{pid}"
                if parent_iid and not self.tree.exists(parent_iid):
                    continue
                self._reconcile(parent_iid, snapshot[pid])
                if parent_iid:
                    self._loaded.add(parent_iid)
        finally:
            self._suppress_select = False
        if self.tree.selection() != before:
            self._on_select()

    def _node_display(self, kind: str, data: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
        if kind == "group":
            total, used = data["total"], data["used"]
            text = f"📁  {data['name']}"
        else:
            total, used = data["total_count"], data["used_count"]
            desc = f"   {data['description']}" if data["description"] else ""
            text = f"🌐  {data['cidr']}{desc}"
        if total:
            pct = usage_percent(used, total)
            return text, f"{pct:.1f}%", (kind, usage_level(pct))
        return text, "—", (kind, "empty")

    def _reconcile(self, parent_iid: str, children: dict[str, list[dict[str, Any]]]) -> None:
        t = self.tree
        items = children.get("items") or ([("group", g) for g in children["groups"]]
                                          + [("subnet", s) for s in children["subnets"]])
        desired: list[tuple[str, str, dict[str, Any]]] = [(node_iid(k, d["id"]), k, d) for k, d in items]
        wanted = {iid for iid, _, _ in desired}
        for iid in t.get_children(parent_iid):
            if iid not in wanted:
                self._forget(iid)
                t.delete(iid)
        for index, (iid, kind, data) in enumerate(desired):
            text, usage, tags = self._node_display(kind, data)
            if t.exists(iid):
                t.item(iid, text=text, values=(usage,), tags=tags)
                if t.parent(iid) != parent_iid or t.index(iid) != index:
                    t.move(iid, parent_iid, index)
            else:
                t.insert(parent_iid, index, iid=iid, text=text, values=(usage,), tags=tags)
            if kind == "group":
                has_children = data["has_children"]
                if iid not in self._loaded:
                    kids = t.get_children(iid)
                    if has_children and not kids:
                        t.insert(iid, "end", iid=iid + DUMMY, text="불러오는 중…")
                    elif not has_children:
                        for k in kids:
                            t.delete(k)
                elif not has_children:
                    for k in t.get_children(iid):
                        self._forget(k)
                        t.delete(k)

    def _forget(self, iid: str) -> None:
        stack = [iid]
        while stack:
            cur = stack.pop()
            self._loaded.discard(cur)
            if self.tree.exists(cur):
                stack.extend(self.tree.get_children(cur))

    # ── 이벤트 ────────────────────────────────────────────
    def _on_open(self, _event: tk.Event | None = None) -> None:
        iid = self.tree.focus()
        kind, gid = parse_iid(iid)
        if kind == "group" and iid not in self._loaded:
            self._request([gid], None)

    def _on_select(self, _event: tk.Event | None = None) -> None:
        if self._suppress_select or self._dragging:
            return
        sel = tuple(i for i in self.tree.selection() if not i.endswith(DUMMY))
        if sel == self._last_selection:
            return
        self._last_selection = sel
        self.on_select(self.selected_nodes())

    def _on_right_click(self, event: tk.Event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid and not iid.endswith(DUMMY):
            if iid not in self.tree.selection():
                self.select(iid)
        else:
            self.tree.selection_set(())
        self.on_context(event, self.selected_nodes())

    # ── Drag & Drop (다중 선택 지원) ──────────────────────
    def _on_press(self, event: tk.Event) -> None:
        self._cancel_drag()
        iid = self.tree.identify_row(event.y)
        element = self.tree.identify_element(event.x, event.y)
        if iid and not iid.endswith(DUMMY) and "indicator" not in str(element) and not event.state & 0x0005:
            # 클래스 바인딩이 선택을 단일로 바꾸기 전에 현재 다중 선택을 기억
            self._press = (iid, event.x, event.y, self.tree.selection())

    def _on_motion(self, event: tk.Event) -> None:
        if not self._press:
            return
        src, x0, y0, before = self._press
        if not self._dragging:
            if abs(event.x - x0) + abs(event.y - y0) < self.theme.px(8):
                return
            self._dragging = True
            self._sources = [i for i in before if not i.endswith(DUMMY)] if src in before and len(before) > 1 \
                else [src]
            self.tree.selection_set(self._sources)
            self.tree.configure(cursor="fleur")
        height = self.tree.winfo_height()
        edge = self.theme.row_height
        if event.y < edge:
            self.tree.yview_scroll(-1, "units")
        elif event.y > height - edge:
            self.tree.yview_scroll(1, "units")
        self._show_spot(self._drop_spot(self._sources, event.y))

    def _on_release(self, event: tk.Event) -> None:
        if not self._dragging or not self._press:
            self._press = None
            return
        sources = list(self._sources)
        spot = self._drop_spot(sources, event.y)
        self._cancel_drag()
        self._last_selection = ()
        self._on_select()
        if spot["valid"] is None:
            return
        if not spot["valid"]:
            messagebox.showwarning("이동 불가", spot["reason"], parent=self)
            return
        nodes = [n for n in (parse_iid(i) for i in sources) if n[0] and n[1] is not None]
        parent = spot["parent"]
        new_parent = parse_iid(parent)[1] if parent else None
        src_name = self.label_of(*nodes[0]) if len(nodes) == 1 else f"선택한 {len(nodes)}개 항목"  # type: ignore[arg-type]
        dst_name = self.label_of("group", new_parent) if new_parent is not None else "(최상위 · 그룹 밖)"
        ref = parse_iid(spot["ref"]) if spot["ref"] else None
        reorder_only = all(self.tree.parent(s) == parent for s in sources)
        self.on_move(nodes, new_parent, src_name, dst_name, ref, spot["after"], reorder_only)  # type: ignore[arg-type]

    def _drop_spot(self, sources: list[str], y: int) -> dict[str, Any]:
        """마우스 위치로 놓을 자리를 계산한다.

        행의 위/아래 가장자리 → 그 항목의 앞/뒤(순서 변경), 그룹 행의 가운데 → 그 그룹 안으로,
        빈 곳 → 최상위 맨 끝. 반환: parent(iid, ''=최상위), ref(기준 행 iid|None), after, row, into,
        valid(True/False/None=변화 없음), reason.
        """
        row = self.tree.identify_row(y)
        spot: dict[str, Any] = {"parent": "", "ref": None, "after": False, "row": row, "into": True,
                                "valid": True, "reason": ""}
        if row.endswith(DUMMY):
            row = spot["row"] = self.tree.parent(row)
        if row:
            bbox = self.tree.bbox(row)
            rel = (y - bbox[1]) / bbox[3] if bbox else 0.5
            if parse_iid(row)[0] == "group" and 0.25 <= rel <= 0.75:
                spot["parent"] = row                                   # 그룹 안으로
            else:
                spot.update(parent=self.tree.parent(row), ref=row, into=False,
                            after=rel > (0.75 if parse_iid(row)[0] == "group" else 0.5))
        parent = spot["parent"]
        for src in sources:
            cur = parent
            while cur:
                if cur == src:
                    spot.update(valid=False, reason="순환 참조 방지: 그룹을 자기 자신 또는 하위 그룹 안으로 "
                                                    "옮길 수 없습니다.")
                    return spot
                cur = self.tree.parent(cur)
        if spot["ref"] in sources:
            spot["valid"] = None                                       # 자기 자리
        elif spot["into"] and all(self.tree.parent(s) == parent for s in sources):
            spot["valid"] = None                                       # 이미 그 그룹 안
        return spot

    def _show_spot(self, spot: dict[str, Any]) -> None:
        """그룹 안으로 → 행 강조, 앞/뒤로 → 가로선, 불가 → 빨간 강조."""
        target = spot["row"] if (spot["into"] or not spot["valid"]) else None
        self._highlight(target if spot["valid"] is not None else None, spot["valid"])
        line = self._ensure_line()
        if not spot["into"] and spot["valid"] and spot["ref"]:
            bbox = self.tree.bbox(spot["ref"])
            if bbox:
                y = bbox[1] + (bbox[3] if spot["after"] else 0) - self.theme.px(1)
                line.place(x=bbox[0], y=y, relwidth=1.0)
                line.lift()
                return
        line.place_forget()

    def _ensure_line(self) -> tk.Frame:
        if self._line is None:
            self._line = tk.Frame(self.tree, height=self.theme.px(3), background=self.theme.palette["accent"])
        return self._line

    def _highlight(self, target: str | None, valid: bool | None) -> None:
        if self._drop_iid and self._drop_iid != target and self.tree.exists(self._drop_iid):
            self.tree.tk.call(self.tree, "tag", "remove", "drop_ok", self._drop_iid)
            self.tree.tk.call(self.tree, "tag", "remove", "drop_bad", self._drop_iid)
        if self._hover_job and self._drop_iid != target:
            self.after_cancel(self._hover_job)
            self._hover_job = None
        self._drop_iid = target or None
        if not target or valid is None:
            return
        self.tree.tk.call(self.tree, "tag", "add", "drop_ok" if valid else "drop_bad", target)
        if valid and not self.tree.item(target, "open") and not self._hover_job:
            self._hover_job = self.after(700, lambda t=target: self._hover_expand(t))

    def _hover_expand(self, iid: str) -> None:
        self._hover_job = None
        if self._dragging and self.tree.exists(iid) and self.tree.get_children(iid):
            self.tree.item(iid, open=True)
            kind, gid = parse_iid(iid)
            if kind == "group" and iid not in self._loaded:
                self._request([gid], None)

    def _cancel_drag(self) -> None:
        if self._hover_job:
            self.after_cancel(self._hover_job)
            self._hover_job = None
        if self._drop_iid and self.tree.exists(self._drop_iid):
            self.tree.tk.call(self.tree, "tag", "remove", "drop_ok", self._drop_iid)
            self.tree.tk.call(self.tree, "tag", "remove", "drop_bad", self._drop_iid)
        self._drop_iid = None
        if self._line is not None:
            self._line.place_forget()
        if self._dragging:
            self.tree.configure(cursor="")
        self._dragging = False
        self._press = None
