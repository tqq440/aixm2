# 第四部分：队列与 Worker 自动执行验收记录

- 日期：2026-10-12。
- 本部分目标：用户通过 HTTP 上传后，后台依次完成通知投递、任务领取、真实转写和结果持久化。
- 涉及架构模块：M02 任务接口、M05 任务档案与执行租约、M06 受管文件、M07 通知队列、M08 首次投递和发布补投、M12 Worker；沿用 v2.1 职责划分。
- 实现状态：代码已实现；304 项自动测试通过、2 项跳过；真实 Whisper 异步链路已验证；用户确认待进行。
- 实际工时：未记录，不推算。下文的秒数仅为实测测试、模型与接口耗时。

## 完成了什么

### 输入、状态与输出

输入仍是第三部分的音频上传接口。提交事务保存任务、幂等记录和待投递事件；API 不执行模型。投递器读取事件，将通知保存到 SQLite，再由 Worker 主动领取。

正常任务依次经过 `created → queued → running → succeeded`。执行错误可保存为 `failed`，任务详情包含错误摘要。成功结果保存全文、分段、模型版本、实际设备和耗时，由新增的 `GET /api/tasks/{task_id}/result` 返回；未完成返回 409 `RESULT_NOT_READY`，失败返回 409 `TASK_FAILED`，不存在返回 404 `TASK_NOT_FOUND`。

新增 `GET /api/runtime` 读取 Worker 和投递器实际报告，区分未启动、模型加载、空闲、繁忙、报告过期、错误和已停止。`ready=true` 表示当前报告允许接单；繁忙时模型可已加载，但 `ready=false`。停止或报告过期时不把历史 `model_ready` 当作仍可用。此接口没有 GPU 指标或集群容量估计，`GET /api/health` 仍只验证 HTTP 与数据库连接。

### 职责与主要实现入口

| 职责 | 实际类或函数 | 所在层 |
|---|---|---|
| M08 首次投递、发布失败补投 | `DispatchService.tick`、`DispatcherRuntime.tick` | `service/dispatch` |
| M07 持久化发布、领取、延长可见性、确认 | `BrokerRepository.publish/receive/extend/acknowledge` | `data/broker` |
| M05 执行资格、续租、终态裁决 | `ExecutionService.acquire/renew/commit_result/commit_failure` | `service/execution` |
| M05 数据库锁定与执行状态持久化 | `ExecutionRepository.lock_task`、`ExecutionTransaction` | `data/execution` |
| M12 单槽位控制、续期、结果提交 | `WorkerService.start/tick/close` | `service/worker` |
| 模型加载、音频校验、真实推理 | `inference_child`、`AsrService.prepare_model/transcribe` | `service/worker`、`service/asr` |
| 受管进程与退出确认 | `ManagedJsonProcess`、Windows Job Object | `common/process` |
| 结果与状态查询 | `TaskService.get_result`、`RuntimeService` | `service/tasks`、`service/runtime` |
| 三个独立运行入口 | `modules/api/run.py`、`modules/dispatcher/task_dispatcher.py`、`modules/worker/asr_worker.py` | `modules` |

入口只组织生命周期、信号和循环，业务规则在 Service，持久化操作在 Data；新增后端文件遵守三级目录与绝对导入。前端、父目录设计文档及原架构展示网页未改动或发布。

### 本阶段保证与边界

1. **首次投递原子性：**通知写入、事件 `published` 和任务 `created → queued` 在同一个 SQLite 事务内完成；已有 `running` 不回退。发布失败保留事件并安排补投，默认等待 5 秒；补投失败状态也会显示到运行报告。
2. **领取不等于执行：**Worker 先读取通知，再申请执行租约。数据库事务检查通知当前领取凭据、任务状态、执行计划版本和投递版本，只有获准者能执行。重复通知不能让同一次有效执行启动两次。
3. **过期凭据失效：**续租和提交检查租约、Worker、尝试次数、版本以及数据库时间。过期或不匹配的提交被拒绝。同一租约已保存的相同结果可幂等重交；不同内容不能覆盖终态。
4. **先保存，再确认：**结果提交异常时保留待提交结果，不再次运行模型，不提前确认通知。已提交但回执未知时可再次提交相同结果。通知确认失败不回退任务终态。
5. **推理与控制分开：**Worker 启动受管子进程并完成模型预加载，再接收任务；模型进程复用。Worker 主进程负责续租、延长通知可见性、状态报告和停止检查。
6. **资源释放需要实际停止：**单数据库使用独立 Worker 文件锁，执行槽位为 1。Windows Job Object 配置关闭即结束所属子进程；控制逻辑只有确认子进程结束才释放槽位。无法确认停止时保持不可接单。

