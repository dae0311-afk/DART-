"""
pages/01_재무제표.py  ─  PE 요약재무 (감사보고서 전용)
매출/EBITDA/영업이익/순이익 + BS(자산·부채·자본) + 현금성자산·총차입금(구성 포함)
모든 금액은 원 단위로 추출 후 표시단위로 환산.
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

if not st.session_state.get("authenticated"):
    st.warning("🔐 메인 페이지에서 먼저 로그인하세요.")
    st.stop()

API_KEY     = st.secrets["DART_API_KEY"]
DART_BASE   = "https://opendart.fss.or.kr/api"
LATEST_YEAR = datetime.date.today().year - 1

UNIT_WON = {"원": 1, "천원": 1_000, "백만원": 1_000_000, "억원": 100_000_000, "십억원": 1_000_000_000}
ROMAN    = "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ"
PARSER_VER = "v5"

# ── 계정 매처 (정규화 라벨 기준) ───────────────────────────────────────────
def m_rev(L):  return L.startswith("매출액") or L.startswith("영업수익") or L.startswith("수익(매출") or L == "매출"
def m_oi(L):   return L.startswith("영업이익") or L.startswith("영업손실")
def m_ni(L):   return (L.startswith("당기순이익") or L.startswith("당기순손") or
                       L.startswith("분기순이익") or L.startswith("반기순이익") or
                       L.startswith("연결당기순이익") or L == "당기순이익(손실)")
def m_asset(L):  return L.startswith("자산총계")
def m_liab(L):   return L.startswith("부채총계")
def m_equity(L): return L.startswith("자본총계")
def m_dep(L):    return L.startswith("감가상각비") or L.startswith("감가상각") or "감가상각비" in L
def m_amort(L):  return (L.startswith("무형자산상각") or L.startswith("무형자산상각비")
                         or "무형자산상각" in L or L.startswith("상각비"))
def m_rou(L):    return L.startswith("사용권자산상각") or "사용권자산상각" in L

# 현금성자산 구성 (valuation: 순부채 계산용)
CASH_SPECS = [
    ("현금및현금성자산", lambda L: L.startswith("현금및현금성자산") or L.startswith("현금및현금등가물")),
    ("단기금융상품",     lambda L: L.startswith("단기금융상품")),
    ("단기투자자산",     lambda L: L.startswith("단기투자자산") or L.startswith("단기금융자산")),
    ("유동당기손익금융자산", lambda L: L.startswith("당기손익-공정가치측정금융자산") or L.startswith("단기매매금융자산")),
    ("장기금융상품",     lambda L: L.startswith("장기금융상품")),
]
# 총차입금 구성
DEBT_SPECS = [
    ("단기차입금",       lambda L: L.startswith("단기차입금")),
    ("유동성장기부채",   lambda L: L.startswith("유동성장기부채") or L.startswith("유동성장기차입금")),
    ("유동성사채",       lambda L: L.startswith("유동성사채")),
    ("유동리스부채",     lambda L: L.startswith("유동리스부채") or L.startswith("유동성리스부채")),
    ("사채",             lambda L: L.startswith("사채") or L.startswith("전환사채") or L.startswith("신주인수권부사채")),
    ("장기차입금",       lambda L: L.startswith("장기차입금")),
    ("비유동리스부채",   lambda L: L.startswith("비유동리스부채") or L.startswith("장기리스부채")),
]


# ── 기업코드 검색 ───────────────────────────────────────────────────────────
@st.cache_data(ttl=86_400, show_spinner=False)
def get_corp_list(api_key: str) -> pd.DataFrame:
    resp = requests.get(f"{DART_BASE}/corpCode.xml", params={"crtfc_key": api_key}, timeout=30)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_bytes = zf.read("CORPCODE.xml")
    root = ET.fromstring(xml_bytes)
    rows = [{"corp_code": it.findtext("corp_code", ""), "corp_name": it.findtext("corp_name", ""),
             "stock_code": (it.findtext("stock_code") or "").strip()} for it in root.findall("list")]
    return pd.DataFrame(rows)


def search_corp(api_key: str, query: str) -> tuple[pd.DataFrame, str]:
    df = get_corp_list(api_key); q = query.strip()
    if q.isdigit() and len(q) == 6:
        result = df[df["stock_code"] == q].copy(); mode = f"종목코드 {q}"
    elif q.isdigit() and len(q) == 8:
        result = df[df["corp_code"] == q].copy(); mode = f"DART 기업코드 {q}"
    else:
        result = df[df["corp_name"].str.contains(q, na=False, regex=False)].copy(); mode = f"회사명 '{q}'"
    result["_listed"] = result["stock_code"].str.strip().ne("")
    result = result.sort_values("_listed", ascending=False).drop(columns="_listed")
    return result.reset_index(drop=True), mode


# ── 파싱 유틸 ───────────────────────────────────────────────────────────────
def parse_amount(s: str) -> Optional[float]:
    if not s:
        return None
    t = str(s).strip()
    neg = ("(" in t and ")" in t) or t.startswith("△") or t.startswith("▲") or t.lstrip().startswith("-")
    t = re.sub(r"[(),\s△▲−-]", "", t)
    if not re.search(r"\d", t):
        return None
    try:
        v = float(t); return -v if neg else v
    except ValueError:
        return None


def norm_label(s: str) -> str:
    s = re.sub(r"\s+", "", s)
    return s.lstrip(ROMAN + "0123456789.()IVXivx-·")


def detect_unit(text: str) -> Optional[str]:
    m = re.search(r"단위[\s:：\(]*?(십억원|백만원|억원|천원|원)", text)
    return m.group(1) if m else None


def detect_unit_near(text: str, anchor: int) -> Optional[str]:
    if anchor < 0:
        return None
    seg = text[max(0, anchor - 4000): anchor + 200]
    found = re.findall(r"단위[\s:：\(]*?(십억원|백만원|억원|천원|원)", seg)
    return found[-1] if found else None


# ── 감사보고서 접수번호 ─────────────────────────────────────────────────────
def find_audit_rcept(api_key, corp_code, fy_year, want_cfs, diag):
    for filing_year in (fy_year + 1, fy_year + 2):
        try:
            data = requests.get(f"{DART_BASE}/list.json",
                params={"crtfc_key": api_key, "corp_code": corp_code,
                        "bgn_de": f"{filing_year}0101", "end_de": f"{filing_year}1231",
                        "pblntf_ty": "F", "page_count": 100}, timeout=15).json()
        except Exception as e:
            diag.append(f"{fy_year} · list.json → ERR ({e})"); continue
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
            if cons: return cons, "연결"
            if sep:  return sep, "별도"
        else:
            if sep:  return sep, "별도"
            if cons: return cons, "연결"
    return None, None


# ── 문서 → 전체 계정 추출 ───────────────────────────────────────────────────
def _section_of(label: str) -> Optional[str]:
    """표 제목/헤더 라벨로 재무제표 종류 판별"""
    L = label.replace(" ", "")
    if "현금흐름표" in L:
        return "CF"
    if "손익계산서" in L or "포괄손익" in L:
        return "IS"
    if "재무상태표" in L or "대차대조표" in L:
        return "BS"
    return None


def collect_rows(files):
    """
    모든 표의 (정규화라벨, [셀텍스트...], 섹션) 리스트 (문서 순서).
    섹션은 직전에 등장한 재무제표 제목으로 추정 (CF/IS/BS/None).
    """
    rows = []
    section = None
    for txt in files:
        soup = BeautifulSoup(txt, "html.parser")
        for el in soup.find_all(["tr", "p", "h1", "h2", "h3", "title", "span"]):
            text = el.get_text(" ", strip=True)
            sec = _section_of(text)
            if sec:
                section = sec
            if el.name == "tr":
                cells = [c.get_text(" ", strip=True) for c in el.find_all(["td", "th", "te"])]
                if len(cells) >= 2:
                    rows.append((norm_label(cells[0]), cells[1:], section))
    return rows


def pick_amount(cells):
    for c in cells:
        m = re.search(r"[\(△▲-]?\s*\d{1,3}(?:,\d{3})+\s*\)?", c) or re.search(r"[\(△▲-]?\s*\d{4,}\s*\)?", c)
        if m:
            v = parse_amount(m.group())
            if v is not None and abs(v) >= 1:
                return v
    return None


def find_value(rows, matcher, section=None):
    for label, cells, sec in rows:
        if section is not None and sec != section:
            continue
        if matcher(label):
            v = pick_amount(cells)
            if v is not None:
                return v
    return None


def sum_values(rows, matcher, section=None):
    """매칭되는 모든 행의 금액 합 (CF의 분리된 감가상각비 합산용)"""
    total, hit = 0.0, False
    for label, cells, sec in rows:
        if section is not None and sec != section:
            continue
        if matcher(label):
            v = pick_amount(cells)
            if v is not None:
                total += abs(v)   # CF 가산항목은 양수로 합산
                hit = True
    return (total, hit)


def parse_financials(api_key, rcept_no, diag) -> dict:
    try:
        r = requests.get(f"{DART_BASE}/document.xml",
                         params={"crtfc_key": api_key, "rcept_no": rcept_no}, timeout=40)
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except Exception as e:
        diag.append(f"document → ERR ({e})")
        return {}

    files = []
    for name in zf.namelist():
        raw = zf.read(name); txt = None
        for enc in ("utf-8", "cp949", "euc-kr"):
            try:
                txt = raw.decode(enc); break
            except Exception:
                continue
        files.append(txt if txt is not None else raw.decode("utf-8", errors="ignore"))
    full_text = "\n".join(files)

    anchor = full_text.find("매출액")
    if anchor < 0:
        anchor = full_text.find("영업수익")
    unit = detect_unit_near(full_text, anchor) or detect_unit(full_text) or "원"
    mult = UNIT_WON.get(unit, 1)

    rows = collect_rows(files)

    def g(matcher):
        v = find_value(rows, matcher)
        return v * mult if v is not None else None

    out = {
        "매출액":     g(m_rev),
        "영업이익":   g(m_oi),
        "당기순이익": g(m_ni),
        "자산총계":   g(m_asset),
        "부채총계":   g(m_liab),
        "자본총계":   g(m_equity),
    }
    # 현금성자산 / 총차입금 구성
    cash_bd, debt_bd = {}, {}
    for name, mf in CASH_SPECS:
        v = g(mf)
        if v is not None:
            cash_bd[name] = v
    for name, mf in DEBT_SPECS:
        v = g(mf)
        if v is not None:
            debt_bd[name] = v
    out["cash_bd"] = cash_bd
    out["debt_bd"] = debt_bd
    out["현금성자산"] = sum(cash_bd.values()) if cash_bd else None
    out["총차입금"]   = sum(debt_bd.values()) if debt_bd else None

    # ── D&A: 현금흐름표(CF)에서 우선 합산 추출 ──
    def da_from(section):
        dep, h1 = sum_values(rows, m_dep,   section)
        amo, h2 = sum_values(rows, m_amort, section)
        rou, h3 = sum_values(rows, m_rou,   section)
        return (dep + amo + rou, (h1 or h2 or h3))

    da_won, da_hit = da_from("CF")          # 1순위: 현금흐름표
    da_src = "현금흐름표"
    if not da_hit:                          # 2순위: 섹션 미상(주석 등 전체)
        da_won, da_hit = da_from(None)
        da_src = "주석/전체"
    da_won = da_won * mult if da_hit else None

    out["DA"]     = da_won
    out["DA_src"] = da_src if da_hit else "미발견"

    # ── EBITDA = 영업이익 + D&A ──
    if out["영업이익"] is not None and da_won is not None:
        out["EBITDA"] = out["영업이익"] + da_won
    else:
        out["EBITDA"] = None   # D&A 못 찾으면 부정확하므로 표기 안 함

    diag.append(f"매출:{out['매출액']} 영업:{out['영업이익']} 순익:{out['당기순이익']} "
                f"D&A:{out['DA']}({out['DA_src']}) EBITDA:{out['EBITDA']} 자산:{out['자산총계']} (단위:{unit})")
    if out["매출액"] is None and out["자산총계"] is None:
        diag.append(f"  '매출액'존재={'매출액' in full_text} '자산총계'존재={'자산총계' in full_text}")
        if anchor >= 0:
            diag.append("  매출액문맥: " + re.sub(r"\s+", " ", full_text[anchor:anchor + 120]))
    return out


@st.cache_data(ttl=3_600, show_spinner=False)
def fetch_year(api_key, corp_code, year, want_cfs) -> dict:
    diag = []
    rcept, kind = find_audit_rcept(api_key, corp_code, year, want_cfs, diag)
    if not rcept:
        return {"kind": "없음", "diag": diag}
    data = parse_financials(api_key, rcept, diag)
    data["kind"] = kind
    data["diag"] = diag
    return data


def run_fetch(corp_code, want_cfs, period):
    max_mode = (period == "최대")
    if max_mode:
        years_to_fetch = list(range(LATEST_YEAR, 1998, -1))
    else:
        # 표시기간 + 성장률 계산용 직전 1년
        years_to_fetch = list(range(LATEST_YEAR, LATEST_YEAR - int(period) - 1, -1))

    year_data, all_diag = {}, []
    consec, found = 0, False
    for yr in years_to_fetch:
        d = fetch_year(API_KEY, corp_code, yr, want_cfs)
        year_data[yr] = d
        all_diag.extend(d.get("diag", []))
        has = any(d.get(k) is not None for k in ("매출액", "자산총계", "영업이익"))
        if has:
            found = True; consec = 0
        else:
            consec += 1
        if max_mode and found and consec >= 3:
            break

    if max_mode:
        disp_start = min(year_data.keys())
    else:
        disp_start = LATEST_YEAR - int(period) + 1
    return year_data, disp_start, all_diag, max_mode


# ── 포맷 유틸 ───────────────────────────────────────────────────────────────
def compress_years(yrs) -> str:
    nums = sorted({int(y) for y in yrs})
    if not nums:
        return ""
    parts, start, prev = [], nums[0], nums[0]
    for y in nums[1:]:
        if y == prev + 1:
            prev = y
        else:
            parts.append(f"{start}~{prev}" if start != prev else f"{start}"); start = prev = y
    parts.append(f"{start}~{prev}" if start != prev else f"{start}")
    return ", ".join(parts)


def fmt_val(x):
    if x is None or pd.isna(x):
        return "—"
    return f"({abs(x):,.0f})" if x < 0 else f"{x:,.0f}"


def fmt_pct(x):
    if x is None or pd.isna(x):
        return "—"
    return f"({abs(x):.1f}%)" if x < 0 else f"{x:.1f}%"


# ── 사이드바 ────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 조회 옵션")
    fs_div_label = st.segmented_control("재무제표 구분", options=["연결", "별도"], default="연결")
    want_cfs = (fs_div_label == "연결")
    period = st.segmented_control("조회 기간", options=[5, 10, 20, "최대"],
                                  format_func=lambda x: (f"{x}년" if isinstance(x, int) else x), default=5)
    display_unit = st.segmented_control("표시 단위", options=["백만원", "억원", "십억원"], default="억원")
    st.caption("옵션을 바꾸면 결과가 즉시 갱신됩니다.")

# ── 검색 ────────────────────────────────────────────────────────────────────
st.title("📊 요약재무제표")
st.caption("감사보고서 기준 · PE 요약 포맷 (원 단위 추출)")
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
        for k in ("year_data", "disp_start", "result_meta", "active_corp", "fetch_sig"):
            st.session_state.pop(k, None)

# ── 회사 선택 ──────────────────────────────────────────────────────────────
if "search_results" in st.session_state:
    results = st.session_state["search_results"]
    def make_label(row):
        return f"{row['corp_name']}  ({row['stock_code']})" if row["stock_code"] else row["corp_name"]
    labels   = results.apply(make_label, axis=1).tolist()
    chosen_i = st.selectbox("회사 선택", range(len(labels)), format_func=lambda i: labels[i])
    chosen   = results.iloc[chosen_i]
    st.divider()
    if st.button("📥 재무데이터 조회", type="primary", use_container_width=True):
        st.session_state["active_corp"] = {"corp_code": chosen["corp_code"], "corp_name": chosen["corp_name"]}
        st.session_state.pop("fetch_sig", None)

# ── 조회 실행 (옵션 변경 시 자동) ───────────────────────────────────────────
if "active_corp" in st.session_state:
    ac  = st.session_state["active_corp"]
    sig = (ac["corp_code"], want_cfs, period)
    if st.session_state.get("fetch_sig") != sig:
        with st.spinner("감사보고서 조회 중…"):
            year_data, disp_start, all_diag, max_mode = run_fetch(ac["corp_code"], want_cfs, period)
        st.session_state["year_data"]   = year_data
        st.session_state["disp_start"]  = disp_start
        st.session_state["result_meta"] = {"corp_name": ac["corp_name"], "corp_code": ac["corp_code"],
                                           "want_cfs": want_cfs, "max_mode": max_mode}
        st.session_state["diag"]        = all_diag
        st.session_state["fetch_sig"]   = sig

# ── 결과 출력 ──────────────────────────────────────────────────────────────
if "year_data" in st.session_state:
    year_data  = st.session_state["year_data"]
    disp_start = st.session_state["disp_start"]
    meta       = st.session_state["result_meta"]
    div        = UNIT_WON[display_unit]

    # 표시 연도: 요청 시작연도 이상 & 데이터 있는 연도
    cand = sorted(y for y in year_data if y >= disp_start)
    disp_years = [y for y in cand if any(year_data[y].get(k) is not None
                                         for k in ("매출액", "자산총계", "영업이익", "당기순이익"))]

    # 헤더 pill
    req_label = "연결" if meta["want_cfs"] else "별도"
    st.markdown(
        f'<div style="margin:2px 0 10px 0;">'
        f'<span style="background:#1E3D6B;color:#fff;padding:4px 14px;border-radius:16px;font-size:0.95rem;margin-right:6px;">{meta["corp_name"]}</span>'
        f'<span style="background:#2E6DA4;color:#fff;padding:4px 14px;border-radius:16px;font-size:0.95rem;margin-right:6px;">{req_label}</span>'
        f'<span style="background:#E8EEF6;color:#1E3D6B;padding:4px 14px;border-radius:16px;font-size:0.95rem;">{display_unit}</span>'
        f'</div>', unsafe_allow_html=True)

    if not disp_years:
        st.error("선택한 기간 내 공시된 데이터가 없습니다.")
        with st.expander("🔍 진단 정보", expanded=True):
            st.caption(f"파서 {PARSER_VER} · 기업코드 `{meta['corp_code']}`")
            st.code("\n".join(st.session_state.get("diag", [])) or "로그 없음")
        st.stop()

    start_year = disp_years[0]
    if start_year > disp_start:
        st.info(f"ℹ️ 데이터는 **{start_year}년부터** 공시되어 이전 기간은 제외했습니다.")
    if meta["want_cfs"]:
        fb = [y for y in disp_years if year_data[y].get("kind") == "별도"]
        if fb:
            st.info(f"ℹ️ {compress_years(fb)}년은 연결감사보고서가 없어 **별도** 기준으로 표시했습니다.")

    # ── 시리즈 헬퍼 ──
    def val(y, key):
        v = year_data[y].get(key)
        return v / div if v is not None else None

    def raw(y, key):
        return year_data[y].get(key)

    def growth(y):
        cur, prev = raw(y, "매출액"), year_data.get(y - 1, {}).get("매출액")
        return (cur / prev - 1) * 100 if (cur and prev) else None

    def margin(y, key):
        num, rev = raw(y, key), raw(y, "매출액")
        return num / rev * 100 if (num is not None and rev) else None

    # ── 요약표 (HTML) ──
    ys = disp_years
    header = "".join(f'<th style="background:#3A4A5E;color:#fff;text-align:right;padding:6px 12px;">{y}</th>' for y in ys)
    def row_main(label, key):
        tds = "".join(f'<td style="text-align:right;padding:5px 12px;">{fmt_val(val(y,key))}</td>' for y in ys)
        return f'<tr><td style="padding:5px 10px;font-weight:600;">{label}</td>{tds}</tr>'
    def row_sub(label, key):
        tds = "".join(f'<td style="text-align:right;padding:4px 12px;font-style:italic;color:#555;">{fmt_val(val(y,key))}</td>' for y in ys)
        return f'<tr><td style="padding:4px 10px 4px 22px;font-style:italic;color:#555;">{label}</td>{tds}</tr>'
    def row_pct(label, fn):
        tds = "".join(f'<td style="text-align:right;padding:4px 12px;font-style:italic;color:#777;">{fmt_pct(fn(y))}</td>' for y in ys)
        return f'<tr><td style="padding:4px 10px 4px 22px;font-style:italic;color:#777;">{label}</td>{tds}</tr>'

    html = f"""
    <table style="border-collapse:collapse;width:100%;font-size:0.9rem;">
      <tr><th style="background:#3A4A5E;color:#fff;text-align:left;padding:6px 10px;">(단위 : {display_unit})</th>{header}</tr>
      {row_main("매출액","매출액")}
      {row_pct("Growth", growth)}
      {row_main("EBITDA","EBITDA")}
      {row_pct("Margin", lambda y: margin(y,"EBITDA"))}
      {row_main("영업이익","영업이익")}
      {row_pct("Margin", lambda y: margin(y,"영업이익"))}
      {row_main("당기순이익","당기순이익")}
      {row_pct("Margin", lambda y: margin(y,"당기순이익"))}
      {row_main("자산총계","자산총계")}
      {row_sub("현금성자산","현금성자산")}
      {row_main("부채총계","부채총계")}
      {row_sub("총차입금","총차입금")}
      {row_main("자본총계","자본총계")}
    </table>
    """
    st.markdown(html, unsafe_allow_html=True)

    # EBITDA D&A 출처 표기 (검증용)
    da_srcs = {year_data[y].get("DA_src", "미발견") for y in ys}
    da_note = ", ".join(sorted(da_srcs))
    missing_da = [str(y) for y in ys if year_data[y].get("DA") is None]
    note = f"EBITDA = 영업이익 + 감가상각비 + 무형자산상각비 + 사용권자산상각비 (D&A 출처: {da_note})"
    if missing_da:
        note += f" · ⚠️ {', '.join(missing_da)}년은 D&A 미발견으로 EBITDA 공란"
    st.caption(note)
    st.write("")

    # ── 차트 헬퍼 ──
    yr_str = [str(y) for y in ys]

    def bar_line(title, bar_key, line_fn, line_name, bar_color="#1E3D6B"):
        bar_vals  = [val(y, bar_key) for y in ys]
        line_vals = [line_fn(y) for y in ys]
        fig = go.Figure()
        fig.add_trace(go.Bar(x=yr_str, y=bar_vals, name=bar_key if title is None else title.split("·")[0].strip(),
                             marker_color=bar_color, yaxis="y",
                             text=[fmt_val(v) for v in bar_vals], textposition="outside", textfont=dict(size=10)))
        fig.add_trace(go.Scatter(x=yr_str, y=line_vals, name=line_name, yaxis="y2",
                                 mode="lines+markers", line=dict(color="#E67E22", width=2)))
        fig.update_layout(title=title, barmode="group", plot_bgcolor="white", paper_bgcolor="white",
                          yaxis=dict(title=display_unit, gridcolor="#EBEBEB"),
                          yaxis2=dict(overlaying="y", side="right", showgrid=False, ticksuffix="%"),
                          xaxis=dict(type="category"),
                          legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                          margin=dict(t=60, b=30, l=60, r=60), height=360)
        st.plotly_chart(fig, use_container_width=True)

    bar_line(f"매출액 · 성장률", "매출액", growth, "성장률(%)")
    bar_line(f"EBITDA · 마진", "EBITDA", lambda y: margin(y, "EBITDA"), "EBITDA 마진(%)")
    bar_line(f"영업이익 · 마진", "영업이익", lambda y: margin(y, "영업이익"), "영업이익률(%)")
    bar_line(f"당기순이익 · 마진", "당기순이익", lambda y: margin(y, "당기순이익"), "순이익률(%)")

    # 5번째: BS 멀티라인
    bs_series = [
        ("자산총계",  "자산총계",  "#1E3D6B"),
        ("현금성자산", "현금성자산", "#6FA8DC"),
        ("부채총계",  "부채총계",  "#C0392B"),
        ("총차입금",  "총차입금",  "#E8A29A"),
        ("자본총계",  "자본총계",  "#27AE60"),
    ]
    fig5 = go.Figure()
    for name, key, color in bs_series:
        fig5.add_trace(go.Scatter(x=yr_str, y=[val(y, key) for y in ys], name=name,
                                  mode="lines+markers", line=dict(color=color, width=2)))
    fig5.update_layout(title="재무상태 (자산·부채·자본 / 현금성자산·총차입금)",
                       plot_bgcolor="white", paper_bgcolor="white",
                       yaxis=dict(title=display_unit, gridcolor="#EBEBEB"), xaxis=dict(type="category"),
                       legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                       margin=dict(t=60, b=30, l=60, r=20), height=380)
    st.plotly_chart(fig5, use_container_width=True)

    # ── 구성 테이블 (현금성자산 / 총차입금) ──
    def breakdown_df(bd_key, total_key):
        comps = []
        for y in ys:
            comps += list(year_data[y].get(bd_key, {}).keys())
        comps = list(dict.fromkeys(comps))  # 순서 유지 중복 제거
        rows = {}
        for comp in comps:
            rows[comp] = [(year_data[y].get(bd_key, {}).get(comp, None)) for y in ys]
        # 합계
        rows["합계"] = [raw(y, total_key) for y in ys]
        df = pd.DataFrame(rows, index=[str(y) for y in ys]).T
        # 표시단위 환산 + 포맷
        return df.applymap(lambda v: fmt_val(v / div) if v is not None else "—")

    cc, dc = st.columns(2)
    with cc:
        st.markdown("**현금성자산 구성**")
        st.dataframe(breakdown_df("cash_bd", "현금성자산"), use_container_width=True)
        st.caption("순부채 산정용. 현금및현금성자산 + 단기금융상품/투자자산 등 valuation 관점 포함.")
    with dc:
        st.markdown("**총차입금 구성**")
        st.dataframe(breakdown_df("debt_bd", "총차입금"), use_container_width=True)
        st.caption("단기·장기차입금 + 사채 + 유동성장기부채 + 리스부채 포함.")

    diag = st.session_state.get("diag", [])
    with st.expander("🔍 진단 정보"):
        st.caption(f"파서 {PARSER_VER} · 기업코드 `{meta['corp_code']}`")
        st.code("\n".join(diag) if diag else "로그 없음")
