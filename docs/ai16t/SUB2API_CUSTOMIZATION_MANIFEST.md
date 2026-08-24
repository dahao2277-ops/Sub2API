# Sub2API 定制清单

| Upstream 路径 | 我们的修改 | 原因 | 冲突风险 | 是否隔离 | 升级处理 |
|---|---|---|---|---|---|
| `backend/internal/ai16t/coreadapter/*` | Core reserve/settle/refund/SecretProvider 合同骨架 | 商业核心 port 与 fail-closed 默认值 | LOW（全新 package） | 是 | 对照 pinned Core contract 后跑专项测试 |
| `backend/internal/integration/ai16tadapter/*` | Sub2API 身份/模型到 Core Execute 的组合边界 | raw key 指纹化、Ledger 结果校验、projection drift | LOW（全新 package） | 是 | 编译/单测后按 upstream auth API 变化调整 |
| `backend/internal/ai16tsecret/*`、`backend/cmd/ai16t-secret-provider/*` | AES-256-GCM SecretProvider 与 Unix socket sidecar | APIYI Key 只按 `secret_ref` 解析，DB/Git/日志不保存明文 | LOW（全新 package） | 是 | 生命周期、权限、密文备份恢复和 secret scan 必须通过 |
| `backend/internal/server/routes/ai16t_hybrid.go` | 动态模型目录、Chat/Responses 路由、durable drift gate | 只开放已定价/已验证模型并在 Ledger 后投影 | MEDIUM（现有 AI16T 扩展文件） | 是 | Core contract、模型配置和故障注入专项测试 |
| `frontend/src/i18n/index.ts` | 注册 `es` locale | 西班牙语入口 | MEDIUM（现有 upstream 文件） | 否 | upstream sync 时人工合并并跑 locale tests |
| `frontend/src/i18n/locales/es/*` | 西班牙语起步翻译 | 覆盖登录/常用导航的第一阶段 | LOW | 是 | 按功能矩阵补全，不把 fallback 误报为完整翻译 |
| `docs/ai16t/*` | 架构、安全、E2E、发布文档 | 固化安全门和证据 | LOW | N/A | upstream sync 保留 |
| `deploy/ai16t-sub2api/*` | 127.0.0.1 独立 sandbox blueprint | 避免 DB/Redis/namespace 共享 | LOW | 是 | Compose config + isolation check |
| `deploy/ai16t-sub2api/apiyi_provider.py`、`secret_provider_client.py`、`apiyi_catalog.py` | APIYI OpenAI-compatible 适配、密钥解析和模型快照 | 固定 Base URL、限制响应、脱敏、禁止环境变量明文 Key | LOW（全新模块） | 是 | fake upstream、真实小额 Canary、四方对账 |
| `tools/ai16t/*` | upstream/advisory/migration/isolation 只读检查 | 防自动升级和配置串用 | LOW | 是 | bash -n + CI；检查器使用 API/ls-remote，不运行 fetch/deploy |

未来任何 upstream 文件修改必须先追加本表；优先 adapter/service/plugin/custom frontend module。没有专项复核不得直接修改 payment webhook、migration、gateway billing、生产部署脚本。

以下文件不得删除、隐藏或通过改品牌绕过：`LICENSE`、README 中的许可证/归属信息、copyright、required notice、upstream attribution。
