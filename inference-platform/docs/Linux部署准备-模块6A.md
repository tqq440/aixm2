# Linux 部署准备：模块 6A

> v2.26增量：新增6A独立环境的成套离线备份与隔离恢复，源需已停止控制面，恢复副本禁止运行；不代表日常迁移或Linux恢复验收。见[说明](本地成套备份与隔离恢复.md)。

> v2.25增量：新建隔离中心库更新为0021，生成独立查询预算凭据；Node仍0004。旧目录不会自动升级；本轮没有Linux实际部署。见[统一限流](跨Node监控统一限流.md)。下文0020为6A原交付快照。

本部分先把 Linux 的启动入口、配置和检查工具准备好。**本地开发验证完成不等于 Linux 已经部署成功。** 当前电脑为 Windows，没有可运行的 WSL、Gunicorn、Redis 服务或 Docker；不在本轮安装它们，不连接或改动实验室服务器。

中心继续负责 Redis 排队、统一调度、并发额度、应用鉴权和权威结果；Node 独立运行，不增加推理队列。CPU/GPU 内存、显存管理由业务侧负责。本轮没有改变这些规则。

## 交付内容

| 内容 | 作用 |
|---|---|
| `backend.modules.deployment.linux_cli plan` | 离线生成本人 systemd 单元、回环 nginx 配置、检查脚本和操作说明；不安装单元或启动进程 |
| `init` | 创建全新独立目录、独立中心/Node 数据库、分角色随机凭据和配置；不读取日常 `.env`，不迁移旧库 |
| `inspect` | 检查配置、数据路径、权限、数据库版本、依赖、磁盘和本机端口；不执行上游 HTTP 查询 |
| `serve center` / `serve node` | Linux Gunicorn 候选运行入口；启动前检查，不自动迁移或改用 Flask 开发服务器 |
| `executor` | 带显式配置和凭据启动已有中心 Node 执行器，不复制调度规则 |
| `requirements/relay.txt`、`linux.txt` | 中转运行依赖与 Linux WSGI 依赖，排除 Whisper、ONNX 等本机模型引擎；未安装，Linux 依赖解析待验证 |

新旧开发入口保留。新运行入口目前限定独立验证环境、回环监听、Node Docker/真实业务关闭，不作为日常数据迁移工具。未来开放真实执行前仍需对应阶段验证与明确切换。

## 1. 在当前电脑生成部署资料

在 `inference-platform` 目录，用现有 Python 执行：

```powershell
python -m backend.modules.deployment.linux_cli plan --spec examples/linux/module6a.json --output runtime/verification/linux-module6a/linux-plan
```

输出目录必须不存在，父目录必须已存在；相同位置再次执行会拒绝覆盖。样例指向既有计划路径 `/home/swufe/FIC-Project/tyb/inference-platform`，**路径登记不证明远端已存在或可登录**。部署名、Linux 路径、四个端口和线程数都经过校验，路径不能包含空白、变量、命令片段或穿越片段。

输出三份单元：`inference-module6a-center.service`、`inference-module6a-executor.service`、`inference-module6a-node.service`。还有 `plan.json`、`nginx-validation.conf`、`review.sh`、`OPERATIONS.md`。生成资料没有真实密钥。

默认端口：中心 `15000`、Node `18020`、本机管理静态站点 `15173`、Node 公共代理 `18080`，均与日常 `5000/5173` 分开。前两个入口和 nginx 验证代理均只绑定回环，校园网开放与 TLS 尚未实现真实部署验收。

## 2. 创建独立验证环境

Linux 服务器后续获准准备时，先使用专属 Python 3.11 环境解析 `requirements/linux.txt`，检查安装结果，不直接升级共享 Python。没有下载或启动模型的命令。Redis 客户端与服务的准备及验收留 6B。

```sh
cd /home/swufe/FIC-Project/tyb/inference-platform
install -d -m 700 runtime/deployment-validation
.venv/bin/python -m backend.modules.deployment.linux_cli init \
  --root /home/swufe/FIC-Project/tyb/inference-platform/runtime/deployment-validation/module6a \
  --name module6a --center-port 15000 --node-port 18020
```

也可在 Windows 用自己的显式绝对路径建立新验证目录。程序只允许全新目录，不能指向项目、其父目录、日常 runtime 子目录或已有验证环境。失败时保留本次未完成的目录，不递归删除、不重试迁移；检查原因后换一个新目录。`READY` 文件表示两个新库初始化完成，不表示 Redis、模型或 Linux 已通过。

目录布局：

```text
module6a/
  config/center.env        # 真实随机凭据，禁止提交或粘贴到日志
  config/node.env
  db/center.sqlite3       # 0020
  db/node.sqlite3         # 独立 Node 0004
  logs/center/
  logs/node/
  uploads/
  models/                 # 空目录，不下载或加载模型
  tmp/
  deployment-validation.json
  READY
```

配置中明确指定 DB、日志、上传和模型目录，不使用进程中残留的 `INFERENCE_*` / `NODE_*` 配置，不读取日常服务选用文件。Linux 的 env 文件要求当前用户拥有、权限不向组或其他用户开放；工具按 `0600` 写 env、按 `0700` 创建目录。Windows 上这些 mode 位不等同于 NTFS ACL 验收。

