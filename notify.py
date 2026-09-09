"""텔레그램 알림 — 기계 밖으로 나가는 마지막 경보 경로.

`ALERT.md` 는 저장소를 열어야 보입니다. 2026-08-06~08-15 의 9일 침묵은
신호가 없어서가 아니라 신호가 파일 안에만 있어서 생겼습니다. 이 모듈은
그 신호를 폰까지 밀어 주는 한 칸입니다.

설계 원칙 세 가지:

1. **상태 전이에서만 보냅니다.** 실패 1회차와 복구 시점, 두 순간뿐입니다.
   30분마다 같은 실패 알림이 오면 사람은 알림을 끕니다. 그러면 9일 침묵과
   결과가 같아집니다 — 방향만 반대인 같은 실패입니다. 전이 판정은 호출자
   (`scripts/run-cron.sh`)가 연속 실패 카운터로 하고, 여기서는 하지 않습니다.

2. **알림이 알림 대상을 망가뜨리지 않습니다.** 전송 실패는 항상 exit 1 과
   로그 한 줄로 끝납니다. 예외를 밖으로 던지지 않고, 파이프라인은 계속
   진행합니다.

3. **메시지에 비밀·메일 내용을 넣지 않습니다.** 토큰과 chat_id 는 stdout·
   로그에 절대 나가지 않고(`_scrub`), 계정은 이메일 주소가 아니라 키
   (`main`/`dev`)로만 적습니다. 발신자·제목·본문은 애초에 읽지 않습니다.
   알림은 텔레그램 서버를 거쳐 나가는 외부 경로입니다 — 메일함 주소를
   실어 보낼 이유가 없습니다.
"""

# venv 가 사라진 고장에서는 /usr/bin/python3(현재 3.9)로 실행됩니다. 그쪽에는
# PEP 604 (`X | None`) 가 없으므로 애노테이션을 문자열로 미룹니다. 이 한 줄이
# 빠지면 "venv 가 없다"는 알림이 venv 가 없다는 이유로 죽습니다.
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
HEALTH_FILE = BASE_DIR / "out" / "health.json"
ENV_FILE = BASE_DIR / ".env"

# cron 은 30분 주기입니다. 텔레그램이 응답하지 않을 때 다음 실행까지 물고
# 있으면 안 되므로 짧게 끊습니다.
TIMEOUT_SEC = 10

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

REAUTH_CMD = "cd ~/workspace/inbox-pilot && ./venv/bin/python auth.py --account {key} --force"


# ── 환경 ──────────────────────────────────────────────────────────────
def _load_env() -> None:
    """`.env` → os.environ.

    정상 경로는 `config.load_env_file` 하나입니다(운영 불변식: 환경은 래퍼
    스크립트가 export 하는 게 아니라 프로그램이 직접 읽는다).

    fallback 이 붙어 있는 이유: 이 모듈은 *파이프라인이 깨졌을 때* 부르는
    코드입니다. `config` 임포트 자체가 깨진 종류의 고장(예: 망가진
    vip_local.py)에서 알림까지 같이 죽으면, 정확히 알려야 할 순간에
    침묵합니다. 경보 경로는 경보 대상에 의존하지 않아야 합니다.
    """
    try:
        import config  # noqa: F401  (임포트 시점에 .env 를 읽습니다)

        config.load_env_file()
        return
    except Exception as exc:  # noqa: BLE001 — 어떤 고장이든 알림은 살아야 합니다
        _log(f"config 임포트 실패({type(exc).__name__}) — .env 를 직접 읽습니다")

    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _scrub(text: str) -> str:
    """토큰·chat_id·이메일 주소를 지운 문자열.

    텔레그램 API 는 토큰을 URL 경로에 담습니다. 예외 문자열에 URL 이
    섞여 나오는 구현이 있으므로, 로그로 나가는 모든 문자열은 이걸 통과시킵니다.
    """
    for key, mask in (("TELEGRAM_BOT_TOKEN", "<token>"), ("TELEGRAM_CHAT_ID", "<chat_id>")):
        raw = os.environ.get(key, "")
        # 공백이 섞인 값이면 실제로 전송에 쓰인 것은 공백을 지운 쪽입니다.
        # 둘 다 가립니다 — 마스킹이 원본 형태에만 걸려 있으면 정작 URL 에
        # 실린 형태가 그대로 로그에 남습니다.
        for value in {raw, re.sub(r"\s+", "", raw)}:
            if value:
                text = text.replace(value, mask)
    return _EMAIL_RE.sub("<주소 생략>", text)


