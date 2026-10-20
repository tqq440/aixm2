# ASR调度执行说明（v2.12本机-2B）

本页保留2B执行契约；本机-2C当前只增加页面及只读解释，不改变这些领取规则。页面用法见[ASR调度页面与故障处理](ASR调度页面与故障处理.md)，当前验证见[2C记录](阶段记录/本机-2C-ASR调度页面与故障说明-验收记录.md)。

本机-2B把已有选择策略接入原任务领取和执行流程。**本轮只开放隔离环境验证，新增加的真实目标仍不能执行。**日常未设置路由修订时，继续使用原remote-default通道、设备unknown和目标/共享组限额1。本部分已于`2026-10-20T09:45:44+08:00`开发侧交付、待用户验收；实际验证及限制见[本机-2B记录](阶段记录/本机-2B-调度策略接入任务执行-验收记录.md)。

全部ASR/YOLO/LLM入口和原运行方式保留；课题组模型部署启停由服务方负责。页面展示已由本机-2C补充，真实目标/设备/容量接入留本机-3A，不把下面的隔离配置复制到日常`.env`来扩大实际服务并发。

## 1. 从资料登记到一次任务选择

[本机-2A目录](ASR目标配置与资源记录说明.md)回答“有哪些待接入目标、资料来自哪里”。本部分另保存两类不可变记录：

| 记录 | 保存内容 | 不等于什么 |
|---|---|---|
| 路由修订`routingRevisionId` | 一个逻辑模型身份、兼容标识、固定候选目录修订、GPU声明及CPU支持/性能依据 | 不会启用目标、增加容量、修改旧任务或证明真实GPU/CPU可用 |
| 任务路由`routing` | 新任务受理时绑定的路由修订，以及领取事务最终冻结的一个候选 | 不是新的任务状态机；不因重启更换默认配置而重定向 |

逻辑任务的模型身份与候选实际服务连接身份分别保存。多个兼容候选可以拥有不同的本地integration版本，最终调用与结果提交依据已冻结候选、连接修订、槽会话和租约共同核对。旧任务的连接指纹和已有结果不改写，也不补造路由字段。

Broker预筛只判断当前固定目标Worker是否可能领取，不冻结目标。真正领取时，原事务重新读取所有候选、资源记录、槽状态及共享占位，执行GPU优先/合条件CPU策略；选中后同事务保存选择、开始原任务、签发permit并把原外部调用置held。任一环节失败整体回滚，不留下“已选目标但没有执行租约”的半成品。

只能选择尚未执行、没有permit且未超期的任务。当前任务已执行或结果不明时，不换GPU/CPU重发；停止、超时、取消、租约和排空继续沿原规则。没有合格候选时任务保留原截止时间等待，不延长队列期限。

## 2. 受控路由登记

结构使用0014，先按[迁移说明](../migrations/README.md)确认版本与配套备份。沿用project311维护入口：

```powershell
Set-Location -LiteralPath 'D:\1 研究生\aixm\inference-platform'
$projectPython = 'C:\Users\86199\miniconda3\envs\project311\python.exe'
& $projectPython -m backend.modules.asr.targets_cli register-route '.\已核对的路由资料.json'
```

文件最多64KiB，严格JSON、camelCase字段；重复键、未知字段、非有限数字和不合类型拒绝。上述文件名是占位说明，须先准备真实或明确标记隔离的资料；不要把模拟GPU/CPU写入日常目录。命令只保存资料，不联网、不启动Worker、不给候选授权执行，成功返回`routingRevisionId/created/executionEnabled=false`。同一输入幂等返回同一修订，内容变化保存新修订，旧修订不覆盖。

| 路由JSON字段 | 约束与含义 |
|---|---|
| `modelId/modelVersion` | 平台逻辑任务模型身份，必须与提交模型一致；不据此冒认候选上游权重版本 |
| `compatibilityKey` | 候选共用的同模型兼容标识；每份目录也须保存对应标识和依据 |
| `gpuConfiguration` | configured/none/unknown，默认unknown；none不能同时含GPU候选 |
| `evidenceKind/evidence` | isolated或provided及可核对说明；类别须与候选目录一致 |
| `cpuBudgetSeconds` | 可选大于0且不超过900秒的CPU可接受预算；未知不猜测 |
| `candidates` | 1—8个固定目录修订，同一目标不能出现两次 |
| 候选`catalogRevisionId` | 当前目录修订；不存在、已变化或资料类别不一致会拒绝 |
| 候选`cpuSupported/cpuSupportEvidence/cpuProfile` | CPU支持声明、依据与性能样本；缺失或不合格时不授予CPU承接 |

`cpuProfile`沿用已有策略契约：记录对应目录修订、资料类别、证据引用、测量时间与有效期、样本数和音频范围、最大实测并发、固定开销/每秒音频耗时/安全系数及排队上界。样本须属于该CPU候选、在有效期内、覆盖本任务规模和并发，预计耗时还须满足CPU预算、任务总期限及原执行/远端超时限制。没有这些证据就继续等待，不提供“先填一个估计值”的日常启用流程。

