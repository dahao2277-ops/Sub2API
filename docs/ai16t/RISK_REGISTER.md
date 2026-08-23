# 风险登记册

| ID | 风险 | 当前分类 | 缓解/验收门 |
|---|---|---|---|
| R-01 | Commercial Core 权威源码只在 M2，当前 Mac 无法编译真实组合 | BLOCKER | 在 M2 pinned commit `56108397...` 建独立 Worktree 并执行 Hybrid E2E |
| R-02 | `ai16t/Sub2API` remote 不存在或本账号不可见 | BLOCKER | 老板创建/授权准确 namespace 后再设置 origin；不得替代 |
| R-03 | 同 idempotency key 可能重复 Usage | UNPROVEN | 顺序/并发 duplicate E2E；Ledger 仅一笔，projection 按 authoritative ID 去重 |
| R-04 | timeout/retry billing | UNPROVEN | timeout before/after provider output、client disconnect、retry 场景；不得双扣 |
| R-05 | concurrent balance invariant | UNPROVEN | 并发 reserve/settle，余额不得为负，insufficient balance 不 dispatch |
| R-06 | Provider secret at rest/log/UI/backup 泄漏 | UNPROVEN | SecretProvider + envelope/Vault/KMS；DB/log/UI/backup/scanner 动态验证 |
| R-07 | upstream main 比 v0.1.179 更新 | UPDATE_AVAILABLE | release/advisory/migration diff；不自动升级生产 |
| R-08 | path traversal、OAuth pending exchange、token leakage、XSS、SSRF、privilege escalation、quota bypass、payment callback、URL allowlist | REVALIDATION_REQUIRED | 当前版本动态复现；按官方 Advisory/confirmed reproducible/open-unconfirmed/false-positive 分类 |
| R-09 | Sub2API projection 被误用为财务 Authority | DESIGN_GUARD | Adapter contract 不接收 Sub2API financial input；漂移只从 Ledger 修复 |
| R-10 | migration 破坏旧版本兼容 | UNPROVEN | backup -> DB clone -> migration -> compatibility -> rollback；必要时报告维护窗口 |
| R-11 | AI 项目工厂 CLI schema 24 > supported 4 | TOOLING_BLOCKER | 不修改共享控制面；本轮按同等规则手工编排，后续由 OS 维护任务修复 |
| R-12 | 西班牙语仅为 English fallback + 起步翻译 | PARTIAL_I18N | 已纳入 message compile；后端安全完成后补齐 login/dashboard/key/models/usage/pricing/recharge/subscription/admin 并做母语复核 |
| R-13 | projection 失败后 durable drift 写入也失败，进程内紧急锁无法跨重启保存 | RUNTIME_BLOCKER | 生产接线必须提供与 Ledger request ID 关联的 durable outbox/gate；故障注入验证写入失败、进程重启、reconciliation 和显式 clear，未通过前不得开放真实流量 |

GitHub open issue 本身只能是 `OPEN_REPORT_UNCONFIRMED`，不能直接作为已确认漏洞或永久生产阻塞。
