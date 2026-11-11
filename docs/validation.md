# 验收状态

本文件持续记录实际执行结果，不将实现完成、TEST 通过、真实模型通过、人审通过混为一谈。

| 检查 | 当前结果 | 范围 |
|---|---|---|
| 最终整仓自动化 | **上传前重跑 637 项通过，16.05s；Ruff、两份 JS 语法检查通过** | 源码外 cwd/HOME/tmp，Node 从 PATH 定位，无跳过；运行前后源文件指纹一致，无真实模型 |
| 合成目录与评分 | 137 项测试通过 | 严格类型、24 小时时效、运费、未知硬规格、稳定整数评分 |
| Provider / Runner / 恢复 / tracing | 221 项测试通过 | MockTransport/TEST，无真实模型 HTTP |
| 第一轮后端集成 | 388 项测试通过，3.93s | 上述测试加 Actions / Tools；不含尚未完成的 API/CLI/页面 |
| 追加 schema / context 回归 | 55 项测试通过，2.39s | schema 根引用、失效推荐状态、Tools 和 Runner；与上表重叠，不能相加 |
| 中文页面、API、CLI | 已实现；专项 72 项通过，最终独立复跑 72 项通过（3.34s） | 三项 UI 状态修复已复核，运行前后四文件哈希一致；没有未解决的已确认入口/UI问题 |
| 冻结 30 场景与预选 8 核心 | 修后正式 CLI TEST **30/30、8/8**；验收器 50 项测试与 23 个独立负例复验通过 | 30 项含 19 TEST 策略 + 11 程序核验；0 真实模型调用、人审 pending |
| 真实 DOM 闭环 | **最终两轮业务闭环通过；严格整体验收两轮均 failed** | 同一冻结源码，各 5 次 Chrome 后台尝试被阻断、转发 0；页面和 Python 出站计数均为 0，不报告零外部尝试 |
| 源码外锁定安装 | **修复后第二次实际执行通过，exit 0 / ok=true；产物补核通过** | sdist→wheel、新 venv 非 editable、50 项 runtime 精确匹配；CLI help/check、ASGI/资源通过，不是重新解析依赖，也未运行 CLI 初始化或 server |
| 独立领域审查 | 212 项检查通过，1.77s | 189 原领域测试 + 23 独立探针；已复现问题修复，限定领域范围，不等于全系统无缺陷 |
| 8 核心真实模型场景 | **未运行** | 没有显式确认的数字 loopback 端点和 model ID；不扫描、不启动模型、不回退云端 |
| 人工解释审查 | **未完成** | 不以程序自填或模型自评代替人工判断 |

## 上传前核对

发布检查发现两份测试文件中三处 Node 路径写死在本机用户目录。已改为从 `PATH` 查找；Node 不存在时相关测试明确显示 skipped，不把跳过当通过。随后在源码外 cwd/HOME/tmp、Node 24 可用的环境重跑：**637 passed in 16.05s，无跳过**，Ruff 和两份 JS 语法检查通过。报告：`/private/tmp/shopping-git-upload-n78gp5c9/prepush-check-ol8e7wx2/result.json`，执行前后文件指纹稳定。

本次仅调整测试的 Node 定位和 README/运行/验收说明，应用代码、浏览器脚本、验收脚本、冻结场景及依赖锁文件未变；没有重跑 Chrome、安装或真实模型。之前安装产物仍对应当时快照，不包含这次测试路径与文档调整。浏览器严格失败、真实模型未运行和人审未完成的结论保持不变。

## 开发期间发现并修复的问题

- 最初 Actions / Tools 测试因模块缺失失败；这仅是初始 RED，不是业务验证通过。
- 针对证据链与事实重算新增测试后，出现 3 个实际断言失败：祖先证据改变未贯穿失效、推荐未重新核对评分结果、无解答复只信空列表。已改为递归核对引用，并从当前权威目录重算搜索、比较和评估；后端集成通过。
- 结构化 schema 的嵌套 `$defs` 与根 `$ref` 不一致；测试先失败，再将共享定义提升到根节点，引用完整性通过。真实服务对 JSON Schema 子集的兼容性仍须实测。
- `read_context` 曾只给出推荐存储状态，没有说明目录变更导致的失效。现独立输出 `display_valid`，且新读取的上下文可明确描述历史无效推荐，不将旧推荐重新变为有效。
- 一个无解测试误将“预算不会自动提高”中的否定短语判成违规。保留后端否定说明，改为检查完整虚假承诺不被发布，以及已确认预算未变。

