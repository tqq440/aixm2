# 外部 ASR 接入说明（v2.8 / ASR-4B）

**2026-10-19最新进度：**v2.9第1部分文档/源码核查与平台侧方案已完成，待用户验收。用户要求不向师兄询问设计选择，由开发侧决定。采用受控target_id绑定已有服务及已核实设备、固定目标Worker主动领取、目标与共享capacity_group原子准入；首轮同步、目标/组限额1，别名和模型版本不增加容量。现外部地址设备未知，不能标GPU；本机Whisper不能作为SenseVoice的CPU替代。GPU优先及CPU承接尚未实现。 下一项为第二部分的2A“计算目标与容量准入基础”：迁移、目标修订、共享容量组、许可与Worker身份/锁/会话及领取资格；先保留真实通道单槽，以隔离并发测试核对规则。2B再接受控并发执行与页面，第3部分再按实测验证GPU/CPU。当前交付后暂停，不自动开始代码。 见[并发方案](并发与设备分配方案.md)。本轮只交付核查与方案，不把它记为真实并发/GPU能力；本轮已集中同步BING-3/BING-7并完整读回，本地与远端均v2.9；用户验收仍待确认，实际时间见任务映射。下方范围调整及旧交付段落保留历史含义。

**2026-10-19范围澄清（本地计划v2.9）：**模型服务由服务方部署、启动、停止和维护，平台通过已有服务执行任务。ASR-4C退出当前交付范围；源码、模型安装与进程管理权限不是调用和后续并发对接的前置。本文仍描述已交付的v2.8单槽同步实现，不表示已实现GPU选择或CPU承接。已有本机Whisper/Agent功能保留，后续顺序见[开发计划](开发计划.md)。

更新日期：2026-10-19。ASR-4B已完成开发侧交付，待用户验收并暂停。真实浏览器已完成音频上传、单次外部转写、结果查看与TXT/JSON下载；重启后仅GET恢复原任务和结果通过。实际上游版本未知，已按用户授权以自动本地接入修订和versionInfo明确标明。最终平台已停止、5000/5173释放；测试、历史数据保持及首次失败修复证据见[ASR-4B联调记录](阶段记录/ASR-4B-真实服务联调-验收记录.md)。Multica本轮已集中更新BING-3/7并读回，用户验收仍待确认，时间以映射为准。以下TLS例外默认关闭，本次已按用户授权仅对固定服务启用；真实调用成功不等于证书验证通过，也不代表远端启停已实现。

## 1. 本机负责调用，对方负责运行模型

```text
页面上传 → 平台保存音频和任务 → 原投递器/持久化队列
         → 本机外部调用 Worker → 对方 Remote-V2 HTTPS 接口
         → 原任务结果存储 → 页面查看与下载
```

固定适配器为 `remote-v2-sensevoice`，调用 `GET /api/v1/asr/health` 和 `POST /api/v1/asr/transcripts`。表单字段为 `audio`、`outputs=transcript`、`engine=sensevoice`、用于关联的 `request_id`。首轮不用对方异步队列、WebSocket、声纹、翻译、总结或 Paraformer。

对方 HTTP 同步返回结果，但平台仍使用现有异步任务和持久化通知队列，不需要浏览器一直打开。并发固定为 1，能力队列为 `asr.remote-v2-sensevoice`。本机不安装对方的 Linux 环境、Redis、CUDA 或模型；继续使用现有 Windows/project311。

未配置外部服务时，平台仍启动 API、投递器、Node Agent、Vision Worker 和页面五个入口；完整配置后增加第六个 `remote_asr_worker`。它是本机 HTTP 调用者，不是远端模型进程，也不是远程 Node Agent。其启动成功只说明本机调用循环运行，外部是否可用还要检查健康接口。

本机 Whisper 保持原默认入口，由本机 Agent 独立启停。外部页面标为“外部服务，仅可调用”，没有启停按钮；本机“停止 ASR”不会关闭对方服务。

## 2. 配置连接并标明版本来源

在本地`.env`成对配置实际服务地址与平台模型标识。`INFERENCE_REMOTE_ASR_MODEL_VERSION`是可选的实际上游模型/部署版本，未知时省略或留空，不填`unknown`、API14.2或示例版本来代替。以下规则按2026-10-19用户明确授权更新，替代此前三项必须齐全的要求：