**尚未实现：**执行失败自动重试、过期租约接管、等待/执行总期限、取消接口、最大在途数 20 的准入限制、完整故障恢复、前端、GPU 与多节点调度。强退、持续数据库故障或租约过期后，任务可能保持 `running`；重启 Worker 不会自动恢复，恢复规则属于第六部分。M08 发布失败补投不等于执行失败重试。

## 如何运行

沿用 `C:\Users\86199\miniconda3\envs\project311\python.exe`，Python 3.11.16。本部分未新增依赖、未重建环境、未下载模型，`pip check` 通过。模型路径、上传目录、数据库等配置仍由同一份 `AppSettings` 加载。

### 迁移和启动

本机已完成迁移；在 API、投递器和 Worker 停止时，可重复执行以下检查：

```powershell
Set-Location -LiteralPath 'D:\1 研究生\aixm\inference-platform'
$projectPython = 'C:\Users\86199\miniconda3\envs\project311\python.exe'
& $projectPython -m alembic upgrade head
& $projectPython -m alembic current
& $projectPython -m alembic check
```

当前迁移为 `20261012_0003`。新增 `notification_records`、`runtime_components` 两张表，给已有任务增加租约、结果和失败字段，给事件增加通知及投递记录。真实迁移前通过 SQLite 备份 API 保存了 `runtime/verification/part4-before-migration-e4a1729a34224db0b3ec1fa34c99dc3c.sqlite3`，原上传文件保留；未手工改表，应用启动不自动迁移。恢复时应同时考虑配套上传文件，不能随意降级有结果的数据库。

打开三个 PowerShell 窗口，每个先设置上面的工作目录和 `$projectPython`，再各执行一条：

```powershell
# 窗口 1：HTTP API
& $projectPython -m backend.modules.api.run
```

```powershell
# 窗口 2：投递器
& $projectPython -m backend.modules.dispatcher.task_dispatcher
```

```powershell
# 窗口 3：Worker；它自行创建推理子进程
& $projectPython -m backend.modules.worker.asr_worker
```

不需要手动运行模型子进程。已有未执行任务也会被处理。现有 `scripts/start-api.ps1` 只启动 API。Worker 仅支持本阶段验证过的 Windows；默认只监听本机 `127.0.0.1:5000`，没有生产部署或账号认证。

在第四个窗口按 [README](../../README.md) 上传并取得 `$taskId`，再查询：

```powershell
Invoke-RestMethod 'http://127.0.0.1:5000/api/runtime' | ConvertTo-Json -Depth 8
Invoke-RestMethod "http://127.0.0.1:5000/api/tasks/$taskId" | ConvertTo-Json -Depth 8
Invoke-RestMethod "http://127.0.0.1:5000/api/tasks/$taskId/result" | ConvertTo-Json -Depth 12
```

日志为 `runtime/logs/api.log`、`dispatcher.log`、`worker.log`、`worker-child.log`。运行状态保存历史 PID；`stopped` 中仍有 PID 不代表进程仍在运行。

### 停止

要保留当前任务的正常结果，先等它完成，再在 Worker 窗口按 Ctrl+C，等待命令提示符重新出现，随后停止投递器和 API。Windows 的 Ctrl+Break 也支持。不要结束全部 Python 进程。

处理中停止 Worker 会先结束并确认子进程退出；租约仍有效时，将尚未完成的执行保存为 `WORKER_STOPPED`。若结果已到达则优先尝试保存结果。租约已失效时不能伪造有效失败提交。这是进程关闭处理，尚不是用户取消功能。

### 默认控制配置

| 配置后缀（均加 `INFERENCE_` 前缀） | 默认秒数 |
|---|---|
| `LEASE_SECONDS` / `RENEW_INTERVAL_SECONDS` | 30 / 10 |
| `NOTIFICATION_VISIBILITY_SECONDS` | 30 |
| `RUNTIME_HEARTBEAT_SECONDS` / `RUNTIME_STALE_SECONDS` | 2 / 15 |
| `POLL_INTERVAL_SECONDS` | 0.25 |
| `DISPATCH_RETRY_SECONDS` | 5 |
| `WORKER_STARTUP_TIMEOUT_SECONDS` / `WORKER_STOP_TIMEOUT_SECONDS` | 120 / 10 |

