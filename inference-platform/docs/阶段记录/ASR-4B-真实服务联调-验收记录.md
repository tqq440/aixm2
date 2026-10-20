# ASR-4B：真实服务联调记录

**2026-10-19交付后的范围澄清：**用户已确认平台最终不负责模型实例启停，当前本地规划调整为v2.9，ASR-4C移出交付范围；下一项为已有服务的并发与设备分配对接核查。本文v2.8实施、测试、真实调用及同步时间保持原证据，ASR-4B仍待用户验收；下方旧版“下一部分ASR-4C”仅作历史记录，不再作为当前计划。

计划版本：v2.8。首次实际连接核验：2026-10-15；本次继续：2026-10-19。

**当前结论：ASR-4B真实调用与平台页面闭环已完成开发侧交付，待用户验收。上游模型版本可选；固定地址TLS例外已获准，不代表严格证书验证通过。后续顺序已按上方v2.9范围调整。**

## ASR-4B开发侧交付（2026-10-19）

实际交付登记：`2026-10-19T10:49:00+08:00`（北京时间，只记录时间点）。ASR-4B已完成开发侧交付，待用户验收并暂停。上游模型版本改为可选；未知时使用稳定的本地接入修订，明确显示“上游模型版本未知”，历史任务和结果继续读取原登记。真实页面上传、外部SenseVoice转写、全文/分段、TXT/JSON下载及平台重启后读取通过；本次平台仅新增1条任务、1次上游尝试。原39任务/39上传/37份成功结果及2个旧模型版本保持，现40任务/40上传/3模型版本，remote_asr_calls仅1条且已释放。两轮平台启动停止通过，最终管理器已退出、5000/5173空闲。本轮未启动本机Whisper，9条旧受管会话保持；Vision完成加载但未新增真实Vision推理。用户验收及旧阶段验收状态不变。

### 本次实现

- 外部地址和平台模型标识仍必填，`INFERENCE_REMOTE_ASR_MODEL_VERSION`可省略或留空。不把API版本14.2、日期或unknown伪造为模型版本。
- 未知时自动生成`integration-...`登记键，并记录`versionInfo={versionBasis: integration_revision, upstreamVersion: null}`；它只追踪接入配置，不能证明服务器权重未改变。以后取得版本可新增登记，不覆盖历史版本或结果。
- 任务列表按批读取不可变模型登记；提交、详情、结果、幂等重放保持原任务身份。已知版本、本机Whisper及旧JSON不增加未知版本字段，无新增表或迁移。
- 页面在模型选择、外部状态、任务和执行信息中区分“接入修订”和真实模型版本。真实浏览器检查发现缺失favicon及版本行负间距重叠，已补本地SVG并修正详情间距。

### 实际测试与真实证据

后台全量首轮1361通过、3失败、2项旧Windows符号链接权限跳过（714.44秒）；失败定位为测试子进程冷启动超过夹具2秒预算，以及正式入口测试误读日常.env导致测试CA与TLS例外冲突。仅修测试夹具：健康预算恢复生产默认5秒，正式入口使用无.env的临时源码副本；其后三项定向全部通过（47.82秒），不宣称一次全量全绿。前端252通过，最终类型检查/构建通过；Edge真实流程复核1通过（11.4秒）。

