"""
YAI 보고서 제출 현황 자동 체크 스크립트
------------------------------------------
사용법:
  1. pip3 install requests anthropic gspread google-auth python-dotenv
  2. Google Cloud Console에서 서비스 계정 설정:
     a) https://console.cloud.google.com 접속
     b) 새 프로젝트 생성 → Google Sheets API + Google Drive API 활성화
     c) IAM → 서비스 계정 생성 → JSON 키 다운로드 → service_account.json으로 저장
     d) Google Sheet 생성 → 서비스 계정 이메일을 편집자로 공유
     e) Sheet URL의 /d/XXXX/edit 에서 XXXX 복사
  3. .env 파일에 아래 항목 추가:
     GOOGLE_SHEET_ID=<복사한 ID>
     SERVICE_ACCOUNT_FILE=service_account.json
  4. python3 notion_report_checker.py
"""

import re
import os
import json
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from eval_criteria import EVAL_SYSTEM_PROMPT  # 평가 기준은 eval_criteria.py에서 관리

load_dotenv()

# ────────────────────────────────────────────
# 설정
# ────────────────────────────────────────────
NOTION_API_KEY       = os.getenv("NOTION_API_KEY")
DB_ID                = "2c478a7dac4681729b02edcf77e79c59"
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_SHEET_ID      = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")

HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2025-09-03",
    "Content-Type": "application/json",
}

# Google Sheets 각 주차별 세부 열 순서
WEEK_SUBCOLS = ["단어수", "이해도(5)", "가독성(5)", "시각자료(3)", "토론(3)", "총점", "팀유형", "평가"]

# 평가 캐시 파일 (재실행 시 이미 평가된 보고서 스킵)
CACHE_FILE = "eval_cache.json"

# 출결 체크 대상 고정 멤버 (글자수 부족 / 미제출 체크 범위)
# ※ 실제 학회원 이름으로 교체하세요. Notion의 Person 속성에 표시되는 이름과 정확히 일치해야 합니다.
FIXED_MEMBERS = {
    "김철수", "김영희", "박야이", "이야이", "최야이", "정야이", "한야이",
    "유야이", "임야이", "오야이", "신야이", "강야이", "고야이", "곽야이",
    "구야이", "김야이", "나야이", "노야이", "문야이",
    "박야이2", "배야이", "서야이", "손야이", "송야이", "안야이", "양야이",
    "엄야이", "윤야이", "이야이2", "장야이", "전야이",
}

# EVAL_SYSTEM_PROMPT는 eval_criteria.py에서 import됨

# ────────────────────────────────────────────
# 이름 정제
# ────────────────────────────────────────────

def clean_name(name):
    """괄호 안 내용 제거 및 앞뒤 공백 정리
    예: '(의과대학 의학과) 노승현' → '노승현'
    예: '노승현 (의학과)'         → '노승현'
    """
    name = re.sub(r'\s*\([^)]*\)\s*', ' ', name)
    return name.strip()


# ────────────────────────────────────────────
# Notion API
# ────────────────────────────────────────────

def safe_request(method, url, retries=8, **kwargs):
    for attempt in range(retries):
        try:
            r = getattr(requests, method)(url, headers=HEADERS, timeout=30, **kwargs)
            if r.status_code == 429:
                wait = min(5 * (attempt + 1), 60)
                print(f"   Notion rate limited, {wait}초 후 재시도... ({attempt+1}/{retries})")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                wait = 5 * (attempt + 1)
                print(f"   Notion 서버 오류({r.status_code}), {wait}초 후 재시도... ({attempt+1}/{retries})")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except requests.exceptions.ConnectionError:
            if attempt < retries - 1:
                time.sleep(5)
            else:
                raise
    raise RuntimeError(f"재시도 {retries}회 초과: {url}")


def safe_get(url, **kwargs):
    return safe_request("get", url, **kwargs)


def get_data_source_ids():
    url = f"https://api.notion.com/v1/databases/{DB_ID}"
    r = safe_get(url)
    data = r.json()
    sources = data.get("data_sources", [])
    if sources:
        return [s["id"] for s in sources]
    return [DB_ID]


