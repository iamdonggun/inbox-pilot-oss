#!/bin/bash
# launchd 에서 inbox-pilot 을 주기 실행하기 위한 래퍼.
#
# launchd 는 로그인 셸이 아니므로 ~/.zshrc 를 읽지 않습니다. PATH 는 거의
# 비어 있고, export 한 환경변수도 물려받지 못합니다. 그래서 이 스크립트는
#   - PATH 를 직접 정하고
#   - python 을 venv 절대경로로 부르고
#   - API 키는 오직 repo 루트 .env → config.load_env_file() 경로에만 의존합니다.
# 여기서 ANTHROPIC_API_KEY 를 다시 export 하지 않는 것은 의도적입니다.
# 키가 로그·프로세스 목록(ps -E)에 노출되지 않아야 하고, cron 환경에서
# .env 로딩이 실제로 동작하는지가 매 실행마다 검증되어야 하기 때문입니다.

set -u

REPO="/Users/YOUR_USER/workspace/inbox-pilot"
PYTHON="$REPO/venv/bin/python"
LOG="$REPO/out/cron.log"
LOCK="$REPO/out/.cron.lock"
ALERT="$REPO/ALERT.md"
FAILS="$REPO/out/.consecutive-failures"

export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
# 로케일이 없으면 파이썬 stdout 이 ASCII 로 잡혀 한글 출력에서 죽습니다.
export LANG="en_US.UTF-8"
export PYTHONIOENCODING="utf-8"
export PYTHONUNBUFFERED="1"

mkdir -p "$REPO/out"
exec >>"$LOG" 2>&1

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# ── 텔레그램 알림 ─────────────────────────────────────────────────────
# notify.py 를 부르는 얇은 껍데기입니다. 여기서 하는 일은 두 가지뿐:
#
#  1. 실패해도 파이프라인을 죽이지 않습니다. 알림은 부수 효과이지 작업이
#     아닙니다 — 알림이 알림 대상을 망가뜨리면 경보 체계가 고장의 원인이
#     됩니다. 항상 return 0.
#  2. venv 파이썬이 없으면 시스템 파이썬으로 넘어갑니다. "venv 가 사라졌다"
#     는 반드시 알려야 하는 고장인데, 그때 $PYTHON 으로만 알리려 하면
#     정확히 알려야 할 순간에 침묵합니다. notify.py 는 표준 라이브러리만
#     쓰므로 /usr/bin/python3 로도 돕니다.
#
# 토큰은 여기서 export 하지 않습니다. notify.py 가 .env 를 직접 읽습니다
# (ANTHROPIC_API_KEY 와 같은 이유 — 위 헤더 주석 참고).
notify() {
    local py=""
    if [ -x "$PYTHON" ]; then
        py="$PYTHON"
    elif [ -x "/usr/bin/python3" ]; then
        py="/usr/bin/python3"
    else
        echo "[$(ts)] 알림 건너뜀: 실행 가능한 파이썬 없음"
        return 0
    fi
    "$py" "$REPO/notify.py" "$@" || echo "[$(ts)] 알림 전송 실패 — 파이프라인은 계속 진행합니다"
    return 0
}