def _log(message: str) -> None:
    """cron.log 로 나가는 한 줄. 값이 아니라 사실만 남깁니다."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] [notify] {_scrub(message)}", flush=True)


def _clean_secret(name: str) -> str:
    """환경변수에서 값을 읽되, 내부 공백까지 지웁니다.

    토큰과 chat_id 에는 공백이 들어갈 수 없습니다. 실제로 `.env` 에
    `12345: AAH...` 처럼 콜론 뒤에 한 칸이 붙어 들어온 적이 있고, 그 결과는
    `InvalidURL` 한 줄 — 즉 붙여넣기 실수 하나로 경보 경로 전체가 죽는
    상태였습니다. 사람이 다시 붙여넣을 일에 경보를 걸어 두지 않습니다.
    """
    raw = os.environ.get(name, "")
    cleaned = re.sub(r"\s+", "", raw)
    if cleaned != raw:
        _log(f"{name} 에 공백이 있어 제거하고 사용합니다 — .env 를 정리하세요")
    return cleaned


# ── 전송 ──────────────────────────────────────────────────────────────
def send(text: str) -> bool:
    """텔레그램으로 한 건 보냅니다. 성공 True / 실패 False — 예외는 던지지 않습니다."""
    token = _clean_secret("TELEGRAM_BOT_TOKEN")
    chat_id = _clean_secret("TELEGRAM_CHAT_ID")

    missing = [
        name
        for name, value in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id))
        if not value
    ]
    if missing:
        _log(f"설정 없음({', '.join(missing)}) — 전송 건너뜀. .env 를 확인하세요")
        return False

    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"User-Agent": "inbox-pilot"},
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            body = response.read(2048).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # 본문에 description 이 들어 있고(예: "Unauthorized"), 원인 파악에
        # 유일하게 쓸모 있는 정보라 남깁니다. 토큰은 _scrub 이 지웁니다.
        detail = ""
        try:
            detail = exc.read(512).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        _log(f"전송 실패 HTTP {exc.code} {detail}".strip())
        return False
    except Exception as exc:  # noqa: BLE001 — 네트워크·DNS·타임아웃 전부
        _log(f"전송 실패({type(exc).__name__}): {exc}")
        return False

    try:
        ok = bool(json.loads(body).get("ok"))
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        _log(f"전송 실패 — 텔레그램 응답: {body[:300]}")
        return False

    _log("전송 완료")
    return True


# ── 메시지 ────────────────────────────────────────────────────────────
def _account_lines() -> tuple[list[str], list[str]]:
    """(계정 상태 줄, 재인증이 필요한 계정 키) — out/health.json 기준.

    주소가 아니라 키(`main`/`dev`)만 씁니다. 사람은 키로 계정을 구분하고,
    외부로 나가는 메시지에 메일함 주소를 실을 이유는 없습니다.
    """
    try:
        data = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [f"- 계정 상태 불명 (health.json {type(exc).__name__})"], []

    lines = [f"점검 시각: {data.get('updated_at', '?')}"]
    failed: list[str] = []
    for account in data.get("accounts", []):
        key = str(account.get("key", "?"))
        status = str(account.get("status", "?"))
        if status == "ok":
            lines.append(f"- {key}: ok")
            continue
        failed.append(key)
        # error 는 OAuth 라이브러리 문자열입니다. 주소가 섞일 수 있어
        # _scrub 을 태우고, 길면 잘라 메시지가 폰에서 접히지 않게 합니다.
        reason = _scrub(str(account.get("error") or "사유 미기록"))
        if len(reason) > 120:
            reason = reason[:117] + "..."
        lines.append(f"- {key}: {status} — {reason}")
    return lines, failed


def build_failure(rc: str, reason: str, count: str, first_fail: str, at: str) -> str:
    account_lines, failed = _account_lines()
    reauth_keys = failed or ["main", "dev"]
    return "\n".join(
        [
            "🚨 inbox-pilot 실행 실패",
            "",
            f"실패 시각: {at}",
            f"연속 실패: {count}회 (첫 실패: {first_fail})",
            f"사유: {reason}",
            f"종료 코드: exit={rc}",
            "",
            "계정 상태",
            *account_lines,
            "",
            "재인증 (사람만 가능)",
            *[REAUTH_CMD.format(key=key) for key in reauth_keys],
            "",
            "다음 실패부터는 복구될 때까지 보내지 않습니다. 상세: ALERT.md / out/cron.log",
        ]
    )


def build_recovery(count: str, first_fail: str, at: str) -> str:
    return "\n".join(
        [
            "✅ inbox-pilot 복구",
            "",
            f"복구 시각: {at}",
            f"연속 {count}회 실패 후 정상 종료 (첫 실패: {first_fail})",
            "",
            "ALERT.md 는 삭제되었습니다. 실패 이력은 out/cron.log 에 남아 있습니다.",
        ]
    )


def build_test(at: str) -> str:
    account_lines, _ = _account_lines()
    return "\n".join(
        [
            "🔔 inbox-pilot 알림 배선 테스트",
            "",
            f"발신 시각: {at}",
            "이 메시지가 보이면 실패·복구 알림이 폰까지 도달합니다.",
            "",
            "계정 상태",
            *account_lines,
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="inbox-pilot 텔레그램 알림 (상태 전이 전용)")
    parser.add_argument("event", choices=["fail", "recover", "test"])
    parser.add_argument("--rc", default="?", help="파이프라인 종료 코드")
    parser.add_argument("--reason", default="사유 미기록")
    parser.add_argument("--count", default="?", help="연속 실패 횟수")
    parser.add_argument("--first-fail", default="?", dest="first_fail")
    parser.add_argument("--at", default="", help="사건 시각 (미지정 시 현재 시각)")
    args = parser.parse_args(argv)

    _load_env()
    at = args.at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if args.event == "fail":
        text = build_failure(args.rc, args.reason, args.count, args.first_fail, at)
    elif args.event == "recover":
        text = build_recovery(args.count, args.first_fail, at)
    else:
        text = build_test(at)

    return 0 if send(text) else 1


if __name__ == "__main__":
    # 여기서 예외가 새어 나가면 호출한 셸이 비정상 종료 코드를 보고 놀랍니다.
    # 알림 실패는 알림 실패로만 끝나야 합니다.
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        _log(f"알림 처리 중 예외({type(exc).__name__}): {exc}")
        sys.exit(1)
