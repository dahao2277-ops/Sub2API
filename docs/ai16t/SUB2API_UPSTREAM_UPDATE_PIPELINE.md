# Upstream 受控更新流程

## Remote 和分支

- `origin`：未来老板控制的 `ai16t/Sub2API` fork；当前目标 namespace 未建立时保持缺失。
- `upstream`：`https://github.com/Wei-Shaw/sub2api.git`，push URL 必须为 `DISABLED`。
- 稳定分支：`main`；当前部署：`production`；下一版本：`develop`。
- 开发：`feature/*` 或 `ai/*`；生产修复：`hotfix/*`；官方同步：`upstream-sync/*`。

## 当前基线

- locked release：`v0.1.179`
- locked commit：`75f88be5f75c27771836b586f7de1503afa0e3bc`
- latest release：`v0.1.179`
- upstream main：`d45135d87df16d48637f04ccd245727bc955ba54`
- 状态：正式 release 无新版；main 有后续提交，需 review，不自动升级。

## Pipeline

```text
detect release (read-only)
 -> release notes
 -> official advisory + security diff
 -> migration diff
 -> upstream-sync/<version>
 -> merge/rebase strategy review
 -> unit/frontend/security tests
 -> Hybrid E2E
 -> production DB clone migration + rollback
 -> Staging
 -> Canary
 -> Blue/Green inactive slot
 -> explicit production approval
```

禁止 release 触发器直接更新 Production。若无 mandatory Critical/High 修复，基线只记录 `UPDATE_AVAILABLE`。v0.1.179 release 线包含 migration 226/227/228 与 long-context billing 行为变化，采用前必须执行 backup、DB clone migration、兼容和 rollback 测试。若 migration 破坏旧版本兼容，报告 `MAINTENANCE_WINDOW_MAY_BE_REQUIRED`。

## 判断分类

- `OFFICIAL_ADVISORY`：官方 GitHub Security Advisory。
- `CONFIRMED_REPRODUCIBLE`：当前 pinned 版本有最小可重复证据且无缓解。
- `OPEN_REPORT_UNCONFIRMED`：Issue/报告未由权威或动态复现确认。
- `FALSE_POSITIVE`：动态验证证明不可触发或已缓解。

生产阻塞只由未缓解的官方 Advisory 或 confirmed reproducible 风险触发。