- 独立领域复核发现正常工具路径可借 `ask_shopper` 的旧模板，无证据发布无解结论；主线程新增测试先失败，再将该模板改成中性询问，无解事实只由有真实空查询引用的 `respond` 发布。
- 复核的本地故障注入还发现推荐派生字段未全面复核。新增共享 `recommendation.py` 渲染器，创建与读取均校验原始选择、事实、偏好、评分版本、比较方向、非最高分理由与声明。此类故障注入不是公开 API 可任意改数据库的证明。最新相关 71 项测试通过，2.56s，Ruff 通过。随后独立复核 212 项通过；另外补充搜索/比较/评估内容严格 JSON 比较，避免 Python 将等值 float/int、bool/int 判为相同；这些是本地完整性故障注入验证。

独立领域复现材料：`/tmp/shopping-domain-review.L0yg045F/test_review_probes.py`。该复核未重审作者本人编写的运行时，也不涵盖 API、页面、浏览器或真实模型。

### 后续运行时与入口复核

- 正常在途调用曾只显示零已知 token 和零 unknown，未说明仍待结算。新增 `pending_calls` 与 `usage_complete`，预算门禁原本已阻止并发继续调用；这不是永久漏账。
- 提交消息与关闭线程池之间存在竞态，会留下已保存但未调度的 queued run。现用同一生命周期锁保护提交边界，关闭等待已接收任务排空；同时覆盖关闭取消排队 future 导致 active_run 留存的情形。
- HTTP 429 带可靠用量时，旧 Runner 会反馈后反复请求到预算上限。现除可纠正的动作格式错误外结束本轮；不把 Provider 单次请求与 Runner 决策循环混称为传输重试。
- 合法数据目录名含 `?` / `#` / `%` 时，SQLite URI 曾误解析；其中 `#` 可导致自有测试目录的邻接位置产生截断文件。已用 `Path.as_uri()` 编码，保持路径字面含义和 `mode=rw`。这是本地路径处理缺陷，不是远程可写数据库的证明。
- CLI 的端口 80 配置曾与浏览器默认端口规范化冲突，导致正常请求 400；配置侧已规范化，入站 Host/Origin 仍严格校验。

主线程本批先执行 **12 failed / 30 passed**，修复后相关 **42 passed，1.96s**，Ruff 通过。运行时另用 **12 项独立探针全部通过，1.22s**，验证在途/未知/未发送用量、HTTP 429 与格式反馈、六任务排空和两个同时关闭；先前两个确认缺陷均关闭。入口复核 **104 项通过，3.86s**，涵盖特殊路径、默认端口和工厂依赖误标修复。之后页面三项状态问题已先失败再修复，最终独立复跑 API/CLI **72 项通过，3.34s**（含真实 JS 事件处理器的三个 Node harness 场景）；它验证编辑起点版本、显式只读冲突恢复和读取失败后不假称卡片有效，不代替真实浏览器。上述子集不能与旧计数相加。

API 工厂曾允许同时传入不同 `provider` 与 `service`，导致 TEST 执行与模式标签不一致。现拒绝双重依赖，并校验外部 Service 与 Store 对应。它是本地 Python 集成配置问题，不是未经授权的远程模式切换。

## 源码外锁定安装

最终实际安装报告：`/private/tmp/shopping-install-mwrpjmsu/report/result.json`，第二次执行 **exit 0 / ok=true**。独立产物核对报告：`/private/tmp/shopping-final-install-review-tko05ee1/final-artifact-verification.json`，**ok=true**。

