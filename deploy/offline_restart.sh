#!/bin/sh
# 停服务 → （可选）在服务停着时执行一条命令（如恢复库备份）→ 再启动服务。用法：deploy/offline_restart.sh ['<停着时执行的命令>']
# 立即返回；自己脱离到后台（见 lib.sh 的 detach，服务重启不会连带杀掉），日志追加到 $MULIAO_HOME/deploy.log。
#
# 用 lib.sh 的 hold_service / release（#706）：服务入口临时指向只会等待的 hold.sh，kickstart -k 让旧进程按关停流程退出，
# launchd 拉起的只是 hold.sh；命令跑完再把入口指回程序，hold.sh 原地 exec 过去。
# 不用 launchctl bootout/bootstrap：从服务里起的进程做 bootstrap 一律报「5: Input/output error」，服务就停着起不来。
set -u
if [ -z "${OFFLINE_STAGED:-}" ]; then
  # 先把自己和 lib.sh、hold.sh 拷进临时目录再从那里执行：等待和停服务期间仓库被 git pull 改了也不受影响
  here=$(dirname "$0")
  stage=$(mktemp -d /tmp/offline_restart.XXXXXX)
  cp "$here/lib.sh" "$here/hold.sh" "$0" "$stage/" || exit 2
  OFFLINE_STAGED=1 exec sh "$stage/$(basename "$0")" "$@"
fi
. "$(dirname "$0")/lib.sh"
detach "$0" "$@"

CMD=${1:-}
sleep "${MULIAO_DELAY:-20}"
say "offline_restart: hold, run command, then release"
if ! hold_service; then
  # hold_service 先改入口再 kickstart：停不下来时把入口指回去，免得下次 launchd 拉起的是只会等待的 hold.sh
  release
  rm -rf "$(dirname "$0")"
  die "service did not reach hold; entry pointed back, command not run"
fi
if [ -n "$CMD" ]; then
  sh -c "$CMD"
  say "command rc=$?: $CMD"
fi
from=$(log_size)
release || die "release failed: service stays on hold.sh; point $ENTRY back to $APP/current/muliao by hand"
wait_healthy "$from" || say "WARNING: not healthy after restart (see above)"
rm -rf "$(dirname "$0")"
