"""inbox-pilot 진입점 — 계정 루프로 전체 파이프라인을 실행합니다.

    ./venv/bin/python main.py --days 3

파이프라인: health(토큰 점검) → fetch(읽기 전용) → tier0(규칙)
            → tier1(LLM, 미분류분만) → audit.jsonl append
            → out/digest-YYYY-MM-DD.md

ANTHROPIC_API_KEY 는 셸 export 또는 repo 루트의 .env 에서 옵니다
(config.load_env_file 참고 — cron 은 셸 환경을 물려받지 않습니다).

종료 코드: 0 정상 / 2 계정 중 하나 이상이 토큰 점검에 실패 (health.py 참고).
"""

import argparse
import sys

import audit
import config
import digest
import fetch
import health
import tier0
import tier1

EXIT_OK = 0
EXIT_AUTH = 2


def classify_account(
    account: dict,
    messages: list[dict],
    classifier: tier1.Tier1Classifier | None,
    classified: dict[tuple[str, str], dict],
) -> tuple[list[dict], list[dict]]:
    """계정 하나를 분류하고 (다이제스트용 전체, 감사로그에 append 할 신규) 를 반환.

    `classified` 에 이미 판정이 있는 메일은 다시 분류하지 않고 그 판정을 그대로
    씁니다. 30분 주기로 --days 3 을 돌리면 같은 메일이 최대 144번 수집되는데,
    매번 다시 분류하면 감사 로그가 부풀고 Tier1 은 같은 건에 반복 과금됩니다.

    재사용분을 두 번째 리스트에서 빼는 것이 핵심입니다. 다이제스트는 그날의
    전체 그림이어야 하므로 재사용분도 포함해야 하지만, 감사 로그는 "분류가
    일어난 사건"의 기록이므로 새로 판정한 건만 들어가야 합니다.
    """
    results = []
    fresh = []
    tier0_hits = 0
    for message in messages:
        cached = classified.get((message["account_email"], message["message_id"]))
        if cached is not None:
            results.append(
                {**message, **cached, "account_alias": account["alias"], "cached": True}
            )
            continue

        verdict = tier0.classify(message, account)
        if verdict is None:
            if classifier is not None:
                verdict = classifier.classify(message)
            else:
                verdict = {
                    "tier": 1,
                    "category": "other",
                    "urgency": "P2",
                    "confidence": 0.0,
                    "needs_reply": False,
                    "rule": "tier1-skipped",
                }
        else:
            tier0_hits += 1

        record = {**message, **verdict, "account_alias": account["alias"]}
        results.append(record)
        fresh.append(record)

    skipped = len(results) - len(fresh)
    print(
        f"[{account['email']}] 신규 {len(fresh)}건 "
        f"(Tier0 {tier0_hits} / Tier1 {len(fresh) - tier0_hits}) / "
        f"스킵 {skipped}건 (이미 분류됨)"
    )
    return results, fresh


