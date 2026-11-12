# deploy/ 下各脚本共用的路径与服务操作（被 . 引入，不单独执行）。
#
# 服务怎么启动：launchd 的 plist 运行「服务入口」$ENTRY（默认 ~/.local/bin/muliao）--config ...。入口是一个链接：
#   Rust 版 → $APP/current/muliao（它自己在安装目录里找前端）；Python 版 → uv tool 装的 $PYTHON_MULIAO。
# 换版本只改这个链接再重启（kickstart -k），不改也不重新加载 plist：从服务里起的进程（agent 的命令、脱离出去的脚本）
# 做 launchctl bootstrap 会报「5: Input/output error」（#706 实测；#644 服务停着起不来也是它），只有 kickstart、kill 可用。
#
# 路径都可以用环境变量改，演练时指到临时目录和别的 launchd label，不碰线上：
#   MULIAO_HOME   配置与数据所在目录（默认 ~/.muliao）：config.toml、data/、muliao.log、backups/、app/
#   MULIAO_LABEL  launchd 服务名（默认 com.muliao）
#   MULIAO_ENTRY  服务入口链接（默认 ~/.local/bin/muliao，须与 plist 的 ProgramArguments 第一项一致）
#   MULIAO_PORT   服务端口（默认 1080，须与 config.toml 的 server.port 一致），健康检查用
#   PYTHON_MULIAO Python 版的程序（默认 uv tool 装的 ~/.local/share/uv/tools/muliao/bin/muliao），回滚用
# 约定：config.toml 里 data_dir、log_file 用默认的相对路径（data、muliao.log），所以库在 $DATA/muliao.db。
MULIAO_HOME=${MULIAO_HOME:-$HOME/.muliao}
LABEL=${MULIAO_LABEL:-com.muliao}
ENTRY=${MULIAO_ENTRY:-$HOME/.local/bin/muliao}
PORT=${MULIAO_PORT:-1080}
PYTHON_MULIAO=${PYTHON_MULIAO:-$HOME/.local/share/uv/tools/muliao/bin/muliao}
SVC=gui/$(id -u)/$LABEL
CONFIG=$MULIAO_HOME/config.toml
DATA=$MULIAO_HOME/data
LOGFILE=$MULIAO_HOME/muliao.log
BACKUPS=$MULIAO_HOME/backups
# Rust 版的安装：app/<时间>-<commit>/{muliao,muliao-migrate,web/,COMMIT}，app/current 指向在用的那一版
APP=$MULIAO_HOME/app
HOLD=$APP/hold.sh
# 切换时把 Python 版的库改名留在这里；它在＝已切到 Rust 版、还能回滚
PYDB=$DATA/muliao.python.db

say() { echo "$(date '+%F %T') $*"; }
die() { say "ERROR: $*"; exit 1; }

svc_pid() { launchctl print "$SVC" 2>/dev/null | awk '$1=="pid" {print $3; exit}'; }

# 入口链接改指 $1（先建临时链接再改名，入口任何时刻都有效）
point_entry() {
  ln -sfn "$1" "$ENTRY.new" && mv -f "$ENTRY.new" "$ENTRY" || return 1
  say "service entry $ENTRY -> $1"
}

# 服务此刻跑的就是报到过的 hold.sh
on_hold() {
  p=$(svc_pid)
  [ -n "$p" ] && [ "$(cat "$HOLD.pid" 2>/dev/null)" = "$p" ]
}

# 让服务停在 hold.sh 上：入口指向它，重启服务（SIGTERM，在跑的程序按自己的关停流程退出，最长 ExitTimeOut），
# 等原来的进程真的退出。之后库可以随便换，launchd 拉起的只是在等的 hold.sh。停不下来返回 1。
hold_service() {
  cp "$(dirname "$0")/hold.sh" "$HOLD.new" && chmod +x "$HOLD.new" && mv -f "$HOLD.new" "$HOLD" || return 1
  old=$(svc_pid)
  rm -f "$HOLD.pid"
  point_entry "$HOLD" || return 1
  launchctl kickstart -k "$SVC" || { say "launchctl kickstart -k $SVC failed"; return 1; }
  # 等旧进程退出、hold.sh 起来报到（launchd 两次启动之间最多隔 ThrottleInterval）
  i=0
  while [ -n "$old" ] && kill -0 "$old" 2>/dev/null || ! on_hold; do
    i=$((i + 1))
    [ $i -ge 150 ] && { say "not on hold after 150s (old process ${old:-none}, pid now $(svc_pid))"; return 1; }
    sleep 1
  done
  say "service on hold (old process ${old:-none} gone, hold.sh is pid $(svc_pid), ${i}s)"
}

