# 模块6B：真实Redis验收记录（进行中）

## 2026-10-22增量：用户确认暂不能接校园网，先本地开发并记录日志

用户已补充并确认既有连接：`swufefic-v100`、`swufe@10.8.21.33:52222`。本机SSH配置与v100私钥/公钥文件存在，未读取秘密原值或重新生成密钥。专门用于旧上传向导的aixm_lab私钥仍不存在，不把它当作本次使用已有v100密钥的前置要求。

- `09:46:58+08:00` 严格BatchMode SSH、禁用主机密钥更新与端口转发，只请求固定只读Python检查；连接超时exit255，stdout为空。没有进入认证、没有远端命令或文件写入。
- `09:47:41+08:00` 单一授权SSH端口5秒TCP复测超时。系统到该地址选中WLAN2默认网关，未取得到实验室的可用连接。不能据此判断Redis或模型故障，也不能确认远端目录归属/写权限、Redis安装。
- 用户随后明确“先在本地开发，记录到日志系统，暂时无法接入校园网”。此后停止网络重试，不再要求重复SSH资料或密码；不安装VPN/WSL，不改网络或服务器认证设置。

连接证据在`runtime/verification/redis-module6b/connection-20261022/`的`ssh-probe.json`、`tcp.json`、`route.json`。该核查仅确认现有配置和网络失败，不代表独立SSH认证成功。

### 本地日志功能与实际登记

新增`ValidationIssueLog`、受控Schema和`backend.modules.deployment.validation_log`本机CLI：

- 快照为`waiting_external_verification`，与推理任务终态分开。固定阶段/错误码/来源，证据只引用verification内JSON并计算SHA-256，不把连接原响应、密码、音频或聊天正文写入运行日志。
- 使用现有JSON滚动日志配置，按PID隔离；独立快照文件用文件锁和原子替换，重复观察更新次数/最近时间并保留首次时间。
- 文件日志打不开/handler写入失败时如实返回`runtimeLogWritten=false`，不撤销已保存的快照。只读`show`不建目录、写日志、探测网络或查询Redis。
- 当前是本机CLI/运行日志与文件快照，没有增加管理页面、公共HTTP入口、数据库迁移或后台重试。

`09:54:11+08:00`实际CLI登记成功，`snapshotSaved=true`、`runtimeLogWritten=true`、`taskStateChanged=false`，随后独立show查询同一记录成功：

- 错误码`CAMPUS_NETWORK_UNAVAILABLE`，来源`operator`（用户确认当前无法联网），状态`waiting_external_verification`，观察次数1。
- 快照：`runtime/logs/validation/issue-f290d93902e911cc4d6063d30d6baf48.json`。
- JSON运行日志：`runtime/logs/validation/environment-validation-39252.log`。时间戳按已有约定保存UTC，本文时间均为北京时间。

**新增日志/结构18项通过，7.05秒**，见`runtime/verification/redis-module6b/local-log-20261022/log-tests.xml`。覆盖脱敏、危险输入拒绝、原始响应不入日志、重复观察、只读查询、损坏快照保护、日志打开/写入失败、忽略日常无效env和三级结构/导入。没有重复运行旧57项或真实Redis测试，不累计历史批次；真实Redis/Lua/重启仍未验。

`09:55:34+08:00`保护复核：日常仍0014，26表行数/内容哈希、42任务、42上传字节及`.env`与本轮前一致。证据为`local-log-20261022/before.json`、`checkpoint.json`。没有启停平台、模型或Redis，没有上传/安装/推送或Multica网络写入；本地v2.23仅追加进行中事件。后续继续本地工作，校园网恢复后再补真实验收，6B不标记完成、不自动进入6C。

下文保留2026-10-21原时点记录，早前“等待连接资料”已由本次确认与用户暂缓联网取代。

用户要求“6A做完做6B”。6A本地准备已收尾；本轮进入6B，真实Linux/Redis条件尚未具备，**6B不能记为已完成或真实通过**。保护快照登记`2026-10-21T22:38:14.589175+08:00`，本地进展登记`22:49:42.718712+08:00`。原6A及此前用户验收状态保持。