def force_tier1(
    messages: list[dict], limit: int, probe: tier1.Tier1Classifier
) -> list[dict]:
    """Tier0 판정과 무관하게 앞 `limit`건을 Tier1에 태워 실제 응답을 받아봅니다.

    Tier0가 100%를 처리하는 기간에는 자연 발생 미분류가 없어서 Tier1 경로가
    한 번도 실행되지 않습니다. 그러면 API 키·모델명·JSON 스키마·비용 계산·
    토큰 기록이 맞는지 알 수 없으므로, 여기서 강제로 한 번 태워 확인합니다.

    결과는 반환만 하고 실제 분류(results)에는 넣지 않습니다. 감사 로그에는
    test=true 로 남습니다 — 호출은 실제로 발생했고 비용도 실제이기 때문입니다.
    """
    # 캐시에서 꺼낸 메일은 본문이 없습니다(fetch.CACHE_FILE 주석). 빈 본문을
    # 태우면 "Tier1 이 무엇을 보고 답했는지"가 검증이 아니라 착시가 됩니다.
    usable = [m for m in messages if not m.get("from_cache")]
    excluded = len(messages) - len(usable)
    sample = usable[:limit]
    print(f"\n{'=' * 60}")
    print(f"■ Tier1 강제 실행 검증 (--force-tier1, {len(sample)}건)")
    print("  실제 분류에는 반영하지 않습니다. audit 에는 test=true 로 남습니다.")
    if excluded:
        print(f"  캐시에서 꺼낸 {excluded}건은 본문이 없어 제외했습니다.")
    if len(sample) < limit:
        print(f"  참고: 태울 수 있는 메일이 {len(usable)}건이라 {len(sample)}건만 태웁니다.")
    print("=" * 60)

    records = []
    for index, message in enumerate(sample, 1):
        account = config.get_account(message["account_key"])
        baseline = tier0.classify(message, account)
        verdict = probe.classify(message)

        print(f"\n[{index}] {message['account_email']} · {message['message_id']}")
        print(f"    제목  : {message['subject'][:70]}")
        if baseline is None:
            print("    Tier0 : 미분류 (규칙 없음)")
        else:
            print(
                f"    Tier0 : {baseline['urgency']} / {baseline['category']}"
                f"  (rule={baseline['rule']})"
            )
        print(
            f"    Tier1 : {verdict['urgency']} / {verdict['category']}"
            f"  conf={verdict['confidence']:.2f}"
            f"  reply={verdict['needs_reply']}  (rule={verdict['rule']})"
        )
        print(
            f"    사용량: model={verdict.get('model')} "
            f"in {verdict.get('input_tokens', 0):,} / "
            f"out {verdict.get('output_tokens', 0):,} tok · "
            f"${verdict.get('cost_usd', 0.0):.6f} USD"
        )

        records.append(
            {**message, **verdict, "account_alias": account["alias"], "test": True}
        )

    print(f"\n  검증 합계: 호출 {probe.calls}건 / 오류 {probe.errors}건 · "
          f"in {probe.input_tokens:,} / out {probe.output_tokens:,} tok · "
          f"${probe.estimated_cost_usd():.6f} USD")
    return records


