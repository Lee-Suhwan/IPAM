"""불러오기: 엑셀 파일 또는 붙여 넣은 텍스트에서 IP 목록을 읽는다(읽기만, DB는 건드리지 않음).

- 엑셀(.xlsx/.xlsm): 다른 프로그램이 내보낸 파일. 모든 시트에서 'IP' 머리글이 있는 표를 찾아 읽는다.
- 텍스트(.csv/.txt 또는 붙여넣기): 파일로 열 수 없는 경우 내용을 복사해 붙여 넣는다.
  구분자는 탭 → 쉼표 → 공백 순으로 자동 판단한다.
- 첫 줄(또는 표 위쪽)에 머리글이 있으면 열 순서를 머리글로 맞추고,
  없으면 기본 순서 'IP, 호스트네임, 담당자, 요청번호, 비고'로 읽는다.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from ipmgr.constants import STATUS_UNUSED, STATUS_USED
from ipmgr.logic.subnet_calc import ip_to_int

FIELDS = ("ip", "status", "hostname", "assignee", "request_no", "note", "allocated_date")
DEFAULT_ORDER = ("ip", "hostname", "assignee", "request_no", "note")
FIELD_NAMES = {"ip": "IP", "status": "상태", "hostname": "호스트네임", "assignee": "담당자",
               "request_no": "요청번호", "note": "비고", "allocated_date": "날짜"}
# 머리글 인식 키워드(공백·대소문자 무시, 앞쪽 항목일수록 우선)
HEADER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "ip": ("ip주소", "ipaddress", "ip", "아이피", "주소"),
    "status": ("사용여부", "상태", "status", "사용유무", "할당여부"),
    "hostname": ("호스트네임", "호스트명", "호스트", "hostname", "host", "서버명", "장비명", "시스템명", "server"),
    "assignee": ("담당자", "관리자", "사용자", "assignee", "owner", "담당"),
    "request_no": ("요청번호", "요청", "request", "티켓", "ticket", "신청번호", "itsm", "itms"),
    "note": ("비고", "메모", "note", "용도", "설명", "remark", "description"),
    "allocated_date": ("할당일", "변경일", "등록일", "날짜", "일자", "date"),
}
USED_WORDS = ("사용중", "사용", "할당", "used", "y", "yes", "o", "1", "true")
UNUSED_WORDS = ("미사용", "회수", "빈", "미할당", "unused", "free", "n", "no", "x", "0", "false")
MAX_ROWS = 70000                    # 한 번에 불러오는 최대 행 수(/16 대역 하나 분량)
_DATE_RE = re.compile(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})")


class ImportFormatError(ValueError):
    """읽을 수 없는 형식."""


@dataclass
class ImportRow:
    """불러온 한 줄."""
    source: str                     # 위치(예: '3번째 줄', '서울IDC!12행')
    ip: str
    ip_id: int | None = None
    values: dict[str, str] = field(default_factory=dict)   # status/hostname/assignee/request_no/note/allocated_date
    error: str = ""


def _key(text: Any) -> str:
    return re.sub(r"[\s_\-()/·.]", "", str(text or "")).lower()


def _match_header(cell: Any) -> str | None:
    key = _key(cell)
    if not key or len(key) > 20:
        return None
    for name, words in HEADER_KEYWORDS.items():
        if key in words:
            return name
    for name, words in HEADER_KEYWORDS.items():      # '호스트네임(서버)' 처럼 덧붙은 경우
        if any(len(w) >= 2 and key.startswith(w) for w in words):
            return name
    return None


def _looks_like_ip(text: Any) -> bool:
    try:
        ip_to_int(str(text).split("/")[0])
        return True
    except ValueError:
        return False


# 이력(작업 기록) 표의 머리글 — 현재 상태가 아니므로 읽지 않는다(이 프로그램이 내보낸 '대역_전체이력' 시트 등)
HISTORY_MARKERS = ("작업유형", "변경상세요약", "이전상태", "변경상태")


def _header_map(row: Sequence[Any]) -> dict[str, int] | None:
    """머리글 행이면 {필드: 열 번호}(이력 표 머리글이면 빈 dict). IP 열이 있고 그 칸이 IP 값이 아니어야 머리글."""
    found: dict[str, int] = {}
    keys = {_key(c) for c in row}
    for col, cell in enumerate(row):
        if _looks_like_ip(cell):
            return None
        name = _match_header(cell)
        if name and name not in found:
            found[name] = col
    if "ip" not in found or len(found) < 2:
        return None
    return {} if keys & set(HISTORY_MARKERS) else found


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalize_status(text: str, has_data: bool) -> str | None:
    """상태 글자 → '사용중'/'미사용'. 비어 있으면 값 유무로 판단, 알 수 없으면 None."""
    key = _key(text).rstrip("?")
    if not key:
        return STATUS_USED if has_data else STATUS_UNUSED
    if key in UNUSED_WORDS or key.startswith("미"):
        return STATUS_UNUSED
    if key in USED_WORDS or "사용" in key:
        return STATUS_USED
    return None


def normalize_date(text: str) -> str | None:
    """'2026-3-2', '2026.03.02', '2026/03/02 10:00' → '2026-03-02'. 빈 값은 '', 형식 오류는 None."""
    text = (text or "").strip()
    if not text:
        return ""
    m = _DATE_RE.match(text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def parse_table(rows: Iterable[Sequence[Any]], label: str = "") -> list[ImportRow]:
    """표(행 목록)에서 IP 행을 읽는다. 머리글이 나오기 전의 제목·빈 줄은 건너뛴다."""
    result: list[ImportRow] = []
    mapping: dict[str, int] | None = None
    prefix = f"{label}!" if label else ""
    for n, raw in enumerate(rows, 1):
        cells = [_cell(v) for v in raw]
        if not any(cells):
            continue
        header = _header_map(cells)
        if header is not None:
            mapping = header
            continue
        if mapping == {}:
            continue                                  # 이력 표 아래의 행은 건너뜀
        cols = mapping or {name: i for i, name in enumerate(DEFAULT_ORDER)}
        ip_text = cells[cols["ip"]] if cols["ip"] < len(cells) else ""
        where = f"{prefix}{n}행" if label else f"{n}번째 줄"
        if not _looks_like_ip(ip_text):
            if mapping is not None and ip_text:
                result.append(ImportRow(where, ip_text, error="IP 형식이 아닙니다"))
            continue                                  # 머리글 이전의 제목·설명 줄
        row = ImportRow(where, ip_text.split("/")[0].strip())
        row.ip_id = ip_to_int(row.ip)
        for name in FIELDS[1:]:
            col = cols.get(name)
            row.values[name] = cells[col] if col is not None and col < len(cells) else ""
        _finish(row)
        result.append(row)
        if len(result) > MAX_ROWS:
            raise ImportFormatError(f"한 번에 {MAX_ROWS:,}행까지 불러올 수 있습니다. 대역별로 나눠 불러오세요.")
    return result


def _finish(row: ImportRow) -> None:
    v = row.values
    has_data = any(v.get(k) for k in ("hostname", "assignee", "request_no"))
    status = normalize_status(v.get("status", ""), has_data)
    if status is None:
        row.error = f"상태 값을 알 수 없습니다: {v.get('status')!r} (사용중/미사용)"
        return
    v["status"] = status
    day = normalize_date(v.get("allocated_date", ""))
    if day is None:
        row.error = f"날짜 형식이 아닙니다: {v.get('allocated_date')!r} (예: 2026-03-02)"
        return
    v["allocated_date"] = day
    if status == STATUS_UNUSED:                     # 미사용 IP는 비고만 남긴다(호스트네임 등은 비움)
        v.update(hostname="", assignee="", request_no="")


def _split_line(line: str, sep: str | None) -> list[str]:
    if sep is None:
        parts = line.split(None, len(DEFAULT_ORDER) - 1)     # 공백 구분: 마지막 칸(비고)은 공백 포함 그대로
        return parts
    return next(csv.reader([line], delimiter=sep))


def parse_text(text: str) -> list[ImportRow]:
    """붙여 넣은 텍스트 / CSV 내용 읽기."""
    lines = [ln for ln in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip()]
    if not lines:
        return []
    sample = "\n".join(lines[:50])
    sep = "\t" if "\t" in sample else ("," if "," in sample else None)
    if sep == ",":
        rows = list(csv.reader(io.StringIO("\n".join(lines))))
    else:   # 탭 구분이어도 탭이 없는 줄(손으로 친 줄)은 공백으로 나눈다
        rows = [_split_line(ln, sep if sep and sep in ln else None) for ln in lines]
    return parse_table(rows)


def parse_excel(path: str | Path) -> list[ImportRow]:
    """엑셀 파일 읽기(모든 시트에서 IP 표를 찾음)."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".csv", ".txt"):
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp949"):
            try:
                return parse_text(raw.decode(enc))
            except UnicodeDecodeError:
                continue
        raise ImportFormatError("텍스트 파일의 인코딩을 알 수 없습니다(UTF-8 또는 ANSI로 저장해 주세요).")
    if suffix == ".xls":
        raise ImportFormatError("예전 엑셀 형식(.xls)은 읽을 수 없습니다. 엑셀에서 .xlsx로 다시 저장해 주세요.")
    from openpyxl import load_workbook
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:                        # 암호화·손상된 파일 등
        raise ImportFormatError(
            "엑셀 파일을 열 수 없습니다. 파일이 열리지 않으면 내용을 복사해 "
            f"'텍스트 붙여넣기'로 불러오세요.\n({type(exc).__name__}: {exc})") from exc
    try:
        result: list[ImportRow] = []
        for ws in wb.worksheets:
            result += parse_table(ws.iter_rows(values_only=True), ws.title)
            if len(result) > MAX_ROWS:
                raise ImportFormatError(f"한 번에 {MAX_ROWS:,}행까지 불러올 수 있습니다. 대역별로 나눠 불러오세요.")
        return result
    finally:
        wb.close()


def mark_duplicates(rows: list[ImportRow]) -> None:
    """입력 안에서 같은 IP가 두 번 이상 나오면 뒤쪽 행을 오류로 표시한다."""
    seen: dict[int, str] = {}
    for row in rows:
        if row.error or row.ip_id is None:
            continue
        if row.ip_id in seen:
            row.error = f"입력 안에서 중복된 IP입니다({seen[row.ip_id]}와 같음)"
        else:
            seen[row.ip_id] = row.source
