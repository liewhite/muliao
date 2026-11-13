# deploy/ 下各脚本共用的路径与服务操作（被 . 引入，不单独执行）。
#
# 服务怎么启动：launchd 的 plist 运行「服务入口」$ENTRY（默认 ~/.local/bin/muliao）--config ...。入口是一个链接，
# 指向 $APP/current/muliao（它自己在程序旁边的 web/ 找前端）；只在停服务换库时临时指向 hold.sh。
# $APP/current 是一个真目录（不是链接），换版本就是把某个版本目录整个复制过来（activate）。为什么：macOS 的文件访问授权
# （隐私与安全性里的「完全磁盘访问」等）按程序的真实路径加签名记，路径或签名一变就要重新授权（#715）。所以程序的路径
# 固定为 $APP/current/muliao，签名用 $SIGNING 下固定的自签证书（sign_program），每次重装都被认成同一个程序。
# 入口链接和 plist 装好就不动，换版本只 activate 再重启（kickstart -k），不重新加载 plist：从服务里起的进程（agent 的命令、脱离出去的脚本）
# 做 launchctl bootstrap 会报「5: Input/output error」（#706 实测；#644 服务停着起不来也是它），只有 kickstart、kill 可用。
#
# 路径都可以用环境变量改，演练时指到临时目录和别的 launchd label，不碰线上：
#   MULIAO_HOME   配置与数据所在目录（默认 ~/.muliao）：config.toml、data/、muliao.log、backups/、app/
#   MULIAO_LABEL  launchd 服务名（默认 com.muliao）
#   MULIAO_ENTRY  服务入口链接（默认 ~/.local/bin/muliao，须与 plist 的 ProgramArguments 第一项一致）
#   MULIAO_PORT   服务端口（默认 1080，须与 config.toml 的 server.port 一致），健康检查用
# 约定：config.toml 里 data_dir、log_file 用默认的相对路径（data、muliao.log），所以库在 $DATA/muliao.db。
MULIAO_HOME=${MULIAO_HOME:-$HOME/.muliao}
LABEL=${MULIAO_LABEL:-com.muliao}
ENTRY=${MULIAO_ENTRY:-$HOME/.local/bin/muliao}
PORT=${MULIAO_PORT:-1080}
SVC=gui/$(id -u)/$LABEL
CONFIG=$MULIAO_HOME/config.toml
DATA=$MULIAO_HOME/data
LOGFILE=$MULIAO_HOME/muliao.log
BACKUPS=$MULIAO_HOME/backups
# Rust 版的安装：app/<时间>-<commit>/{muliao,web/,COMMIT}，app/current 是在用那一版的副本
APP=$MULIAO_HOME/app
# 代码签名证书与私钥（第一次安装时生成，之后不再换：换了就要重新授权）
SIGNING=$MULIAO_HOME/codesign
HOLD=$APP/hold.sh

say() { echo "$(date '+%F %T') $*"; }
die() { say "ERROR: $*"; exit 1; }

svc_pid() { launchctl print "$SVC" 2>/dev/null | awk '$1=="pid" {print $3; exit}'; }

# 入口链接改指 $1（先建临时链接再改名，入口任何时刻都有效）
point_entry() {
  ln -sfn "$1" "$ENTRY.new" && mv -f "$ENTRY.new" "$ENTRY" || return 1
  say "service entry $ENTRY -> $1"
}

# 用固定的自签证书签 $1（第一次调用时生成证书）。签名身份是「标识 muliao + 这张证书」，重编的程序也被 macOS 认成同一个。
# 用 rcodesign（cargo install apple-codesign）直接拿证书文件签：系统的 codesign 只认钥匙串里受信任的证书，而把证书设为
# 受信任要在 Mac 的图形界面上输密码确认，服务里做不了。
sign_program() {
  require_signer
  if [ ! -e "$SIGNING/cert.pem" ]; then
    mkdir -p "$SIGNING" && chmod 700 "$SIGNING" || return 1
    sign_out=$(/opt/homebrew/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 36500 -subj "/CN=muliao code signing" \
      -addext basicConstraints=critical,CA:false -addext keyUsage=critical,digitalSignature \
      -addext extendedKeyUsage=critical,codeSigning \
      -keyout "$SIGNING/key.pem.tmp" -out "$SIGNING/cert.pem.tmp" 2>&1) || { echo "$sign_out"; return 1; }
    mv "$SIGNING/key.pem.tmp" "$SIGNING/key.pem" && mv "$SIGNING/cert.pem.tmp" "$SIGNING/cert.pem" || return 1
    say "created signing certificate in $SIGNING (keep it: a new one means granting file access again)"
  fi
  # 不要时间戳（默认会连 Apple 的时间戳服务器，网络断了安装就失败；本机授权用不到它）；成功时的过程日志不打印
  sign_out=$(rcodesign sign --binary-identifier muliao --timestamp-url none \
    --pem-file "$SIGNING/key.pem" --pem-file "$SIGNING/cert.pem" "$1" 2>&1) || { echo "$sign_out"; return 1; }
}
require_signer() { command -v rcodesign > /dev/null || die "rcodesign not found: cargo install --locked apple-codesign"; }

# 把版本目录 $1 复制成 $APP/current（先复制成 current.new 再换上，current 只在两次改名之间短暂不在）。
# 服务正在跑的程序文件被删掉不影响它；前端从下次请求起就是新的（服务启动时定下的前端目录就是 current/web）。
activate() {
  [ -x "$1/muliao" ] || die "activate: $1/muliao not found"
  rm -rf "$APP/current.new" "$APP/current.old"
  cp -cR "$1" "$APP/current.new" || return 1 # APFS 克隆，不占空间
  if [ -L "$APP/current" ]; then
    rm "$APP/current" # #715 之前 current 是链接
  elif [ -e "$APP/current" ]; then
    mv "$APP/current" "$APP/current.old" || return 1
  fi
  mv "$APP/current.new" "$APP/current" || return 1
  rm -rf "$APP/current.old"
  say "activated $1 as $APP/current ($(cat "$APP/current/COMMIT"))"
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

# 从 hold 放行：入口改回 $APP/current/muliao，hold.sh 一秒内发现并 exec 过去（同一个进程，launchd 不重启）。
release() {
  # 报到只代表「此刻在等的 hold.sh」：exec 过去后 pid 不变，不删的话 on_hold 会把放行后的程序当成还在 hold 上
  rm -f "$HOLD.pid"
  point_entry "$APP/current/muliao"
}

log_size() { if [ -f "$LOGFILE" ]; then wc -c < "$LOGFILE" | tr -d ' '; else echo 0; fi; }

# 放行后的健康检查：90 秒内接口有回应（/api/auth/me 不带 cookie 是 401；200 留给以后改成免登录也认），之后 30 秒内进程没换（没在循环重启），
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

# 自己脱离调用者在后台重跑：重启服务时从服务里起的命令会被打断（后台任务的进程组被杀），所以另起会话（setsid）、
# 不挂在调用者的进程组上；输出追加到 $MULIAO_HOME/deploy.log。
detach() {
  if [ -z "${MULIAO_DETACHED:-}" ]; then
    mkdir -p "$BACKUPS"
    MULIAO_DETACHED=1 nohup /usr/bin/perl -MPOSIX -e 'POSIX::setsid() or die "setsid: $!"; exec @ARGV or die "exec: $!"' sh "$@" \
      >> "$MULIAO_HOME/deploy.log" 2>&1 < /dev/null &
    echo "running in the background (pid $!); follow: tail -f $MULIAO_HOME/deploy.log"
    exit 0
  fi
}
