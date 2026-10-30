#!/bin/sh
# 部署后的通用自检：用法 deploy/selfcheck.sh <目标 commit>（在仓库里跑；新功能验收不在此，由调用方另做）
#
# 逐项打印 PASS/FAIL 和依据，任一失败退出码 1：
#   1. 重启了：服务进程启动时间晚于安装时间（direct_url.json 的修改时间）
#   2. 已装 commit 包含目标 commit（后来装了更新的 main 也算对）
#   3. 日志里本次启动的 "Application startup complete" 之后没有 ERROR
#   4. HTTP 端口返回 200
#   5. 没有循环重启：launchctl runs 隔 RUNS_WAIT 秒（默认 60）不增长
# 只报告不处理：循环重启时是否 bootout 由调用方决定。
set -u
target=${1:?usage: deploy/selfcheck.sh <target-commit>}
HOME_DIR=${MULIAO_HOME:-$HOME/.muliao}
LOG=${MULIAO_LOG:-$HOME_DIR/muliao.log}
PORT=${MULIAO_PORT:-1080}
RUNS_WAIT=${RUNS_WAIT:-60}
LABEL=gui/$(id -u)/com.muliao
SITE=$HOME/.local/share/uv/tools/muliao/lib/python3.12/site-packages
PY=$HOME/.local/share/uv/tools/muliao/bin/python
GIT=$(command -v /opt/homebrew/bin/git || command -v git)
repo=$(cd "$(dirname "$0")/.." && pwd)
failed=0

pass() { echo "PASS $1: $2"; }
fail() { echo "FAIL $1: $2"; failed=1; }

# 1. 重启晚于安装
direct_url=$(ls "$SITE"/muliao-*.dist-info/direct_url.json 2>/dev/null | head -1)
pid=$(launchctl print "$LABEL" 2>/dev/null | awk '$1=="pid" {print $3; exit}')
if [ -z "$direct_url" ]; then
  fail restarted "找不到 $SITE/muliao-*.dist-info/direct_url.json"
elif [ -z "$pid" ]; then
  fail restarted "launchctl 里 com.muliao 没有 pid（服务没在跑）"
else
  installed_at=$(stat -f %m "$direct_url")
  lstart=$(ps -o lstart= -p "$pid" | sed 's/^ *//')
  started_at=$(LC_ALL=C date -j -f "%a %b %e %T %Y" "$lstart" +%s 2>/dev/null || echo 0)
  info="pid $pid 启动于 $(date -r "$started_at" '+%F %T')，安装于 $(date -r "$installed_at" '+%F %T')"
  if [ "$started_at" -gt "$installed_at" ]; then pass restarted "$info"; else fail restarted "$info"; fi
fi

# 2. 已装 commit 含目标 commit
installed=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['vcs_info']['commit_id'])" "$direct_url" 2>/dev/null)
if [ -z "$installed" ]; then
  fail commit "读不到已装 commit_id"
else
  # 已装的可能是别的会话 push 的更新提交，本地没有就先 fetch（网络会间歇掉线，重试）
  for i in 1 2 3; do
    "$GIT" -C "$repo" cat-file -e "$installed^{commit}" 2>/dev/null && break
    "$GIT" -C "$repo" fetch -q origin 2>/dev/null || sleep 20
  done
  if "$GIT" -C "$repo" merge-base --is-ancestor "$target" "$installed" 2>/dev/null; then
    pass commit "已装 $installed 包含 $target"
  else
    fail commit "已装 $installed 不包含 $target（或本地仓库缺提交）"
  fi
fi

# 3. 本次启动后日志无 ERROR
line=$(grep -n "Application startup complete" "$LOG" | tail -1 | cut -d: -f1)
if [ -z "$line" ]; then
  fail log "$LOG 里没有 Application startup complete"
else
  stamp=$(sed -n "${line}p" "$LOG" | cut -c1-19)
  errors=$(awk -v s="$line" 'NR >= s && / ERROR / {print NR ": " substr($0, 1, 300)}' "$LOG" | head -5)
  if [ -n "${started_at:-}" ] && [ "$started_at" -gt 0 ] &&
    [ "$(date -j -f '%Y-%m-%d %H:%M:%S' "$stamp" +%s 2>/dev/null || echo 0)" -lt "$started_at" ]; then
    fail log "最近一次 startup complete（$stamp，第 $line 行）早于进程启动，服务可能没起来"
  elif [ -n "$errors" ]; then
    fail log "startup complete（$stamp，第 $line 行）之后有 ERROR（前 5 条）：
$errors"
  else
    pass log "startup complete（$stamp，第 $line 行）之后无 ERROR"
  fi
fi

# 4. HTTP 200
code=$(curl -s -o /dev/null -m 10 -w "%{http_code}" "http://127.0.0.1:$PORT/")
if [ "$code" = 200 ]; then pass http "127.0.0.1:$PORT 返回 200"; else fail http "127.0.0.1:$PORT 返回 $code"; fi

# 5. runs 不增长
runs() { launchctl print "$LABEL" 2>/dev/null | awk '$1=="runs" {print $3; exit}'; }
r1=$(runs)
sleep "$RUNS_WAIT"
r2=$(runs)
if [ -n "$r1" ] && [ "$r1" = "$r2" ]; then
  pass runs "runs = $r1，${RUNS_WAIT}s 后不变"
else
  fail runs "runs ${r1:-?} → ${r2:-?}（${RUNS_WAIT}s 内），可能在循环重启"
fi

[ "$failed" = 0 ] && echo "ALL PASS" || echo "SOME FAILED"
exit "$failed"