def query_data_source(ds_id):
    pages, payload = [], {"page_size": 100}
    url = f"https://api.notion.com/v1/data_sources/{ds_id}/query"
    while True:
        r = safe_request("post", url, json=payload)
        data = r.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    return pages


def get_all_pages():
    ds_ids = get_data_source_ids()
    print(f"   → data source {len(ds_ids)}개 발견: {ds_ids}")
    pages = []
    for ds_id in ds_ids:
        pages.extend(query_data_source(ds_id))
    return pages


_CJK = re.compile(r'[\uac00-\ud7af\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]')


def count_words(text):
    """한글/CJK 음절은 1글자=1단어, 나머지는 영숫자 포함 토큰만 카운트 (Notion 방식)"""
    if not text.strip():
        return 0
    cjk_count = len(_CJK.findall(text))
    non_cjk = _CJK.sub(' ', text)
    non_cjk_count = len([w for w in non_cjk.split() if re.search(r'[a-zA-Z0-9]', w)])
    return cjk_count + non_cjk_count


def get_page_data(page_id, title=""):
    """페이지의 (단어수, 전체 텍스트, 시각자료 개수)를 한 번에 반환.
    시각자료 = image / equation(블록) / video 블록 + 인라인 equation의 합산."""
    total_words  = count_words(title)
    text_parts   = [title] if title.strip() else []
    visual_count = 0  # bool 대신 개수로 카운트

    def _traverse(pid):
        nonlocal total_words, visual_count
        url = f"https://api.notion.com/v1/blocks/{pid}/children?page_size=100"
        while url:
            r    = safe_get(url)
            data = r.json()
            for block in data.get("results", []):
                btype   = block.get("type", "")
                content = block.get(btype, {})

                # 시각 자료 블록 카운트 (pdf는 내용 파악 불가이므로 제외)
                if btype in ("image", "equation", "video"):
                    visual_count += 1

                if btype == "table_row":
                    for cell in content.get("cells", []):
                        for rt in cell:
                            txt = rt.get("plain_text", "")
                            total_words += count_words(txt)
                            if txt.strip():
                                text_parts.append(txt)
                else:
                    for rt in content.get("rich_text", []):
                        txt = rt.get("plain_text", "")
                        total_words += count_words(txt)
                        if txt.strip():
                            text_parts.append(txt)
                        # 인라인 수식도 시각자료로 카운트
                        if rt.get("type") == "equation":
                            visual_count += 1

                if block.get("has_children"):
                    try:
                        _traverse(block["id"])
                    except Exception as e:
                        print(f"   ⚠️ 블록 {block['id']} 건너뜀: {e}")

            url = (
                f"https://api.notion.com/v1/blocks/{pid}/children"
                f"?page_size=100&start_cursor={data['next_cursor']}"
                if data.get("has_more") else None
            )

    try:
        _traverse(page_id)
    except Exception as e:
        print(f"   ⚠️ 페이지 {page_id} 일부 블록 수집 실패: {e}")

    full_text = "\n".join(text_parts)
    if len(full_text) > 8000:
        full_text = full_text[:8000] + "\n... (이하 생략)"

    return total_words, full_text, visual_count


def extract_info(page):
    """페이지 속성에서 (제목, 이름, 주차, 팀이름) 추출"""
    props = page["properties"]

    name = ""
    title_prop = props.get("Name", {}).get("title", [])
    if title_prop:
        name = title_prop[0].get("plain_text", "")

    person = ""
    people = props.get("Person", {}).get("people", [])
    if people:
        person = clean_name(people[0].get("name", ""))

    week = ""
    week_prop = props.get("작성 주차", {})
    wtype = week_prop.get("type", "")
    if wtype == "select" and week_prop.get("select"):
        week = week_prop["select"]["name"]
    elif wtype == "multi_select" and week_prop.get("multi_select"):
        week = week_prop["multi_select"][0]["name"]
    elif wtype == "rich_text" and week_prop.get("rich_text"):
        week = week_prop["rich_text"][0].get("plain_text", "")
    elif wtype == "number" and week_prop.get("number") is not None:
        week = f"{int(week_prop['number'])}주차"

    # 보고서 제목에서 팀이름 추출 (예: 'NLP팀_홍길동_4주차', '[CV팀] 리뷰')
    m = re.search(r'[A-Za-z가-힣0-9]+팀', name)
    team = m.group(0) if m else ""

    return name, person, week, team


