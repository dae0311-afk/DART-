"""
pages/01_재무제표.py  ─  요약재무제표 (단일 파일 버전)
"""
from __future__ import annotations

import datetime
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

st.set_page_config(page_title="요약재무제표", page_icon="📊", layout="wide")

# ── 인증 체크 ──────────────────────────────────────────────────────────────
if not st.session_state.get("authenticated"):
    st.warning("🔐 메인 페이지에서 먼저 로그인하세요.")
    st.stop()

API_KEY     = st.secrets["DART_API_KEY"]
DART_BASE   = "https://opendart.fss.or.kr/api"
LATEST_YEAR = datetime.date.today().year - 1

# ── 단위 환산 ──────────────────────────────────────────────────────────────
UNIT_WON = {"원": 1, "천원": 1_000, "백만원": 1_000_000, "억원": 100_000_000, "십억원": 1_000_000_000}

ACCOUNT_KEYWORDS = {
    "매출액":  ["매출액", "영업수익", "수익(매출액)"],
    "영업이익": ["영업이익", "영업손익"],
}

# ── DART API 함수 ──────────────────────────────────────────────────────────

@st.cache_data(ttl=86_400, show_spinner=False)
def get_corp_list(api_key: str) -> pd.DataFrame:
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


def search_corp(api_key: str, query: str) -> tuple[pd.DataFrame, str]:
    """
    회사명 / 종목코드(6자리) / DART corp_code(8자리) 통합 검색
    Returns: (결과 DataFrame, 검색방식 설명 문자열)
    """
    df  = get_corp_list(api_key)
    q   = query.strip()

    # 숫자만으로 이루어진 경우 코드 검색
    if q.isdigit():
        if len(q) == 6:
            # 종목코드 (상장사)
            result = df[df["stock_code"] == q].copy()
            mode   = f"종목코드 {q}"
        elif len(q) == 8:
            # DART corp_code (전 법인)
            result = df[df["corp_code"] == q].copy()
            mode   = f"DART 기업코드 {q}"
        else:
            # 그 외 숫자: 회사명 부분검색 fallback
            result = df[df["corp_name"].str.contains(q, na=False, regex=False)].copy()
            mode   = f"회사명 '{q}'"
    else:
        # 문자 포함 → 회사명 검색
        result = df[df["corp_name"].str.contains(q, na=False, regex=False)].copy()
        mode   = f"회사명 '{q}'"

    # 상장사 우선 정렬
    result["_listed"] = result["stock_code"].str.strip().ne("")
    result = result.sort_values("_listed", ascending=False).drop(columns="_listed")
    return result.reset_index(drop=True), mode


@st.cache_data(ttl=3_600, show_spinner=False)
def fetch_annual(api_key: str, corp_code: str, year: int, fs_div: str) -> tuple[list, str, list]:
    """
    단일연도 재무계정 조회.
    1) fnlttSinglAcnt(주요계정) 선택한 fs_div → 반대 fs_div fallback
    2) 둘 다 실패 시 fnlttSinglAcntAll(전체재무제표) 재시도
    Returns: (records, 실제사용 fs_div, 진단로그 list)
    """
    diag: list = []

    def _call(endpoint: str, fsdiv: str) -> dict:
        try:
            r = requests.get(
                f"{DART_BASE}/{endpoint}.json",
                params={
                    "crtfc_key":  api_key,
                    "corp_code":  corp_code,
                    "bsns_year":  str(year),
                    "reprt_code": "11011",   # 사업보고서
                    "fs_div":     fsdiv,
                },
                timeout=15,
            )
            data = r.json()
        except Exception as e:
            data = {"status": "ERR", "message": str(e)}
        diag.append(f"{year} · {endpoint} · {fsdiv} → {data.get('status')} ({data.get('message','')})")
        return data

    primary = fs_div
    other   = "OFS" if fs_div == "CFS" else "CFS"

    # 1) 주요계정 — 선택 → 반대 순서로 시도
    for endpoint in ("fnlttSinglAcnt", "fnlttSinglAcntAll"):
        for fsdiv in (primary, other):
            data = _call(endpoint, fsdiv)
            if data.get("status") == "000" and data.get("list"):
                return data["list"], fsdiv, diag

    return [], fs_div, diag


# ── 재무 계산 함수 ─────────────────────────────────────────────────────────

