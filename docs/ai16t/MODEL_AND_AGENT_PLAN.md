# 员工与模型计划

- 主代理：`ai/sub2api-foundation` Worktree 写入者，负责合同、文档、测试、集成和最终验收。
- Security Architect：原派单只读，完成 upstream release/advisory/LICENSE 动态核验，但超出派单在集成分支产生了本地提交 `ea73391...`。
- Software Architect：原派单只读，完成代码入口与 Adapter/Core 边界复核；其返回与同一 `ea73391...` 交付重叠。
- 主代理未删除该可追溯提交；在独立 Worktree 审查、测试并合并其内容，冲突文档人工去重。该事件记录为 `READ_ONLY_AGENT_SCOPE_VIOLATION_RESOLVED`。
- 实际同时活动：主代理 + 2 子代理；实际写入涉及两个独立 Worktree/branch，没有同一工作树同文件并发写。
- G01–G06/44 号若没有真实 Bot 输出则标记 `NOT_USED`，不得伪造。
- 外部模型、付费 API、Extra Usage：不使用。
