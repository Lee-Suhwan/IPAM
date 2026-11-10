"""전체 이력 패널: 선택한 IP(들)의 이력, 선택이 없으면 대역 전체 이력을 세로 카드 형태로 보여준다.

카드 예)
  ● 2026-11-02 00:41   할당   192.168.50.11
    작업자      이수환(192.168.123.157)
    상태        미사용  →  사용중
    호스트네임  (없음)  →  web02
"""
from __future__ import annotations

import re
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from ipmgr.constants import (ACT_ALLOCATE, ACT_INTEGRITY_REPAIR, ACT_REALLOCATE, ACT_RECLAIM, ACT_SUBNET_CREATE,
                             ACT_SUBNET_DELETE)
from ipmgr.logic.state_machine import FIELD_LABELS
from ipmgr.ui.theme_mgr import Palette, ThemeManager

ACTION_COLOR = {
    ACT_ALLOCATE: "safe", ACT_REALLOCATE: "safe", ACT_RECLAIM: "warning",
    ACT_SUBNET_DELETE: "delete", ACT_SUBNET_CREATE: "create",
    ACT_INTEGRITY_REPAIR: "delete",
}
# '할당일'은 v1.2 이전 이력의 라벨(현재는 '변경일')
_LABELS = "|".join(re.escape(v) for v in (*FIELD_LABELS.values(), "할당일"))
_DIFF_RE = re.compile(rf"^({_LABELS}): (.*) → (.*)$", re.S)
FIELD_ORDER = ["상태", "호스트네임", "담당자", "요청번호", "비고", "변경일", "할당일"]
DATE_LABELS = ("변경일", "할당일")
STATUS_ACTIONS = (ACT_ALLOCATE, ACT_REALLOCATE, ACT_RECLAIM)


def parse_diff(summary: str) -> list[tuple[str, str, str]] | None:
    """'상태: 미사용 → 사용중; 호스트네임: '' → 'web01'' → [(라벨, 이전, 이후)]. 형식이 다르면 None."""
    if not summary:
        return []
    result = []
    # 비고에 '; '가 들어 있어도 깨지지 않도록 '; 라벨: ' 앞에서만 나눈다
    for part in re.split(rf"; (?=(?:{_LABELS}): )", summary):
        m = _DIFF_RE.match(part)
        if not m:
            return None
        label, before, after = m.groups()
        result.append((label, _unquote(before), _unquote(after)))
    result.sort(key=lambda r: FIELD_ORDER.index(r[0]) if r[0] in FIELD_ORDER else 99)
    return result


def _unquote(value: str) -> str:
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] == "'" else value