def parse_amount(s: str) -> Optional[float]:
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
    in_won = value * UNIT_WON.get(from_unit, 1_000)
    return in_won / UNIT_WON.get(to_unit, 100_000_000)


def extract_account(records: list, keywords: list[str]) -> Optional[float]:
    for r in records:
        nm = (r.get("account_nm") or "").strip()
        if any(kw in nm for kw in keywords):
            raw = r.get("thstrm_amount") or r.get("thstrm_add_amount")
            return parse_amount(raw)
    return None


def build_df(years, records_map, dart_unit, display_unit) -> pd.DataFrame:
    rows = []
    for yr in years:
        records = records_map.get(yr, [])
        revenue = extract_account(records, ACCOUNT_KEYWORDS["매출액"])
        op_inc  = extract_account(records, ACCOUNT_KEYWORDS["영업이익"])
        rows.append({
            "연도":    str(yr),
            "매출액":  convert(revenue, dart_unit, display_unit) if revenue is not None else None,
            "영업이익": convert(op_inc,  dart_unit, display_unit) if op_inc  is not None else None,
        })
    return pd.DataFrame(rows)


# ── 사이드바 옵션 ───────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 조회 옵션")

    fs_div_label = st.segmented_control(
        "재무제표 구분",
        options=["연결", "별도"],
        default="연결",
    )
    fs_div = "CFS" if fs_div_label == "연결" else "OFS"

    period = st.segmented_control(
        "조회 기간",
        options=[5, 10, 20],
        format_func=lambda x: f"{x}년",
        default=5,
    )

    display_unit = st.segmented_control(
        "표시 단위",
        options=["백만원", "억원", "십억원"],
        default="억원",
    )

    st.divider()
    with st.expander("🔧 고급 설정"):
        dart_unit = st.segmented_control(
            "DART 원본 단위",
            options=["천원", "백만원", "원"],
            default="천원",
            help="DART API 반환 금액의 원본 단위. 숫자가 이상하면 여기서 조정하세요.",
        )

# ── 회사 검색 ──────────────────────────────────────────────────────────────
st.title("📊 요약재무제표")
st.caption("사업보고서 기준 | 매출액 · 영업이익 · 영업이익률")
st.caption("💡 사명이 바뀐 경우: 현재 사명, 종목코드(6자리), 또는 DART 기업코드(8자리)로 검색하세요.")

with st.form("search_form"):
    c1, c2 = st.columns([4, 1])
    with c1:
        query = st.text_input(
            "검색",
            placeholder="회사명 · 종목코드(6자리) · DART 기업코드(8자리)",
            label_visibility="collapsed",
        )
    with c2:
        search_btn = st.form_submit_button("🔍 검색", use_container_width=True)

if search_btn and query.strip():
    with st.spinner("기업 검색 중…"):
        results, search_mode = search_corp(API_KEY, query.strip())
    if results.empty:
        st.error(f"'{query}' 검색 결과가 없습니다.")
    else:
        st.success(f"**{search_mode}** 기준으로 {len(results)}개 검색됨")
        st.session_state["search_results"] = results
        st.session_state.pop("result_df", None)

# ── 회사 선택 ──────────────────────────────────────────────────────────────
if "search_results" in st.session_state:
    results: pd.DataFrame = st.session_state["search_results"]

    def make_label(row: pd.Series) -> str:
        if row["stock_code"]:
            return f"{row['corp_name']}  ({row['stock_code']})"
        return row["corp_name"]

    labels   = results.apply(make_label, axis=1).tolist()
    chosen_i = st.selectbox(
        "회사 선택",
        range(len(labels)),
        format_func=lambda i: labels[i],
    )
    chosen = results.iloc[chosen_i]

    st.divider()
    fetch_btn = st.button("📥 재무데이터 조회", type="primary", use_container_width=True)

    if fetch_btn:
        years = list(range(LATEST_YEAR - period + 1, LATEST_YEAR + 1))
        records_map: dict[int, list] = {}
        actual_fs:   dict[int, str]  = {}
        all_diag:    list            = []

        prog = st.progress(0, text="DART에서 데이터 불러오는 중…")
        for i, yr in enumerate(years):
            recs, used_fs, diag = fetch_annual(API_KEY, chosen["corp_code"], yr, fs_div)
            records_map[yr] = recs
            actual_fs[yr]   = used_fs
            all_diag.extend(diag)
            prog.progress((i + 1) / len(years), text=f"{yr}년 조회 중…")
        prog.empty()

        fallback_yrs = [yr for yr, fs in actual_fs.items() if fs != fs_div and records_map[yr]]
        if fallback_yrs:
            req_label = "연결" if fs_div == "CFS" else "별도"
            alt_label = "별도" if fs_div == "CFS" else "연결"
            st.info(
                f"ℹ️ {', '.join(map(str, fallback_yrs))}년은 {req_label}재무제표가 없어 "
                f"{alt_label}재무제표로 대체 조회했습니다."
            )

        df = build_df(years, records_map, dart_unit, display_unit)
        st.session_state["result_df"]   = df
        st.session_state["result_meta"] = {
            "corp_name":    chosen["corp_name"],
            "corp_code":    chosen["corp_code"],
            "fs_div":       fs_div,
            "display_unit": display_unit,
        }
        st.session_state["diag"] = all_diag

