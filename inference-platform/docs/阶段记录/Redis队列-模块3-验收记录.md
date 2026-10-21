# Redis 公共通知队列（模块 3）验收记录

- 用户于 2026-10-21 再次要求“继续进行下一部分”，按顺序仅进入模块 3。
- 首轮环境核查：2026-10-21T13:47:52+08:00；日常基线采集：13:53:33+08:00。
- 开发侧登记：2026-10-21T14:30:24.000+08:00。
- 状态：代码和隔离故障验证交付，待用户验收；真实 Redis 联网、Lua 执行、进程重启/持久化验收待补。日常未切换，讲解后暂停，不自动开始模块 4。

## 本轮实现

1. 公共任务/调用绑定时，同一 SQLite 事务写 outbox；原请求、幂等身份和业务结果仍由原任务系统负责，重复绑定不创建第二份通知。
2. Redis Streams 投递、消费组读取、超时未确认消息重新领取；消息包含 eventId/invocationId/修订/逻辑队列，不包含业务正文或凭据。
3. SQLite 短事务取得投递权，事务外 XADD，之后保存投递结果；断在任一边界可补投，重复消息由 SQL 执行资格挡住，不重复推理。
4. SQLite 仍按服务/模型轮询，统一容量和租约不交给 Redis。已发出但结果未知时保留容量及未确认通知，不因 Redis 数据丢失、重新领取或 TTL 解除。
5. 可信结果/终态持久化后确认；固定 Lua 脚本在 Redis 内原子核对消息完整内容与 ACK/删除，ID 对应另一任务时不误删。脚本源已实现，真实 Lua 执行尚未验证。
6. Redis 故障时公共离线任务有界落 SQLite 等待，暂停新的公共领取/发送，公共 LLM 新请求明确 503。在途结果、续租和结算不依赖 Redis，确认失败留待恢复。
7. 新增独立通知维护 CLI、只读本机 queue GET、投递/接收/失败原因变化/结算事件。GET 仅读快照，不连接 Redis。
8. 默认 common_queue_backend=sqlite、common_execution_enabled=false；可选 requirements/queue.txt 仅声明 redis-py 6.4.0，没有安装或修改基础环境。

旧日常 ASR/YOLO/LLM 通道没有自动替换，真实协议执行器在模块 4 接线；API Key/公共接口/页面时间线仍在模块 5，Linux 在模块 6。独立 CLI 只维护通知、到期恢复和确认，不调用模型。

## 环境与真实验证边界

首轮检查：当前 Python 环境不存在 redis/fakeredis 模块，未发现可用 Redis 命令或 6379 监听，未取得可用 WSL Redis 环境；没有下载或安装。没有指定获准的独立 Redis 测试地址。

本轮 Redis 行为由明确命名 RedisStreamFake 的客户端接口替身模拟：包括 PEL、重新领取、掉线、回复丢失、丢数据和消息身份冲突。它不是真实 Redis，也不执行 Lua 源程序。客户端参数装配测试同样是隔离替身，不能冒充真实 TLS/认证通过。

真实 Redis 合同测试已提供：配置 INFERENCE_REDIS_LIVE_TEST_URL 后才运行，使用本次隔离 SQLite 安装 UUID 的专属流，不执行清库，只删除本测试键。当前该项明确跳过。即使以后合同测试通过，真实 Redis 进程重启、持久化和恢复部署仍须另行验收。

## 最终验证

| 批次 | 实际结果 | 范围 |
|---|---|---|
| 最终新增/迁移/结构/CLI | **58 passed、1 skipped，80.01 秒** | Redis 隔离故障、配置/秘密排除、迁移、结构与无副作用导入、两个本地 CLI 子进程 |
| 旧链路/目录兼容 | **214 passed，213.63 秒** | 模块 2 公共执行、SQLite broker/dispatcher、执行租约、原 LLM、日志、公共服务目录 |

不将两批与中间重复批次相加。没有执行后台全量、前端构建/浏览器，也没有真实模型/GPU/CPU验证。本轮无前端功能改动。

最终通过重点：

