# GitNexus 函数索引说明

2026-10-26，本轮按用户要求下载官方 GitNexus，并对当前并发平台做本地静态分析。入口是平台根目录的 [函数索引表.md](../函数索引表.md)，也提供 [CSV](../函数索引表.csv) 和 [JSON](../函数索引表.json)。

## 覆盖与结果

索引包含当前工作树中的后端、React/TypeScript 前端、迁移、脚本、示例及测试，包括尚未提交的代码。Python 578 文件、TypeScript/TSX 99 文件、JavaScript/MJS 2 文件、PowerShell 8 文件、Shell 1 文件、Mako 1 文件、CSS 7 文件、HTML 1 文件、SVG 1 文件，共 698 个代码文件；没有函数的文件也列在覆盖清单中。Shell 脚本没有函数声明，HTML 没有内联脚本，Mako 的两条声明明确标作模板。

共 5,606 条索引：4,023 个具名函数/方法，1,581 个匿名回调/lambda，2 个模板声明。函数所在文件共 525 个。Python AST、GitNexus 安装的 Tree-sitter 与 PowerShell 内置 AST 校核均无语法解析错误。索引中的行号均从1开始，GitNexus 原始 JSON 的行号从0开始，两者已校准。

GitNexus 1.6.12 图数据库记录 813 个文件节点、11,664 个节点、28,351 条关系、418 条执行流。工具报告390个clusters，实际持久图Community节点329个，分别保留口径。另导出 7,033 条静态 CALLS 关系与 3,005 条 IMPORTS 关系。813是工具扫描的文件节点数（含文档/配置），698是逐文件校核的代码文件数，二者口径不同。两次独立CLI查询核对全部节点与关系数量均一致，证据见 `GitNexus查询验证.json`。

GitNexus 原始 Function/Method 节点4,086个，4,080个已匹配实现函数或回调；剩余6个是 `usePlatform.ts` 的 `PlatformApi` 接口方法签名，独立保存在 `GitNexus未匹配符号.json`，不把类型签名算作实现函数。其余1,526条索引由AST或模板扫描补全；没有图身份的条目，其调用计数用空值而非0表示。React `useCallback`、`memo`、`forwardRef` 回调保留赋值名称，便于查找 `refresh` 等函数。

## 文件

| 文件 | 内容 |
| --- | --- |
| `../函数索引表.md` | 按文件排列的完整函数表，含类/外层函数限定名、行号、签名、文档首行、静态调用数量与识别来源 |
| `../函数索引表.csv` | UTF-8 BOM，适合按分类、语言、函数名、位置筛选 |
| `../函数索引表.json` | 完整签名、调用符号ID、图身份、源文件SHA-256和分析摘要 |
| `调用关系.csv/json` | CALLS边、两端索引编号（可空）、GitNexus符号ID、置信度与解析依据 |
| `文件依赖关系.json` | IMPORTS静态依赖边 |
| `文件覆盖清单.csv/json` | 全部698个代码文件，包括0函数文件、解析情况和SHA-256 |
| `GitNexus原始函数符号.json` | 工具直接输出的4,086个Function/Method节点，原始行号从0开始 |
| `GitNexus未匹配符号.json` | 6个接口方法签名，供核查 |
| `源码指纹.json` | 扫描时的平台文件指纹；不含本地.env；交付进度文档在索引完成后追加 |
| `分析摘要.json` | 统计、范围和限制 |
| `索引校验结果.json` | 位置、覆盖、数量、身份和源码保持检查结果 |
| `GitNexus查询验证.json` | 恢复后两次实际CLI图查询的逐类节点/关系数量及一致性核对 |
| [收尾复核说明](收尾复核说明.md) / [收尾复核结果](收尾复核结果.json) | 当前文件覆盖、调用边引用和计数、全部本地文档链接的追加复核 |

原生图数据库位于 `../.gitnexus/`，被自身 `.gitignore` 忽略。没有改写项目 `AGENTS.md` 或安装编辑器钩子/MCP配置。

## 工具位置与来源

以下路径以工作区 `D:\1 研究生\aixm` 为基准：

