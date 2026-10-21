# Node 业务生命周期上报协议 lifecycle-v1

## 4C2 增量：逐任务查询与生命周期分离

生命周期心跳继续报告启动、预热、接单和排空，不直接结算任务。逐任务新增可选 `job-json-v1`，必须关联调用编号、执行次数、中心/Node修订；进度和实际设备只采纳业务证据，设备命名空间默认service（不猜宿主机GPU编号）。参考HTTP提交、查询、结果、取消协议见[4C2](业务协议与监控-模块4C2.md)；缺接口记录接入缺口，师兄的真实ASR队列/上报/排空仍待接入。

## 4C1 增量：中心执行身份与业务上报分权

中心到 Node 新增独立 `NODE_EXECUTOR_TOKEN`，不能复用业务报告、管理员或观察凭据。内部执行档案与生命周期报告分开：报告空队列/停止不直接结算中心任务，也不能绕过 Node 未决调用的停止升级门禁。业务报告格式和原指纹保持。

`catalogServiceId/catalogRevision` 仍是关联资料；执行以中心授权命令中的中心修订、固定 Node 配置修订及实例共同绑定，避免双向内容哈希相互依赖。4C1 不新增实际任务进度/设备上报协议，具体业务适配与采集在 4C2。见[执行协议与恢复说明](Node执行恢复-模块4C1.md)。下方保留 4A/4B 协议。

## 模块 4B 兼容扩展：受控启动、排空和恢复

4A 字段与接口保持兼容。受管容器启动后，业务使用自己的上报 Bearer 凭据，先 GET `/node/internal/v1/services/{serviceId}/assignment`，读取 instanceId/configRevision/generation/sequence/startPaused/operationId。**每次受控容器启动都读取新身份，不复用上次启动 UUID 或重置旧实例序号。** Node 重启但业务未重启时继续原实例及递增序号。

startPaused=true 时，业务先完成加载/预热，报告 ready/healthy/modelReady=true，但 acceptingRequests=false，并报告自身队列与活动数量。Node 恢复命令得到确认前不得自行开放接单。受管启动身份由 Node 控制流程生成，旧手工 instances 接口不用于受管容器。

业务实现登记的 drain/resume POST 路径，使用与本服务相同的专用凭据验证 Node，请求为：

```json
{"operationId":"操作UUID","instanceId":"当前启动UUID","configRevision":"当前配置修订","action":"drain"}
```

返回同样四个字段及 `accepted:true`。应用命令前核对 assignment 当前操作/实例/修订，拒绝旧命令；相同命令幂等。drain 关闭业务入口并保持关闭，直到当前有效的 resume；只返回 ACK 不代表已经排空。resume 同理，不能把 ACK 当作已恢复接单。

在随后生命周期报告中增加可选 `controlOperationId` 和 `controlAction`（drain/resume），必须同时提供或同时省略。排空完成报告须为 draining/stopped、acceptingRequests=false、queuedTasks=0、activeTasks=0，且序号大于命令发出前的报告。恢复后报告 healthy/modelReady=true/acceptingRequests=true，并关联对应 resume；零计数和 ready 可代表业务仍空闲，不必伪造 busy。

无这两个字段的旧心跳仍正常接收，但不能证明某个控制命令已经完成。Node 保存新字段用于核对，不把 HTTP200、旧的空队列、失联或进度100%当作排空/退出证明。旧报告缺新字段时不改变其规范化指纹，0001→0002 后幂等语义保持。

Node 为受控暂停启动的新实例保存“可能已经接单/发生任务活动”标记；任何接单或非零队列/活动报告都会永久置位，不能靠后来的空队列报告恢复成“从未接单”。升级前历史实例保守视为可能用过，不伪造此前过程。

该扩展不增加 Node 推理队列，不把排队搬出 ASR；不修改模型加载或内存策略。详细流程见[主机管理 4B](主机管理-模块4B.md)。下文保留 4A 首版接口及单次上报示例。

本协议由平台侧提供，真实 ASR 内部队列、模型内存/显存管理、上报和排空由业务侧接入。4A 仅实现生命周期和可空任务数量快照，不包含逐任务进度、实际执行设备、排空控制或结果传输。

## 接入步骤与身份

1. 管理者登记服务资料和对应 `NODE_REPORT_TOKEN_...` 环境引用，Node 返回不可变 configRevision。
2. 管理者生成 UUID，调用 POST `/node/v1/admin/services/{serviceId}/instances`，JSON 为 `{"instanceId":"UUID","configRevision":"64位修订","expectedGeneration":0}`。重放相同绑定不重复建实例，旧代次拒绝。这个操作不启动任何服务。
3. 将 instanceId、configRevision、服务上报凭据交给对应业务启动环境。管理凭据和只读凭据不得交给业务。
4. 业务向本机 Node 的 POST `/node/internal/v1/services/{serviceId}/reports` 发送 JSON，并在 Authorization 中使用该服务的 Bearer 凭据。

