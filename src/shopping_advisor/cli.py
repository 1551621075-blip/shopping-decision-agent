"""Explicit local-only CLI. Demo identity tokens are written once, never printed."""
from __future__ import annotations

import os
import time
from pathlib import Path

import typer
import uvicorn

from .agent import require_local_tracing_environment
from .api import create_app
from .provider import LocalHTTPProvider, ProviderError
from .schemas import DomainError
from .storage import Store

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)


def _data_directory(directory):
    path = Path(directory).absolute()
    source = Path(__file__).resolve().parents[2]
    resolved = path.resolve()
    # Include the source workspace so sibling projects cannot receive this runtime.
    workspace = source.parent if (source / "pyproject.toml").is_file() else source
    if resolved == workspace or workspace in resolved.parents:
        raise DomainError("source_runtime", "数据目录必须在源码工作区之外")
    if any(part.is_symlink() for part in (path, *path.parents)):
        # macOS /tmp resolves through /private/tmp; accept that standard ancestor only.
        links = [part for part in (path, *path.parents) if part.is_symlink()]
        if any(str(part) not in {"/tmp", "/var"} for part in links):
            raise DomainError("symlink_runtime", "数据路径不可通过符号链接指定")
    return resolved


@app.command("init-demo")
def initialize(data_dir: Path = typer.Option(..., "--data-dir", help="源码外全新数据目录")):
    """初始化原创合成目录和Alice/Bob/operator临时TEST身份。"""
    try:
        root = _data_directory(data_dir)
        _, tokens = Store.initialize(root, now=int(time.time()))
        for name, token in tokens.items():
            path = root / (name + ".token")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(token + "\n")
                output.flush()
                os.fsync(output.fileno())
    except (DomainError, OSError, ValueError):
        typer.echo("初始化失败：须使用源码外全新普通目录，父目录须存在；未打印任何令牌。", err=True)
        raise typer.Exit(1) from None
    typer.echo("TEST合成数据已初始化；不代表真实模型验收，不下单、不付款。")
    typer.echo(f"数据目录：{root}")
    for name in ("alice", "bob", "operator"):
        typer.echo(f"临时身份文件（0600）：{root / (name + '.token')}")
    typer.echo("令牌仅由授权用户填入网页密码框，不放入URL或浏览器持久存储。")


@app.command()
def serve(
    data_dir: Path = typer.Option(..., "--data-dir"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port", min=1, max=65535),
    test_mode: bool = typer.Option(False, "--test-mode", help="显式TEST脚本，不是真实LLM"),
    model_url: str | None = typer.Option(None, "--model-url", help="显式数字回环HTTP模型地址"),
    model: str | None = typer.Option(None, "--model", help="显式模型ID，无自动发现或回退"),
):
    """只绑定127.0.0.1；不扫描、启动或自动选择模型。"""
    if (test_mode and (model_url is not None or model is not None)) or (
        not test_mode and (not model_url or not model)
    ):
        raise typer.BadParameter("必须互斥选择 --test-mode 或同时指定 --model-url 与 --model")
    if host != "127.0.0.1":
        raise typer.BadParameter("host只能是数字127.0.0.1")
    try:
        require_local_tracing_environment()
    except DomainError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(1) from None
    try:
        root = _data_directory(data_dir)
        if test_mode:
            from .demo_provider import DemoProvider

            provider = DemoProvider()
        else:
            provider = LocalHTTPProvider(model_url, model)
        store = Store(root)
        origin = "http://127.0.0.1" + (f":{port}" if port != 80 else "")
        application = create_app(store, provider, origin=origin)
    except (DomainError, ProviderError, ValueError, OSError, ImportError):
        typer.echo("启动失败：请检查本实例源码外数据目录及显式模型配置；TEST组件须已安装。无自动回退。", err=True)
        raise typer.Exit(1) from None
    typer.echo("TEST脚本演示，不是真实LLM。" if test_mode else "显式本地模型模式；尚不代表模型验收通过。")
    typer.echo(f"服务地址：{origin}")
    uvicorn.run(application, host="127.0.0.1", port=port, access_log=False, proxy_headers=False,
                forwarded_allow_ips="", server_header=False, log_level="warning")


@app.command()
def check(data_dir: Path = typer.Option(..., "--data-dir")):
    """仅核对本实例数据库，不连接模型、不读取身份令牌文件。"""
    try:
        Store(_data_directory(data_dir))
    except (DomainError, OSError, ValueError):
        typer.echo("数据目录检查失败。", err=True)
        raise typer.Exit(1) from None
    typer.echo("本实例数据目录可用；未连接模型，未执行模型验收。")


def main():
    app()


if __name__ == "__main__":
    main()
