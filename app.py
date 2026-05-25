"""
app.py  ─  메인 진입점 (인증 + 네비게이션)
"""
import streamlit as st

st.set_page_config(
    page_title="DART 재무분석",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ── 홈(대시보드) 페이지 함수 ────────────────────────────────────────────────
def home_page():
    # 제목 크기 축소 (h2 수준)
    st.markdown(
        "<h2 style='margin-bottom:2px;'>📈 DART 재무분석 툴</h2>"
        "<div style='color:#888;font-size:0.9rem;margin-bottom:14px;'>"
        "Highland PE · 내부 전용 | 출처: DART(dart.fss.or.kr)</div>",
        unsafe_allow_html=True,
    )
    st.divider()

    col1, col2 = st.columns(2)
    with col1:
        st.markdown(
            "<div style='border:1px solid #e0e0e0;border-radius:10px;padding:16px;'>"
            "<div style='font-size:1.05rem;font-weight:700;margin-bottom:6px;'>📊 요약재무제표</div>"
            "<div style='color:#555;font-size:0.9rem;line-height:1.5;'>"
            "감사보고서·사업보고서 기반 PE 요약 포맷.<br>"
            "매출·EBITDA·영업이익·순이익, 자산·부채·자본, 현금성자산·총차입금 구성과 추이 차트."
            "</div></div>",
            unsafe_allow_html=True,
        )
        st.page_link("pages/01_재무제표.py", label="요약재무제표 열기 →", use_container_width=True)
    with col2:
        st.markdown(
            "<div style='border:1px solid #eee;border-radius:10px;padding:16px;background:#fafafa;'>"
            "<div style='font-size:1.05rem;font-weight:700;margin-bottom:6px;color:#999;'>🔜 추가 예정</div>"
            "<div style='color:#aaa;font-size:0.9rem;line-height:1.5;'>"
            "공시 검색 · 지분구조 · 밸류에이션 등 (준비 중)"
            "</div></div>",
            unsafe_allow_html=True,
        )


# ── 인증 ───────────────────────────────────────────────────────────────────
if not st.session_state.get("authenticated"):
    st.markdown("<h2>📈 DART 재무분석 툴</h2>", unsafe_allow_html=True)
    st.caption("Highland PE · 내부 전용")
    st.divider()
    with st.form("login_form"):
        pw = st.text_input("비밀번호", type="password", label_visibility="collapsed",
                           placeholder="비밀번호를 입력하세요")
        login_btn = st.form_submit_button("로그인", type="primary", use_container_width=True)
    if login_btn:
        if pw == st.secrets.get("APP_PASSWORD", ""):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("비밀번호가 올바르지 않습니다.")
    st.stop()

# ── 네비게이션 (사이드바에 Home으로 표시) ───────────────────────────────────
pages = [
    st.Page(home_page, title="Home", icon="🏠", default=True),
    st.Page("pages/01_재무제표.py", title="요약재무제표", icon="📊"),
    st.Page("pages/00_연결진단.py", title="연결진단", icon="🩺"),
    st.Page("pages/99_기업목록_생성.py", title="기업목록 생성", icon="🗂️"),
]
nav = st.navigation(pages)
nav.run()
