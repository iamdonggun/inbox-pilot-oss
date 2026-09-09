"""Tier 0 — 결정론적 규칙 분류기 (API 호출 없음, 무료).

여기서 분류되면 Tier 1(LLM)을 타지 않으므로 비용이 0입니다.

■ 우선순위(먼저 매칭된 규칙이 이깁니다)
    1. OTP / 인증번호        → Ephemeral   (수명이 몇 분. 가장 구체적이라 최우선)
    2. 계정 보안 경고         → P0         (도메인 **과** 제목이 둘 다 맞을 때만)
    3. VIP 기관 도메인        → P0         (정부·학교·세무. 마감과 법적 효력)
    4. 계정별 infra 도메인    → P0         (장애·배포·보안 알림. 대량 발송과
                                            명백한 영수증은 제외 — 아래 주석)
    5. VIP 개인 주소          → P1         (사람이 쓴 메일. needs_reply=True)
    6. 로그인·기기 알림       → P1         (계정 접근 통지. 봐야 하지만 답장 아님)
    7. 문의 폼 리드           → P1         (제목 말머리 + 본문 폼 구조. needs_reply)
    8. 개인 발신              → P2         (개인 메일 서비스 + personal. needs_reply)
    9. 조치 요구             → P2         (제목의 명시적 요구. 영수증보다 먼저)
   10. 영수증 / 결제 / 주문   → P3
   11. List-Unsubscribe 헤더 → P5         (대량 발송의 사실상 확정 신호)
   12. Gmail 카테고리 라벨    → P4~P5

  ■ 규칙 5~8은 발신 주체를 식별합니다
  5(명시적 VIP 목록)·7(구조화된 문의 폼)·8(개인 메일 서비스)은 "사람이 썼다"
  이고, 6(계정 접근 통지)은 "기계가 보냈지만 사람이 봐야 한다"입니다. 6이
  7·8보다 앞인 이유는 접근 통지가 Gmail 에서 personal 카테고리로 오기
  때문입니다 — 뒤에 두면 로그인 알림이 사람이 쓴 메일로 읽힙니다.

  넷 다 영수증(10)보다 앞입니다. 사람이 쓴 메일에는 "결제"·"payment" 가
  대화체로 섞여 들어오는데, 뒤에 두면 답장해야 할 메일이 영수증 더미로
  떨어집니다. 그 반대 방향의 오탐(개인 주소로 발송되는 소상공인 주문 확인이
  P2가 되는 것)은 비대칭 튜닝 원칙상 감수하는 쪽입니다.

  BOOTSTRAP.md에는 List-Unsubscribe가 먼저 적혀 있지만, 그 순서대로 두면
  "인증번호" 메일에 마케팅 푸터가 붙어 있을 때 P5로 묻히고 OTP가 사라집니다.
  그래서 구체적인 규칙(OTP·VIP·infra·영수증)을 앞으로 옮겼습니다.

  ■ VIP 규칙이 영수증보다 앞에 있는 이유
  세무서·학교·보험공단 메일에는 "납부"·"결제"·"청구" 가 일상적으로 들어갑니다.
  VIP를 영수증 뒤에 두면 국세청 고지서가 키워드 하나로 P3에 묻힙니다. 같은
  패턴으로 이미 한 번 당했습니다 — supabase 인프라 알림이 본문 링크의
  /billing 때문에 영수증으로 떨어졌습니다(README §4-3).

  ■ 순서만으로는 부족합니다
  규칙 4는 계정의 infra_domains 가 비어 있으면 조용히 통과됩니다. 목록이 비면
  순서가 맞아도 인프라 메일은 영수증 규칙까지 흘러가서, 본문에 billing 같은
  단어가 있다는 이유로 P3이 됩니다. 그래서 도메인·주소 목록은 전부
  config 의 모듈 상수(INFRA_DOMAINS / VIP_P0_DOMAINS / VIP_P1_ADDRESSES /
  SECURITY_ALERT_DOMAINS)에서 전 계정이 공유합니다.

  ■ 제목에서만 보는 규칙과 본문까지 보는 규칙
  규칙 2·6·9(보안 경고·접근 통지·조치 요구)는 **제목만** 봅니다. 나머지 키워드 규칙은
  제목+본문(URL 제거)을 봅니다. 본문에는 벤더의 상투구가 섞여 있어서,
  "action required" 푸터나 "security" 안내문 한 줄이 본문에 있다는 이유로
  광고가 P0/P2로 올라옵니다. 발신자가 그 메일을 무엇이라고 부르는지는
  제목에 있고, 승격 규칙은 그 신호만 씁니다.
"""