# ── 실패 에스컬레이션 ─────────────────────────────────────────────────
# 로그의 [AUTH FAILED] 와 exit=2 는 파일 안에서만 소리칩니다 — 2026-08-06
# 부터 9일간 174회 연속 실패를 찍고도 아무도 몰랐습니다. 저장소 루트의
# ALERT.md 는 repo 를 열면 반드시 눈에 걸리는 최소한의 탈출 경로입니다.
# 두 번째 칸은 텔레그램(notify.py)이며, 이쪽은 저장소를 열지 않아도 도달합니다.
#
# 연속 실패 횟수를 out/.consecutive-failures 에 "횟수 첫실패시각" 으로
# 누적합니다. "3회 연속" 같은 정보가 있어야 일시적 네트워크 오류와 진짜
# 고장(토큰 만료·설정 파손)을 구분할 수 있습니다. 정상 실행이 한 번이라도
# 나오면 카운터와 ALERT.md 를 함께 지웁니다 — 지난 실패 이력은 cron.log
# 에 그대로 남습니다.
escalate() {
    # bash 함수의 변수는 기본이 전역입니다. 아래에서 exit "$rc" 가 다시
    # 읽히므로, 이름이 겹치는 rc 를 포함해 전부 local 로 격리합니다.
    local rc="$1"
    local reason="$2"
    local prev_count first_fail count

    prev_count=0
    first_fail="$(ts)"
    if [ -f "$FAILS" ]; then
        # 첫 줄 형식: "<횟수> <첫 실패 시각>" — 시각에 공백이 있으므로
        # 나머지 전체를 first_fail 로 받습니다.
        read -r prev_count first_fail <"$FAILS" || true
        case "$prev_count" in
            ''|*[!0-9]*) prev_count=0; first_fail="$(ts)" ;;
        esac
    fi
    count=$((prev_count + 1))
    printf '%s %s\n' "$count" "$first_fail" >"$FAILS"

    {
        echo "# ⚠️ inbox-pilot 실행 실패"
        echo ""
        echo "- **마지막 실패**: $(ts) (exit=$rc)"
        echo "- **연속 실패**: ${count}회 (첫 실패: $first_fail)"
        echo "- **사유**: $reason"
        echo ""
        echo "## 계정 상태 (out/health.json)"
        echo ""
        if [ -x "$PYTHON" ] && [ -f "$REPO/out/health.json" ]; then
            # health.json 은 main.py 가 토큰 점검 직후 쓰므로 exit=2 실패에서는
            # 항상 최신입니다. 그 전에 죽은 실패(exit=127 등)라면 지난 실행의
            # 상태가 찍히므로 updated_at 을 같이 남깁니다.
            "$PYTHON" - "$REPO/out/health.json" <<'PYEOF' || echo "- health.json 파싱 실패 — out/health.json 을 직접 확인하세요."
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:
    print(f"- health.json 읽기 실패({type(exc).__name__}) — 직접 확인하세요.")
    sys.exit(0)
print(f"- 점검 시각: {data.get('updated_at', '?')}")
for acct in data.get("accounts", []):
    status = acct.get("status", "?")
    line = f"- `{acct.get('account', '?')}` ({acct.get('key', '?')}): **{status}**"
    if status != "ok":
        line += f" — {acct.get('error') or '사유 미기록'} (last_ok: {acct.get('last_ok_at') or '없음'})"
    print(line)
PYEOF
        else
            echo "- health.json 없음/파이썬 불가 — out/cron.log 마지막 실행 블록을 확인하세요."
        fi
        echo ""
        echo "## 조치"
        echo ""
        echo "exit=2(토큰 점검 실패)면 실패한 계정마다 재인증 (사람만 가능):"
        echo ""
        echo '```'
        echo "cd ~/workspace/inbox-pilot && ./venv/bin/python auth.py --account main --force"
        echo "cd ~/workspace/inbox-pilot && ./venv/bin/python auth.py --account dev --force"
        echo '```'
        echo ""
        echo "그 외 exit 코드는 로그 확인: \`tail -60 out/cron.log\`"
        echo ""
        echo "_이 파일은 scripts/run-cron.sh 가 만들었으며, 다음 정상 실행 때 자동 삭제됩니다._"
    } >"$ALERT"

    echo "[$(ts)] ALERT.md 갱신 (연속 ${count}회 실패, exit=$rc)"

    # 상태 전이(정상 → 실패)에서만 폰으로 보냅니다. 2회차부터는 침묵합니다 —
    # 30분마다 같은 알림이 오면 사람이 알림을 끄고, 그러면 9일 침묵과 결과가
    # 같아집니다. 고장이 계속된다는 사실은 ALERT.md 의 연속 횟수가 들고
    # 있으므로 재전송으로 확인시킬 필요가 없습니다.
    if [ "$count" -eq 1 ]; then
        notify fail --rc "$rc" --reason "$reason" --count "$count" \
            --first-fail "$first_fail" --at "$(ts)"
    else
        echo "[$(ts)] 알림 생략: 연속 ${count}회차 (전이 아님 — 1회차에 이미 발송)"
    fi
}

clear_alert() {
    rm -f "$ALERT" "$FAILS"
}

