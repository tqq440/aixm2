# 公共 API 与管理页面（模块 5）

这一部分完成外部调用入口：**业务应用 → 入口 Node → 中心平台 → 目标 Node → 模型服务**。业务应用拿到独立 API Key 后，可以直接上传文件、查询任务和结果、请求取消，或者接收 LLM 文本流，不需要打开网页。

中心保存应用身份、Key 摘要、权限、任务和权威结果。入口 Node 只转发公共接口，不保存应用账户，也没有推理队列。中心不可用时返回暂不可用；不会改成本机接收或绕过中心执行。CPU/GPU 内存、显存管理继续由业务侧负责。

## 本轮默认状态

本轮仅在隔离资料与模拟业务服务上验证。**日常数据库和 .env 未切换，公共入口默认关闭**；新增中心迁移 `0020`，Node 独立迁移链仍 `0004`。真实 Redis、模型、Linux、Docker、多主机及跨 Node 全平台查询总预算仍留模块 6 核验，不能把回环协议验证当成真实模型验收。

## 管理员操作

页面新增“应用与任务监控”，沿用本机管理边界：

1. 在“服务管理”登记服务与目标，展开“关联目标 Node”，填写 Node 编号、本机服务编号、固定配置修订、Node 地址和中心执行凭据的环境变量名。凭据只放环境，不填原值。Node 本机服务资料和启动实例仍使用 4A/4B 的受控登记接口。
2. 保存草稿、测试连接、启用目录分别操作。新修订只影响新任务。目录启用本身不启动模型，不证明真实设备或容量。
3. 登记独立应用编号，选择允许的服务和并发额度。应用额度与目标、所有共享组继续在原 SQLite 事务中同时预约；不是显存额度。原 `local-user` 作为历史本机身份，不签发公共 Key。
4. 创建 Key，完整值只显示这一次。平台 SQLite 只保存随机 Key 编号和 SHA256，不保存可用原值；浏览器不写 localStorage/sessionStorage。隐藏、离开页面后应从安全凭据库取用。
5. 轮换会创建新 Key 并在同一事务撤销旧 Key。任务属于应用，换 Key 不改变归属。撤销 Key 或停用应用影响后续认证，不会中断已发送业务或释放未知容量。服务权限撤销后，该服务的历史任务也不再向这个应用展示；管理员仍可核查。

任务页筛选排队、运行、成功、失败、取消、结果未知；查看平台分配设备、业务实际设备、业务内部队列位置、进度、可信更新时间、异常和游标事件时间线。缺进度接口不显示伪造百分比；实际设备编号沿用 service/host 命名空间。节点页面默认只读平台观测；“核对节点状态”只 GET Node 已保存的就绪档案，不调用推理。额度表区分已用与未知占位。

任务/进度页面的 GET 不触发调度、推理或即时上游查询。刷新显示原来的事实，查询异常不会把推理判失败；显式远端核对仍使用 4C2 的管理 POST。Node 主机资源采集与容器控制继续走独立控制接口，当前中心节点页主要显示服务就绪与接入问题。

## 部署配置边界

先暂停受理并核对在途、成套备份，再按既有迁移流程升级中心到 `20261021_0020`，Node 到 `20261021_node_0004`。本轮没有对日常库执行这些动作。旧 Worker 与新公共链路不能执行同一任务。

中心独立配置文件：

```dotenv
INFERENCE_COMMON_EXECUTION_ENABLED=true
INFERENCE_PUBLIC_API_ENABLED=true
INFERENCE_DATABASE_PATH=/absolute/path/central.sqlite3
INFERENCE_UPLOAD_DIR=/absolute/path/uploads
INFERENCE_LOG_DIR=/absolute/path/logs
# 真实值由部署环境/受保护文件提供，不提交仓库：
# INFERENCE_INGRESS_TOKEN=<32到256位独立随机凭据>
# LAB_NODE_EXECUTOR=<目标Node的中心执行凭据>
# 正式中心队列沿用模块3的Redis配置；缺依赖或连接不回退到SQLite派发。
# INFERENCE_COMMON_QUEUE_BACKEND=redis
# INFERENCE_REDIS_URL=<受保护Redis连接>
```

入口 Node 独立配置：

```dotenv
NODE_ID=entry-node
# 其余独立数据库、日志、管理员凭据等沿用4A。
NODE_CENTRAL_URL=http://127.0.0.1:5000
# 部署时若需要校园网访问，Node支持显式NODE_HOST=0.0.0.0；真实部署留模块6。
# NODE_INGRESS_TOKEN=<与中心INFERENCE_INGRESS_TOKEN相同的入口凭据>
```

