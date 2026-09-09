"""계정별 최초 1회 OAuth 승인 스크립트 (읽기 전용 권한).

로컬 웹서버를 띄우지 않는 수동 방식입니다. SSH 포트 포워딩이 필요 없습니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
사용법 (비개발자용 안내)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

■ 1단계 — 계정 하나씩 실행합니다. (계정당 딱 한 번만 하면 됩니다)

      ./venv/bin/python auth.py --account main     # you@example.com
      ./venv/bin/python auth.py --account dev    # you.dev@example.com

■ 2단계 — 터미널에 "이 URL을 브라우저에서 열어주세요: https://..." 가
  출력됩니다. 그 주소를 복사해 노트북 브라우저에 붙여넣고,
  해당 Gmail 계정으로 로그인 → 권한 승인을 누르세요.

  * 권한 화면에 "Gmail 메시지 및 설정 보기"(읽기 전용)만 나와야 정상입니다.
  * "이 앱은 확인되지 않았습니다" 경고가 나오면
    [고급] → [<앱 이름>(안전하지 않음)으로 이동]을 누르세요.
    (본인이 만든 OAuth 클라이언트이므로 정상입니다)

■ 3단계 — 승인을 누르면 브라우저가 localhost 로 이동하면서
  **"사이트에 연결할 수 없음" 같은 오류 화면**이 나옵니다. 정상입니다.
  이 머신에는 그 주소를 받아줄 서버가 없기 때문입니다.

  화면은 무시하고, **브라우저 주소창의 URL 전체를 복사**하세요.
  이런 모양입니다:

      http://localhost:8080/?state=...&code=4/0AX4...&scope=...

■ 4단계 — 그 URL을 터미널의 "리다이렉트 URL을 붙여넣으세요:" 뒤에
  붙여넣고 Enter. "저장 완료: token_main.json" 이 뜨면 끝입니다.

■ 참고
  - 주소창 URL 대신 code= 값만 붙여넣어도 동작합니다.
  - URL은 몇 분 안에 만료됩니다. 시간이 지났으면 스크립트를 다시 실행하세요.
  - 토큰 파일(token_*.json)은 비밀 정보입니다. .gitignore 에 등록되어 있어
    git 에 올라가지 않습니다. 내용을 열어볼 필요는 없습니다.
  - 토큰이 만료되면 fetch 단계에서 자동으로 갱신됩니다.
  - 다시 승인받고 싶으면 --force 를 붙여 실행하세요.
"""

import argparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

import config


def load_credentials(account: dict) -> Credentials | None:
    """저장된 토큰을 읽고, 필요하면 조용히 갱신합니다. 없으면 None."""
    path = config.token_path(account)
    if not path.exists():
        return None

    creds = Credentials.from_authorized_user_file(str(path), config.SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_credentials(account, creds)
        return creds
    return None


def save_credentials(account: dict, creds: Credentials) -> None:
    path = config.token_path(account)
    path.write_text(creds.to_json(), encoding="utf-8")
    path.chmod(0o600)  # 본인만 읽기


def _normalize_response(raw: str) -> tuple[str, str]:
    """붙여넣은 값을 (종류, 값)으로 정리합니다.

    종류는 "url" 또는 "code". 따옴표·공백은 제거합니다.

    oauthlib 은 OAuth 응답이 https 로 오기를 요구하므로 http:// 를 https:// 로
    바꿔서 넘깁니다. google-auth-oauthlib 의 run_local_server 도 같은 방식을
    씁니다 (flow.py: "oauthlib is very picky that OAuth 2.0 should only occur
    over https"). 실제 네트워크 요청이 아니라 파싱용 문자열이므로 안전합니다.
    """
    value = raw.strip().strip("'\"").strip()
    if not value:
        raise SystemExit("입력이 비어 있습니다. 스크립트를 다시 실행하세요.")

    if "://" not in value:
        # code= 값만 붙여넣은 경우.
        return "code", value

    if value.startswith("http://"):
        value = "https://" + value[len("http://") :]
    return "url", value


def authorize(account: dict, force: bool = False) -> Credentials:
    """수동 붙여넣기 방식으로 승인하고 토큰을 저장합니다."""
    if not force:
        existing = load_credentials(account)
        if existing:
            print(f"이미 승인되어 있습니다: {account['email']}")
            print(f"  토큰 파일: {account['token_file']}")
            print("  다시 승인하려면 --force 를 붙여 실행하세요.")
            return existing

    if not config.CREDENTIALS_FILE.exists():
        raise SystemExit(
            f"credentials.json 을 찾을 수 없습니다: {config.CREDENTIALS_FILE}"
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(config.CREDENTIALS_FILE), config.SCOPES
    )
    # 서버를 띄우지 않습니다. 이 주소는 브라우저가 이동할 목적지일 뿐이고,
    # 우리는 주소창에 남은 code 를 손으로 받아옵니다.
    flow.redirect_uri = config.REDIRECT_URI

    auth_url, _state = flow.authorization_url(
        access_type="offline",  # refresh_token 발급
        prompt="consent",       # 재실행 시에도 refresh_token 확실히 받기
        include_granted_scopes="true",
    )

    print()
    print("=" * 70)
    print(f"승인할 계정: {account['email']}  ({account['alias']})")
    print("요청 권한  : Gmail 읽기 전용 (gmail.readonly)")
    print("=" * 70)
    print()
    print("1) 이 URL을 브라우저에서 열어주세요:")
    print()
    print(f"   {auth_url}")
    print()
    print("2) 로그인 → 권한 승인을 누르세요.")
    print()
    print("3) 승인 후 브라우저에 '사이트에 연결할 수 없음' 오류가 나옵니다.")
    print("   정상입니다. 화면은 무시하고 주소창의 URL 전체를 복사하세요.")
    print(f"   ({config.REDIRECT_URI}/?state=...&code=... 형태)")
    print()

    kind, value = _normalize_response(input("4) 리다이렉트 URL을 붙여넣으세요: "))

    try:
        if kind == "url":
            flow.fetch_token(authorization_response=value)
        else:
            flow.fetch_token(code=value)
    except Exception as exc:  # oauthlib/requests 예외 종류가 다양합니다
        raise SystemExit(
            "\n토큰 교환에 실패했습니다: "
            f"{type(exc).__name__}: {exc}\n\n"
            "자주 있는 원인:\n"
            "  - URL이 만료됨 (몇 분 지나면 무효) → 스크립트를 다시 실행하세요\n"
            "  - 승인 화면에서 '취소'를 눌렀거나 URL에 error= 가 포함됨\n"
            "  - URL 일부만 복사됨 → 주소창 내용을 처음부터 끝까지 복사하세요\n"
            "  - 이미 한 번 사용한 code (code 는 1회용입니다)"
        ) from exc

    creds = flow.credentials
    save_credentials(account, creds)

    print()
    print(f"저장 완료: {account['token_file']}")
    if not creds.refresh_token:
        print("경고: refresh_token 이 없습니다. 토큰 만료 후 재승인이 필요합니다.")
    print("이제 fetch/main 을 실행할 수 있습니다: python main.py --days 3")
    return creds


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gmail 계정별 최초 1회 OAuth 승인 (읽기 전용, 수동 붙여넣기)"
    )
    parser.add_argument(
        "--account",
        required=True,
        choices=[a["key"] for a in config.ACCOUNTS],
        help="승인할 계정 (main 또는 dev)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="이미 토큰이 있어도 다시 승인받기",
    )
    args = parser.parse_args()

    authorize(config.get_account(args.account), force=args.force)


if __name__ == "__main__":
    main()
