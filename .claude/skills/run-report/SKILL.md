---
name: run-report
description: YAI 보고서 평가 스크립트 실행 및 자동 디버깅. 특정 주차 처리, 전체 실행, 디버그 모드를 지원한다.
argument-hint: "[4주차 5주차 | --all | --debug <PAGE_ID>]"
allowed-tools: Bash, Read, Edit, Grep
---

# YAI 보고서 평가 스크립트 실행

작업 디렉토리: `/Users/yoonjincho/Desktop/보고서_평가`
스크립트: `notion_report_checker.py`
실행 인터프리터: `python3` (conda 환경 `report` 사용)

## 실행 방법 결정

인자(`$ARGUMENTS`)를 확인해 아래 중 하나를 선택한다:

- 인자가 없거나 `--all` → 전체 주차 처리
  ```bash
  cd /Users/yoonjincho/Desktop/보고서_평가 && conda run -n report python3 notion_report_checker.py
  ```

- `--debug <PAGE_ID>` 형식 → 단어수 디버그 모드
  ```bash
  cd /Users/yoonjincho/Desktop/보고서_평가 && conda run -n report python3 notion_report_checker.py --debug <PAGE_ID>
  ```

- 그 외 (예: `4주차 5주차`) → 특정 주차만 처리
  ```bash
  cd /Users/yoonjincho/Desktop/보고서_평가 && conda run -n report python3 notion_report_checker.py --weeks $ARGUMENTS
  ```

## 실행 절차

1. **스크립트 실행**: 위 명령어 중 해당하는 것을 Bash로 실행한다.
2. **출력 모니터링**: 실행 중 출력을 그대로 사용자에게 보여준다.
3. **에러 발생 시 자동 디버깅**:
   a. 트레이스백에서 파일명과 줄 번호를 파악한다.
   b. `notion_report_checker.py`의 해당 줄 주변을 Read로 읽는다.
   c. 원인을 분석하고 수정안을 제시한다.
   d. 사용자 승인 후 Edit으로 수정하고 재실행한다.

## 주요 에러 패턴 및 대응

| 에러 | 원인 | 대응 |
|------|------|------|
| `HTTPError: 429` | Notion Rate limit | 자동 재시도됨 — 기다리면 해결 |
| `HTTPError: 500` | Notion 서버 오류 | 자동 재시도됨 — 기다리면 해결 |
| `SpreadsheetNotFound` | GOOGLE_SHEET_ID 오류 또는 서비스 계정 공유 안 됨 | .env 확인 및 공유 설정 안내 |
| `FileNotFoundError: *.json` | 서비스 계정 JSON 파일 없음 | SERVICE_ACCOUNT_FILE 경로 확인 |
| `AuthenticationError` | OpenAI API 키 오류 | .env의 OPENAI_API_KEY 확인 |
| `JSONDecodeError` | GPT 응답 파싱 실패 | 해당 보고서만 건너뜀 — 재실행 시 해결 |
| `RecursionError` | 블록 중첩 깊이 초과 | 해당 블록 건너뜀 처리됨 |

## 성공 시 보고

실행이 완료되면 아래 내용을 요약해서 보여준다:
- 처리된 보고서 수 (N명 × M주차)
- GPT 평가 완료 수
- Google Sheet 링크
- 경고(⚠️)가 있었다면 해당 내용
