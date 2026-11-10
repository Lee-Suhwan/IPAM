"""그룹/대역 등록·수정, 다중 IP 일괄 등록(할당)·회수 대화상자."""
from __future__ import annotations

import ipaddress
import tkinter as tk
from tkinter import ttk
from typing import Any

from ipmgr.constants import MAX_NOTE_LEN, MAX_SPLIT_SUBNETS, MIN_SUBNET_PREFIX
from ipmgr.logic.subnet_calc import parse_cidr, split_cidr
from ipmgr.ui.theme_mgr import ThemeManager
from ipmgr.ui.widgets import FormDialog

NO_GROUP = "(그룹 없음 · 최상위)"


class GroupDialog(FormDialog):
    """그룹 생성/수정: 이름만 입력(생성 시 입력란은 비어 있다)."""

    def __init__(self, parent: tk.Misc, theme: ThemeManager, title: str, location: str, name: str = "") -> None:
        self.location_text, self.initial_name = location, name
        super().__init__(parent, theme, title, ok_text="저장")

    def build(self, body: ttk.Frame) -> None:
        ttk.Label(body, text=self.location_text, style="SurfaceMuted.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, self.theme.px(6)))
        name, self.name_var = self.entry(self.initial_name)
        self.add_row(1, "그룹 이름", name)

    def validate(self) -> dict[str, Any]:
        name = self.name_var.get().strip()
        if not name:
            raise ValueError("그룹 이름을 입력하세요.")
        return {"name": name}


NO_SPLIT = "분할 안 함"


class SubnetDialog(FormDialog):
    """새 대역 등록. 입력한 CIDR을 그대로 만들거나, 원하는 단위로 나눠 여러 대역을 한 번에 만든다.

    예) 10.0.0.0/16 그대로 1개,  10.0.1.0/24 → /25 2개,  10.0.0.0/16 → /20 16개,  10.1.0.0/16 → /24 256개
    분할 단위 목록은 입력한 CIDR보다 작은 대역(/n+1 ~ /30)으로 자동 구성된다. 그룹은 선택 사항.
    """

    def __init__(self, parent: tk.Misc, theme: ThemeManager, groups: list[tuple[int, str]],
                 default_group: int | None) -> None:
        self.groups = groups
        self.default_group = default_group
        self._prefix: int | None = None
        self._user_picked = False         # 분할 단위를 사용자가 직접 골랐는지
        super().__init__(parent, theme, "대역(서브넷) 등록", ok_text="생성")

    def build(self, body: ttk.Frame) -> None:
        px = self.theme.px
        cidr, self.cidr_var = self.entry("", width=24)
        self.add_row(0, "CIDR", cidr, hint="예) 10.0.1.0/24 · 10.0.0.0/16  (/16까지는 분할 없이 그대로 등록 가능)")
        self.split_var = tk.StringVar(self, value=NO_SPLIT)
        holder = ttk.Frame(body, style="Surface.TFrame")
        self.split = ttk.Combobox(holder, textvariable=self.split_var, state="readonly", width=14,
                                  values=[NO_SPLIT], font=self.theme.font)
        self.theme.track_combobox(self.split)
        self.split.pack(side="left")
        self.split.bind("<<ComboboxSelected>>", lambda e: setattr(self, "_user_picked", True))
        ttk.Label(holder, text="  입력한 대역을 이 크기로 나눠 한 번에 생성", style="SurfaceMuted.TLabel",
                  font=self.theme.font_small).pack(side="left")
        self.add_row(2, "분할 단위", holder)
        labels = [NO_GROUP] + [path for _, path in self.groups]
        default = next((path for gid, path in self.groups if gid == self.default_group), NO_GROUP)
        self.group_var = tk.StringVar(self, value=default)
        group_combo = ttk.Combobox(body, textvariable=self.group_var, state="readonly", values=labels,
                                   font=self.theme.font)
        self.theme.track_combobox(group_combo)
        self.add_row(3, "그룹", group_combo)
        desc, self.desc_var = self.entry("")
        self.add_row(4, "대역 설명", desc)
        self.preview = ttk.Label(body, text="", style="SurfaceMuted.TLabel", justify="left", wraplength=px(520))
        self.preview.grid(row=5, column=0, columnspan=2, sticky="w", pady=(px(8), 0))
        self.cidr_var.trace_add("write", lambda *_: self._on_cidr())
        self.split_var.trace_add("write", lambda *_: self._update())
        self._update()

    def _on_cidr(self) -> None:
        """CIDR이 바뀌면 고를 수 있는 분할 단위를 다시 만든다(/n+1 ~ /30)."""
        try:
            prefix = ipaddress.IPv4Network(self.cidr_var.get().strip(), strict=False).prefixlen \
                if "/" in self.cidr_var.get() else None
        except ValueError:
            prefix = None
        if prefix != self._prefix:
            self._prefix = prefix
            units = [f"/{p}" for p in range(max((prefix or 0) + 1, MIN_SUBNET_PREFIX), 31)] if prefix is not None else []
            values = ([NO_SPLIT] if prefix is None or prefix >= MIN_SUBNET_PREFIX else []) + units
            self.split.configure(values=values)
            # 사용자가 직접 고른 단위만 유지한다. 입력 도중(예: '10.0.0.0/1' → '/16') 자동으로 잡힌
            # 단위가 남아 원치 않게 분할되는 일이 없도록, 그 외에는 항상 '분할 안 함'을 기본으로 한다.
            if not (self._user_picked and self.split_var.get() in values):
                self._user_picked = False
                if NO_SPLIT in values:
                    self.split_var.set(NO_SPLIT)
                else:   # /16보다 큰 대역은 그대로 만들 수 없으므로 /24 단위를 제안
                    self.split_var.set("/24" if "/24" in values else values[0])
        self._update()

    def _compute(self) -> list:
        text = self.cidr_var.get().strip()
        if self.split_var.get() == NO_SPLIT:
            return [parse_cidr(text)]
        return split_cidr(text, int(self.split_var.get().lstrip("/")), MAX_SPLIT_SUBNETS)

    def _update(self) -> None:
        if not self.cidr_var.get().strip():
            self.preview.configure(text="CIDR을 입력하면 만들어질 대역이 여기에 표시됩니다.")
            return
        try:
            infos = self._compute()
        except ValueError as exc:
            self.preview.configure(text=f"⚠ {exc}")
            return
        ips = sum(i.host_count for i in infos)
        names = ", ".join(i.cidr for i in infos[:4]) + (f" … {infos[-1].cidr}" if len(infos) > 4 else "")
        fixed = " (입력값을 네트워크 주소로 보정)" if len(infos) == 1 and infos[0].normalized else ""
        self.preview.configure(text=f"→ 대역 {len(infos):,}개 · IP {ips:,}개 생성(미사용){fixed}\n   {names}")

    def validate(self) -> dict[str, Any]:
        infos = self._compute()   # 형식 오류 시 ValueError
        split = self.split_var.get() != NO_SPLIT
        group_id = next((gid for gid, path in self.groups if path == self.group_var.get()), None)
        return {"mode": "split" if split else "single", "cidr": self.cidr_var.get().strip(),
                "split_prefix": infos[0].prefix, "group_id": group_id,
                "description": self.desc_var.get().strip(), "populate": True}


class SubnetEditDialog(FormDialog):
    """대역 설명 수정."""

    def __init__(self, parent: tk.Misc, theme: ThemeManager, cidr: str, description: str) -> None:
        self.cidr_text, self.initial_desc = cidr, description
        super().__init__(parent, theme, "대역 정보 수정", ok_text="저장")

    def build(self, body: ttk.Frame) -> None:
        ro = ttk.Entry(body, style="ReadOnly.TEntry", font=self.theme.font)
        ro.insert(0, self.cidr_text)
        ro.configure(state="readonly")
        self.add_row(0, "CIDR", ro, hint="CIDR은 변경할 수 없습니다.")
        desc, self.desc_var = self.entry(self.initial_desc)
        self.add_row(2, "대역 설명", desc)

    def validate(self) -> dict[str, Any]:
        return {"description": self.desc_var.get().strip()}


class _ScrollRows(ttk.Frame):
    """세로 스크롤되는 행 컨테이너(IP별 입력용)."""

    def __init__(self, master: tk.Misc, theme: ThemeManager, height: int) -> None:
        super().__init__(master, style="Surface.TFrame")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=1, borderwidth=0, height=height,
                                background=theme.palette["surface"],
                                highlightbackground=theme.palette["border"])
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.inner = ttk.Frame(self.canvas, style="Surface.TFrame", padding=theme.px(6))
        self.inner.grid_columnconfigure(1, weight=1)
        window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        for widget in (self.canvas, self.inner):
            widget.bind("<MouseWheel>", self._wheel)

    def _wheel(self, event: tk.Event) -> str:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def ensure_visible(self, widget: tk.Widget) -> None:
        """포커스된 행이 보이도록 스크롤."""
        self.update_idletasks()
        total = max(self.inner.winfo_height(), 1)
        y = widget.winfo_y()
        top, bottom = self.canvas.yview()
        if y / total < top or (y + widget.winfo_height()) / total > bottom:
            self.canvas.yview_moveto(max(0.0, (y - self.canvas.winfo_height() / 3) / total))