- 使用已有离线缓存，构建 sdist，再从 sdist 构建 wheel；安装到源码外全新 venv，cwd/HOME 均独立，非 editable。50 项 runtime 版本逐项匹配 `uv.lock`，依赖一致性检查通过。
- 已安装 CLI 的 help、`init-demo --help`、数据 `check`，以及 ASGI health/me、HTML/JS/CSS 通过。合成数据由 Store API 初始化，令牌只在内存；本项未执行 CLI 初始化或启动 HTTP server，也未测试无缓存的新依赖解析、真实模型或浏览器。
- wheel 的 17 个包文件与冻结源码字节一致；sdist 的 47 个常规文件清单通过，未包含运行产物、凭据文件或父仓材料。安装线程记录的 44 项前后指纹一致；初筛遗漏 `.gitignore` 和 `browser_smoke.mjs`，原初筛报告保留，补核将它们分别绑定当前源码、sdist 字节及主线程独立安装前 SHA-256，不伪称原 44 项已经覆盖。
- 自建子进程已停止、工作目录已删除；报告目录 0700、报告与产物 0600。安装通过不能替代浏览器的严格失败或未运行的真实模型验收。

产物位于安装报告同目录：

| 文件 | SHA-256 |
|---|---|
| `shopping_decision_agent-0.1.0.tar.gz` | `e4a263e595cfaca454d925becae51dda74146000220660991dcddebf5cf08266` |
| `shopping_decision_agent-0.1.0-py3-none-any.whl` | `46baba3f7c2332034405314734ee55d8391c5e79e391fbd16d5be4087f84c3b9` |
| 对应 `uv.lock` | `a0c588c872bc4ba9fc368a18ea9486591c5fe04e16c774ece8ed2395eae98aa3` |

产物包含构建时冻结的 README 和五份 docs；本次追加的最终验收说明不在已构建产物中，运行代码未因文档收尾改变。首轮旧安装报告 `/private/tmp/shopping-install-hd4x_kwt/report/result.json` 只证明当时快照，不用于代替最终结果。

## 最终真实 DOM 记录

浏览器脚本独立复核曾发现三个缺口：Python 防线未覆盖 DNS/UDP 和 audit 分支、没有完整源码快照绑定、代理启动异步失败可能卡住清理。这些是验收防线缺口，不是观察到实际资料外发。已补失败测试并修复为项目导入前共用 socket audit hook、私有源码副本及逐文件哈希、监听 error/timeout/cancel 明确终结与清理。

最终脚本 15 项离线测试通过；独立 14 项 Python 模拟检查、Node VM 的监听五终态/拒绝代理/清理检查通过，源码副本绑定另经静态复核，原三个确认问题均关闭。独立临时复验脚本位于 `/tmp/shopping-browser-review.KkyLHv/`，输出留在会话工具记录中，不冒称持久化浏览器验收报告。这些模拟与静态检查不是 OS 级全网络安全证明。

冻结脚本和 UI 后，保持原配置完成最后两轮，不再调整 Chrome flags/profile 或反复重跑：

- 第一轮：`/private/tmp/shopping-browser-CyeoVF/report/result.json`。
- 第二轮：`/private/tmp/shopping-browser-RsQlLK/report/result.json`。
- 两轮脚本 SHA-256 均为 `64a5f7fcf5de0d4cc1c855a8914ad864d84dd8431a620687041561ba142808bc`；17 文件 manifest 均为 `d351ffd1c3e79d239294d2f450e4fa87db6b20f2198fc0adf7bf97fb34ea61d0`，执行方式 `owned_private_copy`，分别使用全新合成会话。

两轮 **`business_flow_passed=true`**：真实鼠标/键盘完成登录、新会话、6000 元临时查询、精确草案确认、再次发消息比较推荐、完整 9 字段表单改为 100 元、旧推荐明确失效、当前空查询支持无解、刷新后重新登录和选会话。确认与修改偏好不自动发模型请求；刷新前后各为 **8→8 次 TEST 调用、1024 合成 token**，真实模型调用 0。

持久状态均核对到预算 10000 分、偏好 revision 1→2、session version 9→11、旧推荐 v1→v2 且无效；示例商品总价为 249000+0=249000 分，没有下单对象或下单审计。六张截图已逐张检查；无解截图可见无解文本，100 元预算由 DOM 和持久状态断言覆盖，不声称它在该截图裁幅内。