import re

import config

# ── 패턴 ──────────────────────────────────────────────────────────────
# 코드 자체를 가리키는 명사구만 넣습니다. "verify"·"confirm" 같은 동사는 넣지
# 않습니다 — 이 규칙이 1순위라서, 동사를 넣으면 "[Action required] Verify your
# email address"(조치 요구)와 계정 보안 경고까지 Ephemeral 로 삼킵니다.
OTP_PATTERN = re.compile(
    r"인증\s?번호|인증\s?코드|확인\s?코드|임시\s?비밀번호"
    r"|verification code|one[- ]time (?:code|password)|login code|temporary code"
    r"|\bOTP\b|security code|2FA|two[- ]factor",
    re.IGNORECASE,
)

# 계정 보안 경고. **제목에서만** 찾고, config.SECURITY_ALERT_DOMAINS 와 함께
# 걸릴 때만 P0가 됩니다(규칙 2 주석 참고).
SECURITY_ALERT_PATTERN = re.compile(
    r"보안\s?(?:경고|알림|알림말)|security alert|critical security"
    r"|새로운?\s?기기(?:에서)?\s?로그인|비정상\S*\s?로그인|의심스러운?\s?로그인"
    r"|new sign[- ]?in|sign[- ]?in (?:attempt|detected|blocked)"
    r"|suspicious (?:sign[- ]?in|activity|login)"
    r"|비밀번호가?\s?(?:변경|재설정)|password was (?:changed|reset)"
    r"|계정(?:이)?\s?(?:잠겼|정지|해킹)|account (?:was )?(?:locked|compromised|disabled)",
    re.IGNORECASE,
)

# 계정 접근 통지(로그인·새 기기). **제목에서만** 찾습니다.
#
# ■ 위 SECURITY_ALERT_PATTERN 과의 경계
# 두 패턴은 겹치는 것이 아니라 **분업**합니다.
#   · SECURITY_ALERT_PATTERN(규칙 2) — 침해를 **주장**하는 문구까지 포함합니다
#     (비밀번호 변경, 계정 잠김, suspicious). 대신 발신 도메인이
#     config.SECURITY_ALERT_DOMAINS 에 있을 때만 발화하고 결과는 P0입니다.
#   · 이 패턴(규칙 6) — 접근이 **일어났다는 통지**만 봅니다. 도메인을 묻지
#     않는 대신 결과는 P1입니다.
# 즉 축은 하나입니다: "신뢰 목록에 있는 발신자의 보안 경고"만 P0이고, 그 밖의
# 접근 통지는 전부 P1. 규칙 2가 먼저 오므로 accounts.google.com 의 경고는
# 계속 P0이고, 이 규칙이 그것을 끌어내리지 못합니다.
#
# 비밀번호 변경·계정 잠김을 여기 넣지 않은 것은 의도적입니다. 신뢰 목록 밖
# 발신자가 그런 문구를 쓰는 전형적인 경우가 피싱이고, 그것을 P1로 올리는 것은
# 이 규칙의 목적이 아닙니다. 실측 창에서도 해당 사례는 0건이었습니다.
#
# **List-Unsubscribe 를 조건으로 걸지 않습니다.** 규칙 2·9와 다른 점입니다.
# 실측: Instagram 로그인 알림(mail.instagram.com)에 그 헤더가 붙어 있어서
# 규칙 11로 흘러가 P5(광고)로 묻혀 있었습니다. 소비자 서비스는 보안 통지를
# 대량 발송 인프라로 보냅니다 — 기관 마감 통지에서 규칙 3이 같은 이유로
# 이 조건을 빼는 것과 같습니다. 대가로 "New sign-in" 류 제목의 광고가 P1로
# 올라올 수 있지만, 실측 창 161건에서 이 패턴에 걸린 5건은 전부 진짜
# 접근 통지였습니다.
LOGIN_ALERT_PATTERN = re.compile(
    r"새로운?\s?(?:기기|환경|위치|장치)(?:에서)?\s?로그인|로그인\s?(?:알림|안내|되었습니다)"
    r"|new sign[- ]?in|new login|sign[- ]?in (?:detected|from|on|to)"
    r"|logged in (?:from|to)|계정에?\s?로그인",
    re.IGNORECASE,
)

