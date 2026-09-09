"""다이제스트 생성 — out/digest-YYYY-MM-DD.md (통합 1개 파일).

구성
  0. 인증 실패 경고 — 실패한 계정이 있을 때만, 제목 바로 아래
  1. P0/P1 — 계정 통합, 최상단
  2. 조치 필요 (P2)
  3. 답장 대기 (needs_reply) — 경과일 포함
  4. 영수증 / 결제
  5. 계정별 섹션 — 분포 통계 + 나머지 메일
  6. 이번 실행 예상 비용 1줄

2번이 따로 있는 이유: P2는 "내가 무언가 해야 하는 메일"인데, 그중 답장이
아닌 것(마감이 있는 조치 요구)은 needs_reply=False 라 3번에 걸리지 않습니다.
전용 섹션이 없던 동안 P2는 계정별 섹션의 접힌 <details> 안에 들어갔습니다 —
분류를 고쳐 올려놔도 읽는 사람 눈에는 여전히 묻혀 있었습니다.

0번이 맨 위에 있는 이유: 토큰이 죽은 계정은 "메일 0건"으로 보입니다. 경고가
본문 아래에 묻히면 빈 다이제스트를 조용한 하루로 착각하게 됩니다.

메일 본문 전문은 저장하지 않습니다. 제목·발신자·경과일 등 정제 필드만 씁니다.
"""

from collections import Counter
from datetime import date

import config

URGENCY_ORDER = ["P0", "P1", "P2", "P3", "P4", "P5", "Ephemeral"]


def _sort_key(item: dict) -> tuple:
    urgency = item.get("urgency", "P5")
    rank = URGENCY_ORDER.index(urgency) if urgency in URGENCY_ORDER else len(URGENCY_ORDER)
    return (rank, -item.get("confidence", 0.0), item.get("date", ""))


def _clean(text: str, limit: int = 90) -> str:
    """마크다운을 깨뜨리지 않도록 한 줄로 정리하고 길이를 자릅니다."""
    flat = " ".join((text or "").split()).replace("|", "/")
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


def _line(item: dict, show_account: bool = False, show_age: bool = False) -> str:
    parts = []
    if show_account:
        parts.append(f"`{item['account_alias']}`")
    parts.append(f"**{item['urgency']}**")
    parts.append(f"{_clean(item['subject'])}")
    parts.append(f"— {_clean(item['from_email'], 40)}")
    if show_age:
        parts.append(f"(경과 {item['age_days']}일)")
    parts.append(f"[{item['category']} · {item['confidence']:.2f} · T{item['tier']}]")
    return "- " + " ".join(parts)


def _health_block(failures: list[dict]) -> list[str]:
    """인증 실패 경고 블록. 실패가 없으면 빈 리스트."""
    if not failures:
        return []

    lines = [
        "> ## ⛔ 인증 실패 — 이 다이제스트는 불완전합니다",
        ">",
        "> 아래 계정은 토큰 점검에 실패해 **이번 실행에서 수집되지 않았습니다.**",
        "> 메일이 없었던 것이 아니라, 메일을 읽지 못한 것입니다.",
        ">",
    ]
    for record in failures:
        lines.append(f"> - **{record['account']}** (`{record['alias']}`)")
        lines.append(f">   - 사유: `{record['error']}`")
        lines.append(f">   - 마지막 정상: `{record.get('last_ok_at') or '기록 없음'}`")
        if record.get("reauth"):
            lines.append(f">   - 재승인: `{record['reauth']}`")
    lines.append("")
    return lines


