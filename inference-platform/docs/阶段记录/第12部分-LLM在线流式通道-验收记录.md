# 第十二部分：LLM在线流式通道

登记日期：2026-10-13。用户要求继续，并明确回复“暂时没有，先完成接口、页面和异常测试”。本轮仅实施[开发计划](../开发计划.md)第十二部分的获准范围，完成并讲解后暂停，不进入第十三部分。

**当前状态：用户获准的接口、页面与异常测试已完成开发侧交付，等待用户验收；暂停，不进入第十三部分。真实endpoint与模型尚未提供，真实LLM生成和引擎资源回收仍未验收。**接口、页面、测试替身与真实模型验证分开登记；任何模拟内容都不能称为真实LLM输出。

## 1. 本轮边界

- 按v2.1 M01/M02/M03/M13组织独立在线文本请求、有限准入、流事件、断线/取消与幂等finalize；不进入ASR/YOLO的M07离线通知队列。
- 建立独立OnlineRequestRecord，记录请求生命周期、首内容时间、结束原因等，不保存完整提示词、聊天正文或生成正文，不把正文/密钥写入普通日志。
- 在现有工作台增加在线页面与错误反馈。没有配置时明确不可用，不自行提供虚假的可用模型，不在日常程序中自动返回模拟回答。
- 用隔离替身验证协议与失败路径，保持ASR/YOLO接口、数据及页面兼容；真实生成、具体引擎取消和资源回收等待实际服务。
- 不下载模型、不接入GPU或远程服务器、不修改/发布原架构网页。具体接口与配置已按实际代码登记在[接口说明](../接口说明.md)、[模型维护说明](../模型管理说明.md)和[env.example](../../env.example)。

## 2. 已完成的准备

已依次重读根和开发目录AGENTS、完整开发计划/进度、根后端规范与项目补充约定、目录与开发约定；查阅v2.1的LLM在线主链路、在线在途统计、OnlineRequestRecord、M01/M02/M03/M13及统一清理规则。计划更新为v2.4。

本轮先实查停止状态和默认库，再将数据库与17个上传对象成对备份到`runtime/verification/part12/before-development-9c1de1f92f554ae1a0c9adee4e43440e/`，基线见[before-development.json](../../runtime/verification/part12/before-development.json)。原规范缺失的`0-生命周期规范.mdc`仍未声称读取。

已升级0006→0007、重复upgrade及check通过。原17任务、全部旧表数据、17个上传哈希、2模型/2版本保持，在线表初始0行；47个已安装Python分发版本未变，见[migration-preservation.json](../../runtime/verification/part12/migration-preservation.json)。仅把已安装httpx2 2.13.1及5项传递依赖从asr清单归入base，没有pip install；没有下载模型。本轮最终数据、启停与兼容检查见第4节，迁移快照与最终检查分开保存。

## 3. 实现与契约登记

代码已落盘，以下是实现范围；测试是否通过以第4节证据为准：

- `backend/config/llm/model_catalog.py`根据四项明确配置生成准确模型规范；不含endpoint/key。必须显式登记、启用、设默认，默认未配置的LLM目录为空。`serviceVerified=false`只如实保留未做真实模型验收的事实，配置状态不是健康探测。
- `backend/controller/llm/online_controller.py`新增状态、创建、分页、单条、取消5个接口，统一前缀`/api/llm`。创建为JSON POST→SSE，公开`accepted/heartbeat/delta/done/error`事件，包含请求编号和递增sequence；流前拒绝仍用JSON错误。重复onlineRequestId一律409，不重新生成。
- `backend/schema/llm/online_schema.py`约束1—32条纯文本消息、总16,000字符及至少一条user；温度0—2，maxTokens整数1—2048。在线客户端请求体128KiB、输出131,072字符及有界缓冲。API支持有限消息数组，当前页面发送单条用户文本。
- `backend/data/llm/online_models.py`的独立`online_requests`经0007创建。保存生命周期、模型/实例标识、字符计数和安全错误；正文、密钥和endpoint不作为列或结果保存。状态分accepted/waiting_instance/streaming/completed/failed/cancelled；许可分waiting/held/released/uncertain。
- `backend/service/llm/online_service.py`使用单静态实例、SQLite写事务准入、独立监督线程、有限等待、固定总期限、连接断开/取消和幂等finalize。ASR/图片与在线活动记录共享全局20在途，在线实例默认1许可；不实现M09动态池、GPU资源账本或集群许可。
- `backend/service/llm/engine_adapter.py`以已有httpx2调用文本Chat Completions SSE，明确连接/读取/总期限；校验UTF-8/帧/模型名/结束标记，拒绝当前不支持的工具、多模态和隐藏推理内容。无自动重试、重定向、系统代理或假回答回退；流异常不拼接另一轮生成。
- 通用HTTP连接关闭不能确认远端计算已停止，保留uncertain占位。维护入口`python -m backend.modules.llm.requests_cli spec`导出准确规范；`release ID --confirm-engine-stopped`仅在维护者完成引擎端核对后释放已结束且uncertain的记录，不执行远端终止。
- React页面新增“文本生成”工作台、未配置提示、准确版本与参数、增量文字、停止、部分输出说明、复制/TXT下载及元数据历史。`?type=llm&request=编号`刷新只恢复记录，正文不持久保存；隐藏或离开页面尝试停止，不套用ASR/图片的离线续跑规则。