# 명시적 조치 요구. **제목에서만** 찾습니다(규칙 9 주석 참고).
#
# 마감일 패턴("D-3", "by Oct 12", "마감")은 일부러 넣지 않았습니다. 실측 12일
# 창에서 제목에 "마감"이 든 메일은 광고 하나뿐이었습니다
# ("(광고) 마감 D-1 무료 중국 여행"). 마감일은 광고가 가장 즐겨 쓰는 문구라
# 넣는 순간 판촉이 P2로 올라옵니다. 발신자가 "조치가 필요하다"고 **명시한**
# 경우만 봅니다.
ACTION_REQUIRED_PATTERN = re.compile(
    r"actions? require[ds]|action requested|response required|즉시\s?조치"
    r"|조치\s?(?:가\s?)?(?:필요|요망|바랍니다)|확인\s?요망"
    r"|(?:지원|서비스|거래지원)\s?종료\s?안내|서비스\s?(?:종료|중단)\s?(?:예정|안내)"
    r"|이용\s?제한\s?안내|계약\s?만료\s?안내",
    re.IGNORECASE,
)

# 문의 폼 리드의 **제목 말머리**. 제목 맨 앞의 대괄호 말머리만 봅니다 —
# 본문이나 제목 중간의 "문의"는 광고에도 흔합니다("문의는 고객센터로").
INQUIRY_SUBJECT_PATTERN = re.compile(
    r"^\s*[\[\(【]\s*(?:문의|상담|제휴|견적|제안|의뢰)\s*[\]\)】]",
)

# 문의 폼 리드의 **본문 구조**. 웹 폼이 만든 메일은 "라벨: 값" 줄의 나열이고,
# 사람이 자유롭게 쓴 메일이나 광고는 이 형태가 아닙니다.
#
# 발신 도메인으로 잡지 않는 이유가 여기 있습니다: 실측된 리드는 resend.dev
# 에서 왔는데 그 주소는 공용 샌드박스 발신 도메인이라 같은 곳에서 "Hello
# World" 테스트 메일도 옵니다. 도메인은 누가 보냈는지를 말해주지 않고,
# 폼 구조는 말해줍니다.
#
# **서로 다른** 라벨이 3종 이상일 때만 인정합니다. 같은 라벨이 세 번 반복된
# 것(인용된 답장 등)은 폼이 아닙니다.
FORM_LABEL_PATTERN = re.compile(
    r"(이름|성함|담당자|업체명|회사명|회사|연락처|전화번호|휴대폰"
    r"|이메일|메일\s?주소|문의\s?유형|문의\s?내용|상담\s?내용|요청\s?사항"
    r"|예산|희망\s?일정|지역"
    r"|name|phone|e-?mail|company|budget|message|inquiry|subject)\s*[:：]",
    re.IGNORECASE,
)
FORM_LABEL_MIN = 3

