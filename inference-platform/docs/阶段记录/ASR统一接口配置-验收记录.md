# ASR 统一接口配置：本轮实施记录

范围登记时间：2026-10-20T15:53:33+08:00。记录时已进行代码实现，不将该时间冒记为最初开始时间。

用户要求服务由其手工按接口配置、不区分本机/外部，并有测试功能；进一步明确本轮先完成 ASR。

## 实施范围

- 统一页面，保存多份、一次选用一份，明确当前运行与下次启动修订。
- HTTP(S)、Remote-V2、OpenAI Audio；手动健康测试及正常任务链路的音频测试。
- 数据库文件名隔离配置文件，不可变修订、原子保存、编辑冲突检测和进程快照。
- 旧未完成/未知调用阻止换接口，待重启暂停新受理；默认 Remote-V2 指纹兼容。
- Whisper 手动独立 HTTP 入口；旧管理与提交保留在兼容区。
- 不下载依赖/模型，不改日常 `.env`，不迁移日常数据库，不启停实验室模型，不访问或写入 Multica。

## 验证记录

开发侧交付记录：`2026-10-20T16:13:19+08:00`，待用户验收。以下批次分别记录，不把重叠测试相加：

| 验证 | 实际结果 |
|---|---|
| 配置API、文件修订、切换/回退、旧任务保护，原版本/执行/容量及启动/结构 | 135通过，84.29秒；`runtime/verification/unified-asr-config/backend-regression.xml` |
| HTTP协议、鉴权、健康错误分类、原settings/adapter/版本兼容 | 192通过，22.75秒 |
| Whisper HTTP、新接口限制/清理、原AsrService及结构/导入 | 40通过，10.89秒，其中新增HTTP测试22项 |
| 最后补充进程修订固定与接口模式不要求本机权重后的启动定向 | 50通过，2.33秒；`startup-final.xml` |
| 实际回环HTTP + Windows调用子进程 + 正常任务/许可/历史查询 | 1通过，9.51秒 |
| 前端全量 | 28文件326项通过，19.24秒 |
| 前端后续运行状态/布局修正 | 26项、最终12项定向分别通过；最终TypeScript与Vite构建通过 |
| 隔离Edge新配置/旧接口/原调度 | 7项通过，33.1秒；最终待重启文案后新配置1项复验通过，9.2秒 |

实际回环测试只发送一次POST，明确使用固定响应替身；许可先held后released，两调用子进程40044/2200均退出，锁释放，API重建后历史GET/幂等确认没有重发。证据为 `runtime/verification/unified-asr-config/openai-http-child-506efbb5cf9d4af8a5d567c05cd68b36-evidence.json`。它不证明真实模型或浏览器下载事件，`browserDownloadEventVerified=false`。

浏览器全部API为隔离mock，最终1366/390截图已视检，路径 `runtime/verification/asr-service-config/frontend/final-ui/asr-configuration-1366.png` 和 `asr-configuration-390.png`。独立Vite已退出、5183空闲。未重跑完整后台全量或真实模型推理。

16:11:40日常只读保护核对：仍26表、42任务、42上传，上传逐文件字节哈希和 `.env` 与11:55历史checkpoint一致；没有创建日常手工服务配置文件。数据库完整性ok、无外键异常。历史checkpoint的`asr_target_worker_sessions`为6、当前7，因此不声称全表或会话数量不变；本轮没有日常数据库写入验证。当前已有5000/5173监听（PID38540/32720），保留现有平台，未停止或接管；新后端需正常重启后使用。证据 `runtime/verification/unified-asr-config/daily-protection.json`。

已发现并处理：

- 普通终端初始化 `setup refresh had errors`，改用获准的工作区命令；没有自动审批拒绝。
- 系统 pytest TEMP 权限异常，改用工作区独立 `--basetemp`。主线程首次父目录未创建导致 19 项 setup error，创建后继续。
- 首轮配置测试 18 通过、1 项测试断言误把 `apiKeyEnv` 字段名当作秘密；修正为检查实际密钥字段。
- 两次回归命令路径写错，均 `no tests ran`，不记为通过。
- 独立审查发现回退默认版本、模型身份冲突及复用其他能力模型编号问题，已加选择前校验和回退恢复，纳入135项通过结果。
- `git diff --check`默认把既有CRLF文件的新行识别为尾随空白；按Windows换行规则执行`git -c core.whitespace=cr-at-eol diff --check`通过，没有为此改写整份历史文档换行。

本轮开发验证与用户验收分开，不改变本机-3A进行中、3B待真实设备/容量条件和历史验收状态。
