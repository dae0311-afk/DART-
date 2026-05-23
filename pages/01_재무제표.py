"""
pages/01_재무제표.py  ─  요약재무제표 (감사보고서 전용)
모든 회사를 감사보고서 원문 기준으로 매출액·영업이익 추출 (원 단위 정규화)
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
from bs4 import BeautifulSoup

st.set_page_config(page_title="요약재무제표", page_icon="📊", layout="wide")

# ── 인증 체크 ──────────────────────────────────────────────────────────────
if not st.session_state.get("authenticated"):
    st.warning("🔐 메인 페이지에서 먼저 로그인하세요.")
    st.stop()

API_KEY     = st.secrets["DART_API_KEY"]
DART_BASE   = "https://opendart.fss.or.kr/api"
LATEST_YEAR = datetime.date.today().year - 1

UNIT_WON = {"원": 1, "천원": 1_000, "백만원": 1_000_000, "억원": 100_000_000, "십억원": 1_000_000_000}

ROMAN = "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ"


# ── 기업코드 검색 ───────────────────────────────────────────────────────────

@st.cache_data(ttl=86_400, show_spinner=False)
def get_corp_list(api_key: str) -> pd.DataFrame:
    resp = requests.get(f"{DART_BASE}/corpCode.xml", params={"crtfc_key": api_key}, timeout=30)
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
    df = get_corp_list(api_key)
    q  = query.strip()
    if q.isdigit() and len(q) == 6:
        result = df[df["stock_code"] == q].copy(); mode = f"종목코드 {q}"
    elif q.isdigit() and len(q) == 8:
        result = df[df["corp_code"] == q].copy(); mode = f"DART 기업코드 {q}"
    else:
        result = df[df["corp_name"].str.contains(q, na=False, regex=False)].copy(); mode = f"회사명 '{q}'"
    result["_listed"] = result["stock_code"].str.strip().ne("")
    result = result.sort_values("_listed", ascending=False).drop(columns="_listed")
    return result.reset_index(drop=True), mode


# ── 금액/라벨 파싱 ──────────────────────────────────────────────────────────

def parse_amount(s: str) -> Optional[float]:
    if not s:
        return None
    t = str(s).strip()
    neg = ("(" in t and ")" in t) or t.startswith("△") or t.startswith("▲") or t.lstrip().startswith("-")
    t = re.sub(r"[(),\s△▲−-]", "", t)
    if not re.search(r"\d", t):
        return None
    try:
        v = float(t)
        return -v if neg else v
    except ValueError:
        return None


def norm_label(s: str) -> str:
    """로마숫자·번호·괄호 등 접두어 제거 후 공백 제거"""
    s = re.sub(r"\s+", "", s)
    s = s.lstrip(ROMAN + "0123456789.()IVXivx-·")
    return s


def detect_unit(text: str) -> str:
    m = re.search(r"단위\s*[:：]\s*(십억원|백만원|억원|천원|원)", text)
    return m.group(1) if m else "원"


# ── 감사보고서 접수번호 찾기 ────────────────────────────────────────────────

def find_audit_rcept(api_key, corp_code, fy_year, want_cfs, diag) -> tuple[Optional[str], Optional[str]]:
    """
    FY{fy_year} 감사보고서 접수번호 + 종류 반환.
    Returns (rcept_no, kind)  kind ∈ {"연결","별도"}
    """
    for filing_year in (fy_year + 1, fy_year + 2):
        try:
            data = requests.get(
                f"{DART_BASE}/list.json",
                params={"crtfc_key": api_key, "corp_code": corp_code,
                        "bgn_de": f"{filing_year}0101", "end_de": f"{filing_year}1231",
                        "pblntf_ty": "F", "page_count": 100},
                timeout=15,
            ).json()
        except Exception as e:
            diag.append(f"{fy_year} · list.json → ERR ({e})")
            continue
        diag.append(f"{fy_year} · list.json({filing_year}) → {data.get('status')}")
        if data.get("status") != "000":
            continue

        cons, sep = None, None
        for it in data.get("list", []):
            nm = it.get("report_nm", "")
            if "감사보고서" not in nm:
                continue
            if "연결" in nm:
                cons = cons or it.get("rcept_no")
            else:
                sep = sep or it.get("rcept_no")

        if want_cfs:
            if cons:
                diag.append(f"{fy_year} · 연결감사보고서 {cons}")
                return cons, "연결"
            if sep:
                diag.append(f"{fy_year} · (연결없음) 별도감사보고서 {sep}")
                return sep, "별도"
        else:
            if sep:
                diag.append(f"{fy_year} · 별도감사보고서 {sep}")
                return sep, "별도"
            if cons:
                diag.append(f"{fy_year} · (별도없음) 연결감사보고서 {cons}")
                return cons, "연결"
    return None, None


# ── 감사보고서 원문 파싱 ────────────────────────────────────────────────────

def parse_document(api_key, rcept_no, diag) -> tuple[Optional[float], Optional[float], str]:
    """document.xml → 손익계산서에서 매출액·영업이익 추출 (원 단위 정규화)"""
    try:
        r = requests.get(f"{DART_BASE}/document.xml",
                         params={"crtfc_key": api_key, "rcept_no": rcept_no}, timeout=40)
    except Exception as e:
        diag.append(f"document.xml → ERR ({e})")
        return None, None, "원"
    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except Exception:
        diag.append("document.xml → zip 해제 실패")
        return None, None, "원"

    full_text = ""
    for name in zf.namelist():
        raw = zf.read(name)
        for enc in ("utf-8", "cp949", "euc-kr"):
            try:
                full_text += raw.decode(enc); break
            except Exception:
                continue

    unit = detect_unit(full_text)
    soup = BeautifulSoup(full_text, "html.parser")

    def first_amount_in_row(cells) -> Optional[float]:
        for c in cells:
            # 콤마로 묶인 금액 우선 (주석 번호 회피)
            m = re.search(r"[\(△▲-]?\s*\d{1,3}(?:,\d{3})+\s*\)?", c)
            if not m:
                m = re.search(r"[\(△▲-]?\s*\d{4,}\s*\)?", c)
            if m:
                v = parse_amount(m.group())
                if v is not None:
                    return v
        return None

    def scan(match_fn) -> Optional[float]:
        for tr in soup.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            label = norm_label(cells[0])
            if match_fn(label):
                v = first_amount_in_row(cells[1:])
                if v is not None:
                    return v
        return None

    rev = scan(lambda L: L.startswith("매출액") or L.startswith("영업수익") or L.startswith("수익(매출"))
    oi  = scan(lambda L: L.startswith("영업이익") or L.startswith("영업손실"))
    diag.append(f"파싱결과 매출:{rev} 영업이익:{oi} (단위:{unit})")

    mult = UNIT_WON.get(unit, 1)
    return (rev * mult if rev is not None else None,
            oi  * mult if oi  is not None else None,
            unit)


# ── 연도별 통합 조회 (원 단위 반환) ─────────────────────────────────────────

@st.cache_data(ttl=3_600, show_spinner=False)
def fetch_year(api_key, corp_code, year, want_cfs) -> dict:
    diag: list = []
    rcept, kind = find_audit_rcept(api_key, corp_code, year, want_cfs, diag)
    if not rcept:
        return {"revenue_won": None, "opinc_won": None, "kind": "없음", "diag": diag}
    rev_won, oi_won, _ = parse_document(api_key, rcept, diag)
    return {"revenue_won": rev_won, "opinc_won": oi_won, "kind": kind, "diag": diag}


def build_df(years, year_data: dict, display_unit: str) -> pd.DataFrame:
    div = UNIT_WON.get(display_unit, 100_000_000)
    rows = []
    for yr in years:
        d   = year_data.get(yr, {})
        rev = d.get("revenue_won")
        oi  = d.get("opinc_won")
        rows.append({
            "연도":    str(yr),
            "매출액":  rev / div if rev is not None else None,
            "영업이익": oi  / div if oi  is not None else None,
            "기준":    d.get("kind", "없음"),
        })
    return pd.DataFrame(rows)


# ── 사이드바 옵션 ───────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 조회 옵션")

    fs_div_label = st.segmented_control("재무제표 구분", options=["연결", "별도"], default="연결")
    want_cfs = (fs_div_label == "연결")

    period = st.segmented_control(
        "조회 기간", options=[5, 10, 20, "최대"],
        format_func=lambda x: (f"{x}년" if isinstance(x, int) else x), default=5,
    )

    display_unit = st.segmented_control("표시 단위", options=["백만원", "억원", "십억원"], default="억원")
    st.caption("표시 단위는 조회 후에도 즉시 변경됩니다.")

# ── 회사 검색 ──────────────────────────────────────────────────────────────
st.title("📊 요약재무제표")
st.caption("감사보고서 기준 | 매출액 · 영업이익 · 영업이익률 (원 단위 추출)")
st.caption("💡 사명 변경 시: 현재 사명, 종목코드(6자리), DART 기업코드(8자리)로 검색하세요.")

with st.form("search_form"):
    c1, c2 = st.columns([4, 1])
    with c1:
        query = st.text_input("검색", placeholder="회사명 · 종목코드(6자리) · DART 기업코드(8자리)",
                              label_visibility="collapsed")
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
        for k in ("year_data", "years", "result_meta"):
            st.session_state.pop(k, None)

# ── 회사 선택 ──────────────────────────────────────────────────────────────
if "search_results" in st.session_state:
    results: pd.DataFrame = st.session_state["search_results"]

    def make_label(row):
        return f"{row['corp_name']}  ({row['stock_code']})" if row["stock_code"] else row["corp_name"]

    labels   = results.apply(make_label, axis=1).tolist()
    chosen_i = st.selectbox("회사 선택", range(len(labels)), format_func=lambda i: labels[i])
    chosen   = results.iloc[chosen_i]

    st.divider()
    fetch_btn = st.button("📥 재무데이터 조회", type="primary", use_container_width=True)

    if fetch_btn:
        max_mode = (period == "최대")
        if max_mode:
            years_to_fetch = list(range(LATEST_YEAR, 1998, -1))   # 신→구, 조기종료
        else:
            years_to_fetch = list(range(LATEST_YEAR, LATEST_YEAR - period, -1))

        year_data: dict = {}
        all_diag: list  = []
        consecutive_empty = 0
        found_any = False

        with st.status("감사보고서 조회 중…", expanded=False) as status:
            for yr in years_to_fetch:
                status.update(label=f"{yr}년 조회 중…")
                d = fetch_year(API_KEY, chosen["corp_code"], yr, want_cfs)
                year_data[yr] = d
                all_diag.extend(d.get("diag", []))

                has = (d.get("revenue_won") is not None) or (d.get("opinc_won") is not None)
                if has:
                    found_any = True
                    consecutive_empty = 0
                else:
                    consecutive_empty += 1
                # 최대 모드: 데이터가 한 번이라도 나온 뒤 3년 연속 비면 중단
                if max_mode and found_any and consecutive_empty >= 3:
                    break
            status.update(label="조회 완료", state="complete")

        st.session_state["year_data"]   = year_data
        st.session_state["years"]       = sorted(year_data.keys())
        st.session_state["result_meta"] = {
            "corp_name": chosen["corp_name"], "corp_code": chosen["corp_code"],
            "want_cfs": want_cfs, "max_mode": max_mode,
        }
        st.session_state["diag"] = all_diag

# ── 결과 출력 (표시단위는 매 렌더마다 현재 값 사용) ──────────────────────────
if "year_data" in st.session_state:
    year_data = st.session_state["year_data"]
    years     = st.session_state["years"]
    meta      = st.session_state["result_meta"]

    df_full = build_df(years, year_data, display_unit)

    req_label = "연결" if meta["want_cfs"] else "별도"
    st.subheader(f"{meta['corp_name']}  |  요청: {req_label}  |  단위: {display_unit}")

    # 데이터 있는 연도만 추림
    has_mask = df_full[["매출액", "영업이익"]].notna().any(axis=1)
    if not has_mask.any():
        st.error("선택한 기간 내 공시된 데이터가 없습니다.")
        with st.expander("🔍 진단 정보"):
            st.caption(f"기업코드: `{meta.get('corp_code','')}`")
            st.code("\n".join(st.session_state.get("diag", [])) or "로그 없음", language="text")
        st.stop()

    start_year = int(df_full.loc[has_mask.idxmax(), "연도"])
    df = df_full[df_full["연도"].astype(int) >= start_year].reset_index(drop=True)

    # 공시 시작 안내 (요청 시작연도보다 늦게 시작된 경우)
    requested_start = min(int(y) for y in years)
    if start_year > requested_start:
        st.info(f"ℹ️ 이 회사의 데이터는 **{start_year}년부터** 공시되어, 그 이전 기간은 제외했습니다.")

    # 연결 요청했으나 별도로 대체된 연도 안내
    if meta["want_cfs"]:
        fb = [str(yr) for yr in years if yr >= start_year and year_data.get(yr, {}).get("kind") == "별도"]
        if fb:
            st.info(f"ℹ️ {', '.join(fb)}년은 연결감사보고서가 없어 **별도** 기준으로 표시했습니다.")

    fmt = df.copy()
    for col in ["매출액", "영업이익"]:
        fmt[col] = fmt[col].apply(lambda x: f"{x:,.0f}" if (x is not None and not pd.isna(x)) else "—")
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
        fig.add_trace(go.Bar(x=valid_r["연도"], y=valid_r["매출액"], name="매출액",
                             marker_color="#1E3D6B",
                             text=valid_r["매출액"].apply(lambda x: f"{x:,.0f}"),
                             textposition="outside", textfont=dict(size=11)))
    if not valid_o.empty:
        fig.add_trace(go.Bar(x=valid_o["연도"], y=valid_o["영업이익"], name="영업이익",
                             marker_color="#2E6DA4",
                             text=valid_o["영업이익"].apply(lambda x: f"{x:,.0f}"),
                             textposition="outside", textfont=dict(size=11)))
    fig.update_layout(barmode="group", plot_bgcolor="white", paper_bgcolor="white",
                      yaxis=dict(title=display_unit, gridcolor="#EBEBEB", zeroline=True, zerolinecolor="#CCCCCC"),
                      xaxis=dict(title="", type="category"),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                      margin=dict(t=60, b=30, l=70, r=20), height=430)
    st.plotly_chart(fig, use_container_width=True)

    missing = df[df["매출액"].isna()]["연도"].tolist()
    if missing:
        st.warning(f"⚠️ 공시 시작 이후이지만 데이터를 못 가져온 연도: {', '.join(missing)}")

    diag = st.session_state.get("diag", [])
    with st.expander("🔍 진단 정보"):
        st.caption(f"기업코드: `{meta.get('corp_code','')}`")
        st.code("\n".join(diag) if diag else "로그 없음", language="text")
