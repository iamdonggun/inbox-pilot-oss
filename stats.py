"""out/audit.jsonl 사후 집계 — "어느 계정이/어느 카테고리가 비용을 썼나".

    ./venv/bin/python stats.py
    ./venv/bin/python stats.py --by rule --top 10
    ./venv/bin/python stats.py --since 2026-07-30T10:58

audit.jsonl 은 append-only 이므로 비용 필드가 추가되기 전에 쓰인 줄에는 그
필드가 없습니다. 그런 줄은 0으로 처리하되, 총계와 함께 몇 줄이 그런 상태인지
같이 출력합니다 — 0으로 채운 것과 실제로 0원인 것은 다른 사실이기 때문입니다.
"""

import argparse
import collections
import json
import pathlib
import sys
import unicodedata

import config

COST_FIELDS = ("input_tokens", "output_tokens", "cost_usd")


def _width(text: str) -> int:
    """터미널 표시 폭. 한글·CJK 글자는 두 칸을 차지합니다."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _rpad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _lpad(text: str, width: int) -> str:
    return " " * max(0, width - _width(text)) + text


class Bucket:
    """한 그룹(계정·카테고리 등)의 누적치."""

    def __init__(self) -> None:
        self.count = 0
        self.tier0 = 0
        self.tier1 = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        self.llm_calls = 0

    def add(self, row: dict) -> None:
        self.count += 1
        if row.get("tier") == 0:
            self.tier0 += 1
        else:
            self.tier1 += 1
        self.input_tokens += row.get("input_tokens") or 0
        self.output_tokens += row.get("output_tokens") or 0
        self.cost_usd += row.get("cost_usd") or 0.0
        if row.get("model"):
            self.llm_calls += 1


def load(path) -> tuple[list[dict], int]:
    """(줄 목록, 비용 필드가 없는 줄 수)."""
    if not path.exists():
        raise SystemExit(f"감사 로그가 없습니다: {path}")

    rows, legacy = [], 0
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                print(f"경고: {lineno}번째 줄을 건너뜁니다 (JSON 아님)", file=sys.stderr)
                continue
            if not all(field in row for field in COST_FIELDS):
                legacy += 1
            rows.append(row)
    return rows, legacy


def group(rows: list[dict], key: str) -> dict[str, Bucket]:
    buckets: dict[str, Bucket] = collections.defaultdict(Bucket)
    for row in rows:
        buckets[str(row.get(key) or "(없음)")].add(row)
    return buckets


def print_table(title: str, buckets: dict[str, Bucket], top: int | None = None) -> None:
    """비용 내림차순 → 건수 내림차순으로 출력."""
    ordered = sorted(buckets.items(), key=lambda kv: (-kv[1].cost_usd, -kv[1].count))
    if top:
        ordered, hidden = ordered[:top], ordered[top:]
    else:
        hidden = []

    columns = [
        ("건수", 8, lambda b: f"{b.count:,}"),
        ("Tier0", 8, lambda b: f"{b.tier0:,}"),
        ("Tier1", 8, lambda b: f"{b.tier1:,}"),
        ("LLM호출", 9, lambda b: f"{b.llm_calls:,}"),
        ("in_tok", 11, lambda b: f"{b.input_tokens:,}"),
        ("out_tok", 11, lambda b: f"{b.output_tokens:,}"),
        ("cost_usd", 13, lambda b: f"{b.cost_usd:.6f}"),
    ]
    label_width = max([_width(k) for k, _ in ordered] + [_width(title)]) + 2

    print(f"\n■ {title}")
    header = _rpad("", label_width) + "".join(
        _lpad(name, w) for name, w, _ in columns
    )
    print(header)
    print("-" * _width(header))
    for name, b in ordered:
        print(
            _rpad(name, label_width)
            + "".join(_lpad(render(b), w) for _, w, render in columns)
        )
    if hidden:
        rest_count = sum(b.count for _, b in hidden)
        rest_cost = sum(b.cost_usd for _, b in hidden)
        print(
            _rpad(f"… 그 외 {len(hidden)}개", label_width)
            + _lpad(f"{rest_count:,}", 8)
            + _lpad("", 8 + 8 + 9 + 11 + 11)
            + _lpad(f"{rest_cost:.6f}", 13)
        )


def print_total(rows: list[dict], legacy: int, path) -> None:
    total = Bucket()
    for row in rows:
        total.add(row)
    runs = len({row.get("timestamp") for row in rows})

    print(f"{path}")
    print(f"  줄 수        : {total.count:,} ({runs}회 실행)")
    print(f"  비용 필드 有 : {total.count - legacy:,}줄")
    print(f"  비용 필드 無 : {legacy:,}줄  ← 0으로 처리 (필드 추가 이전 기록)")
    print(f"  Tier0 / Tier1: {total.tier0:,} / {total.tier1:,}")
    print(f"  실제 LLM 호출: {total.llm_calls:,}건")
    # test=true 는 --force-tier1 검증 호출입니다. 분류에는 반영되지 않았지만
    # 호출과 비용은 실제이므로 합계에서 빼지 않고, 몇 줄인지만 알립니다.
    tests = sum(1 for row in rows if row.get("test"))
    if tests:
        print(f"  검증 줄       : {tests:,}줄  ← --force-tier1 (분류 미반영, 비용은 실제)")
    print(
        f"  토큰         : in {total.input_tokens:,} / out {total.output_tokens:,}"
    )
    print(f"  총 비용      : ${total.cost_usd:.6f} USD")
    if total.llm_calls:
        print(f"  호출당 평균  : ${total.cost_usd / total.llm_calls:.6f} USD")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="audit.jsonl 계정별·카테고리별 비용/건수 집계"
    )
    parser.add_argument(
        "--by",
        action="append",
        choices=["account", "category", "rule", "model", "tier", "timestamp"],
        help="집계 기준 (반복 지정 가능. 기본: account + category)",
    )
    parser.add_argument("--top", type=int, help="각 표의 상위 N개만 출력")
    parser.add_argument("--since", help="이 timestamp 이후만 (문자열 접두 비교)")
    parser.add_argument(
        "--file", type=pathlib.Path, help=f"집계할 로그 (기본: {config.AUDIT_FILE})"
    )
    args = parser.parse_args()

    path = args.file or config.AUDIT_FILE
    rows, legacy = load(path)
    if args.since:
        before = len(rows)
        rows = [r for r in rows if str(r.get("timestamp", "")) >= args.since]
        legacy = sum(
            1 for r in rows if not all(f in r for f in COST_FIELDS)
        )
        print(f"--since {args.since}: {before:,}줄 중 {len(rows):,}줄 선택\n")
    if not rows:
        raise SystemExit("집계할 줄이 없습니다.")

    print_total(rows, legacy, path)
    for key in args.by or ["account", "category"]:
        print_table(key, group(rows, key), top=args.top)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
