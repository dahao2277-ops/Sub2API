# Platform B：Mac1 隔离 Hybrid Sandbox

该目录是 Sub2API + AI16T Commercial Core 的 Mac1 隔离执行面。Compose 项目固定为 `sub2api-ai16t-mac1`，包含五个健康检查服务：Sub2API、PostgreSQL、Redis、Commercial Core 和 Mock Provider。唯一宿主机入口是 `127.0.0.1:18080`；数据库、Redis、Core 与 Mock Provider 只存在于独立私网。

边界与状态：

- AI16T Commercial Core 固定到 commit `56108397f31a97926f33d28420e9b44a52547bd2` / tree `6057218ef7291f5d9fb493e8fb7b02fa059ef049`，是唯一 Ledger Authority。
- Redis 只保存 UI/报表投影；投影漂移时财务写入 fail closed，只有 Ledger 驱动的管理员 reconcile 可清除。
- Provider、HMAC 与 fingerprint Secret 只通过 `0600` 文件引用。PostgreSQL 和 Redis 各自只挂载自己的密码文件；完整应用环境只提供给 Sub2API。
- 隔离测试钩子要求 `AI16T_ISOLATED_TEST_MODE=true` 与 `AI16T_ISOLATED_TEST_HOOKS_ENABLED=true` 两道门。Staging/Production 配置不得设置这两个值。
- `/health` 是进程健康；`/ready` 同时验证 Redis 与 Commercial Core，并返回 Ledger Authority。
- 本目录通过本地 Hybrid 验收不等于公网 Staging READY；公网域名、服务器与生产切换仍需老板单独批准。

执行顺序：

1. 确认 Worktree、Fork remotes 与 Commercial Core 固定提交无误后，运行 `tools/ai16t/prepare_mac1_hybrid_runtime.sh`。脚本只生成本机 `.runtime` 文件，不输出 Secret。
2. 运行 `tools/ai16t/check_isolation.sh`，必须得到 `ISOLATION_CHECK=PASS`。
3. 获得 Docker、端口和 Compose 项目独占锁后，使用本目录的 `compose.sandbox.yml` 构建并启动。
4. 先验证五个服务健康和 `/ready`，再运行 `dynamic_e2e.py`。证据只保存在忽略的 `.runtime` 目录并保持 `0600`。
5. 不得复用或修改平台 A 的容器、端口、数据库、Redis、volume、域名或 Secret。
