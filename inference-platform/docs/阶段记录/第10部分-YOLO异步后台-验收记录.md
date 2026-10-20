# 第十部分：YOLO 异步后台

登记日期：2026-10-13。用户在第九部分交付后要求继续下一部分，当前仅实施[开发计划](../开发计划.md)中的第十部分，完成并讲解后暂停。

**当前状态：开发侧及本机真实模型验收完成，交付待用户确认并暂停。**第十一部分尚未开始，未将用户授权实施写成第十部分用户验收通过。本记录按本轮实际检查填写，不沿用第九部分计数。

## 1. 本阶段范围

- 复用现有模型和图片处理，接入图片上传、受管文件、持久化任务及检测结果。
- 扩展 M04 模型准入和任务输入/结果类型，保留固定版本、幂等、原 ASR 任务与结果兼容。
- 建立独立视觉能力队列与 Vision Worker，区分两类 Worker 身份、锁和运行状态，复用 M05/M07/M08 的租约、投递、确认、取消与有限重试规则。
- 实测真实图片上传到结果查询、ASR/YOLO 两通道、视觉取消和故障恢复；同步启动停止说明与 API 文档。

沿用 Windows、project311、Flask/SQLAlchemy/Pydantic/Alembic、SQLite及本地存储，不新建另一套架构。本阶段不开发第十一部分图片操作页面，不接入 GPU、远程服务器或 LLM，不下载新模型，不修改或发布原架构网页。

## 2. 实施前核查

已重新实查当时的受管平台，管理器未运行、状态为 `stopped`，5000/5173 空闲，API和页面不可达。没有依据旧记录直接停止或接管其他进程。

默认数据库实际为 `20261013_0005`，共11条 ASR 任务（9成功、2取消），上传目录有11个音频对象。已经成对备份数据库与上传文件，并保存各表原始记录、文件 SHA-256 和现有依赖列表：

- 备份目录：`runtime/verification/part10/before-migration-ec9b709c9cf547e485a94d54b3b2d64b/`。
- 核查证据：[before-migration.json](../../runtime/verification/part10/before-migration.json)，`passed=true`。

以上是本阶段准备时的实查。随后真实默认数据库已完成0005→`20261013_0006`，重复`upgrade head`与`alembic check`通过，原11条ASR任务、11个上传音频哈希及全部原表的原有列值保留；新增字段单独核对，不错误地用新增后的`SELECT *`列数否定旧列保留。证据：[migration-check.json](../../runtime/verification/part10/migration-check.json)。新接口和真实视觉任务的独立验证见下方第4节。

## 3. 实现和契约

已实现并按第4节验证的契约如下：

- 新增`POST /api/tasks/vision`，multipart单文件、准确模型/可选版本及`confidenceThreshold/iouThreshold/maxDetections`；任务保存视觉参数、图片哈希和EXIF归正后的宽高。
- 模型目录及任务列表新增`taskType=asr|vision|all`，省略仍为ASR，兼容旧页面。单任务、结果、取消共用旧路径并按实际类型处理。
- 新增`GET /api/tasks/{id}/image?variant=original|annotated`。原图返回归正PNG；框图根据已提交检测JSON与受管原图按需生成，不重新调用YOLO，不在Worker阶段持久化大型PNG。因此M06-F03/F04仍不标实现。
- ASR使用`asr.whisper-small`、组件`worker`及`.worker.lock`；Vision使用`vision.yolo-general`、组件`vision_worker`及`.vision-worker.lock`。每类1槽，共2槽；全局最大在途仍20。`/api/runtime`保留ASR的`worker`，新增`visionWorker`。
- 平台管理器扩为API、投递器、两个Worker、前端共5个入口，检查两类模型后等待就绪。前端仅把执行并发的TypeScript字面量限制从1调整为number，未增加图片页面。
- 0006只扩结构；维护命令使用[准确YOLO规范](../examples/yolo-model.json)显式register/enable/set-default，核心规范须匹配锁定执行器。登记和模型文件准备分开，本轮不下载模型。

具体字段、示例与启动方式已同步[接口说明](../接口说明.md)、[使用与故障处理](../使用与故障处理.md)及[模型管理说明](../模型管理说明.md)。

## 4. 验证登记

