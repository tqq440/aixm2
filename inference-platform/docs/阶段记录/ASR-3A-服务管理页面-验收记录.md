# ASR-3A：服务管理页面验收记录

日期：2026-10-14；计划v2.7。状态：**开发侧交付，待用户验收；交付讲解后暂停。**用户本次“继续进入下一项”仅授权ASR-3A，不自动确认ASR-1、2A、2B验收，也不授权ASR-3B集中交付。

## 1. 范围与基线

- 继续在原React/TypeScript/Vite音频工作台增加服务管理区，复用0009、节点登记、Agent管理和原上传/任务/结果接口。没有后台代码、迁移、依赖、模型或音频限制变化，没有修改原架构网页。
- 北京时间10:23:45实查并登记基线：平台停止、5000/5173空闲，0009、33任务/33上传、4受管会话已确认退出、2旧观测会话、2模型/2版本、online0。数据库与上传对象成对备份到[本轮备份](../../runtime/verification/asr3a/backup/)，原数据详见[before.json](../../runtime/verification/asr3a/before.json)。此时间是核查登记时间，不补造首次动工时间。
- 沿用project311、Node24.15.0、Whisper small和本机CPU；[环境8项检查](../../runtime/verification/asr3a/environment.txt)通过，不下载或更新模型。

## 2. 已实现

- [AsrServicePanel](../../frontend/src/components/AsrServicePanel.tsx)从实际登记目录选择计算节点与ASR实例，显示准确模型版本、CPU、Agent可用性、当前任务和最近错误。登记成功不等于模型已运行。
- [管理客户端](../../frontend/src/api/management.ts)读取完整分页目录并校验实例身份；[管理Hook](../../frontend/src/hooks/useAsrManagement.ts)每轮完成后等待2秒轮询，旧响应不覆盖新选择。10秒未获得新的有效管理观察时清除可信状态，这是浏览器显示保护，不是Agent停止判据。
- 页面区分未启动、模型加载中、可以转写、正在执行、停止中、停止未确认、启动或运行失败与状态未确认。失联不显示停止成功，旧任务报告不当作当前实时任务。
- 启停仅向固定管理路径提交最新Agent会话与命令修订，202只表示受理意图；409或网络不明只自动GET核对，不重发POST。等待后台报告后才显示就绪或停止。
- 管理令牌用密码框输入，只保存在页面内存；切换工作台/节点/实例、刷新或离开页面时清除，没有写入localStorage、sessionStorage、URL或构建产物。真实浏览器验收关闭trace，未保留真实令牌。局部管理保护不代表13A完整鉴权。
- 上传区在模型未就绪、停止或状态未确认时保留已选文件并阻止新提交；就绪且忙碌仍可按原队列接单。所选模型必须与实例准确版本匹配。已有不明提交仍可用原文件/参数/版本/幂等键确认重放；历史结果、取消、Vision与LLM入口继续复用。

## 3. 实际验证

| 验证 | 实际结果与证据 |
|---|---|
| 前端全量单元与组件测试 | 最终221通过，16文件，5.43秒；[JUnit](../../runtime/verification/asr3a/frontend-tests-final.xml)、[日志](../../runtime/verification/asr3a/frontend-tests-final.txt)。含状态切换、旧响应隔离、观察过期、令牌清除、冲突/网络不重发、版本与不明提交兼容 |
| 类型检查与生产构建 | `tsc --noEmit`通过；Vite构建通过，[构建输出](../../runtime/verification/asr3a/build.txt) |
| 后台定向兼容 | 135通过，38.07秒；管理API、受理门禁、runtime、任务/视觉与health接口，[JUnit](../../runtime/verification/asr3a/backend-tests.xml)。本轮未重复运行ASR-2B的1214项全量 |
| 真实Edge管理页面 | 1项通过：默认停止、错误令牌401且修订不变、有效启动与真实模型就绪、中文上传成功、独立停止与退出确认、停止后TXT下载与刷新恢复、令牌清除、Vision模型保持就绪、LLM仍未配置。没有mock接口，[真实报告](../../runtime/verification/asr3a/browser-real.json) |
| 模拟Edge状态异常 | 另1项通过：模拟加载中、忙碌/当前任务、停止未确认、模型失败与断网展示。仅替换管理响应，不代表真实故障注入，[模拟报告](../../runtime/verification/asr3a/browser-simulated.json) |
| 浏览器汇总 | 共2项通过、18.2秒，[报告](../../runtime/verification/asr3a/playwright-results.json)；NO_COLOR/FORCE_COLOR提示仅为终端颜色设置，无测试失败 |
| 页面尺寸 | 1920×1080、1366×768、390×844均检查无横溢出，3张整页截图已实际查看：[1920](../../runtime/verification/asr3a/asr-management-1920.png)、[1366](../../runtime/verification/asr3a/asr-management-1366.png)、[390](../../runtime/verification/asr3a/asr-management-390.png)。是桌面Edge视口模拟，不宣称真实手机测试 |