# 실패 → 정상 전이. 카운터 파일이 있을 때만, 즉 직전 실행이 실패했을 때만
# 한 번 보냅니다. 평소 성공에서는 아무것도 나가지 않습니다.
notify_recovery() {
    local prev_count first_fail

    [ -f "$FAILS" ] || return 0

    prev_count=0
    first_fail="?"
    read -r prev_count first_fail <"$FAILS" || true
    case "$prev_count" in
        ''|*[!0-9]*) prev_count="?" ;;
    esac

    notify recover --count "$prev_count" --first-fail "${first_fail:-?}" --at "$(ts)"
}

# ── 중복 실행 방지 ────────────────────────────────────────────────────
# mkdir 은 원자적이라 잠금으로 쓸 수 있습니다(macOS 기본 셸에 flock 없음).
# launchd 자체도 같은 Label 의 동시 실행을 막지만, launchctl kickstart 나
# 수동 실행까지 막아주지는 않으므로 스크립트에서도 한 번 더 막습니다.
if ! mkdir "$LOCK" 2>/dev/null; then
    prev=$(cat "$LOCK/pid" 2>/dev/null || echo "")
    if [ -n "$prev" ] && kill -0 "$prev" 2>/dev/null; then
        echo "[$(ts)] SKIP: 이전 실행이 아직 진행 중입니다 (pid=$prev)"
        exit 0
    fi
    # pid 가 없거나 죽은 프로세스면 죽은 잠금으로 보고 회수합니다.
    echo "[$(ts)] WARN: 죽은 잠금 회수 (pid=${prev:-unknown})"
    rm -rf "$LOCK"
    if ! mkdir "$LOCK" 2>/dev/null; then
        echo "[$(ts)] ERROR: 잠금 획득 실패 — 이번 실행을 건너뜁니다"
        escalate 1 "잠금 획득 실패 — 죽은 잠금 회수 직후 재획득에 실패 (동시 실행 경합 의심)"
        exit 1
    fi
fi
echo $$ >"$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

# ── 실행 ──────────────────────────────────────────────────────────────
# --days 1 이 아니라 3 인 이유: 1일 창에는 안전 마진이 없습니다. 기기가 하루
# 이상 꺼져 있거나 launchd 가 스톨하면(2026-08-05 에 14시간 실제 발생) 그 구간
# 메일은 창 밖으로 밀려나 영원히 분류되지 않습니다. 중복 분류 방지(audit.py)가
# 있으므로 창을 넓혀도 이미 판정된 메일은 재분류·재과금되지 않습니다.
echo ""
echo "================================================================"
echo "[$(ts)] ▶ 시작  main.py --days 3  (전 계정, Tier1 포함)"
echo "================================================================"

cd "$REPO" || {
    echo "[$(ts)] ERROR: cd $REPO 실패"
    escalate 1 "cd $REPO 실패 — 저장소 경로가 사라졌거나 마운트되지 않음"
    exit 1
}

if [ ! -x "$PYTHON" ]; then
    echo "[$(ts)] ERROR: venv 파이썬을 찾을 수 없습니다: $PYTHON"
    escalate 127 "venv 파이썬 없음 ($PYTHON) — venv 재생성 필요"
    exit 127
fi

"$PYTHON" main.py --days 3
rc=$?

if [ "$rc" -eq 0 ]; then
    echo "[$(ts)] ■ 종료 정상  exit=0"
    notify_recovery   # 카운터를 지우기 전에 — 몇 회 실패였는지가 메시지에 필요합니다
    clear_alert
elif [ "$rc" -eq 2 ]; then
    # main.py 가 토큰 점검 실패에만 쓰는 코드입니다. 파이프라인이 깨진 것이
    # 아니라 사람이 재승인해야 하는 상태이므로 사유를 구분해 남깁니다.
    echo "[$(ts)] ■■■ ERROR: 토큰 점검 실패  exit=2  — 위 [AUTH FAILED] 줄과 out/health.json 확인 ■■■"
    escalate 2 "토큰 점검 실패 — OAuth 재인증 필요 (아래 조치 명령 실행)"
else
    echo "[$(ts)] ■■■ ERROR: 실행 실패  exit=$rc  ■■■"
    escalate "$rc" "파이프라인 실행 실패 — out/cron.log 의 이번 실행 블록에서 원인 확인"
fi
exit "$rc"
