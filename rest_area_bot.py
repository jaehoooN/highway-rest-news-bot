"""
고속도로 휴게소 뉴스 → 텔레그램 채널 자동 전송 봇
====================================================

n8n 대안: GitHub Actions 또는 cron으로 7-10분마다 실행
필요 환경변수:
    NAVER_CLIENT_ID
    NAVER_CLIENT_SECRET
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID        # @channel_username 또는 -100으로 시작하는 숫자 ID

중복 제거 저장소: 작업 디렉터리의 sent_links.json 파일
    GitHub Actions 사용 시: actions/cache 또는 Git commit으로 영속화
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests

# ───────────────────────────── 설정 ─────────────────────────────

NAVER_API = "https://openapi.naver.com/v1/search/news.json"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

SEARCH_QUERIES: list[str] = [
    "고속도로 휴게소",
    "도로공사 휴게소",
    "EX 휴게소",
    "휴게소 리뉴얼",
]

POSITIVE_KEYWORDS = ("휴게소",)
CONTEXT_KEYWORDS = (
    "고속도로", "도로공사", "EX", "한국도로공사",
    "상행", "하행", "나들목", "IC", "JC", "톨게이트",
)
NEGATIVE_KEYWORDS = ("버스터미널", "휴게시간", "국회 휴게")

HISTORY_FILE = Path(os.getenv("HISTORY_FILE", "sent_links.json"))
MAX_HISTORY = 2000
REQUEST_TIMEOUT = 10
SEND_DELAY_SEC = 1.0  # Telegram rate limit 여유

# ────────────────────────── 유틸 함수 ──────────────────────────

HTML_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str) -> str:
    return html.unescape(HTML_TAG_RE.sub("", s or ""))


def is_relevant(text: str) -> bool:
    if not any(k in text for k in POSITIVE_KEYWORDS):
        return False
    if any(k in text for k in NEGATIVE_KEYWORDS):
        return False
    if not any(k in text for k in CONTEXT_KEYWORDS):
        return False
    return True


def extract_publisher(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        return host.removeprefix("www.").split(".")[0]
    except Exception:
        return ""


def load_history() -> list[str]:
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_history(links: list[str]) -> None:
    HISTORY_FILE.write_text(
        json.dumps(links[-MAX_HISTORY:], ensure_ascii=False),
        encoding="utf-8",
    )


def require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        print(f"[FATAL] 환경변수 {name} 가 설정되어 있지 않습니다.", file=sys.stderr)
        sys.exit(1)
    return v


# ───────────────────────── 핵심 로직 ─────────────────────────


def fetch_news(query: str, client_id: str, client_secret: str) -> list[dict]:
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }
    params = {"query": query, "display": 50, "sort": "date"}
    try:
        r = requests.get(
            NAVER_API, headers=headers, params=params, timeout=REQUEST_TIMEOUT
        )
        r.raise_for_status()
        return r.json().get("items", [])
    except Exception as e:
        print(f"[WARN] '{query}' 조회 실패: {e}", file=sys.stderr)
        return []


def build_message(article: dict) -> str:
    title = html.escape(strip_html(article["title"]))
    description = html.escape(strip_html(article.get("description", "")))[:220]
    link = html.escape(article.get("originallink") or article.get("link", ""))
    publisher = html.escape(extract_publisher(article.get("originallink") or article.get("link", "")))
    date = html.escape(article.get("pubDate", ""))

    ellipsis = "…" if len(description) >= 220 else ""

    parts = [f"📰 <b>{title}</b>"]
    if publisher:
        parts.append(f"🏢 {publisher}")
    if date:
        parts.append(f"🕐 {date}")
    parts.append("")
    parts.append(f"{description}{ellipsis}")
    parts.append("")
    parts.append(f'🔗 <a href="{link}">원문 보기</a>')
    return "\n".join(parts)


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    url = TELEGRAM_API.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code == 429:
            retry_after = r.json().get("parameters", {}).get("retry_after", 5)
            print(f"[WARN] Telegram rate-limited. {retry_after}s 대기")
            time.sleep(retry_after + 1)
            r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[ERROR] Telegram 전송 실패: {e}", file=sys.stderr)
        return False


# ───────────────────────────── main ─────────────────────────────


def main() -> None:
    client_id = require_env("NAVER_CLIENT_ID")
    client_secret = require_env("NAVER_CLIENT_SECRET")
    bot_token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")

    history = load_history()
    history_set = set(history)

    # 1) 수집
    candidates: list[dict] = []
    seen_keys: set[str] = set()
    for q in SEARCH_QUERIES:
        for a in fetch_news(q, client_id, client_secret):
            key = a.get("originallink") or a.get("link")
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            candidates.append(a)

    # 2) 필터 + 중복 제거
    fresh: list[dict] = []
    for a in candidates:
        text = strip_html(a.get("title", "")) + " " + strip_html(a.get("description", ""))
        if not is_relevant(text):
            continue
        key = a.get("originallink") or a.get("link")
        if key in history_set:
            continue
        fresh.append(a)
        history_set.add(key)
        history.append(key)

    print(f"[INFO] 전체 후보 {len(candidates)}건 중 전송 대상 {len(fresh)}건")

    # 3) 전송
    sent = 0
    for a in fresh:
        msg = build_message(a)
        if send_telegram(bot_token, chat_id, msg):
            sent += 1
        time.sleep(SEND_DELAY_SEC)

    # 4) 이력 저장
    save_history(history)
    print(f"[INFO] {sent}건 전송 완료, 이력 {len(history)}건 저장")


if __name__ == "__main__":
    main()
