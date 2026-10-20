# ASR-1：插件边界验收记录

- 登记日期：2026-10-13。
- 当前状态：准备工作与ASR-1已完成开发侧交付及真实模型/浏览器验证，待用户验收并暂停，不进入ASR-2。
- 授权：用户正式要求实施已确认的ASR优先计划，本轮仅准备工作与ASR-1；交付、验证和讲解后暂停，不自动进入ASR-2。
- 计划依据：[开发计划v2.6](../开发计划.md)；外部资料见[ASR服务对接清单](../ASR服务对接清单.md)。

## 范围与中断事项

1. 13A暂停、未验收：后端鉴权没有实现，3处未完成前端鉴权接入已逐项核对并撤销；撤销后前端基线135项与构建通过，不将其写成访问保护已完成。
2. 当前Whisper整理为首个可替换ASR适配器；复用`adapter_key`和受控实现目录，不接收任意模块或命令。
3. 分开输入、调用、结果转换与平台任务规则，兼容旧任务/结果和现有上传、幂等、取消链路；通用结果与Whisper专属元信息分别表达。
4. 本轮使用现有本机Whisper和公开中文音频，不下载模型、不接对方服务；不进入Node Agent、页面启停、多节点或部署。YOLO历史能力保留，LLM仍未配置。

## 核对与验收结果

| 项目 | 本轮状态 | 证据 |
|---|---|---|
| 中断前端鉴权逐项撤销、文档状态和悬空链接修正 | 已完成 | 3处未完成接入撤销，前端基线135项/构建通过；移除未创建13A记录的悬空链接，不编造验收证据 |
| 当前运行状态、环境、数据库与上传对象基线/备份核对 | 实施前核对通过 | [before-development.json](../../runtime/verification/asr1/before-development.json)：20任务、20上传、2模型/2版本，0007，平台停止；最终保留另验 |
| 受控ASR适配器边界、通用结果和旧结果兼容 | 本轮后台回归与隔离真实模型验证通过 | 见下方实际契约及真实证据 |
| 后台相应回归与适配器拒绝、幂等和取消兼容 | 全量981通过、2旧权限跳过 | 150.52秒，1条pytest缓存写权限警告；模型替身与真实模型分别记录 |
| 前端测试、类型检查与构建 | 本轮通过 | 撤销后的基线135项、本轮最终140项分别通过，不累加；两次类型检查/构建均通过 |
| 当前Whisper与现有公开中文音频的真实转写 | 隔离真实Worker验证通过 | 运行取消、确认旧child退出、重新就绪与新任务成功；见[真实报告](../../runtime/verification/asr1/real-worker-latest.json) |
| 旧数据/结果保持及本轮服务启停/最终状态 | 本轮通过 | 18份旧成功output一致，旧20任务/20上传保留，实际五服务就绪后停止；详见下方证据 |

## 已落盘的实际契约

实施前重新检查管理器未运行、API/页面不可达与5000/5173端口可用，并按实际数据库和上传对象成对备份到`runtime/verification/asr1/before-development-e61c855289214ac6bf7a0ed15e0f2d94/`。该基线记录20任务/20上传、2模型/2版本与47个Python分发版本；不能把它当成本轮真实验收结束的最终状态。没有修改迁移、依赖清单或配置。

- [adapter_catalog.py](../../backend/service/asr/adapter_catalog.py)定义`AsrAdapter`调用契约与受控插件目录，当前只有`faster-whisper`工厂。使用完整ModelSpec与`adapter_key`匹配，不凭登记或同名adapter放行其他模型、资源配置或任意代码。
- [asr_schema.py](../../backend/schema/asr/asr_schema.py)保留通用`text/segments/audioDurationSeconds`，语言信息可缺失；可选`engineMetadata`保存adapter标识、实际模型/设备/计算类型/线程数/耗时和运行库版本。没有这些事实的引擎不伪造字段。
- 当前[AsrService](../../backend/service/asr/asr_service.py)仍调用已有本地Whisper；新结果同时提供引擎元信息与旧顶层字段，对应值必须一致。旧JSON缺少`engineMetadata`时序列化不新增此字段，不迁移或覆盖历史结果。HTTP接口路径、任务状态机与现有取消职责不变，详见[接口说明](../接口说明.md)。
- 本地转写入口与Worker推理子进程通过受控目录构造适配器。未开发Node Agent、独立ASR启停页面或对方模型适配器。

## 当前已取得的前端证据

在`frontend/`执行`npm.cmd test -- --reporter=default --reporter=json --outputFile=...`及`npm.cmd run build`。两轮报告分别保存，不能把基线135与最终140相加：

| 轮次 | 实际结果 | 证据 |
|---|---|---|
| 撤销中断接入后的基线 | 135通过，0失败/跳过；TypeScript与Vite构建通过 | [测试JSON](../../runtime/verification/asr1/frontend-baseline-tests.json)、[测试日志](../../runtime/verification/asr1/frontend-baseline-tests.txt)、[构建日志](../../runtime/verification/asr1/frontend-baseline-build.txt) |
| ASR结果展示兼容后的最终前端 | 140通过，0失败/跳过；TypeScript与Vite构建通过 | [测试JSON](../../runtime/verification/asr1/frontend-tests.json)、[测试日志](../../runtime/verification/asr1/frontend-tests.txt)、[构建日志](../../runtime/verification/asr1/frontend-build.txt) |

这些是前端单元/组件测试与构建，不计为真实浏览器测试。后台、真实Whisper、浏览器和最终停止证据分别列在下方。

## 后台回归与真实模型

