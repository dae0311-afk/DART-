"""
pages/00_연결진단.py
DART 서버에 연결 자체가 되는지 1초 만에 확인.
'코드 문제'인지 '망(Streamlit↔DART) 문제'인지 가른다.
"""
from __future__ import annotations
import time
import requests
import streamlit as st


if not st.session_state.get("authenticated"):
    st.warning("🔐 메인 페이지에서 먼저 로그인하세요.")
    st.stop()

API_KEY = st.secrets["DART_API_KEY"]
st.title("🩺 DART 연결 진단")

tests = [
    ("① 단건 company.json (초경량)",
     "https://opendart.fss.or.kr/api/company.json",
     {"crtfc_key": API_KEY, "corp_code": "00126380"}),
    ("② 공시목록 list.json (경량)",
     "https://opendart.fss.or.kr/api/list.json",
     {"crtfc_key": API_KEY, "corp_code": "00126380",
      "bgn_de": "20240101", "end_de": "20241231", "page_count": 10}),
    ("③ 전체 corpCode.xml (대용량)",
     "https://opendart.fss.or.kr/api/corpCode.xml",
     {"crtfc_key": API_KEY}),
]

if st.button("🚀 연결 테스트 실행", type="primary", use_container_width=True):
    for name, url, params in tests:
        t0 = time.time()
        try:
            r = requests.get(url, params=params, timeout=(10, 60))
            dt = time.time() - t0
            st.success(f"{name} → ✅ HTTP {r.status_code} · {len(r.content):,}B · {dt:.1f}초")
        except Exception as e:
            dt = time.time() - t0
            st.error(f"{name} → ❌ {type(e).__name__} · {dt:.1f}초 만에 실패")

    st.divider()
    st.markdown(
        "**해석**\n"
        "- ①②③ 전부 성공 → 망 정상. 코드 로직을 더 봐야 함\n"
        "- ①② 성공, ③만 실패 → 대용량 다운로드만 불안정 → **corpcode.csv 파일 방식**이 정답\n"
        "- ①②③ 전부 실패 → Streamlit↔DART 망 자체 차단 → 코드로 해결 불가"
    )