未来真实服务配置/运行步骤见[使用与故障处理](../使用与故障处理.md)。上游协议参考[vLLM官方在线接口](https://docs.vllm.ai/en/latest/serving/online_serving/)，网络超时含义参考[HTTPX2官方超时说明](https://pydantic.dev/docs/httpx2/advanced/timeouts/)；这些资料不是本项目真实模型验收证据。

## 4. 验证登记

| 项目 | 当前状态 |
|---|---|
| 本轮环境、进程、数据库/上传基线及必要备份 | 已实查并成对备份，最终数据保持通过 |
| 默认未配置API/页面明确不可用 | 真实默认API与Edge页面通过：目录为空、配置false、POST503 LLM_NOT_CONFIGURED，无在线新记录/假回答 |
| 请求校验、准确版本/参数、有限在线准入 | 后台测试通过；默认无真实模型，已配置情形使用隔离替身 |
| 内容/结束/错误、UTF-8分块、上游异常/超时/慢消费 | 单元/集成及隔离HTTP协议替身通过，非真实模型输出 |
| 停止、客户端断开、幂等finalize、保守许可及人工释放 | 后台与页面替身测试通过，不能据此称真实引擎或GPU已停止 |
| 独立生命周期与元数据隐私 | ORM/Schema、公开字段及协议错误检查通过；不保存输入/回答正文或上游凭据 |
| 0006→0007、重复upgrade/check及旧表保留 | 默认库实查通过；独立迁移/Schema34项已纳入全量，不重复累加 |
| 后台全量 | **941通过、2跳过，160.68秒**；943项收集一致，2项为既有Windows符号链接权限限制，不是LLM跳过 |
| 后台最后针对性回归 | **51通过，14.78秒**；补验空白上游模型名/port0拒绝、状态赋值归Repository.update及目录/导入边界；没有在这次小修后重跑全量，不将51加到941 |
| 前端与构建 | 最终**135项通过（12文件）**及tsc/Vite构建通过；含页面后退/跨请求隔离修正新增3项单元回归 |
| Edge浏览器 | **9项通过，33.3秒**：6项真实接口/模型页面 + 3项单独标注的模拟；不存在真实LLM生成用例。最后URL后退修正用新增单元回归验证，未再次启动浏览器 |
| 三视口 | LLM1920×1080、1366×768、390×844截图均已实际查看，布局正常、无页面横向溢出或假回答 |
| 既有真实模型兼容 | 新真实ASR1条、公交车/白图Vision2条均成功；坏音频与坏图片拒绝，不冒充LLM验证 |
| 最终数据 | 17旧任务与17旧上传哈希、旧业务行保持；默认库20任务：ASR12成功/2取消，Vision6成功；online_requests仍0，2模型/2版本和47包版本保持 |
| 实际启动/停止 | 默认五服务真实就绪、两个Worker模型ready；安全停止后5000/5173释放，协议替身监听也已关闭 |
| 真实LLM生成、引擎取消/GPU释放 | 尚无真实服务，明确未验证；未来提供服务后单独联调 |
| 用户验收 | 开发侧已交付待确认；不代替用户验收，不进入第十三部分 |

最终报告与证据：

- [后台全量XML](../../runtime/verification/part12/backend-tests.xml)、[最后针对性51项](../../runtime/verification/part12/final-llm-tests.xml)、[前端最终135项](../../runtime/verification/part12/frontend-tests.txt)、[类型检查与构建](../../runtime/verification/part12/frontend-build.txt)。全量之后的小型配置/分层修正用51项定向复核，没有再次全量，不累加重复执行的计数；前端最后修正以新增3项单元回归及135项全量/构建验证，未再次运行真实浏览器。
- [Edge9项报告](../../runtime/verification/part12/playwright-results.json)：真实用例为未配置LLM只读、ASR与坏音频、公交车/白图/坏图；模拟用例为LLM部分失败、LLM停止未确认及既有断网。浏览器网络替身只验证页面流程。
- [LLM未配置页面](../../runtime/verification/part12/llm-unconfigured-browser.json)：configured=false、generationPosts=0、pageErrors为空。截图[1920](../../runtime/verification/part12/llm-unconfigured-1920.png)、[1366](../../runtime/verification/part12/llm-unconfigured-1366.png)、[390](../../runtime/verification/part12/llm-unconfigured-390.png)均已查看。
- [隔离HTTP协议报告](../../runtime/verification/part12/http-protocol-report.json)：独立临时库、真实本地HTTP传输和协议替身，明确`realModel=false`。success→completed/released；partial-failure→failed/uncertain；cancel→cancelled/uncertain；相同编号重发409。未创建离线任务，两个监听已停止。它不能代替真实LLM推理/取消效果验收。
- [真实ASR报告](../../runtime/verification/part12/real-browser-report.json)：任务`b6ba95145b3443f3a7f75d9c5af4f177`；[公交车报告](../../runtime/verification/part12/vision-browser-report.json)：`3989d0dbd0ba4effbfc02c5945fdf068`；[空检测报告](../../runtime/verification/part12/vision-empty-report.json)：`e27b0e8832884533947bdd97f1e1ab6f`。结果、下载、刷新及三视口检查沿既有真实用例执行。
- [启动与安全停止](../../runtime/verification/part12/platform-stop.json)、[最终数据保持](../../runtime/verification/part12/final-preservation.json)。本轮没有重启后持久化复测；真实运行取消/child恢复仍是第十部分历史证据。本轮不重新宣称完成GPU或集群验收。

停止后网页与API无法打开是正常状态。再次使用，在开发目录执行`powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-platform.ps1`；核对状态/停止分别用`status-platform.ps1`和`stop-platform.ps1`，完整步骤见[使用指南](../使用与故障处理.md)。[最终只读检查](../../runtime/verification/part12/final-checks.json)已通过：25份文档链接0损坏、92个映射ID唯一、后台三级目录/绝对导入、pip check、941+2全量/51针对性/135前端/Edge9证据及最终stopped与端口释放均已核对。

测试中的正常流或故障流只证明对应协议和生命周期处理；不能用“HTTP真实传输”替代“真实模型推理”。已输出部分内容后失败应说明中断，不自动拼接另一轮生成；引擎停止或Permit清理结果不明时必须保守记录。

## 5. 需要理解

1. 为什么LLM使用在线流式连接，而不排进音频和图片的长期离线队列？
2. 为什么关闭浏览器或停止读取，不足以证明上游模型已经停止计算？
3. 接口与模拟异常测试通过后，还需要真实服务验证哪些事情？


## 6. 后续规划同步（2026-10-13，v2.5）

用户本次“实施此计划”授权的是将重规划结果同步到项目文件，不是开始13A开发。已保留第十三至第十六部分主编号，并在[开发计划](../开发计划.md)与[开发进度](../开发进度.md)细化10个子阶段：本机路径13A访问保护与配置/API Key（不做账号系统）→13B配套备份恢复/部署准备→14A真实CPU节点/实例会话心跳→15A单节点计划/原子预留（不启停实例）→15B本机受管执行/有限恢复→16A本机指标/小规模基线；服务器路径13C共享基础设施→14B多节点/GPU登记→15C多节点GPU调度→16B容量整体验收。

全部新子阶段为“已规划、未开始”，13A待用户要求实施；每个子阶段交付后暂停。第十二部分真实LLM联调仍独立等待真实服务，本次没有补做模型验证，也不推定第十二部分已获用户验收。ASR换模型、扩大20MiB/300秒限制继续后置。

以上仅为后续规划衔接。前文实施范围、测试数字、运行证据和用户授权记录逐字保留；本次没有改代码、启停服务、操作数据库、下载模型或新增验收结论。
