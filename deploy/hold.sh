#!/bin/sh
# 切换、回滚期间服务入口（链接）暂时指向这里：launchd 拉起的是它，它什么也不做，等入口改指别处（Rust 版或 Python 版）
# 再 exec 过去。这样不用重新加载 launchd 的 plist（从服务里起的进程 launchctl bootstrap 会失败，#706），服务也不会在
# 换库期间被 KeepAlive 拉起旧程序。用法见 deploy/lib.sh 的 hold_service / release_to。
me=$(readlink "$0")
while [ "$(readlink "$0")" = "$me" ]; do sleep 1; done
exec "$0" "$@"
