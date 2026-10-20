# GPU优先与CPU承接：离线预演说明

本轮提供一个可解释的离线决策工具：读取你指定的本机JSON场景，输出“建议选择哪个目标”或“为什么继续等待”。它没有接入日常任务领取，不签发许可、不修改数据库、不加载模型，也不请求服务或采集硬件。实际网页继续使用2B已有同步调用链路，默认设备unknown、目标/组限额1。

这是第3部分获准策略范围的开发侧交付，待用户验收；真实GPU/CPU接入、资源观测与任务路由尚待条件。全部实际验证见[阶段记录](阶段记录/v2.9-3-设备选择策略与隔离验证-验收记录.md)。

## 1. 在Windows运行

使用现有project311环境，不必启动或停止平台、不必下载模型。在PowerShell运行：

```powershell
Set-Location -LiteralPath 'D:\1 研究生\aixm\inference-platform'
$projectPython = 'C:\Users\86199\miniconda3\envs\project311\python.exe'
& $projectPython -m backend.modules.asr.device_policy_cli preview --input 'tests/fixtures/device-policy/gpu-first.json'
```

命令输出JSON。`action=select`表示建议目标，`action=wait`表示当前输入不足以选择；两种结果均为`mode=preview`、`executionEnabled=false`。`reasonCode`解释总体结果，`candidates`逐个列出候选是否符合条件及原因。正常预演退出码0，包括正常的“等待”；校验失败返回统一错误JSON至stderr并退出2，一般内部异常退出1，中断退出130。

其他三个样例和完整输入契约可分别运行：

```powershell
& $projectPython -m backend.modules.asr.device_policy_cli preview --input 'tests/fixtures/device-policy/gpu-full-cpu.json'
& $projectPython -m backend.modules.asr.device_policy_cli preview --input 'tests/fixtures/device-policy/gpu-unknown-wait.json'
& $projectPython -m backend.modules.asr.device_policy_cli preview --input 'tests/fixtures/device-policy/cpu-outside-measurement-wait.json'
& $projectPython -m backend.modules.asr.device_policy_cli schema
```

输入必须是本机UTF-8 JSON文件，最多512KiB；不接受重复键、非有限数字、契约以外字段或启用执行的参数。CLI没有apply、register或execute操作。`schema`只输出契约，不读取应用配置或数据库。

## 2. 四个场景分别说明什么

| 文件 | 人工构造条件 | 当前预演结果 |
|---|---|---|
| [gpu-first.json](../tests/fixtures/device-policy/gpu-first.json) | 合格GPU和CPU都还有容量 | 选择`fixture-gpu`，`GPU_PREFERRED` |
| [gpu-full-cpu.json](../tests/fixtures/device-policy/gpu-full-cpu.json) | GPU及其组满额，其他证据齐全；CPU符合条件 | 选择`fixture-cpu`，`CPU_FALLBACK_CONFIRMED_GPU_FULL`；预计6秒 |
| [gpu-unknown-wait.json](../tests/fixtures/device-policy/gpu-unknown-wait.json) | GPU虽有满额数字，健康状态却未知 | 等待，`GPU_STATE_UNCONFIRMED`，不据此转给CPU |
| [cpu-outside-measurement-wait.json](../tests/fixtures/device-policy/cpu-outside-measurement-wait.json) | GPU确认满额，12秒音频超出示例CPU样本范围 | 等待，`NO_ELIGIBLE_CPU_TARGET`；CPU候选原因`CPU_AUDIO_OUTSIDE_MEASURED_RANGE` |

四份输入全部是`evidenceKind=isolated`的测试场景，fixture目标、观测、样本数和性能数据均非真实设备证据。它们使用固定`evaluatedAt=2026-10-19T08:00:00Z`及配套时间；这是场景内的演算时刻，重复运行仍按该输入计算，不代表运行命令时的新采样。

