# 数据库迁移说明

## 当前本机-2C：只读诊断与页面，不新增迁移

本轮没有新增ORM表或迁移文件，代码head继续`20261020_0014`。scheduling状态与任务解释只读取既有记录，不通过GET回填旧任务、更新选择或延长TTL。2C日常前后26表（含alembic，即25业务表）及42任务/42上传保持，完整性ok、外键无违规，.env与路由关闭配置不变；证据见[本轮阶段记录](../docs/阶段记录/本机-2C-ASR调度页面与故障说明-验收记录.md)、[读取前](../runtime/verification/laptop-2c/daily-before.json)和[读取后](../runtime/verification/laptop-2c/daily-after.json)。这是只读验证后的保持检查，没有执行新迁移，不把2B迁移证据当成本轮新迁移。下方0014及更早内容保留原阶段含义。

## v2.12本机-2B：0014路由与任务选择（历史，待用户验收）

代码head新增`20261020_0014`，前置`20261020_0013`。只新增`asr_routing_revisions`（逻辑模型、资料类别及不可变路由快照）和`asr_task_routes`（任务绑定路由与一次选择快照/时间）及外键、唯一性、字段一致性约束和查询索引；不改旧任务、模型、调用指纹、许可和结果，不种入路由、候选或任务选择。

任务受理保存路由修订，真正领取时才在原事务内冻结候选。0014迁移不反填旧任务；旧TaskView不会因此增加routing字段。目录0013与本次任务路由是两层记录，登记路由也不授予真实执行权限。

本轮09:39:49完成0013日常库与42上传的成套备份；09:40:54—55首次/重复升级0014及metadata、完整性、外键检查通过，23旧表与42上传哈希保持、新两表为空，.env不变、日常路由未配置。证据见[备份](../runtime/verification/laptop-2b/daily-before.json)、[迁移核对](../runtime/verification/laptop-2b/daily-migration.json)及[本机-2B记录](../docs/阶段记录/本机-2B-调度策略接入任务执行-验收记录.md)。代码与日常库均0014；后续升级前仍须确认相关进程退出，配套备份数据库和上传文件。

降级必须在线核对；任一新表非空时拒绝有损降级，只有两表都为空才可移除。不能通过删除路由/任务选择或回退结构释放未知调用。原0013及更早迁移说明保留，当前以本节为准。

## v2.12本机-2A：0013目录与资源记录（历史，待用户验收）

代码迁移新增`20261020_0013`，前置为`20261019_0012`。它只建立`asr_catalog_revisions`与`asr_resource_observations`及外键、唯一约束、范围约束和有界查询索引；复用原`asr_compute_targets/asr_target_revisions/asr_capacity_groups`，不改原任务、许可、模型版本或结果，不种入目标、目录或资源报告。

`asr_catalog_revisions`按追加顺序保存目录ID、目标、连接修订及元数据快照；`asr_resource_observations`保存目录修订、来源、target/capacity_group范围、采样时间、TTL与报告内容。登记修订和观测由Service/CLI显式完成，迁移成功不代表资源已核实、新目标已启用或真实调度已接入。

本轮于2026-10-20 08:54:01完成日常0012与42上传的成套备份，09:03:54—09:03:55实际完成0012→0013首次升级、重复升级与Alembic metadata检查；21旧表内容及42份上传哈希一致，新两表均0行，SQLite完整性和外键通过。代码与日常库当前均0013；隔离降级保护及兼容测试见[本机-2A阶段记录](../docs/阶段记录/本机-2A-ASR目标配置与资源记录-验收记录.md)。证据：[备份核对](../runtime/verification/laptop-2a/daily-before.json)、[日常迁移核对](../runtime/verification/laptop-2a/daily-migration.json)。以后日常升级仍须先确认无项目进程占用，配套备份数据库与上传文件；查询代码版本与日常库版本是两件事。

0013降级必须在线核对；任一新表已有记录时拒绝有损降级。只有两张新表都为空才允许移除它们；不要删除目录、观测或旧许可来绕过保护，也不能用回退结构释放未知请求。实际执行方式沿用下方Alembic命令。

## v2.9-2B迁移与日常验证（历史，已交付，待用户验收）

v2.9-2B“受控并发执行与页面验证”已于`2026-10-19T14:00:46+08:00`完成开发侧交付，待用户验收并暂停。日常0011→0012升级、隔离多槽及页面、真实限额1转写与重启只读恢复均已验证；原41任务/41上传及39份成功结果保持，最终42任务/42上传、40份成功结果，全部本机服务退出、锁释放。后台全量1464通过、1项历史测试版本断言失配、2项旧Windows权限跳过；修正后20项定向及Windows状态文件修复46项定向分别通过，未重复完整全量。真实设备仍unknown、目标/组限额1；第3部分GPU优先与CPU承接未实施，旧阶段用户验收不变。