两轮严格结果均为 **`ok=false / terminal=failed`**，失败项 `network / external_request_attempted`：每轮 5 次 Chrome 后台连接尝试（`google_accounts` 4、`google_www` 1），全部由拒绝代理返回 403，转发数 0。页面外部请求、Python 服务和审计出站计数均为 0。不能将被阻断的尝试改记为零，也不能称两轮整体验收成功。

两轮所有自建子进程 exit 0，`children_stopped=true`、`owned_work_removed=true`，工作目录实际已删除；报告目录 0700、文件 0600。人审仍为 `pending`。

历史报告 `/private/tmp/shopping-browser-cELGMC/report/result.json` 和 `/private/tmp/shopping-browser-JxMCvu/report/result.json` 同样业务通过、严格失败，但不覆盖后来补齐的脚本防线；最终结论以上述两轮冻结快照记录为准。

## 冻结场景

`evals/cases.json` SHA-256：`021a00b0f6128266b6aee3ee2732c44a32a164354b852d08657b392c28124091`。

预选真实模型核心：`reg-01-office`、`reg-10-shipping`、`reg-11-no-match`、`change-01-budget`、`change-03-proposal`、`change-05-preference`、`change-06-catalog`、`guard-05-message`。

冻结前调整过核心选择，避免把仅程序注入的幂等/恢复用例算作真实模型核心。冻结后不得根据 TEST 或模型成绩替换场景。验收器可以修复，但需重跑并说明；期望条件不进入模型请求。

首轮 CLI 报告位于 `/private/tmp/shopping-acceptance-all-fingerprinted-7b2e1/report.json` 与 `/private/tmp/shopping-acceptance-core-fingerprinted-7b2e1/report.json`：分别得到 30/30 和 8/8，真实模型调用均为 0、人审均 pending。全量为 89 次实际 TEST Provider 调用、90 条账本，含一条程序故障注入账本；核心为 37 次 TEST 调用。这些报告对应旧验收器，不能代替修复后的结果。

随后独立负例复核确认验收器会漏检阶段性跨 owner 污染、audit/receipt 改删、调用身份与上下文绑定、伪造/删除 Observation、oracle 字段、伪回执、摘要与持久终态不一致等；输出路径与记录失败处理也有缺口。这是验收工具的问题，不等于已证明业务 API 能实施这些篡改。作者补失败测试后修复，独立复验在 30 个正常基准上验证 **23 个故障变体全部被拒绝**；记录读取失败保留 30 条场景、逐例文件、总报告和人审包（29 passed / 1 unknown），没有将 unknown 丢掉。复现材料在 `/private/tmp/shopping-acceptance-review-31aG4y5Q/`，复验报告在 `/private/tmp/shopping-acceptance-final-recheck-bibbwzsf/recheck_results.json`。

修复后正式报告：

- 全量：`/private/tmp/shopping-acceptance-all-final-9e3b7/report.json`，30 passed，failed/unknown/not_run 均为 0；19 TEST + 11 程序核验，89 次实际 TEST 调用、90 条账本、11802 合成 token。
- 核心：`/private/tmp/shopping-acceptance-core-final-9e3b7/report.json`，8 passed；37 次 TEST 调用、4736 合成 token。
- 两次真实调用/真实 token 均为 0，人审均为 pending；指纹包含 Python、HTML、JS、CSS、验收脚本和依赖文件，运行前后稳定。cases 未改，验收脚本 SHA-256 为 `ea29520e712df551e20b3afed570b3dee4243fd27546344eb5336509df4a3a3b`。
- 整仓自动化记录：`/private/tmp/shopping-final-check-nhukalil/result.json`。上述专项与整仓数量重叠，不相加。

普通测试禁止真实网络连接；浏览器测试使用本机 loopback HTTP，页面请求、Python 出站、Chrome 后台尝试分别报告，不能称“完全无网络”，也不将被阻断的外部尝试改记为零。所有 TEST token 为合成计数，不能当真实模型成本。历史/子集测试数有重叠，最终以完整重跑结果为准。
