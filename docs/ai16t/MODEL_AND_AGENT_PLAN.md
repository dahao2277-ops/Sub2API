# 员工与模型计划

- 主代理：唯一当前 Worktree 写入者，负责合同、文档、测试、集成和最终验收。
- 只读 Security Architect：upstream release/advisory/LICENSE 动态核验。
- 只读 Software Architect：Sub2API 代码入口与 Adapter/Core 边界复核。
- 写入峰值：1；只读并行员工：2；普通会话不超过主代理 + 2 子代理。
- G01–G06/44 号若没有真实 Bot 输出则标记 `NOT_USED`，不得伪造。
- 外部模型、付费 API、Extra Usage：不使用。