代码head为`20261019_0012`；日常基线`20261019_0011`于`2026-10-19T13:18:22+08:00`完成新的成套备份：41任务、41上传、全表副本/源数据与哈希一致，SQLite完整性ok、外键违规0。不要沿用2A起步40任务/40上传作为本次基线。日常0011→0012于`2026-10-19T13:43:20+08:00`实际完成首次/重复升级、metadata检查：旧表旧列/行和41上传保持，新三列为NULL、新审计表为空，仅预期索引变化，完整性ok、外键违规0。代码与日常head均为0012；随后一次真实浏览器转写及重启只读验证另行通过，最终42任务/42上传，迁移本身不新增业务任务。

0012仅给`asr_target_worker_sessions`增加可空的`active_task_id`、`child_pid`、`last_error`，历史行这三列应为NULL；新增`asr_capacity_changes`审计表及目标/时间索引。移除`remote_asr_calls`的旧profile单活动调用唯一索引`uq_remote_asr_calls_active_profile`，增加普通`ix_remote_asr_calls_profile_state`查询索引。其余旧表旧列、原行、历史模型spec和旧结果不改变，迁移不种入容量变更或执行记录。

移除数据库旧唯一索引不等于取消并发保护：调用池仍独占旧global文件锁，兼容阻止旧版Worker；每槽持有独立槽锁，并由事务化目标/共享组准入及每会话单许可约束控制领取。日常真实限额仍为1。

本轮`runtime/verification/concurrency-2b/upgrade_daily.py`已由主执行流程执行：迁移前两次确认项目进程与监听为空，持有平台/Worker/上传维护锁，核对新备份与源一致后明确升级0012、重复升级并`alembic check`；旧行、新三列NULL、新审计空表、索引差异及上传逐项通过。之后首次平台启动发生Windows状态文件替换失败，属于启动阶段问题，不将其写成迁移失败；原失败与后续复验另见阶段记录。

0012降级须在线检查；已有审计、新槽报告字段、多槽限额或同profile多个活动调用时拒绝有损降级，不通过删记录、改库或回退结构释放未知占位。原始迁移与历史验收文件保留。

证据：[日常0012升级](../runtime/verification/concurrency-2b/daily-upgrade.json)、[本轮备份](../runtime/verification/concurrency-2b/baseline-20261019/verification.json)、[2B阶段记录](../docs/阶段记录/v2.9-2B-受控并发执行与页面验证-验收记录.md)。

2B交付已完成并集中同步Multica，完整读回通过：BING-3 revision=7、BING-7 revision=8，均保持in_progress，正文明确待用户验收。最后写入读回`2026-10-19T14:02:54+08:00`，完整核对`2026-10-19T14:03:04+08:00`。本地与远端计划均v2.9，2条事件已归档，deferred_updates与pending为空；未新建任务，BING-2/4/5/6、评论、项目/资源与runs保持，teacher daemon仍停止。不绑定执行运行时、不启动后台同步；第3部分GPU优先与CPU承接尚未实施。

## 2A迁移状态（2026-10-19历史，已验证）

代码与日常数据库head均为`20261019_0011`。`11:48:31+08:00`完成源库0010及40份上传配套备份；`12:07:49+08:00`完成首次0010→0011、重复升级与`alembic check`。迁移后全部旧表逐行、40份上传哈希保持，新五表为空，SQLite完整性ok、外键违规0。随后受控登记与真实单槽调用另记证据，不把迁移成功当作模型执行。

0010保存外部同步调用事实`remote_asr_calls`。0011只新增`asr_capacity_groups`、`asr_compute_targets`、`asr_target_revisions`、`asr_target_worker_sessions`、`asr_call_permits`五表及约束/索引；不反填旧调用的目标/设备/修订，不移除旧profile单活动调用唯一索引。新执行通过permit关联修订；旧1条released调用不补造permit。

首次升级尝试在迁移前被进程检查发现测试子进程而阻止，没有进入迁移或修改日常库；测试结束复核后才实际升级成功。升级前必须确认全平台/手动入口停止，并保留数据库与上传成套备份。任何新表已有记录时0011拒绝有损降级，不用降级或删记录释放未知许可。本轮未做恢复演练。

