# 本地运行

页面、API、CLI 已实现，自动化与源码外锁定安装通过，可按下列命令运行 TEST 演示。两轮真实页面业务闭环通过，但严格网络检查仍失败；真实模型与人工审查未完成。完整结果和限制见 [validation.md](validation.md)。

## 独立环境

在本工程目录执行，Python 使用 3.12：

```bash
uv sync --frozen --python python3.12
```

此命令在缓存不足时可能下载公开依赖。要求完全离线时加 `--offline --no-config --no-python-downloads`；缺缓存应停止，不自动改成联网或借用别的工程环境。

## 初始化合成数据

使用全新、位于源码目录之外的数据目录，其父目录必须已存在：

```bash
.venv/bin/shopping-advisor init-demo --data-dir /tmp/shopping-demo-data
```

初始化生成 16 款原创合成笔记本、Alice / Bob 客户与 operator 操作员。目录存在时拒绝覆盖。身份文件 `alice.token`、`bob.token`、`operator.token` 放在该数据目录，权限为 0600；命令只显示路径，不打印令牌。只在本机页面的密码框使用这些合成身份，不要上传、贴聊天或放进 URL。

## 启动 TEST 演示

```bash
.venv/bin/shopping-advisor serve --data-dir /tmp/shopping-demo-data --test-mode --port 8765
```

浏览器打开 `http://127.0.0.1:8765`，用 Alice 的合成令牌登录。TEST 页面有醒目标记；这是关键词策略，不是真实大模型。

体验流程：

1. 新建会话，发送“总预算6000元，用于办公，更看重价格。”
2. 核对偏好草案，点击明确确认。确认只保存条件，不购买、不自动继续模型。
3. 再发送“请按已确认条件推荐。”，查看比较与推荐依据。
4. 在完整偏好表单中将预算改成 **100 元** 并提交，旧推荐应显示失效。
5. 再发“重新比较”，查看无合格项及调整条件提示。预算不应自动提高。
6. 刷新只重新读取状态，不产生新模型调用。

operator 登录后只能操作合成目录价格/库存及未知调用对账，不能查看客户会话或替客户确认条件。操作员修改目录后，相关旧证据失效。

表单修改绑定开始编辑时显示的版本，不会因后台轮询自动覆盖其他窗口的新设置。遇到版本冲突，可点击“重新加载”放弃本地未提交输入并读取当前值，再重新编辑；客户偏好和 operator 商品编辑均提供此入口。读取失败时，证据卡标为“未核验”，不能继续确认；成功刷新后按服务端当前状态恢复，不把离线旧卡当有效依据。

停止服务按 `Ctrl-C`。服务停止接收新消息，等待已经接收的排队/在途任务结束后释放实例锁；如不希望某轮继续，可先在页面取消。取消不会物理终止已发送的模型 HTTP，也不退还 token，可能仍需等到返回或超时。

## 显式连接本地模型

在你已经自行运行且确认端点的 OpenAI-compatible 本地服务上：

```bash
.venv/bin/shopping-advisor serve \
  --data-dir /tmp/shopping-demo-data \
  --model-url http://127.0.0.1:8080 \
  --model your-model-id \
  --port 8765
```

`8080` 与 `your-model-id` 只是示例，不是本项目发现或验证过的服务。不要照着示例假定有模型在运行。

- `--test-mode` 与模型参数互斥；缺参数拒绝启动，不自动转 TEST。
- 模型 ID 仅允许字母数字开头以及后续字母、数字、下划线、点或连字符，最多 200 字符。
- 仅接受规范数字 loopback HTTP。模型服务必须支持 `/v1/chat/completions` 与结构化 `response_format`，并可靠返回严格整数 `usage.total_tokens`。
- 服务对根 `anyOf`、共享 `$defs` 等 JSON Schema 子集的兼容性需实际测试。不兼容就是失败，不能声称已通过。
- HTTP Provider 不使用环境代理、重定向或传输重试，不自动启动/下载模型或回退云端。HTTP 错误在用量可靠时结束本轮；发送结果未知时必须对账。只有用量可靠的动作格式错误，才在调用预算内作为 Observation 返回下一轮模型决策，不做静默修复。
- 开启旧 `LANGCHAIN_TRACING` / `LANGCHAIN_HANDLER` 时拒绝启动；请只在启动此进程时关闭相应开关，不改全局环境、不粘贴环境变量内容。

