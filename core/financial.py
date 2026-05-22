"""
core/financial.py
DART 재무데이터 파싱·단위변환·계정 추출
"""
from __future__ import annotations

import re
from typing import Optional

import pandas as pd

# ── 단위 환산표 (→ 원) ──────────────────────────────────────────────────────
UNIT_WON: dict[str, int] = {
    "원":     1,
    "천원":   1_000,
    "백만원": 1_000_000,
    "억원":   100_000_000,
    "십억원": 1_000_000_000,
}

DART_DEFAULT_UNIT = "천원"   # DART API 기본 단위 (미표기 시 가정)

# ── 계정명 매핑 ─────────────────────────────────────────────────────────────
ACCOUNT_KEYWORDS: dict[str, list[str]] = {
    "매출액":  ["매출액", "영업수익", "수익(매출액)"],
    "영업이익": ["영업이익", "영업손익"],
}


def detect_unit(text: str) -> Optional[str]:
    """텍스트에서 단위 패턴 탐지 (regex 전용, 숫자 크기 추론 금지)"""
    m = re.search(r"(십억원|백만원|억원|천원|원)", text)
    return m.group(1) if m else None


def parse_amount(s: str) -> Optional[float]:
    """콤마·공백 포함 금액 문자열 → float"""
    if not s:
        return None
    cleaned = re.sub(r"[,\s]", "", s).strip()
    if cleaned in ("", "-", "−", "0"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """단위 변환: from_unit → to_unit"""
    in_won = value * UNIT_WON.get(from_unit, UNIT_WON[DART_DEFAULT_UNIT])
    return in_won / UNIT_WON.get(to_unit, UNIT_WON["억원"])


def extract_account(records: list, keywords: list[str]) -> Optional[float]:
    """계정명 키워드로 당기 금액 추출 (첫 번째 매칭)"""
    for r in records:
        nm = (r.get("account_nm") or "").strip()
        if any(kw in nm for kw in keywords):
            raw = r.get("thstrm_amount") or r.get("thstrm_add_amount")
            return parse_amount(raw)
    return None


def build_df(
    years: list[int],
    records_map: dict[int, list],
    dart_unit: str,
    display_unit: str,
) -> pd.DataFrame:
    """연도별 records → 매출액·영업이익 DataFrame"""
    rows = []
    for yr in years:
        records = records_map.get(yr, [])
        revenue  = extract_account(records, ACCOUNT_KEYWORDS["매출액"])
        op_inc   = extract_account(records, ACCOUNT_KEYWORDS["영업이익"])
        rows.append({
            "연도":   str(yr),
            "매출액":  convert(revenue,  dart_unit, display_unit) if revenue  is not None else None,
            "영업이익": convert(op_inc,   dart_unit, display_unit) if op_inc   is not None else None,
        })
    return pd.DataFrame(rows)
