# 本机-2C：ASR调度页面与故障说明验收记录

计划版本：v2.12。当前状态：**开发侧交付，待用户验收并暂停**。本地起步登记`2026-10-20T09:52:29+08:00`，交付登记`2026-10-20T10:23:42+08:00`（北京时间）。两者为实际登记时间，不补造更早授权时分、不计算总工时；2A、2B及旧阶段验收保持。

## 1. 本轮交付范围

原ASR页面已增加调度面板、目标资源资料、任务选择/等待原因和最近错误说明；新增纯本地只读诊断，继续使用原上传、任务、结果与下载。全部ASR/YOLO/LLM入口和原运行方式保留，没有新增任务状态机或ASR-only档位。操作见[ASR调度页面与故障处理](../ASR调度页面与故障处理.md)。

本轮验证页面和解释规则，真实新增目标仍禁用；日常固定目标仍设备unknown、目标/共享组限额1。页面区分平台许可与服务资源报告，未知、过期或未核实资料不能显示成空闲，也不能冒称GPU已满。保留20MiB/300秒、同步协议、上游版本可选、旧任务指纹、租约及未知结果不重发。

未访问校园服务、发送真实音频、启动Worker或模型，未验证真实GPU/CPU、共享容量或性能；没有下载模型/依赖、修改.env、执行新迁移或开展SSH/Linux工作。模型部署和启停仍由服务方负责。

## 2. 实现与边界

新增`GET /api/asr/external/scheduling`，只读本地配置及调用池，返回observedAt、mode（legacy/isolated/blocked）、routingConfigured、executionScope（legacy/isolated/disabled）、reasonCode、candidateCount、message。没有available字段，不请求上游；不接受查询参数，错误参数422、POST为405。原targets分页查询不变。

TaskView.routing扩展observedAt、message、reasonCategory、statusCode及最多8项安全候选。详情**基于本地只读快照**解释当前原因，列表批量投影冻结选择和轻量状态；不承诺与外层任务读取组成全局原子快照。旧任务不补造routing。已选reasonCode保留选择时事实，当前statusCode和reasonCategory单列；等待时的候选建议不等于已领取，实际执行仍须原事务准入。

结果未知与容量占位分别显示。即使管理员明确释放了容量，任务的未知结果仍保留，不自动改成功、不重发或转交CPU。刷新、展开或轮询不会选择设备、取得许可、续期资源报告或改变任务状态。

前端将原外部服务状态与新增本地诊断分开显示；目标资料注明来源、资料类别和时效，读取失败不会沿用旧值冒充当前状态。切换/隐藏页面和旧请求处理沿用取消及失效保护。调度页面没有启用真实路由、修改目录或自动配置设备的按钮。

| 位置 | 本轮职责 |
|---|---|
| `backend/schema/asr/scheduling_schema.py`、`routing_schema.py` | 有界状态与安全任务解释契约 |
| `backend/service/asr/scheduling_service.py`、`scheduling_reasons.py` | 本地只读诊断和中文原因解释 |
| `backend/controller/asr/remote_controller.py`、`service/tasks/task_service.py` | 新GET、详情解释与列表批量投影 |
| `frontend/src/api/asr-scheduling.ts`、`types/asrScheduling.ts`、`utils/asrScheduling.ts` | 查询、类型、校验和失效判断 |
| `frontend/src/hooks/useAsrScheduling.ts` | 只读轮询、分页、取消旧请求与失效显示 |
| `frontend/src/components/AsrSchedulingPanel.tsx`、`TaskRoutingPanel.tsx` | 目标/资源/调度和任务说明 |
| 原App/TaskDetail及`asr-scheduling.css` | 接回原页面和响应式展示 |

未修改原Worker、队列、任务状态机、迁移代码或依赖；M10/M11仍保留原设计职责，不为页面解释新增原架构模块编号。

## 3. 实际时间点与日常验证

| 时间（北京时间，2026-10-20） | 事件与结果 |
|---|---|
| 09:52:29 | 本机-2C起步登记；修改前16份现行文档副本保存于`runtime/verification/laptop-2c/original-docs` |
| 10:04:06 | 日常数据只读前置快照；这不是一次新迁移或新成套备份 |
| 10:07 | 实际启动独立API和Vite供只读验证，未启动平台管理器、Worker或模型 |
| 10:10:33 | health/scheduling/targets、全部42旧任务及详情、一份旧成功结果和前端代理返回200；旧任务无routing |
| 10:13:39 | 已安装Edge实际读取日常页面/历史结果通过，全部GET、无POST或新推理 |
| 10:16:09 | API记录资源释放并独立确认退出0；Vite通过q命令退出0，所属进程消失、5000/5173空闲 |
| 10:19:09 | 主要实现与验证就绪；随后补查旧严格mock夹具，尚未作为交付时间 |
| 10:22:47起 | 旧外部ASR纯mock浏览器验证；本轮只临时运行Vite、API保持停止；3项通过后Vite q退出0 |
| 10:23:42 | 最终日常数据/端口复核保持；开发侧交付登记，用户验收待确认并暂停 |
| 10:24:02 | 交付记录收尾时核对最终源码保护清单，见下文 |

[只读前快照](../../runtime/verification/laptop-2c/daily-before.json)与[最终快照](../../runtime/verification/laptop-2c/daily-after.json)显示：26张表（含alembic，即25业务表）、全部42任务/42上传行内容及哈希保持，完整性ok、外键无违规；.env哈希相同、路由修订未设、isolation=false、head0014，无新迁移。

