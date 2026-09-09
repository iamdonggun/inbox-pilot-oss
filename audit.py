"""감사 로그 — 분류 1건당 1줄씩 out/audit.jsonl 에 append.

기록 필드는 {account, message_id, tier, category, urgency, needs_reply, rule,
model, input_tokens, output_tokens, cost_usd, confidence, test, timestamp} 뿐입니다.
제목·본문 등 메일 내용과 토큰·크리덴셜 값은 절대 남기지 않습니다.
("토큰"이 두 뜻으로 쓰입니다 — 여기 기록하는 것은 LLM 사용량 카운트이고,
 절대 남기지 않는 것은 OAuth 액세스 토큰 문자열입니다.)

비용 필드는 건별로 남깁니다. 실행 총액은 stdout으로만 나가고 사라지므로,
"어느 계정이/어느 카테고리가 비용을 썼나"는 로그만으로 답할 수 있어야 합니다.
집계는 stats.py 가 합니다.

`rule` 은 어느 규칙이 이 판정을 냈는지입니다. 규칙 기반 분류기는 예외를 던지지
않고 틀리기 때문에, 이 필드가 없으면 오분류를 사후에 추적할 방법이 없습니다.
(실제로 supabase 오분류를 조사할 때 이 로그로는 원인을 못 찾아, 매 실행마다
덮어써지는 다이제스트를 grep해야 했습니다.)

  주의: infra-domain:<도메인> 형태의 값에는 발신 도메인이 들어갑니다. 값 자체는
  이미 config.INFRA_DOMAINS 에 공개돼 있고 out/ 는 .gitignore 대상이지만,
  발신자 주소·제목은 여기에 절대 넣지 마십시오.

`test` 는 --force-tier1 검증 호출로 만들어진 줄입니다. 실제 분류에는 반영되지
않았지만 API 는 실제로 호출됐으므로 토큰과 비용은 진짜입니다. 비용 집계에서
빼면 지출이 실제보다 적게 보이므로, 빼지 말고 표시만 합니다.

이 파일은 기록만이 아니라 **중복 분류를 막는 상태 저장소**이기도 합니다
(load_classified 참고). 30분 주기로 --days 3 을 돌리면 같은 메일이 창에서
밀려날 때까지 최대 144번 재수집되므로, 이미 판정이 있는 메일은 다시 분류하지
않고 여기 적힌 판정을
재사용합니다. 그래서 urgency/needs_reply 도 기록합니다 — 이 둘이 없으면
판정을 복원할 수 없어 다이제스트를 다시 만들 수 없습니다.
"""

import json
from datetime import datetime, timezone

import config


def _rule_field(record: dict) -> str | None:
    """줄에 남길 규칙 식별자.

    Tier0        → 규칙 식별자 그대로 (예: infra-domain:supabase.com)
    Tier1 성공    → 모델명 (llm: 접두사를 뗍니다)
    미분류        → None. --skip-tier1 이나 API 키 부재로 아직 아무 규칙도
                   판정하지 않은 상태입니다.
    Tier1 실패    → fallback:<이유> 를 유지합니다. null 로 뭉개면 실패 원인이
                   사라져서, 이 필드를 넣은 이유가 무의미해집니다.
    """
    rule = record.get("rule")
    if not rule or rule == "tier1-skipped":
        return None
    if rule.startswith("llm:"):
        return rule.split(":", 1)[1]
    return rule