## 已准备

1. 独立Linux Redis测试进程控制器：固定回环、随机端口/密码、私有新目录、AOF与PID/数据目录核对；只管理自己直接创建的子进程，不使用共用实例的关闭或重启接口。
2. 一键Linux验收脚本：拒绝重复目录和链接runtime、不安装依赖，不清库；只有真实9项全部执行成功且无跳过才返回成功。
3. 9项真实Redis场景：Lua精确身份与重复字段、重复通知/终态、消费者子进程崩溃、正常/强退Redis重启、丢流补投、断联保存结果、Redis成功后SQL失败、三能力共享额度及在线故障门控。业务及健康为隔离回调，不是实际模型。
4. ACK Lua增加拒绝重复字段保护：原来只检查字段数量和值，原始Stream重复字段可能漏掉一个必需字段；现在重复字段直接不匹配。新增真实Lua案例等待实际环境运行，不能因替身回归通过就宣称Lua已通过。
5. Linux独立验证依赖清单和[操作说明](../Redis真实验收-模块6B.md)。没有实际安装依赖。

## 本轮验证

最终定向命令包含：`test_redis_live_harness.py`、`test_redis_queue_settings.py`、`test_redis_queue.py`、`test_redis_queue_process.py`、`test_redis_live.py`、`test_structure.py`。

- **57通过、10跳过，48.45秒**，见`runtime/verification/redis-module6b/tests-final.xml`。其中新增9项真Redis和原1项URL契约均因没有真实环境跳过；没有任何真实Lua/Redis/重启通过记录。
- 本地通过部分包括控制器配置注入拒绝、Windows拒绝、只操作拥有的Popen、PID不符清理、停止超时清理，以及原队列隔离故障回归、旧CLI依赖缺失/禁用保护和三级结构/导入检查。进程控制单测使用替身，不是真Redis进程。
- `D:/develop/git/bin/bash.exe -n scripts/verify-redis-linux.sh` 返回0，仅为shell语法检查，不是Linux运行。
- 初批57通过/9跳过保留在`tests-first.xml`；后加第9个真实场景形成最终10项跳过，不累计两批数字。对重启场景补上可见期后的重复终态通知清理等待，该真实场景仍未执行。
- 没有运行完整后端/前端全量或浏览器，前端与6A运行代码未改。

本机事实：Python3.11.16，没有redis-py、redis-server/redis-cli、Docker/Podman或可运行Linux；未设置`INFERENCE_REDIS_LIVE_TEST_URL`。已通过异步问题请求用户提供Linux测试主机或独立Redis信息，当前未取得可用连接；没有猜测密码、连接未知地址、安装WSL或启动共用服务。

## 日常保护与范围

`2026-10-21T22:51:02.385625+08:00`复核：日常库仍`20261020_0014`，26表全部行数/内容哈希、42份上传文件、`.env`哈希与本轮起点一致。654份起点源码/测试/迁移/依赖/脚本/前端文件中仅本轮有意修改的`backend/common/queue/redis_streams.py`变化；6A未提交工作保留，不回退。证据：`runtime/verification/redis-module6b/before.json`、`checkpoint.json`。

未迁移日常数据、启用日常Redis、安装/下载依赖或模型、调用真实模型、启停真实容器、SSH上传/服务器部署、推送Git或Multica网络读写。中心统一调度/容量/结果与Node无推理队列的边界保持，未做模型内存显存管理。

本地计划v2.23，Multica远端仍v2.9与`2026-10-19T14:37:17+08:00`写入/`14:37:27+08:00`核对时间，仅追加本地进行中事件。未把6B标为交付或完成。

## 待完成

取得Linux测试主机的可用连接、项目目录、独立Python与已安装redis-server路径后，准备独立依赖，执行本批工具、核对真实XML/版本/进程证据并修复实际失败。只有现成专用URL时可先跑非重启契约；不能替代进程/AOF验收。密码仅通过本地私有配置提供，不发聊天。

6B保持进行中，不自动转下一模块。6A实际Linux运行、多机/跨Node总采集预算、真实模型、备份恢复及历史迁移仍在后续范围。
