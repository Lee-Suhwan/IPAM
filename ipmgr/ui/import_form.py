"""불러오기 대화상자: ① 엑셀 파일 또는 텍스트 붙여넣기 → ② 미리보기(새로 입력 / 덮어쓰기 대상 / 오류)."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any

from ipmgr.logic import importer
from ipmgr.ui.theme_mgr import ThemeManager
from ipmgr.ui.widgets import FormDialog, scrolled_tree

MODE_FILE, MODE_TEXT = "file", "text"
TEXT_EXAMPLE = ("IP\t호스트네임\t담당자\t요청번호\t비고\n"
                "10.10.1.10\tweb01\t김민수\tREQ-2026-0412\t서비스 웹 서버\n"
                "10.10.1.11\tweb02\t김민수\tREQ-2026-0412\t")
MAX_PREVIEW = 3000


class ImportDialog(FormDialog):
    """불러올 내용 선택: 엑셀 파일 / 텍스트 붙여넣기."""

    def __init__(self, parent: tk.Misc, theme: ThemeManager) -> None:
        super().__init__(parent, theme, "불러오기", ok_text="미리보기 →", resizable_y=True)

    def build(self, body: ttk.Frame) -> None:
        px = self.theme.px
        self.mode = tk.StringVar(self, value=MODE_FILE)
        modes = ttk.Frame(body, style="Surface.TFrame")
        modes.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, px(8)))
        ttk.Radiobutton(modes, text="엑셀 파일 (.xlsx · .csv)", value=MODE_FILE, variable=self.mode,
                        command=self._switch).pack(side="left")
        ttk.Radiobutton(modes, text="텍스트 붙여넣기", value=MODE_TEXT, variable=self.mode,
                        command=self._switch).pack(side="left", padx=(px(16), 0))

        self.file_row = ttk.Frame(body, style="Surface.TFrame")
        self.file_row.grid_columnconfigure(0, weight=1)
        self.path_var = tk.StringVar(self)
        path = ttk.Entry(self.file_row, textvariable=self.path_var, width=48, font=self.theme.font)
        path.grid(row=0, column=0, sticky="ew")
        ttk.Button(self.file_row, text="찾아보기…", width=0, command=self._browse).grid(row=0, column=1,
                                                                                     padx=(px(6), 0))
        ttk.Label(self.file_row, text="다른 프로그램에서 내보낸 엑셀도 됩니다. 모든 시트에서 'IP' 머리글이 있는 표를 찾아 읽습니다.",
                  style="SurfaceMuted.TLabel", font=self.theme.font_small).grid(row=1, column=0, columnspan=2,
                                                                                sticky="w", pady=(px(4), 0))

        self.text_row = ttk.Frame(body, style="Surface.TFrame")
        self.text_row.grid_columnconfigure(0, weight=1)
        self.text_row.grid_rowconfigure(1, weight=1)
        ttk.Label(self.text_row, justify="left", style="SurfaceMuted.TLabel", font=self.theme.font_small,
                  text="불러올 내용을 복사해 아래에 붙여 넣으세요(엑셀에서 복사하면 탭으로 구분됩니다).\n"
                       "첫 줄에 머리글(IP · 호스트네임 · 담당자 · 요청번호 · 비고 · 상태 · 날짜)을 두면 열 순서를 맞춰 읽고,\n"
                       "머리글이 없으면 'IP, 호스트네임, 담당자, 요청번호, 비고' 순서로 읽습니다. 구분자: 탭 · 쉼표 · 공백").grid(
            row=0, column=0, sticky="w", pady=(0, px(4)))
        self.text = self.note_box("", height=12)
        self.text.text.configure(wrap="none", width=70)
        self.text.grid(in_=self.text_row, row=1, column=0, sticky="nsew")
        ttk.Button(self.text_row, text="예시 넣기", width=0,
                   command=lambda: self.text.set(TEXT_EXAMPLE)).grid(row=2, column=0, sticky="w", pady=(px(4), 0))
        self.first_focus = path
        self._switch()

    def _switch(self) -> None:
        px = self.theme.px
        if self.mode.get() == MODE_FILE:
            self.text_row.grid_remove()
            self.file_row.grid(row=1, column=0, columnspan=2, sticky="ew")
            self.body.grid_rowconfigure(1, weight=0)
        else:
            self.file_row.grid_remove()
            self.text_row.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(0, px(4)))
            self.body.grid_rowconfigure(1, weight=1)
            self.text.text.focus_set()

    def _browse(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="불러올 파일 선택",
                                          filetypes=[("엑셀 / CSV", "*.xlsx *.xlsm *.csv *.txt"), ("모든 파일", "*.*")])
        if path:
            self.path_var.set(path)

    def validate(self) -> dict[str, Any]:
        try:
            if self.mode.get() == MODE_FILE:
                path = self.path_var.get().strip().strip('"')
                if not path:
                    raise ValueError("불러올 파일을 선택하세요.")
                if not Path(path).exists():
                    raise ValueError(f"파일을 찾을 수 없습니다: {path}")
                rows = importer.parse_excel(path)
                source = f"파일 '{Path(path).name}'"
            else:
                text = self.text.get()
                if not text:
                    raise ValueError("붙여 넣은 내용이 없습니다.")
                rows = importer.parse_text(text)
                source = "텍스트 붙여넣기"
        except importer.ImportFormatError as exc:
            raise ValueError(str(exc)) from exc
        except PermissionError as exc:
            raise ValueError("파일이 다른 프로그램에서 열려 있거나 권한이 없습니다. 파일을 닫고 다시 시도하세요.") from exc
        if not rows:
            raise ValueError("IP 행을 찾지 못했습니다. 'IP' 머리글이나 IP 주소가 있는지 확인하세요.")
        return {"rows": rows, "source": source}


class ImportPreviewDialog(FormDialog):
    """미리보기: 무엇이 새로 들어가고, 무엇이 이미 값이 있고(덮어쓰기 대상), 무엇이 오류인지 보여 준다."""

    def __init__(self, parent: tk.Misc, theme: ThemeManager, plan: dict[str, Any], source: str) -> None:
        self.plan, self.source = plan, source
        n_apply = len(plan["new"]) + len(plan["dup"])
        super().__init__(parent, theme, "불러오기 미리보기", ok_text="불러오기" if n_apply else "닫기",
                         resizable_y=True)

    def build(self, body: ttk.Frame) -> None:
        px = self.theme.px
        p = self.plan
        ttk.Label(body, text=f"{self.source}  ·  대역 {p['subnets']:,}개", style="Section.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w")
        summary = (f"새로 입력 {len(p['new']):,}건   ·   이미 값이 있음(덮어쓰기 대상) {len(p['dup']):,}건   ·   "
                   f"변경 없음 {len(p['same']):,}건   ·   오류 {len(p['errors']):,}건")
        ttk.Label(body, text=summary, style="SurfaceMuted.TLabel").grid(row=1, column=0, columnspan=2, sticky="w",
                                                                         pady=(px(2), px(8)))
        cols = (("kind", "구분", 90, "w", False), ("where", "위치", 90, "w", False), ("ip", "IP", 110, "w", False),
                ("status", "상태", 60, "center", False), ("host", "호스트네임", 120, "w", True),
                ("assignee", "담당자", 80, "w", False), ("req", "요청번호", 110, "w", False),
                ("note", "비고", 140, "w", True), ("info", "현재 값 / 사유", 220, "w", True))
        frame, tree = scrolled_tree(body, self.theme, cols, selectmode="browse")
        tree.configure(height=14)
        frame.grid(row=2, column=0, columnspan=2, sticky="nsew")
        body.grid_rowconfigure(2, weight=1)
        pal = self.theme.palette
        tree.tag_configure("dup", foreground=pal["warning"])
        tree.tag_configure("err", foreground=pal["critical"])
        tree.tag_configure("same", foreground=pal["text_muted"])
        shown = 0

        def add(kind: str, row: Any, info: str, tag: str) -> None:
            nonlocal shown
            if shown >= MAX_PREVIEW:
                return
            v = row.values
            tree.insert("", "end", values=(kind, row.source, row.ip, v.get("status", ""), v.get("hostname", ""),
                                           v.get("assignee", ""), v.get("request_no", ""),
                                           (v.get("note", "") or "").replace("\n", " ⏎ "), info), tags=(tag,))
            shown += 1

        for row, why in p["errors"]:
            add("오류", row, why, "err")
        for row, cur in p["dup"]:
            now = " · ".join(x for x in (cur["status"], cur["hostname"], cur["assignee"], cur["request_no"]) if x)
            add("덮어쓰기 대상", row, f"현재: {now or '(비고만 있음)'}", "dup")
        for row in p["new"]:
            add("새로 입력", row, "", "")
        for row in p["same"]:
            add("변경 없음", row, "DB와 같음", "same")
        total = len(p["errors"]) + len(p["dup"]) + len(p["new"]) + len(p["same"])
        hint = "오류 행은 건너뜁니다." if p["errors"] else ""
        if p["dup"]:
            hint += (" 이미 값이 있는 IP는 [불러오기]를 누르면 덮어쓸지 다시 묻습니다." if hint else
                     "이미 값이 있는 IP는 [불러오기]를 누르면 덮어쓸지 다시 묻습니다.")
        if total > MAX_PREVIEW:
            hint += f"  (목록은 처음 {MAX_PREVIEW:,}건만 표시)"
        if hint:
            ttk.Label(body, text=hint.strip(), style="SurfaceMuted.TLabel", font=self.theme.font_small).grid(
                row=3, column=0, columnspan=2, sticky="w", pady=(px(6), 0))
        self.minsize(px(900), px(420))

    def validate(self) -> dict[str, Any]:
        return {"apply": bool(self.plan["new"] or self.plan["dup"])}
