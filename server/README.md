# muliao 后端

由 Python 版重写而来（#675，Python 版已随 #717 删除）。LLM 层用 agenter（liewhite/agenter）。

## 结构
Cargo workspace，一个领域一个 crate（`crates/<领域>`，包名 `muliao-<领域>`），成员按目录收：加领域只加目录。

| 层 | crate |
|---|---|
| 0 | `platform`：配置、数据库、路径身份、vault、起命令与写沙箱、SSE 广播、日志（不认识领域词汇） |
| 1 | `assets`、`files`、`usage`、`web`、`speech` |
| 2 | `runtime` |
| 3 | `sessions` |
| 4 | `workspace`、`notifications`、`issues` |
| 5 | `triggers` |
| 6 | `history` |
| 适配 | `http`（接口与 SSE）、`muliao`（组合根，二进制 `muliao`） |

规则（docs/architecture.md 2.3）：
- 只依赖更早一层的 crate；同层、反向需要的调用由组合根注入函数。由 `crates/muliao/tests/layers.rs` 检查（层表在那里，新领域先在那里定层）。
- 路径的身份只经 `platform::fs::real` 算，「在不在某目录下」、写沙箱规则只收它给的 `RealPath`。
- 每个领域独占自己的表，迁移是自己 crate 里的一串 SQL（`platform::db::Migration`，按领域记版本），领域之间只用 id 引用，没有跨领域外键。
- 写事务经 `platform::db::Database::write`：同步闭包，在单写者线程上串行执行，里面不能 await。
- agent 的命令只经 `platform::shell` 起；以哪个 agent 的身份（secret、可写路径）由工作区领域拼好交进来。

## 开发
```
cd server
cargo test --workspace          # 含对本机线上配置与 secret 的只读检查（没有 ~/.muliao/config.toml 时跳过）
cargo clippy --workspace --all-targets && cargo fmt --check
```
