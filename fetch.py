"""Gmail 읽기 전용 수집기.

계정별 토큰으로 최근 N일(기본 3일)의 메일 메타데이터 + 본문 앞 1KB만 가져옵니다.

하드룰:
  - 호출하는 API는 users().messages().list / .get 뿐입니다. (읽기 전용)
  - modify / send / trash / batchModify 는 이 파일에 존재하지 않습니다.
  - 본문 전문은 메모리에서도 1KB로 잘라내고, 디스크에 저장하지 않습니다.
"""

import argparse
import base64
import json
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import auth
import config

_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

# Gmail이 붙여주는 카테고리 라벨
GMAIL_CATEGORY_LABELS = {
    "CATEGORY_PROMOTIONS": "promotions",
    "CATEGORY_SOCIAL": "social",
    "CATEGORY_UPDATES": "updates",
    "CATEGORY_FORUMS": "forums",
    "CATEGORY_PERSONAL": "personal",
}


def build_service(account: dict):
    creds = auth.load_credentials(account)
    if creds is None:
        raise SystemExit(
            f"{account['email']} 토큰이 없습니다. 먼저 실행하세요:\n"
            f"  ./venv/bin/python auth.py --account {account['key']}"
        )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _header_map(payload: dict) -> dict:
    """헤더를 소문자 키의 dict로. 같은 이름이 여러 번 오면 첫 값을 씁니다."""
    headers = {}
    for header in payload.get("headers", []):
        name = header.get("name", "").lower()
        if name not in headers:
            headers[name] = header.get("value", "")
    return headers


def _decode(data: str) -> str:
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data.encode("ascii") + b"==")
    return raw.decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> str:
    """text/plain 우선, 없으면 text/html에서 태그를 벗겨 사용."""
    plain, html = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        if mime == "text/plain" and body.get("data"):
            plain.append(_decode(body["data"]))
        elif mime == "text/html" and body.get("data"):
            html.append(_decode(body["data"]))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)

    text = "\n".join(plain) if plain else _HTML_TAG.sub(" ", "\n".join(html))
    return _WS.sub(" ", text).strip()


def _parse_date(value: str, internal_date_ms: str | None) -> datetime:
    if value:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except (TypeError, ValueError):
            pass
    if internal_date_ms:
        return datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=timezone.utc)
    return datetime.now(timezone.utc)


def _extract_email(value: str) -> str:
    match = re.search(r"[\w.+-]+@[\w.-]+", value or "")
    return match.group(0).lower() if match else ""



# ── 쿼터 초과 / 일시 장애 재시도 ──────────────────────────────────────
# Gmail API 는 분당 쿼터를 넘기면 403(rateLimitExceeded) 또는 429 를 줍니다.
# 이 수집기는 한 번에 수백 건을 format="full"(건당 5 쿼터유닛)로 긁으므로,
# 창을 넓히는 순간 정확히 여기서 걸립니다. 실측(2026-09-13): 기계가 9일간
# 꺼져 있다 돌아와 --days 12 로 백필하다 첫 계정에서 403 이 떨어졌고,
# 파이프라인이 예외를 던지고 죽었습니다. 계정을 나누고 150건으로 줄여도
# 같았습니다.
#
# 재시도가 없으면 **기계가 하루 이상 꺼졌다 돌아왔을 때의 복구 경로가
# 막힙니다.** README §5.5 는 바로 그 상황을 위해 창을 3일로 넓혀 뒀는데,
# 정작 창을 더 넓혀야 하는 순간에 수집이 죽으면 그 마진은 무의미합니다.
#
# 쿼터는 분 단위로 리셋되므로 누적 대기가 1분을 넘도록 잡습니다.
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
RETRYABLE_REASONS = {
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "quotaExceeded",
    "backendError",
    "internalError",
}
MAX_RETRIES = 7            # 1+2+4+…+64 ≈ 최대 127초
BASE_BACKOFF_SEC = 1.0

# 건별 get 사이 간격. 쿼터에 닿기 전에 속도를 낮추는 쪽이, 닿은 뒤 백오프로
# 1분씩 기다리는 것보다 총 시간이 짧습니다.
#
# 값의 근거 (Gmail API 사용량 한도 문서):
#   · per-user 한도 = 분당 6,000 쿼터유닛
#   · messages.get = 건당 20유닛  (messages.list 는 5유닛)
#   → 분당 상한이 300건. 0.21초 간격이면 약 285건/분으로 그 아래에 머뭅니다.
#
# 처음에 0.05초로 잡았던 것은 get 을 5유닛으로 잘못 알고 계산한 값입니다.
# 실제로는 분당 1,200건 속도라 상한의 4배였고, 백오프가 있어 죽지는 않지만
# 매 실행이 한도를 치고 기다리는 모양이 됩니다.
GET_PACING_SEC = 0.21


