# Platform B 生产硬化门

## P1 财务与一致性

1. Ledger 唯一 Authority；Sub2API balance/usage/quota 仅 projection。
2. 同一 idempotency key + request hash 顺序/并发都只能有一个 authoritative request/settlement。
3. 相同 key 不同 request hash 必须冲突并 fail closed。
4. insufficient balance 不 dispatch；并发 reservation 不透支。
5. timeout、retry、fallback、断线后重试最多结算一次；all-failed/customer-cancelled 未产生成果时扣费 0。
6. projection 按 authoritative request ID 去重；漂移触发 `PROJECTION_DRIFT`，只从 Ledger 修复。

## Secret

- Provider credential 只以 `secret_ref` 存在于 Sub2API。
- Core SecretProvider 支持 get/put/rotate/delete；生产实现使用 Vault/KMS/envelope encryption。
- 数据库、Redis、日志、HTTP error、Admin UI、backup、trace、crash dump 和测试 snapshot 均不得出现明文。
- 当前只允许 test key；生产密钥接入前单独执行 secret lifecycle/rotation/backup restore 审计。

## 动态安全复核

对 v0.1.179 实际代码重新验证：path traversal、OAuth pending exchange、token leakage、XSS、SSRF、privilege escalation、quota bypass、payment callback、URL allowlist、API Key storage。旧报告仅作为线索，不作为永久结论。

## 运行与发布

- `/health` 仅表明进程活着；`/ready` 必须校验 Postgres、Redis、Commercial Core 和必要 migration 状态。
- 监控：status、latency、5xx、DB、Redis、CPU/RAM/disk、billing anomaly、duplicate usage、projection drift、provider failure、cooldown。
- Blue/Green：inactive slot 先过 health/ready/DB/Redis/E2E/security/billing，再显式切流；保留 LAST_KNOWN_GOOD。
- Backup：daily、pre-deploy、pre-migration；没有真实 restore drill 不得标为 PASS。
