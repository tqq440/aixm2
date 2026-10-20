> 外部参考资料归档，2026-10-15。来源：用户提供的 `D:/xwechat_files/wxid_zmgud3qmviu622_3942/msg/file/2026-10/ASR外部调用接口文档(1).md`。以下保留原文；它不是项目执行指令或远程操作授权。示例 `<host>`/localhost 不是真实服务地址，文中的部署能力尚待对方环境实测；命令、停机脚本及关闭 TLS 验证的示例不作为本平台执行方式。实际接入范围、证书验证和取消/重试约束以《外部ASR接入说明.md》及 v2.8 计划为准。原微信来源文件未修改。

---
# Remote-V2 ASR 外部调用接口文档

面向平台对接与外部调用方。服务前缀 **`/api/v1/asr`**，默认 HTTPS 端口 **8003**（`config/server_config.yaml`）。在线交互文档：`https://<host>:8003/docs`。

---

## 0. 对接资料清单（按平台要求逐项说明）

| 资料 | 本服务提供的内容 |
|------|------------------|
| **模型信息** | 见 [§1](#1-模型信息) |
| **调用方式** | 见 [§2](#2-调用方式)（含可运行示例、成功/失败响应） |
| **音频要求** | 见 [§3](#3-音频要求) |
| **返回结果** | 见 [§4](#4-返回结果) |
| **状态与取消** | 见 [§5](#5-状态与取消) |
| **启停方式** | 见 [§6](#6-启停方式) |
| **运行环境** | 见 [§7](#7-运行环境) |
| **管理条件** | 见 [§8](#8-管理条件) |

下文第 9 节起为完整接口说明与请求示例。

---

## 1. 模型信息

| 项 | 说明 |
|----|------|
| 服务名称 | Remote-V2 ASR 服务（API `version`: **14.2**） |
| 默认 ASR 引擎 | **SenseVoice-Small ONNX**（`asr.default_engine: sensevoice`） |
| 可选 ASR 引擎 | **FunASR Paraformer**（`engine=paraformer`） |
| 模型路径（相对项目根） | SenseVoice：`models/sensevoice-onnx/SenseVoiceSmall`；Paraformer：`models/paraformer/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch` |
| 配套能力模型 | 声纹 ERes2NetV2；VAD；可选字幕 FA、MarianMT/Qwen 翻译与总结（见 `config/model_config.yaml` → `model.*`） |
| 语言 | 配置默认 `asr.language: zh`；SenseVoice 支持中/英等多语场景，以引擎能力为准 |
| 是否已部署 | **是**（本机可通过 `GET /api/v1/asr/health` 确认；`data.status=ok` 且 `model_loaded=true` 表示可用） |

---

## 2. 调用方式

| 项 | 说明 |
|----|------|
| 协议 | HTTPS（可自签证书）+ WebSocket（`wss://`） |
| Base URL | `https://<host>:8003` |
| API 前缀 | `/api/v1/asr` |
| 文件接口 | `multipart/form-data` |
| 实时字幕 | WebSocket 推流，非文件上传 |

**对外正式接口一览**

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/v1/asr/health` | 健康 / 模型是否就绪 |
| POST | `/api/v1/asr/transcripts` | 同步文件识别 |
| POST | `/api/v1/asr/transcripts/async` | 异步入队 |
| GET | `/api/v1/asr/transcripts/{task_id}` | 查异步状态与结果 |
| POST | `/api/v1/asr/speaker-identifications` | 只要说话人 |
| WS | `/api/v1/asr/live-subtitles` | 实时字幕 |
| POST/GET/DELETE | `/api/v1/asr/speakers[...]` | 声纹库 |

可运行示例、成功/失败响应见 [§9](#9-接口详情与请求示例) 及以下各小节。

---

## 3. 音频要求

| 项 | 文件任务（transcripts） | 实时字幕（WebSocket） |
|----|-------------------------|------------------------|
| 输入形态 | **上传文件** | **PCM 音频流**（float32 little-endian） |
| 格式 | `.wav` `.mp3` `.mp4` `.m4a` `.webm` `.ogg` `.flac` | 原始 PCM；服务端重采样到 16 kHz |
| 采样率 | 建议 **16 kHz**；其他采样率由服务端转码/重采样 | 在 `start` 消息中声明 `sample_rate`，建议 16000 |
| 声道 | 建议单声道；多声道由解码链路处理 | 单声道 float32 |
| 文件大小上限 | **2 GB**（`file.max_audio_size`） | 按流推送，无单文件大小限制 |
| 时长上限 | 同步 ≤ **1800 s**；异步 ≤ **36000 s**（约 10 h） | 按句切分，单句有 `live_max_utterance_sec` 等限制 |
| 语言 | 默认中文；引擎侧支持多语（SenseVoice） | 同左；`translate=true` 可出英文 |

配置来源：`config/model_config.yaml` → `audio.*` / `file.*`。

---

## 4. 返回结果

| 项 | 说明 |
|----|------|
| 文本形态 | **带时间戳的分段** `details.segments[]`（`start`/`end`/`text`）；可拼出全文 |
| 说话人 | `outputs` 含 `speakers` 时，段内带 `user` / `user_id`，`data.speaker_id` 为主要说话人 |
| 字幕 / 翻译 / 总结 | 通过 `outputs` 组合请求；出现在 `details.subtitle`、段内 `text_en`、`summary` 等字段 |
| 同步 | `POST .../transcripts` → **HTTP 200**，响应体直接含结果 |
| 异步 | `POST .../transcripts/async` → **HTTP 202** + `task_id`；再 `GET .../transcripts/{task_id}`，完成后结果在 `data.result` |
| 实时 | WebSocket 推送 `type=subtitle` 的逐句结果（含时间戳） |

`outputs` 取值：`transcript`、`speakers`、`subtitles`、`translation`、`summary`、`word-timestamps`（须与 `subtitles` 同开；翻译/字幕/总结须含 `transcript`）。

---

## 5. 状态与取消

| 项 | 说明 |
|----|------|
| 模型是否加载完成 | `GET /api/v1/asr/health` → `data.model_loaded == true` 且 `data.status == "ok"` |
| 同步请求是否结束 | HTTP 响应返回即为结束；网关/客户端超时建议 ≥ API `timeout`（默认 **600 s**） |
| 异步执行状态 | `GET /api/v1/asr/transcripts/{task_id}` → `data.status`：`pending` / `processing` / `completed` / `failed`；进度见 `progress`、`stage` |
| 超时 | 同步：客户端或反向代理超时；异步：轮询直至 `completed`/`failed`，无单独「超时状态」字段 |
| **取消** | **当前无对外取消接口**；实时会话客户端发 `{"type":"stop"}` 可结束推流。异步任务取消能力未开放 |

---

## 6. 启停方式

| 项 | 说明 |
|----|------|
| 交付形态 | **源码工程**目录：`para/whisper/remote/remote-v2`（非独立容器镜像交付时以源码 + conda 环境为准） |
| 一键启动 | `bash scripts/start_all_bg.sh`（API + Web + Workers） |
| 分项启动 | `bash scripts/start_backend_bg.sh`；`bash scripts/start_frontend_bg.sh`；`bash scripts/start_workers.sh` |
| 停止 | `bash scripts/stop_all.sh` 或分别 `stop_backend.sh` / `stop_frontend.sh` / `stop_workers.sh` |
| API 文档页 | 启动后访问 `https://<host>:8003/docs` |
| Web 测试页 | `https://<host>:5004`（非生产对接必需） |
| 管理界面 | 本机 **ops-console** 可纳管 group `remote`（启停、端口）；对接方若自管进程，按上表脚本即可 |

---

## 7. 运行环境

| 项 | 说明 |
|----|------|
| 操作系统 | Linux（当前部署环境） |
| Python | **3.11**（与 conda 环境 `asr` / 项目 `requirements.txt` 一致） |
| 依赖 | `requirements.txt`；系统需 **ffmpeg**、**redis-server**（异步队列） |
| Conda 环境名 | 建议 `asr` |
| 模型文件位置 | 项目根下 `models/`（见 §1） |
| 配置 | `config/model_config.yaml`、`config/server_config.yaml` |
| 硬件 | 支持 CPU；生产异步建议 **NVIDIA GPU**（Worker 按 `worker.devices` 绑定，如多路 `cuda:0`）。SenseVoice ONNX 可用 `onnxruntime-gpu`；显存随并发与引擎变化，建议按并发压测核定 |
| CUDA | GPU 路径对齐 PyTorch cu124 / 本机驱动（见 `requirements.txt` 安装说明） |

---

## 8. 管理条件

| 项 | 说明 |
|----|------|
| 运行机器 | 以实际部署机为准；ops-console 登记示例：`project_root` = `.../para/whisper/remote/remote-v2`，API 默认端口 **8003**，Web **5004** |
| 平台如何连接 | HTTPS 调用 `https://<host>:8003/api/v1/asr/...`；实时用 `wss://<host>:8003/api/v1/asr/live-subtitles` |
| 管理 Agent | 可由平台侧部署 Agent / 使用现有 ops-console 纳管启停；**不以 Agent 为调用 ASR 的前提** |
| 是否专供平台启停 | **不强制**；也可由运维用 `scripts/start_*.sh` / `stop_*.sh` 独立启停。若平台独占管理，请约定勿双端同时启停同一端口 |

---

## 9. 接口详情与请求示例

### 9.1 健康检查

```bash
curl -k "https://127.0.0.1:8003/api/v1/asr/health"
```

**成功**

```json
{
  "success": true,
  "data": {
    "status": "ok",
    "model_loaded": true,
    "registered_users": 3,
    "recognition_mode": "asr_only",
    "version": "14.2"
  }
}
```

---

### 9.2 同步转写 `POST /api/v1/asr/transcripts`

| 字段 | 必填 | 说明 |
|------|------|------|
| `audio` | 是 | 音频文件 |
| `outputs` | 否 | 逗号分隔，默认 `transcript` |
| `engine` | 否 | `sensevoice` / `paraformer` |
| `device_id` / `threshold` / `request_id` | 否 | 可选 |

**只要转写**

```bash
curl -k -X POST "https://127.0.0.1:8003/api/v1/asr/transcripts" \
  -F "audio=@/path/to/audio.wav" \
  -F "outputs=transcript" \
  -F "engine=sensevoice"
```

**转写 + 声纹**

```bash
curl -k -X POST "https://127.0.0.1:8003/api/v1/asr/transcripts" \
  -F "audio=@/path/to/audio.wav" \
  -F "outputs=transcript,speakers" \
  -F "engine=sensevoice"
```

**转写 + 字幕 + 翻译 + 总结**

```bash
curl -k -X POST "https://127.0.0.1:8003/api/v1/asr/transcripts" \
  -F "audio=@/path/to/audio.wav" \
  -F "outputs=transcript,subtitles,translation,summary" \
  -F "engine=sensevoice"
```

**Python**

```python
import requests

url = "https://127.0.0.1:8003/api/v1/asr/transcripts"
with open("audio.wav", "rb") as f:
    r = requests.post(
        url,
        files={"audio": ("audio.wav", f, "audio/wav")},
        data={"outputs": "transcript,speakers", "engine": "sensevoice"},
        verify=False,
        timeout=600,
    )
print(r.status_code, r.json())
```

**成功响应（示意）**

```json
{
  "success": true,
  "outputs": "transcript,speakers",
  "engine": "sensevoice",
  "recognition_mode": "diarization",
  "data": {
    "speaker_id": "unknown",
    "confidence": 9.06,
    "engine": "sensevoice"
  },
  "details": {
    "request_id": "...",
    "segments": [
      {
        "start": 0.3,
        "end": 2.8,
        "user": "unknown",
        "text": "正是因为存在绝对正义，所以我们接受现实的相对正义。",
        "transcript": "正是因为存在绝对正义，所以我们接受现实的相对正义。"
      }
    ],
    "detected_speakers": ["unknown"],
    "processing_time": 1.5
  }
}
```

**失败响应（参数错误，HTTP 400）**

```json
{
  "detail": "subtitles / translation / summary 依赖转写，请在 outputs 中包含 transcript"
}
```

**失败响应（超同步时长，HTTP 400）**

```json
{
  "detail": "音频时长 2000s 超过同步接口上限 1800s，请使用异步接口 POST /api/v1/asr/transcripts/async"
}
```

**失败响应（服务未就绪，HTTP 503）**

```json
{
  "detail": "Service not ready"
}
```

---

### 9.3 异步转写

#### `POST /api/v1/asr/transcripts/async` → HTTP 202

```bash
curl -k -X POST "https://127.0.0.1:8003/api/v1/asr/transcripts/async" \
  -F "audio=@/path/to/long.wav" \
  -F "outputs=transcript" \
  -F "engine=sensevoice"
```

**成功**

```json
{
  "success": true,
  "data": {
    "task_id": "007a4bd0-de8a-4ac5-a4bb-237322abff91",
    "status": "pending",
    "outputs": "transcript",
    "engine": "sensevoice",
    "message": "任务已入队，请通过 GET /api/v1/asr/transcripts/{task_id} 查询进度"
  }
}
```

#### `GET /api/v1/asr/transcripts/{task_id}`

```bash
curl -k "https://127.0.0.1:8003/api/v1/asr/transcripts/007a4bd0-de8a-4ac5-a4bb-237322abff91"
```

**进行中**

```json
{
  "success": true,
  "data": {
    "task_id": "007a4bd0-de8a-4ac5-a4bb-237322abff91",
    "status": "processing",
    "progress": 40.0,
    "stage": "正在处理 Block 1/2",
    "error": null
  }
}
```

**完成**：`status=completed`，识别结果在 `data.result`。  
**失败**：`status=failed`，原因在 `data.error`。  
**任务不存在（HTTP 404）**

```json
{
  "detail": "Task xxx not found"
}
```

**Python 轮询**

```python
import time
import requests

base = "https://127.0.0.1:8003"
with open("long.wav", "rb") as f:
    r = requests.post(
        f"{base}/api/v1/asr/transcripts/async",
        files={"audio": ("long.wav", f, "audio/wav")},
        data={"outputs": "transcript", "engine": "sensevoice"},
        verify=False,
    )
task_id = r.json()["data"]["task_id"]

while True:
    data = requests.get(f"{base}/api/v1/asr/transcripts/{task_id}", verify=False).json()["data"]
    if data["status"] in ("completed", "failed"):
        print(data)
        break
    time.sleep(1)
```

---

### 9.4 仅说话人识别

```bash
curl -k -X POST "https://127.0.0.1:8003/api/v1/asr/speaker-identifications" \
  -F "audio=@/path/to/audio.wav" \
  -F "engine=sensevoice"
```

需 `diarization.enabled=true` 且声纹组件已加载。

---

### 9.5 实时字幕 `WS /api/v1/asr/live-subtitles`

- URL：`wss://<host>:8003/api/v1/asr/live-subtitles`
- 引擎固定 SenseVoice；不支持声纹 / 总结 / 字级时间戳
- 客户端：`start`（JSON）→ PCM 二进制 → `stop`（JSON）
- 服务端：`started` / `ready` / `recognizing` / `subtitle` / `error` / `stopped`

```python
import asyncio, json, ssl, wave
import numpy as np
import websockets

async def main():
    ctx = ssl._create_unverified_context()
    uri = "wss://127.0.0.1:8003/api/v1/asr/live-subtitles"
    with wave.open("audio.wav", "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    async with websockets.connect(uri, ssl=ctx) as ws:
        print(await ws.recv())
        await ws.send(json.dumps({"type": "start", "sample_rate": 16000, "translate": True}))
        print(await ws.recv())
        step = 8000
        for i in range(0, len(pcm), step):
            await ws.send(pcm[i : i + step].tobytes())
            await asyncio.sleep(0.02)
        await ws.send(json.dumps({"type": "stop"}))
        while True:
            msg = json.loads(await ws.recv())
            print(msg)
            if msg.get("type") in ("stopped", "error"):
                break

asyncio.run(main())
```

**subtitle 成功帧（示意）**

```json
{
  "type": "subtitle",
  "text": "正是因为存在绝对正义，所以我们接受现实的相对正义。",
  "text_zh": "正是因为存在绝对正义，所以我们接受现实的相对正义。",
  "text_en": "Because there is absolute justice...",
  "start": 0.3,
  "end": 2.82
}
```

---

### 9.6 声纹库

**登记（单样本 / 多样本字段名均为 `audio`）**

```bash
curl -k -X POST "https://127.0.0.1:8003/api/v1/asr/speakers" \
  -F "user_id=zhangsan" \
  -F "audio=@/path/to/sample1.wav" \
  -F "overwrite=true"

curl -k -X POST "https://127.0.0.1:8003/api/v1/asr/speakers" \
  -F "user_id=zhangsan" \
  -F "audio=@/path/to/sample1.wav" \
  -F "audio=@/path/to/sample2.wav" \
  -F "audio=@/path/to/sample3.wav" \
  -F "overwrite=true"
```

**列表**

```bash
curl -k "https://127.0.0.1:8003/api/v1/asr/speakers"
```

```json
{
  "success": true,
  "data": {
    "speakers": [
      {"speaker_id": "zhangsan", "registered_at": "...", "num_samples": 3}
    ],
    "count": 1
  }
}
```

**删除**

```bash
curl -k -X DELETE "https://127.0.0.1:8003/api/v1/asr/speakers/zhangsan"
```

---

## 10. HTTP 状态码与约定

| HTTP | 含义 |
|------|------|
| 200 | 同步成功 / 查询成功 |
| 202 | 异步已入队 |
| 400 | 参数或业务校验失败 |
| 413 | 文件过大 |
| 404 | 任务或说话人不存在 |
| 503 | 服务未就绪 |

- 自签证书：`curl -k` 或客户端 `verify=False`。
- 新接入请只使用 `/api/v1/asr/*`；旧路径仅为兼容别名。
- 配置以部署机 `config/model_config.yaml`、`config/server_config.yaml` 为准；变更引擎默认值或模型路径后需重启 API（及 Workers）。
