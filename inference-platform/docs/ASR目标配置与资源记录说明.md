# ASR目标配置与资源记录说明（v2.12本机-2A）

本机-2C现将目录和资源来源接入原ASR页面，查看方法见[调度页面与故障处理](ASR调度页面与故障处理.md)。本页登记/观测语义保持；新页面不能把未核实资料变成执行许可。

本机-2B已完成隔离任务执行接入的开发侧交付、待用户验收，新增路由登记和限制见[ASR调度执行说明](ASR调度执行说明.md)。本页保留2A目录/资源契约；登记仍不授予执行权限，真实新增目标继续禁用。

本部分把“准备调用哪些服务”和“拿到了哪些资源资料”保存成可追溯的记录。**登记目标不会启用调度，录入GPU/CPU说明也不代表平台已经核实设备。**当前实际开发及验证状态见[本机-2A记录](阶段记录/本机-2A-ASR目标配置与资源记录-验收记录.md)。

旧`remote-default`仍由原调用池按原配置执行，设备unknown、目标/共享组限额1保持；本部分不接Worker调度、不发送转写请求，不修改旧任务指纹或重新发送结果不明的任务。已有ASR/YOLO/LLM入口、模型启停方式保持，课题组模型仍由服务方部署和维护。

## 1. 三种记录的区别

| 记录 | 用途 | 本部分不据此承诺 |
|---|---|---|
| 连接修订`connectionRevisionId` | 保存地址、模型及TLS配置指纹，复用原`asr_target_revisions` | 可重定向旧任务或绕过未知调用 |
| 目录修订`catalogRevisionId` | 保存设备声明、模型兼容依据、共享容量归属及资料类别 | 设备已核实、同模型兼容已证明或执行已启用 |
| 资源观测`observationId` | 保存来源、采样时间、有效期及报告值 | 服务器此刻仍空闲，或平台许可限额已扩大 |

同一份连接和元数据重复登记复用修订；首次出现的元数据组合产生新目录修订，旧记录保留。重放旧组合只返回原修订，不自动回滚当前目录。资源观测追加保存，重复相同采样幂等；只允许向当前目录修订添加新观测，旧观测不会移到新配置上。

`provided`表示有人提供了资料，`isolated`表示隔离验证资料。两者不是“验证通过/失败”，本部分目录的`verificationState`统一为`unverified`。隔离数据只应进入隔离数据库，不登记到日常资源池冒充真实服务器。

## 2. 受控维护入口

使用现有project311。业务数据库必须先按[迁移说明](../migrations/README.md)完成0013；迁移前确认相关服务停止并成对备份数据库与上传目录。以下仅是调用方式，JSON文件须由维护人员根据实际资料准备，不直接复制演示目标到日常库。

```powershell
Set-Location -LiteralPath 'D:\1 研究生\aixm\inference-platform'
$projectPython = 'C:\Users\86199\miniconda3\envs\project311\python.exe'
& $projectPython -m backend.modules.asr.targets_cli register-config '.\待核对目标.json'
& $projectPython -m backend.modules.asr.targets_cli record-observation '.\已取得的资源报告.json'
& $projectPython -m backend.modules.asr.targets_cli catalog --offset 0 --limit 20
```

文件上限64KiB，必须是严格JSON；重复键、非有限数字、未知字段、超限及不合契约的类型拒绝。命令不接受任意Python模块、系统命令或远端采样脚本，不联网探测。CLI成功给出JSON；错误沿用统一错误响应和`asr-targets-maintenance.log`。

原`register-current`、`list`、`configure-capacity`继续保留，见[计算目标与容量准入说明](计算目标与容量准入说明.md)。本部分的`register-config`不提供启用命令，不允许覆盖保留的`remote-default`，也不修改已启用目标。新目标`enabled=false`、目标初始限额1；新增组初始限额1，复用既有组不会重置其限额。

### 目标JSON的字段

| 字段 | 含义与约束 |
|---|---|
| `targetId` | 1—64位平台标识；不能使用保留的remote-default |
| `connection` | 必填对象：`baseUrl`、`modelId`；可选`modelVersion`、`caFile`、`insecureTlsOrigin`，沿原HTTPS配置校验 |
| `capacityGroupId` | 默认remote-asr-shared；同一服务入口必须共用容量组，已登记目标不能改组 |
| `independenceEvidence` | 新独立组必须附依据；文字本身不会证明物理容量独立 |
| `deviceKind` | unknown/gpu/cpu，默认unknown；这是待核实的声明 |
| `nodeId/deviceId/deviceEvidence` | gpu/cpu声明必须同时填写；unknown不得附这三项，不能猜测设备编号 |
| `compatibilityKey/compatibilityEvidence` | 同模型兼容标识及依据，必须同时提供或同时省略 |
| `evidenceKind` | provided或isolated，默认provided |

