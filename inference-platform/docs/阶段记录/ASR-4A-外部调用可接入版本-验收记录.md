# ASR-4A：外部调用可接入版本验收记录

- 计划：v2.8；当前授权仅 ASR-4A，用户确认“确定，实施此计划”，后续“继续”为本轮收尾。
- 基线核查及配套备份时间：2026-10-15T13:31:48+08:00。
- 开发侧交付登记：2026-10-15T14:14:26+08:00（北京时间）；不计算总开发工时。
- 用户验收：待明确确认。ASR-4B真实调用、ASR-4C受管启停未开始。

## 实现与关键决定

ASR-4A外部调用可接入版本已完成开发侧交付，待用户验收并暂停。后台全量1314通过/2项旧Windows符号链接权限跳过；全量启动后最后停止竞态修复另行定向54通过，分列不累加。前端冻结全量240通过、类型检查/构建通过，Edge隔离浏览器2项通过。真实本机Whisper短中文转写及独立/整平台停止通过，36份历史成功结果逐项一致；原38任务/38上传保持，当前39任务/39上传、9受管会话均退出确认。0010首次/重复迁移与结构检查通过。外部协议仅由隔离HTTPS和真实Windows子进程验证，课题组真实调用及远端启停尚未验证；日常未配置外部模型，remote_asr_calls为空。平台已停止、锁释放、5000/5173空闲。没有新增依赖、下载模型或扩大20MiB/300秒限制；既有阶段用户验收不变。

1. 固定 `remote-v2-sensevoice` 适配器调用 Remote-V2 文件接口，验证健康/就绪、转换16kHz单声道PCM16 WAV、转换全文/分段。API版本只记为接口元信息；不伪造语言、概率、硬件和模型权重版本。原Whisper结果继续兼容。
2. 复用原模型登记、任务、幂等、上传、投递、租约及结果提交，增加独立能力队列和单槽外部调用Worker。它是本机HTTP调用进程，不是远程Node Agent，不拥有远端启停权限。未配置仍为原五入口，完整配置才增加第六入口。
3. 0010新增调用事实表及运行类型约束，发送前事务预留。只在领取前可取消；领取后409且不自动重试。断线/超时/无法确认结果时持久化未知并阻断此通道，重启不能清除；管理员须先核对远端结束并留下说明，才可解除占位，旧失败结果不改写。
4. 平台停止先持久化停止意图，阻止新受理/领取，等待当前调用在原期限内收尾。停止意图不会被错误或就绪报告覆盖。只有本机进程确认退出才完成本机关闭；远端结果未知仍保留，不声称远端停止。
5. 页面区分未配置、不可达、就绪、调用中、停止中和结果未知，外部区没有远端启停按钮；外部故障与本机Whisper门禁独立。外部提交不传语言参数，明确提示音频发送给配置服务。

## 实际验证

| 类别 | 结果与证据 |
|---|---|
| 后台全量 | 1314通过、2权限跳过，JUnit套件时间558.43秒；[输出](../../runtime/verification/asr4a/backend-verified.txt)、[JUnit](../../runtime/verification/asr4a/backend-verified.xml)。跳过仍是已有Windows符号链接创建权限测试 |
| 最后停止竞态 | 全量运行开始后修复同会话error覆盖stopping，定向54通过、33.51秒；[原工具输出](../../runtime/verification/asr4a/stopping-regression.txt)。不累加成新的全量数 |
| 前端 | 最终冻结全量240通过、18文件、13.84秒；[测试](../../runtime/verification/asr4a/frontend/final/unit-tests-verified.txt)、[类型检查和构建](../../runtime/verification/asr4a/frontend/final/build.txt) |
| 浏览器 | Edge2项隔离场景通过，13.69秒；含外部上传/结果/TXT下载、排队取消、领取后取消不可用、未知阻断、本机门禁独立和未配置无假模型；[报告](../../runtime/verification/asr4a/frontend/final/playwright-results.json)。上游和API响应为测试替身，非真实SenseVoice |
| HTTPS与进程 | `tests/e2e/test_remote_asr_processes.py`三场景：本机可信测试CA、真实HTTP子进程、成功排空/重启继续、断线后未知且不重发、正式child_entry及CTRL_BREAK排空与Job归零。包含在后台全量内；上游返回测试文字，不是模型推理 |
| 真实默认库迁移 | 0009→0010、重复upgrade、alembic check、SQLite完整性/外键和全部原表行保持通过；[迁移报告](../../runtime/verification/asr4a/migration.json)。迁移前数据库和上传成对备份在同目录backup下 |
| 真实本机兼容 | API/页面可达、外部显示未配置、36份旧成功output逐值一致；现有公开短中文音频经本机Whisper成功，任务`eff6f7079822419b8f79c54fb35ad077`；[HTTP证据](../../runtime/verification/asr4a/live-smoke.json)。这是本机Whisper证据，非外部模型证据 |
| 真实停止/保留 | ASR退出确认、整平台所属进程确认停止、端口释放、原38任务/38上传及关联档案/文件哈希保留；当前39任务/39上传、9受管会话；[最终核对](../../runtime/verification/asr4a/preservation.json)、[平台停止](../../runtime/verification/asr4a/platform-stop.txt) |