| 项目 | 当前状态 |
|---|---|
| 环境、进程和实际数据核查 | 已核查准备时平台停止、0005迁移和11条ASR任务，成对备份；第4.3节登记验收结束状态 |
| 成对备份、迁移和原 ASR 数据保留 | 已备份并迁移0006，重复升级及结构核对通过，全部原表原列及11个音频哈希保留 |
| 图片上传、幂等、模型准入与查询 | 真实HTTP上传/重放/异参冲突/跨类型冲突/坏图/禁用兼容与最终回归通过 |
| 队列隔离、Worker身份/锁/运行状态 | 两类真实Worker分别报告独立PID、childPID和模型就绪；能力隔离与故障回归通过 |
| 真实 YOLO 异步执行与 ASR 兼容 | 真实并行提交并执行成功，公开图片4框，纯白图空结果；具体任务见下方 |
| 视觉取消、有限重试、故障恢复和迟到提交 | 隔离数据库的真实模型取消、子进程故障恢复和两次耗尽通过；租约围栏及迟到提交纳入回归 |
| 后台回归与必要的页面兼容检查 | 最终后台780通过/2旧权限跳过；前端38项、构建与1项模拟断网浏览器测试通过 |
| 实际启动、停止、重启保留与最终数据 | 五服务真实启动、重复启动、停止、重启持久化及最终停止通过；旧业务行和上传哈希保持 |
| 用户验收 | 开发侧完成，等待用户验收；尚未记录用户确认或逐题回答 |

### 4.1 默认数据库的真实HTTP任务

[http-acceptance.json](../../runtime/verification/part10/http-acceptance.json)记录`realModels=true`、`passed=true`。先确认API健康、两类Worker均模型就绪，报告中的父/子进程PID各不相同，再同时通过真实HTTP提交ASR和Vision并查询结果。

| 类型 | 真实任务编号 | 观察结果 |
|---|---|---|
| 公开公交车图片 | `f33259e6892f400db9faf8a8b92683c7` | 810×1080，3个person和1个bus，attempt=1成功，观察到created→queued→running→succeeded |
| 公开短中文音频 | `d248bdac65c647039a8a0a2c600ab738` | Whisper CPU/int8_float32，attempt=1成功，原文“欢迎大家来体验打磨院推出的语音识别模型” |
| 640×480纯白图片 | `8540cf7ce69249b390d707f0bdfcba59` | attempt=1成功，detections为空列表 |

两类执行时间确有重叠：ASR为04:04:28.080—04:04:30.747 UTC，Vision为04:04:28.327—04:04:28.602 UTC。它验证本机两类通道可同时工作，不能据此推算吞吐或GPU容量；公开样例不是准确率评估集。

真实HTTP还覆盖：

- 省略类型默认ASR，`taskType=vision/all`分别返回对应模型；任务列表默认ASR和视觉过滤正确。
- 同一图片改显示文件名、相同幂等键返回原任务；更改阈值或把同键用于ASR入口均409冲突；损坏图片返回422，没有伪装成功。
- 原图和框图接口返回810×1080的PNG，分别保存为[original.png](../../runtime/verification/part10/original.png)和[annotated.png](../../runtime/verification/part10/annotated.png)。框图读取已提交JSON，不再推理，不持久化Worker大型产物。
- 维护命令禁用YOLO后，新提交422 `MODEL_DISABLED`，视觉模型目录为空；原幂等重放、JSON结果和框图仍可读取。验证后已重新启用，未删除历史结果。

### 4.2 真实视觉取消、子进程崩溃与有限重试

证据入口：[worker-real-latest.json](../../runtime/verification/part10/worker-real-latest.json)，完整报告位于 `runtime/verification/part10/worker-real-64be3db2dd084a17b7d120282a3ed573/evidence.json`。该验收使用隔离数据库和真实 ONNX 模型子进程，`realModel=true`、`testSubstitutes=false`、`defaultDatabaseAccessed=false`，不向用户默认库写入故障任务。

