# 本机 ASR 独立启动与安全停止

本说明覆盖本机Agent管理、服务管理页面和ASR-3B的完整演示步骤。ASR-2B与ASR-3A历史交付分别见[Agent记录](阶段记录/ASR-2B-独立启动与安全停止-验收记录.md)、[页面记录](阶段记录/ASR-3A-服务管理页面-验收记录.md)。ASR-3B本轮真实浏览器闭环、在途排空及整平台重启已完成开发侧验证，仍待用户验收，详细结果按[开发进度](开发进度.md)登记。已有音频上传、结果、图片及未配置LLM页面继续保留。

## 1. 谁负责启动模型

平台管理器启动 API、投递器、Node Agent、Vision Worker 和页面五个入口。Node Agent 常驻本机，只管理已登记的一个 CPU ASR 实例；平台启动成功不代表 Whisper 已加载，ASR 默认等待显式启动。

Agent 在自身进程中复用原 `WorkerService` 的领取、租约、取消和结果提交逻辑，Whisper 推理仍在独立的受管子进程中运行。停止 ASR 时退出的是这个模型子进程，Agent、API、页面和 Vision 可以继续运行。Agent 不是 Multica 的执行 Agent，两者没有绑定关系。

原 `compute_nodes/service_instances/instance_sessions` 登记和两条旧观测历史保留。0009 增加 `agent_controls` 与 `managed_instance_sessions`：前者保存当前管理意图和报告，后者保存每次受管启动及真实退出确认。旧观测不能补写为受管退出证明。

## 2. 在 Windows 上操作

数据库须完成配套备份与 `alembic upgrade head`；当前版本为ASR-2B已验证的 `20261014_0009`，ASR-3A没有新增迁移。日常使用已有环境和本地模型，不重建环境或重新下载。

```powershell
Set-Location -LiteralPath 'D:\1 研究生\aixm\inference-platform'
$projectPython = 'C:\Users\86199\miniconda3\envs\project311\python.exe'
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check-environment.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-platform.ps1
```

### 使用页面启动和停止

1. 打开 <http://127.0.0.1:5173>，进入“音频转写”。在“ASR 服务管理”中核对真实计算节点、ASR实例及其模型版本。登记信息不表示模型已运行，首次应看到“未启动”。
2. 在本机编辑器打开项目 `.env`，复制 `INFERENCE_AGENT_TOKEN` 的值，填入“本机管理令牌”密码框。不要发送到聊天，也不要改成前端构建变量。令牌只在当前页面内存；切换工作台、节点或实例，退出或刷新页面都会清除，不写localStorage、sessionStorage或URL。
3. 点击“启动 ASR”。“命令已受理”只说明后台收到意图；等待“模型加载中”转为“可以转写”，再提交音频。若模型已加载且正在执行，页面显示“正在执行”；这与模型加载中不同。
4. 选择已有 `runtime/samples/asr_example_zh.wav`、Whisper small及中文，提交后查看全文、分段和下载文件。当前任务链接跳转到原任务详情，后台仍使用原能力队列、租约和结果规则。
5. 点击“停止 ASR”，等待“停止中”转为“未启动”。停止后不接收和领取新任务，当前任务按原规则完成后退出；有过本次启动的实例由Agent确认进程退出后才报告已停止。需要核对具体退出时间时，可用下方状态命令查看 `exitConfirmedAt`。
6. 保持ASR停止，重新下载刚才的TXT，刷新任务链接，确认仍能读取原结果。切到“图片检测”，核对Vision仍就绪；切回音频后重新填写令牌，点击“启动 ASR”，再次等待“可以转写”。这是一次新的模型启动会话，原节点、实例和任务编号不会因此重建。
7. 如需结束演示，先再次停止ASR并确认“未启动”，再按下文停止整个平台。只关闭网页不会停止ASR，也不会取消后台已受理的离线任务。

页面正常情况下约每2秒读取一次管理状态，并将收到的状态限制在10秒浏览器观察有效期内；隐藏页面、切换工作台、读取失败或超过有效期时放弃旧的可用结论，恢复后重新查询。这些时间只是页面刷新与观察规则，不是Agent心跳周期，也不是进程停止证明。连接断开显示“状态未确认”，停止意图下无退出证据显示“停止未确认”，不会因此开启新提交或启动替代实例。

### 演示“当前任务做完再停”

