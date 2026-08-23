# M2 真实 Hybrid E2E 矩阵

状态：`BLOCKED_ON_LOCAL_COMMERCIAL_CORE_SOURCE`。

运行要求：真实 Sub2API Postgres、Redis、HTTP authentication/API；真实 Sub2APIAdapter；pinned Commercial Core `56108397...`；仅 Mock Provider；绑定 `127.0.0.1`；独立 namespace/ports/temp/volumes。所有场景必须动态执行且不得 skip。

| # | 场景 | 核心断言 | 当前状态 |
|---:|---|---|---|
| 1 | Root login | 真实 HTTP 成功且 RBAC 正确 | Pending |
| 2 | Admin login | 可访问 admin、不可越权财务写 | Pending |
| 3 | User login | 仅用户权限 | Pending |
| 4 | valid API Key | fingerprint auth 成功，raw key 不出边界 | Pending |
| 5 | revoked API Key | Core 未调用 | Pending |
| 6 | disabled user | Core 未调用 | Pending |
| 7 | model mapping | public model 映射到 Core model | Pending |
| 8 | sticky routing | 同策略稳定，故障时可转移 | Pending |
| 9 | primary success | 一次 dispatch/settlement | Pending |
| 10 | primary 500 -> fallback | fallback success、客户仅一次 charge | Pending |
| 11 | 429 cooldown | cooldown 生效并切换候选 | Pending |
| 12 | timeout | reservation 正确 settle/release，不双扣 | Pending |
| 13 | both providers fail | charge=0、reservation released | Pending |
| 14 | usage | authoritative usage 与 projection 对账 | Pending |
| 15 | Ledger | transaction/entries 平衡且 reference 可追踪 | Pending |
| 16 | customer charge | integer micro money 精确 | Pending |
| 17 | provider cost | cost 由 Core 记录 | Pending |
| 18 | margin | 最低利润 dispatch/settlement 双门 | Pending |
| 19 | duplicate idempotency | 同 hash 回放，无第二条 Ledger/Usage | Pending |
| 20 | concurrent duplicate | 并发仅一个权威结果 | Pending |
| 21 | insufficient balance | 不 dispatch、不透支 | Pending |
| 22 | concurrent balance | 多请求竞争后余额不为负 | Pending |
| 23 | failed request no charge | charge=0 | Pending |
| 24 | retry no double charge | transport retry 回放首次结果 | Pending |
| 25 | projection drift | 标记 drift，Ledger 不被反向覆盖 | Pending |
| 26 | Secret not leaked | DB/Redis/log/UI/error/backup/trace 扫描 0 raw secret | Pending |

阻塞原因：当前 Mac 没有分支 `integration/ai16t-commercial-core-v1-remediated`、commit `56108397f31a97926f33d28420e9b44a52547bd2` 的源码或可达隔离服务。合同可在本机编译，但不可把它当作真实 runtime wiring。

完成后保存：固定 commit、Compose config digest、容器镜像 digest、测试命令/exit/count、DB/Redis 隔离证明、Ledger 对账摘要和 redacted scanner 报告。不得保存密码、Token 或 test secret 原文。
