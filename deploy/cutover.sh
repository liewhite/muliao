#!/bin/sh
# 从 Python 版切换到 Rust 版（一次性，#675）：用法 deploy/cutover.sh
#
# 先用 deploy/install.sh 装好 Rust 版（app/current）。本脚本自己转到后台运行（见 lib.sh 的 detach），
# 输出追加到 $MULIAO_HOME/deploy.log：
#   1. 等 20 秒（让调用它的那步工具调用先存下结果）
#   2. 服务停到 hold 上（入口指向 hold.sh 并重启：Python 版正常关停，launchd 拉起的只是在等的 hold.sh）
#   3. 备份库：backups/muliao.db.pre-rust.<时间>（sqlite3 .backup）
#   4. 用 muliao-migrate 从备份迁出新库 data/muliao.rust.db（迁移报告放进 backups/）
#   5. Python 版的库改名为 data/muliao.python.db（回滚用），新库就位为 data/muliao.db
#   6. 入口改指 Rust 版（hold.sh 随即 exec 过去），健康检查（见 lib.sh 的 wait_healthy）
# 第 2 步之后任何一步失败：停回 hold，换回 Python 版的库，放行给 Python 版。最后一行是 CUTOVER OK 或 CUTOVER FAILED。
# 之后要退回 Python 版用 deploy/rollback.sh（切换后 Rust 版新写的数据不带回去）。
set -u
. "$(dirname "$0")/lib.sh"
detach "$0" "$@"

TS=$(date +%Y%m%d-%H%M%S)
NEWDB=$DATA/muliao.rust.db
say "=== cutover to Rust ($TS) ==="

[ -e "$PYDB" ] && die "$PYDB exists: already cut over. To go back use deploy/rollback.sh"
[ -f "$DATA/muliao.db" ] || die "no database at $DATA/muliao.db"
for f in muliao muliao-migrate web/index.html COMMIT; do
  [ -e "$APP/current/$f" ] || die "Rust version not installed ($APP/current/$f missing): run deploy/install.sh first"
done
[ -x "$PYTHON_MULIAO" ] || die "Python version not found at $PYTHON_MULIAO (needed to fall back)"
[ "$(readlink "$ENTRY")" = "$PYTHON_MULIAO" ] || die "service entry $ENTRY is not the Python version's link ($(readlink "$ENTRY"))"
[ -n "$(svc_pid)" ] || die "service $LABEL is not running"
# 备份、新库、改名留着的旧库同时在盘上：要 3 倍库的大小
need=$(($(du -k "$DATA/muliao.db" | cut -f1) * 3))
free=$(df -k "$DATA" | awk 'NR==2 {print $4}')
[ "$free" -gt "$need" ] || die "not enough disk: ${free}KB free, need ${need}KB"
rm -f "$NEWDB"
say "switching to Rust $(cat "$APP/current/COMMIT") ($(readlink "$APP/current"))"

sleep "${MULIAO_DELAY:-20}"
mkdir -p "$BACKUPS"
if ! hold_service; then
  # 入口可能已改指 hold：改回 Python 版（hold.sh 若已在跑会 exec 过去），旧进程还在就什么都没变
  release_to python
  die "could not stop the Python version; entry restored, nothing else changed"
fi

# 从这里起，失败就回到 Python 版
fail() {
  say "cutover failed: $*"
  # 停不住就不能换库（Rust 版可能还在写它）：保持现状，等人处理
  hold_service || { say "CUTOVER FAILED and could not stop the service: database left as is, entry $ENTRY -> $(readlink "$ENTRY")"; exit 1; }
  rm -f "$NEWDB"
  if restore_python "$TS"; then
    say "CUTOVER FAILED; back on the Python version"
  else
    say "CUTOVER FAILED and the Python version did not come back healthy: check $LOGFILE and the entry $ENTRY"
  fi
  exit 1
}

BACKUP=$BACKUPS/muliao.db.pre-rust.$TS
sqlite3 "$DATA/muliao.db" ".backup '$BACKUP'" || fail "backing up the database"
say "backed up the database to $BACKUP"

say "migrating (takes a few minutes)"
"$APP/current/muliao-migrate" "$BACKUP" "$NEWDB" -c "$CONFIG" > "$BACKUPS/migrate.$TS.out" 2>&1
rc=$?
[ -f "$NEWDB.report.md" ] && mv "$NEWDB.report.md" "$BACKUPS/migrate-report.$TS.md"
tail -5 "$BACKUPS/migrate.$TS.out"
[ $rc = 0 ] || fail "muliao-migrate exited $rc (output: $BACKUPS/migrate.$TS.out, report: $BACKUPS/migrate-report.$TS.md)"
say "migrated; report: $BACKUPS/migrate-report.$TS.md"

for s in "" -wal -shm; do
  if [ -e "$DATA/muliao.db$s" ]; then mv "$DATA/muliao.db$s" "$PYDB$s" || fail "moving the Python database aside"; fi
done
mv "$NEWDB" "$DATA/muliao.db" || fail "putting the new database in place"
say "database swapped: Python version's kept as $PYDB"

from=$(log_size)
release_to rust || fail "pointing the entry at the Rust version"
wait_healthy "$from" || fail "the Rust version is not healthy"
say "CUTOVER OK: running Rust $(cat "$APP/current/COMMIT"); roll back with deploy/rollback.sh"