class AssignDialog(FormDialog):
    """선택한 IP 일괄 등록(할당): 담당자·요청번호·비고는 공통, 호스트네임은 IP별."""

    def __init__(self, parent: tk.Misc, theme: ThemeManager, ips: list[tuple[int, str]]) -> None:
        self.ips = ips
        super().__init__(parent, theme, f"IP 등록 ({len(ips):,}개)", ok_text="등록", resizable_y=True)
        self.minsize(theme.px(520), theme.px(420))

    def build(self, body: ttk.Frame) -> None:
        px = self.theme.px
        body.grid_rowconfigure(6, weight=1)
        ttk.Label(body, text=f"선택한 미사용 IP {len(self.ips):,}개를 '사용중'으로 등록합니다. "
                             "변경일은 오늘 날짜로 자동 기록됩니다.", style="SurfaceMuted.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, px(6)))
        assignee, self.assignee_var = self.entry("")
        self.add_row(1, "담당자 *", assignee)
        request, self.request_var = self.entry("")
        self.add_row(2, "요청번호 *", request)
        self.note = self.note_box("", height=3, limit=MAX_NOTE_LEN)
        self.add_note_row(3, "비고", self.note)
        body.grid_rowconfigure(3, weight=0)
        assignee.bind("<Return>", lambda e: (request.focus_set(), "break")[1])
        request.bind("<Return>", lambda e: (self.note.text.focus_set(), "break")[1])
        ttk.Label(body, text="IP별 호스트네임", style="Section.TLabel").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(px(12), 0))
        ttk.Label(body, text="비고: Enter 줄바꿈 · Tab 다음 칸   |   호스트네임: Enter 다음 칸 · "
                             "엑셀에서 여러 줄을 복사해 붙여넣으면 아래 칸으로 차례로 채워집니다.",
                  style="SurfaceMuted.TLabel", font=self.theme.font_small).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(0, px(4)))
        visible = min(len(self.ips), 10)
        self.rows = _ScrollRows(body, self.theme, height=visible * (self.theme.row_height + px(4)) + px(14))
        self.rows.grid(row=6, column=0, columnspan=2, sticky="nsew")
        self.host_vars: list[tk.StringVar] = []
        self.host_entries: list[ttk.Entry] = []
        for i, (_ip_id, address) in enumerate(self.ips):
            ttk.Label(self.rows.inner, text=address, style="Surface.TLabel", width=16).grid(
                row=i, column=0, sticky="w", padx=(0, px(10)), pady=px(2))
            var = tk.StringVar(self)
            entry = ttk.Entry(self.rows.inner, textvariable=var, font=self.theme.font)
            entry.grid(row=i, column=1, sticky="ew", pady=px(2))
            entry.bind("<Return>", lambda e, n=i: self._next(n))
            entry.bind("<<Paste>>", lambda e, n=i: self._paste(n))
            entry.bind("<FocusIn>", lambda e, w=entry: self.rows.ensure_visible(w))
            entry.bind("<MouseWheel>", self.rows._wheel)
            self.host_vars.append(var)
            self.host_entries.append(entry)

    def _next(self, index: int) -> str:
        if index + 1 < len(self.host_entries):
            self.host_entries[index + 1].focus_set()
        else:
            self.ok_button.focus_set()
        return "break"

    def _paste(self, index: int) -> str | None:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return None
        lines = [line.split("\t")[0].strip() for line in text.replace("\r", "").split("\n")]
        while lines and not lines[-1]:
            lines.pop()
        if len(lines) <= 1:
            return None          # 한 줄이면 기본 붙여넣기
        for offset, value in enumerate(lines):
            if index + offset >= len(self.host_vars):
                break
            self.host_vars[index + offset].set(value)
        last = min(index + len(lines), len(self.host_entries)) - 1
        self.host_entries[last].focus_set()
        return "break"

    def validate(self) -> dict[str, Any]:
        if not self.assignee_var.get().strip():
            raise ValueError("담당자는 필수 입력 항목입니다.")
        if not self.request_var.get().strip():
            raise ValueError("요청번호는 필수 입력 항목입니다.")
        if len(self.note.get()) > MAX_NOTE_LEN:
            raise ValueError(f"비고는 최대 {MAX_NOTE_LEN:,}자까지 입력할 수 있습니다.")
        return {"assignee": self.assignee_var.get().strip(), "request_no": self.request_var.get().strip(),
                "note": self.note.get(),
                "hostnames": {ip_id: var.get().strip() for (ip_id, _a), var in zip(self.ips, self.host_vars)}}


