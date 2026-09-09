"""inbox-pilot 설정.

Stage 1 = shadow mode(읽기 전용). scope는 gmail.readonly 하나뿐이고,
메일함을 바꾸거나 메일을 보내는 코드는 이 프로젝트에 존재하지 않습니다.
"""

import importlib.util
import os
from pathlib import Path

# ── 경로 ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "out"
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
AUDIT_FILE = OUT_DIR / "audit.jsonl"
ENV_FILE = BASE_DIR / ".env"


# ── .env 로드 ─────────────────────────────────────────────────────────
def load_env_file(path: Path = ENV_FILE) -> list[str]:
    """repo 루트의 .env 를 읽어 os.environ 에 채우고, 채운 키 이름을 반환합니다.

    cron 은 사용자 셸을 거치지 않으므로 `export ANTHROPIC_API_KEY=...` 를
    물려받지 못합니다. 그 상태로 등록하면 키 없이 실행되고 Tier1이 조용히
    건너뛰어지므로, 프로세스 시작 시점에 파일에서 직접 읽습니다.

    - 이미 설정된 환경변수가 우선입니다(setdefault). 셸에서 준 값을
      .env 가 덮어쓰지 않습니다.
    - 형식은 한 줄 KEY=VALUE. 빈 줄과 # 주석은 무시합니다.
    - 값은 절대 로그·stdout 으로 나가지 않습니다. 반환값은 키 이름뿐입니다.

    표준 라이브러리만 씁니다 — 이 한 가지를 위해 의존성을 늘리지 않습니다.
    """
    if not path.exists():
        return []

    loaded = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key:
            continue
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


# import 시점에 로드합니다. main.py / auth.py / fetch.py 어느 진입점으로
# 들어와도 config 를 거치므로, 진입점마다 호출을 기억할 필요가 없습니다.
LOADED_ENV_KEYS = load_env_file()

# ── Gmail ─────────────────────────────────────────────────────────────
# 읽기 전용 단일 scope. 여기에 다른 scope를 추가하지 마세요.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# OAuth 리다이렉트 주소. auth.py 는 로컬 서버를 띄우지 않습니다 — 이 주소는
# 브라우저가 이동할 목적지일 뿐이고, 주소창에 남은 code 를 사람이 붙여넣습니다.
# (headless 머신이라 SSH 포워딩 없이 승인하기 위한 구조)
OAUTH_PORT = 8080
REDIRECT_URI = f"http://localhost:{OAUTH_PORT}"

# 본문은 앞 1KB만 사용하고, 원문은 절대 디스크에 저장하지 않습니다.
BODY_SNIPPET_BYTES = 1024

# ── Tier 1 (Anthropic) ────────────────────────────────────────────────
# BOOTSTRAP.md 요구사항대로 "저가 모델"을 사용합니다.
# Haiku 4.5는 현재 라인업 중 가장 저렴하며(입력 $1/MTok, 출력 $5/MTok),
# JSON 강제(structured outputs)를 지원합니다.
TIER1_MODEL = "claude-haiku-4-5"
TIER1_MAX_TOKENS = 256
TIER1_INPUT_COST_PER_MTOK = 1.00
TIER1_OUTPUT_COST_PER_MTOK = 5.00

# ── Tier 0 규칙 ───────────────────────────────────────────────────────
# 여기서 온 메일은 장애·배포·보안 알림으로 보고 P0로 올립니다.
# 계정별로 따로 적지 않고 공유합니다 — 같은 벤더를 쓰는데 계정마다 목록이
# 갈라지면, 목록이 빈 계정에서 인프라 규칙이 조용히 죽습니다(아래 main 주석).
INFRA_DOMAINS = [
    "github.com",
    "supabase.com",
    "vercel.com",
    "amazonaws.com",
    "anthropic.com",
]

# 계정 보안 경고를 보내는 발신 도메인.
# ■ 이 목록만으로는 P0를 만들지 않습니다. tier0.SECURITY_ALERT_PATTERN 이
#   **제목**에서도 걸려야 합니다. 도메인은 그 자체로 신호가 아니기 때문입니다 —
#   accounts.google.com 은 계정 탈취 경고와 "데이터 공유 안내" 를 같이 보냅니다.
#   (실측: 보안 경고 4건은 no-reply@accounts.google.com / List-Unsubscribe 없음,
#    저신호 데이터 공유 알림은 noreply-accounts@google.com / 헤더 있음 — 도메인도
#    헤더도 다르지만, 어느 하나에만 기대면 다음 발신 형태에서 무너집니다.)
# 여기에 도메인을 추가할 때는 그 발신자의 저신호 메일이 무엇인지 함께 확인하세요.
SECURITY_ALERT_DOMAINS = [
    "accounts.google.com",
]

