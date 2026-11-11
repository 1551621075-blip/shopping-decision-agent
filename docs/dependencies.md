# 依赖与安装隔离

本工程使用 Python `>=3.12,<3.13`、独立 `.venv` 和 `uv.lock`，不跨项目 import，不借用其它 Agent 的虚拟环境。

当前开发环境实际版本：

| 包 | 版本 | 用途 |
|---|---|---|
| FastAPI | 0.162.2 | 本地 API |
| Uvicorn | 0.54.0 | ASGI 服务 |
| Pydantic | 2.14.3 | 严格输入和领域结构 |
| HTTPX | 0.28.1 | 显式本地模型 HTTP；测试 MockTransport |
| LangGraph | 1.2.5 | agent / guard / tools 三节点 |
| LangSmith | 0.14.17 | 使用本地 tracing context 显式关闭追踪；不上传轨迹 |
| Typer | 0.29.0 | CLI |
| pytest | 9.2.0 | 自动化测试 |
| Ruff | 0.17.4 | 静态检查 |
| build | 1.6.1 | wheel / sdist 构建 |

完整传递依赖以 `uv.lock` 为准，不将上表当作完整依赖闭包。页面为原生 HTML/CSS/JS，不依赖 CDN、npm 构建、向量库、真实购物网站或支付 SDK。

开发环境已从现有公开依赖缓存离线安装。此结果不代表在空缓存下完成了联网解析，也不等于源码外 wheel 安装通过。独立安装验收状态见 [validation.md](validation.md)。

## 外部连接边界

- 只有用户显式指定的数字 loopback 模型地址可被真实 Provider 请求。
- HTTP 禁止环境代理、重定向、自动重试和云端回退；请求及响应有大小和绝对时限。
- 不扫描端口，不读取已有模型 runtime 配置，不下载或启动模型。
- 运行调用以 `tracing_context(enabled=False, parent=False)` 关闭追踪，callbacks 为空。旧版追踪开关若开启则拒绝启动，不读取或打印其余环境凭据。
- 浏览器验收使用自己的临时 profile 和本机服务；不读取用户日常浏览器数据。

所有依赖保留各自许可证。本工程没有擅自附加整体开源许可证；不应将私有代码上传视为已获任意再许可。
