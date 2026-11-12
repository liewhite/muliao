# deploy/ 下各脚本共用的路径与服务操作（被 . 引入，不单独执行）。
#
# 路径都可以用环境变量改，演练时指到临时目录和别的 launchd label，不碰线上：
#   MULIAO_HOME   配置与数据所在目录（默认 ~/.muliao）：config.toml、data/、muliao.log、backups/、app/
#   MULIAO_LABEL  launchd 服务名（默认 com.muliao）；plist 在 ~/Library/LaunchAgents/<label>.plist（MULIAO_PLIST 可改）
#   MULIAO_PORT   服务端口（默认 1080，须与 config.toml 的 server.port 一致），健康检查用
#   PYTHON_MULIAO Python 版的入口（默认 ~/.local/bin/muliao，uv tool 装的），回滚用
# 约定：config.toml 里 data_dir、log_file 用默认的相对路径（data、muliao.log），所以库在 $DATA/muliao.db。
MULIAO_HOME=${MULIAO_HOME:-$HOME/.muliao}
LABEL=${MULIAO_LABEL:-com.muliao}
PLIST=${MULIAO_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}
PORT=${MULIAO_PORT:-1080}
PYTHON_MULIAO=${PYTHON_MULIAO:-$HOME/.local/bin/muliao}
SVC=gui/$(id -u)/$LABEL
CONFIG=$MULIAO_HOME/config.toml
DATA=$MULIAO_HOME/data
LOGFILE=$MULIAO_HOME/muliao.log
BACKUPS=$MULIAO_HOME/backups
# Rust 版的安装：app/<时间>-<commit>/{muliao,muliao-migrate,web/,COMMIT}，app/current 指向在用的那一版
APP=$MULIAO_HOME/app
# 切换时把 Python 版的库改名留在这里；它在＝已切到 Rust 版、还能回滚
PYDB=$DATA/muliao.python.db

say() { echo "$(date '+%F %T') $*"; }
die() { say "ERROR: $*"; exit 1; }

# 停服务并等它真的停了（bootout 返回时进程可能还在关停，ExitTimeOut 最长 60s；#644）。停不下来返回 1。
svc_stop() {
  launchctl bootout "$SVC" 2>&1 | sed 's/^/  launchctl: /'
  i=0
  while launchctl print "$SVC" >/dev/null 2>&1; do
    i=$((i + 1))
    [ $i -ge 120 ] && { say "service $LABEL still loaded after 120s"; return 1; }
    sleep 1
  done
  say "service $LABEL stopped (${i}s)"
}

# 按 plist 启动（重读 plist）。bootstrap 偶尔报 5: Input/output error，重试。
svc_start() {
  for n in 1 2 3 4 5 6 7 8 9 10; do
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>&1 | sed 's/^/  launchctl: /'
    launchctl print "$SVC" >/dev/null 2>&1 && { say "service $LABEL bootstrapped (try $n)"; return 0; }
    sleep 5
  done
  say "bootstrap of $LABEL failed after 10 tries"
  return 1
}

svc_pid() { launchctl print "$SVC" 2>/dev/null | awk '$1=="pid" {print $3; exit}'; }

# plist 里的启动命令改成 Rust 版或 Python 版（只改 ProgramArguments，其余照旧）。服务停着时改，下次 bootstrap 生效。
set_program() {
  case $1 in
    rust) args="[\"$APP/current/muliao\",\"--config\",\"$CONFIG\",\"--web-dir\",\"$APP/current/web\"]" ;;
    python) args="[\"$PYTHON_MULIAO\",\"--config\",\"$CONFIG\"]" ;;
    *) die "set_program: unknown program $1" ;;
  esac
  plutil -replace ProgramArguments -json "$args" "$PLIST" || return 1
  say "plist now runs: $(plutil -extract ProgramArguments json -o - "$PLIST")"
}

# 启动后的健康检查：90 秒内接口有回应（/api/auth/me 返回 200 或 401），之后 30 秒内进程没换（没在循环重启），
# 日志从 $1 字节起（本次启动前的长度）没有 ERROR。不健康返回 1 并打印原因。
wait_healthy() {
  from=$1
  code=000
  for i in $(seq 1 45); do
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
  size=$(wc -c < "$LOGFILE" | tr -d ' ')
  [ "$size" -ge "$from" ] || from=0
  errors=$(tail -c +"$((from + 1))" "$LOGFILE" | grep ' ERROR ' | head -5 | cut -c1-300)
  [ -z "$errors" ] || { say "unhealthy: ERROR in log since start:"; echo "$errors"; return 1; }
  say "healthy: pid $pid, /api/auth/me $code, no ERROR in log"
}

log_size() { if [ -f "$LOGFILE" ]; then wc -c < "$LOGFILE" | tr -d ' '; else echo 0; fi; }

# 服务停着时，把数据换回 Python 版并启动它：Rust 版的库（若有）移进 backups，muliao.python.db 改回 muliao.db。
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
  set_program python || return 1
  from=$(log_size)
  svc_start || return 1
  wait_healthy "$from"
}

# 自己脱离调用者在后台重跑（停服务会杀掉从服务里起的前台命令）；输出追加到 $MULIAO_HOME/deploy.log。
detach() {
  if [ -z "${MULIAO_DETACHED:-}" ]; then
    mkdir -p "$BACKUPS"
    MULIAO_DETACHED=1 nohup sh "$@" >> "$MULIAO_HOME/deploy.log" 2>&1 < /dev/null &
    echo "running in the background (pid $!); follow: tail -f $MULIAO_HOME/deploy.log"
    exit 0
  fi
}
