#!/bin/sh
# 编译并安装 Rust 版 muliao：用法 deploy/install.sh [分支或 tag，默认 main]
#
# 在 $MULIAO_BUILD/src（默认 ~/.muliao/build/src，常驻的仓库副本，保留编译缓存和 node_modules）检出要装的版本，
# release 编译 muliao 与 muliao-migrate、构建前端，用固定证书签名 muliao，放进新目录 $MULIAO_HOME/app/<时间>-<commit>/，
# 再把它复制成 app/current（路径和签名固定，macOS 的文件访问授权才不丢，见 lib.sh 开头）。
# 重启后才用上新程序（前端装完就是新的）。保留最近 3 个版本目录，退回上一版：deploy/activate.sh <上一版目录>，再重启服务。
# 依赖：~/.cargo/bin/cargo（rustup）、~/.cargo/bin/rcodesign（cargo install --locked apple-codesign）、npm（Homebrew）；
# Cargo.lock 与 package-lock.json 锁定版本（--locked / npm ci）。
set -eu
. "$(dirname "$0")/lib.sh"
REF=${1:-main}
REPO=https://github.com/liewhite/muliao.git
BUILD=${MULIAO_BUILD:-$HOME/.muliao/build}
SRC=$BUILD/src
export PATH="$HOME/.cargo/bin:/opt/homebrew/bin:$PATH"
# agenter 是 ssh 地址的 git 依赖：用系统 git + 指定 key 拉（不读 ~/.ssh/config 的 Include，免得卡在 iCloud 文件上）
export CARGO_NET_GIT_FETCH_WITH_CLI=true
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -F /dev/null -i $HOME/.ssh/id_rsa}"
require_signer # 编译前先查，免得编完才发现签不了

[ -d "$SRC/.git" ] || git clone -q "$REPO" "$SRC"
git -C "$SRC" fetch -q origin "$REF"
git -C "$SRC" checkout -q --force --detach FETCH_HEAD
# 清掉上次留下的东西，只留编译缓存与依赖
git -C "$SRC" clean -q -ffdx -e server/target -e web/node_modules
sha=$(git -C "$SRC" rev-parse HEAD)
say "building $REF ($sha)"

(cd "$SRC/server" && cargo build --release --locked -p muliao-server -p muliao-migrate)
(cd "$SRC/web" && npm ci --no-audit --no-fund --loglevel=error && npm run build)

dest=$APP/$(date +%Y%m%d-%H%M%S)-$(echo "$sha" | cut -c1-10)
mkdir -p "$APP"
rm -rf "$dest.tmp"
mkdir "$dest.tmp"
cp "$SRC/server/target/release/muliao" "$SRC/server/target/release/muliao-migrate" "$dest.tmp/"
sign_program "$dest.tmp/muliao" || die "signing $dest.tmp/muliao failed"
cp -R "$SRC/web/dist" "$dest.tmp/web"
echo "$sha" > "$dest.tmp/COMMIT"
mv "$dest.tmp" "$dest"
prev=$(cat "$APP/current/COMMIT" 2>/dev/null || true)
activate "$dest" || die "could not activate $dest"
say "installed $sha into $dest (previous: ${prev:-none}); restart the service to apply"

# 只留最近 3 个版本目录（服务跑的是 current 里的副本，删版本目录不影响它）
ls -1d "$APP"/2*-* 2>/dev/null | sort -r | tail -n +4 | while read -r old; do
  rm -rf "$old"
  say "removed old version $old"
done
