# DART 재무분석 툴 v2

DART(dart.fss.or.kr) API 기반 재무데이터 조회 Streamlit 앱

## 기능 (v1)

- 회사명 검색 → 기업코드 자동 조회
- 매출액 · 영업이익 · 영업이익률 다년도 추이
- 연결 / 별도 선택 (연결 없는 기업 자동 fallback)
- 조회 기간: 5 / 10 / 20년
- 표시 단위: 백만원 / 억원 / 십억원

## 구조

```
├── app.py                  ← 메인 (비밀번호 인증)
├── pages/
│   └── 01_재무제표.py      ← 요약재무제표
├── core/
│   ├── dart_api.py         ← DART API 호출
│   └── financial.py        ← 데이터 파싱·변환
├── requirements.txt
└── .streamlit/
    └── secrets.toml        ← ⚠️ GitHub 비공개
```

## Streamlit Cloud 배포

1. GitHub 새 레포 생성 후 이 코드 push
1. [share.streamlit.io](https://share.streamlit.io) → New app → 레포 연결
1. Main file: `app.py`
1. Secrets 탭에 추가:

```toml
DART_API_KEY = "발급받은_키"
APP_PASSWORD  = "원하는_비밀번호"
```

## ⚠️ 단위 주의사항

DART API는 금액 단위를 응답에 명시하지 않습니다.

- 대부분 기업: **천원** 단위
- 일부 기업: **백만원** 단위
  → 숫자가 이상하면 사이드바 > 고급 설정 > DART 원본 단위를 조정하세요.