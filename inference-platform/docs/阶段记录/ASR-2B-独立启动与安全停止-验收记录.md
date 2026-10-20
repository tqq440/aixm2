# ASR-2B：独立启动与安全停止验收记录

日期：2026-10-14；本地交付登记时间为北京时间09:47:45，见[delivery.json](../../runtime/verification/asr2b/delivery.json)。**已完成开发侧交付及本轮真实验证，待用户验收并暂停。**下一项ASR-3A服务管理页面尚未开始。

## 1. 授权与范围

用户在ASR-2A交付后以“继续进入下一项”授权本小部分。实现本机Node Agent对一个固定CPU ASR实例的唯一管理、受控启动/停止/查询、任务排空，以及Windows平台管理器衔接。不进入ASR-3A页面或ASR-3B完整浏览器交付；ASR-1、ASR-2A的用户验收不因继续开发自动改为通过。

沿用Windows、project311、现有Whisper small、CPU、SQLite和原任务/通知/租约机制。未下载模型、未更换模型、未扩大20MiB/300秒限制，未开发GPU、多节点、YOLO/LLM新功能或恢复13A。

## 2. 基线、迁移和数据保持

本轮基线报告登记于北京时间2026-10-14 09:22:57，见[before.json](../../runtime/verification/asr2b/before.json)。这是报告中的实际核查时间，不补造首次动工时分或总工时。默认库及配套上传已备份，原数据作为本轮逐值/哈希核对基线。

0009仅新增`agent_controls`、`managed_instance_sessions`，分别保存管理意图/Agent报告和受管启动/退出历史，不修改旧表或种入虚构运行记录。默认库0008→0009、重复升级和`alembic check`通过，迁移时旧表内容完全保持，新两表为空；证据见[migration.json](../../runtime/verification/asr2b/migration.json)。退出或Agent关闭未确认时拒绝降级删除控制记录，默认库没有执行降级。

最终[final-checks.json](../../runtime/verification/asr2b/final-checks.json)确认：

- 原23任务和23份上传文件完整保留；原任务/结果/幂等/执行历史等旧行内容与上传哈希保持。运行报告及节点最后观测时间可以随实际运行更新，不将其当作不可变历史。
- 当前33任务、33上传：ASR21成功/2取消，Vision10成功；34条执行历史，在线请求仍0。
- 2模型、2版本、1节点、1实例和2条旧legacy会话观测保留；4条受管启动会话均有退出确认，Agent控制记录有关闭确认。
- 47份前端/依赖文件与ASR-2A快照相同，无新增依赖或模型下载；Multica远端映射及原成功同步信息保持。

## 3. 实现与管理边界

平台管理器现在启动API、投递器、Agent、Vision Worker和页面五个入口。ASR默认停止，显式启动后才加载Whisper。Agent在自身进程中复用`WorkerService`控制逻辑，实际推理仍在独立受管Whisper子进程；M12任务规则和M11进程管理保持职责区分。

停止意图、新任务受理、通知领取和执行准入使用原SQLite事务边界排序。意图生效后关闭新受理/领取，等待当前任务遵守原超时、取消、租约和结果提交规则完成；剩余排队任务保留原编号和原截止时间，重启不重新计时。停止未确认不报告成功，不为继续接单无必要地重建模型进程。

CLI `backend.modules.agent.agent_cli status/start/stop` 与HTTP管理共用Service；新增`scripts/asr-service.ps1 -Action status/start/stop`简化Windows操作，默认等待180秒，允许0—600秒。等待超时仍保留意图，不强制中断音频；包装脚本status已实际运行验证。

HTTP管理仅允许本机固定实例及start/stop，使用`X-Management-Token`与`agentSessionId/expectedRevision`校验。令牌在本地安全配置，本报告不显示真实值。202只表示意图受理；`starting`可能尚在等待进程启动，没有受管会话也不能宣称模型已运行。旧会话或修订冲突、重复停止但退出未确认均明确拒绝。该局部管理保护不等于13A完整账号鉴权。

旧0008会话观测保持只读可查；Agent受管会话从独立管理历史读取，托管后拒绝`observe-local`将Agent报告归入legacy来源。历史childPid仅作记录，不是操作该PID的授权。

## 4. 自动测试

| 检查 | 实际结果 | 证据 |
|---|---|---|
| 最终后台全量回归 | 1214通过、2旧Windows符号链接权限跳过；207.83秒，无warning | [XML](../../runtime/verification/asr2b/backend-tests.xml)、[文本](../../runtime/verification/asr2b/backend-tests.txt) |
| 随后补充的平台真实进程信号回归 | 3通过，1.81秒；包含全量之后新增的信号用例，单独报告 | [XML](../../runtime/verification/asr2b/platform-signal-tests.xml)、[文本](../../runtime/verification/asr2b/platform-signal-tests.txt) |
| 环境检查 | 8项通过 | [环境输出](../../runtime/verification/asr2b/environment.txt) |
| 数据结构、约束、旧会话和修订隔离、降级保护 | 纳入本轮后台回归；普通故障用例使用隔离替身，不充当真实模型证据 | [数据定向报告](../../runtime/verification/asr2b/data-tests.xml)及最终后台报告 |

