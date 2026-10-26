# 模块6B：真实Redis验收

> v2.25本地后续：跨Node监控统一查询预算已实现并隔离验证；它使用中心SQLite，不代表本模块真实Redis/Lua/重启已验收。校园网未恢复，不主动重连。见[统一限流](跨Node监控统一限流.md)。

## 2026-10-22补充：先本地开发，环境问题已经登记

用户已确认既有SSH资料：`swufefic-v100` → `swufe@10.8.21.33:52222`，当前暂不能接校园网。本机别名配置和已有v100密钥存在；本次SSH及TCP超时，未进入认证，不能确认远端项目目录写权限或Redis是否已安装。暂时继续本地开发，不反复试连、不重新生成密钥，也不要求重复提供连接资料。

新增本地验收问题日志，复用现有JSON滚动运行日志；问题快照使用独立文件，**不使用日常任务数据库，不把网络问题写成任务失败，也不影响已运行的日常服务**。

```powershell
python -m backend.modules.deployment.validation_log defer --stage module6b --target swufefic-v100 --code CAMPUS_NETWORK_UNAVAILABLE --source operator --evidence-ref redis-module6b/connection-20261022/ssh-probe.json
python -m backend.modules.deployment.validation_log show --stage module6b --target swufefic-v100
```

`defer`只由人工或明确检查结果调用，不启动轮询。默认保存到`runtime/logs/validation/`：按PID的`environment-validation-<pid>.log`与按阶段/目标标识的`issue-*.json`。状态`waiting_external_verification`表示待外部验收，来源`operator`表示用户确认当前无法联网；证据仅存相对引用和SHA-256，不将SSH原响应或密钥写入日志。错误码使用白名单，拒绝原始错误正文/URL/任务终态。

重复登记更新观察次数和最近时间，保留首次时间；快照写入有独立文件锁并原子替换。文件运行日志写入失败时返回`runtimeLogWritten=false`，不撤销已保存的快照。`show`只读取快照，不建目录、写日志、连接Redis或探测校园网。当前仅提供本机CLI和文件记录，尚未增加管理页面或对外HTTP查询接口。

已完成本地实际登记，18项日志/结构测试通过；真实Redis/Lua/重启仍未验。后续接通校园网再继续真实检查，下文关于缺连接资料的内容为上一阶段状态，当前不缺SSH参数。

**当前进行中：验收工具与本地隔离回归已准备，真实Redis/Lua/进程重启尚未通过。** 当前电脑没有Redis客户端、Redis服务或可运行Linux环境，也未配置独立测试地址。需要独立Linux测试Redis条件，不能拿共用实例做重启实验。

6A的本地部署准备已经完成；Linux上的Gunicorn/systemd/nginx真实启动仍待服务器环境。6B沿用原Redis Streams＋SQLite设计，不迁移日常数据库，不改默认SQLite设置，不启动模型，不涉及模型内存或显存管理。

## 新增工具

- `scripts/verify-redis-linux.sh`：从项目目录执行的一键验收入口，不安装依赖或管理系统服务。
- `tests/e2e/redis_live_support.py`：在新私有目录启动独立、回环、随机端口、独立随机密码的Redis；只通过自己持有的Popen对象停止/强退自己创建的进程，不对外部URL发SHUTDOWN，不调用systemctl或kill其他PID。
- `tests/e2e/test_redis_live.py`：9个真实Redis场景；没有明确配置时跳过，不能算通过。
- `requirements/queue-validation.txt`：独立Linux验证依赖，包含官方redis-py与pytest，不含本机模型引擎。本轮未安装，目标Linux依赖解析仍待验。

启动后核对Redis上报PID与直接子进程一致、数据目录匹配、AOF设置匹配。写入不含密码的`started-*.json`，记录版本、PID、Linux与持久化配置；密码只在私有配置和进程内。配置为`appendonly yes`、`appendfsync always`，不用淘汰策略删除排队通知。这里是隔离的故障测试配置，不是生产性能承诺。

## 验收场景