Pydantic 同时校验范围和间隔组合：续期间隔的两倍小于租约及通知可见性期限，心跳间隔的两倍小于报告过期时间，轮询间隔不大于续期及心跳间隔。停止超时是一次停止确认的等待预算，不是无条件释放槽位的许可。

## 验收证据

### 自动测试

实际最终命令：

```powershell
& $projectPython -m pytest -q --basetemp=runtime/tmp/pytest-part4-final --junitxml=runtime/verification/part4-tests.xml
```

结果为 **304 passed, 2 skipped in 46.00s**。两项跳过均因当前 Windows 账户不能创建符号链接；没有调整系统权限。已有目录联接点拒绝测试通过。测试使用临时数据，不下载模型。

| 检查项 | 实际执行方法 | 实际结果 | 证据位置 |
|---|---|---|---|
| 前四部分回归、目录和配置 | 完整 pytest | 304 通过，2 跳过 | [最终 XML](../../runtime/verification/part4-tests.xml) |
| 迁移和旧记录保留 | 临时 SQLite 升级、降级与约束测试 | 通过；默认数据库升级后结构一致 | `tests/integration/test_migrations.py`、真实验收进程日志 |
| 通知和发布失败补投 | 数据库事务、重复领取、确认与故障注入 | 通过，不回退已执行任务 | `tests/integration/test_broker_dispatch.py` |
| 租约、并发领取、过期提交、重复结果 | 真实 SQLite 与可控数据库时钟 | 通过，旧凭据不能覆盖结果 | `tests/integration/test_execution_leases.py` |
| 结果及状态接口 | Flask 测试客户端；报告过期及投递失败持续展示 | 通过 | `tests/integration/test_execution_api.py` |
| Worker 生命周期与 JSON 契约 | 替身依赖故障注入、子进程事件验证 | 通过，提交异常不重复推理，不提前确认 | `tests/unit/test_worker_service.py`、`test_worker_child.py` |
| Windows 子进程退出 | 实际 spawn 与 Job Object，强制父进程退出检查 | 通过 | `tests/e2e/test_worker_process.py` |
| 完整多进程流程 | 实际三个入口和受管子进程；临时源码副本使用模型替身 | 顺序执行、续租、结果保存、停止及重启通过 | `tests/e2e/test_async_processes.py`、[独立运行 XML](../../runtime/verification/part4-async-process-tests.xml) |

多进程替身测试在 2 秒租约下执行约 3 秒模拟任务，确认续租跨过初始到期时间；它验证流程，不作为真实模型输出证据。独立最终复跑为 1 passed in 19.98s，之后已包含在上述完整回归中。

### 真实 Whisper 异步链路

实际命令为 `& $projectPython -m runtime.verification.verify_part4`，本机使用可发送 Windows Ctrl+Break 的终端执行。此验收辅助程序操作默认数据库，会创建新任务并启动/停止自己创建的进程，不是只读查询命令。

验收时间：2026-10-12 北京时间 19:29:13—19:29:37。完整报告为 [part4-real-pipeline.json](../../runtime/verification/part4-real-pipeline.json)。

- 音频：此前准备的公开短中文 `runtime/samples/asr_example_zh.wav`，177572 字节，时长 5.5466875 秒；来源记录在 `runtime/samples/asr_example_zh.source.json`，没有重新下载。
- SHA-256：`a1bd32dc78493c123f9625a66deee562aed2895f53fbc39f2cca3be7e6f4f20f`。
- 模型：`Systran/faster-whisper-small`，固定提交 `536b0662742c02347bc0e980a01041f333bce120`，复用本地模型。
- 引擎：faster-whisper 1.2.1 / CTranslate2 4.8.2，CPU 4 线程，结果报告实际计算类型 `int8_float32`。
- 模型预加载：Worker 日志记录 2.010 秒。单任务结果的 `modelLoadSeconds=0` 表示复用已加载模型，并非没有加载模型。

两个新任务的原始输出均为：

> 欢迎大家来体验打磨院推出的语音识别模型

保留模型原文中的“打磨院”，未人工改成正确专名。分段为 0.00—5.36 秒，不把该样例当作准确率评测集。