证据：[备份报告](../runtime/verification/concurrency-2a/baseline-20261019/verification.json)、[日常迁移核对](../runtime/verification/concurrency-2a/daily-upgrade.json)、[最终数据保持](../runtime/verification/concurrency-2a/daily-final-verified.json)、[2A记录](../docs/阶段记录/v2.9-2A-计算目标与容量准入-验收记录.md)。下方旧阶段head只保留当时含义。

2A交付已完成并集中同步Multica，完整读回通过：BING-3 revision=6、BING-7 revision=7，均保持in_progress，正文明确待用户验收。最后写入读回`2026-10-19T12:19:01+08:00`，完整核对`2026-10-19T12:19:11+08:00`。本地与远端计划均v2.9，2条事件已归档，deferred_updates与pending为空；未新建任务，BING-2/4/5/6、评论、项目/资源与runs保持，teacher daemon仍停止。不绑定执行运行时、不启动后台同步；下一项2B尚未实施。

Alembic负责记录数据库结构的版本。第一部分的`20261012_0001`是空基线；第三部分`20261012_0002`增加`tasks`、`idempotency_records`、`dispatch_events`。第四部分`20261012_0003`增加M07的`notification_records`、实际运行报告`runtime_components`，并给任务补充租约、开始/结束时间、结果和失败字段，给待投递事件补充通知编号、投递时间与失败重投信息。第六部分`20261012_0004`增加固定排队/总期限、单次期限、最大次数、下次重试时间及`execution_attempts`执行历史表。

第八部分`20261013_0005`建立`models/model_versions`，登记现有Whisper准确版本并保留原任务。第十部分`20261013_0006`增加任务`input_media_type/image_width/image_height`，允许视觉任务的音频时长为空，通过检查约束使任务类型与输入元数据匹配；运行组件增加`vision_worker`。0006不登记或启用YOLO；使用维护命令和[准确规范](../docs/examples/yolo-model.json)完成登记，见[模型管理说明](../docs/模型管理说明.md)。

第十二部分的`20261013_0007`：只新增独立`online_requests`表，记录在线生命周期、许可、时间和字符计数；不保存聊天正文、回答、endpoint或key，不改既有批任务/模型表，不种入LLM模型。调用者时间分页、实例许可及状态租期有索引，状态与非负计数有约束。没有真实LLM服务也应完成本次结构升级，才能正常读取“未配置”和空历史。

ASR-2A的`20261014_0008`：仅新增`compute_nodes`、`service_instances`、`instance_sessions`三表及其唯一约束、外键与分页索引，不改旧表，也不自动登记机器或构造启动会话。节点身份哈希唯一，实例绑定节点和准确模型版本，同一实例的同一真实Worker会话只保存一条最新已采样报告。当前ASR实例固定CPU和`asr-default`，实际登记与观测由[本机维护命令](../docs/节点与实例登记说明.md)执行；迁移成功不等于模型已运行。

ASR-2B交付时的代码head为`20261014_0009`：只新增 `agent_controls` 和 `managed_instance_sessions`，分别保存单实例管理意图/Agent报告与受管启动/退出历史。0008及之前表结构和旧观测保持；迁移不种入运行记录，Agent实际取得唯一管理权后才创建控制记录，启动尝试另保存会话。默认库实际迁移和旧数据保持见[本轮阶段记录](../docs/阶段记录/ASR-2B-独立启动与安全停止-验收记录.md)，本轮已验证升级、重复升级与结构一致，旧表内容完全保持。

待投递事件属于 M05 的可靠受理记录，通知表属于 M07；它们共用 SQLite 文件，但职责分开。升级保留原任务和结果。0004 按旧记录的原始创建/开始时间回填600/1800/900秒期限，保留微秒精度，最大次数补为2；旧成功任务不会改成失败，旧非终态若早已超期则由新投递器收尾。应用启动不会调用 `create_all()`，不会代替迁移修改表结构。

先通过平台管理器及Agent确认受管ASR退出，再停止API、投递器、Agent和Vision等入口，并备份数据库及配套上传文件。在PowerShell中进入`inference-platform`后，沿用`project311`：

```powershell
$projectPython = 'C:\Users\86199\miniconda3\envs\project311\python.exe'
& $projectPython -m alembic upgrade head
& $projectPython -m alembic current
& $projectPython -m alembic check
```

`upgrade head` 将数据库更新到已有迁移的最新版本；重复执行不会重复创建结构。`current` 查看数据库当前版本。`check` 比较已导入的 SQLAlchemy 模型与数据库结构，检查是否遗漏迁移；它不能证明业务规则正确。

