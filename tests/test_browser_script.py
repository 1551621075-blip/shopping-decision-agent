"""Offline browser-driver interface checks; these never launch Chrome or a service."""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts" / "browser_smoke.mjs"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node 24 is required on PATH for JS checks")


def invoke(*args):
    return subprocess.run(
        [str(NODE), str(SCRIPT), *args], cwd=PROJECT,
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=10,
        check=False,
    )


def test_node_syntax():
    result = subprocess.run(
        [str(NODE), "--check", str(SCRIPT)], cwd=PROJECT,
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


def test_dry_run_is_test_only_and_creates_nothing(tmp_path):
    target = tmp_path / "must-remain-absent"
    result = invoke("--dry-run", "--output-dir", str(target))
    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert result.stderr == ""
    assert not target.exists()
    assert report["mode"] == "TEST"
    assert report["terminal"] == "dry_run"
    assert report["human_review"] == "pending"
    assert report["llm_acceptance"] is False
    assert report["side_effects"] is False
    assert report["children_started"] == 0
    assert report["cleanup_required"] is False
    assert "synthetic" in report["token_provenance"]
    assert len(report["flow"]) == 9


def test_help_does_not_launch_or_offer_model_discovery():
    result = invoke("--help")
    assert result.returncode == 0
    assert result.stderr == ""
    assert "TEST only" in result.stdout
    assert "Dry run starts nothing and creates nothing" in result.stdout
    assert "--model" not in result.stdout
    assert "--discover" not in result.stdout


@pytest.mark.parametrize("args", [
    ("--model", "anything"), ("--discover",), ("--mode", "REAL"),
    ("--output-dir", "relative"), ("--output-dir",),
    ("--timeout-seconds", "0"), ("--timeout-seconds", "301"),
    ("--timeout-seconds", "NaN"), ("--timeout-seconds",),
])
def test_invalid_arguments_fail_closed_without_launch(args):
    result = invoke(*args)
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert result.stderr == ""
    assert report["mode"] == "TEST"
    assert report["ok"] is False
    assert report["terminal"] == "failed"
    assert report["human_review"] == "pending"
    assert report["llm_acceptance"] is False
    assert "report_dir" not in report
    assert "failure" in report


def test_python_launcher_rejects_network_before_both_modes():
    script = SCRIPT.read_text()
    launcher = script.split("const LAUNCHER = String.raw`", 1)[1].split("`;", 1)[0]
    tree = ast.parse(launcher)
    guard = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "socket_boundary")
    source = ast.get_source_segment(launcher, guard)
    assert launcher.index("sys.addaudithook(socket_boundary)") < launcher.index("if sys.argv[1] == 'audit'")
    assert launcher.index("sys.addaudithook(socket_boundary)") < launcher.index("from shopping_advisor")
    # Exercise the real extracted guard in a disposable interpreter, never opening a
    # connection. Even numeric DNS and datagrams are rejected before OS dispatch.
    program = "import socket, sys\ndenied = [0]\n" + source + "\nsys.addaudithook(socket_boundary)\n" + '''
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
actions = [
    lambda: sock.connect(("127.0.0.1", 1)),
    lambda: socket.getaddrinfo("127.0.0.1", 1),
    lambda: socket.gethostbyname("127.0.0.1"),
    lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
    lambda: sys.audit("socket.sendto", sock, ("127.0.0.1", 1)),
    lambda: sys.audit("socket.sendmsg", sock, ("127.0.0.1", 1)),
    lambda: sys.audit("socket.bind", sock, ("0.0.0.0", 1)),
]
for action in actions:
    try:
        action()
    except OSError as error:
        assert str(error) == "TEST Python outbound networking disabled"
    else:
        raise AssertionError("socket boundary did not reject")
assert denied == [len(actions)]
sock.close()
'''
    result = subprocess.run([sys.executable, "-I", "-c", program], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_proxy_startup_wait_is_bounded_and_source_execution_is_owned():
    source = SCRIPT.read_text()
    assert "await listenOwned(proxy, healthy)" in source
    assert "proxy.on('error', () => { asyncFailure = 'owned_proxy_error'; })" in source
    assert "execution: 'owned_private_copy'" in source
    assert "source_changed_during_snapshot" in source
    assert "'serve', work, path.join(work, 'source')" in source
    assert "'audit', work, path.join(work, 'source')" in source


def test_owned_proxy_startup_handles_every_terminal_without_sockets():
    script = SCRIPT.read_text()
    start = script.index("async function listenOwned(")
    helper = script[start:script.index("\nfunction options()", start)]
    program = '''import {EventEmitter} from 'node:events';
class SmokeError extends Error { constructor(code) { super(code); this.code = code; } }
''' + helper + '''
class FakeServer extends EventEmitter {
  constructor(mode) { super(); this.mode = mode; this.listening = false; this.closed = false; }
  listen() {
    if (this.mode === 'throw') throw new Error('local bind rejected');
    if (this.mode === 'error') queueMicrotask(() => this.emit('error', new Error('fake EMFILE')));
    if (this.mode === 'success') queueMicrotask(() => { this.listening = true; this.emit('listening'); });
  }
  close() { this.closed = true; }
}
for (const [mode, expected] of [['error','owned_proxy_listen_failed'], ['throw','owned_proxy_listen_failed'],
  ['pending','owned_proxy_listen_timeout'], ['cancel','cancelled_test'], ['success', null]]) {
  const server = new FakeServer(mode); let failure = null, cleaned = false;
  try {
    await listenOwned(server, () => { if (mode === 'cancel') throw new SmokeError('cancelled_test'); }, 35);
  } catch (error) { failure = error.code; }
  finally { server.close(); cleaned = true; }
  if (failure !== expected || !cleaned || !server.closed || server.listenerCount('error') || server.listenerCount('listening')) {
    throw new Error('bounded startup cleanup assertion failed');
  }
}
'''
    result = subprocess.run([str(NODE), "--input-type=module", "-e", program],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