- 创建任务/outbox/事件一起回滚，任务重复绑定仍只有一份 outbox。
- 模拟 XADD 成功但回复丢失、以及在 SQL 记录前中断：允许重复通知，只推理一次。
- 丢 Stream 后从等待任务补投；不重发成功、已执行或结果未知任务。
- 两个投递器/消费者并发不重复取得执行权；ASR/YOLO/LLM 共用额度不超额，LLM 不进入离线流。
- Redis 断连时已受理离线任务保留；公共 LLM 新请求不落错误的受理记录。
- 调用已开始时 Redis 失联，结果仍能提交；ACK 失败不撤销结果或再次释放容量。
- 未知保留 PEL 和容量，到显式可信终态后才结算/确认。
- 无效或旧修订通知不改变任务；消息 ID 内容冲突不误删；缺消息正文时清理已结束调用的残留确认。
- 投递和确认不持 SQLite 写锁；GET 不发 Redis 命令、不写事件；快照时间带 UTC 标记。
- 后端切回 SQLite 不能绕过已绑定的 Redis 调用。
- 0016→0017 保持原结果、未知许可、旧事件和任务字段；仅给原公共调用补 outbox，重复升级安装 UUID 保持；非空降级拒绝。
- URI 和密码不进入响应/事件/运行日志；重复投递失败原因不刷事件。
- 两个真实本地 CLI 子进程分别验证“未选择 Redis”和“模拟客户端缺失”，均按预期报错退出 2；没有联网、加载模型或修改日常库，不能把这两项当成 Redis 启动成功。

最终命令：

```powershell
python -m pytest tests/integration/test_redis_queue.py tests/unit/test_redis_queue_settings.py tests/integration/test_migrations.py tests/unit/test_structure.py tests/e2e/test_redis_queue_process.py --basetemp=runtime/verification/redis-module3/delivered -q --tb=short --junitxml=runtime/verification/redis-module3/delivered.xml

python -m pytest tests/integration/test_common_execution.py tests/integration/test_broker_dispatch.py tests/integration/test_execution_leases.py tests/integration/test_online_api.py tests/unit/test_common_logging.py tests/integration/test_service_catalog.py --basetemp=runtime/verification/redis-module3/compat -q --tb=short --junitxml=runtime/verification/redis-module3/compat.xml
```

报告 delivered.xml / delivered.txt 和 compat.xml / compat.txt 均在 runtime/verification/redis-module3/。中间 smoke、first、queue-final、verified、verified-final、reviewed 批次保留但不累计。使用工作区独立 basetemp，不修改系统临时目录权限。结构检查也确认新模块导入不读取坏环境或创建文件。

## 迁移、数据与运行核对

0017 增加 inference_queue_states / inference_queue_outboxes 两表，不改旧通知/任务/结果或容量；只在隔离库升级。安装 UUID 只是新队列身份，不是伪造历史任务事件。关闭模式仍兼容 0014 日常库，未做日常激活。

2026-10-21T14:28:26+08:00 只读复核：日常仍 **20261020_0014、26 表、42 任务**。与 13:53:33 基线相比，全部 26 表记录摘要、上传文件摘要及 .env 摘要一致。证据 daily-before.json、daily-after.json；不把这两个时点之外未采集的数据称为已核对。

同次查询 5000/5173/6379 监听结果为空。本轮没有启停日常平台、模型或 Redis，也未改写旧管理器 state。没有真实校园服务请求、真实推理、依赖/模型下载、SSH/Linux部署、Git 推送或访问/写入 Multica。仅本地文档和待集中同步记录更新；旧模块、3A/3B和历史用户验收保持。

## 待补与下一部分

真实 Redis 服务、官方客户端、ACL/TLS、Lua、真实进程重启和持久化恢复均待条件具备后补验。Redis 数据备份不能代替 SQLite/文件/配置备份；上线切换须正常停止相关组件、核对在途与未知调用、成套备份并迁移，不并行运行两份数据库克隆。

本模块代码/隔离交付后讲解暂停。下一部分模块 4 服务适配与监控采集，尚未开始。用法、限制及真实补验入口见[模块说明](../Redis队列-模块3.md)。