# ────────────────────────────────────────────
# Claude 평가
# ────────────────────────────────────────────

_claude_client = None


def get_claude_client():
    global _claude_client
    if _claude_client is None:
        _claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _claude_client


def evaluate_with_gpt(title, full_text, visual_count, retries=6):
    """Claude Haiku로 보고서 평가. 성공 시 딕셔너리, 최종 실패 시 None 반환.
    None을 반환한 항목은 시트에 기록하지 않아 다음 실행에서 재평가된다."""
    visual_note = f"\n[시각자료 블록 수: {visual_count}개 (이미지/수식/동영상 블록 합산)]"
    user_content = f"**보고서 제목:** {title}\n\n**보고서 내용:**\n{full_text}{visual_note}"
    client = get_claude_client()

    for attempt in range(retries):
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                temperature=0.3,
                system=EVAL_SYSTEM_PROMPT,
                messages=[
                    {"role": "user",      "content": user_content},
                    {"role": "assistant", "content": "{"},  # JSON 출력 강제 prefill
                ],
            )
            text = "{" + response.content[0].text
            return json.loads(text)
        except anthropic.RateLimitError as e:
            wait = None
            try:
                wait = float(e.response.headers.get("retry-after", 0)) or None
            except Exception:
                pass
            if not wait:
                m = re.search(r'(?:try again in|retry after)\s*(\d+(?:\.\d+)?)\s*s', str(e), re.IGNORECASE)
                wait = float(m.group(1)) + 2.0 if m else 60.0
            print(f"   Rate limited, {wait:.0f}초 후 재시도... ({attempt+1}/{retries})")
            time.sleep(wait)
        except Exception as e:
            print(f"   ⚠️ 평가 오류: {e}")
            return None

    print(f"   ⚠️ 재시도 {retries}회 초과 → 다음 실행 시 재평가됩니다")
    return None


# ────────────────────────────────────────────
# 평가 캐시
# ────────────────────────────────────────────

def load_eval_cache():
    """eval_cache.json 로드. 없으면 빈 딕셔너리 반환."""
    cache_path = os.path.join(os.path.dirname(__file__), CACHE_FILE)
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_eval_cache(cache):
    """eval_cache.json 저장."""
    cache_path = os.path.join(os.path.dirname(__file__), CACHE_FILE)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ────────────────────────────────────────────
# Google Sheets 연동
# ────────────────────────────────────────────

GSPREAD_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


def get_worksheet():
    """서비스 계정으로 Google Sheets 워크시트 열기 (없으면 생성)"""
    if not GOOGLE_SHEET_ID:
        raise RuntimeError(
            "GOOGLE_SHEET_ID가 .env에 설정되지 않았습니다.\n"
            "설정 방법: .env 파일에 GOOGLE_SHEET_ID=<시트 ID> 추가"
        )
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=GSPREAD_SCOPES
    )
    gc = gspread.authorize(creds)
    try:
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
    except gspread.exceptions.APIError as e:
        msg = str(e)
        if "403" in msg and "sheets.googleapis.com" in msg:
            raise RuntimeError(
                "Google Sheets API가 비활성화 상태입니다.\n"
                "아래 URL에서 API를 활성화하세요 (Google Drive API도 함께):\n"
                "  https://console.developers.google.com/apis/api/sheets.googleapis.com/overview\n"
                "  https://console.developers.google.com/apis/api/drive.googleapis.com/overview\n"
                "활성화 후 수 분 기다렸다가 다시 실행하세요."
            ) from e
        raise
    try:
        ws = sh.worksheet("보고서 제출 현황")
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet("보고서 제출 현황", rows=200, cols=300)
        ws.update([["학회원"]])
    return ws


def week_sort_key(w):
    digits = "".join(c for c in w if c.isdigit())
    return int(digits) if digits else 0