- GitHub源码：`tools/GitNexus-source/GitNexus-main/`。
- GitHub原始压缩包：`tools/GitNexus-main.zip`，SHA-256：`6ce0ea983e864255cc1eaf93b42514a857c657ef67e36dfe413823d7e7d87a1e`。
- 分析CLI：`tools/gitnexus-cli/node_modules/gitnexus/`，版本1.6.12，来自npm官方源；安装完整性记录在 `tools/gitnexus-cli/package-lock.json`。
- 本轮使用已发布CLI；源码压缩包是GitHub main的下载快照，两者不宣称为同一提交。
- 导出/语法校核/校验脚本：`tools/gitnexus-index/`。
- 本轮GitNexus注册资料：`tools/gitnexus-home/`，未使用用户全局注册文件。
- 原始分析日志：`tools/gitnexus-index/analyze.log`。首次git clone网络等待后已终止，成功交付的GitHub下载是上述源码压缩包。

## 更新索引

在工作区根目录用PowerShell执行以下命令。分析工具及其依赖已独立安装，平台依赖不需要更改。刷新时重新读取当前代码，因此统计随代码变化。

```powershell
Set-Location -LiteralPath 'D:\1 研究生\aixm'
$env:GITNEXUS_HOME = Join-Path (Get-Location) 'tools/gitnexus-home'
$env:GITNEXUS_MEMORY = 'off'
$env:GITNEXUS_LBUG_BUFFER_POOL_SIZE = '268435456'
python -X utf8 tools/gitnexus-index/snapshot.py
node --max-old-space-size=4096 tools/gitnexus-cli/node_modules/gitnexus/dist/cli/index.js analyze inference-platform --skip-git --index-only --skip-fts --workers 2 --max-file-size 32768 --name inference-platform
node tools/gitnexus-index/extract-js.mjs
& tools/gitnexus-index/extract-powershell.ps1
node tools/gitnexus-index/export-graph.mjs
node tools/gitnexus-index/checkpoint-index.mjs
python -X utf8 tools/gitnexus-index/build-index.py
python -X utf8 tools/gitnexus-index/verify-index.py
```

应逐条确认命令成功后继续下一条。`--skip-git` 将子目录本身作为索引根，不向上分析工作区归档；`--index-only` 不注入项目协作说明或skills。`GITNEXUS_MEMORY=off` 避免本机默认堆调整的重复启动等待，显式使用4GiB堆上限。`--skip-fts` 避免另行加载全文搜索扩展；函数表与Cypher图查询可用，未生成向量嵌入或下载模型。

`checkpoint-index.mjs` 只打开生成的 `.gitnexus/lbug`，完成检查点并关闭，处理本轮Windows出现的shadow恢复页问题；不打开平台业务SQLite库。原始小WAL已由GitNexus自身恢复守卫隔离留存。查询复验成功；FTS未安装提示不影响Cypher图查询，不需要为此安装扩展。

查询示例（保留上面的环境变量）：

```powershell
node tools/gitnexus-cli/node_modules/gitnexus/dist/cli/index.js cypher 'MATCH (f:Function) RETURN count(f) AS functions' -r inference-platform
node tools/gitnexus-cli/node_modules/gitnexus/dist/cli/index.js cypher 'MATCH (f:Function) WHERE f.name = "create_app" RETURN f.name, f.filePath, f.startLine' -r inference-platform
```

## 解读限制

本轮只静态读取源码，未执行平台模块、业务测试、迁移或模型推理。函数表不是业务验收报告。无源码文档的函数不编造用途。

调用边是静态推断，动态派发、反射、框架注入、跨语言字段、字符串协议和HTTP远端调用可能缺失或误连；应同时查看 `confidence` 与 `reason`。0条调用不能证明未使用，AST补全条目的空调用数量代表图关系未知。

工具明确报告执行流预算截断：961个候选入口中761个未进入排名，906个被调分支被省略，28条路径受入口预算限制，1条达到深度限制。因此418条流程不是完整运行路径列表。另有205处跨语言属性访问没有链接，不能判断其未使用。

日常 `runtime/`、数据库/上传/日志、环境秘密、依赖、构建缓存与工作区旧版归档没有进入源码索引；代码没有上传到外部分析服务。本轮仅下载工具及其分析依赖。
