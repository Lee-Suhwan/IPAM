"""IPv4 / CIDR 계산 및 사용률 임계치 판정."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from ipmgr.constants import (LEVEL_CRITICAL, LEVEL_SAFE, LEVEL_WARNING, MAX_CREATE_IPS, MIN_SUBNET_PREFIX,
                            THRESHOLD_CRITICAL, THRESHOLD_WARNING)

_PARTIAL_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){0,3}\.?$")
_NAT_RE = re.compile(r"(\d+)")


@dataclass(frozen=True)
class SubnetInfo:
    """정규화된 서브넷 정보."""
    cidr: str
    network_int: int
    broadcast_int: int
    prefix: int
    normalized: bool = False   # 입력에 호스트 비트가 있어 네트워크 주소로 보정됨

    @property
    def host_first(self) -> int:
        """첫 호스트 IP 정수값(/31, /32는 전체 포함)."""
        return self.network_int if self.prefix >= 31 else self.network_int + 1

    @property
    def host_last(self) -> int:
        """마지막 호스트 IP 정수값."""
        return self.broadcast_int if self.prefix >= 31 else self.broadcast_int - 1

    @property
    def host_count(self) -> int:
        """호스트 IP 수."""
        return self.host_last - self.host_first + 1


def int_to_ip(value: int) -> str:
    """정수 → 점 표기 IPv4 문자열."""
    return f"{(value >> 24) & 255}.{(value >> 16) & 255}.{(value >> 8) & 255}.{value & 255}"


def ip_to_int(text: str) -> int:
    """점 표기 IPv4 문자열 → 정수. 형식 오류 시 ValueError."""
    try:
        return int(ipaddress.IPv4Address(text.strip()))
    except (ipaddress.AddressValueError, ValueError) as exc:
        raise ValueError(f"올바른 IPv4 주소가 아닙니다: {text!r}") from exc


def parse_cidr(text: str, min_prefix: int = MIN_SUBNET_PREFIX) -> SubnetInfo:
    """CIDR 문자열을 검증·정규화한다. (예: '10.1.2.7/24' → 10.1.2.0/24, normalized=True)"""
    raw = (text or "").strip()
    if "/" not in raw:
        raise ValueError("CIDR 형식(예: 10.1.2.0/24)으로 입력하세요.")
    try:
        net = ipaddress.IPv4Network(raw, strict=False)
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError) as exc:
        raise ValueError(f"올바른 CIDR이 아닙니다: {raw}") from exc
    if net.prefixlen < min_prefix:
        raise ValueError(f"하나의 대역은 /{min_prefix}까지 등록할 수 있습니다. "
                         f"더 큰 대역은 '분할 단위'를 골라 나눠서 등록하세요.")
    normalized = int(ipaddress.IPv4Address(raw.split("/")[0])) != int(net.network_address)
    return SubnetInfo(str(net), int(net.network_address), int(net.broadcast_address), net.prefixlen, normalized)


def split_cidr(text: str, new_prefix: int, max_count: int) -> list[SubnetInfo]:
    """큰 대역을 /new_prefix 단위 서브넷 목록으로 분할한다."""
    try:
        net = ipaddress.IPv4Network((text or "").strip(), strict=False)
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError) as exc:
        raise ValueError(f"올바른 CIDR이 아닙니다: {text}") from exc
    if not MIN_SUBNET_PREFIX <= new_prefix <= 32:
        raise ValueError(f"분할 단위는 /{MIN_SUBNET_PREFIX} ~ /32 사이여야 합니다.")
    if new_prefix < net.prefixlen:
        raise ValueError(f"분할 단위(/{new_prefix})는 원본 대역(/{net.prefixlen})보다 작아야 합니다.")
    count = 2 ** (new_prefix - net.prefixlen)
    if count > max_count:
        raise ValueError(f"분할 결과가 {count:,}개로 한 번에 생성 가능한 {max_count:,}개를 초과합니다.")
    hosts = count * (2 ** (32 - new_prefix) - (0 if new_prefix >= 31 else 2))
    if hosts > MAX_CREATE_IPS:
        raise ValueError(f"한 번에 만들 IP가 {hosts:,}개로 최대 {MAX_CREATE_IPS:,}개를 초과합니다. "
                         f"대역을 나눠서 여러 번 등록하세요.")
    return [SubnetInfo(str(s), int(s.network_address), int(s.broadcast_address), s.prefixlen)
            for s in net.subnets(new_prefix=new_prefix)]


def ip_prefix_range(text: str) -> tuple[int, int] | None:
    """검색어가 IP/IP 접두어/CIDR이면 (시작, 끝) 정수 범위를 반환한다.

    '10.1' → 10.1.0.0~10.1.255.255, '10.1.2.3' → 단일 IP, '10.1.0.0/16' → 해당 대역.
    """
    q = (text or "").strip()
    if "/" in q:
        try:
            net = ipaddress.IPv4Network(q, strict=False)
        except ValueError:
            return None
        return int(net.network_address), int(net.broadcast_address)
    if not _PARTIAL_IP_RE.match(q):
        return None
    parts = [p for p in q.rstrip(".").split(".") if p != ""]
    if len(parts) < 2 or any(int(p) > 255 for p in parts):
        return None                      # 숫자 하나만으로는 IP 접두어로 보지 않음
    octets = [int(p) for p in parts]
    start = sum(o << (24 - 8 * i) for i, o in enumerate(octets))
    span = 2 ** (8 * (4 - len(octets)))
    return start, start + span - 1


def usage_percent(used: int, total: int) -> float:
    """사용률(%)을 소수 1자리로 반환한다."""
    return round(used * 100.0 / total, 1) if total else 0.0


def usage_level(percent: float) -> str:
    """임계치 정책에 따른 단계: 경고 > 80%, 위험 > 90% (화면에 보이는 소수 1자리 값 기준)."""
    if percent > THRESHOLD_CRITICAL:
        return LEVEL_CRITICAL
    if percent > THRESHOLD_WARNING:
        return LEVEL_WARNING
    return LEVEL_SAFE


def natural_key(text: str) -> list:
    """'10.2' < '10.10' 처럼 숫자를 숫자로 비교하는 정렬 키."""
    return [int(t) if t.isdigit() else t.lower() for t in _NAT_RE.split(text or "")]