def update_google_sheet(records_with_eval, weeks_set, members_set):
    """Google Sheet에 평가 결과를 업데이트 (기존 주차 데이터 보존)"""
    ws = get_worksheet()

    weeks   = sorted(weeks_set, key=week_sort_key)
    members = sorted(members_set)

    # 현재 시트 전체 로드
    all_values = ws.get_all_values()
    if not all_values or not all_values[0]:
        all_values = [["학회원"]]

    headers = list(all_values[0])

    # "팀" 열 확보 (학회원 바로 다음, 없으면 삽입)
    if "팀" not in headers:
        headers.insert(1, "팀")
        for i in range(1, len(all_values)):
            if all_values[i]:
                all_values[i] = [all_values[i][0], ""] + list(all_values[i][1:])

    header_idx = {h: i for i, h in enumerate(headers)}

    # 필요한 열 추가 (새 주차)
    for week in weeks:
        for sub in WEEK_SUBCOLS:
            col = f"{week}_{sub}"
            if col not in header_idx:
                header_idx[col] = len(headers)
                headers.append(col)

    # 멤버 → 행 인덱스 맵핑
    member_row = {}
    for i, row in enumerate(all_values[1:], 1):
        if row and row[0]:
            member_row[row[0]] = i

    # 새 멤버 행 추가
    for member in members:
        if member not in member_row:
            member_row[member] = len(all_values)
            all_values.append([member])

    # 모든 행 길이를 헤더 길이에 맞춤
    n_cols = len(headers)
    for i in range(len(all_values)):
        row = all_values[i]
        if len(row) < n_cols:
            all_values[i] = row + [""] * (n_cols - len(row))

    all_values[0] = headers

    # 평가 데이터 채우기
    for (member, week), data in records_with_eval.items():
        if member not in member_row:
            continue
        ri = member_row[member]
        ev = data.get("evaluation", {})

        wc = data.get("word_count", "")
        wc_str = f"⚠️ {wc}" if isinstance(wc, int) and wc < 700 else str(wc)

        updates = {
            f"{week}_단어수":    wc_str,
            f"{week}_이해도(5)": str(ev.get("이해도",   {}).get("score", "")),
            f"{week}_가독성(5)": str(ev.get("가독성",   {}).get("score", "")),
            f"{week}_시각자료(3)": str(ev.get("시각자료", {}).get("score", "")),
            f"{week}_토론(3)":   str(ev.get("토론",     {}).get("score", "")),
            f"{week}_총점":      str(ev.get("총점", "")),
            f"{week}_팀유형":    ev.get("team_type", ""),
            f"{week}_평가":      ev.get("종합평가", ""),
        }
        for col_name, value in updates.items():
            if col_name in header_idx:
                all_values[ri][header_idx[col_name]] = value

        # 팀이름 기록 (Notion에서 가져온 실제 팀 이름)
        team_name = data.get("team", "")
        if team_name and "팀" in header_idx:
            all_values[ri][header_idx["팀"]] = team_name

    # ── 팀이름 기준 정렬 ──
    def get_member_team(member):
        # 1. 이번 실행 records_with_eval에서 팀이름 찾기
        for (m, _), d in records_with_eval.items():
            if m == member and d.get("team"):
                return d["team"]
        # 2. 기존 시트 "팀" 열에서 찾기
        if member in member_row and "팀" in header_idx:
            row = all_values[member_row[member]]
            ti = header_idx["팀"]
            if ti < len(row) and row[ti]:
                return row[ti]
        return "기타"

    sorted_members = sorted(member_row.items(), key=lambda x: (get_member_team(x[0]), x[0]))
    final_values = [all_values[0]] + [all_values[ri] for _, ri in sorted_members]

    ws.clear()
    ws.update(final_values, value_input_option="USER_ENTERED")
    print(f"\n✅ Google Sheet 업데이트 완료!")
    print(f"   시트: https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}")