def render(results: list[dict], stats: dict) -> str:
    today = date.today().isoformat()
    lines: list[str] = []

    lines.append(f"# Inbox Pilot Digest — {today}")
    lines.append("")
    health_failures = stats.get("health_failures", [])
    lines.extend(_health_block(health_failures))
    # 이번 실행에서 새로 판정한 건과, 이전 판정을 그대로 쓴 건을 구분해 둡니다.
    # 다이제스트는 그날 전체를 담으므로, 이 줄이 없으면 30분마다 같은 문서가
    # 다시 쓰이는 것처럼만 보이고 무엇이 새로 들어왔는지 알 수 없습니다.
    reuse = ""
    if "new_count" in stats:
        reuse = f" · 신규 {stats['new_count']} / 재사용 {stats['skipped_count']}"
    lines.append(
        f"_shadow mode (읽기 전용) · 최근 {stats['days']}일 · "
        f"총 {len(results)}건{reuse} · 계정 {len(stats['accounts'])}개_"
    )
    lines.append("")

    # ── 1. P0/P1 ─────────────────────────────────────────────────────
    urgent = sorted(
        [r for r in results if r["urgency"] in ("P0", "P1")], key=_sort_key
    )
    lines.append(f"## 🔴 즉시 확인 — P0/P1 ({len(urgent)}건)")
    lines.append("")
    if urgent:
        lines.extend(_line(r, show_account=True) for r in urgent)
    else:
        lines.append("- 없음")
    lines.append("")

    # ── 2. 조치 필요 (P2) ─────────────────────────────────────────────
    action = sorted([r for r in results if r["urgency"] == "P2"], key=_sort_key)
    lines.append(f"## ⚡ 조치 필요 — P2 ({len(action)}건)")
    lines.append("")
    if action:
        lines.extend(_line(r, show_account=True, show_age=True) for r in action)
    else:
        lines.append("- 없음")
    lines.append("")

    # ── 3. 답장 대기 ──────────────────────────────────────────────────
    needs_reply = sorted(
        [r for r in results if r["needs_reply"]],
        key=lambda r: (-r["age_days"], _sort_key(r)),
    )
    lines.append(f"## ✉️ 답장 대기 ({len(needs_reply)}건)")
    lines.append("")
    if needs_reply:
        lines.extend(
            _line(r, show_account=True, show_age=True) for r in needs_reply
        )
    else:
        lines.append("- 없음")
    lines.append("")

    # ── 4. 영수증 / 결제 ──────────────────────────────────────────────
    receipts = sorted([r for r in results if r["category"] == "receipt"], key=_sort_key)
    lines.append(f"## 🧾 영수증 / 결제 ({len(receipts)}건)")
    lines.append("")
    if receipts:
        lines.extend(_line(r, show_account=True) for r in receipts)
    else:
        lines.append("- 없음")
    lines.append("")

    # ── 5. 계정별 섹션 ────────────────────────────────────────────────
    lines.append("## 📬 계정별")
    lines.append("")
    failed_keys = {r["key"] for r in health_failures}
    for account in stats["accounts"]:
        items = [r for r in results if r["account_key"] == account["key"]]
        lines.append(f"### {account['email']} — {account['alias']} ({len(items)}건)")
        lines.append("")

        # 인증에 실패한 계정을 "_수집된 메일 없음_" 으로 적으면, 위 경고 블록을
        # 못 본 사람에게는 조용한 하루와 똑같이 보입니다. 여기서도 못을 박습니다.
        if account["key"] in failed_keys:
            lines.append("**⛔ 인증 실패로 수집하지 못했습니다 — 상단 경고 참고.**")
            lines.append("")
            continue

        if not items:
            lines.append("_수집된 메일 없음_")
            lines.append("")
            continue

        urgency_counts = Counter(r["urgency"] for r in items)
        category_counts = Counter(r["category"] for r in items)
        tier_counts = Counter(r["tier"] for r in items)

        lines.append("| 구분 | 분포 |")
        lines.append("| --- | --- |")
        lines.append(
            "| 우선순위 | "
            + ", ".join(
                f"{u} {urgency_counts[u]}" for u in URGENCY_ORDER if urgency_counts[u]
            )
            + " |"
        )
        lines.append(
            "| 카테고리 | "
            + ", ".join(f"{c} {n}" for c, n in category_counts.most_common())
            + " |"
        )
        lines.append(
            f"| 분류 경로 | Tier0(규칙) {tier_counts[0]}, Tier1(LLM) {tier_counts[1]} |"
        )
        lines.append("")

        # 상단 섹션에 이미 나온 건은 계정별 목록에서 제외해 중복을 줄입니다.
        shown = {id(r) for r in urgent + action + needs_reply + receipts}
        rest = sorted([r for r in items if id(r) not in shown], key=_sort_key)
        if rest:
            lines.append(f"<details><summary>나머지 {len(rest)}건</summary>")
            lines.append("")
            lines.extend(_line(r) for r in rest)
            lines.append("")
            lines.append("</details>")
            lines.append("")

    # ── 6. 비용 ──────────────────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append(
        f"**이번 실행 예상 비용: ${stats['cost_usd']:.4f} USD** "
        f"({config.TIER1_MODEL} · Tier1 호출 {stats['tier1_calls']}건 · "
        f"in {stats['input_tokens']:,} / out {stats['output_tokens']:,} tok · "
        f"Tier0 무료 {stats['tier0_count']}건)"
    )
    lines.append("")

    return "\n".join(lines)


def write(results: list[dict], stats: dict) -> str:
    """다이제스트를 파일로 쓰고 경로를 반환합니다."""
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.OUT_DIR / f"digest-{date.today().isoformat()}.md"
    path.write_text(render(results, stats), encoding="utf-8")
    return str(path)
