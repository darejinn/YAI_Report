# YAI 보고서 자동 평가 스크립트

연세대학교 AI 학회(YAI) 스터디 보고서를 Notion DB에서 자동으로 수집하고, Claude AI로 채점한 뒤 결과를 Google Sheets에 저장하는 스크립트입니다.

---

## 동작 방식

```
Notion DB → 보고서 텍스트 수집 → Claude AI 채점 → Google Sheets 저장
```

1. **Notion 수집**: DB의 모든 페이지를 가져와 블록을 재귀 순회하며 텍스트·단어수·시각자료 여부 추출
2. **Claude 채점**: 수집한 텍스트를 Claude Haiku에 전달해 이해도/가독성/시각자료/토론 4개 항목을 JSON으로 채점
3. **Google Sheets 저장**: 평가 결과를 "보고서 제출 현황" 시트에 기록하고, "주차별 요약" 시트에 등수·미제출·글자수 부족을 정리

---

## 설치

```bash
pip install requests anthropic gspread google-auth python-dotenv
```

---

## 환경 설정

### 1. `.env` 파일 생성

프로젝트 루트에 `.env` 파일을 만들고 아래 항목을 입력합니다.

```env
# Notion API 키 (https://www.notion.so/my-integrations 에서 발급)
NOTION_API_KEY=ntn_xxxxxxxxxxxxxxxxxxxxxxxx

# Anthropic API 키 (https://console.anthropic.com 에서 발급)
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxx

# Google Sheets 설정
GOOGLE_SHEET_ID=your_google_sheet_id_here
SERVICE_ACCOUNT_FILE=service_account.json
```

> **GOOGLE_SHEET_ID**: 시트 URL `https://docs.google.com/spreadsheets/d/[여기]/edit` 에서 `[여기]` 부분

### 2. Google 서비스 계정 설정

1. [Google Cloud Console](https://console.cloud.google.com) 접속
2. 새 프로젝트 생성 → **Google Sheets API** + **Google Drive API** 활성화
3. IAM → 서비스 계정 생성 → JSON 키 다운로드
4. 다운로드한 파일을 프로젝트 폴더에 저장 (`.env`의 `SERVICE_ACCOUNT_FILE` 경로와 일치해야 함)
5. Google Sheet을 열고 → 공유 → 서비스 계정 이메일을 **편집자**로 추가

### 3. Notion 통합 설정

1. [Notion Integrations](https://www.notion.so/my-integrations) 에서 통합 생성
2. 보고서 DB가 있는 Notion 페이지 → 우측 상단 `...` → **연결** → 생성한 통합 추가

---

## 실행 방법

```bash
# 4주차~7주차 처리
python3 notion_report_checker.py --weeks 4주차 5주차 6주차 7주차

# 전체 주차 처리 (Notion DB에 있는 모든 주차)
python3 notion_report_checker.py

# 특정 페이지 단어수 디버그 (PAGE_ID는 Notion 페이지 URL 끝 32자리)
python3 notion_report_checker.py --debug <PAGE_ID>
```

> **권장 실행 환경**: `conda activate report` 후 실행
> ```bash
> /opt/anaconda3/envs/report/bin/python3 notion_report_checker.py --weeks 4주차 5주차 6주차 7주차
> ```

---

## 출력 결과

### Google Sheets — "보고서 제출 현황" 탭

| 학회원 | 팀 | 4주차_단어수 | 4주차_이해도(5) | 4주차_가독성(5) | 4주차_시각자료(3) | 4주차_토론(3) | 4주차_총점 | 4주차_팀유형 | 4주차_평가 | … |
|---|---|---|---|---|---|---|---|---|---|---|

- 단어수 700 미만이면 `⚠️ 588` 형식으로 표시

### Google Sheets — "주차별 요약" 탭

| 구분 | 4주차 | 5주차 | … |
|---|---|---|---|
| 🏆 1위 | NLP팀: 김야이 [5/4/3/2] | … | |
| 🏆 2위 | … | | |
| 🏆 3위 | … | | |
| ⚠️ 글자수 부족 | 박야이 (588단어) | … | |
| ✅ 제출 | 김철수, 이영희, … | … | |
| ❌ 미제출 | 홍길동 | … | |

> 점수 형식: `[이해도/가독성/시각자료/토론]`

---

## 채점 기준

`eval_criteria.py`에서 관리합니다. Claude에게 전달하는 시스템 프롬프트가 담겨 있으며, 4개 항목으로 채점합니다.

| 항목 | 만점 | 설명 |
|---|---|---|
| 이해도 | 5점 | 논문/개념 이해 수준 |
| 가독성 | 5점 | 구조화·가독성 |
| 시각자료 | 3점 | 이미지·수식·도표 활용 |
| 토론 | 3점 | 비판적 사고·토론 기여도 |
| **총점** | **16점** | |

---

## 출결 체크 대상

`notion_report_checker.py` 상단의 `FIXED_MEMBERS` 집합에서 관리합니다. 글자수 부족·미제출 체크는 이 목록에 포함된 학회원만 대상으로 합니다.

---

## 캐싱

한 번 평가된 보고서는 `eval_cache.json`에 저장되어, 다음 실행 시 재평가하지 않습니다. 재평가가 필요한 경우 해당 항목을 `eval_cache.json`에서 삭제하거나 파일 전체를 삭제하면 됩니다.

---

## 주의사항

- `.env` 파일과 서비스 계정 JSON 파일은 `.gitignore`에 포함되어 있으므로 **절대 커밋하지 마세요.**
- Notion API rate limit(429) 발생 시 자동으로 최대 8회 재시도합니다.
- 보고서 텍스트는 8,000자까지만 Claude에 전달됩니다.
