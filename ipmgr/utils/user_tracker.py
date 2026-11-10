"""AD 계정 기반 작업자 식별.

Win32 `GetUserNameExW(NameDisplay=3)`로 AD DisplayName을 조회해 한글 이름만 추출하고,
로컬 IPv4 주소와 결합해 `홍길동(192.168.1.55)` 형태의 작업자 문자열을 만든다.
"""
from __future__ import annotations

import getpass
import os
import re
import socket
import sys
from functools import lru_cache

NAME_DISPLAY = 3
_HANGUL_RE = re.compile(r"[가-힣]+")


def _get_ad_display_name() -> str:
    """AD DisplayName을 반환한다. 도메인 미가입/비Windows 환경이면 빈 문자열."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        func = ctypes.windll.secur32.GetUserNameExW
        func.argtypes = [ctypes.c_int, wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG)]
        func.restype = wintypes.BOOLEAN
        size = wintypes.ULONG(0)
        func(NAME_DISPLAY, None, ctypes.byref(size))          # 필요한 버퍼 크기 조회
        if size.value == 0:
            return ""
        buf = ctypes.create_unicode_buffer(size.value + 1)
        if func(NAME_DISPLAY, buf, ctypes.byref(size)):
            return buf.value.strip()
    except Exception:  # noqa: BLE001 - 어떤 환경에서도 앱 기동을 막지 않는다
        pass
    return ""


def extract_korean_name(text: str) -> str:
    """문자열에서 한글 이름을 추출한다.

    `홍길동/부장/IT팀`, `홍길동 (인프라팀)` 등에서 2자 이상인 첫 한글 토큰을 이름으로 본다.
    """
    tokens = _HANGUL_RE.findall(text or "")
    for token in tokens:
        if len(token) >= 2:
            return token
    return tokens[0] if tokens else ""


def get_local_ipv4() -> str:
    """기본 경로 인터페이스의 IPv4 주소를 반환한다(패킷은 전송하지 않음)."""
    for target in ("10.255.255.255", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((target, 1))
                ip = sock.getsockname()[0]
                if ip and not ip.startswith("0."):
                    return ip
        except OSError:
            continue
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


@lru_cache(maxsize=1)
def get_operator_info() -> str:
    """`이름(IPv4)` 형태의 작업자 식별 문자열을 반환한다(프로세스 내 캐시)."""
    display = _get_ad_display_name()
    name = extract_korean_name(display)
    if not name:
        fallback = display or os.environ.get("USERNAME") or ""
        if not fallback:
            try:
                fallback = getpass.getuser()
            except Exception:  # noqa: BLE001
                fallback = "unknown"
        name = extract_korean_name(fallback) or fallback
    return f"{name}({get_local_ipv4()})"
