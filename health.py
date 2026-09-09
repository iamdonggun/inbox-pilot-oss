"""토큰 health check — 실행 시작 시 계정별 인증 상태를 점검합니다.

cron 은 사람이 stdout 을 보지 않습니다. 토큰이 죽어도 파이프라인은 계속 돌고
다이제스트는 "수집된 메일 없음"으로 조용히 완성되므로, **메일이 없었던 것**과
**메일을 읽지 못한 것**이 구분되지 않습니다. 이 프로젝트는 그런 조용한 실패로
이미 다섯 번 당했습니다(쿼리 문법 / config drift / URL 매칭 / fallback 토큰 /
launchd StartInterval 스톨). 그래서 이 모듈은 상태를 **네 곳에 동시에** 남깁니다.

  1. stdout 의 [AUTH FAILED] 마커 + 재승인 명령어  (사람이 로그를 볼 때)
  2. 다이제스트 최상단 경고 블록                    (결과물만 볼 때)
  3. out/health.json                                (기계가 읽을 때)
  4. 0 이 아닌 종료 코드                            (cron 래퍼가 ERROR 를 남기게)

정상일 때도 health.json 을 갱신하고 last_ok_at 을 남깁니다. 마지막 성공 시각이
없으면 "지금 죽어 있다"는 알아도 "언제부터 죽어 있었나"는 알 수 없습니다.

한 계정의 실패는 그 계정만 건너뜁니다. 두 계정은 서로 독립적인 토큰을 쓰므로,
한쪽이 죽었다고 다른 쪽 메일까지 분류되지 않을 이유가 없습니다.

하드룰: 토큰·크리덴셜 값은 stdout·health.json 어디에도 남기지 않습니다.
남는 것은 예외 타입과 사유 문자열뿐이고, 그마저 _redact 를 거칩니다.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

import auth
import config

HEALTH_FILE = config.OUT_DIR / "health.json"

STATUS_OK = "ok"
STATUS_AUTH_FAILED = "auth_failed"   # 재승인 필요 (invalid_grant, 손상된 토큰 파일)
STATUS_NO_TOKEN = "no_token"         # 최초 승인이 아직 안 된 계정
STATUS_UNAVAILABLE = "unavailable"   # 네트워크 등 일시 장애 — 재승인 불필요

# 사람이 손대야 하는 상태. cron 로그에서 이 마커를 grep 합니다.
_MARKER = {
    STATUS_AUTH_FAILED: "[AUTH FAILED]",
    STATUS_NO_TOKEN: "[AUTH FAILED]",
    STATUS_UNAVAILABLE: "[CHECK FAILED]",
}

# 만에 하나 예외 문자열에 크리덴셜이 섞여 들어와도 파일·로그로 새지 않게 합니다.
# (refresh 실패 시 google-auth 가 담는 것은 서버 '응답'이라 보통 토큰이 없지만,
#  이 저장소는 public 이고 토큰 유출 = 메일함 유출이라 가정을 믿지 않습니다.)
_SECRET = re.compile(r"1//[\w-]{8,}|ya29\.[\w.-]{8,}|GOCSPX-[\w-]{8,}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(exc: Exception, limit: int = 300) -> str:
    text = f"{type(exc).__name__}: {exc}"
    text = _SECRET.sub("<redacted>", text)
    text = " ".join(text.split())
    return text[: limit - 1] + "…" if len(text) > limit else text


def reauth_command(account: dict) -> str:
    """이 계정을 다시 승인하는 명령어. cron 로그에 그대로 붙여넣을 수 있게 절대경로."""
    return (
        f"cd {config.BASE_DIR} && "
        f"./venv/bin/python auth.py --account {account['key']} --force"
    )


def check(account: dict) -> dict:
    """계정 하나의 토큰을 점검합니다. 필요하면 갱신하고 저장합니다.

    갱신이 성공하면 토큰 파일도 새 것으로 저장합니다 — 점검이 곧 갱신이므로,
    뒤따르는 fetch 는 이미 유효한 토큰을 파일에서 그대로 읽습니다.
    """
    record = {
        "account": account["email"],
        "alias": account["alias"],
        "key": account["key"],
        "status": STATUS_OK,
        "checked_at": _now(),
        "error": None,
    }
    path = config.token_path(account)

    if not path.exists():
        record["status"] = STATUS_NO_TOKEN
        record["error"] = f"토큰 파일이 없습니다: {account['token_file']}"
        return record

    try:
        creds = Credentials.from_authorized_user_file(str(path), config.SCOPES)
    except (ValueError, json.JSONDecodeError, KeyError) as exc:
        # 파일이 깨졌거나 스키마가 아닙니다. 재승인 외에는 복구 방법이 없습니다.
        record["status"] = STATUS_AUTH_FAILED
        record["error"] = f"토큰 파일을 읽을 수 없습니다 — {_redact(exc)}"
        return record

    if creds.valid:
        return record

    if not creds.refresh_token:
        record["status"] = STATUS_AUTH_FAILED
        record["error"] = "refresh_token 이 없어 자동 갱신할 수 없습니다."
        return record

    try:
        creds.refresh(Request())
    except RefreshError as exc:
        # invalid_grant: 사용자가 접근을 철회했거나, 비밀번호를 바꿨거나,
        # 토큰이 6개월 이상 미사용으로 만료됐습니다. 전부 재승인이 답입니다.
        record["status"] = STATUS_AUTH_FAILED
        record["error"] = _redact(exc)
        return record
    except TransportError as exc:
        # 네트워크가 끊겼을 뿐입니다. 재승인을 시키면 안 됩니다 — 멀쩡한 토큰을
        # 사람이 손으로 갈아엎게 만드는 오진이 됩니다.
        record["status"] = STATUS_UNAVAILABLE
        record["error"] = _redact(exc)
        return record
    except Exception as exc:  # 알 수 없는 실패도 조용히 넘기지 않습니다
        record["status"] = STATUS_UNAVAILABLE
        record["error"] = _redact(exc)
        return record

    auth.save_credentials(account, creds)
    return record


def check_all(accounts: list[dict]) -> list[dict]:
    return [check(account) for account in accounts]


def _load_previous() -> dict[str, dict]:
    """이전 health.json 을 {계정 이메일: 레코드} 로 읽습니다. 없거나 깨졌으면 빈 dict."""
    if not HEALTH_FILE.exists():
        return {}
    try:
        data = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    entries = data.get("accounts", []) if isinstance(data, dict) else []
    return {e["account"]: e for e in entries if isinstance(e, dict) and "account" in e}


def write(records: list[dict], path: Path | None = None) -> list[dict]:
    """health.json 을 갱신하고, last_ok_at 이 채워진 이번 실행분 레코드를 반환합니다.

    - 정상일 때도 씁니다. last_ok_at 이 갱신돼야 "언제부터 죽었나"를 계산할 수
      있습니다. 실패한 계정의 last_ok_at 은 직전 값을 그대로 물려받습니다.
    - --account 로 한 계정만 돌린 실행이 다른 계정의 기록을 지우지 않도록,
      이번에 점검하지 않은 계정의 항목은 파일에 그대로 남겨 둡니다.
    """
    path = path or HEALTH_FILE
    previous = _load_previous()

    merged: dict[str, dict] = dict(previous)
    current: list[dict] = []
    for record in records:
        entry = dict(record)
        prior = previous.get(record["account"], {})
        if record["status"] == STATUS_OK:
            entry["last_ok_at"] = record["checked_at"]
        else:
            entry["last_ok_at"] = prior.get("last_ok_at")
            entry["reauth"] = reauth_command(record)
        merged[record["account"]] = entry
        current.append(entry)

    payload = {
        "updated_at": _now(),
        "ok": all(r.get("status") == STATUS_OK for r in merged.values()),
        "accounts": list(merged.values()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return current


def failures(records: list[dict]) -> list[dict]:
    return [r for r in records if r["status"] != STATUS_OK]


def marker(record: dict) -> str:
    """cron 로그에서 grep 할 마커. 정상 계정에는 쓰지 않습니다."""
    return _MARKER.get(record["status"], "[CHECK FAILED]")


def report(records: list[dict], previous: dict[str, dict] | None = None) -> None:
    """점검 결과를 stdout 에 출력합니다. 실패한 계정은 마커와 재승인 명령까지."""
    previous = _load_previous() if previous is None else previous

    print("\n■ 토큰 health check")
    for record in records:
        email = record["account"]
        if record["status"] == STATUS_OK:
            print(f"  [OK] {email} ({record['alias']})")
            continue

        marker = _MARKER[record["status"]]
        print(f"  {marker} {email} ({record['alias']}) — {record['error']}")
        last_ok = previous.get(email, {}).get("last_ok_at")
        print(f"      마지막 정상: {last_ok or '기록 없음'}")
        if record["status"] == STATUS_UNAVAILABLE:
            print("      일시 장애로 보입니다. 재승인 전에 네트워크를 먼저 확인하세요.")
        else:
            print(f"      재승인: {reauth_command(record)}")
        print("      이 계정은 이번 실행에서 건너뜁니다. 다른 계정은 계속 처리합니다.")