# 개인이 직접 쓰는 메일 서비스의 도메인.
# ■ 이 목록만으로는 아무것도 승격시키지 않습니다. tier0 규칙 7이 Gmail
#   카테고리 `personal` **과** List-Unsubscribe 부재까지 함께 요구합니다.
#   도메인 하나로 올리면 gmail.com 주소로 발송하는 소상공인 자동메일이
#   전부 답장 대상이 됩니다.
#
# tier0._matches_domain 의 규칙을 그대로 씁니다 — "naver.com" 은 naver.com 과
# 그 서브도메인만 잡고, **navercorp.com 은 잡지 않습니다.** 이 구분이 이
# 목록의 핵심입니다: 실측 12일 창에서 navercorp.com 은 네이버페이 결제 알림
# (자동발송, 영수증)을 보냈고 naver.com 은 사람이 쓴 메일 4건을 보냈습니다.
# 문자열 포함(`in`)으로 구현하면 두 개가 같은 것이 되어 영수증이 P2로
# 올라옵니다.
CONSUMER_DOMAINS = [
    "naver.com",
    "gmail.com",
    "googlemail.com",
    "daum.net",
    "hanmail.net",
    "nate.com",
    "kakao.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "icloud.com",
    "me.com",
    "proton.me",
    "protonmail.com",
    "yahoo.com",
    "yahoo.co.jp",
]

# ── VIP 발신자 ────────────────────────────────────────────────────────
# INFRA_DOMAINS 와 같은 이유로 계정 공용 모듈 상수입니다. 계정별로 갈라 두면
# 목록이 빈 계정에서 규칙이 조용히 죽습니다(위 main 주석 참고).

# 놓치면 되돌릴 수 없는 기관 — 마감이 있고 법적 효력이 있습니다.
# 하나를 놓치는 비용이 광고 몇 건이 P0로 올라오는 비용보다 훨씬 크므로,
# 이 목록은 정밀도가 아니라 재현율 쪽으로 튜닝합니다(README §6).
#
# 두 형태를 섞어 씁니다 (tier0._matches_domain 이 구분합니다).
#   ".gov" 처럼 점으로 시작 → 접미사. 도메인이 정확히 그것으로 끝날 때만.
#                              ("notgov.com" 은 매칭되지 않습니다)
#   "state.gov" 처럼 점 없이 → 그 도메인 자신과 서브도메인.
#                              (travel.state.gov ⊂ state.gov)
# 개별 도메인을 **먼저** 적습니다. 분류 결과는 어느 쪽에 걸리든 P0로 같지만,
# 먼저 매칭된 항목이 감사 로그의 rule 값이 됩니다. 접미사가 앞에 있으면 국세청
# 고지서가 전부 "vip-p0-domain:.go.kr" 로만 남아, 나중에 오분류를 추적할 때
# 어느 기관이었는지 알 수 없습니다.
VIP_P0_DOMAINS = [
    # 개별 도메인 — 반드시 P0여야 하는 발신자. 접미사 목록을 나중에 좁히더라도
    # 이들은 남아야 합니다.
    "state.gov",          # 미 국무부
    "ustraveldocs.com",   # 미국 비자 예약 — 유일하게 접미사로 안 걸립니다
    "hometax.go.kr",      # 국세청 홈택스
    "nts.go.kr",          # 국세청
    "mofa.go.kr",         # 외교부
    "nhis.or.kr",         # 국민건강보험공단
    # 접미사 — 위에 없는 기관까지 한 줄로 덮습니다.
    ".gov",      # 미국 연방·주 정부
    ".edu",      # 미국 학교
    ".go.kr",    # 대한민국 정부기관
    ".or.kr",    # 공공기관·협회
]

