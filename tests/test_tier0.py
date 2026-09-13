"""Tier 0 규칙 회귀 테스트.

    python tests/test_tier0.py          # 종료 코드 0 = 전부 통과, 1 = 실패

의존성 없이 표준 라이브러리만 씁니다. 네트워크도, 토큰도, `credentials.json` 도
필요하지 않습니다 — 규칙 분류기는 순수 함수이므로 합성 메시지로 전부 검증됩니다.

**왜 이 파일이 필요한가.** 이 저장소가 잡은 버그 다섯 건은 전부 예외를 던지지
않았습니다(README §4). 규칙 분류기는 죽는 대신 조용히 다른 답을 내놓고, 그
답은 다이제스트에 정상적인 얼굴로 실립니다. 규칙을 고칠 때마다 "무엇을 잡을까"는
눈에 보이지만 "무엇을 삼킬까"는 보이지 않습니다. 아래 케이스는 그 삼킴을
고정해 둔 것입니다.

케이스 이름의 `R<n>` 은 README §6 규칙표의 번호, `B<n>` 은 README §4 버그
번호입니다. 규칙을 바꿀 때 여기가 깨지면, 테스트가 틀린 것인지 규칙이 틀린
것인지 **판단한 결과를 커밋 메시지에 남기고** 고치십시오.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import tier0  # noqa: E402

ACCOUNT = config.ACCOUNTS[0]


def msg(subject="", body="", frm="someone@vendor.example", lu=False, cats=None):
    """합성 메시지. fetch.py 가 만드는 dict 와 같은 모양입니다."""
    return {
        "account_key": ACCOUNT["key"],
        "account_email": ACCOUNT["email"],
        "message_id": "test",
        "thread_id": "test",
        "subject": subject,
        "from": frm,
        "from_email": frm,
        "from_domain": frm.split("@")[-1],
        "to": "",
        "date": "",
        "age_days": 0,
        "labels": [],
        "gmail_categories": cats or [],
        "is_unread": True,
        "has_list_unsubscribe": lu,
        "body_snippet": body,
    }


FORM_BODY = "이름: 홍길동\n연락처: 000-0000-0000\n문의 내용: 견적 요청드립니다"

# (이름, 메시지, 기대 category, 기대 urgency) — None/None 은 미분류(→ Tier 1).
CASES = [
    ("R1  OTP", msg(subject="[인증번호] 123456"), "otp", "Ephemeral"),
    ("R2  보안 경고 P0",
     msg(subject="보안 경고: 의심스러운 로그인이 감지되었습니다",
         frm="no-reply@accounts.google.com"), "security", "P0"),
    # 같은 발신자·같은 제목이라도 대량 발송 헤더가 붙으면 P0가 아닙니다.
    # 신뢰 목록 발신자는 경고와 마케팅을 같은 도메인에서 보냅니다.
    ("R2' 보안 경고 + LU 헤더",
     msg(subject="보안 경고: 의심스러운 로그인",
         frm="no-reply@accounts.google.com", lu=True), "promo", "P5"),
    ("R3  기관 P0",
     msg(subject="종합소득세 신고 안내", frm="noreply@hometax.go.kr"),
     "institution", "P0"),
    ("R4  인프라 P0",
     msg(subject="Project paused", frm="noreply@supabase.com"), "infra", "P0"),
    # 같은 인프라 도메인이 청구서도 보냅니다. 도메인은 그 자체로 신호가 아닙니다.
    ("R4' 인프라 발신 영수증은 P3",
     msg(subject="Your receipt from Vercel", frm="invoice@vercel.com"),
     "receipt", "P3"),
    ("R6  접근 통지 P1",
     msg(subject="새로운 기기에서 로그인되었습니다", frm="noreply@service.example"),
     "access", "P1"),
    ("R7  문의 폼 리드 P1",
     msg(subject="[문의] 제품 도입 관련", body=FORM_BODY, frm="noreply@resend.dev"),
     "lead", "P1"),
    # 제목 말머리만 있고 폼 구조가 없으면 리드가 아닙니다.
    ("R7' 말머리만, 폼 라벨 없음",
     msg(subject="[문의] 안녕하세요", body="안녕하세요 대표님", frm="noreply@resend.dev"),
     None, None),
    ("R8  개인 발신 P2",
     msg(subject="지난번 건 관련해서", frm="someone@naver.com", cats=["personal"]),
     "personal", "P2"),
    # 서브도메인만 잡고 다른 회사 도메인은 잡지 않습니다.
    # navercorp.com 은 자동발송(영수증)이고 naver.com 은 사람이 씁니다.
    ("R8' navercorp 는 개인 발신 아님",
     msg(subject="네이버페이 결제 안내", frm="noreply@navercorp.com", cats=["personal"]),
     "receipt", "P3"),
    ("R9  조치 요구 P2",
     msg(subject="[Action Required] 계정 정보를 확인해 주세요"), "action", "P2"),
    ("R10 영수증 P3", msg(subject="결제 영수증이 발행되었습니다"), "receipt", "P3"),
    ("R11 LU 헤더 P5", msg(subject="9월 신상품 소식", lu=True), "promo", "P5"),
    ("R12 Gmail updates P4",
     msg(subject="서비스 정책 변경", cats=["updates"]), "update", "P4"),
    # B3: 결제 키워드가 사람이 쓴 문장이 아니라 링크 경로에만 있는 경우.
    # URL 경로는 벤더의 라우팅 구조이지 결제 사실의 증거가 아닙니다.
    ("B3  URL 경로의 billing 은 무시",
     msg(subject="Weekly digest",
         body="https://vendor.example/org/1/billing?panel=subscriptionPlan"),
     None, None),
    # B5: 일반 규칙(영수증)이 더 구체적인 신호(마감 있는 조치 요구)를 삼키지 않습니다.
    ("B5  조치 요구가 영수증보다 먼저",
     msg(subject="[조치 필요] 결제 수단을 갱신하세요"), "action", "P2"),
    # 어느 규칙도 걸리지 않으면 판정을 지어내지 않고 Tier 1 로 넘깁니다.
    ("T1  미분류는 None",
     msg(subject="안녕하세요", body="지난주에 말씀드린 건 확인 부탁드립니다"),
     None, None),
]


def main() -> int:
    failed = []
    for name, message, want_cat, want_urg in CASES:
        got = tier0.classify(message, ACCOUNT)
        cat = got["category"] if got else None
        urg = got["urgency"] if got else None
        rule = got["rule"] if got else "(미분류 → Tier 1)"
        ok = cat == want_cat and urg == want_urg
        if not ok:
            failed.append((name, want_cat, want_urg, cat, urg))
        print(f"{'PASS' if ok else 'FAIL'}  {name:28} -> "
              f"{str(cat):12} {str(urg):10} rule={rule}")

    print()
    print(f"{len(CASES) - len(failed)}/{len(CASES)} 통과")
    for name, wc, wu, gc, gu in failed:
        print(f"  FAIL {name}: 기대 {wc}/{wu}, 실제 {gc}/{gu}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
