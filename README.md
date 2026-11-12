# Muliao

个人多 agent 助手：Rust 后端（`server/`，LLM 层用 agenter）的多 agent、多 session 服务 + 可添加到 iOS 主屏并接收推送的 React PWA（`web/`）。

- 架构与设计取舍：[docs/architecture.md](docs/architecture.md)
- 部署与使用：[docs/deployment.md](docs/deployment.md)

```bash
deploy/install.sh   # 编译安装到 ~/.muliao/app/current（要 rustup 的 cargo 和 npm）
mkdir -p ~/.muliao && cp config.example.toml ~/.muliao/config.toml   # 填写密码、密钥、LLM
chmod 600 ~/.muliao/config.toml
~/.muliao/app/current/muliao   # 默认读 ~/.muliao/config.toml，可用 --config 指定
```