# "booking" 을 홀로 두지 않는 이유: 여행·테마파크 광고 본문에 "book now",
# "booking period" 가 상투구로 들어 있어서, 생일 프로모션 메일이 영수증(P3)이
# 됐습니다. 거래를 가리키는 명사구일 때만 매칭합니다.
RECEIPT_PATTERN = re.compile(
    r"영수증|결제|주문|청구서|입금|출금|receipt|invoice|order confirmation"
    r"|booking (?:confirm\w*|reference|number|details)|your booking"
    r"|예약\s?(?:확인|완료|내역)"
    r"|payment|billing|subscription renewal",
    re.IGNORECASE,
)

# infra 도메인 매칭을 뒤집을 수 있는 **강한** 영수증 신호.
# 같은 도메인이 장애 알림과 청구서를 둘 다 보냅니다 — README §4-3 버그의
# 거울상으로, "Your receipt from Anthropic, PBC" 가 infra P0로 올라갔습니다.
# 도메인 규칙은 내용을 보지 않으므로, 문서 명사(영수증·청구서·invoice)가
# 있을 때만 P3로 내립니다. payment·billing 같은 일반 결제 단어는 여기 넣지
# 않습니다 — "payment failed" 류 과금 이상은 분류표상 P0가 맞고, P0는
# 재현율 쪽으로 기울인다는 원칙(위 규칙 2 주석)이 여기에도 적용됩니다.
RECEIPT_STRONG_PATTERN = re.compile(
    r"영수증|청구서|receipt|invoice|order confirmation",
    re.IGNORECASE,
)

# 키워드 매칭에서 URL을 빼기 위한 패턴.
# URL 경로는 사람이 쓴 문장이 아니라 벤더의 라우팅 구조입니다. /billing 이나
# /payment 같은 경로 조각이 결제 사실의 증거로 읽히면 안 됩니다.
# (실제 사례: Supabase 프로젝트 일시중지 알림이 대시보드 링크의
#  ".../billing?panel=subscriptionPlan" 때문에 영수증으로 분류됐습니다.)
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

# Gmail 카테고리 라벨 → (category, urgency)
CATEGORY_MAP = {
    "promotions": ("promo", "P5"),
    "social": ("social", "P5"),
    "updates": ("update", "P4"),
}


def _infra_domains(account: dict) -> list[str]:
    return account.get("tier0_overrides", {}).get("infra_domains", [])


# VIP 개인 주소는 집합으로 미리 정규화합니다. 조회가 O(1)인 것보다,
# 대소문자 정규화를 한 곳에서만 하는 것이 중요합니다 — From 헤더의 대소문자는
# 발신 서버 마음이라 목록과 메시지 양쪽이 같은 규칙을 따라야 합니다.
# (fetch._extract_email 이 이미 소문자로 내리지만 여기서 다시 보장합니다)
VIP_P1_ADDRESSES = frozenset(a.strip().lower() for a in config.VIP_P1_ADDRESSES)


def _matches_domain(from_domain: str, patterns: list[str]) -> str | None:
    """도메인 매칭. 목록에는 두 형태가 섞여 들어옵니다.

    "." 로 시작하는 항목은 **접미사**입니다. 도메인이 정확히 그 접미사로 끝날
    때만 매칭됩니다. 항목 자체가 점으로 시작하므로 라벨 경계가 보장되고,
    "notgov.com" 은 ".gov" 에 걸리지 않습니다. 이걸 문자열 포함(`in`)으로
    구현하면 바로 그 오탐이 납니다 — 도메인 규칙은 조용히 틀리기 때문에
    경계 조건을 코드가 아니라 데이터 형태로 못 박아 둡니다.

    점 없이 시작하는 항목은 그 도메인 자신과 서브도메인입니다.
    (travel.state.gov ⊂ state.gov, mail.github.com ⊂ github.com)
    """
    if not from_domain:
        return None
    for pattern in patterns:
        if pattern.startswith("."):
            if from_domain.endswith(pattern):
                return pattern
        elif from_domain == pattern or from_domain.endswith("." + pattern):
            return pattern
    return None


def _keyword_haystack(message: dict) -> str:
    """키워드 규칙이 볼 텍스트. URL은 통째로 제거합니다.

    도메인 판정은 URL이 아니라 From 헤더(from_domain)로 하므로, 여기서 링크를
    버려도 규칙 2는 영향을 받지 않습니다.
    """
    text = f"{message['subject']}\n{message['body_snippet']}"
    return URL_PATTERN.sub(" ", text)