**示例中的10秒上界和6秒预计耗时都不是实测阈值或生产配置。**示例CPU样本范围设为1—10秒音频；5秒音频按`(0.5秒固定开销 + 5 × 0.5) × 2安全系数 + 0秒排队上界 = 6秒`计算。真实“小任务”范围、样本有效期、最大并发和耗时系数仍要用同一模型、同一CPU目标修订的实际测量确定，不能将这份JSON登记成真实资源。

## 3. 决策规则

先核对任务：只有`status=queued`、`attempt=0`、`hasPermit=false`且`upstreamExecutionStarted=false`的尚未执行任务可预演；还须有不可变路由修订和兼容标识，且未越过原排队/总截止时间。其他情况返回等待，不把执行中、已经尝试或结果不明的任务复制到其他设备重跑。

候选需同时具有模型兼容声明、已说明的设备类型及证据、新鲜健康状态、目标与共享组容量、新鲜的Worker能力观察。缺失、未来时间或过期证据不能当作空闲；`unknown`设备不能改名成GPU或CPU。输入标记`provided`仅表示调用者提供的数据，离线工具不会自动核实其真实性；输出保留证据来源，涉及isolated输入时仍标为isolated。

`slotReady`说明绑定Worker具有执行能力，不表示槽当前没有任务。它与容量分别判断：Worker不可工作时，即使容量数字已满，也不能将该目标解释成“健康GPU已满，可改给CPU”。目标和共享容量组都按`held + uncertain`计算占用；同组不同别名不能各自增加总容量，未知执行仍占位。

有合格GPU时，按**目标自身的`(held + uncertain) / limit`**由低到高排列，相同占比用`targetId`稳定排序。组容量是共同约束，不是排序分母；本规则不按显存、硬件利用率或预计最快速度排序。没有合格GPU时，只有相关GPU都已确认满额，或明确未配置GPU，才继续检查CPU。GPU配置、设备或状态不明时继续等待。

CPU还须声明同模型CPU支持及证据，提供与目标修订一致且新鲜的性能样本，音频在样本范围内，目标及组限额不超过样本验证的并发范围。预计处理耗时为`(固定开销 + 音频秒数 × 每音频秒耗时) × 安全系数`，再加排队上界。排队预计必须严格短于剩余排队期限；预计总耗时必须不超过CPU/单次执行/远端预算，并严格短于剩余总期限。不能只凭“文件小”认定CPU来得及；没有合格CPU就继续等待。

## 4. 与真实系统的关系

策略在[device_policy_service.py](../backend/service/asr/device_policy_service.py)，契约在[device_policy_schema.py](../backend/schema/asr/device_policy_schema.py)，命令入口在[device_policy_cli.py](../backend/modules/asr/device_policy_cli.py)。这次只增加可验证的决策边界，不改变M02能力队列、M12租约与提交，也不把原M10实例部署职责改写成已实现的GPU派发器。原[计算目标与容量准入](计算目标与容量准入说明.md)和[并发方案](并发与设备分配方案.md)保持衔接。

下一项是本部分真实目标接入与路由联调：核实可调用的GPU/CPU目标、服务与物理设备映射、共享容量、同模型CPU支持和性能证据，再在获准范围内接入执行前的原子资格复核。当前统一外部接口设备仍未知；本机Whisper也不能自动视为外部SenseVoice的CPU副本。

第3部分本轮纯策略与离线预演范围已集中同步Multica，完整读回通过：BING-3 revision=8、BING-7 revision=9，均保持in_progress，正文明确待用户验收。最后写入读回`2026-10-19T14:37:17+08:00`，完整核对`2026-10-19T14:37:27+08:00`。本地与远端计划均v2.9，2条事件已归档，deferred_updates与pending为空；未新建任务，BING-2/4/5/6、评论、项目/资源与runs保持，teacher daemon仍停止。不绑定执行运行时、不启动后台同步；真实目标接入、资源观测和执行路由仍待条件及后续授权，不表示整个第3部分已完整交付。