凭据彼此独立：管理员、观察者、业务上报、中心到目标 Node 执行、入口 Node 到中心；只有对应通信双方共享相应凭据。中心 env 中的 `DEPLOY_NODE_EXECUTOR_TOKEN` 供以后目标绑定引用。应用 API Key 仍由中心管理页面/API 单独签发，初始化不自动创建应用、服务或任务。

## 3. 只读检查与启动

```sh
.venv/bin/python -m backend.modules.deployment.linux_cli \
  --env-file runtime/deployment-validation/module6a/config/center.env inspect --require-linux
.venv/bin/python -m backend.modules.deployment.linux_cli \
  --env-file runtime/deployment-validation/module6a/config/node.env inspect --require-linux
```

成功返回 0，发现问题返回 2。检查不会修复依赖、创建丢失的库或升级版本；SQLite 使用只读连接。端口检查只临时尝试绑定本机端口并立即关闭，不启动 HTTP 服务，不保证随后启动时端口仍空闲。输出不包含凭据或完整配置。`notVerified` 明确列出系统启动、Redis、容器、模型和网络仍待验证。

`inspect` 不加 `--require-linux` 可用于 Windows 的独立库检查；这时 `ok=true` 仅说明便携检查通过，不能写成 Linux 通过。实际 `serve` / `executor` 自动要求 Linux，WSGI 还要求指定 Gunicorn 版本；不满足时拒绝启动。

Linux 真实启动的候选命令：

```sh
.venv/bin/python -m backend.modules.deployment.linux_cli --env-file runtime/deployment-validation/module6a/config/center.env serve center
.venv/bin/python -m backend.modules.deployment.linux_cli --env-file runtime/deployment-validation/module6a/config/node.env serve node
.venv/bin/python -m backend.modules.deployment.linux_cli --env-file runtime/deployment-validation/module6a/config/center.env executor
```

每条运行一个进程角色，实际运维使用生成的 systemd 单元。中心 API 与 Node 各固定 **1 个 Gunicorn worker、默认 16 个线程**，不 preload、不热重载、不定期自动换 worker、不启用额外 Gunicorn 控制 socket。应用、SQLite 连接和 Node 后台采集均在 worker 启动后创建，退出时释放。Node 独立数据库已有所有权锁，中心 API 增加专属运行锁；中心执行器继续使用原锁。

线程数只是 HTTP 请求处理能力，不表示允许同时派发 16 个推理任务。真正的并发仍由应用、目标和共享资源组的 SQLite 事务额度控制。

不要发送 Gunicorn HUP/USR2/TTIN 做热升级或增加 worker。当前单库所有权不支持滚动启动；升级应显式停止、核对，再重新启动。关闭中心不会连带停止 Node 或业务容器；Node 单独退出也不停止业务容器。

## 4. systemd、静态页面与日志

生成的是本人 `systemctl --user` 单元，运行身份不需要 root，不通过命令行传密钥。不会自动复制到 `~/.config/systemd/user/`，也不执行 enable/start。未来获准部署后先审阅，再安装本人本批单元、daemon-reload、分别启动并查看状态。开机启动还需 enable 和实际主机 user lingering 条件核实；本轮未执行。

单元设置 `UMask=0077`、失败重启退避和启动频率限制。配置检查退出 2 时不自动反复重启。Gunicorn 等待退出 930 秒，systemd 等待 960 秒，给现有有界调用和清理留出时间；达到进程退出期限仍不能视作上游已停止，也不能据此释放未知调用的额度。

nginx 模板是**回环验证配置**，需已有 `frontend/dist`、权限和 nginx 检查通过。管理入口代理中心 `/api/` 并提供静态页面；公共入口只代理 Node 的 `/api/v1/`，其他路径 404。流式代理关闭缓冲，设置有界读写等待。不得直接覆盖服务器现有 nginx 配置。真实语法、静态资源权限、SSE 和校园网 TLS 都待 Linux 环境验证。

运行日志复用已有 JSON 单行滚动日志，各进程使用带 PID 的文件名，单文件 2 MiB、3 个备份；center 与 Node 分目录。systemd stdout/stderr 进入 journal，journal 保留策略由主机管理员核实。关闭 nginx access log，避免完整 URL/查询串进入访问日志。任务事件、上游快照仍保存在各自数据库中，不随进程退出清理。

## 5. 后续验收与切换

6A 不迁移日常 `0014` 数据。真实 Redis Streams/Lua/重启、跨主机链路、跨 Node 总查询预算、真实 ASR/YOLO/LLM、备份恢复和旧任务迁移仍在后续模块执行；本轮不把这些记为通过。切换前仍需暂停新受理、备份、核对在途与未知占位，避免新旧链路执行同一任务。

源码打包白名单已补入独立 `node-migrations/` 和 `examples/`，避免未来首次源码包缺 Node 初始化资料；runtime、实际 env、依赖、模型仍排除。只修改本地打包能力，不执行上传。

实现所依据的官方资料：[Gunicorn 26.2.0 元数据](https://pypi.org/project/gunicorn/26.2.0/)、[BaseApplication](https://github.com/benoitc/gunicorn/blob/26.2.0/gunicorn/app/base.py)、[配置与退出 hook](https://github.com/benoitc/gunicorn/blob/26.2.0/gunicorn/config.py)、[systemd 服务](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)。版本固定与本地契约验证不能代替目标 Linux 的安装及启动验收。