| 新任务 ID | 状态 / 次数 | 推理耗时 | 开始 → 结束（UTC） | 最后租约到期（UTC） |
|---|---|---|---|---|
| `90698d7fda464786907bcb6eabb01246` | succeeded / 1 | 1.5016 秒 | 11:29:28.450 → 11:29:30.002 | 11:29:35.452 |
| `7c50b185491c4e46b58c0cf0a32a458c` | succeeded / 1 | 1.5332 秒 | 11:29:30.015 → 11:29:31.571 | 11:29:37.014 |

为了让短样例实际触发续期，验收进程临时使用租约/可见性 6 秒、续期 1 秒、心跳 1 秒、报告过期 6 秒、轮询 0.1 秒。报告证明最后租约到期时间大于开始时间加 6 秒；没有修改 `env.example` 中 30/10 秒默认值。两个任务无执行时间重叠。

第三部分已有任务 `40e3b17a44e144e8ab68a7b9039b6d27` 在两个新任务前自动执行成功。默认数据库最终共 3 条成功任务、3 条幂等记录、3 条已发布事件、3 条已确认通知，每项任务的 `attempt=1`。

模型执行期间实际轮询健康接口和运行状态，观察到 Worker `busy`；健康请求采样最大约 **23.6 毫秒**。这是本次短样例观测，不代表吞吐能力、压力测试或响应时间保证。

### 停止和重启

| 进程 | 本次 PID | 实际结果 |
|---|---|---|
| API | 22056 | 正常停止，退出码 0 |
| 投递器 | 31212 | 正常停止，退出码 0 |
| Worker | 28276 | 正常停止，退出码 0 |
| 推理子进程 | 31064 | Windows 原始进程句柄确认已结束 |
| 重启后的 API | 30188 | 结果读取与重启前一致，再停止，退出码 0 |

Worker 和投递器停止后，API 曾实际返回两者 `availability=stopped`。API 随后停止，5000 端口释放。PID 仅用于标记这次验收，不应据此在未来结束可能复用这些编号的进程。

进程输出在 `runtime/verification/part4-api-process.log`、`part4-dispatcher-process.log`、`part4-worker-process.log`、`part4-api-restart-process.log`；模型预加载和执行顺序在 `runtime/logs/worker.log`。

## 问题与处理

- Windows 工具沙箱出现 `helper_sandbox_lock_failed`，后续命令通过获准的本机执行完成；没有改动系统 Conda profile、权限或环境。端到端信号验证使用带终端的执行方式。
- 子进程 JSON 配置转换需显式经过 Pydantic `AppSettings`，已处理路径类型转换并加入测试。
- 内部 Worker 状态与 API 报告状态统一映射为模型加载、空闲和繁忙；提交结果但回执未知时先重交同一结果，避免误判续租失败后丢失待提交结果。
- 发布失败后处于退避等待时，投递器仍报告最近的待处理错误；成功补投后清除错误状态，避免等待期间误报完全正常。
- 真实模型运行、进程停止和测试替身结果分别记录，不用单元测试替代真实模型验收。
- 原规范引用的 `0-生命周期规范.mdc` 仍未找到，不声称已阅读。当前目录不是 Git 仓库，未提交或推送代码。

实现参考了 [SQLite 事务](https://www.sqlite.org/lang_transaction.html)、[Python 3.11 multiprocessing](https://docs.python.org/3.11/library/multiprocessing.html) 和 [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) 官方文档；本项目实际能力以以上测试和运行记录为准。

## 用户理解与确认

- 应能解释：API 负责受理与查询，投递器负责送出通知，Worker 申请租约后执行，模型位于 Worker 创建的子进程；数据库保存任务的权威状态。
- 需要补充学习：数据库事务、消息重复投递、执行租约及结果幂等的基本含义；“消息已领取”“有权执行”“进程已停止”是不同事实。
- 需要向老师或师兄确认的问题：本部分未发现阻断项；多节点、GPU 和容量目标留待后续单独规划。
- 理解检查问题：
  1. API 已保存任务，为什么仍需要 M08 投递器发送通知？
  2. Worker 已领到通知，为什么还要向任务档案申请并续期租约？
  3. 租约到期后，为什么必须确认推理子进程已停止，才能释放执行槽位？
- 用户确认：尚未确认。用户授权进入第四部分不代表已确认理解本部分全部内容。

## 下一步

第四部分交付后暂停，等待用户确认或要求继续。下一部分是独立 React、TypeScript、Vite 操作页面：连接真实上传、任务、结果和运行状态接口；不自动修改或发布原架构展示网页。取消、执行重试和故障恢复仍留在第六部分。