上游模型版本仍可省略，沿用稳定本地接入修订及版本来源说明，不伪造上游版本。不接受含秘密的URL、任意路径或重定向配置；证书只从本机文件读取，不使用UNC路径。目录不能扩大当前设置明确授权的TLS例外，缺少可信配置时应保留待条件，不增加跳过校验的范围。

设备与兼容依据保存在本地目录元数据中；不要填密码、令牌、密钥或完整账号信息。HTTP只返回“是否有依据”，不返回原始依据文本或连接/TLS配置。

### 资源观测JSON的字段

| 字段 | 含义与约束 |
|---|---|
| `catalogRevisionId` | register-config返回的64位目录修订ID；必须是当前修订 |
| `scope` | target或capacity_group，默认target；区分报告针对一个目标还是共享组 |
| `source/evidence` | 来源标识及可核对说明，必填；不是自动采集器名称或执行命令 |
| `evidenceKind` | 必须与目录资料类别一致，不混用isolated与provided |
| `observedAt` | 带时区的真实采样时间，不可晚于数据库当前时间；UTC转换及加TTL不可溢出 |
| `ttlSeconds` | 1—3600秒，表示报告有效期；从采样时间起算，不从录入或查询时间起算 |
| `healthState` | unknown/ready/unavailable，默认unknown，只是报告健康值 |
| `availableConcurrency` | 可选0—1024整数；null为未知，0为报告无可用量，不改平台许可限额 |
| `gpuUtilizationPercent/gpuMemoryFreeMiB/cpuAvailableCores` | 可选非负报告值；GPU利用率上限100%，未知就省略，不填0冒充空闲 |

报告不会改变目标/共享组许可、调用池槽数或已持有的许可。`capacity_group`范围的报告仍归当前目录修订保存，不自动汇总其他调用方占用或分配共享容量。

## 3. 只读目录与状态含义

`GET /api/asr/external/targets?offset=0&limit=20`仅查询本地档案，返回统一成功外层。offset默认0、最大1000000；limit默认20、范围1—100；未知或重复查询字段拒绝。完整输出见[接口说明](接口说明.md)。

- `mode=legacy`表示原执行目标，`legacyEnabled`保留其原启用事实；`mode=catalog_only`表示新增待接入目录。
- `routingEnabled=false`在本部分固定表示“新调度未启用”，不把旧目标原有可用性改成不可用。
- `hasDeviceEvidence/hasCompatibilityEvidence`只说明已保存资料，不是验证结论。`deviceKind=gpu`也仍是待核实声明，不是本轮探测结果。
- 每个目录最多展示target和capacity_group两种范围的最新采样；完整历史仍保存。没有观测时返回空列表，不造一个空闲资源快照。
- `freshness=fresh/stale/unknown`表示报告有效、已过期或查询时钟异常。数据库当前时间早于采样时间或录入时间时均为unknown；GET按数据库当前时间计算，不刷新采样时间；最新报告即使已过期，也不能退回更旧报告假装可用。
- `reportedHealth`及`reportedAvailableConcurrency`都是报告内容，`usableForScheduling=false`固定保持。未来接回执行时仍须再次核实目标、设备、模型兼容、共享容量和许可，不能直接相信本页数字。

接口不会返回baseUrl、CA路径、TLS例外、连接快照、原始证据或deviceId；不会发健康或资源请求、登记目标、增加修订或修改占位。

## 4. 常见拒绝及下一步

| 错误码 | 原因与处理 |
|---|---|
| `ASR_CATALOG_INVALID_JSON` | 文件超限或不是严格JSON，先核对本地文件 |
| `ASR_CATALOG_INVALID_CONNECTION` / `ASR_CATALOG_TLS_NOT_AUTHORIZED` | HTTPS/证书配置不符或扩大TLS例外，补真实配置，不猜测 |
| `ASR_CATALOG_LEGACY_RESERVED` / `ASR_CATALOG_TARGET_ACTIVE` | 保留目标或已启用目标不可由待接入目录覆盖 |
| `ASR_CATALOG_SHARED_CAPACITY` / `ASR_CATALOG_GROUP_IMMUTABLE` | 同源入口或原目标共享组关系冲突，不用别名绕过容量 |
| `ASR_CATALOG_GROUP_EVIDENCE_REQUIRED` | 新独立共享组缺核对依据 |
| `ASR_CATALOG_NOT_FOUND` / `ASR_OBSERVATION_OLD_REVISION` | 修订不存在或已变更，核对当前修订后再记录观测 |
| `ASR_OBSERVATION_FUTURE` / `ASR_OBSERVATION_EVIDENCE_MISMATCH` | 采样时间异常或资料类别不符，不改成假时间/假类别绕过 |

下一部分为本机-2B“调度策略接入任务执行”，本部分完成并讲解后暂停，等待用户继续。真实GPU/CPU与容量仍须核实，CPU承接还需同模型支持及性能证据；本机Whisper不会自动作为外部SenseVoice的CPU替代。本机-3B真实闭环交付后才集中同步Multica。