新 Node 端口默认 8020，实际主机、容器访问地址和网络隔离在部署时确定。上报端口与业务调用端口区分；外部使用者通过 Node 获取状态，不直接访问 ASR 的内部状态接口。

## 请求字段

```json
{
  "instanceId": "b03516d6-8660-43b2-87d2-fc5e7c3edcc2",
  "configRevision": "此处替换为登记返回的64位小写十六进制修订",
  "sequence": 3,
  "state": "ready",
  "health": "healthy",
  "modelReady": true,
  "acceptingRequests": true,
  "queuedTasks": 0,
  "activeTasks": 0,
  "errorCode": null,
  "reportedAt": "2026-10-21T08:00:00+00:00"
}
```

| 字段 | 规则 |
|---|---|
| instanceId/configRevision | 必须属于当前管理者绑定的实例及其修订 |
| sequence | 严格整数，1—9007199254740991；同一启动实例持续递增，重试同一条使用原序号和原内容 |
| state | starting / warming / ready / busy / draining / stopped / failed |
| health | unknown / healthy / unhealthy，默认 unknown；仅为业务报告，不等于容器观测 |
| modelReady | 严格布尔值，默认 false |
| acceptingRequests | 严格布尔值，默认 false；表示业务是否接受新提交 |
| queuedTasks/activeTasks | 未提供为 null；提供时为 0—1000000 的整数，来源是业务内部账本 |
| errorCode | 可空的最多64字符大写字母/数字/下划线错误标识；不得传密钥、异常正文或原始响应 |
| reportedAt | 可选、带时区 ISO 时间；只归档，不能控制 Node 的新鲜度 |

ready/busy 必须同时报告 modelReady=true、health=healthy；只有这两种状态可声明 acceptingRequests=true。busy 提供 activeTasks 时不能为 0。stopped 必须明确 modelReady=false、acceptingRequests=false、queuedTasks=0、activeTasks=0；这表示业务自报停止，不能证明容器已经退出。

不提供进度/实际设备时不推算，不把全局健康视作逐任务证明。未定义字段、字符串形式的布尔值/计数、负数和无时区时间被拒绝，正文上限 16 KiB。

## 重复、过期与重启

- 相同序号及相同规范化内容返回 duplicate=true，不刷新接收时间或重复写事件。
- 更小序号、相同序号不同内容、旧实例或旧修订返回 HTTP409，不改变快照。
- 停止后的同一实例不能重新报告运行。新业务启动需要新的受控实例身份；旧实例未报告停止时 4A 不允许直接替换。
- 建议每五秒发新序号；Node 接收后满十五秒标记过期，effective state=unknown，保留最后可信 reportedState 和时间。
- Node 重启不要求业务重启。原实例发送更大序号的新心跳即可恢复新鲜度；重放重启前的最后一条不算新心跳。
- 接口返回或网络结果不明时，先重试同一条相同内容。不要因一次网络异常重置序号或伪造新的业务实例。
- 业务进程重启后的实例创建和退出证据，将通过 4B 的受控生命周期接入，不靠复用旧 UUID 或删除 Node 数据绕过。

200 成功响应为 `{"data":{"duplicate":false,"service":{...}},"requestId":"..."}`。401 表示凭据不符，409 表示身份/修订/序号冲突，422 表示字段不合法，413 表示过大。错误响应不回显请求原值；500/断线不证明上报已经或尚未提交，应按原序号幂等核对。

## Python 参考

以下是业务侧集成示意，实际实例、修订和凭据由启动环境注入，不写入镜像或源代码。样例展示单次上报；持续心跳由业务侧维护递增序号和自身状态。

```python
import json
import os
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

report = {
    "instanceId": os.environ["ASR_NODE_INSTANCE_ID"],
    "configRevision": os.environ["ASR_NODE_CONFIG_REVISION"],
    "sequence": 1,
    "state": "starting",
    "health": "unknown",
    "modelReady": False,
    "acceptingRequests": False,
}
request = Request(
    os.environ["ASR_NODE_REPORT_URL"],
    data=json.dumps(report).encode("utf-8"),
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer " + os.environ["ASR_NODE_REPORT_TOKEN"]},
)
with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=3) as response:
    receipt = json.load(response)
```

可信 HTTPS 使用默认验证；不跟随重定向，不把凭据放在 URL。隔离模拟器和真实业务分别验收；本协议提供不代表师兄服务已经支持。