短样例可能在点击停止前已经完成。要观察排空，可使用已保存的 `runtime/verification/asr2b/repeated-public-sample.wav`：它是公开中文样例重复8次形成的44.3735秒音频，没有下载新内容，仍在原有20MiB/300秒限制内。

1. ASR就绪后提交这段较长音频，记下任务A编号。等待管理区显示“正在执行”，且“当前任务”指向A，再继续操作。
2. 在A仍执行时提交短中文样例作为任务B，记下编号并确认它正在排队，然后点击“停止 ASR”。页面应显示“停止中”，新音频提交不可用；A继续按原规则完成，B不再被领取。
3. 等待A结果保存且管理区显示“未启动”，查看或下载A的结果。B保留原编号及首次受理时的排队、总期限；停止服务不等于取消B，也不会暂停计时。期限已到时可能进入超时终态，这是原有规则，不能保证重新启动后仍会执行。
4. 如B仍在有效等待期限内，再次启动ASR并等待就绪，观察B继续按原队列规则执行；随后核对原A结果仍可下载。演示结束后正常停止ASR及整个平台。

如果观察时A已完成或B已被领取，本轮没有覆盖“一个执行、一个等待”的情形；如需重试，用正常页面新建任务并如实记录，不改数据库状态。任务取消、超时、模型故障仍可能让A以相应终态结束，“等待当前任务”不是保证一定转写成功。

需要对照后台证据时，运行 `asr-service.ps1 -Action status` 查看 `state`、`workerSessionId` 和 `exitConfirmedAt`。当前模型会话退出后才有对应退出时间；再次启动应使用新的 `workerSessionId`。`childPid`可能是历史值，不应用它手动结束进程。任务详情接口保留 `queueDeadlineAt`、`totalDeadlineAt`，前后对比可核对原期限没有被重启延长。

### 使用CLI管理

CLI仍可直接使用，不需要把管理令牌写入命令：

```powershell
& $projectPython -m backend.modules.agent.agent_cli status
& $projectPython -m backend.modules.agent.agent_cli start --wait-seconds 180
& $projectPython -m backend.modules.agent.agent_cli status
```

直接调用CLI的`start`默认只提交启动意图；`--wait-seconds 180`用于等待目标状态，允许0—600秒。等待超时不撤销意图、不强行杀进程，继续查询管理状态；只有报告新鲜、模型已就绪，才提交音频。

只停止 ASR：

```powershell
& $projectPython -m backend.modules.agent.agent_cli stop
& $projectPython -m backend.modules.agent.agent_cli status
```

`stop` 提交停止意图。若当前正在转写，等待其按照原有超时、取消、租约和结果提交规则收尾；查询确认模型进程已退出后，才算本次停止完成。想再次转写时重新执行 `start` 并等待就绪，不需要重启 API、页面或 Vision。

停止整个平台仍使用原入口：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop-platform.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\status-platform.ps1
```

整个平台停止也要通过 Agent 确认 ASR 退出。命令等待超时不等于进程已经停止，继续查询当前状态，不要通过结束所有 Python/Node 进程、删除锁文件或手动改数据库绕过确认。取消某个任务与停止整个 ASR 实例是两种操作，已有取消入口保持。

### Windows简化入口

也可直接使用新增的PowerShell包装脚本，不需要记完整Python模块名：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\asr-service.ps1 -Action start
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\asr-service.ps1 -Action status
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\asr-service.ps1 -Action stop
```

每次按需要执行一条。脚本默认 `-WaitSeconds 180`，范围0—600；`status`立即查询，start/stop等待目标状态，设置0则只提交意图。等待超时仍保留启停意图，不强行中断当前音频。已有Python路径可通过 `-PythonPath` 指定，脚本不安装依赖或下载模型。

## 3. 状态与任务行为

| 管理状态 | 如何理解 |
|---|---|
| `stopped` | 当前没有运行中的受管 ASR；有历史启动时须有退出确认，不能仅凭心跳缺失判断 |
| `starting` | 已受理启动意图，等待进程启动或模型加载；可能尚无受管启动会话 |
| `ready` | 当前报告有效、模型已加载且可领取任务 |
| `busy` | 模型已加载，正在处理已有任务；不等于模型未加载 |
| `stopping` | 已关闭新任务受理和领取，正在等待当前任务或退出收尾 |
| `stop_unconfirmed` | 尚不能确认受管进程已退出；不得宣称停止成功或启动替代进程 |
| `failed` | 启动或运行发生错误，查看安全错误摘要后处理 |
| 未知或心跳过期 | Agent 当前状态无法确认；不是停止成功，也不能视作空闲 |

