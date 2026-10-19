# Muliao

个人多 agent 助手：pydantic-ai 驱动的多 agent、多 session 后端 + 可添加到 iOS 主屏并接收推送的 React PWA。

- 架构与设计取舍：[docs/architecture.md](docs/architecture.md)
- 部署与使用：[docs/deployment.md](docs/deployment.md)

```bash
uv tool install git+https://github.com/liewhite/muliao.git
cp .env.example .env   # 填写密码、密钥、模型 API key
muliao
```