class HistoryTimeline(ttk.Frame):
    """이력 패널."""

    def __init__(self, master: tk.Misc, theme: ThemeManager, on_order_change: Callable[[bool], None],
                 on_more: Callable[[], None]) -> None:
        super().__init__(master, style="Surface.TFrame", padding=(theme.px(16), theme.px(10)))
        self.theme = theme
        self.on_order_change = on_order_change
        self.on_more = on_more
        self.newest_first = True
        self._state: tuple[list[dict[str, Any]], str, bool, bool] | None = None
        px = theme.px

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        head = ttk.Frame(self, style="Surface.TFrame")
        head.grid(row=0, column=0, sticky="ew", pady=(0, px(6)))
        head.grid_columnconfigure(1, weight=1)
        ttk.Label(head, text="전체 이력", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.scope = ttk.Label(head, text="", style="SurfaceMuted.TLabel")
        self.scope.grid(row=0, column=1, sticky="w", padx=(px(12), 0))
        self.order_btn = ttk.Button(head, text="최신순 ▼", width=0, command=self._toggle_order)
        self.order_btn.grid(row=0, column=2, sticky="e")

        box = ttk.Frame(self, style="Surface.TFrame")
        box.grid(row=1, column=0, sticky="nsew")
        box.grid_rowconfigure(0, weight=1)
        box.grid_columnconfigure(0, weight=1)
        self.text = tk.Text(box, wrap="word", cursor="arrow", padx=px(6), pady=px(4), height=8,
                            font=theme.font)
        vsb = ttk.Scrollbar(box, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=vsb.set, state="disabled")
        self.text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        theme.register(self._apply_theme)

    def _apply_theme(self, p: Palette) -> None:
        t = self.text
        self.theme.style_text(t)
        px = self.theme.px
        indent = px(24)
        value_x = indent + max(self.theme.font.measure(label) for label in ("호스트네임", "요청번호", "작업자")) + px(22)
        t.tag_configure("head", font=self.theme.font_bold, spacing1=px(12), spacing3=px(4))
        t.tag_configure("first", spacing1=px(2))
        for key in ("safe", "warning", "delete", "create", "modify"):
            t.tag_configure(f"c_{key}", foreground=p[key])
        t.tag_configure("ip", foreground=p["text"], font=self.theme.font_bold)
        t.tag_configure("row", lmargin1=indent, lmargin2=value_x, tabs=(value_x,), spacing1=px(1),
                        spacing3=px(1))
        t.tag_configure("label", foreground=p["text_muted"])
        t.tag_configure("old", foreground=p["text_muted"])
        t.tag_configure("arrow", foreground=p["text_muted"])
        t.tag_configure("new", foreground=p["text"], font=self.theme.font_bold)
        t.tag_configure("plain", foreground=p["text"])
        t.tag_configure("empty", foreground=p["text_muted"], justify="center", spacing1=px(30))
        t.tag_configure("more", foreground=p["accent"], justify="center", spacing1=px(14),
                        font=self.theme.font_bold, underline=True)
        t.tag_bind("more", "<Button-1>", lambda e: self.on_more())
        t.tag_bind("more", "<Enter>", lambda e: t.configure(cursor="hand2"))
        t.tag_bind("more", "<Leave>", lambda e: t.configure(cursor="arrow"))
        if self._state is not None:
            self.render(*self._state)

    def _toggle_order(self) -> None:
        self.newest_first = not self.newest_first
        self.order_btn.configure(text="최신순 ▼" if self.newest_first else "과거순 ▲")
        self.on_order_change(self.newest_first)

    def clear(self, message: str = "") -> None:
        """비우기."""
        self._state = None
        self.scope.configure(text="")
        self._write([(f"\n{message}", ("empty",))] if message else [])

    def show_loading(self, scope: str) -> None:
        """조회 중 표시(이전 내용 유지)."""
        self.scope.configure(text=f"{scope}  ·  불러오는 중…")

    def render(self, rows: list[dict[str, Any]], scope: str, has_more: bool, show_ip: bool) -> None:
        """이력 카드 렌더링(rows는 현재 정렬 순서)."""
        self._state = (rows, scope, has_more, show_ip)
        more = "+" if has_more else ""
        self.scope.configure(text=f"{scope}  ·  {len(rows):,}{more}건")
        chunks: list[tuple[str, tuple[str, ...]]] = []
        if not rows:
            chunks.append(("\n기록된 이력이 없습니다.", ("empty",)))
        for i, e in enumerate(rows):
            color = ACTION_COLOR.get(e["action"], "modify")
            head = ("head", "first") if i == 0 else ("head",)
            chunks.append(("●  ", (*head, f"c_{color}")))
            chunks.append((f"{e['timestamp'][:16]}    ", head))
            chunks.append((e["action"], (*head, f"c_{color}")))
            if show_ip and e["ip_id"] is not None:
                chunks.append((f"    {e['ip_address']}", (*head, "ip")))
            elif e["ip_id"] is None and e["ip_address"]:
                chunks.append((f"    {e['ip_address']}", (*head, "label")))
            chunks.append(("\n", head))
            chunks.extend(self._kv("작업자", [(e["operator"] or "-", "plain")]))
            diff = parse_diff(e["diff_summary"])
            if diff is None:
                chunks.extend(self._kv("내용", [(e["diff_summary"], "plain")]))
            else:
                if e["action"] in STATUS_ACTIONS and not any(lbl in DATE_LABELS for lbl, _b, _a in diff) \
                        and e["allocated_date"]:
                    # 같은 날 회수 후 재할당 등 날짜가 그대로여도 언제 바뀌었는지 시각을 보여준다
                    diff.append(("변경일", e["allocated_date"], e["allocated_date"]))
                for label, before, after in diff:
                    if label in DATE_LABELS:
                        stamp = f" {e['timestamp'][11:16]}" if after and after == e["timestamp"][:10] else ""
                        if before == after:
                            chunks.extend(self._kv(label, [(after + stamp, "new")]))
                            continue
                        after = (after + stamp) if after else after
                    if "\n" in before + after:          # 여러 줄 비고: 이전 / 이후를 각각 블록으로
                        chunks.extend(self._kv(label, [(before or "(없음)", "old")]))
                        chunks.extend(self._kv("", [("→  ", "arrow"), (after or "(없음)", "new")]))
                        continue
                    chunks.extend(self._kv(label, [(before or "(없음)", "old"), ("  →  ", "arrow"),
                                                   (after or "(없음)", "new")]))
        if has_more:
            chunks.append(("\n더 보기 ▾\n", ("more",)))
        self._write(chunks)

    @staticmethod
    def _kv(label: str, parts: list[tuple[str, str]]) -> list[tuple[str, tuple[str, ...]]]:
        out: list[tuple[str, tuple[str, ...]]] = [(f"{label}\t", ("row", "label"))]
        # 값 안의 줄바꿈은 다음 줄도 값 열(탭 위치)에서 시작하도록
        out.extend((text.replace("\n", "\n\t"), ("row", tag)) for text, tag in parts)
        out.append(("\n", ("row",)))
        return out

    def _write(self, chunks: list[tuple[str, tuple[str, ...]]]) -> None:
        t = self.text
        y = t.yview()[0]
        keep = bool(self._state) and getattr(self, "_keep_scroll", False)
        t.configure(state="normal")
        t.delete("1.0", "end")
        for text, tags in chunks:
            t.insert("end", text, tags)
        t.configure(state="disabled")
        t.yview_moveto(y if keep else 0)
        self._keep_scroll = False

    def keep_scroll_once(self) -> None:
        """다음 렌더링에서 스크롤 위치 유지(더 보기/백그라운드 갱신용)."""
        self._keep_scroll = True
