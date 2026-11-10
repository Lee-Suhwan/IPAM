"""다크/라이트 모드 컬러 팔레트 및 ttk.Style 동적 재구성 매니저.

- ttk 위젯은 Style 재구성만으로 즉시 반영된다.
- tk 위젯(Text, Canvas, Menu 등)과 Treeview 태그 색상은 `register(callback)`으로 등록된
  콜백이 테마 변경 시 다시 칠한다.
- Windows 10/11 제목 표시줄도 DWM API로 다크/라이트에 맞춘다.
"""
from __future__ import annotations

import sys
import tkinter as tk
import weakref
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

from ipmgr.constants import FONT_FAMILY, FONT_FAMILY_FALLBACK, FONT_SIZE, MIN_ROW_HEIGHT

Palette = dict[str, str]

PALETTES: dict[str, Palette] = {
    "light": {
        "bg": "#F3F5F9", "surface": "#FFFFFF", "surface_alt": "#F7F9FC", "toolbar": "#FFFFFF",
        "border": "#D8DEE8", "sash": "#E3E8EF",
        "text": "#1F2937", "text_muted": "#6B7280", "text_disabled": "#A0A8B5",
        "accent": "#2563EB", "accent_hover": "#1D4ED8", "accent_fg": "#FFFFFF",
        "button_bg": "#F1F4F8", "button_hover": "#E4E9F1", "button_pressed": "#D6DDE8",
        "entry_bg": "#FFFFFF", "entry_ro": "#F1F4F8",
        "select_bg": "#DCE8FD", "select_fg": "#0B1A33",
        "heading_bg": "#EEF2F7", "heading_hover": "#E2E8F0", "heading_fg": "#334155",
        "scroll_thumb": "#C9D1DD", "scroll_hover": "#AEB8C7",
        "safe": "#047857", "safe_bg": "#D1FAE5", "safe_tile": "#E5F7EF",
        "warning": "#B45309", "warning_bg": "#FEF3C7", "warning_tile": "#FDF0D2",
        "critical": "#B91C1C", "critical_bg": "#FEE2E2", "critical_tile": "#FDE3E3",
        "used": "#1D4ED8", "used_row": "#F2F7FF",
        "info_bg": "#E0ECFF", "info_fg": "#1E3A8A", "warn_bg": "#FFF4D6", "warn_fg": "#7C4A03",
        "drop_ok": "#BFDBFE", "drop_bad": "#FECACA",
        "timeline_line": "#CBD5E1", "create": "#2563EB", "delete": "#B91C1C", "modify": "#6B7280",
        "tooltip_bg": "#111827", "tooltip_fg": "#F9FAFB",
    },
    "dark": {
        "bg": "#1B1D22", "surface": "#24272E", "surface_alt": "#2A2E36", "toolbar": "#22252B",
        "border": "#3A3F4A", "sash": "#2E323B",
        "text": "#E5E7EB", "text_muted": "#9CA3AF", "text_disabled": "#5F6673",
        "accent": "#4F8CFF", "accent_hover": "#6B9FFF", "accent_fg": "#FFFFFF",
        "button_bg": "#2F333C", "button_hover": "#3A3F4A", "button_pressed": "#454B58",
        "entry_bg": "#1E2026", "entry_ro": "#2A2D34",
        "select_bg": "#2D4A7C", "select_fg": "#FFFFFF",
        "heading_bg": "#2C3038", "heading_hover": "#363B45", "heading_fg": "#D1D5DB",
        "scroll_thumb": "#454B57", "scroll_hover": "#5A6170",
        "safe": "#34D399", "safe_bg": "#123A2D", "safe_tile": "#173B30",
        "warning": "#FBBF24", "warning_bg": "#3D2E0C", "warning_tile": "#45360F",
        "critical": "#F87171", "critical_bg": "#45201F", "critical_tile": "#512526",
        "used": "#7AA8FF", "used_row": "#26303F",
        "info_bg": "#1E3A5F", "info_fg": "#DBEAFE", "warn_bg": "#4A3A12", "warn_fg": "#FDE68A",
        "drop_ok": "#1E4E8C", "drop_bad": "#6B1F1F",
        "timeline_line": "#3F4552", "create": "#6B9FFF", "delete": "#F87171", "modify": "#9CA3AF",
        "tooltip_bg": "#F3F4F6", "tooltip_fg": "#111827",
    },
}