| 检查 | 结果与证据 |
|---|---|
| 后台首轮全量 | [1361通过/3失败/2跳过](../../runtime/verification/asr4b/backend-version-full-20261019.xml)，保留首轮失败事实 |
| 隔离进程最终定向 | [3通过](../../runtime/verification/asr4b/version-process-isolated-final-20261019.xml)；[冷/暖初始化证据](../../runtime/verification/asr4b/async-cold-start-20261019.json)、[环境隔离证据](../../runtime/verification/asr4b/process-env-isolation-diagnosis-20261019.json) |
| 配置与版本身份 | 先行97项配置/适配器及5项新集成通过，均与全量重叠，不能累加；[97项](../../runtime/verification/asr4b/version-optional-unit.xml)、[5项](../../runtime/verification/asr4b/version-integration-final-20261019.xml) |
| 前端 | [252通过](../../runtime/verification/asr4b/frontend-version-tests-complete-20261019.xml)，[最终类型检查与构建](../../runtime/verification/asr4b/frontend-build-final-20261019.txt) |
| 真实Edge | [最终报告](../../runtime/verification/asr4b/browser-live/external-asr-live-report.json)，[首次提交回执](../../runtime/verification/asr4b/browser-live/submitted-task.json)，[1366详情](../../runtime/verification/asr4b/browser-live/external-detail-1366.png)、[390详情](../../runtime/verification/asr4b/browser-live/external-detail-390.png) |
| 真实下载 | [TXT](../../runtime/verification/asr4b/browser-live/transcript.txt)、[JSON](../../runtime/verification/asr4b/browser-live/result.json)与结果API完全一致 |
| 数据保持 | [首次](../../runtime/verification/asr4b/persistence-browser-live-20261019.json)、[重启后](../../runtime/verification/asr4b/persistence-restart-20261019.json)、[最终停止后](../../runtime/verification/asr4b/persistence-final-stopped-20261019.json)均通过；前两次另逐项GET核对37份历史成功结果 |
| 本机启停 | [启动](../../runtime/verification/asr4b/platform-start-20261019.json)、[首次停止](../../runtime/verification/asr4b/platform-stop-first-20261019.json)、[重启](../../runtime/verification/asr4b/platform-restart-20261019.json)、[最终停止](../../runtime/verification/asr4b/platform-stop-final-20261019.json) |

10:39:00网页提交现有5.5466875秒公开中文音频，10:39:02任务`0db13671065e4618b8a7c10476670083`成功，attempt/maxAttempts均1、remoteOutcome=confirmed。全文“欢迎大家来体验达摩院推出的语音识别模型。”，1个时间分段。首次浏览器最后一项因favicon404失败，提交及结果证据保留在`browser-live-first-failed`；未重发音频。10:44重启后仅用原回执GET恢复，下载、刷新、两种宽度及控制台检查全部通过。这与09:40独立直接调用是两份证据，不计为两条平台任务。

### 如何使用与当前限制

在开发目录用现有`project311`运行`python -m backend.modules.platform.platform_cli start`，启动后打开`http://127.0.0.1:5173/`，在“转写模型”选择`sensevoice-external · 外部服务`，外部状态为“可以转写”后上传音频。需当前电脑可访问校园服务；无需先启动本机Whisper。用`python -m backend.modules.platform.platform_cli stop --timeout-seconds 60`停止本机平台，远端课题组服务不会被关闭。最终交付状态为本机平台已停止；启动脚本说明仍见README。

实际上游模型版本/硬件未报告，严格证书验证未通过；沿用用户明确允许的固定origin TLS例外。没有扩大20MiB/300秒限制、下载模型、添加依赖或修改架构展示网页。真实异常断网/负载压力、本次新的取消或幂等重复POST未做；故障与幂等兼容证据来自隔离测试，不冒充真实故障演练。GPU调度、异步协议、远端受管启停均未实现。

下一部分为**ASR-4C：远端受管启停**，目标是取得服务管理条件后验证启动、就绪、停止及退出确认。需要部署材料/受控管理接口、操作权限与唯一管理者约定；普通转写接口不等于设备选择或启停接口。本轮暂停，不自动进入ASR-4C。Multica集中同步实际结论以下方记录与映射读回为准。

以下保留本次交付前的授权、准备和验证历史，不作为当前阻塞条件。

## ASR-4B集中同步实际结果

2026-10-19已完成ASR-4B集中同步：BING-3/BING-7均保持in_progress（revision分别为4/5），正文明确开发侧交付、待用户验收。最后写入读回`2026-10-19T10:50:45+08:00`，最终完整核对`2026-10-19T10:52:05+08:00`；10条本地事件已归档，待汇总和pending为空。BING-2/4/5/6、评论、项目及资源保持，runs无新增，teacher daemon仍停止。未新建任务、绑定运行时或启动后台同步。本地/远端计划均v2.8，历史批次和时间保留；ASR-4C仍未实施。

证据：[全批读回](../../runtime/verification/asr4b/multica-batch/verification-verify.json)、[任务映射](../multica-sync.json)。写前核对受管正文并保留人工区，写后核对其余任务、评论及运行记录；仅更新两条现有任务，没有把记录同步作为用户验收。