def _error_reason(error: HttpError) -> str:
    """Google 이 준 reason 문자열. 못 읽으면 빈 문자열."""
    try:
        payload = json.loads(error.content.decode("utf-8"))
    except Exception:
        return ""
    errors = payload.get("error", {}).get("errors") or []
    return errors[0].get("reason", "") if errors else ""


def _execute(request, what: str):
    """재시도 가능한 오류면 지수 백오프로 다시 걸고, 아니면 그대로 올립니다.

    403 에는 두 가지가 섞여 있습니다 — 쿼터 초과(기다리면 풀림)와 권한 없음
    (기다려도 영원히 안 풀림). reason 으로 가릅니다. 권한 문제를 재시도로
    덮으면 scope 가 잘못된 상태를 2분 동안 조용히 기다리다 같은 자리에서
    죽습니다. 조용한 실패를 하나 더 만드는 셈입니다.

    재시도는 stdout 에 한 줄씩 남깁니다. cron 로그에 남아야 "그냥 느린 실행"과
    "쿼터에 계속 걸리는 실행"이 사후에 구분됩니다.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            return request.execute()
        except HttpError as error:
            status = getattr(error.resp, "status", None)
            reason = _error_reason(error)
            retryable = status in RETRYABLE_STATUSES or (
                status == 403 and reason in RETRYABLE_REASONS
            )
            if not retryable or attempt == MAX_RETRIES:
                raise
            wait = BASE_BACKOFF_SEC * (2 ** attempt) + random.uniform(0, 0.5)
            print(
                f"  [RETRY {attempt + 1}/{MAX_RETRIES}] {what} — "
                f"{status} {reason} · {wait:.1f}s 대기"
            )
            time.sleep(wait)


def fetch_messages(account: dict, days: int = 3, max_messages: int = 500) -> list[dict]:
    """최근 `days`일 메일을 정제된 dict 목록으로 반환합니다."""
    service = build_service(account)
    query = f"newer_than:{days}d"

    ids: list[str] = []
    page_token = None
    while len(ids) < max_messages:
        response = _execute(
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=min(100, max_messages - len(ids)),
                pageToken=page_token,
                includeSpamTrash=False,
            ),
            f"list {account['key']} ({len(ids)}건까지)",
        )
        ids.extend(m["id"] for m in response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    messages = []
    for index, message_id in enumerate(ids):
        raw = _execute(
            service.users().messages().get(userId="me", id=message_id, format="full"),
            f"get {account['key']} {index + 1}/{len(ids)}",
        )
        messages.append(_normalize(account, raw))
        if GET_PACING_SEC:
            time.sleep(GET_PACING_SEC)

    return messages


def _normalize(account: dict, raw: dict) -> dict:
    payload = raw.get("payload", {})
    headers = _header_map(payload)
    body = _extract_body(payload)

    # 본문은 앞 1KB만 유지. 원문은 여기서 버려집니다.
    snippet = body.encode("utf-8")[: config.BODY_SNIPPET_BYTES].decode(
        "utf-8", errors="ignore"
    )

    label_ids = raw.get("labelIds", []) or []
    categories = [
        GMAIL_CATEGORY_LABELS[label]
        for label in label_ids
        if label in GMAIL_CATEGORY_LABELS
    ]

    sender = headers.get("from", "")
    sender_email = _extract_email(sender)
    date = _parse_date(headers.get("date", ""), raw.get("internalDate"))

    return {
        "account_key": account["key"],
        "account_email": account["email"],
        "message_id": raw.get("id", ""),
        "thread_id": raw.get("threadId", ""),
        "subject": headers.get("subject", "(제목 없음)"),
        "from": sender,
        "from_email": sender_email,
        "from_domain": sender_email.split("@")[-1] if "@" in sender_email else "",
        "to": headers.get("to", ""),
        "date": date.isoformat(),
        "age_days": max(0, (datetime.now(timezone.utc) - date).days),
        "labels": label_ids,
        "gmail_categories": categories,
        "is_unread": "UNREAD" in label_ids,
        "has_list_unsubscribe": "list-unsubscribe" in headers,
        "body_snippet": snippet,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail 수집 단독 실행 (디버그용)")
    parser.add_argument(
        "--account", required=True, choices=[a["key"] for a in config.ACCOUNTS]
    )
    parser.add_argument("--days", type=int, default=3)
    args = parser.parse_args()

    account = config.get_account(args.account)
    messages = fetch_messages(account, days=args.days)
    print(f"{account['email']}: 최근 {args.days}일 메일 {len(messages)}건")
    for message in messages[:10]:
        print(f"  [{message['date'][:10]}] {message['from_email']} — {message['subject'][:60]}")


if __name__ == "__main__":
    main()
