# Sub2API × AI16T 独立平台启动包

## 项目名称

SUB2API_INDEPENDENT_PLATFORM_BUILD

## 背景

老板决定长期保留两套独立 Token Platform。平台 A 继续 New API 路线；平台 B 以 Sub2API v0.1.179 为控制面候选，通过 Sub2APIAdapter 对接 AI16T Commercial Core。商业授权证据由老板独立管理，技术团队不读取或索取 Telegram 内容。

## 目标

建立平台 B 的可审查实现基础、隔离边界、适配器合同、安全整改门、动态 E2E 矩阵和受控升级/发布流程。AI16T Ledger 是唯一财务 Authority。

## 不做什么

- 不修改或部署 ai16t.com；该域名保留给平台 A。
- 不购买域名、VPS 或其他付费资源。
- 不接真实 Provider Secret、支付、客户或批量真实 Key。
- 不自动升级 upstream、合并 main 或 production、发布公网生产。
- 不删除 README、LICENSE、版权、notice 或 upstream attribution。
- 不读取、检查、索取或发布老板的 Telegram 对话。

## 用户

老板、平台管理员及未来 API 客户；当前阶段仅本地 Mock/test 与隔离 Staging 准备。

## 功能范围

- Sub2API 身份、API Key、Provider、Model、Admin、Usage 等成熟控制面继续保留。
- Sub2APIAdapter 只把身份/模型/请求上下文交给 Commercial Core。
- Ledger、幂等、余额预留、定价、最低利润、Router、成本与 Secret 由 Commercial Core 负责。
- Sub2API 财务字段只作为只读 projection；漂移报告为 `PROJECTION_DRIFT`。
- 独立 Postgres、Redis、session、API Key/Provider 数据、Docker namespace、备份、域名和监控。

## 技术限制

- Sub2API baseline：tag `v0.1.179`，commit `75f88be5f75c27771836b586f7de1503afa0e3bc`。
- Commercial Core pin：branch `integration/ai16t-commercial-core-v1-remediated`，commit `56108397f31a97926f33d28420e9b44a52547bd2`。
- 当前开发分支：`bot2/sub2api-ai16t-integration`；功能分支/Worktree 独立。
- 当前机器没有上述 Commercial Core 源码，只能完成 Adapter 端合同与单元验证；完整 Hybrid E2E 必须在 M2 权威 Worktree 执行。

## 业务限制

`AUTHOR_PERMISSION_HANDLED_BY_OWNER = TRUE`。技术侧继续遵守 LICENSE、copyright、required notices、dependency licenses 和适用合规义务。

## 已有项目路径

`/Users/hhhh/Projects/sub2api`

## 参考资料

- `README.md`、`README_CN.md`、`LICENSE`
- `/Users/hhhh/Desktop/20260823_AI16T_COMMERCIAL_CORE_V1_BOSS_REPORT.md`
- `/Users/hhhh/Desktop/20260823_NEW_API_AI16T_HYBRID_V1_BOSS_REPORT.md`
- 老板 2026-08-23 正式执行指令

## 成功标准

- Adapter 单元测试证明 raw API Key 不出边界、禁用/撤销 fail closed、权威 Ledger 结果校验、projection drift 不重复计费。
- 26 项真实 Hybrid E2E 在 M2 的真实 Sub2API Postgres/Redis/HTTP + Mock Provider 上全部通过。
- Critical/High 官方 Advisory 或可复现未缓解风险清零。
- migration clone、backup/restore drill、Blue/Green、health/ready、监控门均有可重复证据。
- 双独立 Reviewer 对同一最终 HEAD 给出 GO。

## 风险

- 本机无法访问 M2 Commercial Core 权威源码与其 Git 历史。
- 目标 GitHub namespace `ai16t/Sub2API` 当前不存在或本账号不可见，不能安全设置正式 origin。
- v0.1.179 与 upstream main 存在更新差异；必须先审 release/security/migration diff。
- Sub2API 自带 balance 与 Ledger projection 若接线错误可能形成第二财务 Authority。
- Secret at rest、重复 Usage、timeout billing、并发余额仍需真实 Hybrid 动态证明。

## 时间优先或质量优先

质量优先。任何财务、安全或迁移证据不完整时不得标记 READY。

## 预算

本阶段外部费用上限为 0；不购买域名、服务器，不调用付费外部 API。

## 是否允许联网

仅允许访问官方 GitHub upstream/release/advisory 等公开技术资料。

## 是否允许使用外部模型

否。

## 是否允许创建 GitHub Issue

否。

## 是否允许创建 Draft PR

否；正式 origin 未建立。

## 是否允许自动部署测试环境

仅允许未来在 M2 的 127.0.0.1 隔离环境，经资源锁后执行；本轮不自动启动 Docker。

## 最终交付物

Adapter 合同及测试、隔离 Compose blueprint、架构/风险/验收/E2E/硬化/更新流程文档、老板报告。
