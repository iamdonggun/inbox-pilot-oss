"""Gmail 읽기 전용 수집기.

계정별 토큰으로 최근 N일(기본 3일)의 메일 메타데이터 + 본문 앞 1KB만 가져옵니다.

하드룰:
  - 호출하는 API는 users().messages().list / .get 뿐입니다. (읽기 전용)
  - modify / send / trash / batchModify 는 이 파일에 존재하지 않습니다.
  - 본문 전문은 메모리에서도 1KB로 잘라내고, 디스크에 저장하지 않습니다.
"""

import argparse
import base64
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from googleapiclient.discovery import build

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


def fetch_messages(account: dict, days: int = 3, max_messages: int = 500) -> list[dict]:
    """최근 `days`일 메일을 정제된 dict 목록으로 반환합니다."""
    service = build_service(account)
    query = f"newer_than:{days}d"

    ids: list[str] = []
    page_token = None
    while len(ids) < max_messages:
        response = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=min(100, max_messages - len(ids)),
                pageToken=page_token,
                includeSpamTrash=False,
            )
            .execute()
        )
        ids.extend(m["id"] for m in response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    messages = []
    for message_id in ids:
        raw = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        messages.append(_normalize(account, raw))

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