def update_summary_sheet(records_with_eval, page_data, weeks_set):
    """'주차별 요약' 시트에 상위 3명 / 글자수 부족 / 미제출자를 기록"""
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=GSPREAD_SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)

    try:
        ws = sh.worksheet("주차별 요약")
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet("주차별 요약", rows=50, cols=30)

    sorted_weeks = sorted(weeks_set, key=week_sort_key)

    # ── 헤더 행 ──
    header = ["구분"] + sorted_weeks
    rows = [header]

    # ── 상위 3명 ──
    for rank in range(3):
        row = [f"🏆 {rank+1}위"]
        for week in sorted_weeks:
            scores = []
            for (p, w), data in records_with_eval.items():
                if w != week:
                    continue
                try:
                    score = int(data.get("evaluation", {}).get("총점", 0))
                except (ValueError, TypeError):
                    score = 0
                ev = data.get("evaluation", {})
                s_이해도   = ev.get("이해도",   {}).get("score", "")
                s_가독성   = ev.get("가독성",   {}).get("score", "")
                s_시각자료 = ev.get("시각자료", {}).get("score", "")
                s_토론     = ev.get("토론",     {}).get("score", "")
                team = data.get("team", "")
                scores.append((p, score, team, s_이해도, s_가독성, s_시각자료, s_토론))
            scores.sort(key=lambda x: -x[1])
            if rank < len(scores):
                p, score, team, s1, s2, s3, s4 = scores[rank]
                prefix = f"{team}: " if team else ""
                row.append(f"{prefix}{p} [{s1}/{s2}/{s3}/{s4}]")
            else:
                row.append("")
        rows.append(row)

    rows.append([""] * len(header))  # 구분선

    # ── 글자수 부족 (FIXED_MEMBERS 대상만) ──
    row = ["⚠️ 글자수 부족"]
    for week in sorted_weeks:
        low = [
            (p, page_data[(p, w)]["word_count"])
            for (p, w) in page_data
            if w == week and p in FIXED_MEMBERS and page_data[(p, w)]["word_count"] < 700
        ]
        low_sorted = [f"{p} ({wc:,}단어)" for p, wc in sorted(low, key=lambda x: x[1])]
        row.append("\n".join(low_sorted) if low_sorted else "없음")
    rows.append(row)

    rows.append([""] * len(header))  # 구분선

    # ── 제출자 (FIXED_MEMBERS 대상만) ──
    row = ["✅ 제출"]
    for week in sorted_weeks:
        submitted = sorted(p for (p, w) in page_data if w == week and p in FIXED_MEMBERS)
        row.append("\n".join(submitted) if submitted else "없음")
    rows.append(row)

    rows.append([""] * len(header))  # 구분선

    # ── 미제출자 (FIXED_MEMBERS 대상만) ──
    row = ["❌ 미제출"]
    for week in sorted_weeks:
        submitted = {p for (p, w) in page_data if w == week}
        not_submitted = sorted(FIXED_MEMBERS - submitted)
        row.append("\n".join(not_submitted) if not_submitted else "전원 제출")
    rows.append(row)

    ws.clear()
    ws.update(rows, value_input_option="USER_ENTERED")
    print(f"   요약 시트: '주차별 요약' 탭 업데이트 완료")


# ────────────────────────────────────────────
# 주차별 요약 리포트
# ────────────────────────────────────────────