## 3. 本轮隔离执行开关

`INFERENCE_REMOTE_ASR_ROUTING_REVISION`默认未配置；`INFERENCE_REMOTE_ASR_ROUTING_ISOLATION`默认false。env.example仅说明可选修订与默认关闭的隔离开关，日常`.env`保持原样。

仅设置修订或打开隔离开关并不能使用真实服务器。本轮执行入口同时检查：

- 路由及候选资料类别都是isolated；provided可登记为资料，但不能运行。
- 数据库、上传和日志都在项目`runtime/tmp`、`runtime/verification`或系统临时目录内，不能共用日常数据。
- 候选HTTPS地址限回环localhost/127.0.0.1/::1或保留的`.invalid`域；`.invalid`是隔离测试地址，不证明存在可用服务。
- 候选必须显式启用且目录未更新；本轮只有隔离测试夹具设置候选启用，没有开放日常启用命令。目录登记、路由登记都不会自动启用。
- 全部固定目标槽合计1—8个，每个目标槽数仍受目标和共享组限额约束。调用池继续统一持有原global锁、管理子进程、排空并保留未知占位。

如果旧固定通道仍有待执行任务，或其他路由修订仍有未执行任务，拒绝切换新路由；应按原配置把任务收尾，不删任务、改指纹或延长截止时间来绕过检查。停止后旧未知调用仍保守占位，不能靠换目标、换版本或重启释放。

## 4. 资源与CPU承接的保守条件

每个候选同时需要target和capacity_group两种范围的最新报告，来源一致、类别匹配、健康明确，且按采样时间加TTL严格有效；到期即失效，数据库时钟早于采样或录入时间也失效。共享组多个候选的来源必须一致，组报告采用保守下界，不把别名的报告相加。

策略同时核对本平台held/uncertain占位、目标/共享组许可、真实报告余量和匹配修订的Worker心跳。扣除本地占位后余量不足会等待；保守扣减得到0并不自动证明GPU资源已满，避免误将不确定任务改派CPU。未知、过期、缺报或来源不一致不能当作空闲，也不能当作“所有GPU都满了”。

CPU承接仍要求同模型支持及有效性能依据，并只处理尚未执行任务。GPU是否已配置/已确认无可用容量须先有依据；CPU样本过期、时钟回退、并发或音频范围不匹配、预算不足时继续等待。本机Whisper不能充当SenseVoice的等价CPU替代。

## 5. 查询与常见拒绝

原任务提交、详情、列表、取消和结果路径保持。新路由任务在TaskView中增加可选`routing`，包含waiting或selected、路由/目录/目标编号、设备类别、选择原因、资料类别及选择时间；没有路由记录的旧任务不返回该字段。waiting只表示目标尚未冻结，具体未选择字段可为null；selected表示已在领取事务冻结，不表示转写已成功。页面如何展示这些信息留本机-2C，完整契约见[接口说明](接口说明.md)。

| 错误码 | 处理原则 |
|---|---|
| `ASR_ROUTING_ISOLATION_REQUIRED` / `ASR_ROUTING_REAL_TARGET_PENDING` | 隔离目录/开关不合要求，或真实资料尚未获准执行；保持原日常通道 |
| `ASR_ROUTING_NOT_FOUND` / `ASR_ROUTING_CATALOG_MISSING` | 路由或固定候选缺失，不自动改用其他服务 |
| `ASR_ROUTING_CATALOG_CHANGED` / `ASR_ROUTING_COMPATIBILITY_REQUIRED` | 目录已更新、类别或同模型依据不符，需重新核对资料 |
| `ASR_ROUTING_TARGET_DUPLICATE` / `ASR_ROUTING_GPU_CONFLICT` | 候选重复或GPU声明矛盾，修正资料，不扩大容量 |
| `ASR_ROUTING_CPU_PROFILE_MISMATCH` | CPU样本不属于当前候选/资料类别 |
| `ASR_ROUTING_TARGET_DISABLED` / `ASR_ROUTING_SLOT_LIMIT` | 候选未启用/已变化，或总槽数不在1—8内；本轮真实目标继续禁用 |
| `ASR_ROUTING_LEGACY_PENDING` / `ASR_ROUTING_PREVIOUS_ROUTE_PENDING` | 原固定通道或其他修订有待执行任务，先沿原配置收尾 |
| `ASR_ROUTING_CONNECTION_INVALID` / `ASR_ROUTING_TLS_NOT_AUTHORIZED` | 候选连接/证书变化或扩大原TLS例外，不自动跳过校验 |
| `ASR_ROUTING_MODEL_MISMATCH` / `ASR_ROUTING_SELECTION_CONFLICT` | 逻辑模型、冻结候选或执行资格不匹配，不重定向旧任务 |

本轮完成后暂停。下一部分是本机-2C“ASR调度页面与故障说明”；真实GPU/CPU执行与资源测量仍留3A核实后，再由3B联调交付并集中同步Multica。
