"""맥락 기반 Excel(.xlsx) 내보내기 (openpyxl, Import 없음).

- 모드 A(그룹 선택): 시트 `대역별_사용현황` — 산하 전체 대역 + 사용률 통계
- 모드 B(서브넷 선택): 시트 `IP_할당현황` + 시트 `대역_전체이력`

데이터 수집(collect_*)은 DB Worker에서, 파일 생성(write_*)은 별도 스레드에서 실행한다.
openpyxl은 내보내기 시점에 지연 import하여 프로그램 기동 속도에 영향을 주지 않는다.
"""
from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ipmgr.constants import LEVEL_CRITICAL, LEVEL_LABELS, LEVEL_SAFE, LEVEL_WARNING
from ipmgr.logic.subnet_calc import usage_level, usage_percent
from ipmgr.models import group_model, ip_model, subnet_model
from ipmgr.models.common import now_str
from ipmgr.models.history_model import subnet_history

GROUP_SHEET = "대역별_사용현황"
GROUP_HEADERS = ("그룹 경로", "서브넷 대역(CIDR)", "대역 설명", "전체 IP 수", "사용중 IP 수",
                 "미사용 IP 수", "사용률(%)", "임계치 상태")
IP_SHEET = "IP_할당현황"
IP_HEADERS = ("IP 주소", "상태", "호스트네임", "변경일(할당·회수)", "담당자", "요청번호", "비고", "최종 수정일")
HISTORY_SHEET = "대역_전체이력"
HISTORY_HEADERS = ("일시", "IP 주소", "작업 유형", "이전 상태", "변경 상태", "호스트네임", "담당자",
                   "요청번호", "비고", "변경 상세요약", "작업자(이름/IP)")


@dataclass
class Report:
    """Excel 생성에 필요한 순수 데이터(스레드 간 전달용)."""
    title: str
    meta: str
    sheets: list[tuple[str, Sequence[str], list[tuple]]] = field(default_factory=list)
    level_col: dict[str, int] = field(default_factory=dict)      # 시트별 임계치 색상 열 index
    status_col: dict[str, int] = field(default_factory=dict)     # 시트별 상태 색상 열 index
    numeric_cols: dict[str, set[int]] = field(default_factory=dict)
    percent_cols: dict[str, set[int]] = field(default_factory=dict)


# ── 데이터 수집 (Worker 스레드) ─────────────────────────────
def collect_group_report(conn: sqlite3.Connection, group_id: int, operator: str) -> Report:
    """모드 A: 그룹 산하 모든 대역의 사용률 통계."""
    group = group_model.require_group(conn, group_id)
    title = group_model.all_group_paths(conn).get(group["id"], group["name"])
    return collect_scope_report(conn, [group_id], [], operator, title)


def collect_scope_report(conn: sqlite3.Connection, group_ids: list[int], subnet_ids: list[int], operator: str,
                         title: str = "") -> Report:
    """모드 A(다중 선택 포함): 선택한 그룹들 산하 + 선택한 대역들의 사용률 통계."""
    paths = group_model.all_group_paths(conn)
    rows = []
    total_all = used_all = 0
    for s in subnet_model.list_scope_subnets(conn, group_ids, subnet_ids):
        pct = usage_percent(s["used_count"], s["total_count"])
        total_all += s["total_count"]
        used_all += s["used_count"]
        rows.append((paths.get(s["group_id"], group_model.NO_GROUP_LABEL), s["cidr"], s["description"], s["total_count"],
                     s["used_count"], s["total_count"] - s["used_count"], pct / 100.0,
                     LEVEL_LABELS[usage_level(pct)]))
    pct_all = usage_percent(used_all, total_all)
    report = Report(
        title=f"대역별 사용현황 — {title or f'선택 항목 {len(group_ids) + len(subnet_ids)}개'}",
        meta=(f"대역 {len(rows):,}개 · 전체 IP {total_all:,} · 사용중 {used_all:,} · 사용률 {pct_all}% "
              f"({LEVEL_LABELS[usage_level(pct_all)]})   |   출력: {now_str()} · {operator}"))
    report.sheets.append((GROUP_SHEET, GROUP_HEADERS, rows))
    report.level_col[GROUP_SHEET] = 7
    report.numeric_cols[GROUP_SHEET] = {3, 4, 5}
    report.percent_cols[GROUP_SHEET] = {6}
    return report


def collect_subnet_report(conn: sqlite3.Connection, subnet_id: int, operator: str) -> Report:
    """모드 B: IP 할당현황 + 대역 전체 이력."""
    detail = ip_model.subnet_detail(conn, subnet_id)
    subnet = detail["subnet"]
    ip_rows = [(r["ip_address"], r["status"], r["hostname"], r["allocated_date"], r["assignee"],
                r["request_no"], r["note"], r["updated_at"]) for r in detail["ips"]]
    history = subnet_history(conn, subnet_id, subnet["network_int"], subnet["broadcast_int"])
    hist_rows = [(h["timestamp"], h["ip_address"], h["action"], h["old_status"], h["new_status"],
                  h["hostname"], h["assignee"], h["request_no"], h["note"], h["diff_summary"], h["operator"])
                 for h in history]
    pct = usage_percent(subnet["used_count"], subnet["total_count"])
    report = Report(
        title=f"대역 IP 현황 — {subnet['cidr']} ({detail['path']})",
        meta=(f"설명: {subnet['description'] or '-'} · 전체 {subnet['total_count']:,} · "
              f"사용중 {subnet['used_count']:,} · 사용률 {pct}% ({LEVEL_LABELS[usage_level(pct)]})"
              f"   |   출력: {now_str()} · {operator}"))
    report.sheets.append((IP_SHEET, IP_HEADERS, ip_rows))
    report.sheets.append((HISTORY_SHEET, HISTORY_HEADERS, hist_rows))
    report.status_col[IP_SHEET] = 1
    return report