真实任务为`9e97b901db744d1aabc6499aaae822e0`，公开中文音频5.5466875秒，CPU转写成功，结果为“欢迎大家来体验打磨院推出的语音识别模型”。TXT与接口全文逐值一致；未修正模型文字或据此推导准确率/容量。

首次前端全量有1项夹具失败：空目录测试重复消费同一个Response；修改为每次新建Response后最终全量通过。保留[首次报告](../../runtime/verification/asr3a/frontend-tests.txt)，不隐去失败过程，不把多轮通过数字累加。

收尾将页面内“使用说明”更新为新的ASR启动步骤，并移除重复CSS导入后，App已有5项回归通过（2.86秒），类型检查与生产构建再次通过；[收尾回归](../../runtime/verification/asr3a/help-regression.txt)单独记录，不累加为新的全量计数。这两项说明/导入整理之后未重新运行真实浏览器。

## 4. 数据保持与停止

[数据核对](../../runtime/verification/asr3a/preservation.json)于北京时间10:34:09通过：原33任务、业务档案及33上传哈希保持；节点仅正常刷新更新时间，组件报告与Agent控制按实际运行变化。当前34任务/34上传：ASR22成功、2取消，Vision10成功；35条执行记录。原4条受管会话保持，新增1条，共5条均退出确认；Agent已关闭确认。0009、2模型/2版本、1节点/1实例、2旧会话和online0保持。

237份后台、迁移、依赖、Windows脚本及前端package文件哈希不变；源码、构建和报告文本未检出真实管理令牌。平台经受管入口安全停止，[停止结果](../../runtime/verification/asr3a/platform-stop.txt)显示管理器未运行、API/页面不可达、5000/5173释放；锁已释放。停止状态只代表本次核查时刻。

## 5. 边界、下一步与运行方法

本轮没有再次启动ASR、运行中停止排空、真实Agent失联/模型加载故障或新的YOLO推理；这些不以模拟界面或旧阶段证据代替。本轮完成页面接入与真实兼容验证，**ASR-3B完整浏览器“停止→再次启动”、故障/历史回归及集中交付仍未开始**。真实课题组服务、GPU、多节点和完整访问保护继续等待相应阶段。

日常先在项目目录执行`powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-platform.ps1`，打开本机5173页面。在“ASR服务管理”选择节点和实例，从项目本地`.env`复制`INFERENCE_AGENT_TOKEN`值到密码框，点击“启动ASR”并等待“可以转写”；完成后点“停止ASR”等待实际确认。令牌不发聊天。仍可使用`asr-service.ps1 -Action start/status/stop`。完整说明见[ASR独立启停说明](../ASR独立启停说明.md)。

本地计划、进度、协作入口、实现对照与待汇总事件同步更新；本地v2.7，Multica仍为最后已同步v2.6。没有远端写入，原同步时间与BING任务状态不变，等待ASR-3B交付后集中同步。

开发侧交付登记时间为北京时间10:37:23，见[交付时间点](../../runtime/verification/asr3a/delivery.json)。本地链接、92个函数ID、测试报告及远端快照保持的收尾核对见[最终检查](../../runtime/verification/asr3a/final-checks.json)。

帮助理解：为什么“启动请求已受理”还不能立即上传？为什么断网不能直接显示“已停止”？为什么停止ASR之后仍然能下载以前的转写结果？