## 预算与未知恢复

默认每 run 最多 12 次模型调用、12 次工具尝试、300 秒主动耗时；每会话最多 60 次调用与 100000 token。token 与耗时只能调用前后核对，超限响应动作不执行，但已消耗用量仍记账。

未知调用阻塞会话。operator 必须依据实际记录填写用量和说明；“不知道”不能填零。对账不重放旧动作，客户需另发新消息。TEST 的 token 是合成计数，不与真实成本混用。

用量中的 `real_tokens` / `synthetic_tokens` 是已知小计；`pending_calls` 表示仍在途，`unknown_calls` 表示需要对账，`usage_complete=false` 时不能将小计当最终成本。确定未发送的连接失败不当作未知费用。`Runner.recover()` 是服务取得独占实例锁后的启动恢复步骤，不支持与活跃 `run()` 并发调用；正常页面/API不暴露该调用。

## 测试

```bash
.venv/bin/python -B -m pytest -q -p no:cacheprovider
.venv/bin/ruff check --no-cache src tests scripts
```

单测禁止真实网络。JS 相关测试从 `PATH` 查找 Node，请先使 Node 24 在当前终端可用；找不到时这些测试显示 `skipped`，不能把跳过当成通过。普通 `pytest` 通过不是模型自主能力或人工解释审查通过。

### 冻结场景

```bash
.venv/bin/python -B scripts/run_acceptance.py --test-mode --suite all
.venv/bin/python -B scripts/run_acceptance.py --test-mode --suite core
```

不指定 `--output` 时，各次自动使用全新的源码外临时目录；也可指定全新绝对目录。30 场景中，19 项运行 TEST 决策策略，11 项是程序权限/恢复等定向核验；预选 8 核心均可连接真实模型。期望条件不进入模型请求，人工审查默认 `pending`，不能用程序改成通过。

真实模型评测须明确传入自己的 `--model-url` 和 `--model`，去掉 `--test-mode`；没有两项参数会拒绝执行。项目不自动寻找模型。

### 人工解释审查

每轮输出目录的 `human-review/reviews.json` 是待审清单，`report.json` 和逐场景记录保留输入、实际调用、工具返回及最终事实卡。人工逐例核对：

1. 推荐是否回答了该用户的用途和偏好，而不是只复述最高分。
2. 总价是否包含运费，所有硬条件是否满足；未知项有没有明确说明。
3. 指标、比较方向和取舍是否忠实于实际证据，是否把合成数据说成真实测评。
4. 无解时有没有明确冲突并征求调整，而非悄悄提高预算。
5. 变化后的旧结果是否明确失效，是否虚构购买、付款或全市场最优。
6. 若运行真实模型，下一步动作是否确实来自该轮模型，并依据实际 Observation；TEST 固定策略不能作为此项证据。

由人工另行记录审查人、场景、结论及问题说明，不覆盖原始轨迹。自动检查通过、模型自评或另一 Agent 审代码都不能把这里的 `pending` 自动变为人工通过。

### 源码外安装

```bash
python3.12 -I -B scripts/check_install.py
```

该脚本使用已有 uv 离线缓存，构建 sdist，再从 sdist 构建 wheel，装入全新源码外 venv；以锁定依赖清单安装后执行 `pip check` 和逐项版本核对，不声称完成无缓存的新依赖解析。验证已安装 CLI 帮助/数据 `check`、ASGI 和静态资源；不执行 CLI `init-demo`、不启动 HTTP 服务、不连接模型。成功与失败均保留私有报告。

### 真浏览器 TEST

```bash
node scripts/browser_smoke.mjs --dry-run
node scripts/browser_smoke.mjs
```

需要本机 Chrome 与 Node 24。脚本只使用自建临时 profile、合成数据与本机端口，不使用用户浏览器资料；以实际鼠标/键盘完成推荐、改预算、失效、无解和刷新。浏览器本机 HTTP 不等于完全无网络。页面请求、Python 出站与 Chrome 后台尝试分开审计；被代理阻断的外部尝试也保留，不把它改记成零。最终状态以报告为准，当前已知失败见验收记录。