- 运行取消：任务获得租约并进入running后请求取消，旧child PID 25404确认退出，任务变为cancelled；新child PID 10868重新加载、预热后ready。结果接口拒绝已取消任务；不能把“取消已受理”当作进程已停止。
- 一次故障恢复：主动结束本次验收拥有的child 10868，第一次记录`WORKER_CHILD_EXITED`并进入retry_wait；新child 9736接手第二次执行，真实输出4个检测框并成功。
- 次数耗尽：另一个任务先后经历两次受控child终止，attempt=2后最终failed，没有第三次执行。最大2次包含首次执行。
- 验收结束确认5个受管child均退出、没有残留当前child、Worker锁已释放。这里只验证子进程崩溃时存活Worker的恢复，不能说已经真实验证Vision父进程崩溃后的集群接管。

故障脚本为加快隔离验收，将重试等待设为0.25秒、轮询0.05秒，未修改日常默认配置。租约、运行状态和真实进程终止分别核对；没有用模型替身或人为延迟模型证明取消。

### 4.3 五服务启停、重启和原数据保留

[restart-acceptance.json](../../runtime/verification/part10/restart-acceptance.json)记录：重复start保留同一管理实例及5个服务PID；stop确认所属进程退出、5000/5173释放；再次start产生新实例，两类Worker重新加载真实模型并ready。重启前后的3条新任务与结果逐值相同，原图/框图PNG字节和幂等重放一致。

最终已停止平台，管理器状态为`stopped`，API、投递器、ASR Worker、Vision Worker退出码均0，前端包装进程退出码4294967295；端口释放和进程退出另行确认，没有把前端包装进程描述为零退出。此状态是本次验收结束时的观察，阅读时须重新运行状态脚本。

默认数据库最终14条任务：12条ASR（10成功、2取消）和2条Vision（均成功），没有非终态任务。ASR及YOLO各有一个启用的默认版本。全部原业务表旧行原列值、11个原音频上传哈希保持不变；本阶段Python依赖全集及版本未改变。隔离故障任务不计入这14条。

### 4.4 检查中发现的问题与处理

- 首次受限工具环境启动因已有模型文件读取权限失败，在服务启动前结束；使用正常Windows用户权限后真实模型及整套启停通过。没有修改ACL、重新下载或替换权重。首次失败记录保留。
- 原迁移回归用`SELECT *`比较，因新增输入列产生列数差异；调整为逐项比较所有历史列，再独立验证新列及约束，没有删除旧数据或降低保留要求。
- 最终后台计数以本轮完整报告为准，不累加定向测试或未收齐新增文件的初次全量结果。普通替身测试与以上真实模型验收分别记录。

### 4.5 最终回归、检查与证据边界

- 后台最终完整回归：**780项通过、2项跳过，149.25秒**，与782项收集数一致。见[backend-tests-final.txt](../../runtime/verification/part10/backend-tests-final.txt)。两个跳过仍是旧Windows符号链接创建权限1314，目录联接点防护通过；并非视觉测试跳过。初次750通过/2跳过未收齐最后新增的30项，不作为最终计数，也不和本轮相加。
- 前端：**38项通过（4个测试文件）**，TypeScript检查与生产构建通过；仅`unavailable.spec.ts`的**1项模拟断网浏览器测试**通过。临时Vite已停止，5173释放，见[frontend-stop.json](../../runtime/verification/part10/frontend-stop.json)。本轮没有图片页面，也未重跑真实网页推理端到端；真实模型全链路证据是第4.1节双模型HTTP，不能混称浏览器真实推理验收。
- [final-checks.json](../../runtime/verification/part10/final-checks.json)通过：文档链接、92个函数ID唯一、后台三级布局与绝对导入、`pip check`无冲突，以及平台停止/端口释放/HTTP不可达。
- 普通单元和集成测试中使用的模型替身只验证流程；本记录明确列出的真实ONNX/Whisper任务另有实际进程与结果证据。样例通过不证明准确率、GPU兼容、显存准入或并发容量。

## 5. 需要理解

1. 为什么ASR和图片任务可以共用租约、重试和取消规则，却要使用两个能力队列与独立Worker？
2. 取消请求已被接受，为什么还要确认模型子进程退出，才能把执行槽位交给下一个任务？

## 6. 后续授权记录

2026-10-13，用户在本部分交付后明确要求继续，现按开发计划进入第十一部分“图片检测操作页面”。本记录的原始实证与交付状态保留为当时事实；继续指令不等于已记录逐题理解回答。
