# 双平台与 Sub2APIAdapter 架构

## 已确认边界

```text
                  AI16T Commercial Core（代码可复用）
                    /                         \
           NewAPIAdapter                 Sub2APIAdapter
                 |                             |
      PLATFORM_A runtime              PLATFORM_B runtime
      独立 DB/Redis/Ledger             独立 DB/Redis/Ledger
```

“Core 代码可复用”不代表运行时共享。平台 A 与 B 的 Core deployment、Ledger 数据、Postgres、Redis、session、Provider secret、API Key 数据、备份和域名都必须分离。

## PLATFORM_A

- 路线：New API + AI16T Commercial Core，不改变。
- 证据化状态：Commercial Core `56108397...` 为本地 READY；New API Hybrid 报告实际为 `NEW_API_AI16T_HYBRID_V1_PARTIAL`，不可把路线名称误报为完整 READY。
- 域名：`ai16t.com` 保留给平台 A。

## PLATFORM_B

- 控制面：Sub2API v0.1.179。
- 商业核心：独立部署的 AI16T Commercial Core。
- 财务：仅 Core Ledger 可写；Sub2API balance/usage/quota 是 projection。
- Secret：Sub2API 只保存 opaque `secret_ref`；真实 Secret 由 SecretProvider/Vault/KMS/envelope encryption 提供。

## 请求流

```text
Sub2API HTTP auth
  -> keyed API-key fingerprint
  -> Sub2APIAdapter identity/model lookup
  -> Commercial Core Execute(idempotency_key, request_hash)
  -> Core Dynamic Router
  -> Mock Provider（当前阶段）
  -> Core Ledger settle/release/refund
  -> read-only projection to Sub2API
```

适配器不得接受 Sub2API balance、售价、成本或利润作为 Core 的财务输入。Core 返回已结算结果时必须包含 authoritative request ID 和 Ledger reference；失败请求的 customer charge 必须为 0，否则 fail closed。

## Projection drift

Projection 写失败不回滚已经完成的权威 Ledger 交易，也不得触发第二次计费。适配器返回 `ProjectionDrift=true`，监控记录 `PROJECTION_DRIFT`，后续仅从 Ledger 重新投影。

## Secret 数据流

- 入库：只存 secret reference、provider metadata 和轮换版本。
- 解析：仅在 Core dispatch 的最小作用域内由 SecretProvider 取回。
- 出口：禁止出现在 UI、projection、日志、error、repr、backup 明文或测试快照。
- 当前阶段：只允许 synthetic/test secret；生产 Vault/KMS 未通过前不得接真实 Provider。

## Git 与升级

- `upstream` 只读：`https://github.com/Wei-Shaw/sub2api.git`，push URL 禁用。
- `origin` 必须精确指向老板控制的 `ai16t/Sub2API` fork；目标 namespace 不存在或不可见时保持未配置，不得用个人仓库静默替代。
- `bot2/sub2api-ai16t-integration` 是当前集成线；功能开发使用独立 Worktree/branch。