# 사람이 직접 쓴 메일. 답장을 기대하는 상대이므로 needs_reply=True 로 둡니다.
# 도메인이 아니라 **주소 정확 일치**입니다 — 도메인으로 잡으면 같은 회사의
# 자동발송·마케팅까지 전부 P1이 됩니다.
#
# ■ 이 목록은 이 파일에 적지 않습니다 — vip_local.py 에서 읽습니다.
#
# 개인 주소는 제3자 데이터이고 이 저장소는 공개될 수 있습니다. 도메인(위
# VIP_P0_DOMAINS)은 기관의 공개 정보라 로직의 일부로 두어도 되지만, 개인
# 주소는 README §7 하드룰이 코드·문서·커밋에 넣는 것을 금지합니다.
#
# 정책을 사람의 주의력에 맡기면 언젠가 뚫립니다. 그래서 목록이 사는 파일을
# .gitignore 안으로 옮겨, 커밋 자체가 불가능해지는 구조로 막습니다.
# 형식은 vip_local.example.py 를 참고하세요.
VIP_LOCAL_FILE = BASE_DIR / "vip_local.py"


def _load_vip_addresses(path: Path = VIP_LOCAL_FILE) -> tuple[list[str], str]:
    """vip_local.py 에서 VIP 개인 주소 목록을 읽어 (목록, 상태문구) 를 반환합니다.

    파일이 없으면 빈 목록입니다 — 오류가 아닙니다. 이 저장소를 clone 한
    사람은 이 파일을 갖고 있지 않고, 그 상태에서도 파이프라인은 끝까지
    돌아야 합니다. VIP_P1 규칙만 발화하지 않습니다.

    다만 **파일이 있는데 읽다가 실패한 경우는 조용히 넘기지 않습니다.**
    두 상황은 전혀 다릅니다.

      - 파일 없음        → 설정하지 않은 것. 정상.
      - 파일 있는데 오류  → 설정했는데 동작하지 않는 것. 사고입니다.

    `try: import vip_local / except ImportError: return []` 로 쓰면 두 경우가
    같은 빈 목록으로 뭉개지고, 파일 안의 오타 하나가 VIP 규칙을 통째로 죽인
    채 아무 신호도 남기지 않습니다. 이 저장소가 반복해서 당한 패턴이라
    (README §4) 여기서는 존재 여부를 먼저 확인하고, 그다음 오류는 터뜨립니다.

    importlib 로 경로를 직접 지정합니다. `import vip_local` 은 실행 위치가
    repo 루트일 때만 동작하는데, cron 과 사람의 cwd 가 같으리라는 보장이
    없습니다.
    """
    if not path.exists():
        return [], f"없음 ({path.name} 미설정)"

    spec = importlib.util.spec_from_file_location("vip_local", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)   # 여기서 나는 오류는 그대로 올립니다

    raw = getattr(module, "VIP_P1_ADDRESSES", None)
    if raw is None:
        raise SystemExit(
            f"{path.name} 에 VIP_P1_ADDRESSES 가 없습니다.\n"
            f"      vip_local.example.py 의 형식을 확인하세요."
        )

    addresses = []
    for entry in raw:
        entry = entry.strip().lower()
        if entry:
            addresses.append(entry)
    return addresses, f"{path.name} ({len(addresses)}건)"


VIP_P1_ADDRESSES, VIP_P1_SOURCE = _load_vip_addresses()

# ── 계정 ──────────────────────────────────────────────────────────────
ACCOUNTS = [
    {
        "key": "main",
        "email": "you@example.com",
        "token_file": "token_main.json",
        "alias": "personal/legacy",
        # 광고·영수증 비중이 높은 계정이지만 "인프라 도메인 없음" 가정은 틀렸습니다.
        # 이 계정으로 Supabase 프로젝트 일시중지 알림이 실제로 옵니다. 목록이
        # 비어 있던 동안 그 메일은 인프라 규칙을 통과하지 못하고 본문의 billing
        # URL 때문에 영수증(P3)으로 떨어졌습니다.
        "tier0_overrides": {"infra_domains": INFRA_DOMAINS},
    },
    {
        "key": "dev",
        "email": "you.dev@example.com",
        "token_file": "token_dev.json",
        "alias": "dev",
        "tier0_overrides": {"infra_domains": INFRA_DOMAINS},
    },
]


def get_account(key: str) -> dict:
    for account in ACCOUNTS:
        if account["key"] == key:
            return account
    valid = ", ".join(a["key"] for a in ACCOUNTS)
    raise SystemExit(f"알 수 없는 계정 '{key}'. 사용 가능: {valid}")


def token_path(account: dict) -> Path:
    return BASE_DIR / account["token_file"]
