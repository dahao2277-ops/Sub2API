# Sub2API 定制清单

| Upstream 路径 | 我们的修改 | 原因 | 冲突风险 | 可否移到 extension | 升级处理 |
|---|---|---|---|---|---|
| `backend/internal/integration/ai16tadapter/*` | 新增 Adapter port/types/tests | 隔离 Commercial Core 边界 | LOW（全新 package） | 已隔离 | 编译/单测后按 upstream API 变化调整 |
| `docs/ai16t/*` | 新增架构、安全、E2E、发布文档 | 固化安全门和证据 | LOW | N/A | upstream sync 保留 |
| `deploy/ai16t-sub2api/*` | 新增 127.0.0.1 独立 sandbox blueprint | 避免 DB/Redis/namespace 共享 | LOW | 已隔离 | Compose config + isolation check |
| `tools/ai16t/*` | 新增只读 upstream/isolation 检查 | 防自动升级和配置串用 | LOW | 已隔离 | shellcheck/bash -n + CI |

未来任何对现有 upstream 文件的修改必须先追加本表；优先 adapter/service/plugin/custom frontend module。不得删除 README、LICENSE、copyright、required notice 或 upstream attribution。