Node 地址须为完整 origin，不能带账号、路径、查询或跳转。入口凭据须独立于 Node 管理、观察、执行、业务报告和上游业务凭据。中心公共 API 同时要求可信入口凭据和应用 Key，外部只需携带自己的应用 Key；Node 自动附加入口凭据。应用 Key 不能直接调用目标 Node 的内部执行接口。

中心当前仍保持原本机监听限制，以上先让入口 Node 与中心同机；外部应用访问 Node。跨主机入口到中心的网络部署、可信代理与Linux监听配置在模块6处理，本轮不宣称多主机已验收。

目标 Node 的 `NODE_BUSINESS_ENABLED=true`、业务协议、生命周期上报和执行凭据沿用 [4C2](业务协议与监控-模块4C2.md)。入口和目标可以是同一个 Node。中心显式运行 `backend.modules.worker.node_executor --env-file ...`；LLM 由公共 POST 直接进入相同容量/桥接层，默认最多等 2 秒，不进离线队列、不抢占。

公共开关开启后，原 `/api/*` 旧业务/下载/管理接口只允许可信本机页面或本机客户端，不让远程调用者用旧入口绕过应用隔离。新 `/api/v1` 的列表、详情、事件、监控、取消和下载均检查应用与服务权限；其他应用的编号返回 404。保留原本机兼容入口，未把本机管理接口当作互联网管理账户系统。

## 公共接口

请求头：`Authorization: Bearer <应用Key>`。提交必须带稳定的 `Idempotency-Key`（1—128 位英文数字、点、下划线、冒号或短横线），同一业务请求一直使用同一个键。

| 接口 | 行为 |
|---|---|
| GET `/api/v1/services` | 只返回应用获准服务的逻辑资料，不泄露内部地址/凭据 |
| POST `/api/v1/tasks/asr` | multipart：serviceId、file、可选 language；Remote-V2 不支持指定 language |
| POST `/api/v1/tasks/vision` | multipart：serviceId、file、可选 parameters JSON 字符串 |
| GET `/api/v1/tasks` | page/pageSize/status/serviceId/kind；仅本应用离线任务 |
| GET `/api/v1/tasks/{id}` | 原任务与公共状态、等待原因、结果可用性 |
| GET `/api/v1/tasks/{id}/result` | 保存的结果、lateResult、originalOutcome；未保存返回409 |
| GET `/api/v1/tasks/{id}/result/download?format=json` | 受权限保护的 JSON 下载；ASR 另支持 format=txt |
| POST `/api/v1/tasks/{id}/cancel` | 空 JSON，请求取消；响应 cancelAction/cancelError 不等于远端已停 |
| GET `.../{id}/events` | afterSequence 游标，默认50最多200，返回 nextSequence/hasMore |
| GET `.../{id}/monitoring` | 平台状态、分配、服务快照/异常和结果待取状态 |
| POST `/api/v1/llm/requests` | JSON：serviceId/messages/parameters，返回自定义 SSE |
| GET `/api/v1/llm/requests/{id}` | 在线元数据，不存聊天正文，不重放 token |
| POST `/api/v1/llm/requests/{id}/cancel` | 空 JSON，请求取消 |
| GET `.../llm/requests/{id}/events`、`/monitoring` | 相同归属隔离与只读规则 |
| GET `/api/v1/openapi.json` | 当前公共 OpenAPI，仍须应用 Key |

除 OpenAPI、SSE 和下载外，JSON 成功响应为 `{data, requestId}`，错误为 `{error:{code,message,details},requestId}`。这里 `requestId` 是 HTTP 排错编号；任务用 `data.id/taskId`，在线请求用 `id/onlineRequestId`，公共调用用 `invocationId`，不要混用。

离线任务返回 202，前提是媒体文件、任务、幂等档案和调用 outbox 均已持久化；事务失败会清理本次未引用文件。同一键和内容返回原任务，不创建第二次推理；不同内容使用同一键返回409。中心暂时故障可能导致提交结果不明，请保留原键重试受理，不更换键重复推理。

ASR 限20 MiB/300秒，视觉限10 MiB/2000万像素，且不能超过服务配置。实际解码核验，上传文件名不作为路径。没有为公共受理生成旧执行队列消息，不会让新旧 Worker 同时执行。

LLM 正常响应为 `text/event-stream`：先 `accepted`（包含 id），再 `delta`（requestId/text），最后 `done` 或 `error`。只有已验证终帧且结果元数据保存后才 `done`，普通 EOF 不是成功。重复 POST 返回普通 JSON `{data:{request,streamReplayed:false},requestId}`，从不重放 token。响应头 `X-Online-Request-ID` 也给出在线 id。忙时429会在消息中给出已保存但未发送的请求编号；Node 失联或断流可能保留未知占位。

