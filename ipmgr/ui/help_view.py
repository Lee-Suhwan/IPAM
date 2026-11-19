"""도움말 창: 단축키와 마우스 사용법."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ipmgr.ui.theme_mgr import ThemeManager
from ipmgr.ui.widgets import center_over

SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("단축키", (
        ("F3  /  Ctrl+F", "IP 검색 — IP 주소 전체(예: 192.168.10.55)를 입력하면 그 IP로 이동"),
        ("F5", "새로고침(화면 다시 읽기 + DB 확인)"),
        ("F2", "선택한 IP 정보 수정"),
        ("Enter", "선택한 IP — 미사용은 등록, 사용중은 회수"),
        ("Esc", "IP 선택 해제(아래 이력이 대역 전체로) · 검색어 지우기"),
        ("Ctrl+E", "Excel 내보내기"),
        ("Ctrl+C", "선택한 행 복사(엑셀에 붙여넣기)"),
        ("Ctrl+A", "IP 목록 전체 선택"),
        ("Del", "트리에서 선택한 그룹·대역 삭제"),
        ("Ctrl+Enter", "비고 입력 중 저장(Enter는 줄바꿈)"),
    )),
    ("마우스", (
        ("더블클릭 (비고 칸)", "IP 정보 수정"),
        ("더블클릭 (그 밖의 칸)", "미사용 IP는 등록, 사용중 IP는 회수"),
        ("Ctrl / Shift + 클릭", "여러 개 선택(IP 목록 · 트리)"),
        ("우클릭 (IP 목록)", "여러 IP 한 번에 등록·회수, 정보 수정, 행 복사"),
        ("우클릭 (트리)", "그룹·대역 추가 · 수정 · 삭제, Excel 내보내기"),
        ("우클릭 (그룹 화면의 대역 표)", "IP 목록 열기, 대역 수정 · 삭제"),
        ("드래그 (트리)", "그룹·대역 이동 · 순서 변경"),
        ("비고에 마우스 올리기", "잘린 비고 전체 내용 보기"),
    )),
    ("찾기", (
        ("IP 검색 (툴바)", "IP 주소 전체를 입력하면 그 IP로 이동(F3 · Ctrl+F와 같음)"),
        ("트리 위 검색창", "192.168.10.  ·  192.168.10.0/24  ·  192.168.10.55 → 그 대역 / DMZ → 그룹·대역 이름"),
        ("전체 이력 (툴바)", "모든 이력을 최신순으로 · 호스트네임·담당자·요청번호·비고로 검색"),
    )),
)


class HelpDialog(tk.Toplevel):
    """단축키·마우스 사용법 안내(모달 아님)."""

    def __init__(self, parent: tk.Misc, theme: ThemeManager) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title("도움말")
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        theme.register_toplevel(self)
        px = theme.px
        outer = ttk.Frame(self, style="Surface.TFrame", padding=px(20))
        outer.pack(fill="both", expand=True)
        row = 0
        for title, items in SECTIONS:
            ttk.Label(outer, text=title, style="Section.TLabel").grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(px(10) if row else 0, px(4)))
            row += 1
            for key, desc in items:
                ttk.Label(outer, text=key, style="Surface.TLabel", font=theme.font_bold).grid(
                    row=row, column=0, sticky="w", padx=(px(8), px(18)), pady=px(2))
                ttk.Label(outer, text=desc, style="Surface.TLabel").grid(row=row, column=1, sticky="w", pady=px(2))
                row += 1
        ttk.Button(outer, text="닫기", style="Accent.TButton", command=self.destroy).grid(
            row=row, column=0, columnspan=2, sticky="e", pady=(px(16), 0))
        self.bind("<Escape>", lambda e: self.destroy())
        center_over(self, parent)
        self.deiconify()
        self.focus_set()
