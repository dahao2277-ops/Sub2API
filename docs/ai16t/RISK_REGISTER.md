# 风险登记册

| ID | 风险 | 当前分类 | 缓解/验收门 |
|---|---|---|---|
| R-01 | Commercial Core 权威源码版本漂移 | MITIGATED | Core 独立 Worktree 已基于 pinned base 加固；部署镜像固定 commit + tree，完整测试后才切换 |
| R-02 | 正式组织 Fork 不可用 | ACCEPTED_OWNER_DECISION | 使用老板批准的 `dahao2277-ops/Sub2API` origin；保留 upstream remote 和同步门 |
| R-03 | 同 idempotency key 可能重复 Usage | TESTED_MOCK_PENDING_REAL_CANARY | Core/Hybrid 顺序与并发测试通过；真实上游逐笔对账未完成前不得开放客户 |
| R-04 | timeout/retry billing | TESTED_MOCK_PENDING_REAL_CANARY | Core 故障场景通过；APIYI 超时/429/5xx 小额验证和上游日志对账仍为发布门 |
| R-05 | concurrent balance invariant | TESTED_MOCK_PENDING_REAL_CANARY | Core 并发/余额测试通过；客户并发上限和真实 Canary 仍需验证 |
| R-06 | Provider secret at rest/log/UI/backup 泄漏 | HARDENED_PENDING_DEPLOY_SCAN | AES-256-GCM SecretProvider、mode-0600 Unix socket、密文备份恢复和动态扫描；生产落地后复核 |
| R-07 | upstream main 比 v0.1.179 更新 | UPDATE_AVAILABLE | release/advisory/migration diff；不自动升级生产 |
| R-08 | path traversal、OAuth pending exchange、token leakage、XSS、SSRF、privilege escalation、quota bypass、payment callback、URL allowlist | REVALIDATION_REQUIRED | 当前版本动态复现；按官方 Advisory/confirmed reproducible/open-unconfirmed/false-positive 分类 |
| R-09 | Sub2API projection 被误用为财务 Authority | DESIGN_GUARD | Adapter contract 不接收 Sub2API financial input；漂移只从 Ledger 修复 |
| R-10 | migration 破坏旧版本兼容 | UNPROVEN | backup -> DB clone -> migration -> compatibility -> rollback；必要时报告维护窗口 |
| R-11 | AI 项目工厂 CLI schema 24 > supported 4 | TOOLING_BLOCKER | 不修改共享控制面；本轮按同等规则手工编排，后续由 OS 维护任务修复 |
| R-12 | 西班牙语仅为 English fallback + 起步翻译 | PARTIAL_I18N | 已纳入 message compile；后端安全完成后补齐 login/dashboard/key/models/usage/pricing/recharge/subscription/admin 并做母语复核 |
| R-13 | projection 失败后 durable drift 写入也失败，进程内紧急锁无法跨重启保存 | MITIGATED_PENDING_DEPLOY | 共享文件 durable gate + Redis reconciliation 已通过重启/失败测试；生产挂载与故障注入仍需复核 |
| R-14 | APIYI Streaming 在 Ledger 结算前被缓冲，非实时首 Token | KNOWN_CANARY_LIMIT | 安全优先；只可报告 SSE 兼容，未证明实时首 Token 前不得宣称完整 Streaming 就绪 |
| R-15 | APIYI 实时价格、模型能力和分组倍率未知 | RELEASE_BLOCKER | Key A 拉取模型后逐模型核价并保存快照；未知价格模型 `enabled=false` |
| R-16 | APIYI Key 额度/IP/模型白名单配置错误 | RELEASE_BLOCKER | Key A US$10/7天；Key B US$20/30天、VPS IP 和已验证模型白名单；外部 IP 负向验证 |

GitHub open issue 本身只能是 `OPEN_REPORT_UNCONFIRMED`，不能直接作为已确认漏洞或永久生产阻塞。