# 从 hold 放行到 Rust 版或 Python 版：改入口，hold.sh 一秒内发现并 exec 过去（同一个进程，launchd 不重启）。
release_to() {
  case $1 in
    rust) point_entry "$APP/current/muliao" ;;
    python) point_entry "$PYTHON_MULIAO" ;;
    *) die "release_to: unknown program $1" ;;
  esac
}

log_size() { if [ -f "$LOGFILE" ]; then wc -c < "$LOGFILE" | tr -d ' '; else echo 0; fi; }

# 放行后的健康检查：90 秒内接口有回应（/api/auth/me 返回 200 或 401），之后 30 秒内进程没换（没在循环重启），
# 日志从 $1 字节起（放行前的长度）没有 ERROR。不健康返回 1 并打印原因。
wait_healthy() {
  from=$1
  code=000
  for _ in $(seq 1 45); do
    code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:$PORT/api/auth/me")
    case $code in 200 | 401) break ;; esac
    sleep 2
  done
  case $code in 200 | 401) ;; *) say "unhealthy: /api/auth/me on port $PORT answered $code within 90s"; return 1 ;; esac
  pid=$(svc_pid)
  sleep 30
  pid2=$(svc_pid)
  [ -n "$pid" ] && [ "$pid" = "$pid2" ] || { say "unhealthy: pid changed ${pid:-none} -> ${pid2:-none} (restarting?)"; return 1; }
  # 日志在检查期间轮转过（变短了）就从头看
  size=$(log_size)
  [ "$size" -ge "$from" ] || from=0
  errors=$(tail -c +"$((from + 1))" "$LOGFILE" | grep ' ERROR ' | head -5 | cut -c1-300)
  [ -z "$errors" ] || { say "unhealthy: ERROR in log since start:"; echo "$errors"; return 1; }
  say "healthy: pid $pid, /api/auth/me $code, no ERROR in log"
}

# 服务停在 hold 上时，把数据换回 Python 版并放行给它：Rust 版的库（若有）移进 backups，muliao.python.db 改回 muliao.db。
# 切换失败的自动回退和 deploy/rollback.sh 共用。$1：时间戳（文件名用）。
restore_python() {
  ts=$1
  if [ -e "$PYDB" ]; then
    for s in "" -wal -shm; do
      [ -e "$DATA/muliao.db$s" ] && mv "$DATA/muliao.db$s" "$BACKUPS/muliao.rust.db$s.$ts"
    done
    for s in "" -wal -shm; do
      [ -e "$PYDB$s" ] && mv "$PYDB$s" "$DATA/muliao.db$s"
    done
    say "database: Python version's restored as $DATA/muliao.db; Rust version's kept as $BACKUPS/muliao.rust.db.$ts"
  fi
  from=$(log_size)
  release_to python || return 1
  wait_healthy "$from"
}

# 自己脱离调用者在后台重跑：重启服务时从服务里起的命令会被打断（后台任务的进程组被杀），所以另起会话（setsid）、
# 不挂在调用者的进程组上；输出追加到 $MULIAO_HOME/deploy.log。
detach() {
  if [ -z "${MULIAO_DETACHED:-}" ]; then
    mkdir -p "$BACKUPS"
    MULIAO_DETACHED=1 nohup /usr/bin/perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV or die "exec: $!"' sh "$@" \
      >> "$MULIAO_HOME/deploy.log" 2>&1 < /dev/null &
    echo "running in the background (pid $!); follow: tail -f $MULIAO_HOME/deploy.log"
    exit 0
  fi
}