第十部分默认数据库已实测升级0005→0006、重复升级与结构检查通过；迁移前11条ASR任务、11个音频和全部原表原列值保留，证据见[迁移检查](../runtime/verification/part10/migration-check.json)。后续测试和真实视觉任务必须单独验收，不能仅凭迁移成功认定业务可用。

第十二部分默认库已成对备份后升级0006→0007，重复升级与check通过；原17任务、17上传哈希、2模型/2版本及旧表数据保持，新增在线表为空，47项已安装Python分发版本不变。证据见[本轮迁移保持检查](../runtime/verification/part12/migration-preservation.json)。本轮最终测试与运行状态另见[阶段记录](../docs/阶段记录/第12部分-LLM在线流式通道-验收记录.md)。

ASR-2A在配套备份后实测0007→0008、重复升级、`alembic check`、SQLite完整性和外键检查。2026-10-14 08:59:03核对时，原9张业务表的全部原行保持，新3表为空；之后显式登记真实本机与实例，不能把这些后续写入混作迁移seed。证据见[ASR-2A迁移核对](../runtime/verification/asr2a/migration.json)和[阶段记录](../docs/阶段记录/ASR-2A-节点与实例登记-验收记录.md)。

数据库路径统一由 `backend.config.settings.app_settings.load_settings()` 读取，再由 `backend.data.database.database.build_database_url()` 转为数据库 URL。`alembic.ini` 不保存另一份路径，避免应用和迁移连接不同的数据库。程序化测试可以向 `alembic.config.Config.attributes["settings"]` 传入临时 `AppSettings`，隔离真实运行数据。

以后获得相应阶段开发授权并新增 ORM 模型时：

1. 模型继承 `backend.data.database.base.Base`，按规范提供复数 `snake_case` 表名与 `to_dict()`。
2. 在 `migrations/env.py` 的模型导入注释处显式添加绝对导入，让表进入 `Base.metadata`。
3. 执行 `python -m alembic revision --autogenerate -m "describe schema change"` 生成候选迁移。
4. 人工检查生成的 `upgrade()`、`downgrade()`，特别关注删除列、改名与数据保留；自动生成不能替代审查。
5. 在测试数据库验证迁移后执行 `python -m alembic upgrade head`，再用 `python -m alembic check` 确认模型与结构一致。

SQLite 采用 Alembic 的 batch 模式生成结构修改。0004 需要读取旧任务并分批回填日期，不支持仅生成离线 SQL 的迁移流程；请在备份后连接目标数据库执行上述命令。

`python -m alembic downgrade base` 仅用于测试数据库回退。0004 降级会丢失执行历史与期限；存在 `retry_wait`、`cancel_requested`、`cancelled` 时，在任何删除操作前明确拒绝降级，因为旧API不认识这些状态。第四部分降级到 `20261012_0002` 会删除通知、运行报告、任务租约和结果等新增数据；继续降到 `20261012_0001` 会删除第三部分三张表及全部记录。不要在有任务的数据库随意降级。

0006降级为0005前会检查视觉任务和`vision_worker`运行记录；只要存在任一项就拒绝，不自动删除记录来满足降级条件。已有业务数据时，按配套备份恢复方案处理，不把降级当作日常清理工具。

0007降级为0006会删除整张在线请求表及其元数据，原ASR/图片表不变；隔离迁移测试覆盖该行为。它不通知远端引擎停止，也不能用于清除uncertain许可。已有在线记录时，应先核对引擎/平台停止状态和配套备份，不在日常库随意回退。

备份和恢复应将数据库与上传目录作为配套数据；仅恢复旧数据库会使较新文件失去引用。第四部分的备份路径保留在 `runtime/verification/part4-real-pipeline.json`；第六部分实际备份与迁移证据见 `runtime/verification/part6/real-pipeline.json`。未删除原上传对象。升级、原始微秒时间及结果保留、测试数据库降级和新状态降级拒绝均有自动测试。

0008降级至0007会按会话→实例→节点顺序删除这三张新表及其档案；它不停止Worker或确认退出，不能作为日常取消登记、释放资源或切换机器的办法。默认库不执行降级；隔离测试与配套备份恢复的边界分别说明。

0009降级至0008会删除Agent控制与受管启动历史。若任一受管会话的 `exit_confirmed_at` 为空，或Agent的 `shutdown_confirmed_at` 为空，迁移在删除前明确拒绝；显示stopped或心跳过期不能绕过。降级必须连接目标库检查，不支持离线降级SQL；不要为了通过检查手工填写确认时间。默认库不执行降级。
