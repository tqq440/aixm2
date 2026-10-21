# 独立主机 Node 基础（模块 4A）验收记录

- 用户于 2026-10-21 确认增量方案并授权实施，按模块先进行 4A。
- 首轮环境核查时间：2026-10-21T15:30:14.6074227+08:00。
- 日常只读基线：2026-10-21T15:33:32.039414+08:00。
- 开发侧交付登记：2026-10-21T16:02:49.779144+08:00。本轮 4A 开发侧验证交付，待用户验收；讲解后暂停，下一部分 4B 等待用户继续。

## 实现范围

1. 独立 NodeSettings、角色凭据、独立 SQLite/日志路径，不读取平台 .env/AppSettings。
2. 独立 HostNodeBase 与 node-migrations Alembic 链，五个业务表及专属版本表；显式 init-db，启动不迁移。
3. 多服务资料、不可变配置修订、并发版本检查、独立服务上报凭据及中心关联声明。
4. 管理者绑定启动身份、代次和修订；本轮不启动服务。未确认停止阻止替换，旧实例不复活。
5. 生命周期上报、严格校验、序号幂等、乱序拒绝、五秒建议心跳、十五秒过期、Node 重启后新心跳确认。
6. 分页只读快照/事件，状态变化同事务事件，普通心跳不刷事件；无网络查询、无伪造进度或设备。
7. 独立 CLI/进程锁、真实本机回环 HTTP 和模拟 ASR 上报器、当前接口 OpenAPI 及业务侧接入文档。

原中心公共执行、Redis、GPU 优先/合条件 CPU 承接、并发额度、结果保存、鉴权及日志计划全部继续。Node 无推理队列；真实 ASR 内部队列、上报、排空和内存管理由业务侧接入。

本轮没有 Docker 管理、动态资源采集、中心调度接线、公共 `/api/v1`、逐任务监控/结果回传或 systemd。对应能力显式 false，留 4B/4C/5/6。没有修改日常 .env/数据库/任务/许可，也未下载依赖/模型、访问真实服务、推送 Git、部署 Linux 或访问/写 Multica。

## 验证进展与首次问题

- first：61 passed、1 failed；停止报告校验样例被测试辅助函数补齐零计数，修正样例为明确未知计数；未放宽业务规则。
- verified：62 passed、1 failed；真实回环 HTTP 被沙箱 WinError10013 阻止，不能据此登记 HTTP 通过。临时进程正常退出。
- loopback-verified：允许隔离回环后 63 passed、2 failed；新增进程所有权检查使用 filelock 默认线程本地上下文，HTTP线程/并发线程被错误拒绝。修改为进程内共享锁上下文并保持数据库启动代次校验。
- checked：**65 passed，47.06 秒**；包含独立 Node 测试、配置、结构/无副作用导入和真实 Windows Node HTTP/停止/重启/模拟 ASR 上报。
- 原目录/执行/Redis/旧 Agent 定向回归：**207 passed、1 skipped，206.84 秒**。跳过项为未配置真实 Redis 的合同测试，和模块 3 的待补事实一致。以上批次不相加，未跑完整后台、前端、浏览器或真实模型。

模拟上报器不加载 ASR，不做音频转写、内部队列或资源采集。Windows 回环子进程通过不代表 Linux、Docker、跨机器 TLS 或真实模型验证。真实 Redis 联网/Lua/重启仍保持原模块 3 的待补状态。

证据目录：runtime/verification/node-module4a；各批次 XML 与临时数据库、stdout/stderr 均独立保存。日常保护 before.json 与 after.json 已完整核对；详见下节。

## 最终命令与数据保护

```powershell
python -m pytest tests/integration/test_host_node.py tests/unit/test_host_node_settings.py tests/e2e/test_host_node_process.py tests/unit/test_structure.py --basetemp=runtime/verification/node-module4a/checked -q --tb=short --junitxml=runtime/verification/node-module4a/checked.xml

python -m pytest tests/integration/test_service_catalog.py tests/integration/test_common_execution.py tests/integration/test_redis_queue.py tests/integration/test_node_agent_service.py tests/integration/test_node_registry.py tests/integration/test_agent_management_api.py tests/unit/test_common_logging.py --basetemp=runtime/verification/node-module4a/compat -q --tb=short --junitxml=runtime/verification/node-module4a/compat.xml
```

2026-10-21T16:00:45.180197+08:00 最终只读核对：日常 head=20261020_0014、42 任务、42 上传。27 张 SQLite 表中包含 sqlite_sequence 内部表，排除内部表为原 26 张；全部表记录摘要、上传文件和 .env 与 15:33:32 基线一致。432 份本轮前既有 backend/迁移/依赖/测试源码摘要保持，无修改；新代码和 Node 迁移单独增加。

真实进程验收：显式 init-db 连续两次成功；两次启动同一 Node 库，模拟 ASR 通过真实 HTTP 发送 starting/warming/ready，重启后旧报告显示未知、相同报告不续期、新序号恢复；继续 busy/draining/stopped，两次 Node 均按信号正常退出并记录 NODE_STOPPED。模拟器不持有管理/只读凭据、不跟随重定向、不走系统代理。其余故障在 Flask/数据库隔离边界验证。

GET 快照检查包含分页上限/乱序报告/时间倒退/重复上报/凭据轮换/旧对象失效/并发重复/事务事件失败回滚/无 N+1，以及当前 OpenAPI 路由、组件命名与引用有效性。没有静默放宽 stopped 规则或凭据权限。已实现接口的 OpenAPI 静态副本与实际生成内容一致。

## 交付与下一部分

本次只交付 4A 基础，保持原中心模块 1/2/3 与旧业务入口。独立应用的启动本身不等于真实服务、Docker 或公共调度已接入，executionReady/publicGateway/containerControl/resourceMetrics 均明确 false；容器状态 unknown、逐任务进度和实际设备 null。

下一部分为 **4B 主机管理**：受控 Docker 启停、排空、升级/回退、资源动态采集和操作日志；中心执行与结果恢复在 4C，Node 公共 API/应用身份和管理页面在模块 5，生产 Linux/systemd/TLS 与真实联调在模块 6。真实 Redis 仍待服务/客户端条件补验。交付后暂停，不自动进入后续子模块，不补记历史或用户验收通过。