| 配置键 | 实际含义 |
|---|---|
| `INFERENCE_REMOTE_ASR_BASE_URL` | HTTPS 服务根地址，例如对方实际提供的 `https://主机:8003`；不附 `/api/v1/asr`，不含凭据、查询串或片段 |
| `INFERENCE_REMOTE_ASR_MODEL_ID` | 本平台的固定模型标识，不能覆盖已有 Whisper 标识/版本 |
| `INFERENCE_REMOTE_ASR_MODEL_VERSION` | 可选；有准确资料时填写对方模型或部署版本，未知时省略或留空。API `14.2` 不是模型权重版本 |
| `INFERENCE_REMOTE_ASR_CA_FILE` | 可选的可信 CA 证书文件；相对路径以开发目录为基准 |
| `INFERENCE_REMOTE_ASR_INSECURE_TLS_ORIGIN` | 默认不配置；经明确授权时填写与外部服务地址匹配的HTTPS origin，仅对该origin跳过证书与主机名验证。不得含路径、凭据、查询或通配符；不能与CA文件同时设置 |
| `INFERENCE_REMOTE_ASR_CONNECT_TIMEOUT_SECONDS` | 默认 5 秒，只限制建立连接 |
| `INFERENCE_REMOTE_ASR_TOTAL_TIMEOUT_SECONDS` | 默认 600 秒，且不能超出当前任务剩余执行/总期限 |

前两项必须全有或全无，上游版本不再必填。未知时系统自动导出`modelVersion=integration-<48位哈希>`，同时附带`versionInfo={"versionBasis":"integration_revision","upstreamVersion":null}`。它是本地接入配置的不可变修订，不是远端模型权重版本，也不能证明服务方没有更新模型。相同连接配置重启保持同一修订；修改地址、平台模型标识、CA文件路径或TLS例外会生成新修订，须重新导出规范并登记。已知上游版本模式保持原规范与指纹；以后补得真实版本时新增登记，既有任务和结果不改写、不补填。

默认严格验证HTTPS证书，可提供可信CA。2026-10-19用户明确授权本次对接跳过校验，因此增加上述按origin绑定的显式例外；不配置该项仍严格验证。origin使用`https://主机[:端口]`，不带尾斜杠；例外只接受ASCII主机、IP或明确的xn--域名，匹配时规范化主机大小写及默认443端口。修改服务地址必须重新核对例外，地址不匹配会拒绝配置。跳过校验会单独参与任务配置指纹，不能用于重定向已经受理的严格模式任务；不跟随重定向、不自动重试，LLM等其他通道不受影响。证书信任和主机名均不再验证，联调结果须注明这个条件。

目前文档没有说明鉴权协议，本版没有假设 Bearer/API Key 等方案。如果真实服务需要认证，先补充协议并完成适配与测试，再做真实联调；不要把密码塞进 URL。服务地址、证书路径和配置指纹不通过页面状态或结果接口返回。

实际校园网地址已提供，2026-10-19按授权跳过校验后健康及同步转写成功，request_id回显已核对；实际模型/部署版本仍未知，但不再阻止配置和登记。回显不等于幂等保证，断线后的执行情况、去重/查询和异常响应仍需分别核对。资料状态见[ASR服务对接清单](ASR服务对接清单.md)。

## 3. 升级与登记

0010 新增 `remote_asr_calls`，并扩展已有 `runtime_components` 的运行类型约束，不自动登记或启用任何外部模型。升级前先停止平台，成套备份 SQLite 数据库和上传目录，再使用既有迁移流程。不要单独复制正在写入的数据库文件；当前实际迁移与旧数据核对结论见本轮阶段记录。

以下命令仅在真实连接配置齐全、数据库迁移完成后使用。打开 PowerShell：

```powershell
Set-Location -LiteralPath 'D:\1 研究生\aixm\inference-platform'
$projectPython = 'C:\Users\86199\miniconda3\envs\project311\python.exe'
& $projectPython -m backend.modules.asr.remote_cli spec
```

`spec`只生成与当前配置匹配的准确登记规范；版本未知时自动生成接入修订和versionInfo，不需要自行编造版本。它不下载、不登记、不验证远端可用。把成功输出保存到UTF-8 JSON后，沿用模型登记命令；不要手改核心规范来绕过适配器校验：

```powershell
$remoteSpecJson = & $projectPython -m backend.modules.asr.remote_cli spec
if ($LASTEXITCODE -ne 0) { throw '请先修正外部 ASR 配置' }
New-Item -ItemType Directory -Force -Path '.\runtime\tmp' | Out-Null
$remoteSpecJson | Set-Content -LiteralPath '.\runtime\tmp\remote-asr-spec.json' -Encoding UTF8
$remoteSpec = $remoteSpecJson | ConvertFrom-Json
& $projectPython -m backend.modules.models.model_registry register --file '.\runtime\tmp\remote-asr-spec.json'
```

登记成功且核对模型标识、版本来源及导出的登记修订后，再依次执行；这里的`$remoteSpec.modelVersion`可以是自动本地接入修订，不要求上游版本已知。每条成功后才进行下一条：

```powershell
& $projectPython -m backend.modules.models.model_registry enable --model-id $remoteSpec.modelId --model-version $remoteSpec.modelVersion
& $projectPython -m backend.modules.models.model_registry set-default --model-id $remoteSpec.modelId --model-version $remoteSpec.modelVersion
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-platform.ps1
& $projectPython -m backend.modules.asr.remote_cli status
```

