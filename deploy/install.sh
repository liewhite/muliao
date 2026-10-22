#!/bin/sh
# 按仓库里的 uv.lock 安装或升级 muliao：用法 deploy/install.sh [分支或 tag，默认 main；不接受 commit sha]
#
# uv tool install 不读 uv.lock，会重新解析依赖：上游发布不兼容的大版本时（如 anthropic 2.0 与 pydantic-ai），
# 线上装到的就是测试从没跑过的组合。这里把锁文件导出为约束，让安装端与测试用的版本一致。
# --locked：锁与 pyproject.toml 不一致（改了依赖没更新锁）时直接失败，而不是导出过期的约束
set -eu
REPO=https://github.com/liewhite/muliao.git
src=$(mktemp -d)
trap 'rm -rf "$src"' EXIT
git clone -q --depth 1 --branch "${1:-main}" "$REPO" "$src"
sha=$(git -C "$src" rev-parse HEAD)
uv export --project "$src" --locked --no-dev --no-hashes --no-emit-project -q -o "$src/constraints.txt"
# 在仓库目录外安装，uv 不会自己读 .python-version，显式传入
uv tool install --force --python "$(cat "$src/.python-version")" --constraints "$src/constraints.txt" "git+$REPO@$sha"
echo "installed muliao $sha with locked dependencies; restart the service to apply"
