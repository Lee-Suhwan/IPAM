"""IP 정보 수정 대화상자.

- 열기 직전 DB에서 재조회한 최신 레코드로 채운다(Pre-edit Fetch). 그리드와 버전이 다르면 안내 문구 표시.
- 상태(미사용↔사용중)는 [IP 등록]/[IP 회수]로만 바뀐다. 사용중 IP는 호스트네임·담당자·요청번호·비고,
  미사용 IP는 비고만 수정할 수 있다. 변경일(할당·회수 일자)은 자동 기록(입력 불가).
- 저장 시 version 비교(낙관적 잠금)는 Worker의 ip_model.update_ip가 수행한다.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from ipmgr.constants import MAX_NOTE_LEN, MAX_TEXT_LEN, STATUS_USED
from ipmgr.logic.state_machine import EDITABLE_FIELDS
from ipmgr.ui.theme_mgr import ThemeManager
from ipmgr.ui.widgets import FormDialog


class IPEditDialog(FormDialog):
    """단일 IP 정보 수정."""

    def __init__(self, parent: tk.Misc, theme: ThemeManager, record: dict[str, Any], notice: str = "") -> None:
        self.record = record
        self.notice = notice
        super().__init__(parent, theme, f"IP 정보 수정 — {record['ip_address']}", ok_text="저장", resizable_y=True)

    def build(self, body: ttk.Frame) -> None:
        px = self.theme.px
        rec = self.record
        used = rec["status"] == STATUS_USED
        head = ttk.Frame(body, style="Surface.TFrame")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, px(8)))
        ttk.Label(head, text=rec["ip_address"], style="SurfaceTitle.TLabel").pack(side="left")
        ttk.Label(head, text=f"● {rec['status']}", style="Used.Badge.TLabel" if used else "Unused.Badge.TLabel"
                  ).pack(side="left", padx=(px(10), 0))
        row = 1
        if self.notice:
            ttk.Label(body, text=self.notice, style="Info.Banner.TLabel", wraplength=px(420), justify="left").grid(
                row=row, column=0, columnspan=2, sticky="ew", pady=(0, px(8)))
            row += 1
        self.vars: dict[str, tk.StringVar] = {}
        entries: dict[str, Any] = {}
        for key, label in (("hostname", "호스트네임"), ("allocated_date", "변경일"), ("assignee", "담당자"),
                           ("request_no", "요청번호"), ("note", "비고")):
            if key == "note":
                self.note = self.note_box(str(rec.get("note") or ""), height=6, limit=MAX_NOTE_LEN)
                self.add_note_row(row, label, self.note)
                entries[key] = self.note.text
                row += 1
                continue
            entry, var = self.entry(str(rec.get(key) or ""))
            self.vars[key], entries[key] = var, entry
            if key == "allocated_date":
                entry.configure(state="readonly", style="ReadOnly.TEntry", takefocus=False)
                self.add_row(row, label, entry, hint="할당·회수한 날 자동 기록 (입력 불가)")
                row += 2
                continue
            if not used and key != "note":
                entry.configure(state="disabled")
            self.add_row(row, label, entry)
            row += 1
        self.first_focus = entries["hostname" if used else "note"]
        tip = "비고: Enter 줄바꿈 · Ctrl+Enter 저장"
        if not used:
            tip = "미사용 IP는 비고만 수정할 수 있습니다. 할당하려면 [IP 등록]을 사용하세요.\n" + tip
        ttk.Label(body, text=tip, style="SurfaceMuted.TLabel", font=self.theme.font_small, justify="left").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(px(4), 0))

    def validate(self) -> dict[str, Any]:
        values = {k: (self.note.get() if k == "note" else self.vars[k].get().strip()) for k in EDITABLE_FIELDS}
        for key, value in values.items():
            limit = MAX_NOTE_LEN if key == "note" else MAX_TEXT_LEN
            if len(value) > limit:
                raise ValueError(f"입력값이 너무 깁니다(최대 {limit}자).")
        if all(values[k] == str(self.record.get(k) or "") for k in EDITABLE_FIELDS):
            raise ValueError("변경된 내용이 없습니다.")
        return {"status": self.record["status"], **values}