每个模型标识分别管理默认版本，以上命令不更改 Whisper 的模型定义。未配置时不生成假规范，不向日常数据库加入测试模型。完整启动/停止命令仍见[README](../README.md)。已受理任务绑定提交时的配置指纹；更换连接配置不会把旧任务悄悄发到新地址。切换配置前先处理已有任务及未确认记录。

## 4. 页面与任务规则

页面始终有外部服务说明区；未配置时显示“尚未配置”，不会出现假的可选模型。启用准确外部模型后，刷新模型目录，在“转写模型”中选择带“外部服务”的模型。

版本未知的外部模型应显示“接入修订”和“上游版本未知”。任务、历史详情及JSON结果保留versionInfo，不能仅因当前连接配置补得版本就把旧任务改称已知版本；旧Whisper/YOLO及已知版本结果格式继续兼容。这些页面和结果投影的实际验收以本轮阶段记录为准。

外部模式不受本机 Whisper 启停门禁影响。页面明确提示音频将发送给配置的外部服务；语言由服务端决定，平台不传语言参数。20 MiB、300 秒限制保持，即使对方允许 2 GB 或更长音频，也不能突破本平台上限。

结果按 `details.segments` 转换为原全文/时间分段和 TXT/JSON 下载。没有返回的语言、概率、硬件、计算精度和耗时不伪造；说话人分数不是语言置信度。接口版本只作为 API 元信息，原历史 Whisper 结果继续可读。

任务只在尚未被 Worker 领取时允许取消。领取与取消在同一数据库写事务内裁决；领取后取消请求返回 `409 REMOTE_ASR_CANCEL_UNSUPPORTED`，页面禁用取消并说明原因。外部任务最多尝试 1 次，HTTP 不自动重试或跟随重定向。上传响应丢失时，仍可使用原幂等键确认平台是否已受理；这不等于重新向远端推理。

## 5. 远端结果未知时如何处理

对方没有外部取消接口，也没有承诺 `request_id` 可防止重复执行。后台在发送前事务内保存调用占位；若可能发出请求后遇到断线、超时、进程异常或结果无法确认，任务失败并注明“远端结果未知”。本通道停止新受理/领取，已排队任务仍保留原截止时间，不能自动重发。重启平台或删除外部配置也不能清除这个事实。

本机 API、历史结果、Whisper 和 Vision 继续可用。页面不能直接解除阻塞。管理员应先联系服务维护方，核对对应请求确实结束，再确认本机对应调用也已结束；不要因为健康接口正常就认为旧请求已经退出。具备这些证据后，使用维护命令并保存实际核对说明：

```powershell
& $projectPython -m backend.modules.asr.remote_cli release '<32位任务编号>' --confirm-request-ended --note '填写实际核对依据和结果，不要复制占位说明'
```

命令要求原任务处于失败/未确认状态，且本机对应调用已收尾；否则拒绝。成功只解除占位并保存核对时间/说明，不改写旧失败任务，也不重新发送。重复解除返回 `replayed=true`，不会重复操作。不得删除锁文件或直接改库绕过。

整平台停止会先停止外部新受理/领取，等待当前调用在原期限内结束，再确认本机所属进程退出；不会运行对方 `stop_all.sh`。若远端结束仍无法确认，停止说明会明确“本机服务已停；远端请求停止未确认”，相关通道仍保持阻塞。

## 6. 本轮证据和后续边界

ASR-4A 的测试 HTTP 服务和浏览器替身只存在于隔离测试中，不写入日常真实任务库。前端单元、类型检查、构建及两项隔离 Edge 场景有实际证据，位于 `runtime/verification/asr4a/frontend/final/`；后台全量、迁移、启停和最终计数由本轮阶段记录统一保存，不能套用历史数字。

ASR-4B已完成真实调用闭环的开发侧交付：页面任务`0db13671065e4618b8a7c10476670083`于10:39:00提交、10:39:02成功，上游尝试为1；重启后只读恢复同一任务，TXT/JSON与持久化结果一致，1366和390宽度页面通过检查。第一次浏览器收尾发现的favicon404及长修订文本重叠已修复，首次失败证据保留，复验没有再提交音频。完整测试结果及局限以阶段记录为准；后台全量曾有3项测试夹具失败，修复后定向3项通过，未声称一次全量全绿。本轮没有启动本机Whisper或新增Vision推理，也没有控制远端服务进程。

下一部分为ASR-4C远端受管启停，需要部署包/脚本内容或可信管理接口、机器管理权限、专用或共享服务说明与唯一管理权；仅收到脚本名称不代表已获得启停授权。当前暂停，待用户继续及条件齐备后再实施。本轮未开发GPU、多节点、额外模型、流式字幕或新的访问保护系统。
