"""CLI testing stays in-process and uses only fresh external runtime directories."""
import importlib
import importlib.util
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from shopping_advisor.storage import Store


def test_cli_entrypoint_exists():
    assert importlib.util.find_spec("shopping_advisor.cli") is not None, "CLI entrypoint is missing"


@pytest.fixture
def cli():
    return importlib.import_module("shopping_advisor.cli")


def test_init_demo_writes_private_test_identities_without_printing_tokens(cli, tmp_path):
    root = tmp_path / "runtime"
    result = CliRunner().invoke(cli.app, ["init-demo", "--data-dir", str(root)])
    assert result.exit_code == 0, result.output
    store = Store(root)
    for name, role in (("alice", "customer"), ("bob", "customer"), ("operator", "operator")):
        path = root / (name + ".token")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        token = path.read_text().strip()
        assert token not in result.output
        assert store.authenticate(token).role == role
        assert str(path) in result.output
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    before = (root / "alice.token").read_bytes()
    assert CliRunner().invoke(cli.app, ["init-demo", "--data-dir", str(root)]).exit_code != 0
    assert (root / "alice.token").read_bytes() == before


def test_source_tree_and_symlink_runtime_rejected_before_write(cli, tmp_path):
    source = Path(cli.__file__).resolve().parents[2]
    for root in (source / "forbidden-runtime", source / "src" / "forbidden-runtime"):
        assert not root.exists()
        result = CliRunner().invoke(cli.app, ["init-demo", "--data-dir", str(root)])
        assert result.exit_code != 0 and not root.exists()
    link = tmp_path / "source-link"
    link.symlink_to(source, target_is_directory=True)
    result = CliRunner().invoke(cli.app, ["init-demo", "--data-dir", str(link / "forbidden-runtime")])
    assert result.exit_code != 0 and not (source / "forbidden-runtime").exists()
    target = tmp_path / "ordinary"
    target.mkdir()
    link2 = tmp_path / "runtime-link"
    link2.symlink_to(target, target_is_directory=True)
    assert CliRunner().invoke(cli.app, ["init-demo", "--data-dir", str(link2)]).exit_code != 0


@pytest.mark.parametrize("options", [[], ["--model", "demo"], ["--model-url", "http://127.0.0.1:9999"],
    ["--test-mode", "--model", "demo"], ["--test-mode", "--model-url", "http://127.0.0.1:9999"],
    ["--test-mode", "--host", "localhost"], ["--test-mode", "--host", "0.0.0.0"],
    ["--test-mode", "--host", "127.0.0.2"], ["--test-mode", "--host", "::1"],
    ["--model-url", "https://example.test", "--model", "demo"]])
def test_serve_rejects_implicit_or_conflicting_modes_and_nonlocal_bind(cli, tmp_path, monkeypatch, options):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid CLI input must not start a server")

    monkeypatch.setattr(cli.uvicorn, "run", forbidden)
    result = CliRunner().invoke(cli.app, ["serve", "--data-dir", str(tmp_path / "missing"), *options])
    assert result.exit_code != 0


def test_explicit_local_model_serve_does_not_discover_or_call_model(cli, tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    Store.initialize(root, now=1800000000)
    captured = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda application, **kwargs: captured.append((application, kwargs)))
    result = CliRunner().invoke(cli.app, ["serve", "--data-dir", str(root), "--model-url",
                                        "http://127.0.0.1:9999", "--model", "explicit-test", "--port", "8769"])
    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    options = captured[0][1]
    assert options["host"] == "127.0.0.1" and options["port"] == 8769
    assert options["access_log"] is False and options["proxy_headers"] is False
    assert options["forwarded_allow_ips"] == ""
    assert "http://127.0.0.1:8769" in result.output


def test_http_default_port_serves_normalized_host_and_origin(cli, tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    _, tokens = Store.initialize(root, now=1800000000)
    captured = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda application, **kwargs: captured.append((application, kwargs)))
    result = CliRunner().invoke(cli.app, ["serve", "--data-dir", str(root), "--test-mode", "--port", "80"])
    assert result.exit_code == 0, result.output
    application, options = captured[0]
    assert options["port"] == 80 and options["host"] == "127.0.0.1"
    with TestClient(application, base_url="http://127.0.0.1:80") as client:
        auth = {"Authorization": "Bearer " + tokens["alice"]}
        assert client.get("/api/me", headers=auth).status_code == 200
        mutation = {**auth, "Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
        assert client.post("/api/sessions", headers=mutation, json={"action_id": "port-80"}).status_code == 200
        assert client.post("/api/sessions", headers={**mutation, "Origin": "http://127.0.0.1:81"},
                           json={"action_id": "wrong-origin"}).status_code == 403
        assert client.get("/api/me", headers={**auth, "Host": "127.0.0.1:81"}).status_code == 400
    assert "服务地址：http://127.0.0.1\n" in result.output


def test_check_is_read_only_and_does_not_require_model(cli, tmp_path):
    root = tmp_path / "runtime"
    Store.initialize(root, now=1800000000)
    result = CliRunner().invoke(cli.app, ["check", "--data-dir", str(root)])
    assert result.exit_code == 0, result.output
    assert "模型" in result.output


def test_legacy_tracing_rejected_before_start(cli, tmp_path, monkeypatch):
    monkeypatch.setenv("LANGCHAIN_TRACING", "true")
    result = CliRunner().invoke(cli.app, ["serve", "--data-dir", str(tmp_path / "runtime"), "--test-mode"])
    assert result.exit_code != 0
    assert "LANGCHAIN_TRACING" in result.output
    assert os.environ["LANGCHAIN_TRACING"] == "true"