def _body_haystack(message: dict) -> str:
    """본문만 보는 규칙이 볼 텍스트. 여기서도 URL을 먼저 제거합니다.

    폼 라벨은 `이메일:` 처럼 콜론으로 끝나는데, URL 안에는 `https:` 와
    쿼리스트링(`?name=...&company=...`)이 그대로 들어 있습니다. URL을 남기면
    링크 하나가 라벨 3종을 혼자 채워서, 폼이 아닌 메일이 리드가 됩니다
    (README §4-3 의 /billing 오탐과 같은 계열).
    """
    return URL_PATTERN.sub(" ", message["body_snippet"])


def _distinct_form_labels(body: str) -> int:
    """본문에 나타난 **서로 다른** 폼 라벨의 개수."""
    return len({m.group(1).lower().replace(" ", "") for m in FORM_LABEL_PATTERN.finditer(body)})


def classify(message: dict, account: dict) -> dict | None:
    """분류되면 결과 dict, 아니면 None(→ Tier 1로 넘김)."""
    haystack = _keyword_haystack(message)

    # 1. OTP — 수명이 짧아 다이제스트에서도 별도 취급.
    if OTP_PATTERN.search(haystack):
        return _result("otp", "Ephemeral", 0.95, "otp-pattern")

    # 2. 계정 보안 경고 — 도메인과 제목이 **둘 다** 맞을 때만 P0.
    #
    #    도메인만으로 올리면 안 됩니다. accounts.google.com 은 계정 탈취 경고와
    #    "Google 계정 데이터 일부를 X에 공유하셨습니다"(저신호)를 같이 보냅니다.
    #    반대로 제목만으로 올리면 "보안" 이 든 광고·뉴스레터가 전부 P0가 됩니다.
    #    두 신호가 만나는 지점만 P0입니다.
    #
    #    P0의 정의가 "보안 알림"인데 이 규칙이 없는 동안 Google 계정 보안 경고
    #    4건이 규칙 9(Gmail 카테고리 updates)까지 흘러가 P4로 묻혔습니다.
    #    일반 규칙이 구체적 신호를 삼킨, README 4절과 같은 계열의 버그입니다.
    if not message["has_list_unsubscribe"]:
        security_domain = _matches_domain(
            message["from_domain"], config.SECURITY_ALERT_DOMAINS
        )
        if security_domain and SECURITY_ALERT_PATTERN.search(message["subject"]):
            return _result("security", "P0", 0.90, f"security-alert:{security_domain}")

    # 3. VIP 기관 — 정부·학교·세무·보험. 마감이 있고 되돌릴 수 없습니다.
    #
    #    infra 규칙과 달리 List-Unsubscribe 를 예외로 두지 **않습니다.** 기관은
    #    마감 통지를 대량 발송 시스템으로 보내는 경우가 실제로 있어서, 그 조건을
    #    걸면 정작 놓치면 안 되는 메일이 P5로 떨어집니다. 대신 학교·기관
    #    뉴스레터가 P0로 올라오는 오탐을 감수합니다 — P0는 정밀도가 아니라
    #    재현율 쪽으로 기울이는 것이 이 프로젝트의 비대칭 튜닝 원칙입니다.
    vip_domain = _matches_domain(message["from_domain"], config.VIP_P0_DOMAINS)
    if vip_domain:
        return _result("institution", "P0", 0.90, f"vip-p0-domain:{vip_domain}")

    # 4. 계정별 인프라 도메인.
    #    단, 대량 발송(List-Unsubscribe)은 제외합니다. 같은 벤더가 장애 알림과
    #    마케팅 뉴스레터를 같이 보내는데(supabase.com: ant.wilson=알림,
    #    welcome=뉴스레터), 장애 알림에는 구독 취소 헤더가 붙지 않습니다.
    #    이 조건이 없으면 뉴스레터가 P0로 올라옵니다.
    if not message["has_list_unsubscribe"]:
        matched = _matches_domain(message["from_domain"], _infra_domains(account))
        if matched:
            # 예외: 명백한 영수증(문서 명사)은 P3로 내립니다. haystack 은 이미
            # URL이 제거된 텍스트라, 링크 경로의 /billing 은 여기 걸리지 않고
            # supabase 일시중지 알림은 P0로 남습니다(README §4-3 수정 유지).
            if RECEIPT_STRONG_PATTERN.search(haystack):
                return _result("receipt", "P3", 0.80, f"receipt-over-infra:{matched}")
            return _result("infra", "P0", 0.90, f"infra-domain:{matched}")

    # 5. VIP 개인 — 사람이 직접 쓴 메일. 답장 대상으로 표시합니다.
    #
    #    감사 로그에는 주소가 아니라 도메인만 남깁니다. audit.jsonl 은
    #    append-only 이고 "발신자 주소는 넣지 않는다"가 그 파일의 하드룰입니다
    #    (audit.py 상단). 어느 VIP였는지는 다이제스트의 발신자 줄로 확인합니다.
    if message["from_email"] in VIP_P1_ADDRESSES:
        return _result(
            "vip",
            "P1",
            0.95,
            f"vip-p1:{message['from_domain']}",
            needs_reply=True,
        )

    # 6. 로그인·기기 알림 — 계정 접근이 일어났다는 통지.
    #
    #    needs_reply=False 입니다. 본인 접속이면 무시하고 아니면 즉시 대응인데,
    #    그 판별은 기계가 할 수 없고 사람만 할 수 있습니다. 사람이 매번 봐야
    #    하므로 P1이고, 답장할 상대가 없으므로 needs_reply 는 끕니다.
    #
    #    이 규칙이 존재하는 이유는 정확도가 아니라 **재현성**입니다. 같은
    #    megazone 로그인 알림이 Tier1에서 8/15 P0/infra/0.95, 8/17
    #    P1/update/0.92 로 갈렸습니다. LLM 판정은 실행마다 흔들리고 규칙은
    #    흔들리지 않습니다 — 반복되는 유형은 비용이 아니라 안정성 때문에
    #    규칙으로 내려야 합니다.
    if LOGIN_ALERT_PATTERN.search(message["subject"]):
        return _result("access", "P1", 0.85, "login-alert")

    # 7. 문의 폼 리드 — 웹사이트 문의 폼이 만들어 보낸 영업 리드.
    #
    #    제목 말머리(구조 신호 1) **와** 본문의 서로 다른 폼 라벨 3종 이상
    #    (구조 신호 2)이 동시에 맞을 때만 P1입니다. 대량 발송은 제외합니다.
    #
    #    발신 도메인을 조건에 넣지 않는 것이 이 규칙의 핵심입니다. 실측된
    #    리드는 resend.dev — 공용 샌드박스 발신 도메인이라 같은 주소에서
    #    "Hello World" 테스트 메일도 옵니다. 도메인으로 잡았다면 그 테스트
    #    메일까지 P1이 됐습니다. 누가 보냈는지가 아니라 무엇이 담겼는지로
    #    판단합니다.
    #
    #    놓치는 비용이 큽니다 — 답장하지 않은 영업 리드는 그대로 잃은
    #    매출이라, VIP 개인(5)과 같은 P1에 둡니다.
    if not message["has_list_unsubscribe"] and INQUIRY_SUBJECT_PATTERN.search(
        message["subject"]
    ):
        labels = _distinct_form_labels(_body_haystack(message))
        if labels >= FORM_LABEL_MIN:
            return _result(
                "lead", "P1", 0.85, f"inquiry-form:{labels}labels", needs_reply=True
            )

    # 8. 개인 발신 — 개인 메일 서비스에서 온, 대량 발송이 아닌 개인 메일.
    #
    #    세 신호가 모두 맞아야 합니다: Gmail 이 `personal` 로 분류했고,
    #    발신 도메인이 개인이 직접 쓰는 메일 서비스이며, List-Unsubscribe 가
    #    없을 것. 하나라도 빼면 무너집니다 —
    #      · 카테고리만: megazone/navercorp 의 자동 로그인 알림도 personal 입니다.
    #      · 도메인만:   gmail.com 주소로 보내는 소상공인 자동메일이 다 걸립니다.
    #      · 헤더만:     의미 없는 조건입니다.
    #
    #    P1이 아니라 P2인 이유: P1은 **명시적 VIP 목록**(규칙 5)에만 남깁니다.
    #    "개인이 보냈다"는 답장 대상이라는 근거는 되지만 최우선이라는 근거는
    #    아닙니다. 그래도 needs_reply=True 는 켭니다 — 사람이 보낸 메일의
    #    기본값은 답장입니다.
    if (
        not message["has_list_unsubscribe"]
        and "personal" in message["gmail_categories"]
    ):
        consumer = _matches_domain(message["from_domain"], config.CONSUMER_DOMAINS)
        if consumer:
            return _result(
                "personal", "P2", 0.80, f"personal-sender:{consumer}", needs_reply=True
            )

    # 9. 조치 요구 — 발신자가 제목에서 명시적으로 요구한 경우.
    #
    #    영수증(10)보다 **먼저** 옵니다. 조치 요구 메일에는 결제·거래 단어가 거의
    #    항상 같이 들어 있어서("Update your API billing", "거래지원 종료 안내"),
    #    뒤에 두면 마감이 있는 메일이 영수증 더미에 묻힙니다. 규칙 3이 영수증보다
    #    앞에 있는 것과 같은 이유입니다.
    #
    #    대량 발송(List-Unsubscribe)은 제외합니다. 판촉 메일은 "지금 조치하세요"
    #    류 문구를 쓰는데, 실측 창에서 조치 요구 메일 3건은 모두 헤더가 없었고
    #    헤더가 있는 쪽은 광고였습니다. 기관 마감 통지는 규칙 3이 이미 앞에서
    #    잡으므로 이 조건 때문에 놓치지 않습니다.
    if not message["has_list_unsubscribe"] and ACTION_REQUIRED_PATTERN.search(
        message["subject"]
    ):
        return _result("action", "P2", 0.80, "action-required")

    # 10. 영수증 / 결제 / 주문.
    if RECEIPT_PATTERN.search(haystack):
        return _result("receipt", "P3", 0.80, "receipt-keyword")

    # 11. List-Unsubscribe 헤더 = 대량 발송.
    #
    #     영수증(10)보다 **뒤**입니다. 실측: 토스페이먼츠 결제 알림
    #     (PG사 결제 알림 전용 발신 주소)에 List-Unsubscribe 헤더가 붙어
    #     있었습니다. 이 규칙을 앞에 두거나 영수증 규칙에 "헤더 없을 것"을
    #     걸면 진짜 결제 내역이 광고(P5)로 떨어집니다.
    if message["has_list_unsubscribe"]:
        return _result("promo", "P5", 0.85, "list-unsubscribe-header")

    # 12. Gmail 카테고리 라벨.
    for gmail_category in message["gmail_categories"]:
        if gmail_category in CATEGORY_MAP:
            category, urgency = CATEGORY_MAP[gmail_category]
            return _result(category, urgency, 0.75, f"gmail-category:{gmail_category}")

    return None


def _result(
    category: str,
    urgency: str,
    confidence: float,
    rule: str,
    needs_reply: bool = False,
) -> dict:
    return {
        "tier": 0,
        "category": category,
        "urgency": urgency,
        "confidence": confidence,
        # 기본값은 False 입니다. 자동 발송(OTP·영수증·광고·인프라 알림)은
        # 답장 대상이 아니고, 애매한 건의 판단은 Tier 1의 몫입니다.
        # 예외는 VIP 개인뿐 — 사람이 쓴 메일은 기본적으로 답장 대상입니다.
        "needs_reply": needs_reply,
        "rule": rule,
    }