| 场景 | 要确认的事实 |
|---|---|
| 真Lua身份确认 | 错误身份/重复字段不确认；已消失的条目只清相应pending，不改其他消息 |
| 重复通知和重复终态 | 推理回调仅一次，结果先落库，容量只释放一次，重复通知最终清理 |
| 消费者子进程直接退出 | 重新领取通知不能重执行已发出的调用；租约失效保留未知占位，显式终态只释放一次 |
| 正常停止后重启Redis | Stream、消费组、pending从本次AOF恢复 |
| 强退后重启Redis | 验证本次Redis进程故障下的恢复；不能写成整机断电/磁盘故障通过 |
| 丢失本测试Stream | 从SQLite补投，保持一次业务执行；不FLUSHDB/FLUSHALL |
| Redis故障时保存在途结果 | 结果和额度正常提交，确认延后；离线新任务保留等待 |
| Redis写入后SQL记录失败 | 再次投递可能重复通知，不能重复推理 |
| 三类服务共享额度 | ASR/YOLO/LLM仍遵守同一共享额度，LLM不进离线Stream；Redis断开时在线请求拒绝 |

ASR/YOLO/LLM业务及健康是明确的隔离回调，不调用真实模型。需要真实验证的是Redis协议、Lua、进程、AOF和SQLite恢复关系。真实模型、设备、业务内部队列及多主机仍另验。

## Linux执行方式

先准备用户本人可运行的Linux项目副本、Python3.11、已审查的独立验证依赖和已安装的Redis6.2+。不使用服务器的共用Python或共用Redis做破坏性验证。依赖可按`requirements/queue-validation.txt`在新环境准备，脚本本身不会下载、安装或升级。

在Linux项目根目录运行，例如：

```sh
sh scripts/verify-redis-linux.sh \
  /home/swufe/FIC-Project/tyb/inference-platform/.venv/bin/python \
  /usr/bin/redis-server \
  module6b-first
```

两个可执行文件用目标机器的真实路径替换，批次名只能包含小写字母、数字和短横线。需要目录尚不存在；脚本拒绝重复使用旧批次和链接的runtime目录，不清理已有目录。每次使用新的批次名。

脚本只在`runtime/redis-validation/<批次名>`下写入临时SQLite、Redis配置、AOF、私有日志和XML；测试结束停止自己的Redis，保留证据。只有pytest成功、XML至少9个测试且**没有失败、错误、跳过**时脚本才返回成功。Redis版本和实际进程启动证据在各测试目录的`owned-redis/started-*.json`中。

证据目录内可能包含测试用密码和带连接URL的私有设置，**不得上传整个runtime或粘贴原始env/settings文件**。对外只摘录不含凭据的XML统计、错误码和版本事实。

如果只有一个已有测试Redis URL，可以先运行原模块3的非重启契约：在本地私有环境中配置`INFERENCE_REDIS_LIVE_TEST_URL`，运行`tests/integration/test_redis_queue.py -k live_redis_contract`。该入口只删除本次新库UUID对应的测试Stream；不能证明Redis进程重启或AOF恢复。本轮没有配置该URL或访问任何Redis。

## 本轮代码修正

确认消息的Lua增加重复字段检查。Redis Stream原始条目允许重复字段名，字段数量相同不代表完整身份；现在发现重复字段就返回不匹配，避免只比较已有字段值而漏检缺失字段。新增真Lua场景专门构造这种XADD原始消息，**该场景仍待真实Redis执行**；现有替身回归不冒充Lua执行证据。

## 当前缺少的条件

需要可执行测试的Linux主机连接方式及本人项目目录/独立Python/redis-server路径；如只做现有实例契约，需要专用测试Redis地址和键空间权限。密码通过本地私有文件配置，不在聊天提供。没有上述条件时，本模块保持进行中，不标为完成、不自动开始下一模块，不修改日常配置兜底。

原始依据：[Redis持久化说明](https://redis.io/docs/latest/management/persistence/)、[Windows运行方式](https://redis.io/docs/latest/operate/oss_and_stack/install/install-redis/install-redis-on-windows/)。官方能力不能代替本项目实际验收；本轮结果见[阶段记录](阶段记录/Redis真实验收-模块6B-验收记录.md)。