最终全量不是此前1211项报告，也不把随后3项再加成“1217项全量通过”。全量包含当时已有的平台2项，新增信号用例之后再运行3项并独立保存；不得声称最终全量已包含该新增项。旧权限跳过未通过修改系统权限绕开。

首次数据定向运行曾因pytest临时目录父目录未创建而发生fixture失败，创建目录后完整复跑通过；这是测试运行准备问题。实际模型文件在受限执行环境中曾被拒绝访问，随后使用获准的正常Windows权限完成只读检查及真实验证；不是模型损坏，没有重新下载或修改模型权限来掩盖问题。

## 5. 本轮真实模型、HTTP及进程验证

完整逐步证据见[live.json](../../runtime/verification/asr2b/live.json)，普通替身故障测试与下列真实结果分开。

| 场景 | 实际结果 |
|---|---|
| 平台默认启动 | API、投递器、Agent、Vision、页面就绪；Agent在线，ASR默认stopped，没有自动加载Whisper |
| 显式启动与重复启动 | 模型真实就绪，重复start不增加模型实例；缺少HTTP令牌和旧修订命令被拒绝 |
| 公开中文样例 | 现有5.5466875秒音频真实转写成功；使用现有Whisper small CPU |
| 转写中HTTP停止 | 用现有公开音频扩展为44.3735秒进行执行中停止；当前任务成功收尾，后续queued任务未继续领取，原截止时间保持，模型真实句柄确认退出 |
| 独立停止期间 | API、Vite 5173和代理仍可访问，旧结果可读取；真实Vision任务成功，未随ASR停止 |
| 再次启动 | 新Worker会话真实就绪，原queued任务按既有编号和期限成功完成 |
| 整个平台停止期间仍在转写 | 另一条44.3735秒音频成功完成，随后Agent确认模型退出及自身关闭，整个平台安全停止 |
| 再启动整个平台 | ASR仍默认stopped，旧2条legacy观测及4条受管启动历史保持 |
| 最终停止 | 所有本项目受管入口安全停止，5000/5173空闲；不操作无关进程 |

44.3735秒音频只用于稳定地观察执行中停止，不是增加输入限制、准确率评估或容量压测。HTTP访问页面及代理是可达性检查；本轮没有新增前端测试、构建或浏览器自动化，不将其称为ASR-3B浏览器闭环。

## 6. 首次真实验收故障与修复

首次真实验收中，独立ASR停止排空通过，但“整个平台停止时仍在转写”发现原生Fortran库收到Windows `CTRL_BREAK`，模型子进程被提前中断。原任务`b5faf20dd06a4db694e559ab6d951e21`进入`retry_wait`；初次证据保留在[initial-failed-live/live.json](../../runtime/verification/asr2b/initial-failed-live/live.json)，没有删除任务或失败报告。

随后在模型子进程接入控制台隔离（`FreeConsole`），避免平台父进程停止信号直接中断原生推理，受管Job及进程句柄仍负责生命周期保护。修复后重新实际验证长音频平台停止排空、退出确认和整套重启，并补充真实进程信号回归。原失败任务通过原有限重试机制最终`attempt=2`成功，保留真实恢复历史。

## 7. 限制、交付与下一步

- 只实现本机固定CPU ASR实例，不包含GPU、多节点、资源账本、自动扩缩容、Docker或课题组服务接入；真实LLM仍未配置。
- Agent异常死亡后，旧受管会话退出未确认时保守阻断，没有自动接管或清理流程。需保留日志并人工核对，不能删锁、填退出时间或改数据库绕过。
- 服务管理页面仍留给ASR-3A，完整真实浏览器演示与集中交付留给ASR-3B。本轮不冒充页面闭环已完成。
- 操作方法见[ASR独立启停说明](../ASR独立启停说明.md)，全部接口见[接口说明](../接口说明.md)。已同步本地计划、进度、两级AGENTS、实现对照及运行说明。
- Multica本轮不写远端。本地v2.7、远端最后成功同步v2.6及原同步时间保持；本轮只追加本地待汇总事件。ASR-2A/2B复用BING-5，ASR-3A/3B复用BING-6，ASR-3B本机闭环交付后再集中同步。

开发侧交付后暂停，等待用户验收或要求进入ASR-3A。帮助理解的问题：为什么平台启动成功后ASR仍可能没有加载？为什么点击停止后要等待当前任务并确认退出？为什么重启ASR不能重新延长排队任务的截止时间？
