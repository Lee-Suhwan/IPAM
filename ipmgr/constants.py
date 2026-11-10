"""IP관리 전역 상수 정의.

상태값, 사용률 임계치, 화면/DB 관련 상수를 한 곳에서 관리한다.
DB `status` 컬럼에는 한글 상태 텍스트가 그대로 저장된다.
"""
from __future__ import annotations

from typing import Final

APP_NAME: Final = "IP관리"
APP_TITLE: Final = "IP관리"

# ── IP 상태 (2단계 상태 머신) ───────────────────────────────
STATUS_UNUSED: Final = "미사용"
STATUS_USED: Final = "사용중"
STATUSES: Final = (STATUS_UNUSED, STATUS_USED)

# ── 사용률 임계치 (%) ──────────────────────────────────────
THRESHOLD_WARNING: Final = 80.0         # 경고: 80% 초과
THRESHOLD_CRITICAL: Final = 90.0        # 위험: 90% 초과

LEVEL_SAFE: Final = "safe"
LEVEL_WARNING: Final = "warning"
LEVEL_CRITICAL: Final = "critical"
LEVEL_LABELS: Final = {LEVEL_SAFE: "안전", LEVEL_WARNING: "경고", LEVEL_CRITICAL: "위험"}

# ── 화면/처리 한도 ──────────────────────────────────────────
MAX_SUBNET_DISPLAY: Final = 256          # 그룹 대시보드 1회 표시 대역 수 (/16 → 256개 /24), [더 보기]로 추가
HISTORY_PAGE: Final = 300                # 이력 패널 1회 표시 건수, [더 보기]로 추가
MIN_SUBNET_PREFIX: Final = 16            # 그대로 등록 가능한 가장 큰 대역 (/16 = 65,534 호스트, 목록 표시 약 0.6초)
MAX_SPLIT_SUBNETS: Final = 16384         # 한 번에 만들 수 있는 대역 수 (/16 → /30 까지 허용)
MAX_CREATE_IPS: Final = 262144           # 한 번에 만들 수 있는 IP 총수(= /24 1,024개 · /16 4개 분량).
                                         # 생성 중에는 DB 쓰기 잠금이 유지되어 다른 사용자의 저장이 대기하므로
                                         # 공유 드라이브에서도 수 초 안에 끝나는 크기로 제한한다.
SEARCH_LIMIT: Final = 1000               # 통합 검색 최대 결과 수
MAX_TEXT_LEN: Final = 200                # 호스트네임/담당자/요청번호 최대 길이
MAX_NOTE_LEN: Final = 4000               # 비고(여러 줄 메모) 최대 길이

# ── 경로/백업 ──────────────────────────────────────────────
DATA_DIR_NAME: Final = "data_삭제금지"
DB_FILE_NAME: Final = "IP관리.db"
LEGACY_DB_FILE_NAME: Final = "ipam.db"     # v1.0 파일명 → 최초 실행 시 자동 이름 변경
BACKUP_DIR_NAME: Final = "Backup"
BACKUP_PREFIX: Final = "IP관리_"
BACKUP_RETENTION_DAYS: Final = 365

# ── DB / 동시성 ────────────────────────────────────────────
DB_TIMEOUT: Final = 15.0                 # sqlite busy timeout (초)
SCHEMA_VERSION: Final = 4                # v2: 그룹 없는 대역 허용, v3: 위변조 감지 해시, v4: 외부 수정 감시
POLL_INTERVAL_MS: Final = 3000           # 타 사용자 변경 감지 폴링 주기
UI_PUMP_MS: Final = 20                   # Worker 결과 → UI 전달 주기

# ── 이력(작업 유형) ────────────────────────────────────────
ACT_SUBNET_CREATE: Final = "대역 생성"
ACT_ALLOCATE: Final = "할당"
ACT_REALLOCATE: Final = "재할당"
ACT_RECLAIM: Final = "회수"
ACT_MODIFY: Final = "정보 수정"
ACT_SUBNET_UPDATE: Final = "대역 수정"
ACT_SUBNET_MOVE: Final = "대역 이동"
ACT_SUBNET_DELETE: Final = "대역 삭제"
ACT_GROUP_CREATE: Final = "그룹 생성"
ACT_GROUP_UPDATE: Final = "그룹 수정"
ACT_GROUP_MOVE: Final = "그룹 이동"
ACT_GROUP_DELETE: Final = "그룹 삭제"
ACT_INTEGRITY_REPAIR: Final = "무결성 복구"

# ── 무결성/백업 파일 ───────────────────────────────────────
ANCHOR_FILE_NAME: Final = "IP관리.anchor"          # 검증된 이력 체인 끝 지점(DB 밖 기준값)
GOOD_BACKUP_NAME: Final = "IP관리_정상백업.db"       # 무결성 검사를 통과한 최신 사본
PREV_BACKUP_NAME: Final = "IP관리_정상백업_이전.db"  # 그 직전 정상 사본
BACKUP_INFO_NAME: Final = "백업정보.json"
TAMPERED_PREFIX: Final = "변조본_"

# ── UI ────────────────────────────────────────────────────
FONT_FAMILY: Final = "Malgun Gothic"
FONT_FAMILY_FALLBACK: Final = "Segoe UI"
FONT_SIZE: Final = 10
DEFAULT_WINDOW_SIZE: Final = (1440, 900)
MIN_WINDOW_SIZE: Final = (1100, 700)
MIN_ROW_HEIGHT: Final = 28
