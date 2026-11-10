"""그룹(또는 트리 다중 선택) 대시보드: 산하 대역 사용률 표.

로딩 속도를 위해 256개씩 표시하고, 더 있으면 [더 보기]로 이어서 불러온다.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from ipmgr.constants import LEVEL_LABELS, THRESHOLD_CRITICAL, THRESHOLD_WARNING
from ipmgr.logic.subnet_calc import usage_level, usage_percent
from ipmgr.ui.theme_mgr import Palette, ThemeManager
from ipmgr.ui.widgets import scrolled_tree

COLUMNS = (
    ("cidr", "서브넷 대역(CIDR)", 150, "w", False),
    ("desc", "대역 설명", 180, "w", True),
    ("total", "전체 IP", 80, "e", False),
    ("used", "사용중", 80, "e", False),
    ("free", "잔여", 80, "e", False),
    ("pct", "사용률", 80, "e", False),
    ("bar", "사용률 그래프", 130, "w", False),
    ("level", "임계치 상태", 100, "center", False),
)
BAR_CELLS = 10


def usage_bar(pct: float) -> str:
    """텍스트 프로그레스 바 (▰▱)."""
    filled = min(BAR_CELLS, int(round(pct / 100 * BAR_CELLS)))
    return "▰" * filled + "▱" * (BAR_CELLS - filled)


class SubnetDashboard(ttk.Frame):
    """대역 사용률 요약 뷰."""

    def __init__(self, master: tk.Misc, theme: ThemeManager, *, on_open_subnet: Callable[[int], None],
                 on_export: Callable[[], None], on_more: Callable[[bool], None]) -> None:
        super().__init__(master, style="TFrame")
        self.theme = theme
        self.on_open_subnet = on_open_subnet
        self.on_more = on_more
        self._data: dict[str, Any] | None = None
        px = theme.px

        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # ── 헤더 카드 ──
        card = ttk.Frame(self, style="Surface.TFrame", padding=(px(16), px(12)))
        card.grid(row=0, column=0, sticky="ew")
        card.grid_columnconfigure(0, weight=1)
        top = ttk.Frame(card, style="Surface.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        self.title = ttk.Label(top, text="", style="SurfaceTitle.TLabel")
        self.title.grid(row=0, column=0, sticky="w")
        self.badge = ttk.Label(top, text="", style="Safe.Badge.TLabel")
        self.badge.grid(row=0, column=1, sticky="e", padx=(px(8), px(8)))
        ttk.Button(top, text="📊 Excel 내보내기", width=0, command=on_export).grid(row=0, column=2, sticky="e")
        self.subtitle = ttk.Label(card, text="", style="SurfaceMuted.TLabel")
        self.subtitle.grid(row=1, column=0, sticky="w", pady=(px(2), px(8)))

        stats = ttk.Frame(card, style="Surface.TFrame")
        stats.grid(row=2, column=0, sticky="ew")
        self.stat_labels: dict[str, ttk.Label] = {}
        for i, (key, caption) in enumerate((("subnets", "대역"), ("total", "전체 IP"), ("used", "사용중 IP"),
                                            ("free", "잔여 IP"), ("pct", "사용률"))):
            stats.grid_columnconfigure(i, weight=1, uniform="stat")
            box = ttk.Frame(stats, style="Surface.TFrame")
            box.grid(row=0, column=i, sticky="w")
            ttk.Label(box, text=caption, style="SurfaceMuted.TLabel").pack(anchor="w")
            lbl = ttk.Label(box, text="-", style="Big.TLabel")
            lbl.pack(anchor="w")
            self.stat_labels[key] = lbl
        self.progress = ttk.Progressbar(card, maximum=100, style="Safe.Horizontal.TProgressbar")
        self.progress.grid(row=3, column=0, sticky="ew", pady=(px(10), 0))

        # ── 안내/범례 ──
        legend = ttk.Frame(self, padding=(px(16), px(8), px(16), px(6)))
        legend.grid(row=1, column=0, sticky="ew")
        legend.grid_columnconfigure(0, weight=1)
        self.notice = ttk.Label(legend, text="", style="Muted.TLabel")
        self.notice.grid(row=0, column=0, sticky="w")
        for i, (level, text) in enumerate((("safe", f"안전 ≤ {THRESHOLD_WARNING:g}%"),
                                           ("warning", f"경고 > {THRESHOLD_WARNING:g}%"),
                                           ("critical", f"위험 > {THRESHOLD_CRITICAL:g}%"))):
            ttk.Label(legend, text=text, style=f"{level.title()}.Badge.TLabel").grid(
                row=0, column=i + 1, padx=(px(6), 0))

        # ── 사용률 표 + 더 보기 ──
        table_frame, self.table = scrolled_tree(self, theme, COLUMNS, selectmode="browse",
                                                numeric={"total", "used", "free", "pct"})
        table_frame.grid(row=2, column=0, sticky="nsew")
        self.more_bar = ttk.Frame(self, style="Surface.TFrame", padding=(px(16), px(6)))
        self.more_bar.grid(row=3, column=0, sticky="ew")
        self.more_bar.grid_columnconfigure(0, weight=1)
        self.more_label = ttk.Label(self.more_bar, text="", style="SurfaceMuted.TLabel")
        self.more_label.grid(row=0, column=0, sticky="w")
        self.more_btn = ttk.Button(self.more_bar, text="더 보기", width=0, command=lambda: self.on_more(False))
        self.more_btn.grid(row=0, column=1, sticky="e")
        self.all_btn = ttk.Button(self.more_bar, text="모두 보기", width=0, command=lambda: self.on_more(True))
        self.all_btn.grid(row=0, column=2, sticky="e", padx=(px(6), 0))

        self.table.bind("<Double-Button-1>", self._open_from_table)
        self.table.bind("<Return>", self._open_from_table)
        theme.register(self._apply_theme)

    # ── 테마 ──────────────────────────────────────────────
    def _apply_theme(self, p: Palette) -> None:
        for level in ("safe", "warning", "critical"):
            self.table.tag_configure(level, foreground=p[level] if level != "safe" else p["text"],
                                     background=p[f"{level}_bg"] if level != "safe" else p["surface"])
        if self._data is not None:
            self._update_header()     # 사용률 강조색 재적용

    # ── 데이터 ────────────────────────────────────────────
    @property
    def path(self) -> str:
        """현재 표시 중인 제목(파일명 등에 사용)."""
        return self._data["path"] if self._data else ""

    @property
    def loaded(self) -> int:
        """현재까지 불러온 대역 수."""
        return len(self._data["subnets"]) if self._data else 0

    def load(self, data: dict[str, Any], append: bool = False) -> None:
        """대시보드 데이터 바인딩. append=True면 [더 보기]로 받은 페이지를 이어 붙인다."""
        if append and self._data is not None:
            self._data["subnets"].extend(data["subnets"])
            self._data["stats"] = data["stats"]
            self._append_rows(data["subnets"])
        else:
            self._data = data
            self._fill_table()
        self._update_header()

    def _update_header(self) -> None:
        data = self._data
        assert data is not None
        stats = data["stats"]
        total, used = stats["total"], stats["used"]
        pct = usage_percent(used, total)
        level = usage_level(pct)
        self.title.configure(text=data["title"])
        self.subtitle.configure(text=data.get("subtitle", ""))
        self.badge.configure(text=f"{LEVEL_LABELS[level]} {pct:.1f}%", style=f"{level.title()}.Badge.TLabel")
        self.stat_labels["subnets"].configure(text=f"{stats['subnet_count']:,}")
        self.stat_labels["total"].configure(text=f"{total:,}")
        self.stat_labels["used"].configure(text=f"{used:,}")
        self.stat_labels["free"].configure(text=f"{total - used:,}")
        self.stat_labels["pct"].configure(text=f"{pct:.1f}%", foreground=self.theme.palette[level])
        self.progress.configure(value=pct, style=f"{level.title()}.Horizontal.TProgressbar")
        shown, count = len(data["subnets"]), stats["subnet_count"]
        self.notice.configure(text=f"대역 {count:,}개 · 더블클릭하면 IP 목록으로 이동합니다.")
        if count > shown:
            self.more_label.configure(text=f"표시 {shown:,} / 전체 {count:,}")
            self.more_btn.configure(text=f"더 보기 (+{min(256, count - shown):,})")
            self.more_bar.grid()
        else:
            self.more_bar.grid_remove()

    def _row(self, s: dict[str, Any]) -> tuple[tuple, str]:
        pct = usage_percent(s["used_count"], s["total_count"])
        level = usage_level(pct)
        return (s["cidr"], s["description"], f"{s['total_count']:,}", f"{s['used_count']:,}",
                f"{s['total_count'] - s['used_count']:,}", f"{pct:.1f}%", usage_bar(pct), LEVEL_LABELS[level]), level

    def _append_rows(self, subnets: list[dict[str, Any]]) -> None:
        for s in subnets:
            if not self.table.exists(str(s["id"])):
                values, level = self._row(s)
                self.table.insert("", "end", iid=str(s["id"]), values=values, tags=(level,))

    def _fill_table(self) -> None:
        assert self._data is not None
        t = self.table
        selected = t.selection()
        yview = t.yview()[0]
        t.delete(*t.get_children())
        self._append_rows(self._data["subnets"])
        keep = [iid for iid in selected if t.exists(iid)]
        if keep:
            t.selection_set(keep)
        t.yview_moveto(yview)

    def _open_from_table(self, _event: tk.Event) -> None:
        sel = self.table.selection()
        if sel:
            self.on_open_subnet(int(sel[0]))
