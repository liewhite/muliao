#!/bin/sh
# 从 Rust 版退回 Python 版（切换后头一两天内用）：用法 deploy/rollback.sh
#
# 自己转到后台运行，输出追加到 $MULIAO_HOME/deploy.log：等 20 秒 → 停服务 → Rust 版的库移进 backups/muliao.rust.db.<时间>
# → 切换时留下的 data/muliao.python.db 改回 data/muliao.db → plist 改为启动 Python 版（uv tool 装的那个仍在）→ 启动、
# 健康检查。切换之后在 Rust 版里新写的会话、issue 等不带回去（留在 backups 里的 Rust 版库中）。
# 最后一行是 ROLLBACK OK 或 ROLLBACK FAILED。
set -u
. "$(dirname "$0")/lib.sh"
detach "$0" "$@"

TS=$(date +%Y%m%d-%H%M%S)
say "=== rollback to Python ($TS) ==="
[ -e "$PYDB" ] || die "$PYDB not found: not cut over, or already rolled back"
[ -x "$PYTHON_MULIAO" ] || die "Python version not found at $PYTHON_MULIAO"
sleep "${MULIAO_DELAY:-20}"
mkdir -p "$BACKUPS"
cp "$PLIST" "$BACKUPS/$LABEL.plist.$TS"
svc_stop || die "could not stop the service; nothing changed"
if restore_python "$TS"; then
  say "ROLLBACK OK: running the Python version"
else
  say "ROLLBACK FAILED: check $LOGFILE and plist $PLIST (backup $BACKUPS/$LABEL.plist.$TS)"
  exit 1
fi
