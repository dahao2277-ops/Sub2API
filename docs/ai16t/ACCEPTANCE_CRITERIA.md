# 验收标准

## 阶段 1：Adapter foundation

- [x] Sub2APIAdapter 是独立新 package，不修改 upstream 核心实现。
- [x] raw API Key 只在调用栈内生成 keyed HMAC fingerprint，不传给 dependency。
- [x] request hash 由 Adapter 以 identity、credential、Core model 和原始 payload 计算，不信任调用方传入值。
- [x] revoked key、disabled user、missing mapping 在调用 Core 前 fail closed。
- [x] billable success 缺 Ledger reference 时 fail closed。
- [x] failed request 出现非零 charge 时 fail closed。
- [x] projection 失败写入 durable drift gate；gate 不可用或 drift 未清除时后续财务调用 fail closed。
- [ ] 与 pinned Commercial Core 的真实接口完成编译期对接。

## 阶段 2：Hybrid dynamic E2E

- [ ] 在 M2 使用真实 Sub2API Postgres、Redis、HTTP auth/API 和 Sub2APIAdapter。
- [ ] 仅连接 Mock Provider；不使用真实 Provider Secret。
- [ ] `SUB2API_HYBRID_E2E_MATRIX.md` 26 个场景全部通过且不 skip。
- [ ] Ledger、用量、projection 和日志可交叉对账；无 raw secret 泄漏。

## 阶段 3：Staging safety

- [ ] 独立 namespace/DB/Redis/session/storage/backup/monitoring。
- [ ] `/health` 与 `/ready` 分别证明进程健康及 DB/Redis/Core readiness。
- [ ] migration 在 production clone 上验证兼容与 rollback。
- [ ] Blue/Green inactive slot 完成 health/ready/E2E/security/billing 门后才允许切流。
- [ ] pre-deploy/pre-migration/daily backup 完成真实 restore drill。
- [ ] 官方 Advisory + 可复现未缓解 Critical/High = 0。
- [ ] 同一最终 HEAD 的 code/security Reviewer 均为 GO。

## READY 定义

`SUB2API_AI16T_HYBRID_READY` 只有阶段 1 和 2 全部完成才可报告；`SUB2API_STAGING_READY` 还要求阶段 3 全部完成。其后必须停止并报告 `OWNER_DOMAIN_SELECTION_REQUIRED`，不得自动进入公网生产。