## 上游版本可选调整（用户已确认，2026-10-19）

用户明确要求“修改下逻辑吧，这个我觉得不是必须项”，替代此前必须取得真实版本才可登记的条件。外部地址和平台模型标识仍必需，上游版本可省略；自动以接入配置生成稳定修订并记录versionInfo={versionBasis: integration_revision, upstreamVersion: null}，不能据此推断模型权重版本。修改配置后旧任务仍读取原不可变登记；已知版本、本机模型及旧JSON格式保留。

10:23:35已配置日常外部通道与此前获准的固定地址TLS例外，登记并启用sensevoice-external；重复登记不新增版本，实际上游版本仍为空。[登记核对](../../runtime/verification/asr4b/registration-20261019.json)保存非秘密配置事实。真实页面、完整回归及最终收尾结果将在实际完成后填写，本段不是提前交付或用户验收通过。

以下为本次修改获准前的准备记录，待确认版本语义的限制已由上述用户明确决定解除。

## 继续推进前的本机准备（2026-10-19 09:55—09:58）

用户继续授权推进ASR-4B平台与页面闭环。已核对本机旧状态：9条受管ASR会话均有退出确认、无未结束任务；旧Agent没有整体shutdown确认，上次管理器状态仍为running，不能补写为正常停机。09:56:53正常Windows权限下未发现项目服务进程，环境8项检查通过；沙箱内模型清单读取失败已确认属于权限差异，未下载模型。09:57:52完成数据库与39份上传配套备份，完整性、外键、全部表行及上传哈希核对通过，原39任务/2模型版本/remote_asr_calls0保持。现有版本登记只接受真实上游模型/部署版本；已向用户提出明确区分“上游版本未知＋本地接入修订”的小范围调整，仍待回复，尚未实施该语义调整或填写占位版本。本次未修改.env、登记模型、启动平台或再次发送音频；不进入ASR-4C，不写Multica。

- [旧会话核查](../../runtime/verification/asr4b/platform-session-preflight-20261019.json)：9次ASR启动历史均已确认模型退出；当前控制行state=stopped且无activeTask，但旧Agent的整体shutdown时间为空，管理器文件残留running。此次只记录证据，不删除锁或手工改数据库。
- [正常Windows进程核查](../../runtime/verification/asr4b/process-preflight-20261019.json)没有项目服务进程；[环境检查](../../runtime/verification/asr4b/environment-preflight-20261019.json)8项通过，含现有Whisper/YOLO文件哈希。首次沙箱检查因模型目录读取权限失败，复核没有重装环境或模型。
- [备份核对](../../runtime/verification/asr4b/platform-baseline-20261019/verification.json)：保持管理器/启动客户端和各Worker锁的临时互斥期间，使用SQLite备份API和上传成套复制；备份完整性、外键、全部行及39份上传哈希通过。备份位于同目录backup，before.json保留原表与上传基线；未声称已完成独立恢复验收。
- 待选择的具体调整：原准确模型版本模式保留；仅外部服务允许显式版本来源为本地接入修订，上游版本为空且页面/任务结果明确显示未知，不能直接把unknown、API14.2或接入日期当作权重版本。原登记不可变，拿到真实版本后新增记录。因这是对已确认准入条件的调整，已请求用户选择；未选择前不改代码或日常配置。
- 本轮无新测试通过数量或远端音频调用；之前90/24/43及一次真实转写仍归此前时点。

## 最新授权与真实结果（2026-10-19 09:38—09:42）

用户进一步明确“可以跳过”，允许按服务方curl -k示例调用；这替代此前先等CA的限制。本次只对已给出的固定校园网地址使用该例外，未改变系统全局信任。