def print_summary(records_with_eval, page_data, weeks):
    """주차별 요약: 점수 상위 3명 / 글자수 부족 / 미제출자"""
    sorted_weeks = sorted(weeks, key=week_sort_key)

    print("\n" + "=" * 60)
    print("📋 주차별 요약 리포트")
    print("=" * 60)

    for week in sorted_weeks:
        print(f"\n🗓️  {week}")

        # 해당 주차 실제 제출자 (Notion 데이터 기준)
        submitted = {p for (p, w) in page_data if w == week}

        # 1. 점수 상위 3명 (평가 성공한 경우만)
        scores = []
        for (p, w), data in records_with_eval.items():
            if w != week:
                continue
            try:
                score = int(data.get("evaluation", {}).get("총점", 0))
            except (ValueError, TypeError):
                score = 0
            scores.append((p, score))
        scores.sort(key=lambda x: -x[1])

        print(f"  🏆 점수 상위 3명:")
        for i, (name, score) in enumerate(scores[:3], 1):
            print(f"     {i}. {name}: {score}점")

        # 2. 글자수 부족 (700단어 미만, FIXED_MEMBERS 대상만)
        low = [
            (p, page_data[(p, w)]["word_count"])
            for (p, w) in page_data
            if w == week and p in FIXED_MEMBERS and page_data[(p, w)]["word_count"] < 700
        ]
        if low:
            low.sort(key=lambda x: x[1])
            print(f"  ⚠️  글자수 부족 ({len(low)}명, 시트에 ⚠️ 표시):")
            for name, wc in low:
                print(f"     - {name}: {wc:,}단어")

        # 3. 제출자 (FIXED_MEMBERS 대상만)
        submitted_fixed = sorted(submitted & FIXED_MEMBERS)
        print(f"  ✅ 제출 ({len(submitted_fixed)}명): {', '.join(submitted_fixed)}")

        # 4. 미제출자 (FIXED_MEMBERS 대상만)
        not_submitted = FIXED_MEMBERS - submitted
        if not_submitted:
            print(f"  ❌ 미제출 ({len(not_submitted)}명): {', '.join(sorted(not_submitted))}")
        else:
            print(f"  ✅ 전원 제출 완료")

    print("\n" + "=" * 60)


# ────────────────────────────────────────────
# 메인
# ────────────────────────────────────────────

def main(weeks_filter=None):
    """
    weeks_filter: 처리할 주차 집합 (예: {"4주차", "5주차"}). None이면 전체 처리.
    """
    if weeks_filter:
        print(f"📥 노션 데이터베이스 불러오는 중... (대상 주차: {', '.join(sorted(weeks_filter, key=week_sort_key))})")
    else:
        print("📥 노션 데이터베이스 불러오는 중...")
    pages = get_all_pages()
    print(f"   → {len(pages)}개 페이지 발견\n")

    valid, skipped = [], 0
    for page in pages:
        name, person, week, team = extract_info(page)
        if not person or not week:
            skipped += 1
        elif weeks_filter and week not in weeks_filter:
            pass  # 대상 주차 아님 → 조용히 건너뜀
        else:
            valid.append((page["id"], person, week, name, team))

    # ── Step 1: Notion 페이지 내용 병렬 수집 ──
    print(f"📄 페이지 내용 수집 중 ({len(valid)}개)...")

    def fetch(item):
        page_id, person, week, title, team = item
        wc, text, visual_count = get_page_data(page_id, title=title)
        return person, week, title, wc, text, visual_count, team

    page_data       = {}
    weeks, members  = set(), set()
    done            = 0
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(fetch, item): item for item in valid}
        for future in as_completed(futures):
            person, week, title, wc, text, visual_count, team = future.result()
            page_data[(person, week)] = {
                "title": title, "word_count": wc,
                "full_text": text, "visual_count": visual_count,
                "team": team,
            }
            weeks.add(week)
            members.add(person)
            done += 1
            print(f"[{done:3}/{len(valid)}] {person} | {week} → {wc:,}단어")

    if skipped:
        print(f"\n⚠️  Person 또는 주차 정보 없는 페이지 {skipped}개 건너뜀")

    # ── Step 2: GPT-4o 평가 (순차 처리 — TPM 한도 30,000 초과 방지) ──
    print(f"\n🤖 GPT-4o 평가 중 ({len(page_data)}개 보고서)...")

    records_with_eval = {}
    failed_eval       = []
    done              = 0
    keys        = sorted(page_data.keys())
    eval_cache  = load_eval_cache()
    api_calls   = 0  # 실제 Claude 호출 횟수 (캐시 히트는 제외)

    cached_count = sum(1 for p, w in keys if f"{p}|{w}" in eval_cache)
    if cached_count:
        print(f"   → 캐시 히트 {cached_count}건 스킵, {len(keys) - cached_count}건 새로 평가")

    for key in keys:
        person, week = key
        cache_key    = f"{person}|{week}"
        done        += 1

        if cache_key in eval_cache:
            cached = eval_cache[cache_key]
            ev     = cached["evaluation"]
            team   = cached.get("team", page_data[key].get("team", ""))
            records_with_eval[key] = {
                "word_count": page_data[key]["word_count"],
                "team": team,
                "evaluation": ev,
            }
            print(f"[{done:3}/{len(page_data)}] {person} | {week} → {ev.get('총점', '?')}점 [캐시]")
            continue

        # 실제 Claude 호출
        if api_calls > 0:
            time.sleep(3)  # 과도한 동시 요청 방지
        api_calls += 1
        d  = page_data[key]
        ev = evaluate_with_gpt(d["title"], d["full_text"], d["visual_count"])

        if ev is None:
            failed_eval.append(f"{person} | {week}")
            print(f"[{done:3}/{len(page_data)}] {person} | {week} → ⚠️ 평가 실패 (다음 실행 시 재평가)")
        else:
            gpt_team = ev.get("team_name", "").strip()
            team     = gpt_team if gpt_team else page_data[key].get("team", "")
            entry    = {"word_count": page_data[key]["word_count"], "team": team, "evaluation": ev}
            records_with_eval[key] = entry
            # 캐시에 저장 (성공한 것만)
            eval_cache[cache_key] = {"team": team, "evaluation": ev}
            save_eval_cache(eval_cache)
            print(
                f"[{done:3}/{len(page_data)}] {person} | {week}"
                f" → {ev.get('총점', '?')}점 [{ev.get('team_type', '?')}]"
            )

    if failed_eval:
        print(f"\n⚠️ GPT 평가 실패 {len(failed_eval)}건 (시트 기록 없음 → 다음 실행에서 재평가):")
        for f in failed_eval:
            print(f"   - {f}")

    # ── Step 3: Google Sheet 업데이트 ──
    print(f"\n📊 Google Sheet 업데이트 중 ({len(members)}명 × {len(weeks)}주차)...")
    update_google_sheet(records_with_eval, weeks, members)

    # ── Step 4: 요약 시트 업데이트 + 콘솔 출력 ──
    update_summary_sheet(records_with_eval, page_data, weeks)
    print_summary(records_with_eval, page_data, weeks)


