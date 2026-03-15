# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

YAI(연세대학교 AI 학회) 스터디 보고서 자동 평가 스크립트. Notion DB에서 보고서를 수집하고, GPT-4o로 채점한 뒤 결과를 Google Sheets에 저장한다.

## Setup

```bash
pip3 install requests openai gspread google-auth python-dotenv
```

`.env` 파일 필요:
```
OPENAI_API_KEY=<key>
GOOGLE_SHEET_ID=<sheet_id>
SERVICE_ACCOUNT_FILE=service_account.json  # default
```

Google 서비스 계정 JSON 키 파일(`service_account.json` 또는 `.env`에서 지정한 경로)이 필요하다.

## Running

```bash
# 전체 주차 처리
python3 notion_report_checker.py

# 특정 주차만 처리
python3 notion_report_checker.py --weeks 4주차 5주차

# 특정 Notion 페이지 단어수 디버그
python3 notion_report_checker.py --debug <PAGE_ID>
```

## Architecture

파이프라인 3단계:

1. **Notion 데이터 수집** (`get_all_pages`, `get_page_data`): Notion API로 DB의 모든 페이지를 가져오고, 각 페이지 블록을 재귀 순회해 텍스트·단어수·시각자료 여부를 추출. `ThreadPoolExecutor(max_workers=5)`로 병렬 처리.

2. **GPT-4o 평가** (`evaluate_with_gpt`): 수집한 텍스트를 GPT-4o에 전달해 이해도/가독성/시각자료/토론 4개 항목을 JSON으로 채점. `ThreadPoolExecutor(max_workers=3)`로 병렬 처리.

3. **Google Sheets 저장** (`update_google_sheet`): 기존 시트를 로드해 새 주차 열을 추가하고, 학회원별로 평가 결과를 채움. 시트 이름은 `보고서 제출 현황`으로 고정.

## Key Details

- **Notion API key**는 소스코드 34번 줄에 하드코딩되어 있음 (`NOTION_API_KEY`). 실제 키이므로 커밋 시 주의.
- **단어 수 계산** (`count_words`): 한글/CJK 음절은 1글자=1단어, 영숫자 토큰은 공백 기준으로 카운트 (Notion 방식).
- **Rate limit 처리** (`safe_request`): Notion 429 응답 시 최대 8회 재시도, 대기시간 30초 단위로 증가.
- **Google Sheets 열 구조**: `{주차}_{단어수|이해도(5)|가독성(5)|시각자료(3)|토론(3)|총점|팀유형|평가}` 형식.
- 시각자료 블록 감지: `image`, `equation`, `pdf`, `video` 블록 타입 또는 인라인 수식(`rich_text.type == "equation"`)이 있으면 시각자료로 판단.
- 보고서 텍스트는 8,000자로 잘라 GPT에 전달.