- 09:38:32健康GET返回200，success=true、status=ok、model_loaded=true、API版本14.2。证据：[健康响应](../../runtime/verification/asr4b/health-insecure-authorized-20261019-093832.json)。
- 09:40:06发送现有公开中文样例，09:40:07收到200，总耗时约1.016秒。请求编号`be4467df33aa4f42a362e4a63c386fbe`准确回显；仅提交1次，没有自动重试或重定向。全文为“欢迎大家来体验达摩院推出的语音识别模型。”，1段，起止1.2—5.16秒，在真实5.5466875秒音频范围内。
- 真实响应通过现有RemoteTranscriptResponse和通用结果Schema核对；生成TXT及通用JSON。语言置信度、硬件设备及模型/部署版本均未伪造；API14.2仅记作接口元信息。
- 调用证据：[单次请求及结果](../../runtime/verification/asr4b/direct-call-20261019/attempt.json)、[原始响应](../../runtime/verification/asr4b/direct-call-20261019/response.json)、[结果核对](../../runtime/verification/asr4b/direct-call-20261019/result-validation.json)；可读产物：[TXT](../../runtime/verification/asr4b/direct-call-20261019/transcript.txt)、[通用JSON](../../runtime/verification/asr4b/direct-call-20261019/result.json)。
- 这是独立真实调用，没有建立平台任务。09:41:54只读核对仍为39任务、2模型版本、remote_asr_calls=0；.env未改、模型未登记、平台未启停。实际部署标识仍待提供。
- 一次性验证脚本`runtime/verification/asr4b/direct_call_20261019.py`以独占输出目录阻止重复运行；不能删目录后重发或将其当作日常Worker。

## 受控TLS例外实现与回归

本轮配置新增可选`INFERENCE_REMOTE_ASR_INSECURE_TLS_ORIGIN`，默认None；完整配置并匹配准确origin后才可跳过，不可与CA同时设置。例外仅接受ASCII主机、IP或明确的xn--域名，避免两套IDNA转换产生地址歧义；默认严格模式不受该限制。SSL上下文由原适配器统一创建，原严格模式的任务指纹兼容，跳过模式另行绑定。

| 本轮最终检查 | 实际结果 | 报告 |
|---|---|---|
| 外部配置、适配器、真实回环HTTPS | 90通过，12.40秒，无失败/跳过 | [最终TLS定向报告](../../runtime/verification/asr4b/tls-unit-ascii-final.xml) |
| 原外部任务链路和Windows子进程 | 24通过，56.73秒，无失败/跳过 | [兼容报告](../../runtime/verification/asr4b/compatibility-tests-20261019.xml) |
| 共用配置、ASR与LLM配置兼容 | 43通过，1.20秒，无失败/跳过 | [共享配置报告](../../runtime/verification/asr4b/shared-settings-tests-20261019.xml) |

三组结果分列，不与此前重复定向或历史全量数量累加。本轮未运行全量后台、前端或浏览器测试。回环HTTPS验证默认拒绝未知自签证书、明确例外后成功、移除例外后恢复拒绝，以及可信CA路径成功；这是协议测试，真实模型证据是上方独立单次调用。最后审阅增加ASCII来源限制后只重跑受影响的三份TLS单测文件，保留此前报告。无新增依赖、迁移或模型下载，20MiB/300秒保持。

复核命令（开发目录、现有project311）：

```powershell
& 'C:\Users\86199\miniconda3\envs\project311\python.exe' -m pytest tests/unit/test_remote_asr_settings.py tests/unit/test_remote_v2_adapter.py tests/unit/test_remote_asr_tls.py -q -p no:cacheprovider
& 'C:\Users\86199\miniconda3\envs\project311\python.exe' -m pytest tests/integration/test_remote_asr_execution.py tests/e2e/test_remote_asr_processes.py -q -p no:cacheprovider
& 'C:\Users\86199\miniconda3\envs\project311\python.exe' -m pytest tests/unit/test_settings.py tests/unit/test_asr_settings.py tests/unit/test_llm_settings.py -q -p no:cacheprovider
```


## 本次直接调用仍未覆盖的内容

实际部署标识、平台模型登记、Worker真实服务任务、网页上传/下载闭环、本机旧会话恢复和真实异常均尚未验证；没有远端启停、GPU调度、异步任务查询或完整容量结论。ASR-4B整体交付后才集中同步Multica。

以下1—5节记录09:29时的检查和等待条件，保留原时点事实；其中等待CA的前提已由上方最新用户授权调整。

## 1. 授权与本次范围

