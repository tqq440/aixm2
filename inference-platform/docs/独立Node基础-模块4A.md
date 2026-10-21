# 独立主机 Node 基础（模块 4A）

> 2026-10-21 后续已进入模块 4B；当前容器控制、指标和扩展接口见[主机管理 4B](主机管理-模块4B.md)。下文是 4A 交付时的能力边界与用法，原65项证据保持历史含义。

本模块提供独立 HTTP 控制面基础：多服务登记、不可变配置修订、业务启动身份绑定、上报鉴权、心跳、新鲜度、只读快照和追加事件。支持 ASR、YOLO（kind=vision）、LLM 的资料和生命周期协议，未运行任何模型。

最终架构仍为：外部 → 入口 Node → 中心平台 Redis/调度/容量/权威结果 → 目标 Node → 业务服务。4A 尚未实现此业务中转路径，`executionReady`、`businessProxy`、`publicGateway` 均为 false；无容器控制、资源动态采集或推理队列。下一步 4B 管主机，4C 再接执行与监控，5 再开放应用 API。

## 独立运行

Node 入口不读取平台 `.env` 或 AppSettings，不导入平台应用、不启动旧 Agent/Worker。自己的 SQLite 默认 `runtime/host-node/node.sqlite3`，日志默认 `runtime/host-node/logs`；两个用途的数据库不能混用。独立迁移链位于 `node-migrations`，版本表 `host_node_alembic_versions`；不会升级中心 0014。

沿用现有 project311。依赖声明 `requirements/node.txt` 复用 base 和已有 filelock；本轮没有安装下载。下例是 Linux shell 操作示意；真实 Linux、systemd、生产 WSGI、TLS 和容器接入留模块 6，当前 CLI 用于回环开发与协议联调。

```bash
export NODE_ID=lab-node-01
export NODE_DATABASE_PATH=runtime/host-node-dev/node.sqlite3
export NODE_LOG_DIR=runtime/host-node-dev/logs
export NODE_HOST=127.0.0.1
export NODE_PORT=8020
# 每个变量分别生成不同的随机值，勿把真实值提交到仓库。
export NODE_ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export NODE_OBSERVER_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export NODE_REPORT_TOKEN_ASR="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

python -m backend.modules.host_node.node_cli init-db
python -m backend.modules.host_node.node_cli serve
```

也可显式 `python -m backend.modules.host_node.node_cli --env-file runtime/host-node-dev/.env.node init-db` / `serve`。文件不进行变量插值，进程环境优先；默认不自动读取任何环境文件。秘密为 32—256 个非空白 ASCII 字符，管理、只读及各服务上报凭据必须不同。

启动前须显式 init-db；重复初始化保持记录。已有非 Node 表、身份不符或同库已有 Node 运行时拒绝启动/迁移。一个状态库只允许一个控制进程，HTTP 工作线程共享进程锁。关闭只记录 Node 本身退出并释放自己的锁，不关闭业务。

## 已实现接口

除标准 OPTIONS 外，所有功能接口均要求 `Authorization: Bearer ...`，没有网页登录要求。

| 方法与路径 | 凭据 | 内容 |
|---|---|---|
| GET `/node/v1/status` | 管理或只读 | 当前 Node 启动代次、时间和能力边界 |
| GET `/node/v1/services` | 管理或只读 | 分页服务快照；page 默认 1，pageSize 默认 20、最多 100 |
| GET `/node/v1/services/{id}` | 管理或只读 | 本地快照，不即时查询业务 |
| GET `/node/v1/events` | 管理或只读 | Node 和全部服务事件 |
| GET `/node/v1/services/{id}/events` | 管理或只读 | 单服务事件 |
| GET/PUT `/node/v1/admin/services/{id}` | 管理 | 读/存资料，带 expectedVersion 的并发校验 |
| POST `/node/v1/admin/services/{id}/instances` | 管理 | 绑定本次业务上报身份；不启动容器 |
| POST `/node/internal/v1/services/{id}/reports` | 对应服务 | 上报生命周期与任务数量 |
| GET `/node/v1/openapi.json` | 管理或只读 | 精确覆盖当前路由及请求/响应 Schema |

JSON 请求上限 16 KiB。事件 afterSequence 游标默认 0、limit 默认 50、最多 200；不自动清理历史。GET 只读本地，不写事件、不修改新鲜度、不采样、不发网络。查询变更操作或提供重复/未知参数返回校验错误。OpenAPI 静态副本为 `docs/node-foundation.openapi.json`。

## 登记与业务接入

```bash
curl -X PUT http://127.0.0.1:8020/node/v1/admin/services/asr \
  -H "Authorization: Bearer $NODE_ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"expectedVersion":null,"config":{"name":"实验室 ASR","kind":"asr","modelId":"sensevoice","internalBaseUrl":"http://127.0.0.1:8003","reportProtocol":"lifecycle-v1","reportTokenEnv":"NODE_REPORT_TOKEN_ASR"}}'

curl http://127.0.0.1:8020/node/v1/services/asr \
  -H "Authorization: Bearer $NODE_OBSERVER_TOKEN"
```

地址仅登记，不进行 health 或推理调用。可选 catalogServiceId/catalogRevision 只保存中心关联声明，不证明中心已有该配置或调度已启用。每个服务使用自己的上报环境引用，不能复用另一服务的引用。

根据返回 configRevision，管理者创建 UUID，并以 `instanceId/configRevision/expectedGeneration` 调用 instances 接口，取得启动身份后交给业务侧。首次 expectedGeneration=0；同一请求可幂等重放。4B 将把绑定身份接入受控启动；4A 的绑定只是准备接收报告。

业务状态上报的完整 Python 示例和规则见[Node 业务上报协议](Node业务上报协议-v1.md)。模拟器在 `tests/fixtures/host-node/simulated_asr.py`，显式接收实例与修订，只允许回环地址；它不加载模型、不排任务、不产生转写结果。

缺上报能力可登记 `reportProtocol=none, reportTokenEnv=null`，返回 `LIFECYCLE_REPORT_UNSUPPORTED`，不伪装就绪。活跃或状态未知的实例阻止配置改写及绑定替代实例；有效 stopped 报告只说明业务声明停止，**容器退出仍为 unknown**，真正的进程/容器证据在 4B 建立。

## 恢复、日志和边界

报告默认每五秒一次；以 Node 接收时间判定，满十五秒过期，时钟倒退也按未知处理。保留 reportedState/最后接收时间，effective state 变为 unknown、停止声明可接单；不把过期写成 stopped/failed。

Node 重启保持资料、实例和序号，但上一 Node 启动代次收到的报告暂不视作当前就绪。业务须用原实例发出更大序号的新心跳；相同序号的重复报告不恢复新鲜度。凭据轮换通过更新 Node 环境并重启生效，业务实例及任务身份不随 Key 改变。

配置、实例绑定、业务状态变化和 Node 启停事件与状态在同一事务保存。普通心跳更新快照，不逐次写事件；队列数量变动可从最新快照看到。日志使用独立进程滚动文件 `host-node-<PID>.log`，2 MiB、3 备份，复用原 JSON 格式；不记录 Authorization、上报正文、音频、图片或聊天内容。已提交的状态不因运行日志写入失败撤销。

这一版没有执行/结果回传接口，不会产生公共推理调用或改变旧容量。API Key、公共 `/api/v1` 入口、中心关联核验、任务进度/设备/结果、Docker、资源采集和 systemd 的验收分别在后续模块，不把当前 Windows 回环上报验证当作这些能力完成。