def _reusable(record: dict) -> bool:
    """이 줄을 '이미 분류함'으로 인정할지.

    인정하지 않는(= 다음 실행에서 다시 분류할) 경우는 셋뿐입니다.

    1. rule 이 없음 — "tier1-skipped" 를 _rule_field 가 None 으로 적은 줄입니다.
       --skip-tier1 이었거나 API 키가 없어서 **아직 아무도 판정하지 않은** 상태이지,
       판정 결과가 아닙니다. 이걸 캐시로 인정하면 키를 넣은 뒤에도 그 메일은
       영원히 미분류로 굳습니다. (구버전 줄에는 rule 필드 자체가 없습니다 —
       .get 이 None 을 주므로 같은 가지에서 걸립니다.)
    2. urgency/needs_reply 가 없음 — 이 두 필드가 생기기 전의 구버전 줄입니다.
       판정을 복원할 수 없으니 한 번만 다시 분류해 새 스키마로 채웁니다.
    3. rule 이 fallback:api-error — 호출이 서버에 닿지 못한 경우입니다. 토큰이
       과금되지 않았고 원인(네트워크·키)도 일시적이므로 다음 실행에서 재시도합니다.

    반대로 fallback:refusal / truncated / unparseable 은 재시도하지 않습니다.
    이미 토큰을 태웠고 원인이 메일 내용에 있어서, 30분마다 다시 태워도 같은
    결과가 나오면서 돈만 나갑니다. 사람이 보라고 P2/other 로 남겨둡니다.
    """
    rule = record.get("rule")
    if not rule:
        return False
    if rule.startswith("fallback:api-error"):
        return False
    return "urgency" in record and "needs_reply" in record


def load_classified(path=None) -> dict[tuple[str, str], dict]:
    """audit.jsonl 에서 (계정, message_id) → 재사용할 판정 을 읽어옵니다.

    키를 message_id 단독이 아니라 (계정, message_id) 로 잡는 이유: Gmail 의
    message id 는 메일함 단위로 발급됩니다. 계정이 다르면 같은 id 라도 다른
    메일이므로, id 만으로 묶으면 한쪽 계정의 판정이 다른 계정에 새어 들어갑니다.

    test:true 줄은 제외합니다. --force-tier1 검증 호출은 실제 분류에 반영되지
    않은 결과라서, 이걸 캐시로 쓰면 검증 한 번이 진짜 판정을 덮어씁니다.

    같은 키가 여러 줄이면 마지막 줄이 이깁니다(파일이 시간순 append 이므로
    가장 최근 판정). 깨진 줄은 조용히 건너뜁니다 — append 도중 죽어서 생긴
    잘린 마지막 줄 하나 때문에 전체 실행이 멈추면 안 됩니다.
    """
    path = path or config.AUDIT_FILE
    if not path.exists():
        return {}

    classified: dict[tuple[str, str], dict] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("test"):
                continue
            try:
                key = (record["account"], record["message_id"])
            except KeyError:
                continue
            if _reusable(record):
                classified[key] = {
                    "tier": record["tier"],
                    "category": record["category"],
                    "urgency": record["urgency"],
                    "needs_reply": record["needs_reply"],
                    "confidence": record.get("confidence", 0.0),
                    "rule": record["rule"],
                    "model": record.get("model"),
                    "input_tokens": record.get("input_tokens", 0),
                    "output_tokens": record.get("output_tokens", 0),
                    "cost_usd": record.get("cost_usd", 0.0),
                }
            else:
                # 재사용 불가 판정이 이전의 쓸 만한 판정을 덮어써야 합니다.
                # 그래야 "마지막 줄이 이긴다"가 실제로 지켜집니다.
                classified.pop(key, None)

    return classified


def append(records: list[dict]) -> int:
    """분류 결과를 audit.jsonl 에 append 하고 기록한 줄 수를 반환합니다."""
    if not records:
        return 0

    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()

    with config.AUDIT_FILE.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    {
                        "account": record["account_email"],
                        "message_id": record["message_id"],
                        "tier": record["tier"],
                        "category": record["category"],
                        # urgency/needs_reply 는 다이제스트를 만드는 데 필요한
                        # 판정 필드입니다. 재사용 경로가 이 둘을 읽습니다.
                        "urgency": record["urgency"],
                        "needs_reply": bool(record["needs_reply"]),
                        "rule": _rule_field(record),
                        # Tier0 판정에는 이 키들이 아예 없습니다. 규칙 분류기가
                        # LLM 개념을 알 필요는 없으므로 여기서 0/None 을 채웁니다.
                        "model": record.get("model"),
                        "input_tokens": record.get("input_tokens", 0),
                        "output_tokens": record.get("output_tokens", 0),
                        "cost_usd": round(record.get("cost_usd", 0.0), 8),
                        "confidence": record["confidence"],
                        # --force-tier1 검증 호출로 만들어진 줄인지.
                        "test": bool(record.get("test", False)),
                        "timestamp": timestamp,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    return len(records)
