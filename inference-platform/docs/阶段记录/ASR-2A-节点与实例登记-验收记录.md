# ASR-2A：节点与实例登记验收记录

实施日期：2026-10-14。计划版本：v2.7。当前状态：已完成开发侧交付，待用户验收并暂停。

开发侧交付登记时间：2026-10-14 09:07:15（北京时间）；不是用户验收时间，不计算总开发工时。

## 1. 本次授权与范围

用户在v2.7规划落盘后以“进入下一项”授权本部分。目标是给真实笔记本、一个CPU ASR实例和实际启动会话建立可查询的持久化档案，明确“已登记”和“已运行”是两回事。完成后讲解并暂停，不自动进入ASR-2B。

本轮不改变原Windows管理器及Worker的启停归属，不实现Node Agent、独立ASR启停或服务管理页面。原五服务模式保持；ASR-1仍待用户验收，ASR-2B/3A/3B未开始。模型、音频限额、GPU、多节点、YOLO/LLM新功能及原13A不在本轮范围内。

## 2. 实施前核查与配套备份

2026-10-14 08:54:10（北京时间）完成基线核查并登记开发进行中。这是实际核查记录时间，不补造精确首次动工时间，也不计算总开发工时。

- 沿用`project311`，Python3.11.16；现有依赖与锁定清单匹配，没有新增依赖或下载模型。
- 默认库迁移版本`20261013_0007`，23条任务、23个上传对象、2个模型/2个版本、`online_requests=0`，没有活跃任务。
- 基线核查时平台已停止，5000和5173端口空闲；这是当时的状态，不代表后续读取本文件时仍停止。
- 已用SQLite备份方式保存数据库，同时复制23个上传对象并核对哈希。备份和基线位于`runtime/verification/asr2a/`，不能把仅备份数据库当作完整业务备份。

证据：[实施前基线](../../runtime/verification/asr2a/before.json)。后续迁移、实际登记、重复登记、重启持久化、旧数据保持及测试结果按真实执行补充。

## 3. 实现边界

以下职责已落盘，并有下方测试与实际运行证据：

- 本机维护命令登记真实Windows节点身份及默认一个CPU ASR实例，绑定已有准确Whisper版本，不修改原模型定义。
- 登记不会启动进程；初次没有实际运行报告时不生成虚构会话、就绪状态或GPU数据。
- 显式观测命令只读取已有ASR Worker真实报告并归档其会话。来源是旧Worker报告，不是Agent心跳；报告新鲜度按数据库时钟判断。
- 查询只读；过期报告、旧PID、曾报告停止均不能用作当前受管进程退出证明。
- 新表通过Alembic迁移，状态规则位于service、持久化位于data、输入输出位于schema，入口只负责衔接。

## 4. 验收登记

| 项目 | 当前结果 |
|---|---|
| 环境与真实数据基线 | 已核对，见上文；不等于业务实现完成 |
| 迁移前数据库/上传配套备份 | 已保存并核对 |
| 数据迁移及原业务记录保持 | 0008首次/重复升级、check、完整性与外键检查通过；旧业务行与23上传哈希保留 |
| 真实本机登记、重复登记与稳定身份 | CLI实际登记1节点/1实例，重复不新增、不换ID；初始0会话 |
| 只读接口、重启持久化及真实会话观测 | 5个GET、两轮原五服务启动停止与HTTP查询通过，2个不同真实Worker会话保留 |
| 后台回归及异常测试 | 1071通过、2旧Windows符号链接权限跳过，165.89秒；退出0，无warning |
| 本轮真实模型/浏览器验证 | 两轮真实Whisper/Vision加载就绪；未提交新转写/检测，没有浏览器自动化验收；不是Agent独立启停闭环 |
| 用户验收 | 待确认 |

## 5. 实际迁移与登记证据

- 本轮通过Python脚本调用Alembic的upgrade/check将默认库从0007升级至`20261014_0008`，重复升级和结构检查通过。可复跑的等价CLI命令为`python -m alembic upgrade head`和`python -m alembic check`。2026-10-14 08:59:03核对原9张业务表的原行全部不变，三张新增表为空；SQLite完整性为ok、外键检查为空。证据：[migration.json](../../runtime/verification/asr2a/migration.json)。
- 08:59:45首次`python -m backend.modules.nodes.node_registry register-local`登记真实`LAPTOP-IA9BJCS3`，Windows `10.0.26200`、AMD64、20个逻辑处理器；GPU标为未探测。节点ID为`688abc6f22cf5a36aba439606670cb25`，ASR实例ID为`3810428ae98152ef9426e337e461377d`，绑定`whisper-small`固定版本`536b0662742c02347bc0e980a01041f333bce120`。
- 重复登记复用原ID，`nodeCreated=false`且`instanceCreated=false`，实例绑定及登记时间不变；只更新节点元信息采集时间。登记不启动Worker，也不添加假会话。登记前的旧Worker报告实际返回`recorded=false/reason=pre_registration_report/session=null`。
- 实际命令输出：[首次登记](../../runtime/verification/asr2a/register-first.stdout.json)、[重复登记](../../runtime/verification/asr2a/register-repeat.stdout.json)、[拒绝归属登记前旧报告](../../runtime/verification/asr2a/observe-before-start.stdout.json)。维护命令与响应解释见[节点与实例登记说明](../节点与实例登记说明.md)。

