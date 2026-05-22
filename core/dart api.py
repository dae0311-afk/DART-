"""
core/dart_api.py
DART(dart.fss.or.kr) API 호출 전담 모듈
"""
from __future__ import annotations

import io
import zipfile
import xml.etree.ElementTree as ET

import pandas as pd
import requests
import streamlit as st

DART_BASE = "https://opendart.fss.or.kr/api"


# ── 기업코드 ────────────────────────────────────────────────────────────────

@st.cache_data(ttl=86_400, show_spinner=False)
def get_corp_list(api_key: str) -> pd.DataFrame:
    """DART 전체 기업코드 목록 다운로드 (1일 캐시)"""
    resp = requests.get(
        f"{DART_BASE}/corpCode.xml",
        params={"crtfc_key": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_bytes = zf.read("CORPCODE.xml")
    root = ET.fromstring(xml_bytes)
    rows = [
        {
            "corp_code":  item.findtext("corp_code", ""),
            "corp_name":  item.findtext("corp_name", ""),
            "stock_code": (item.findtext("stock_code") or "").strip(),
        }
        for item in root.findall("list")
    ]
    return pd.DataFrame(rows)


def search_corp(api_key: str, query: str) -> pd.DataFrame:
    """회사명으로 기업코드 검색 (상장사 우선 정렬)"""
    df = get_corp_list(api_key)
    mask = df["corp_name"].str.contains(query.strip(), na=False, regex=False)
    result = df[mask].copy()
    result["_listed"] = result["stock_code"].str.strip().ne("")
    result = result.sort_values("_listed", ascending=False).drop(columns="_listed")
    return result.reset_index(drop=True)


# ── 재무제표 ────────────────────────────────────────────────────────────────

@st.cache_data(ttl=3_600, show_spinner=False)
def fetch_annual(
    api_key: str,
    corp_code: str,
    year: int,
    fs_div: str,
) -> tuple[list, str]:
    """
    단일연도 주요 재무계정 조회 (사업보고서 기준)
    CFS(연결) 없으면 OFS(별도)로 자동 fallback

    Returns:
        (records: list, actual_fs_div: str)
    """
    def _call(fsdiv: str) -> dict:
        return requests.get(
            f"{DART_BASE}/fnlttSinglAcnt.json",
            params={
                "crtfc_key":   api_key,
                "corp_code":   corp_code,
                "bsns_year":   str(year),
                "reprt_code":  "11011",   # 사업보고서
                "fs_div":      fsdiv,
            },
            timeout=15,
        ).json()

    data = _call(fs_div)
    if data.get("status") == "000":
        return data.get("list", []), fs_div

    # 연결 없으면 별도 fallback
    if fs_div == "CFS" and data.get("status") in ("013", "020"):
        data2 = _call("OFS")
        if data2.get("status") == "000":
            return data2.get("list", []), "OFS"

    return [], fs_div