def main() -> int:
    parser = argparse.ArgumentParser(
        description="inbox-pilot — Gmail shadow-mode 분류 (읽기 전용)"
    )
    parser.add_argument("--days", type=int, default=3, help="최근 N일 (기본 3)")
    parser.add_argument(
        "--account",
        choices=[a["key"] for a in config.ACCOUNTS],
        help="특정 계정만 실행 (기본: 전체)",
    )
    parser.add_argument(
        "--max-messages", type=int, default=500, help="계정당 최대 수집 건수"
    )
    parser.add_argument(
        "--skip-tier1", action="store_true", help="LLM 분류를 건너뜁니다 (비용 0)"
    )
    parser.add_argument(
        "--force-tier1",
        action="store_true",
        help="Tier0 판정과 무관하게 --limit 건을 Tier1에 태워 실제 API 응답을 "
        "확인합니다 (검증용, 실제 분류에는 미반영)",
    )
    parser.add_argument(
        "--limit", type=int, default=3, help="--force-tier1 로 태울 건수 (기본 3)"
    )
    parser.add_argument(
        "--force-reclassify",
        action="store_true",
        help="이미 분류된 메일도 무시하고 전부 다시 분류합니다 (규칙을 고친 뒤 "
        "재판정할 때. Tier1 대상은 다시 과금되고 감사 로그에 줄이 새로 쌓입니다)",
    )
    args = parser.parse_args()

    if args.force_tier1 and args.skip_tier1:
        raise SystemExit("--force-tier1 과 --skip-tier1 은 같이 쓸 수 없습니다.")
    if args.force_tier1 and args.limit < 1:
        raise SystemExit("--limit 은 1 이상이어야 합니다.")

    accounts = (
        [config.get_account(args.account)] if args.account else list(config.ACCOUNTS)
    )

    # 토큰 점검을 수집보다 먼저 합니다. 죽은 토큰으로 fetch 에 들어가면
    # SystemExit 로 실행 전체가 멈춰서, 멀쩡한 다른 계정까지 분류되지 않습니다.
    # 여기서 갱신까지 끝내므로 뒤따르는 fetch 는 유효한 토큰을 파일에서 읽습니다.
    #
    # report 를 write 보다 먼저 부릅니다. write 가 last_ok_at 을 갱신해 버리면
    # "마지막 정상" 줄이 방금 시각을 가리켜 아무 정보도 주지 않습니다.
    health_records = health.check_all(accounts)
    health.report(health_records)
    health_records = health.write(health_records)
    health_failures = health.failures(health_records)

    # 실패한 계정은 수집에서 빼되, 다이제스트의 계정 목록에는 남깁니다.
    # 목록에서 지우면 "그 계정은 메일이 없었다"와 구분되지 않습니다.
    requested_accounts = accounts
    failed_keys = {r["key"] for r in health_failures}
    accounts = [a for a in accounts if a["key"] not in failed_keys]

    if not accounts:
        # stderr 는 버퍼링되지 않습니다. 먼저 flush 하지 않으면 이 줄이 위의
        # health 보고보다 앞서 찍혀서, 로그만 보면 원인 없이 결론만 남습니다.
        sys.stdout.flush()
        print(
            "\n점검을 통과한 계정이 없어 수집을 건너뜁니다. "
            "위 재승인 명령을 실행한 뒤 다시 돌리세요.",
            file=sys.stderr,
        )

    # Tier1 상태를 한 곳에서 정하고, 마지막 요약에 그대로 출력합니다.
    # "비용 $0.0000" 만으로는 '할 일이 없었다'와 '키가 없어 건너뛰었다'가
    # 구분되지 않습니다 — cron 으로 넘기기 전에 반드시 구분돼야 합니다.
    classifier: tier1.Tier1Classifier | None = None
    if args.skip_tier1:
        tier1_status = "비활성 (--skip-tier1)"
        print("Tier1 생략 (--skip-tier1). 규칙으로 분류되지 않은 메일은 미분류로 남습니다.")
    elif not tier1.Tier1Classifier.available():
        tier1_status = "비활성 (키 없음)"
        print(
            "경고: ANTHROPIC_API_KEY 가 설정되지 않아 Tier1을 건너뜁니다.\n"
            f"      {config.ENV_FILE} 에 ANTHROPIC_API_KEY=... 를 넣거나\n"
            "      export ANTHROPIC_API_KEY=sk-ant-... 후 다시 실행하세요.",
            file=sys.stderr,
        )
    else:
        tier1_status = "활성 (키 감지됨)"
        classifier = tier1.Tier1Classifier()

    if args.force_tier1 and classifier is None:
        raise SystemExit(
            "--force-tier1 은 실제 API 호출이 목적이므로 키가 없으면 의미가 없습니다.\n"
            f"      {config.ENV_FILE} 에 ANTHROPIC_API_KEY=... 를 넣고 다시 실행하세요."
        )

    # 이미 판정이 있는 메일을 건너뛰기 위한 상태. --force-reclassify 는 이
    # 맵을 비워서 모든 메일을 신규로 취급합니다.
    if args.force_reclassify:
        classified: dict[tuple[str, str], dict] = {}
        print("--force-reclassify: 기존 판정을 무시하고 전부 다시 분류합니다.")
    else:
        classified = audit.load_classified()
        print(f"기존 판정 {len(classified)}건 로드 ({config.AUDIT_FILE.name})")

    results: list[dict] = []
    fresh: list[dict] = []
    collected: list[dict] = []
    for account in accounts:
        print(f"\n[{account['email']}] 수집 중 (최근 {args.days}일)...")
        # 이미 판정이 있는 id 는 fetch 단계에서 캐시로 대체합니다. 예전에는
        # 전부 받아 온 뒤 classify_account 에서 버렸는데, 버릴 메일에도
        # messages.get 20유닛을 썼습니다.
        skip_ids = {
            message_id
            for (email, message_id) in classified
            if email == account["email"]
        }
        messages = fetch.fetch_messages(
            account,
            days=args.days,
            max_messages=args.max_messages,
            skip_ids=skip_ids,
        )
        print(f"[{account['email']}] {len(messages)}건 수집")
        collected.extend(messages)
        account_results, account_fresh = classify_account(
            account, messages, classifier, classified
        )
        results.extend(account_results)
        fresh.extend(account_fresh)

    # 검증 호출은 별도 인스턴스로 셉니다. 실제 분류 비용(stats/digest)에
    # 테스트 호출이 섞이면 "이 실행이 분류에 쓴 돈"이 부풀려집니다.
    probe: tier1.Tier1Classifier | None = None
    test_records: list[dict] = []
    if args.force_tier1:
        probe = tier1.Tier1Classifier()
        test_records = force_tier1(collected, args.limit, probe)

    skipped = len(results) - len(fresh)
    stats = {
        "days": args.days,
        "accounts": requested_accounts,
        "health_failures": health_failures,
        "tier0_count": sum(1 for r in results if r["tier"] == 0),
        "tier1_calls": classifier.calls if classifier else 0,
        "input_tokens": classifier.input_tokens if classifier else 0,
        "output_tokens": classifier.output_tokens if classifier else 0,
        "cost_usd": classifier.estimated_cost_usd() if classifier else 0.0,
        "new_count": len(fresh),
        "skipped_count": skipped,
    }

    # 감사 로그에는 신규 판정만 씁니다. 재사용분까지 쓰면 30분마다 같은 줄이
    # 다시 쌓여서, 이 변경이 없애려던 그 부풀림이 그대로 남습니다.
    # 다이제스트에는 재사용분도 넣습니다 — 그날의 전체 그림이어야 하므로
    # "지난 30분에 새로 온 메일"만 남으면 다이제스트가 아니게 됩니다.
    written = audit.append(fresh + test_records)
    path = digest.write(results, stats)

    print(f"\n{'=' * 60}")
    print(f"총 {len(results)}건 — 신규 {len(fresh)}건 / 스킵 {skipped}건 (이미 분류됨)")
    print(f"분류 경로: Tier0 {stats['tier0_count']} / Tier1 호출 {stats['tier1_calls']}")
    print(f"Tier1: {tier1_status}")
    # Tier1 상태 줄과 같은 이유로 찍습니다. VIP 개인 목록은 추적되지 않는
    # 파일에서 오므로, 기기를 옮기거나 clone 하면 조용히 비어 있게 됩니다.
    # "P1 0건"이 '그런 메일이 없었다'인지 '목록이 없었다'인지 구분돼야 합니다.
    print(f"VIP 개인 목록: {config.VIP_P1_SOURCE}")
    if classifier and classifier.errors:
        print(f"Tier1 오류 {classifier.errors}건 (해당 메일은 P2/other 로 표시)")
    print(f"예상 비용: ${stats['cost_usd']:.4f} USD")
    if probe is not None:
        print(
            f"검증 호출: {probe.calls}건 · ${probe.estimated_cost_usd():.6f} USD "
            f"(--force-tier1, 분류 미반영)"
        )
    print(f"감사 로그: {config.AUDIT_FILE} (+{written}줄)")
    print(f"다이제스트: {path}")
    print(f"토큰 상태: {health.HEALTH_FILE}")

    if not health_failures:
        print("=" * 60)
        return EXIT_OK

    # 0 이 아닌 코드로 끝냅니다. run-cron.sh 가 이걸 보고 ■■■ ERROR ■■■ 를
    # 남기므로, 로그를 훑기만 해도 죽은 구간이 눈에 띕니다.
    for record in health_failures:
        print(f"{health.marker(record)} {record['account']} — {record['error']}")
    print(
        f"■■■ 계정 {len(health_failures)}/{len(requested_accounts)}개가 "
        f"토큰 점검에 실패했습니다 (exit={EXIT_AUTH}) ■■■"
    )
    print("=" * 60)
    return EXIT_AUTH


if __name__ == "__main__":
    sys.exit(main())