class ReclaimDialog(FormDialog):
    """선택한 사용중 IP 일괄 회수."""

    def __init__(self, parent: tk.Misc, theme: ThemeManager, ips: list[tuple[int, str]]) -> None:
        self.ips = ips
        super().__init__(parent, theme, f"IP 회수 ({len(ips):,}개)", ok_text="회수")

    def build(self, body: ttk.Frame) -> None:
        px = self.theme.px
        ttk.Label(body, text=f"회수 대상 IP ({len(self.ips):,}개)", style="Section.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w")
        box = tk.Text(body, height=min(6, max(2, len(self.ips))), width=40, font=self.theme.font,
                      padx=px(6), pady=px(4))
        self.theme.style_text(box)
        box.configure(background=self.theme.palette["entry_ro"])
        box.insert("1.0", "\n".join(address for _i, address in self.ips))
        box.configure(state="disabled")
        box.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(px(4), px(8)))
        requester, self.requester_var = self.entry("")
        self.add_row(2, "회수 요청자 *", requester)
        request, self.request_var = self.entry("")
        self.add_row(3, "회수 요청번호 *", request)
        self.reason = self.note_box("", height=3, limit=MAX_NOTE_LEN)
        self.add_note_row(4, "회수 사유", self.reason)
        ttk.Label(body, text="⚠ 회수하면 호스트네임이 초기화되고 변경일은 오늘(회수일)로 기록되며,\n"
                             "   담당자/요청번호/비고는 회수 정보로 바뀝니다. 이전 값은 이력에 그대로 남습니다.",
                  style="SurfaceMuted.TLabel", foreground=self.theme.palette["warning"]).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(px(8), 0))

    def validate(self) -> dict[str, Any]:
        if not self.requester_var.get().strip():
            raise ValueError("회수 요청자는 필수 입력 항목입니다.")
        if not self.request_var.get().strip():
            raise ValueError("회수 요청번호는 필수 입력 항목입니다.")
        if len(self.reason.get()) > MAX_NOTE_LEN:
            raise ValueError(f"회수 사유는 최대 {MAX_NOTE_LEN:,}자까지 입력할 수 있습니다.")
        return {"requester": self.requester_var.get().strip(), "request_no": self.request_var.get().strip(),
                "reason": self.reason.get()}
