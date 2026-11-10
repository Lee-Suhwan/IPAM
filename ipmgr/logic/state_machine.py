"""IP 2단계 상태 머신(미사용 ↔ 사용중)과 변경일(할당·회수 일자) 자동화, 변경 요약 생성.

DB 컬럼명은 호환을 위해 allocated_date 를 유지하지만 의미는 '상태 변경일'이다.
  사용중 + 날짜 = 할당일,  미사용 + 날짜 = 회수일,  미사용 + 빈 값 = 한 번도 쓰지 않은 IP
"""
from __future__ import annotations

from datetime import date
from typing import Mapping

from ipmgr.constants import (ACT_ALLOCATE, ACT_MODIFY, ACT_REALLOCATE, ACT_RECLAIM, MAX_NOTE_LEN,
                            MAX_TEXT_LEN, STATUS_UNUSED, STATUS_USED, STATUSES)

EDITABLE_FIELDS: tuple[str, ...] = ("hostname", "assignee", "request_no", "note")
FIELD_LABELS: dict[str, str] = {
    "status": "상태",
    "hostname": "호스트네임",
    "allocated_date": "변경일",
    "assignee": "담당자",
    "request_no": "요청번호",
    "note": "비고",
}


class TransitionError(ValueError):
    """허용되지 않은 상태 값."""


def validate_status(status: str) -> str:
    """상태 값이 '미사용' / '사용중' 중 하나인지 검증한다."""
    if status not in STATUSES:
        raise TransitionError(f"허용되지 않은 상태입니다: {status!r} (허용: {', '.join(STATUSES)})")
    return status


def resolve_changed_date(old_status: str | None, new_status: str, old_date: str,
                         today: date | None = None) -> str:
    """상태 전이에 따른 변경일을 계산한다(사용자 입력 불가).

    - 할당(→사용중) 또는 회수(→미사용)가 일어나는 순간: 오늘 날짜(YYYY-MM-DD)
    - 상태가 그대로면(정보 수정): 기존 변경일 유지
    """
    validate_status(new_status)
    if old_status != new_status:
        return (today or date.today()).isoformat()
    return old_date or ""


def determine_action(old_status: str, new_status: str, allocated_before: bool) -> str:
    """이력 작업 유형을 결정한다(할당/재할당/회수/정보 수정)."""
    if old_status != STATUS_USED and new_status == STATUS_USED:
        return ACT_REALLOCATE if allocated_before else ACT_ALLOCATE
    if old_status == STATUS_USED and new_status == STATUS_UNUSED:
        return ACT_RECLAIM
    return ACT_MODIFY


def normalize_fields(values: Mapping[str, str]) -> dict[str, str]:
    """입력값 공백 제거 및 길이 검증."""
    result: dict[str, str] = {}
    for key in EDITABLE_FIELDS:
        value = str(values.get(key, "") or "").strip()
        limit = MAX_NOTE_LEN if key == "note" else MAX_TEXT_LEN
        if len(value) > limit:
            raise ValueError(f"{FIELD_LABELS[key]}은(는) 최대 {limit}자까지 입력할 수 있습니다.")
        result[key] = value
    return result


def build_diff_summary(old: Mapping[str, str], new: Mapping[str, str]) -> str:
    """변경된 필드를 `상태: 미사용 → 사용중; 호스트네임: '' → 'web01'` 형태로 요약한다."""
    parts: list[str] = []
    for key in ("status", *EDITABLE_FIELDS, "allocated_date"):
        before, after = old.get(key, "") or "", new.get(key, "") or ""
        if before != after:
            label = FIELD_LABELS[key]
            if key == "status":
                parts.append(f"{label}: {before} → {after}")
            else:
                parts.append(f"{label}: '{before}' → '{after}'")
    return "; ".join(parts)