# ────────────────────────────────────────────
# 디버그 (특정 페이지 단어수 확인)
# ────────────────────────────────────────────

def debug_page(page_id, title=""):
    """python3 notion_report_checker.py <page_id> 로 실행"""
    print(f"\n[제목] '{title}' → {count_words(title)}단어")
    total = count_words(title)

    def _collect(pid, depth=0):
        nonlocal total
        u = f"https://api.notion.com/v1/blocks/{pid}/children?page_size=100"
        while u:
            r    = safe_get(u)
            data = r.json()
            for block in data.get("results", []):
                btype   = block.get("type", "")
                content = block.get(btype, {})
                if btype == "table_row":
                    for cell in content.get("cells", []):
                        for rt in cell:
                            txt = rt.get("plain_text", "")
                            if txt.strip():
                                w = count_words(txt)
                                print(f"{'  '*depth}[{btype}] '{txt}' → {w}단어")
                                total += w
                else:
                    for rt in content.get("rich_text", []):
                        txt = rt.get("plain_text", "")
                        if txt.strip():
                            w = count_words(txt)
                            print(f"{'  '*depth}[{btype}] '{txt[:40]}' → {w}단어")
                            total += w
                if block.get("has_children"):
                    _collect(block["id"], depth + 1)
            u = (
                f"https://api.notion.com/v1/blocks/{pid}/children"
                f"?page_size=100&start_cursor={data['next_cursor']}"
                if data.get("has_more") else None
            )

    _collect(page_id)
    print(f"\n총합: {total}단어")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="YAI 보고서 제출 현황 체크")
    parser.add_argument(
        "--weeks", nargs="+", metavar="주차",
        help="처리할 주차 (예: --weeks 4주차 5주차). 생략하면 전체 처리."
    )
    parser.add_argument(
        "--debug", metavar="PAGE_ID",
        help="특정 페이지 단어수 디버그"
    )
    args = parser.parse_args()

    if args.debug:
        debug_page(args.debug, title="(디버그)")
    else:
        main(weeks_filter=set(args.weeks) if args.weeks else None)
