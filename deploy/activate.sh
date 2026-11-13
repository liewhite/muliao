#!/bin/sh
# 换用某个已装的版本（退回上一版）：用法 deploy/activate.sh <版本目录，如 ~/.muliao/app/<时间>-<commit>>
# 把它复制成 app/current（见 lib.sh 的 activate），重启服务后生效：launchctl kickstart -k gui/$(id -u)/com.muliao
# #715 之前装的版本没有用固定证书签名，退回到它要在「隐私与安全性」里重新授权。
set -eu
. "$(dirname "$0")/lib.sh"
activate "${1:?usage: deploy/activate.sh <version dir>}"
