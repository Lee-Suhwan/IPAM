"""UI 공용 위젯/헬퍼: 스크롤 Treeview, 컬럼 자동 폭, 정렬, 툴팁, 배너, 테마 적용 대화상자."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Sequence

from ipmgr.ui.theme_mgr import ThemeManager

# (컬럼 id, 헤더, 최소 폭(px@96dpi), 정렬, 늘어남 여부)
ColumnSpec = tuple[str, str, int, str, bool]


def scrolled_tree(parent: tk.Misc, theme: ThemeManager, columns: Sequence[ColumnSpec], *,
                  show: str = "headings", selectmode: str = "extended",
                  numeric: set[str] | None = None) -> tuple[ttk.Frame, ttk.Treeview]:
    """세로/가로 스크롤바가 붙은 Treeview를 만든다. 헤더 글자가 잘리지 않도록 폭을 보장한다."""
    frame = ttk.Frame(parent, style="Surface.TFrame")
    frame.grid_rowconfigure(0, weight=1)
    frame.grid_columnconfigure(0, weight=1)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show=show, selectmode=selectmode)
    vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    hsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    for col, heading, width, anchor, stretch in columns:
        need = theme.font_bold.measure(heading) + theme.px(34)   # 정렬 화살표/패딩 여유
        w = max(theme.px(width), need)
        tree.heading(col, text=heading, anchor=anchor if anchor != "e" else "e")
        tree.column(col, width=w, minwidth=need, anchor=anchor, stretch=stretch)
    make_sortable(tree, [c[0] for c in columns], [c[1] for c in columns], numeric or set())
    return frame, tree


def make_sortable(tree: ttk.Treeview, columns: Sequence[str], headings: Sequence[str],
                  numeric: set[str]) -> None:
    """헤더 클릭 시 오름/내림차순 정렬(숫자 컬럼은 숫자 비교)."""
    state: dict[str, Any] = {"col": None, "desc": False}

    def key_of(col: str, iid: str) -> Any:
        value = tree.set(iid, col)
        if col in numeric:
            cleaned = "".join(ch for ch in str(value) if ch.isdigit() or ch in ".-")
            try:
                return (0, float(cleaned))
            except ValueError:
                return (1, 0.0)
        if col in ("ip", "ip_address", "cidr"):
            head = str(value).split("/")[0].split(".")
            try:
                return (0, tuple(int(x) for x in head))
            except ValueError:
                return (1, ())
        return (0, str(value).lower())

    def sort(col: str) -> None:
        desc = not state["desc"] if state["col"] == col else False
        state.update(col=col, desc=desc)
        items = list(tree.get_children(""))
        items.sort(key=lambda iid: key_of(col, iid), reverse=desc)
        for index, iid in enumerate(items):
            tree.move(iid, "", index)
        for c, h in zip(columns, headings):
            arrow = (" ▼" if desc else " ▲") if c == col else ""
            tree.heading(c, text=h + arrow)

    for col in columns:
        tree.heading(col, command=lambda c=col: sort(c))


class Tooltip:
    """마우스 위치 근처에 표시되는 경량 툴팁."""

    def __init__(self, master: tk.Misc, theme: ThemeManager) -> None:
        self.master = master
        self.theme = theme
        self.win: tk.Toplevel | None = None
        self.label: tk.Label | None = None

    def show(self, x_root: int, y_root: int, text: str) -> None:
        """화면 좌표에 툴팁 표시."""
        p = self.theme.palette
        if self.win is None:
            self.win = tk.Toplevel(self.master)
            self.win.wm_overrideredirect(True)
            self.win.attributes("-topmost", True)
            self.label = tk.Label(self.win, justify="left", font=self.theme.font_small, wraplength=self.theme.px(520),
                                  padx=self.theme.px(8), pady=self.theme.px(5))
            self.label.pack()
        assert self.label is not None
        self.label.configure(text=text, background=p["tooltip_bg"], foreground=p["tooltip_fg"])
        self.win.wm_geometry(f"+{x_root + 14}+{y_root + 16}")
        self.win.deiconify()

    def hide(self) -> None:
        """툴팁 숨김."""
        if self.win is not None:
            self.win.withdraw()


def one_line(text: str, limit: int = 120) -> str:
    """여러 줄 비고를 표(그리드)용 한 줄로: 줄바꿈은 ' ⏎ '로, 길면 말줄임."""
    flat = " ⏎ ".join(part.strip() for part in str(text or "").splitlines() if part.strip())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


def tsv_cell(value: Any) -> str:
    """엑셀 붙여넣기용 셀: 줄바꿈/탭/따옴표가 있으면 따옴표로 감싼다(엑셀이 한 셀로 인식)."""
    text = "" if value is None else str(value)
    if any(ch in text for ch in "\t\n\r\""):
        return '"' + text.replace('"', '""') + '"'
    return text


class NoteBox(ttk.Frame):
    """메모장처럼 여러 줄을 쓰는 비고 입력칸(세로 스크롤 + 글자 수 표시).

    Enter = 줄바꿈, Ctrl+Enter = 대화상자 확인, Tab = 다음 칸으로 이동.
    """

    def __init__(self, master: tk.Misc, theme: ThemeManager, value: str = "", height: int = 5,
                 limit: int = 0, on_submit: Callable[[], Any] | None = None) -> None:
        super().__init__(master, style="Surface.TFrame")
        self.theme = theme
        self.limit = limit
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        px = theme.px
        self.box = tk.Frame(self, highlightthickness=1)
        self.box.grid(row=0, column=0, sticky="nsew")
        self.box.grid_columnconfigure(0, weight=1)
        self.box.grid_rowconfigure(0, weight=1)
        self.text = tk.Text(self.box, height=height, width=36, wrap="word", undo=True, font=theme.font,
                            padx=px(6), pady=px(4))
        vsb = ttk.Scrollbar(self.box, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=vsb.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.counter = ttk.Label(self, style="SurfaceMuted.TLabel", font=theme.font_small)
        self.counter.grid(row=1, column=0, sticky="e")
        self.set(value)
        t = self.text
        t.bind("<Return>", lambda e: (t.insert("insert", "\n"), t.see("insert"), "break")[2])
        t.bind("<Control-Return>", lambda e: (on_submit() if on_submit else None, "break")[1])
        t.bind("<Tab>", lambda e: (t.tk_focusNext().focus_set(), "break")[1])
        t.bind("<Shift-Tab>", lambda e: (t.tk_focusPrev().focus_set(), "break")[1])
        t.bind("<<Modified>>", self._changed)
        t.bind("<FocusIn>", lambda e: self._paint(True))
        t.bind("<FocusOut>", lambda e: self._paint(False))
        theme.register(lambda _p: self._paint(self.focus_get() is t))

    def _paint(self, focused: bool) -> None:
        p = self.theme.palette
        self.theme.style_text(self.text)
        self.text.configure(background=p["entry_bg"])
        self.box.configure(background=p["entry_bg"], highlightbackground=p["border"],
                           highlightcolor=p["accent"], highlightthickness=1)
        self.box.configure(highlightbackground=p["accent"] if focused else p["border"])

    def _changed(self, _event: tk.Event | None = None) -> None:
        self.text.edit_modified(False)
        n = len(self.get())
        lines = self.get().count("\n") + 1 if self.get() else 0
        over = self.limit and n > self.limit
        self.counter.configure(text=f"{lines}줄 · {n:,}" + (f" / {self.limit:,}자" if self.limit else "자"),
                               foreground=self.theme.palette["critical" if over else "text_muted"])

    def get(self) -> str:
        """입력 내용(앞뒤 공백 줄 제거, 줄 끝 공백 제거)."""
        lines = [line.rstrip() for line in self.text.get("1.0", "end-1c").replace("\r", "").split("\n")]
        return "\n".join(lines).strip("\n").strip()

    def set(self, value: str) -> None:
        """내용 바꾸기."""
        self.text.delete("1.0", "end")
        self.text.insert("1.0", value or "")
        self.text.edit_reset()
        self._changed()

    def disable(self) -> None:
        """읽기 전용."""
        self.text.configure(state="disabled", foreground=self.theme.palette["text_disabled"])


class Banner(ttk.Label):
    """일정 시간 후 자동으로 사라지는 안내 배너."""

    def __init__(self, master: tk.Misc, **kwargs: Any) -> None:
        super().__init__(master, style="Info.Banner.TLabel", anchor="w", **kwargs)
        self._job: str | None = None
        self._grid_opts: dict[str, Any] | None = None

    def place_in_grid(self, **grid_opts: Any) -> None:
        """표시 위치(grid 옵션)를 기억하고 숨긴 상태로 시작."""
        self._grid_opts = grid_opts

    def show(self, text: str, kind: str = "info", timeout_ms: int = 8000) -> None:
        """배너 표시(kind: info | warn)."""
        self.configure(text=text, style="Warn.Banner.TLabel" if kind == "warn" else "Info.Banner.TLabel")
        if self._grid_opts is not None:
            self.grid(**self._grid_opts)
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(timeout_ms, self.hide) if timeout_ms else None

    def hide(self) -> None:
        """배너 숨김."""
        self._job = None
        if self._grid_opts is not None:
            self.grid_remove()


def center_over(win: tk.Toplevel, parent: tk.Misc) -> None:
    """부모 창 중앙에 배치(화면 밖으로 나가지 않게 보정)."""
    win.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    px_, py_ = parent.winfo_rootx(), parent.winfo_rooty()
    pw, ph = parent.winfo_width(), parent.winfo_height()
    x = max(0, min(px_ + (pw - w) // 2, win.winfo_screenwidth() - w))
    y = max(0, min(py_ + (ph - h) // 3, win.winfo_screenheight() - h - 40))
    win.geometry(f"+{x}+{y}")


class FormDialog(tk.Toplevel):
    """테마가 적용된 모달 입력 대화상자 기반 클래스.

    하위 클래스는 `build(body)`에서 입력 위젯을 만들고 `validate()`에서 결과(dict)를 반환한다.
    """

    def __init__(self, parent: tk.Misc, theme: ThemeManager, title: str, ok_text: str = "확인",
                 resizable_y: bool = False) -> None:
        # 주의: 하위 클래스는 self._name 을 쓰면 안 된다(Tkinter 내부 위젯 이름 속성과 충돌)
        super().__init__(parent)
        self.withdraw()
        self.theme = theme
        self.result: dict[str, Any] | None = None
        self.title(title)
        self.transient(parent.winfo_toplevel())
        self.resizable(True, resizable_y)
        theme.register_toplevel(self)
        outer = ttk.Frame(self, style="Surface.TFrame", padding=theme.px(18))
        outer.pack(fill="both", expand=True)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(0, weight=1)
        self.body = ttk.Frame(outer, style="Surface.TFrame")
        self.body.grid(row=0, column=0, sticky="nsew")
        self.body.grid_columnconfigure(1, weight=1)
        self.error = ttk.Label(outer, style="SurfaceMuted.TLabel", foreground=theme.palette["critical"],
                               wraplength=theme.px(460), justify="left")
        self.error.grid(row=1, column=0, sticky="w", pady=(theme.px(8), 0))
        buttons = ttk.Frame(outer, style="Surface.TFrame")
        buttons.grid(row=2, column=0, sticky="e", pady=(theme.px(12), 0))
        ttk.Button(buttons, text="취소", command=self.destroy).pack(side="right")
        self.ok_button = ttk.Button(buttons, text=ok_text, style="Accent.TButton", command=self._on_ok)
        self.ok_button.pack(side="right", padx=(0, theme.px(8)))
        self.first_focus: tk.Widget | None = None
        self.build(self.body)
        self.bind("<Return>", lambda e: self._on_ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.minsize(theme.px(440), 1)
        center_over(self, parent)
        self.deiconify()
        self.grab_set()
        (self.first_focus or self.ok_button).focus_set()

    # 하위 클래스 구현 ──────────────────────────────────────
    def build(self, body: ttk.Frame) -> None:
        """입력 위젯 구성."""
        raise NotImplementedError

    def validate(self) -> dict[str, Any]:
        """입력 검증 후 결과 반환. 오류 시 ValueError."""
        raise NotImplementedError

    # 헬퍼 ──────────────────────────────────────────────────
    def add_row(self, row: int, label: str, widget: tk.Widget, hint: str = "") -> None:
        """'라벨 : 위젯' 한 줄 추가."""
        px = self.theme.px
        ttk.Label(self.body, text=label, style="Surface.TLabel").grid(
            row=row, column=0, sticky="w", padx=(0, px(12)), pady=px(5))
        widget.grid(row=row, column=1, sticky="ew", pady=px(5))
        if hint:
            ttk.Label(self.body, text=hint, style="SurfaceMuted.TLabel", font=self.theme.font_small).grid(
                row=row + 1, column=1, sticky="w")

    def note_box(self, value: str = "", height: int = 5, limit: int = 0) -> NoteBox:
        """여러 줄 비고 입력칸(Enter 줄바꿈 · Ctrl+Enter 확인)."""
        box = NoteBox(self.body, self.theme, value, height=height, limit=limit, on_submit=self._on_ok)
        if self.first_focus is None:
            self.first_focus = box.text
        return box

    def add_note_row(self, row: int, label: str, box: NoteBox) -> None:
        """라벨을 위쪽 정렬로 둔 여러 줄 입력 행."""
        px = self.theme.px
        ttk.Label(self.body, text=label, style="Surface.TLabel").grid(
            row=row, column=0, sticky="nw", padx=(0, px(12)), pady=(px(9), px(5)))
        box.grid(row=row, column=1, sticky="nsew", pady=px(5))
        self.body.grid_rowconfigure(row, weight=1)

    def entry(self, value: str = "", width: int = 36) -> tuple[ttk.Entry, tk.StringVar]:
        """StringVar 바인딩된 Entry 생성."""
        var = tk.StringVar(self, value=value)
        widget = ttk.Entry(self.body, textvariable=var, width=width, font=self.theme.font)
        if self.first_focus is None:
            self.first_focus = widget
        return widget, var

    def _on_ok(self) -> None:
        try:
            self.result = self.validate()
        except ValueError as exc:
            self.error.configure(text=f"⚠ {exc}")
            return
        self.destroy()

    def show(self) -> dict[str, Any] | None:
        """모달로 실행하고 결과(dict) 또는 None 반환."""
        self.wait_window()
        return self.result


def ask_form(dialog_cls: Callable[..., FormDialog], *args: Any, **kwargs: Any) -> dict[str, Any] | None:
    """FormDialog 하위 클래스를 띄우고 결과를 반환한다."""
    return dialog_cls(*args, **kwargs).show()
