# Muliao

个人多 agent 助手：pydantic-ai 驱动的多 agent、多 session 后端 + 可添加到 iOS 主屏并接收推送的 React PWA。

- 架构与设计取舍：[docs/architecture.md](docs/architecture.md)
- 部署与使用：[docs/deployment.md](docs/deployment.md)

```bash
uv tool install git+https://github.com/liewhite/muliao.git
mkdir -p ~/.muliao && cp config.example.toml ~/.muliao/config.toml   # 填写密码、密钥、LLM
chmod 600 ~/.muliao/config.toml
muliao   # 默认读 ~/.muliao/config.toml，可用 --config 指定
```