查询或取消失败不重发推理。超期后取得的可信结果保留原超时历史；结果接口用 `lateResult=true` 明示迟到，不改写原成功/失败过程。

## curl 示例：仅访问入口 Node

以下由调用方在自己的终端使用，环境变量从安全配置中提供，不把真实 Key 写进示例或仓库。

```bash
export INFERENCE_NODE_URL=http://entry-node:8020
# 从安全配置载入 INFERENCE_API_KEY
curl --fail-with-body -H "Authorization: Bearer $INFERENCE_API_KEY" "$INFERENCE_NODE_URL/api/v1/services"

curl --fail-with-body -H "Authorization: Bearer $INFERENCE_API_KEY" \
  -H 'Idempotency-Key: attendance-audio-001' \
  -F 'serviceId=campus-asr' -F 'file=@sample.wav' "$INFERENCE_NODE_URL/api/v1/tasks/asr"

curl --fail-with-body -H "Authorization: Bearer $INFERENCE_API_KEY" \
  -H 'Idempotency-Key: camera-image-001' -F 'serviceId=campus-yolo' -F 'file=@sample.png' \
  -F 'parameters={"confidenceThreshold":0.25,"iouThreshold":0.45,"maxDetections":100}' \
  "$INFERENCE_NODE_URL/api/v1/tasks/vision"

curl --fail-with-body -H "Authorization: Bearer $INFERENCE_API_KEY" "$INFERENCE_NODE_URL/api/v1/tasks/$TASK_ID"
curl --fail-with-body -H "Authorization: Bearer $INFERENCE_API_KEY" "$INFERENCE_NODE_URL/api/v1/tasks/$TASK_ID/result"
curl --fail-with-body -H "Authorization: Bearer $INFERENCE_API_KEY" "$INFERENCE_NODE_URL/api/v1/tasks/$TASK_ID/events?limit=50&afterSequence=0"
curl --fail-with-body -X POST -H "Authorization: Bearer $INFERENCE_API_KEY" -H 'Content-Type: application/json' \
  -d '{}' "$INFERENCE_NODE_URL/api/v1/tasks/$TASK_ID/cancel"

curl --no-buffer --fail-with-body -H "Authorization: Bearer $INFERENCE_API_KEY" \
  -H 'Idempotency-Key: chat-turn-001' -H 'Content-Type: application/json' \
  -d '{"serviceId":"campus-llm","messages":[{"role":"user","content":"介绍一下图书馆"}],"parameters":{"temperature":0.7,"maxTokens":128}}' \
  "$INFERENCE_NODE_URL/api/v1/llm/requests"
```

Python 标准库示例：[examples/gateway_client.py](../examples/gateway_client.py)，不新增依赖：

```bash
python examples/gateway_client.py --kind asr --service campus-asr --file sample.wav --idempotency-key audio-001
python examples/gateway_client.py --kind vision --service campus-yolo --file sample.png --idempotency-key image-001
python examples/gateway_client.py --kind llm --service campus-llm --text '介绍一下图书馆' --idempotency-key chat-001
```

示例只在业务请求明确需要时调用真实服务，本轮没有用示例访问实验室模型。[OpenAPI快照](gateway.openapi.json) 与实际公共路由有测试核对；[Node控制面快照](node-foundation.openapi.json) 另列内部和控制角色，不混为公共权限。

## 本机管理 API 与待验项目

`/api/admin/gateway/status`、`/applications`、PUT `/applications/{id}`、POST `/applications/{id}/keys`、POST `/applications/{id}/keys/{keyId}/rotate|revoke`；GET `/requests`、`/requests/{invocationId}/events|monitoring`、GET `/nodes`、POST `/nodes/refresh`。管理写请求必须本机、可信 Origin、JSON，表单不存真实 Key。显式远端核对继续用 4C2 的 `/api/admin/common-execution/invocations/{id}/reconcile`。

当前提供并发与受理上限，不宣称已提供按分钟计费或 RPM 配额。中心本机管理不是完整远程管理员登录系统。真实服务缺接口仍记录接入问题，不伪造验证通过。下一模块 6 执行 Linux/systemd、Redis/Lua、多主机、真实服务及备份恢复验收；跨 Node 查询总预算也仍在该阶段处理。实际验证和保护证据见[本轮记录](阶段记录/公共API与管理-模块5-验收记录.md)。本模块讲解后暂停。
