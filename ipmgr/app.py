"""IP관리 단일 진입점: 반응형 윈도우 초기화, 단축키 바인딩, 결과 펌프, 변경 감지 폴링 루프.

실행:
    python -m ipmgr.app                       # 기본: <실행 폴더>/data_삭제금지/IP관리.db
    python -m ipmgr.app --data-dir data_stress  # 부하 테스트 DB로 실행
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import tkinter as tk
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import messagebox
from typing import Any, Callable

if __package__ in (None, ""):          # `python ipmgr/app.py` 또는 PyInstaller 진입 시
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ipmgr.constants import (APP_TITLE, DEFAULT_WINDOW_SIZE, MIN_WINDOW_SIZE, POLL_INTERVAL_MS,
                            UI_PUMP_MS)
from ipmgr.db.worker import DBWorker
from ipmgr.models.common import get_revisions
from ipmgr.utils import path_helper
from ipmgr.utils.user_tracker import get_operator_info

log = logging.getLogger("ipmgr")


def _setup_logging() -> None:
    """사용자 로컬 폴더에 회전 로그(공유 드라이브에 쓰지 않음)."""
    handler = RotatingFileHandler(path_helper.get_user_config_dir() / "app.log", maxBytes=1_000_000,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _enable_dpi_awareness() -> None:
    """고해상도 모니터에서 흐릿한 글꼴 방지(시스템 DPI 인식)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


class Settings:
    """사용자별 UI 설정(%LOCALAPPDATA%\\IP관리\\settings.json)."""

    def __init__(self) -> None:
        self.path = path_helper.get_user_config_dir() / "settings.json"
        try:
            self.data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def save(self) -> None:
        """설정 저장(실패해도 무시)."""
        try:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass


class App:
    """애플리케이션 수명 주기 관리."""

    def __init__(self, data_dir: str | None = None, backup: bool = True) -> None:
        path_helper.set_data_dir(data_dir)
        self.db_path = path_helper.get_db_path()
        self.settings = Settings()
        _enable_dpi_awareness()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_TITLE)
        self.root.report_callback_exception = self._on_tk_error

        from ipmgr.ui.theme_mgr import ThemeManager
        from ipmgr.ui.main_window import MainWindow

        # 기본은 Windows 시스템 테마를 따른다(사용자가 버튼으로 고정하면 그 값을 기억)
        # 예전 버전의 'theme_pref'는 버튼 한 번에 고정되던 값이라 읽지 않는다(한 번 시스템 따르기로 초기화)
        self.theme = ThemeManager(self.root, self.settings.data.get("theme_choice", "system"))
        self._init_geometry()
        self.worker = DBWorker(self.db_path)
        self.worker.start()
        self.operator = get_operator_info()
        self.window = MainWindow(self.root, self.theme, self.worker, self.operator, self.db_path,
                                 request_poll=self.poll_now)
        self._poll_inflight = False
        self._poll_again = False
        self._busy_since: float | None = None
        self._bind_shortcuts()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.deiconify()
        if self.settings.data.get("zoomed"):
            self.root.state("zoomed")
        self.theme.apply()                      # 창 표시 후 제목 표시줄 테마 적용

        self.window.start()
        self.poll_now()
        self.root.after(UI_PUMP_MS, self._pump)
        self.root.after(POLL_INTERVAL_MS, self._poll_loop)
        # 스키마 초기화(첫 작업)가 끝나면 빠른 무결성 검사 → 통과 시 하루 1회 정상 백업 갱신(백그라운드)
        self.window.auto_backup = backup
        self.worker.submit(lambda conn: None, silent=True,
                           on_success=lambda _r: self.window.run_quick_check(startup=True))

    # ── 창 ──────────────────────────────────────────────
    def _init_geometry(self) -> None:
        px = self.theme.px
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        min_w, min_h = min(px(MIN_WINDOW_SIZE[0]), sw - 40), min(px(MIN_WINDOW_SIZE[1]), sh - 80)
        self.root.minsize(min_w, min_h)
        saved = self.settings.data.get("geometry")
        if isinstance(saved, str) and "x" in saved:
            self.root.geometry(saved)
            return
        w, h = min(px(DEFAULT_WINDOW_SIZE[0]), sw - 40), min(px(DEFAULT_WINDOW_SIZE[1]), sh - 80)
        self.root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        if px(DEFAULT_WINDOW_SIZE[0]) > sw or px(DEFAULT_WINDOW_SIZE[1]) > sh:
            self.settings.data["zoomed"] = True

    def _bind_shortcuts(self) -> None:
        w = self.window

        def main_only(handler: Callable[[], Any]) -> Callable[[tk.Event], str | None]:
            def wrapped(event: tk.Event) -> str | None:
                try:
                    if event.widget.winfo_toplevel() is not self.root:
                        return None     # 대화상자 안에서는 무시
                except (tk.TclError, AttributeError):
                    return None
                handler()
                return "break"
            return wrapped

        for seq in ("<F3>", "<Control-f>", "<Control-F>"):
            self.root.bind_all(seq, main_only(w.open_search))
        self.root.bind_all("<F5>", main_only(w.refresh_all))
        self.root.bind_all("<Escape>", main_only(w.escape))
        for seq in ("<Control-e>", "<Control-E>"):
            self.root.bind_all(seq, main_only(w.export_excel))
        w.tree.tree.bind("<Delete>", lambda e: w.delete_node())

    # ── 루프 ────────────────────────────────────────────
    def _pump(self) -> None:
        """Worker/백그라운드 결과를 UI 스레드에서 처리(20ms 주기)."""
        try:
            self.worker.drain()
            self.window.drain_background()
            pending = self.worker.pending
            now = time.monotonic()
            if pending:
                self._busy_since = self._busy_since or now
                if now - self._busy_since > 0.3:
                    self.window.set_busy(True)
            elif self._busy_since is not None:
                self._busy_since = None
                self.window.set_busy(False)
        finally:
            self.root.after(UI_PUMP_MS, self._pump)

    def poll_now(self) -> None:
        """meta 리비전 1행 조회로 타 사용자 변경 감지."""
        if self._poll_inflight:
            self._poll_again = True
            return
        self._poll_inflight = True

        def done(revs: Any) -> None:
            self._poll_inflight = False
            if isinstance(revs, BaseException):
                self.window.set_connection(False, f"({type(revs).__name__})")
            else:
                self.window.on_revisions(revs)
            if self._poll_again:
                self._poll_again = False
                self.poll_now()

        self.worker.submit(get_revisions, on_success=done, on_error=done, silent=True)

    def _poll_loop(self) -> None:
        self.poll_now()
        self.theme.sync_system()          # Windows 테마를 바꾸면 실행 중에도 따라간다
        self.root.after(POLL_INTERVAL_MS, self._poll_loop)

    # ── 종료/오류 ───────────────────────────────────────
    def _on_tk_error(self, exc: type[BaseException], value: BaseException, tb: Any) -> None:
        log.error("UI 오류", exc_info=(exc, value, tb))
        messagebox.showerror("오류", f"예기치 않은 오류가 발생했습니다.\n{exc.__name__}: {value}\n\n"
                                   f"로그: {path_helper.get_user_config_dir() / 'app.log'}", parent=self.root)

    def close(self) -> None:
        """설정 저장 후 종료."""
        zoomed = self.root.state() == "zoomed"
        self.settings.data["zoomed"] = zoomed
        if not zoomed:
            self.settings.data["geometry"] = self.root.geometry()
        self.settings.data.pop("theme", None)
        self.settings.data.pop("theme_pref", None)
        self.settings.data["theme_choice"] = self.theme.preference
        self.settings.save()
        self.worker.stop(timeout=3.0)
        self.root.destroy()

    def run(self) -> None:
        """메인 루프 실행."""
        self.root.mainloop()


def main(argv: list[str] | None = None) -> int:
    """명령행 진입점."""
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--data-dir", help="데이터 폴더 지정(기본: 실행 폴더/data_삭제금지)")
    parser.add_argument("--no-backup", action="store_true", help="기동 시 자동 백업 생략")
    args = parser.parse_args(argv)
    _setup_logging()
    try:
        app = App(args.data_dir, backup=not args.no_backup)
    except Exception as exc:  # noqa: BLE001
        log.exception("기동 실패")
        try:
            messagebox.showerror(APP_TITLE, f"프로그램을 시작할 수 없습니다.\n{exc}")
        except tk.TclError:
            print(exc, file=sys.stderr)
        return 1
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