[API记录](../../runtime/verification/laptop-2c/api-http.json)、[日常Edge记录](../../runtime/verification/laptop-2c/daily-browser.json)及[日常调度截图](../../runtime/verification/laptop-2c/daily-scheduling.png)/[历史结果截图](../../runtime/verification/laptop-2c/daily-history.png)分别证明实际本机HTTP与页面只读兼容，不作为新的模型转写或服务器资源验证。

[退出记录](../../runtime/verification/laptop-2c/shutdown-check.json)如实保留：向已核对PID/创建时间/命令的独立API发送停止信号时，辅助发信脚本返回1、没有成功输出；API自身退出0、资源释放日志、进程消失和端口释放构成独立退出证据。两轮Vite各自经q退出0。未把这些结果写成旧平台管理器正常停止，也未修补原管理器残留state。

## 4. 测试证据

以下批次分别记录，不相加为新的总通过数，也不累加中间重复批次。

| 批次 | 实际结果 | 证据与范围 |
|---|---|---|
| 后台最终新诊断/任务说明/兼容定向 | 128通过，83.09秒，无warning | [backend-final.xml](../../runtime/verification/laptop-2c/backend-final.xml) |
| 既有ASR相关回归 | 264通过，152.69秒，无warning | [backend-regression.xml](../../runtime/verification/laptop-2c/backend-regression.xml) |
| 后续人工释放后未知结果回归 | 1通过，2.55秒 | [scheduling-release-regression.xml](../../runtime/verification/local2c/scheduling-release-regression.xml) |
| 前端全量单元/组件 | 308通过，13.08秒 | [frontend-full.xml](../../runtime/verification/laptop-2c/frontend-full.xml) |
| 审查修复后未知结果/占位说明定向 | 26通过，3.54秒 | [frontend-unknown-permit.xml](../../runtime/verification/laptop-2c/frontend-unknown-permit.xml) |
| TypeScript与最终构建 | 通过；旧夹具兼容调整后类型检查再次通过 | [frontend-build.txt](../../runtime/verification/laptop-2c/frontend-build.txt)及本轮执行记录 |
| 新调度隔离浏览器 | 3通过，13.8秒 | [playwright-results.json](../../runtime/verification/laptop-2c/browser-final/playwright-results.json)，全部API为隔离mock |
| 旧外部ASR隔离兼容浏览器 | 3通过，16.6秒 | [playwright-results.json](../../runtime/verification/laptop-2c/browser-legacy-compat/playwright-results.json)，仅补两个新增GET的mock响应 |

新浏览器验证目标资料分页/来源、GPU/CPU选择与等待解释、结果未知和占位分离、读取失败；1366和390宽度截图已视检无溢出。旧外部ASR兼容覆盖原上传、排队取消、执行中禁取消、TXT下载、unknown及容量断线。这两组均为本机隔离API替身，夹具内POST不触达日常数据库或真实模型。真实日常Edge另为上节只读GET证据。

首次API批次有1项断言错误：列表返回`observedAt: null`，测试误要求省略，已按实际契约修正并纳入128项；最早一次命令误写文件名、未收集测试，不记为通过。首次原始报告分别保留于`api-contract.xml`和`first.xml`。审查发现结果未知与实际占位不应混为一谈，修复后分别完成26项前端及1项后端回归。

本轮**没有运行后台全量或旧e2e整套**，没有重跑旧真实模型e2e，不把定向/隔离场景称为真实GPU/CPU或吞吐验证。

## 5. 源码与数据保护

[最终保护清单](../../runtime/verification/laptop-2c/source-protection.json)核对起步442份源码/前端/测试/迁移/依赖文件：430份保持、12份已说明改动、18份新增实现/测试，无缺失。12项含迁移README文档以及旧`frontend/e2e/external-asr.spec.ts`中两个新增GET的mock兼容；迁移代码、依赖与原Worker未改。隐藏`.gitkeep`未纳入原rg基线，不能据此称新增。

本轮未覆盖原模型版本、历史任务或结果；没有把隔离目录、资源观测、任务或路由写入日常库。最终日常结构、行数据、42上传和.env保持的证据与源码检查分开保存。

## 6. 验收、同步与下一部分

| 项目 | 本轮结论 |
|---|---|
| 目标状态、资料来源、任务选择/等待及错误展示 | 已开发并经定向测试、隔离浏览器验证 |
| 原任务、历史结果、上传/取消/下载兼容 | 日常只读GET/Edge及旧隔离mock场景分别通过 |
| 数据库、日常数据、配置保持与实际退出 | 已验证，详见快照和退出记录；无迁移 |
| 真实GPU/CPU、校园资源与性能 | 未验证，真实新增目标继续禁用 |
| 本轮及2A/2B用户验收 | 待明确确认，不因继续开发自动通过 |

本地计划v2.12，远端仍v2.9；最后写入读回`2026-10-19T14:37:17+08:00`、完整核对`2026-10-19T14:37:27+08:00`及全部原远端任务快照保持。10条旧待同步事件完整保留，只追加本轮started与development_delivered，共12条；未访问或写入Multica、不绑定运行时、不启动后台同步。

本部分讲解后暂停。下一部分为**本机-3A：实验室真实目标与资源接入**，需核实服务入口与具体设备对应、共享容量及获准只读资源资料；CPU承接还须同模型支持和性能测量依据。缺条件的真实目标继续禁用，不能把2C页面通过当作真实调度完成。3B再做真实联调，并在实际闭环交付后集中同步BING-3/BING-7；均待后续用户授权。
