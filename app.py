"""
app.py  ─  메인 진입점 (비밀번호 인증)
"""
import streamlit as st

st.set_page_config(
    page_title="DART 재무분석",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── 인증 ───────────────────────────────────────────────────────────────────
if not st.session_state.get("authenticated"):
    st.title("📈 DART 재무분석 툴")
    st.caption("Highland PE · 내부 전용")
    st.divider()

    pw = st.text_input("비밀번호", type="password", key="pw_input", label_visibility="collapsed",
                       placeholder="비밀번호를 입력하세요")
    if st.button("로그인", type="primary", use_container_width=True):
        if pw == st.secrets.get("APP_PASSWORD", ""):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("비밀번호가 올바르지 않습니다.")
    st.stop()

# ── 메인 화면 ──────────────────────────────────────────────────────────────
st.title("📈 DART 재무분석 툴")
st.caption("출처: DART(dart.fss.or.kr) | 사업보고서 기준")
st.divider()

col1, col2 = st.columns(2)
with col1:
    st.info("**📊 요약재무제표**\n\n매출액 · 영업이익 다년도 추이 조회\n\n← 왼쪽 메뉴에서 선택")
with col2:
    st.info("**🔜 추가 예정**\n\nBS / CF · EBITDA · 공시 검색 · 지분구조 등")