class ThemeManager:
    """테마 상태와 스타일을 관리한다."""

    def __init__(self, root: tk.Tk, preference: str = "system") -> None:
        self.root = root
        # preference: "system"(Windows 설정 따라감, 기본) | "light" | "dark"(사용자가 직접 고정)
        self.preference = preference if preference in ("system", *PALETTES) else "system"
        self.mode = system_mode() if self.preference == "system" else self.preference
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        family = FONT_FAMILY if FONT_FAMILY in set(tkfont.families(root)) else FONT_FAMILY_FALLBACK
        self.family = family
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkTooltipFont",
                     "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont"):
            try:
                tkfont.nametofont(name).configure(family=family, size=FONT_SIZE)
            except tk.TclError:
                pass
        self.font = tkfont.Font(root, family=family, size=FONT_SIZE)
        self.font_bold = tkfont.Font(root, family=family, size=FONT_SIZE, weight="bold")
        self.font_small = tkfont.Font(root, family=family, size=FONT_SIZE - 1)
        self.font_small_bold = tkfont.Font(root, family=family, size=FONT_SIZE - 1, weight="bold")
        self.font_title = tkfont.Font(root, family=family, size=FONT_SIZE + 3, weight="bold")
        self.font_big = tkfont.Font(root, family=family, size=FONT_SIZE + 8, weight="bold")
        self.row_height = max(self.px(MIN_ROW_HEIGHT), self.font.metrics("linespace") + self.px(10))
        self._listeners: list[Callable[[Palette], None]] = []
        self._toplevels: weakref.WeakSet[tk.Misc] = weakref.WeakSet()
        self._comboboxes: weakref.WeakSet[ttk.Combobox] = weakref.WeakSet()
        self.apply()

    # ── 공개 API ──────────────────────────────────────────
    @property
    def palette(self) -> Palette:
        """현재 팔레트."""
        return PALETTES[self.mode]

    @property
    def is_dark(self) -> bool:
        """다크 모드 여부."""
        return self.mode == "dark"

    def px(self, value: float) -> int:
        """96 DPI 기준 픽셀을 현재 DPI에 맞게 환산한다."""
        return int(round(value * self.scale))

    def set_preference(self, preference: str) -> None:
        """'system' | 'light' | 'dark' 설정 후 즉시 적용."""
        if preference not in ("system", *PALETTES):
            return
        self.preference = preference
        self.set_mode(system_mode() if preference == "system" else preference)

    def sync_system(self) -> None:
        """시스템 테마를 따르는 중이면 Windows 설정 변경을 반영한다(주기 호출)."""
        if self.preference == "system":
            self.set_mode(system_mode())

    def set_mode(self, mode: str) -> None:
        """테마를 지정 모드로 변경하고 모든 위젯에 즉시 적용한다."""
        if mode in PALETTES and mode != self.mode:
            self.mode = mode
            self.apply()

    def register(self, callback: Callable[[Palette], None]) -> None:
        """테마 변경 콜백 등록(등록 즉시 1회 호출)."""
        self._listeners.append(callback)
        callback(self.palette)

    def register_toplevel(self, win: tk.Misc) -> None:
        """Toplevel 창 배경/제목 표시줄을 테마에 맞춘다."""
        self._toplevels.add(win)
        self._paint_toplevel(win)

    def track_combobox(self, combo: ttk.Combobox) -> None:
        """콤보박스 드롭다운 목록 색상을 테마에 맞춘다."""
        self._comboboxes.add(combo)
        self._paint_combobox(combo)

    def level_colors(self, level: str) -> tuple[str, str]:
        """임계치 단계별 (글자색, 배경색)."""
        p = self.palette
        return p[level], p[f"{level}_bg"]

    def style_text(self, widget: tk.Text) -> None:
        """tk.Text 위젯 색상 적용."""
        p = self.palette
        widget.configure(background=p["surface"], foreground=p["text"], insertbackground=p["text"],
                         selectbackground=p["select_bg"], selectforeground=p["select_fg"],
                         highlightthickness=0, borderwidth=0, relief="flat")

    def style_menu(self, menu: tk.Menu) -> None:
        """tk.Menu 색상 적용."""
        p = self.palette
        menu.configure(background=p["surface"], foreground=p["text"], activebackground=p["select_bg"],
                       activeforeground=p["select_fg"], disabledforeground=p["text_disabled"],
                       borderwidth=1, relief="solid", font=self.font)

    # ── 내부 ──────────────────────────────────────────────
    def apply(self) -> None:
        """현재 팔레트로 ttk 스타일 및 등록된 위젯을 갱신한다."""
        p = self.palette
        s = self.style
        px = self.px
        s.configure(".", background=p["bg"], foreground=p["text"], fieldbackground=p["entry_bg"],
                    bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"], troughcolor=p["bg"],
                    focuscolor=p["accent"], selectbackground=p["select_bg"], selectforeground=p["select_fg"],
                    insertcolor=p["text"], arrowcolor=p["text_muted"], font=self.font)
        s.map(".", foreground=[("disabled", p["text_disabled"])])

        for name, bg in (("TFrame", "bg"), ("Surface.TFrame", "surface"), ("Toolbar.TFrame", "toolbar"),
                         ("Status.TFrame", "toolbar")):
            s.configure(name, background=p[bg])
        s.configure("Card.TFrame", background=p["surface"], bordercolor=p["border"], relief="solid",
                    borderwidth=1)
        s.configure("Divider.TFrame", background=p["border"])

        s.configure("TLabel", background=p["bg"], foreground=p["text"])
        s.configure("Surface.TLabel", background=p["surface"], foreground=p["text"])
        s.configure("Muted.TLabel", background=p["bg"], foreground=p["text_muted"])
        s.configure("SurfaceMuted.TLabel", background=p["surface"], foreground=p["text_muted"])
        s.configure("Title.TLabel", background=p["bg"], foreground=p["text"], font=self.font_title)
        s.configure("SurfaceTitle.TLabel", background=p["surface"], foreground=p["text"], font=self.font_title)
        s.configure("Section.TLabel", background=p["surface"], foreground=p["heading_fg"], font=self.font_bold)
        s.configure("Big.TLabel", background=p["surface"], foreground=p["text"], font=self.font_big)
        s.configure("Toolbar.TLabel", background=p["toolbar"], foreground=p["text_muted"])
        s.configure("Status.TLabel", background=p["toolbar"], foreground=p["text_muted"], font=self.font_small)
        for level in ("safe", "warning", "critical"):
            s.configure(f"{level.title()}.Badge.TLabel", background=p[f"{level}_bg"], foreground=p[level],
                        font=self.font_small_bold, padding=(px(8), px(2)))
            s.configure(f"{level.title()}.Horizontal.TProgressbar", background=p[level],
                        troughcolor=p["surface_alt"], bordercolor=p["border"], lightcolor=p[level],
                        darkcolor=p[level], thickness=px(8))
        s.configure("Used.Badge.TLabel", background=p["select_bg"], foreground=p["used"],
                    font=self.font_small_bold, padding=(px(8), px(2)))
        s.configure("Unused.Badge.TLabel", background=p["button_bg"], foreground=p["text_muted"],
                    font=self.font_small_bold, padding=(px(8), px(2)))
        s.configure("Info.Banner.TLabel", background=p["info_bg"], foreground=p["info_fg"],
                    padding=(px(10), px(6)), font=self.font)
        s.configure("Warn.Banner.TLabel", background=p["warn_bg"], foreground=p["warn_fg"],
                    padding=(px(10), px(6)), font=self.font_bold)

        button_pad = (px(12), px(5))
        s.configure("TButton", background=p["button_bg"], foreground=p["text"], bordercolor=p["border"],
                    lightcolor=p["button_bg"], darkcolor=p["button_bg"], padding=button_pad,
                    focusthickness=1, focuscolor=p["accent"], anchor="center")
        s.map("TButton",
              background=[("disabled", p["button_bg"]), ("pressed", p["button_pressed"]),
                          ("active", p["button_hover"])],
              lightcolor=[("pressed", p["button_pressed"]), ("active", p["button_hover"])],
              darkcolor=[("pressed", p["button_pressed"]), ("active", p["button_hover"])],
              foreground=[("disabled", p["text_disabled"])])
        s.configure("Accent.TButton", background=p["accent"], foreground=p["accent_fg"],
                    bordercolor=p["accent"], lightcolor=p["accent"], darkcolor=p["accent"],
                    font=self.font_bold)
        s.map("Accent.TButton",
              background=[("disabled", p["button_bg"]), ("pressed", p["accent_hover"]),
                          ("active", p["accent_hover"])],
              lightcolor=[("active", p["accent_hover"])], darkcolor=[("active", p["accent_hover"])],
              foreground=[("disabled", p["text_disabled"])])
        s.configure("Toolbar.TButton", background=p["toolbar"], bordercolor=p["toolbar"],
                    lightcolor=p["toolbar"], darkcolor=p["toolbar"], padding=(px(10), px(5)))
        s.map("Toolbar.TButton",
              background=[("disabled", p["toolbar"]), ("pressed", p["button_pressed"]),
                          ("active", p["button_hover"])],
              bordercolor=[("active", p["border"])],
              lightcolor=[("active", p["button_hover"])], darkcolor=[("active", p["button_hover"])],
              foreground=[("disabled", p["text_disabled"])])

        for widget in ("TEntry", "TCombobox", "TSpinbox"):
            s.configure(widget, fieldbackground=p["entry_bg"], foreground=p["text"], bordercolor=p["border"],
                        lightcolor=p["entry_bg"], darkcolor=p["entry_bg"], insertcolor=p["text"],
                        padding=(px(6), px(4)), background=p["button_bg"], arrowcolor=p["text_muted"],
                        selectbackground=p["select_bg"], selectforeground=p["select_fg"])
            s.map(widget,
                  fieldbackground=[("readonly", "!focus", p["entry_bg"]), ("disabled", p["entry_ro"])],
                  foreground=[("disabled", p["text_disabled"])],
                  bordercolor=[("focus", p["accent"])], lightcolor=[("focus", p["accent"])],
                  background=[("active", p["button_hover"])])
        s.configure("ReadOnly.TEntry", fieldbackground=p["entry_ro"], foreground=p["text_muted"])
        s.map("ReadOnly.TEntry", fieldbackground=[("readonly", p["entry_ro"])],
              foreground=[("readonly", p["text_muted"])])
        s.map("TCombobox", fieldbackground=[("readonly", p["entry_bg"]), ("disabled", p["entry_ro"])],
              selectbackground=[("readonly", p["entry_bg"])], selectforeground=[("readonly", p["text"])],
              bordercolor=[("focus", p["accent"])], foreground=[("disabled", p["text_disabled"])])

        s.configure("Treeview", background=p["surface"], fieldbackground=p["surface"], foreground=p["text"],
                    rowheight=self.row_height, bordercolor=p["surface"], lightcolor=p["surface"],
                    darkcolor=p["surface"], borderwidth=0, font=self.font)
        s.map("Treeview", background=[("selected", p["select_bg"])], foreground=[("selected", p["select_fg"])])
        s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        s.configure("Treeview.Heading", background=p["heading_bg"], foreground=p["heading_fg"],
                    font=self.font_bold, relief="flat", bordercolor=p["border"], lightcolor=p["heading_bg"],
                    darkcolor=p["heading_bg"], padding=(px(8), px(6)))
        s.map("Treeview.Heading", background=[("active", p["heading_hover"])],
              lightcolor=[("active", p["heading_hover"])], darkcolor=[("active", p["heading_hover"])])
        s.configure("Treeview.Item", indicatorsize=px(11))

        for orient in ("Vertical", "Horizontal"):
            s.configure(f"{orient}.TScrollbar", background=p["scroll_thumb"], troughcolor=p["surface_alt"],
                        bordercolor=p["surface_alt"], lightcolor=p["scroll_thumb"], darkcolor=p["scroll_thumb"],
                        arrowcolor=p["text_muted"], gripcount=0, arrowsize=px(12))
            s.map(f"{orient}.TScrollbar", background=[("active", p["scroll_hover"])],
                  lightcolor=[("active", p["scroll_hover"])], darkcolor=[("active", p["scroll_hover"])])

        s.configure("TPanedwindow", background=p["sash"])
        s.configure("Sash", sashthickness=px(6), gripcount=0, background=p["sash"], lightcolor=p["sash"],
                    bordercolor=p["sash"])
        s.configure("TLabelframe", background=p["surface"], bordercolor=p["border"], lightcolor=p["surface"],
                    darkcolor=p["surface"], relief="solid", borderwidth=1)
        s.configure("TLabelframe.Label", background=p["surface"], foreground=p["heading_fg"], font=self.font_bold)
        for widget in ("TCheckbutton", "TRadiobutton"):
            s.configure(widget, background=p["surface"], foreground=p["text"], indicatorbackground=p["entry_bg"],
                        indicatorforeground=p["accent"], focuscolor=p["surface"], padding=px(2))
            s.map(widget, background=[("active", p["surface"])],
                  indicatorbackground=[("selected", p["entry_bg"]), ("active", p["button_hover"])])
        s.configure("TSeparator", background=p["border"])
        s.configure("Horizontal.TProgressbar", background=p["accent"], troughcolor=p["surface_alt"],
                    bordercolor=p["border"], lightcolor=p["accent"], darkcolor=p["accent"], thickness=px(8))

        r = self.root
        r.configure(background=p["bg"])
        for pattern, value in (("*TCombobox*Listbox.background", p["surface"]),
                               ("*TCombobox*Listbox.foreground", p["text"]),
                               ("*TCombobox*Listbox.selectBackground", p["select_bg"]),
                               ("*TCombobox*Listbox.selectForeground", p["select_fg"]),
                               ("*TCombobox*Listbox.font", self.font),
                               ("*Menu.background", p["surface"]), ("*Menu.foreground", p["text"]),
                               ("*Menu.activeBackground", p["select_bg"]),
                               ("*Menu.activeForeground", p["select_fg"])):
            r.option_add(pattern, value)

        for combo in list(self._comboboxes):
            self._paint_combobox(combo)
        self._paint_toplevel(r)
        for win in list(self._toplevels):
            self._paint_toplevel(win)
        alive = []
        for callback in self._listeners:
            try:
                callback(p)
                alive.append(callback)
            except tk.TclError:
                pass  # 이미 파괴된 위젯
        self._listeners = alive

    def _paint_combobox(self, combo: ttk.Combobox) -> None:
        p = self.palette
        try:
            popdown = combo.tk.eval(f"ttk::combobox::PopdownWindow {combo}")
            combo.tk.call(f"{popdown}.f.l", "configure", "-background", p["surface"], "-foreground", p["text"],
                          "-selectbackground", p["select_bg"], "-selectforeground", p["select_fg"])
        except tk.TclError:
            pass

    def _paint_toplevel(self, win: tk.Misc) -> None:
        try:
            win.configure(background=self.palette["bg"])
        except tk.TclError:
            return
        set_dark_titlebar(win, self.is_dark)


def system_mode() -> str:
    """Windows 앱 테마 설정('라이트'/'다크')을 읽는다. 읽을 수 없으면 'light'."""
    if sys.platform != "win32":
        return "light"
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if value else "dark"
    except OSError:
        return "light"


def set_dark_titlebar(win: tk.Misc, dark: bool) -> None:
    """Windows 10(1809+)/11 제목 표시줄을 다크/라이트로 전환한다(실패 시 무시)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (신/구 빌드)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                                          ctypes.sizeof(value)) == 0:
                break
        # SWP_NOMOVE|SWP_NOSIZE|SWP_NOZORDER|SWP_FRAMECHANGED → 제목 표시줄 즉시 다시 그리기
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0004 | 0x0020)
    except Exception:  # noqa: BLE001
        pass