# ── 파일 생성 (백그라운드 스레드) ──────────────────────────
def _display_width(value: Any) -> int:
    text = f"{value:.1%}" if isinstance(value, float) else str(value if value is not None else "")
    if text.isascii() and "\n" not in text:
        return len(text)
    width = 0
    for line in text.split("\n"):
        width = max(width, sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in line))
    return width


def write_report(report: Report, path: str | Path, progress: Callable[[str], None] | None = None) -> Path:
    """Report를 스타일이 적용된 xlsx로 저장한다(헤더 스타일·테두리·열 너비 자동 맞춤·틀 고정·필터)."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Border, Font, NamedStyle, PatternFill, Side
    from openpyxl.utils import get_column_letter

    font_name = "맑은 고딕"
    thin = Side(style="thin", color="BFC7D1")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    def named(name: str, *, bold: bool = False, color: str = "1F2937", fill: str | None = None,
              align: str = "left", fmt: str = "General", wrap: bool = False) -> NamedStyle:
        style = NamedStyle(name=name)
        style.font = Font(name=font_name, size=10, bold=bold, color=color)
        style.border = border
        style.alignment = Alignment(horizontal=align, vertical="top" if wrap else "center", wrap_text=wrap)
        if fill:
            style.fill = PatternFill("solid", start_color=fill, end_color=fill)
        style.number_format = fmt
        return style

    # write_only: 행을 스트리밍으로 기록 → 6만 행 이상도 메모리/시간 절약
    wb = Workbook(write_only=True)
    styles = {
        "head": named("xl_head", bold=True, color="FFFFFF", fill="1F4E78", align="center"),
        "text": named("xl_text"),
        "wrap": named("xl_wrap", wrap=True),          # 여러 줄 비고: 셀 안에서 줄바꿈 표시
        "center": named("xl_center", align="center"),
        "num": named("xl_num", align="right", fmt="#,##0"),
        "pct": named("xl_pct", align="right", fmt="0.0%"),
        LEVEL_SAFE: named("xl_safe", bold=True, color="065F46", fill="D1FAE5", align="center"),
        LEVEL_WARNING: named("xl_warn", bold=True, color="92400E", fill="FEF3C7", align="center"),
        LEVEL_CRITICAL: named("xl_crit", bold=True, color="991B1B", fill="FEE2E2", align="center"),
        "used": named("xl_used", bold=True, color="1D4ED8", fill="DBEAFE", align="center"),
    }
    for style in styles.values():
        wb.add_named_style(style)
    level_by_label = {label: key for key, label in LEVEL_LABELS.items()}

    title_font = Font(name=font_name, size=14, bold=True, color="1F4E78")
    meta_font = Font(name=font_name, size=10, color="4B5563")
    for sheet_name, headers, rows in report.sheets:
        if progress:
            progress(f"'{sheet_name}' 시트 작성 중… ({len(rows):,}행)")
        ws = wb.create_sheet(sheet_name)
        ncol = len(headers)
        header_row = 4

        # 열 너비 자동 맞춤: write_only는 행 기록 전에 너비를 정해야 하므로 먼저 계산
        widths = [_display_width(h) + 4 for h in headers]
        for row in rows:
            for c, value in enumerate(row):
                w = _display_width(value)
                if w > widths[c]:
                    widths[c] = w
        for c, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(c)].width = min(max(w + 2, 8), 70)
        last_row = header_row + max(len(rows), 1)
        ws.freeze_panes = f"A{header_row + 1}"
        ws.auto_filter.ref = f"A{header_row}:{get_column_letter(ncol)}{last_row}"

        def text_cell(value: Any, font: Font) -> WriteOnlyCell:
            cell = WriteOnlyCell(ws, value=value)
            cell.font = font
            return cell

        style_cache: dict[str, Any] = {}

        def styled(value: Any, style: str) -> WriteOnlyCell:
            # 이름 스타일 조회는 셀마다 비싸므로 스타일별 StyleArray를 한 번 만들어 공유한다
            cell = WriteOnlyCell(ws, value=value)
            array = style_cache.get(style)
            if array is None:
                cell.style = style
                style_cache[style] = cell._style
            else:
                cell._style = array
            return cell

        ws.append([text_cell(report.title, title_font)])
        ws.append([text_cell(report.meta, meta_font)])
        ws.append([])
        ws.append([styled(h, "xl_head") for h in headers])

        numeric = report.numeric_cols.get(sheet_name, set())
        percent = report.percent_cols.get(sheet_name, set())
        level_col = report.level_col.get(sheet_name)
        status_col = report.status_col.get(sheet_name)
        col_style = []
        for c in range(ncol):
            if c in numeric:
                col_style.append("xl_num")
            elif c in percent:
                col_style.append("xl_pct")
            elif c in (level_col, status_col):
                col_style.append("xl_center")
            else:
                col_style.append("xl_text")

        for row in rows:
            out = []
            for c, value in enumerate(row):
                if c == level_col:
                    out.append(styled(value, styles[level_by_label.get(value, LEVEL_SAFE)].name))
                elif c == status_col and value == "사용중":
                    out.append(styled(value, "xl_used"))
                elif isinstance(value, str) and "\n" in value:
                    out.append(styled(value, "xl_wrap"))
                else:
                    out.append(styled(value, col_style[c]))
            ws.append(out)
        if not rows:
            ws.append([styled("(데이터 없음)", "xl_text")])

    if progress:
        progress("파일 저장 중…")
    target = Path(path)
    wb.save(target)
    return target
