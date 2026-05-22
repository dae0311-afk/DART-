"""
pages/01_재무제표.py  ─  요약재무제표 (매출액·영업이익)
"""
from __future__ import annotations

import datetime
import sys
import os

# core 모듈 경로 추가 (Streamlit pages/ 실행 시 필요)
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core import dart_api, financial

st.set_page_config(page_title="요약재무제표", page_icon="📊", layout="wide")

# ── 인증 체크 ──────────────────────────────────────────────────────────────
if not st.session_state.get("authenticated"):
    st.warning("🔐 메인 페이지에서 먼저 로그인하세요.")
    st.stop()

API_KEY     = st.secrets["DART_API_KEY"]
LATEST_YEAR = datetime.date.today().year - 1   # 가장 최근 사업보고서

# ── 사이드바 옵션 ───────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 조회 옵션")

    fs_div = st.radio(
        "재무제표 구분",
        options=["CFS", "OFS"],
        format_func=lambda x: "연결 (CFS)" if x == "CFS" else "별도 (OFS)",
        index=0,
        horizontal=True,
    )

    period = st.radio(
        "조회 기간",
        options=[5, 10, 20],
        format_func=lambda x: f"{x}년",
        index=0,
        horizontal=True,
    )

    display_unit = st.radio(
        "표시 단위",
        options=["백만원", "억원", "십억원"],
        index=1,
        horizontal=True,
    )

    st.divider()
    with st.expander("🔧 고급 설정"):
        dart_unit = st.radio(
            "DART 원본 단위",
            options=["천원", "백만원", "원"],
            index=0,
            help=(
                "DART API 반환 금액의 원본 단위.\n"
                "대부분 **천원**이나 일부 기업은 백만원 사용.\n"
                "숫자가 이상하면 여기서 조정하세요."
            ),
            horizontal=True,
        )

# ── 회사 검색 ──────────────────────────────────────────────────────────────
st.title("📊 요약재무제표")
st.caption("사업보고서 기준 | 매출액 · 영업이익 · 영업이익률")

c1, c2 = st.columns([4, 1])
with c1:
    query = st.text_input(
        "회사명 검색",
        placeholder="예: 삼성전자   카카오   LG에너지솔루션",
        label_visibility="collapsed",
    )
with c2:
    search_btn = st.button("🔍 검색", use_container_width=True)

if search_btn and query.strip():
    with st.spinner("기업 검색 중…"):
        results = dart_api.search_corp(API_KEY, query.strip())
    if results.empty:
        st.error("검색 결과가 없습니다.")
    else:
        st.session_state["search_results"] = results
        st.session_state.pop("result_df", None)   # 이전 결과 초기화

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

        prog = st.progress(0, text="DART에서 데이터 불러오는 중…")
        for i, yr in enumerate(years):
            recs, used_fs       = dart_api.fetch_annual(API_KEY, chosen["corp_code"], yr, fs_div)
            records_map[yr]     = recs
            actual_fs[yr]       = used_fs
            prog.progress((i + 1) / len(years), text=f"{yr}년 조회 중…")
        prog.empty()

        # fallback 알림
        fallback_yrs = [yr for yr, fs in actual_fs.items() if fs != fs_div and records_map[yr]]
        if fallback_yrs:
            st.info(
                f"ℹ️ {', '.join(map(str, fallback_yrs))}년은 연결재무제표가 없어 "
                "별도재무제표로 대체 조회했습니다."
            )

        df = financial.build_df(years, records_map, dart_unit, display_unit)
        st.session_state["result_df"]   = df
        st.session_state["result_meta"] = {
            "corp_name":    chosen["corp_name"],
            "fs_div":       fs_div,
            "display_unit": display_unit,
        }

# ── 결과 출력 ──────────────────────────────────────────────────────────────
if "result_df" in st.session_state:
    df:   pd.DataFrame = st.session_state["result_df"]
    meta: dict         = st.session_state["result_meta"]

    corp_name    = meta["corp_name"]
    u            = meta["display_unit"]
    fs_label     = "연결" if meta["fs_div"] == "CFS" else "별도"

    st.subheader(f"{corp_name}  |  {fs_label}  |  단위: {u}")

    # ── 테이블 ─────────────────────────────────────────────────────────────
    fmt = df.copy()
    for col in ["매출액", "영업이익"]:
        fmt[col] = fmt[col].apply(
            lambda x: f"{x:,.1f}" if (x is not None and not pd.isna(x)) else "—"
        )
    # 영업이익률 추가
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

    # ── 차트 ───────────────────────────────────────────────────────────────
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
        yaxis=dict(
            title=u,
            gridcolor="#EBEBEB",
            zeroline=True,
            zerolinecolor="#CCCCCC",
        ),
        xaxis=dict(title="", type="category"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(t=60, b=30, l=70, r=20),
        height=430,
    )

    st.plotly_chart(fig, use_container_width=True)

    # ── 데이터 없는 연도 경고 ───────────────────────────────────────────────
    missing = df[df["매출액"].isna()]["연도"].tolist()
    if missing:
        st.warning(f"⚠️ 다음 연도는 데이터를 가져오지 못했습니다: {', '.join(missing)}")
