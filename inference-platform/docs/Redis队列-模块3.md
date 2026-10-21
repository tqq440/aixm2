# Redis 队列（模块 3）

本模块给公共执行内核增加可选 Redis Streams 通知通道。SQLite 仍保存任务、结果、容量、调用身份和待投递通知，Redis 负责发送、领取和确认通知。Redis 丢数据不会变成“任务没提交过”，也不能解除未知占位。

**代码和隔离故障验证已实现，真实 Redis 联网/重启验收待补。** 当前没有可用测试 Redis，Python 也未安装 redis 客户端，本轮没有安装或下载。日常库和入口未切换，默认仍 SQLite。真实模型适配与监控在模块 4，API Key 和对外接口在模块 5，部署在模块 6。

## 通知如何流转

1. 创建原任务并绑定公共调用时，同一 SQLite 事务写入 outbox 和受理事件。任务幂等仍以原请求记录为准；重复绑定不创建第二份通知。
2. 投递器在短写事务中取得有期限的投递权，退出事务后执行 XADD，最后保存消息编号。网络调用不占 SQLite 写锁。
3. 若 XADD 已写入但回复丢失，或者进程在保存消息编号前退出，后续会补投同一个 eventId。消息可能重复，推理不能因此重复。
4. 消费组读取新通知，或重新领取长时间未确认的通知。平台校验 eventId、invocationId、服务修订和逻辑队列，保存收到通知的事实。
5. 公共调度器仍按 SQLite 的服务/模型逻辑队列轮询，每队列每轮最多成功一个。收到通知只是领取条件，执行权、容量和租约仍由 SQLite 事务决定。
6. 结果或明确失败/取消已持久化，且没有未知占位时，才确认通知。Redis 内以固定 Lua 脚本原子核对完整消息身份，再 ACK/删除，防止相同消息 ID 指向其他任务时误删。

使用一个物理 Stream 承载公共离线通知，服务/模型的逻辑队列键随通知保存，公平选择仍由模块 2 负责。LLM 不进入离线 Stream，仅共享容量并检查 Redis 可用性。

## 故障与恢复

| 情况 | 平台行为 |
|---|---|
| Redis 不可用，提交公共离线任务 | SQLite 有界受理，保留待投递；不刷新原任务期限 |
| Redis 不可用，公共在线请求或新领取/发送 | 明确返回暂不可用或停止进入调用，不能静默改走 SQLite |
| 已取得租约、尚未发送时 Redis 故障 | 不发送，保留原租约/期限；恢复后在有效租约内继续，逾期按模块 2 未发送规则结束 |
| 已发出的调用在 Redis 故障时返回结果 | 继续保存结果和结算容量，确认通知留待重连 |
| ACK 失败 | 不撤销已提交结果；恢复后重复确认 |
| 通知丢失/Stream 丢失 | 从 SQLite 仍在等待的 outbox 补投，重建消费组从头读取；不重发已经执行或结果未知的调用 |
| 通知被重新领取 | 只改变通知归属，不能取得已经存在的 SQL 执行权 |
| 已发送后执行器退出/租约过期 | 保留结果未知、容量和未确认通知，不能据此重推理 |
| 后来显式核实远端终态 | 幂等结算容量，之后确认通知；原超时/失败历史保持 |
| 通知格式错误或身份不匹配 | 记录脱敏运行日志，仅清除该条无效通知，不改变任务或容量 |

补投默认每 30 秒重新检查等待任务；明确投递失败按原 dispatch_retry_seconds（默认 5 秒）退避。每轮最多扫描 20 个待投递、读取 20 条通知、结算 20 个终态记录。投递权失效仅允许重新投递通知，不释放任务容量。

任务原期限照常生效。恢复不等于无限延长任务，也不意味着未发送但租约已经过期的任务会自动重新执行。未知调用须由后续可信状态查询或显式核对解决。

## 配置、身份与存储

新增迁移 0017：`inference_queue_states` 保存安装身份和最新可用性快照，`inference_queue_outboxes` 保存通知、投递权、补投时间和 Redis 路由约束。迁移只给原公共调用补通知资料，不变更原任务、结果、事件或许可；旧 ASR/YOLO SQLite 队列不搬迁。已有 outbox 时拒绝降级删除。

