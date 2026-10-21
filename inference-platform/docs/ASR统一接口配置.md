# ASR 统一接口配置

本轮先统一 ASR，图片检测与 LLM 保持现有入口。ASR 服务由用户手动配置，不根据本机、局域网或服务器地址划分配置方式。

## 使用流程

1. 打开音频转写页的 **ASR 服务配置**，填写服务标识、名称、协议、服务地址、转写路径、健康路径和平台模型标识。
2. 点击 **测试连接**。此操作只执行有界健康 GET，返回耗时和错误类别；不发送音频，不代表转写已经验证。
3. 点击 **保存配置**，再 **选用此修订**。可保存多份，一次选用一份；编辑草稿不改变已选修订。
4. 正常停止并重新启动平台。页面分别显示“当前运行”和“下次启动”，待重启时暂停接收新的接口 ASR 任务。
5. 点击 **用当前服务测试音频**，选择短音频正常提交。在任务详情查看状态、文字、分段和下载。这一步经过正常队列、调用许可与结果保存，验证实际转写链路。

服务应由你或服务方事先运行，保存配置不会启停模型。有其他连接配置的未完成任务或结果未知调用时，不允许切换，须先按原配置处理。平台模型标识不能占用已有 Whisper、Vision 或 LLM 模型的编号。

## 支持的协议

| 配置项 | SenseVoice Remote-V2 | OpenAI 兼容音频接口 |
|---|---|---|
| 服务地址 | HTTP(S) 根地址 | HTTP(S) 根地址 |
| 转写路径默认值 | `/api/v1/asr/transcripts` | `/v1/audio/transcriptions` |
| 健康路径默认值 | `/api/v1/asr/health` | `/v1/models` |
| 音频字段 | `audio` | `file` |
| 模型字段 | 固定 `engine=sensevoice` | 用户填写 `model` 参数 |
| 健康成功依据 | `success=true`、`status=ok`、`model_loaded=true` | 模型列表包含填写的上游模型名 |
| 转写结果 | 校验 `details.segments` | 校验 `text`，`segments` 可选 |

路径可调整，服务须符合所选协议的请求与返回格式。健康测试不推断 CPU/GPU、设备空闲或共享容量。OpenAI 兼容接口返回纯文本时，分段保持为空，不编造时间戳。

上游模型版本可留空；本地接入修订仅表示连接配置身份，不宣称权重固定。原 Remote-V2 默认配置的历史指纹保持兼容。

## 使用已有本机 Whisper

在项目目录、已有 `project311` 环境手动执行：

```powershell
python -m backend.modules.asr.http_server --port 8001
```

只监听 `127.0.0.1`，加载已有 Whisper small，不下载模型，也不由平台自动拉起。

| 字段 | 值 |
|---|---|
| 服务标识/平台模型标识 | `whisper-http` |
| 服务名称 | `Whisper 接口` |
| 协议 | OpenAI 兼容音频接口 |
| 服务地址 | `http://127.0.0.1:8001` |
| 转写路径 | `/v1/audio/transcriptions` |
| 健康路径 | `/v1/models` |
| 接口 model 参数 | `whisper-small` |

原 Node Agent 能力保留在“已有实例管理与兼容入口”折叠区。不要同时启动独立 HTTP Whisper 与原实例处理同一模型；平台不会自动推断额外实例的资源预算。

## 认证、证书与保存

- 可选 API Key：页面填写环境变量名称，例如 `ASR_API_KEY`；你在后端运行环境设置实际值，调用时发送 Bearer。页面、配置文件、任务及公开错误不保存密钥值。
- HTTPS 默认校验证书，可填写本机 CA。TLS 例外只作用于准确 HTTPS Origin，不能同时填写 CA；请求不跟重定向。
- 配置位于数据库旁 `<数据库文件名>.asr-services.json`，使用文件锁、原子替换、大小上限与不可变修订。日常在被 Git 忽略的 `runtime/`，不新增业务数据库表。
- 选用后重启自动登记准确接口模型，旧版本/任务/结果不覆盖，原目标和共享组许可不扩大。
- “下次启动恢复原环境配置”恢复 `.env` 原连接，不改写 `.env`。

## 配置 API

本机 API 接受 camelCase JSON；GET 不探测网络。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/asr/services` | 已保存、已选择与当前运行配置 |
| PUT | `/api/asr/services/<serviceId>` | `{config, expectedRevision}` 保存草稿 |
| DELETE | `/api/asr/services/<serviceId>` | `{expectedRevision}` 删除未使用配置 |
| POST | `/api/asr/services/selection` | `{serviceId, expectedRevision}` 选择下次启动修订 |
| POST | `/api/asr/services/test` | `{config}` 手动健康测试 |

音频测试复用 `POST /api/tasks/asr` 和结果接口，遵守 20 MiB/300 秒、单次调用及未知结果不重发。浏览器超时或断线不能证明服务端已停止。
