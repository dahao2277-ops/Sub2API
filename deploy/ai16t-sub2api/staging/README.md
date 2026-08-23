# SUB2API_STAGING_PREP（Mac1 本地隔离）

该目录建立独立的 `sub2api-ai16t-mac1-staging` Compose 命名空间，不复用 Hybrid Sandbox、平台 A 或 AI 公司 OS 的数据库、Redis、网络、volume、Secret、端口或发布目标。

## 结构

- `sub2api-blue`：`127.0.0.1:18181`，当前/回滚槽。
- `sub2api-green`：`127.0.0.1:18182`，候选槽。
- PostgreSQL、Redis、Commercial Core、Mock Provider：仅独立内部网络。
- `migration-check`：仅在备份恢复验证时临时启动，针对一次性恢复数据库运行真实应用迁移。
- `nginx.active-slot.conf.template`：未来宿主 Nginx 的 loopback reverse-proxy 模板；不包含域名或 TLS 决策。
- `validate_blue_green.py`：在同一进程内启动临时 loopback 验证代理，实际执行 Blue → Green → Blue rollback，结束后不留后台进程。
- `backup_restore_verify.py`：生成 PostgreSQL 与 Core Ledger 本地权限 `0600` 备份，在一次性数据库中恢复、启动真实迁移检查，并再次从不可变备份回滚；绝不替换 live Staging 数据。Core 容器内的临时副本只写入其隔离 `tmpfs`。
- `monitor_staging.py`：只读健康快照，不执行自动升级或切流。

## 执行门

```sh
./prepare_staging_runtime.sh
./check_staging_isolation.sh
docker compose --env-file .runtime/staging.env -f compose.staging.yml up -d --build --wait
python3 validate_blue_green.py
python3 backup_restore_verify.py
python3 monitor_staging.py
```

Staging 的 Sub2API 测试 Header 双门固定关闭。Commercial Core 仍运行在 `isolated-test + mock-only-enabled`，原因是本轮只允许 Mock Provider 和隔离测试额度；该状态不得直接提升为公网 Production。

升级监控只调用仓库现有 `tools/ai16t/check_upstream_status.sh` 并报告 `UPDATE_AVAILABLE`。禁止自动升级 Production。

升级模拟可将只含镜像选择的 Compose override 放在忽略的 `.runtime/` 中，并通过 `AI16T_STAGING_COMPOSE_OVERRIDE` 交给备份恢复脚本。脚本只接受该目录内的现有文件，候选 migration 仍只作用于一次性恢复数据库。