Stream 名包含 SQLite 安装 UUID，防止不同平台数据库意外共用通知。恢复备份须停止原活动副本，不能同时运行两份数据库克隆。切换 Redis 地址、namespace 或后端前须正常停止相关组件并核对在途状态，不能把配置切换当作故障兜底。已绑定 Redis 的调用，即使切回 SQLite 也不绕过通知约束。

| 配置 | 默认值/含义 |
|---|---|
| INFERENCE_COMMON_EXECUTION_ENABLED | false，公共内核仍需显式启用 |
| INFERENCE_COMMON_QUEUE_BACKEND | sqlite；选择 redis 才启用新通道 |
| INFERENCE_REDIS_URL | 无默认值，redis/rediss URL；密码不输出、不入事件 |
| INFERENCE_REDIS_NAMESPACE | inference，加安装 UUID 形成实际键 |
| INFERENCE_REDIS_TIMEOUT_SECONDS | 1 秒，单次网络操作上限，允许 0.1—3 秒 |
| INFERENCE_REDIS_REPUBLISH_SECONDS | 30 秒，允许 1—300 秒 |

连接 URL 只支持数据库 0—15，不允许查询参数覆盖 TLS、超时等选项。rediss 校验证书；客户端禁止隐式重试，重试统一由 SQLite outbox 负责。

可选依赖清单 `requirements/queue.txt` 固定声明 redis-py 6.4.0，当前尚未安装验证；基础依赖未增加强制 Redis 导入。需要 Redis 6.2+，除认证、选择数据库等连接命令外，ACL 须允许 PING、INFO、XGROUP CREATE、XADD、XREADGROUP、XAUTOCLAIM、EVAL 及脚本调用的 XRANGE/XACK/XDEL，并限定到该平台自己的键空间。

上述能力依据 [Redis 消费组](https://redis.io/docs/latest/commands/xreadgroup/)、[重新领取](https://redis.io/docs/latest/commands/xautoclaim/)、[Lua 执行](https://redis.io/docs/latest/commands/eval/)及 [redis-py 版本资料](https://github.com/redis/redis-py/releases/tag/v6.4.0)。官方文档存在不等于本机真实环境已通过。

## 运行和查看

本轮不替用户改 .env 或升级日常库。以后在获准的隔离环境准备客户端和 Redis，正常备份/迁移至 0017，并设置公共执行开关及 Redis 配置后，可以运行：

```powershell
python -m backend.modules.queue.run_queue --once
python -m backend.modules.queue.run_queue
```

此入口只维护通知、补投、到期恢复和确认，**不会调用模型**。模块 4 才连接真实协议执行器；内部 `QueueService.run_once(worker_id, sender)` 已通过隔离回调验证。旧日常启动方式不自动新增 Redis 进程或此组件。

本机只读接口 `GET /api/admin/common-execution/queue` 返回后端、最后可用状态、检查时间、脱敏错误码和 outbox 数量。GET 不连接 Redis、不补投、不恢复、不调度。API 沿用模块 2 本机限制；尚未开放校园网鉴权公共接口。可用性快照只表示上次主动操作的观察，不当作实时 GPU 或远端模型状态。

任务时间线新增通知投递、接收、失败原因变化和结算事件。不写完整 Redis URL、密码、聊天正文或原始消息正文。公共模式运行日志按 PID 分文件。

## 真实 Redis 补验入口

准备获准的独立测试 Redis 和可选客户端后，设置 `INFERENCE_REDIS_LIVE_TEST_URL`，运行：

```powershell
python -m pytest tests/integration/test_redis_queue.py -k live_redis_contract -q
```

该测试只操作本次隔离库 UUID 对应的 Stream，不清空 Redis 数据库，需要本测试键的 DEL 权限。它覆盖真实客户端/消费组/脚本和删除本测试 Stream 后补投，**不等于真实 Redis 进程重启或持久化验收**；这些仍须独立补验。未提供地址时明确跳过，不能记为通过。

准确测试与未验证项见[模块记录](阶段记录/Redis队列-模块3-验收记录.md)。本模块讲解后暂停；下一部分是服务适配与监控采集，不自动继续。
