"""
pages/01_재무제표.py  ─  요약재무제표 (매출액·영업이익)
정기보고서 API → 없으면 감사보고서 원문 파싱 fallback
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

REVENUE_KW = ["매출액", "영업수익", "수익(매출액)", "매출"]
OPINC_KW   = ["영업이익", "영업손익", "영업이익(손실)", "영업손실"]


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
        result = df[df["stock_code"] == q].copy()
        mode   = f"종목코드 {q}"
    elif q.isdigit() and len(q) == 8:
        result = df[df["corp_code"] == q].copy()
        mode   = f"DART 기업코드 {q}"
    else:
        result = df[df["corp_name"].str.contains(q, na=False, regex=False)].copy()
        mode   = f"회사명 '{q}'"
    result["_listed"] = result["stock_code"].str.strip().ne("")
    result = result.sort_values("_listed", ascending=False).drop(columns="_listed")
    return result.reset_index(drop=True), mode


# ── 금액 파싱 유틸 ──────────────────────────────────────────────────────────

def parse_amount(s: str) -> Optional[float]:
    if not s:
        return None
    cleaned = re.sub(r"[,\s]", "", str(s)).strip()
    if cleaned in ("", "-", "−", "0"):
        return None
    neg = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = re.sub(r"[()△▲]", "", cleaned)
    try:
        v = float(cleaned)
        return -v if neg else v
    except ValueError:
        return None


def detect_unit(text: str) -> str:
    """문서 텍스트에서 '(단위 : 천원)' 같은 패턴 탐지"""
    m = re.search(r"단위\s*[:：]?\s*(십억원|백만원|억원|천원|원)", text)
    return m.group(1) if m else "원"


# ── 1) 정기보고서 API ───────────────────────────────────────────────────────

def _fetch_structured(api_key, corp_code, year, fs_div, diag) -> tuple[Optional[list], str]:
    primary = fs_div
    other   = "OFS" if fs_div == "CFS" else "CFS"
    for endpoint in ("fnlttSinglAcnt", "fnlttSinglAcntAll"):
        for fsdiv in (primary, other):
            try:
                data = requests.get(
                    f"{DART_BASE}/{endpoint}.json",
                    params={"crtfc_key": api_key, "corp_code": corp_code,
                            "bsns_year": str(year), "reprt_code": "11011", "fs_div": fsdiv},
                    timeout=15,
                ).json()
            except Exception as e:
                data = {"status": "ERR", "message": str(e)}
            diag.append(f"{year} · {endpoint} · {fsdiv} → {data.get('status')} ({data.get('message','')})")
            if data.get("status") == "000" and data.get("list"):
                return data["list"], fsdiv
    return None, fs_div


def _extract_from_records(records: list, keywords: list[str]) -> Optional[float]:
    for r in records:
        nm = (r.get("account_nm") or "").strip()
        if any(kw == nm or kw in nm for kw in keywords):
            raw = r.get("thstrm_amount") or r.get("thstrm_add_amount")
            v = parse_amount(raw)
            if v is not None:
                return v
    return None


# ── 2) 감사보고서 원문 파싱 fallback ────────────────────────────────────────

def _find_audit_rcept(api_key, corp_code, fy_year, want_cfs, diag) -> Optional[str]:
    """FY{fy_year} 감사보고서 접수번호 찾기 (보통 다음 해에 제출)"""
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
            if "연결" in nm and cons is None:
                cons = it.get("rcept_no")
            elif "연결" not in nm and sep is None:
                sep = it.get("rcept_no")
        chosen = (cons or sep) if want_cfs else (sep or cons)
        if chosen:
            diag.append(f"{fy_year} · 감사보고서 rcept_no={chosen}")
            return chosen
    return None


def _parse_document(api_key, rcept_no, diag) -> tuple[Optional[float], Optional[float], str]:
    """document.xml 다운로드 → 손익계산서에서 매출액·영업이익 추출 (원 단위로 정규화)"""
    try:
        r = requests.get(f"{DART_BASE}/document.xml",
                         params={"crtfc_key": api_key, "rcept_no": rcept_no}, timeout=40)
    except Exception as e:
        diag.append(f"document.xml → ERR ({e})")
        return None, None, "원"

    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except Exception:
        diag.append(f"document.xml → zip 해제 실패 (status 응답?)")
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

    def scan(keywords):
        for tr in soup.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            label = cells[0].replace(" ", "")
            if any(label == kw or label.startswith(kw) for kw in keywords):
                for c in cells[1:]:
                    m = re.search(r"\(?\s*-?[\d,]{4,}\s*\)?", c)
                    if m:
                        v = parse_amount(m.group())
                        if v is not None:
                            return v
        return None

    rev = scan(REVENUE_KW)
    oi  = scan(OPINC_KW)
    diag.append(f"감사보고서 파싱 → 매출:{rev} 영업이익:{oi} (단위:{unit})")

    # 원 단위로 정규화
    mult = UNIT_WON.get(unit, 1)
    rev_won = rev * mult if rev is not None else None
    oi_won  = oi  * mult if oi  is not None else None
    return rev_won, oi_won, unit


# ── 통합 조회 (연도 1건) ────────────────────────────────────────────────────

@st.cache_data(ttl=3_600, show_spinner=False)
def fetch_year(api_key, corp_code, year, fs_div, dart_unit) -> dict:
    """Returns dict(revenue_won, opinc_won, source, fs, diag)"""
    diag: list = []
    want_cfs = (fs_div == "CFS")

    # 1) 정기보고서
    records, used_fs = _fetch_structured(api_key, corp_code, year, fs_div, diag)
    if records:
        rev = _extract_from_records(records, REVENUE_KW)
        oi  = _extract_from_records(records, OPINC_KW)
        mult = UNIT_WON.get(dart_unit, 1_000)
        return {
            "revenue_won": rev * mult if rev is not None else None,
            "opinc_won":   oi  * mult if oi  is not None else None,
            "source": "정기보고서", "fs": used_fs, "diag": diag,
        }

    # 2) 감사보고서 fallback
    rcept = _find_audit_rcept(api_key, corp_code, year, want_cfs, diag)
    if rcept:
        rev_won, oi_won, _ = _parse_document(api_key, rcept, diag)
        if rev_won is not None or oi_won is not None:
            return {"revenue_won": rev_won, "opinc_won": oi_won,
                    "source": "감사보고서", "fs": fs_div, "diag": diag}

    return {"revenue_won": None, "opinc_won": None, "source": "없음", "fs": fs_div, "diag": diag}


def build_df(years, year_data: dict, display_unit) -> pd.DataFrame:
    div = UNIT_WON.get(display_unit, 100_000_000)
    rows = []
    for yr in years:
        d = year_data.get(yr, {})
        rev = d.get("revenue_won")
        oi  = d.get("opinc_won")
        rows.append({
            "연도":    str(yr),
            "매출액":  rev / div if rev is not None else None,
            "영업이익": oi  / div if oi  is not None else None,
            "출처":    d.get("source", "없음"),
        })
    return pd.DataFrame(rows)


# ── 사이드바 옵션 ───────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 조회 옵션")

    fs_div_label = st.segmented_control("재무제표 구분", options=["연결", "별도"], default="연결")
    fs_div = "CFS" if fs_div_label == "연결" else "OFS"

    period = st.segmented_control("조회 기간", options=[5, 10, 20],
                                  format_func=lambda x: f"{x}년", default=5)

    display_unit = st.segmented_control("표시 단위", options=["백만원", "억원", "십억원"], default="억원")

    st.divider()
    with st.expander("🔧 고급 설정"):
        dart_unit = st.segmented_control(
            "정기보고서 원본 단위", options=["천원", "백만원", "원"], default="천원",
            help="정기보고서 API 금액의 원본 단위. (감사보고서는 자동 인식)",
        )

# ── 회사 검색 ──────────────────────────────────────────────────────────────
st.title("📊 요약재무제표")
st.caption("매출액 · 영업이익 · 영업이익률 | 정기보고서 + 감사보고서 자동 조회")
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
        st.session_state.pop("result_df", None)

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
        years = list(range(LATEST_YEAR - period + 1, LATEST_YEAR + 1))
        year_data: dict = {}
        all_diag: list  = []

        prog = st.progress(0, text="DART에서 데이터 불러오는 중…")
        for i, yr in enumerate(years):
            d = fetch_year(API_KEY, chosen["corp_code"], yr, fs_div, dart_unit)
            year_data[yr] = d
            all_diag.extend(d.get("diag", []))
            prog.progress((i + 1) / len(years), text=f"{yr}년 조회 중…")
        prog.empty()

        df = build_df(years, year_data, display_unit)
        st.session_state["result_df"]   = df
        st.session_state["result_meta"] = {
            "corp_name": chosen["corp_name"], "corp_code": chosen["corp_code"],
            "fs_div": fs_div, "display_unit": display_unit,
        }
        st.session_state["diag"] = all_diag

# ── 결과 출력 ──────────────────────────────────────────────────────────────
if "result_df" in st.session_state:
    df:   pd.DataFrame = st.session_state["result_df"]
    meta: dict         = st.session_state["result_meta"]

    u        = meta["display_unit"]
    fs_label = "연결" if meta["fs_div"] == "CFS" else "별도"
    st.subheader(f"{meta['corp_name']}  |  {fs_label}  |  단위: {u}")

    fmt = df.copy()
    for col in ["매출액", "영업이익"]:
        fmt[col] = fmt[col].apply(lambda x: f"{x:,.1f}" if (x is not None and not pd.isna(x)) else "—")
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
                      yaxis=dict(title=u, gridcolor="#EBEBEB", zeroline=True, zerolinecolor="#CCCCCC"),
                      xaxis=dict(title="", type="category"),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                      margin=dict(t=60, b=30, l=70, r=20), height=430)
    st.plotly_chart(fig, use_container_width=True)

    missing = df[df["매출액"].isna()]["연도"].tolist()
    if missing:
        st.warning(f"⚠️ 다음 연도는 데이터를 가져오지 못했습니다: {', '.join(missing)}")

    # ── 진단 정보 ──────────────────────────────────────────────────────────
    diag = st.session_state.get("diag", [])
    with st.expander("🔍 진단 정보"):
        st.caption(f"기업코드: `{meta.get('corp_code','')}`")
        st.caption("status: 000=정상, 013=정기보고서없음(→감사보고서 시도), 020=호출제한, ERR=오류")
        st.code("\n".join(diag) if diag else "로그 없음", language="text")