后台最终完整pytest回归为**981通过、2跳过，150.52秒**。两项跳过仍为Windows账户不能创建符号链接，不把跳过说成通过；另有1条`.pytest_cache`写入被拒警告，不影响用例结论。此前定向351通过（46.06秒）不与981相加。证据：[后台日志](../../runtime/verification/asr1/backend-tests.txt)、[JUnit报告](../../runtime/verification/asr1/backend-tests.xml)。本轮没有新增依赖、修改迁移或放宽权限。

真实模型使用[verify_worker.py](../../runtime/verification/asr1/verify_worker.py)及现有`runtime/samples/asr_example_zh.wav`，在独立SQLite与上传目录运行，没有访问默认业务数据库，也没有使用模型替身。实际结果见[real-worker-latest.json](../../runtime/verification/asr1/real-worker-latest.json)及[执行日志](../../runtime/verification/asr1/real-worker-run.txt)：

- 初始真实模型child `38076`就绪；运行中任务`9e9aa7401b7045d3a0c75d31a11a7693`取消后，确认旧child退出，结果接口返回409，新child `39572`重新就绪。
- 后续任务`1be40643f99042b8a93cf193765de233`真实成功、attempt=1；同幂等键重放不重复创建，成功终态后取消不会破坏结果。
- 5.5466875秒中文音频返回“欢迎大家来体验打磨院推出的语音识别模型”。保留模型实际误识别，不人工替换为预期文本；这一样例不代表模型准确率。
- 固定Whisper small版本`536b0662742c02347bc0e980a01041f333bce120`，CPU实际`computeType=int8_float32`，转写约1.715秒。`engineMetadata`与保留的旧字段一致；这不是平台吞吐或GPU性能结论。
- 最终两个真实child均确认退出，Worker锁释放；真实取消沿用现有受管进程逻辑，不等于已实现Node Agent或独立服务管理。

## 浏览器、旧结果与数据保持

浏览器首轮**8通过、1跳过**：只读LLM状态项因未设置`E2E_LLM_STATUS`未执行，见[首轮日志](../../runtime/verification/asr1/browser-tests.txt)和[Playwright报告](../../runtime/verification/asr1/playwright-results.json)。随后仅补跑该只读项，**1通过**，见[补跑日志](../../runtime/verification/asr1/browser-llm-readonly.txt)及[补跑报告](../../runtime/verification/asr1/llm-readonly/playwright-results.json)。合计9个场景实际执行通过：6项真实接口/既有模型或未配置状态，3项明确模拟；不能把9项都写成真实模型测试。

- [真实浏览器报告](../../runtime/verification/asr1/real-browser-report.json)：真实ASR任务`86616800b7e64b1d89a4729e1cbcd99c`成功、attempt=1；全文复制、TXT/JSON下载、刷新恢复和1920×1080、1366×768、390×844视口通过。没有网络模拟；真实输出继续保留原误识别。Vision仅回归既有图片能力，没有新增检测模型。
- [实时结果核对](../../runtime/verification/asr1/live-results-check.json)：18份既有成功任务的`output`经HTTP逐值一致；新任务`engineMetadata`与旧字段及下载内容一致。
- [元信息浏览器报告](../../runtime/verification/asr1/metadata-browser-report.json)：新ASR结果与旧无`engineMetadata`结果分别在1366px和390px宽度下只读检查，共4种组合，无新增任务、网络模拟或页面错误，无横向溢出；截图已实际查看，没有关键信息裁切。
- [最终数据保持](../../runtime/verification/asr1/final-preservation.json)：原20任务、20上传哈希及关联档案全部保留，55个保护文件与47个Python分发版本未变，迁移仍为`20261013_0007`。默认库新增ASR1条/图片2条成功任务，最终23任务：ASR13成功/2取消、Vision8成功，在线记录0。隔离Worker的取消任务不计入默认库。

## 实际启停与收尾

[platform-stop.json](../../runtime/verification/asr1/platform-stop.json)记录受管实例`60c12f590af648739185488c537757c8`实际启动，API、投递器、ASR Worker、Vision Worker和页面5服务均就绪，两个真实模型child就绪。验收后通过原受管入口停止，确认所属服务退出、5000/5173释放、API/页面不可达；这是本轮实际停止结果，未来使用前仍须查询实时状态。

[环境检查日志](../../runtime/verification/asr1/environment-check.txt)最终8项通过，没有下载模型或安装依赖。[收尾检查](../../runtime/verification/asr1/final-checks.json)验证三级布局、绝对导入、92个唯一函数ID、本地文档链接、`pip check`无冲突和平台停止；第一次受限环境读取第九部分历史证据遇权限错误，随后在正常Windows权限下只读核对通过，没有修改ACL。文档最终编辑后再核对链接，不把链接数量写成固定能力指标。

日常仍按[README](../../README.md)运行已有`start-platform.ps1`、`status-platform.ps1`与`stop-platform.ps1`，打开音频工作台使用现有样例；本阶段没有新增启停命令、凭据或数据库迁移。平台脚本仍管理整套服务，单独ASR启停属于尚未开始的ASR-2。

## 交付边界与理解问题

准备与ASR-1已完成开发侧与真实验证，用户验收待确认。ASR-2—4尚未开始；原13A暂停、未验收，13—16后续目标暂缓。没有接入对方ASR、Node Agent、多节点、GPU或真实LLM；通用结果和可替换边界不表示任何未知模型可以直接运行。实际耗时只引用执行报告，没有估算总工时。

1. 将来替换ASR引擎时，哪些是适配器职责，哪些队列、租约与取消规则应该继续复用？
2. 如果对方没有返回语言置信度或分段时间戳，为什么不能补一个看似完整的数值？
3. 对方只提供已运行的调用接口，为什么还不能在平台上直接承诺“启动/停止服务”？
