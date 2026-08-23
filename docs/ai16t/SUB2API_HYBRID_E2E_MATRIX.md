# M2 真实 Hybrid E2E 矩阵

运行要求：真实 Sub2API Postgres、Redis、HTTP authentication/API；真实 Sub2APIAdapter；pinned Commercial Core；仅 Mock Provider；绑定 `127.0.0.1`；独立 namespace/ports/temp/volumes。所有场景必须动态执行且不得 skip。

| # | 场景 | 核心断言 |
|---:|---|---|
| 1 | Root login | 真实 HTTP 成功且 RBAC 正确 |
| 2 | Admin login | 可访问 admin、不可越权财务写 |
| 3 | User login | 仅用户权限 |
| 4 | valid API Key | fingerprint auth 成功，raw key 不出边界 |
| 5 | revoked API Key | Core 未调用 |
| 6 | disabled user | Core 未调用 |
| 7 | model mapping | public model 映射到 Core model |
| 8 | sticky routing | 同策略稳定，故障时可转移 |
| 9 | primary success | 一次 dispatch/settlement |
| 10 | primary 500 -> fallback | fallback success、客户仅一次 charge |
| 11 | 429 cooldown | cooldown 生效并切换候选 |
| 12 | timeout | reservation 正确 settle/release，不双扣 |
| 13 | both providers fail | charge=0、reservation released |
| 14 | usage | authoritative usage 与 projection 对账 |
| 15 | Ledger | transaction/entries 平衡且 reference 可追踪 |
| 16 | customer charge | integer micro money 精确 |
| 17 | provider cost | cost 由 Core 记录 |
| 18 | margin | 最低利润 dispatch/settlement 双门 |
| 19 | duplicate idempotency | 同 hash 回放，无第二条 Ledger/Usage |
| 20 | concurrent duplicate | 并发仅一个权威结果 |
| 21 | insufficient balance | 不 dispatch、不透支 |
| 22 | concurrent balance | 多请求竞争后余额不为负 |
| 23 | failed request no charge | charge=0 |
| 24 | retry no double charge | transport retry 回放首次结果 |
| 25 | projection drift | 标记 drift，Ledger 不被反向覆盖 |
| 26 | Secret not leaked | DB/Redis/log/UI/error/backup/trace 扫描 0 raw secret |

完成后保存：固定 commit、compose config digest、容器镜像 digest、测试命令/exit/count、DB/Redis 隔离证明、Ledger 对账摘要和 redacted scanner 报告。不得保存密码、Token 或 test secret 原文。