# ── 결과 출력 ──────────────────────────────────────────────────────────────
if "result_df" in st.session_state:
    df:   pd.DataFrame = st.session_state["result_df"]
    meta: dict         = st.session_state["result_meta"]

    corp_name = meta["corp_name"]
    u         = meta["display_unit"]
    fs_label  = "연결" if meta["fs_div"] == "CFS" else "별도"

    st.subheader(f"{corp_name}  |  {fs_label}  |  단위: {u}")

    fmt = df.copy()
    for col in ["매출액", "영업이익"]:
        fmt[col] = fmt[col].apply(
            lambda x: f"{x:,.1f}" if (x is not None and not pd.isna(x)) else "—"
        )
    oi_rate = []
    for _, row in df.iterrows():
        if (row["매출액"] is not None and not pd.isna(row["매출액"])
                and row["영업이익"] is not None and not pd.isna(row["영업이익"])
                and row["매출액"] != 0):
            oi_rate.append(f"{row['영업이익'] / row['매출액'] * 100:.1f}%")
        else:
            oi_rate.append("—")
    fmt["영업이익률"] = oi_rate

    st.dataframe(fmt, use_container_width=True, hide_index=True)

    valid_r = df.dropna(subset=["매출액"])
    valid_o = df.dropna(subset=["영업이익"])

    fig = go.Figure()

    if not valid_r.empty:
        fig.add_trace(go.Bar(
            x=valid_r["연도"],
            y=valid_r["매출액"],
            name="매출액",
            marker_color="#1E3D6B",
            text=valid_r["매출액"].apply(lambda x: f"{x:,.0f}"),
            textposition="outside",
            textfont=dict(size=11),
        ))

    if not valid_o.empty:
        fig.add_trace(go.Bar(
            x=valid_o["연도"],
            y=valid_o["영업이익"],
            name="영업이익",
            marker_color="#2E6DA4",
            text=valid_o["영업이익"].apply(lambda x: f"{x:,.0f}"),
            textposition="outside",
            textfont=dict(size=11),
        ))

    fig.update_layout(
        barmode="group",
        plot_bgcolor="white",
        paper_bgcolor="white",
        yaxis=dict(title=u, gridcolor="#EBEBEB", zeroline=True, zerolinecolor="#CCCCCC"),
        xaxis=dict(title="", type="category"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(t=60, b=30, l=70, r=20),
        height=430,
    )

    st.plotly_chart(fig, use_container_width=True)

    missing = df[df["매출액"].isna()]["연도"].tolist()
    if missing:
        st.warning(f"⚠️ 다음 연도는 데이터를 가져오지 못했습니다: {', '.join(missing)}")

    # ── 진단 정보 ──────────────────────────────────────────────────────────
    diag = st.session_state.get("diag", [])
    with st.expander("🔍 진단 정보 (데이터가 안 나올 때 펼쳐보세요)"):
        st.caption(f"기업코드: `{meta.get('corp_code','')}`")
        st.caption(
            "DART status 코드: **000**=정상, **013**=데이터없음(사업보고서 미제출/소규모 외감), "
            "**020**=호출제한초과, **ERR**=네트워크오류"
        )
        if diag:
            st.code("\n".join(diag), language="text")
        else:
            st.write("진단 로그 없음")
