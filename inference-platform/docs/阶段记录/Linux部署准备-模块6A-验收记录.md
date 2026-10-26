# 模块6A：Linux部署准备验收记录

本轮用户要求继续下一部分，按模块推进，仅完成6A本地准备与隔离验证。起步保护快照：`2026-10-21T22:03:17.832503+08:00`；开发侧交付登记：`2026-10-21T22:30:51.823620+08:00`。**待用户验收，真实Linux部署未完成。** 原阶段用户验收不自动通过。

## 实现与边界

- 新增显式部署Schema/Config/Service/Modules，三级目录、绝对导入，Service不导入应用工厂。
- `plan` 离线生成3份systemd用户单元、回环nginx配置、review.sh和操作说明；生成不安装或启停。
- `init` 只允许全新独立目录，分别初始化中心0020与Node0004，随机生成分角色私有env，不创建模型、业务登记或应用Key，不迁移旧任务。
- `inspect` 只读检查配置/版本/权限/依赖/磁盘；端口仅临时绑定后关闭。数据库缺失或版本不符不自动修复，不打印原配置或凭据。
- Linux WSGI入口固定单worker/gthread、不preload/热重载，应用及Node后台线程在worker内创建，退出清理。Node和中心进程所有权独立；缺库拒绝启动。保留原开发入口。
- 启动不继承残留的INFERENCE/NODE/DEPLOY配置，不读日常服务选用文件；执行器继续原调度内核。新6A环境仅回环，Node真实业务/Docker关闭。
- 中转依赖清单不包含本机模型引擎，Linux依赖解析与安装待服务器验证；本轮没有安装或下载。
- 源码打包白名单补入独立Node迁移和examples，仍排除runtime、实际env、模型和依赖。不执行上传或服务器安装。

使用方式见[6A说明](../Linux部署准备-模块6A.md)。CPU/GPU内存显存由业务侧管理；中心Redis、调度、共享额度、权威结果和原日志监控规则不改变。

## 最终测试

| 批次 | 实际结果 | 证据 |
|---|---|---|
| 最终部署/结构/新运行工厂回环HTTP | **52通过、1跳过，52.17秒** | `runtime/verification/linux-module6a/delivery-tests.xml` |
| 原源码打包/传输边界、公共启动、Node、协议CLI、Redis隔离回归 | **128通过、1跳过，66.31秒** | `runtime/verification/linux-module6a/compatibility.xml` |
| 最终白名单精简后源码包专项复验 | **13通过，4.42秒** | `runtime/verification/linux-module6a/source-final.xml` |

各批分列，不累计中间重复测试；13项源码包复验与128项兼容批次有重复。第一批跳过的是Windows当前用户不能创建目录符号链接；真实Linux链接权限处理待验。第二批跳过的是未提供独立真实Redis，不能记作Lua/重启通过。未运行完整后端全量、前端或浏览器测试，本轮没有前端代码改动。

重点覆盖：新库与凭据隔离、旧路径回退拒绝、重复初始化不覆盖、跨组件配置/重复变量/插值拒绝、硬链接env拒绝、环境变量恢复、不同迁移版本表读取、丢库不创建、版本不符只读失败、端口占用、Windows启动拒绝、systemd资料不执行、Node/中心重复进程所有权、退出再启动、采集线程启动失败清理、空队列执行器、Gunicorn hook集成契约。

新增HTTP测试使用真正回环HTTP和真实应用工厂，WSGI服务器为测试用Werkzeug：外部通过Node查询中心服务目录、Key校验、Node观察接口；关闭本测试的中心HTTP后公共调用503，本机Node状态仍200，Node无推理执行记录、模型目录为空。**这不是Gunicorn/systemd在Linux运行通过。** Gunicorn集成本身使用BaseApplication替身核验参数与清理hook，实机仍待验。

## 实际本机演练与保护复核

- `runtime/deployment-validation/module6a-local` 首次独立初始化成功，中心0020、Node0004；没有日常资料迁移。
- 实际CLI生成 `runtime/verification/linux-module6a/linux-plan` 七个文件，未安装或启动单元，文件没有真实密钥。
- 一次本地源码包快照包含744文件/5,490,703字节，5份独立Node迁移资料及Linux示例/入口/依赖文件均在包内，实际env与runtime排除；见`source-package.json`。没有上传。该包仅为当时验证快照，后续文档有追加，未来实际传输必须重新打包，不能称最新源码包。
- 中心和Node便携只读检查均通过；要求Linux时明确报告 `LINUX_REQUIRED`、`GUNICORN_PIN_REQUIRED`，没有开发服务器回退。本机Windows/Python3.11.16，没有Gunicorn、redis客户端及systemctl/nginx/redis-server/docker工具。WSL不可运行，没有安装。
- 初次只读检查把Node版本表误按中心表读取，修正为 `host_node_alembic_versions` 后实际Node检查及测试通过。
- 首次pytest因系统临时目录权限产生30项setup错误、18项通过；改用项目独立basetemp，不修改系统权限。第二批47通过/1失败为测试用了不存在的Node health路径，改为鉴权 `/node/v1/status`；最终批已通过。早期批次XML保留，不充当最终通过证据。
- `2026-10-21T22:33:51.734839+08:00` 最终复核：日常 **0014、26表各行数/内容哈希一致、42份上传字节一致、.env哈希一致**。655份原源码/测试/迁移/依赖/前端/脚本中，仅本轮有意调整的源码打包白名单发生变化；新增文件另列。证据 `before.json`、`delivery.json`。
- 成功的只读进程复核见 `process-check.json`：没有本轮部署入口/执行器/Node残留进程，15000/18020/15173/18080无监听；5000/5173有日常API/Vite进程，本轮未操作它们。初次沙箱CIM读取失败后仅用获准只读复核，不把失败查询的空结果当作无进程。
- Git差异检查保留旧文件CRLF格式，用 `git -c core.whitespace=cr-at-eol diff --check` 通过；不重写旧文档换行。

所有本地证据位于 `runtime/verification/linux-module6a/`，runtime不进入源码包。

## 未验与下一部分

真实Linux安装/WSGI/systemd开机启动/nginx/SSE、真实Redis联网/Lua/重启、真实Docker与模型、多机网络、跨Node监控总预算、备份恢复及旧任务迁移仍待对应环境与后续阶段。没有访问真实模型、发送推理请求、启停真实容器、SSH上传/服务器部署、推送Git或Multica网络读写。

本地计划v2.22，远端仍v2.9；原写入/核对时间 `2026-10-19T14:37:17+08:00` / `14:37:27+08:00` 保留，仅新增本地待同步事件。下一部分为6B真实Redis专项；本部分讲解后暂停，不自动开始。