停止意图、新任务受理和领取共用 SQLite 事务边界：意图生效后不再接受或领取新的 ASR 任务。已经排队的任务保留原编号、幂等记录和原截止时间，重启不重新计时；期限到达后仍由原恢复规则处理。已成功的历史结果继续查询和下载。相同内容的幂等重放仍返回原任务，不当作新的提交。

当前任务的取消、超时或子进程失败继续按原规则处理；已经要求停止时，不为继续接单而无必要地重建模型进程。整个流程仍使用原 ASR 能力队列和 M05 执行租约，不建立另一套任务状态机。

## 4. HTTP 管理接口与局部保护

HTTP 查询使用 `GET /api/instances/{instanceId}/management`，受管启动历史使用 `GET /api/instances/{instanceId}/management/sessions`。管理写操作只有固定的 `POST .../management/start` 和 `POST .../management/stop`。

HTTP 启停需要在本地 `.env` 或进程环境配置 `INFERENCE_AGENT_TOKEN`，长度为 32—256 个字符。请求头是 `X-Management-Token`；JSON 请求包含最近一次状态查询得到的 `agentSessionId` 与 `expectedRevision`。实际字段和错误见[接口说明](接口说明.md)。不要把真实令牌放进文档、聊天或截图；CLI 直接使用本机受控维护入口，不向外发送令牌。

两个管理POST返回202只表示意图已受理，页面继续GET核对实际状态，不乐观地显示就绪或已停。停止未确认时重复stop也返回409并明确保留未确认，不伪装成成功。Agent会话变化、命令修订冲突或管理请求响应丢失时，页面只读查询，不自动重发POST；核对状态与错误后再决定是否手动重试。API 不接受任意 shell 命令、PID、模型文件位置或另一台机器地址。该令牌仅保护管理写操作，13A 登录、完整 API 访问保护和多用户权限仍暂停，不能把本机服务公开为生产平台。

## 5. 故障与维护边界

- Agent未运行或心跳过期：先查整个平台和Agent日志。历史childPid可以保留在记录里，但不代表该PID当前仍属于本实例，也不能据此控制进程。
- ASR 未启动或加载中：等待真实就绪后再提交新音频，历史结果读取不受影响。
- 旧管理命令或修订冲突：点“刷新服务状态”，核对操作是否已经生效。只读确认后再手动决定是否重试，不能重复发送旧会话命令。
- Agent重启后发现旧模型会话退出未确认：这与普通命令版本冲突不同；即使Agent重新在线，也会保持“停止未确认”并阻止新模型进程。保留日志和会话记录交由维护排查，不通过多点几次启动或重启整个平台绕过。
- `stop_unconfirmed`：保留当前会话和锁，核查受管退出证据；不能删记录来恢复启动。Agent异常死亡留下未确认会话时，当前实现保守阻断，没有自动接管或清理流程；需先查日志并人工核对，不能删锁、填确认时间或改库绕过。
- 已登记实例不能通过旧独立 ASR Worker 入口绕开 Agent。原 `observe-local` 只用于旧观测来源；托管后拒绝把 Agent 报告写成 `legacy_worker_observation`。

ASR-3A历史验证包含真实页面启动/就绪、公开中文转写、独立停止、历史TXT下载和刷新兼容；当时的停止未确认、加载失败、过期/断网页面提示由模拟测试验证。ASR-3B本轮真实浏览器/模型、隔离故障及Windows测试进程分别登记，不能把模拟故障或历史报告写成本轮真实模型故障。没有为演示破坏模型文件或伪造任务结果。

2026-10-14的ASR-3B开发侧实测完成了页面多次启动/就绪、真实转写与下载、执行中停止、后续任务保留及再次启动后继续执行；ASR停止期间Vision真实检测和历史结果仍可用。3个本轮模型进程句柄均确认退出，整平台退出/再启动后ASR仍默认停止；32份原成功结果经HTTP逐值核对保持。11:32:24最终核对时平台已停止、5000/5173空闲；这不是之后任何时刻的实时状态，使用前仍须查询。

当前限于一个Windows本机CPU ASR实例；GPU、多节点、自动扩缩容、Docker和课题组服务未接入，LLM仍未配置。管理令牌不是完整账号系统，未确认旧会话没有自动接管/清理能力。ASR-3B开发侧闭环验收通过并交付后才集中同步Multica；用户明确验收前仍保持待验收，具体同步结果以本地映射与读回记录为准。