## 6. 实际启动、观测、停止与重启

本轮使用已有`python -m backend.modules.platform.platform_cli start/stop/status`进行两轮完整平台启停；等价的日常PowerShell脚本保持原样。两轮均真实加载现有Whisper和Vision，`/api/runtime`中模型就绪；API及页面HTTP可达，5个新增GET、原任务列表和一份历史ASR结果读取成功。第二轮验证API/平台重启后节点和实例ID保持，第一次会话仍可读取。

| 轮次 | 实际Worker会话 | 启动时间（北京时间） | 观测结果 |
|---|---|---|---|
| 1 | `f35c6b9165c448a1a90a2ab6bb5f7576` | 08:59:59 | 就绪报告显式保存，停止后同会话更新为stopped报告 |
| 2 | `820d817b31b64540a439c6ea65f25d82` | 09:00:22 | 独立新会话保存，旧会话显示superseded，停止后报告保留 |

两轮均由原Windows管理器确认其所有受管服务退出，状态脚本确认管理器不运行、5000/5173空闲、HTTP不可达。**这是旧管理器的真实退出证据，不是新增节点档案自己确认了退出。**新增会话即使保存stopped报告，`exitConfirmed`仍为false；实例`runtimeState`仍为unknown。没有Node Agent、独立ASR停止或在途任务排空实现。

完整证据：[live.json](../../runtime/verification/asr2a/live.json)。根代理收尾时另查询[最终平台状态](../../runtime/verification/asr2a/final-platform-status.json)：管理器未运行，5000/5173可用，API及页面HTTP不可达。本轮没有上传新的音频/图片，没有新增推理任务或重新验收转写效果；模型加载、HTTP读取和历史结果可用性与ASR-1真实转写历史分别记录。前端没有改动，没有复跑前端测试、构建或浏览器自动化；页面HTTP可达不等于浏览器闭环验收。

2026-10-14 09:01:17最终数据核对：原8张业务表全部原行一致、23个上传对象哈希保持，默认库仍为23任务、2模型/2版本、online0；新增1节点、1实例、2会话。`runtime_components`因真实启停而正常更新，不把运行心跳变化误记为旧任务变化。SQLite完整性及外键检查通过，见[preservation.json](../../runtime/verification/asr2a/preservation.json)。环境检查8项全部通过，含0008、Python3.11.16、依赖、既有两模型文件SHA、Node24.15.0和前端依赖，见[environment.txt](../../runtime/verification/asr2a/environment.txt)。

## 7. 后台回归与文档核对

实际在开发目录执行：

```powershell
& 'C:\Users\86199\miniconda3\envs\project311\python.exe' -m pytest -q --tb=short --basetemp=runtime/tmp/asr2a-full-tests-1 --junitxml=runtime/verification/asr2a/backend-tests.xml
```

最终**1071通过、2跳过，165.89秒，退出码0，无warning**。两项跳过仍为既有Windows账户创建符号链接权限不足（WinError1314），未修改系统权限。记录：[标准输出](../../runtime/verification/asr2a/backend-tests.txt)、[JUnit结果](../../runtime/verification/asr2a/backend-tests.xml)。

本轮新增测试覆盖重复/并发登记和稳定身份、准确模型绑定与冲突回滚、迁移旧数据保持/约束/隔离回退、只读GET和分页校验、真实采集接口的隔离异常、无报告/登记前旧报告、同毫秒状态变化、旧会话/迟到报告、报告过期/时钟异常及不可伪造退出确认。隔离测试里的机器和报告用于验证规则，不作为默认库真实节点或模型运行证据。

本地文档、链接、函数ID、备份读回、受保护文件及远端映射保留的最终检查单独保存为[final-checks.json](../../runtime/verification/asr2a/final-checks.json)，不将文档核对项累加为后台测试通过数。

## 8. Multica与后续

本次仅在本地计划、进度、阶段记录及`multica-sync.json`的`deferred_updates`保存事实。ASR-2A/2B共同归属既有BING-5，本地计划仍v2.7、远端最后成功同步仍v2.6，原任务ID、状态和同步时间不变；没有联网更新远端，没有绑定执行运行时或启动后台同步。

ASR-2B/3A/3B依次另行获准实施，四部分完成且ASR-3B开发侧闭环验收通过并交付后，再集中同步BING-3/5/6。ASR-4继续等待真实调用和管理资料。

## 9. 帮助理解的问题

1. 为什么登记了一条ASR实例，还不能说模型已经加载？
2. 为什么Worker报告stopped或心跳过期，不能直接把`exitConfirmed`改成true？
3. 下一步ASR-2B引入Agent后，为什么仍只能由一个管理者负责同一ASR实例的启停？