用户于2026-10-19明确“校园网已经连接，请继续ASR-4B”。沿用原同步适配器、原任务与结果链路，目标为使用已有公开短中文音频验证课题组SenseVoice服务。此授权不改变其他阶段验收状态，不扩展到GPU调度、异步协议切换或ASR-4C远端管理。

## 2. 本次实际检查

| 时间（北京时间） | 检查 | 实际结果 |
|---|---|---|
| 2026-10-19 09:24:34 | 在现有project311中连接固定目标10.8.21.33:8003，再使用默认SSL信任校验TLS | TCP连接成功；TLS返回SSLCertVerificationError，verifyCode=18，self-signed certificate；整个连接检查约0.282秒 |
| 同次连接 | 仅在TLS验证通过后才发送健康GET | 验证未通过，因此未发送HTTP健康请求、未上传音频，没有创建远端推理请求 |
| 2026-10-19 09:27:11 | 读取本机环境、配置、公开样例、平台状态及SQLite | Python3.11.16，Flask3.1.3、SQLAlchemy2.0.54、Pydantic2.14.1、Alembic1.20.0、httpx2 2.13.1；外部地址/模型标识/版本/CA配置均为空 |
| 同次本地检查 | 只读数据库与样例检查 | 迁移20261015_0010；39任务（37成功、2取消）、2模型版本、remote_asr_calls=0；样例5.5466875秒、16kHz单声道、177572字节，SHA256与既有公开样例记录一致 |
| 同次平台检查 | 实时管理器和端口状态 | 管理器未运行，5000/5173可绑定，API和页面不可达；保存的上次状态仍是running。这不是一次正常停止或ASR退出确认，本轮未启动、停止或清理平台 |

证据：[TCP/TLS检查](../../runtime/verification/asr4b/connection-20261019-092434.json)、[本机准备情况](../../runtime/verification/asr4b/local-readiness-20261019-092711.json)。本机检查只是本轮快照，不冒充逐文件历史数据对比或恢复验收。

## 3. 当前缺口与用户回复

用户说明此前服务方认为不需要证书，并要求如有必要先记为“待提供”，之后再询问。现将服务器公钥证书或可信CA记录为**待提供**；不需要私钥、密码或令牌。

对方示例的`curl -k`跳过证书验证，能解释示例调用为何不需配置证书；平台现有TLS校验继续保留。当前错误是证书信任未通过，不是已经证明模型故障、鉴权错误、证书主机名不符或证书过期。之后取得可信资料还需检查访问地址与证书是否匹配。

真实模型/部署版本仍未取得，API版本14.2不能替代权重或部署版本。版本缺口不阻止只读网络/TLS/健康检查，但启用现有平台模型登记时需要可追溯的实际标识，不能填写虚构版本。

## 4. 尚未验证与变更边界

- 未取得健康响应，模型是否就绪及是否需要接口鉴权尚未实测。
- 未上传音频，未验证真实全文、分段、TXT/JSON下载、页面闭环或远端运行硬件。
- 未修改业务代码、依赖、运行配置或业务数据；未运行前后端全量测试，历史通过数量不计入本轮。
- 未关闭TLS验证、自动信任网络取得的证书或改为HTTP；未运行外部启停命令，也未下载模型或扩大20MiB/300秒限制。
- 本轮只更新本地记录及待集中同步摘要，未写Multica。最后远端成功批次仍是ASR-4A。

## 5. 恢复顺序与下一部分

1. 取得服务方可核对的公钥证书/CA与匹配的访问地址，验证TLS。
2. 获取健康响应，确认真实服务和模型就绪；核对实际部署标识及必要鉴权条件。
3. 按既有受管流程核查本机旧会话、配套备份数据库与上传文件，再配置、登记模型和启动本机调用服务。
4. 经原平台任务链路提交一次公开短中文音频，检查真实结果、下载、历史兼容与本机服务收尾；实际完成后再集中同步BING-3/7。

ASR-4B完成后，下一部分为**ASR-4C：远端受管启停**，目标是验证真实服务启动、就绪、停止与退出确认；需要部署材料或可信管理接口、管理权限及唯一管理者约定，并等待该部分实施指令。
