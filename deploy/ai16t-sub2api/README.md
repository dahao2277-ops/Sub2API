# Platform B 本地 Sandbox blueprint

该 Compose 仅用于未来 M2 的 `127.0.0.1` 隔离控制面验证，不会自动启动。它使用独立 project/container/network/volume/DB/Redis 名称，不引用 ai16t.com 或平台 A 资源。

当前 blueprint 尚未包含 Commercial Core 和 Mock Provider 服务，因为其权威源码在本机不可达；因此不得把它单独运行后的结果称为 Hybrid E2E 或 Staging READY。

使用前必须：

1. 在本地 shell 临时注入所有 `AI16T_SUB2API_*` 必填值，不把值写入仓库或命令历史。
2. 将 pinned Commercial Core 和 Mock Provider 以独立服务接入 private network。
3. 先运行 `tools/ai16t/check_isolation.sh`。
4. 获得 Docker/端口独占锁后才启动；不得复用平台 A 容器、端口、volume 或数据库。
5. 完成后保存 redacted config digest，不保存 Secret 原值。

`/health` 已有 upstream 路由；独立 `/ready` 仍是待实现验收项。