后台命令：在开发目录、现有project311下执行 `python -m pytest tests -q -p no:cacheprovider --basetemp runtime/tmp/asr4a-full-verified --junitxml runtime/verification/asr4a/backend-verified.xml`。前端和浏览器具体执行记录保留在上述证据目录。不把定向、全量和重复测试相加。

## 发现的问题与保留过程

- 初次0010测试发现Alembic约束名称重复前缀，已使用准确已命名约束修复；首次失败报告保留为core-1，修复后定向及全量通过。
- 第一轮全量为1312通过、1失败、2跳过：新增测试将字典按对象属性读取，已修正；随后补齐领取时排队超时释放未发送记录的分支和测试，再执行本次全量。
- 最终独立审查发现停止意图可能被error报告覆盖，已修复并用54项相关回归复验；该测试晚于全量收集，分列记录。
- 首次浏览器隔离路由过宽拦截了页面模块，修正为只拦截API后两项通过；原失败截图/trace保留。
- 首次真实平台启动受到工具沙箱读取现有模型目录限制；以现有Windows权限复核成功，没有修改ACL或下载模型。启动失败报告platform-start与成功报告platform-start-verified均保留。

## 运行方法与剩余条件

日常仍在开发目录运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-platform.ps1`，页面地址 `http://127.0.0.1:5173/`。本机ASR通过页面或 `scripts/asr-service.ps1` 显式启动；整平台用 `scripts/stop-platform.ps1` 停止。

当前外部配置保持空，不能直接转写课题组服务。取得实际HTTPS根地址、访问/鉴权要求、可信CA与可追溯部署版本后，再获准进入ASR-4B；具体配置、模型登记和未知结果核对命令见[外部ASR接入说明](../外部ASR接入说明.md)。保持默认TLS验证；接口资料中不安全示例不作为本平台操作方式。

受管启停另需部署材料、权限和唯一管理者约定，留在ASR-4C。无真实服务器/GPU联调，无完整账号鉴权、自动多节点调度、容量压测或新YOLO/LLM功能。现有管理令牌不等于完整访问保护。

## Multica与交付边界

2026-10-15已完成v2.8集中同步：BING-3保持in_progress（revision=3），BING-7由backlog更新为in_progress（revision=2）。最后写入读回`2026-10-15T14:15:38+08:00`，完整批次核对`2026-10-15T14:15:45+08:00`；未新建任务。BING-2/4/5/6、评论、项目元数据及资源均保持，runs无新增，teacher daemon仍停止。2条本地基线/交付事件已归档，deferred_updates及pending为空。本地与远端计划均v2.8，v2.6/v2.7原快照及时间保留；ASR-4A待用户验收，4B/4C待条件，未标done。 详见[读回报告](../../runtime/verification/asr4a/multica-batch/verification.json)和[任务映射](../multica-sync.json)。

理解检查：为什么外部接口同步返回，平台仍保留任务队列？为什么本机进程退出或连接中断，不能证明远端请求已结束？为什么API版本14.2不能直接作为模型权重版本？本部分交付后暂停。
