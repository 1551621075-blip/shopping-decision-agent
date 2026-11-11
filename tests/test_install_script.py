"""Offline unit tests only; simulated workflow results are not install acceptance."""
from __future__ import annotations

import importlib.util
import io
import json
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_install.py"


def test_install_script_exists():
    assert SCRIPT.is_file(), "shopping install checker has not been implemented"


@pytest.fixture
def checker():
    assert SCRIPT.is_file(), "shopping install checker has not been implemented"
    spec = importlib.util.spec_from_file_location("shopping_install_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def project(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname="shopping-decision-agent"\nversion="0.1.0"\n'
        '[project.scripts]\nshopping-advisor="shopping_advisor.cli:main"\n'
    )
    (source / "uv.lock").write_text(
        'version=1\n[[package]]\nname="shopping-decision-agent"\nversion="0.1.0"\n'
        'source={editable="."}\n[[package]]\nname="httpx"\nversion="0.28.1"\n'
        'source={registry="https://pypi.org/simple"}\n'
    )
    return source


def test_shopping_defaults_and_probe_compile(checker):
    assert checker.SOURCE == SCRIPT.parents[1]
    assert checker.DIST_NAME == "shopping-decision-agent"
    assert checker.PACKAGE == "shopping_advisor"
    assert checker.CONSOLE == "shopping-advisor"
    assert checker.DEFAULT_PYTHON == Path(
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12"
    )
    assert checker.DEFAULT_UV == checker.DEFAULT_PYTHON.parent / "uv"
    assert checker.DEFAULT_CACHE == Path.home() / ".cache/uv"
    compile(checker.INSTALLED_PROBE, "installed-probe", "exec")


def test_lock_is_shopping_specific_and_unchanged(checker, tmp_path):
    source = project(tmp_path)
    original = (source / "uv.lock").read_bytes()
    data, versions = checker.read_lock(source)
    assert data == original
    assert versions == {"shopping-decision-agent": {"0.1.0"}, "httpx": {"0.28.1"}}
    assert (source / "uv.lock").read_bytes() == original


@pytest.mark.parametrize("before,after", [
    ('source={editable="."}', 'source={editable="../other"}'),
    ('name="shopping-decision-agent"', 'name="ecommerce-after-sales-agent"'),
    ('source={registry="https://pypi.org/simple"}', 'source={path="../other"}'),
    ('source={registry="https://pypi.org/simple"}', 'source={registry="https://user:secret@host/simple"}'),
    ('version="0.1.0"', 'version="0.2.0"'),
])
def test_lock_rejects_foreign_project_or_nonpublic_sources(checker, tmp_path, before, after):
    source = project(tmp_path)
    lock = source / "uv.lock"
    lock.write_text(lock.read_text().replace(before, after))
    with pytest.raises(checker.CheckFailure):
        checker.read_lock(source)


def test_manifest_requires_exact_locked_registry_runtime(checker):
    versions = {"httpx": {"0.28.1"}, "shopping-decision-agent": {"0.1.0"}}
    checker.validate_manifest('# safe export\nhttpx==0.28.1 ; python_version >= "3.12"\n', versions)
    for text in ("", "httpx>=0.28.1", "httpx==0.27.0", "-e ../source", "httpx @ file:///tmp/x",
                 "shopping-decision-agent==0.1.0", "--index-url https://host", "httpx==0.28.*"):
        with pytest.raises(checker.CheckFailure):
            checker.validate_manifest(text, versions)


def test_environment_does_not_inherit_credentials_or_old_venv(checker, tmp_path, monkeypatch):
    for name in ("PYTHONPATH", "VIRTUAL_ENV", "HTTPS_PROXY", "OPENAI_API_KEY", "UV_INDEX_URL",
                 "LANGSMITH_API_KEY", "BASH_ENV", "ZDOTDIR"):
        monkeypatch.setenv(name, "not-to-inherit")
    env = checker.clean_environment(tmp_path / "work", tmp_path / "public-cache")
    assert "not-to-inherit" not in env.values()
    assert env["HOME"] == str(tmp_path / "work/home")
    assert env["UV_OFFLINE"] == "1"
    assert env["UV_NO_CONFIG"] == "1"
    assert env["UV_PYTHON_DOWNLOADS"] == "never"
    assert env["PIP_NO_INDEX"] == "1"
    assert env["PYTHONNOUSERSITE"] == "1"


def archive(path, names):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path, "w") as wheel:
            for name in names:
                wheel.writestr(name, b"synthetic asset")
    else:
        with tarfile.open(path, "w:gz") as sdist:
            for name in names:
                info = tarfile.TarInfo(name)
                info.size = len(b"synthetic asset")
                sdist.addfile(info, io.BytesIO(b"synthetic asset"))


@pytest.mark.parametrize("kind", ["sdist", "wheel"])
def test_archive_requires_shopping_html_js_css(checker, tmp_path, kind):
    path = tmp_path / ("shopping-0.1.0.tar.gz" if kind == "sdist" else "shopping-0.1.0.whl")
    prefix = "shopping-0.1.0/src/shopping_advisor/" if kind == "sdist" else "shopping_advisor/"
    names = [prefix + asset for asset in checker.ASSETS]
    archive(path, names)
    checker.check_archive(path, kind)
    archive(path, names[:-1])
    with pytest.raises(checker.CheckFailure, match="missing_html_js_css"):
        checker.check_archive(path, kind)


def test_wheel_rejects_path_injection(checker, tmp_path):
    path = tmp_path / "shopping.whl"
    archive(path, ["shopping_advisor/" + asset for asset in checker.ASSETS] + ["escape.pth"])
    with pytest.raises(checker.CheckFailure, match="path_injection"):
        checker.check_archive(path, "wheel")


def test_diagnostic_never_repeats_raw_output(checker):
    raw = b"httpx not found in the cache https://user:secret@private.test token=private-value"
    result = checker.dependency_diagnostic(raw, {"httpx": {"0.28.1"}})
    assert result["reasons"] == ["cache_entry_missing"]
    assert result["mentioned_locked_packages"] == [{"package": "httpx", "locked_versions": ["0.28.1"]}]
    assert all(secret not in json.dumps(result) for secret in ("secret", "private", "token=", "https://"))


def test_owned_directory_identity_rejects_replacement(checker, tmp_path):
    owned = tmp_path / "owned"
    owned.mkdir()
    identity = (owned.stat().st_dev, owned.stat().st_ino)
    assert checker.same_directory(owned, identity)
    moved = tmp_path / "moved"
    owned.rename(moved)
    owned.symlink_to(moved, target_is_directory=True)
    assert not checker.same_directory(owned, identity)


def test_stops_only_own_subprocess_group(checker, tmp_path):
    child = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "import time; time.sleep(60)"],
        cwd=tmp_path, env={"PATH": "/usr/bin:/bin"}, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert checker.stop_owned_group(child)
        assert child.poll() is not None
        assert checker.stop_owned_group(child)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def fake_workflow(checker, tmp_path, monkeypatch, failure=None):
    """Replace the external tool boundary, never run uv or real installation here."""
    source = project(tmp_path)
    cache = tmp_path / "public-cache"
    cache.mkdir()
    monkeypatch.setattr(checker, "SOURCE", source)
    calls = []
    mkdtemp = checker.tempfile.mkdtemp
    monkeypatch.setattr(checker.tempfile, "mkdtemp", lambda **kw: mkdtemp(prefix=kw["prefix"], dir=tmp_path))
    monkeypatch.setattr(checker, "group_exists", lambda pid: False)
    monkeypatch.setattr(checker, "stop_owned_group", lambda process: True)

    class Process:
        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))
            self.pid = 999999
            self.returncode = 0
            self.output = b""
            self.stderr = b""
            self.argv = argv
            work = Path(kwargs["cwd"]).parent
            if "build" in argv:
                dist = Path(argv[argv.index("--out-dir") + 1])
                if "--sdist" in argv:
                    archive(dist / "shopping_decision_agent-0.1.0.tar.gz", [
                        "shopping_decision_agent-0.1.0/src/shopping_advisor/" + a for a in checker.ASSETS
                    ])
                else:
                    archive(dist / "shopping_decision_agent-0.1.0-py3-none-any.whl", [
                        "shopping_advisor/" + a for a in checker.ASSETS
                    ])
            if "export" in argv:
                Path(argv[argv.index("--output-file") + 1]).write_text("httpx==0.28.1\n")
            if "venv" in argv:
                bindir = Path(argv[-1]) / "bin"
                bindir.mkdir(parents=True)
                (bindir / "python").touch()
                (bindir / "shopping-advisor").touch()
            if "--help" in argv:
                self.output = b"init-demo serve --data-dir"
            if checker.INSTALLED_PROBE in argv:
                checker.write_private_json(work / "installed-probe.json", {
                    "explicit_test_mode": True, "noneditable_wheel": True,
                    "console_entry": "shopping_advisor.cli:main", "package_version": "0.1.0",
                    "modules": {}, "resources": [], "outside_source_cwd": True,
                    "runtime_versions_match_lock": True, "runtime_versions": {"httpx": "0.28.1"},
                    "cli_data_check": True, "api_health": True, "api_identity": True,
                    "credentials_memory_only": True, "api_initialization_tested": True,
                })
            if failure == "install" and "install" in argv:
                self.returncode = 1
                self.stderr = b"httpx not found in the cache token=NEVER-PUBLISH"
            if failure == "probe" and checker.INSTALLED_PROBE in argv:
                self.returncode = 1
            if failure == "lock_changed" and "export" in argv:
                (source / "uv.lock").write_text("changed")

        def communicate(self, timeout):
            if failure == "timeout" and "build" in self.argv:
                raise subprocess.TimeoutExpired(self.argv, timeout)
            return self.output, self.stderr

    monkeypatch.setattr(checker.subprocess, "Popen", Process)
    return SimpleNamespace(uv=checker.DEFAULT_UV, python=checker.DEFAULT_PYTHON,
                           cache_dir=cache, timeout_seconds=30), calls, source


def test_simulated_workflow_is_offline_locked_external_and_retains_private_artifacts(
    checker, tmp_path, monkeypatch, capsys,
):
    args, calls, source = fake_workflow(checker, tmp_path, monkeypatch)
    assert checker.run(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["fully_locked_install"] is True
    assert report["fresh_dependency_resolution_tested"] is False
    assert report["llm_acceptance"] is False
    assert report["cli_initialization_tested"] is False
    assert report["owned_work_removed"] is True
    output = Path(report["report_dir"]) / "result.json"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    assert not output.is_relative_to(source)
    assert not (output.parent.parent / "work").exists()
    for item in report["artifacts"].values():
        artifact = Path(item["path"])
        assert artifact.is_file()
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
        assert len(item["sha256"]) == 64
    for argv, options in calls:
        assert not Path(options["cwd"]).is_relative_to(source)
        assert options["env"]["HOME"] != str(Path.home())
        assert "VIRTUAL_ENV" not in options["env"]
        assert options["start_new_session"] is True
        if argv[0] == str(args.uv):
            assert all(flag in argv for flag in ("--offline", "--no-config", "--no-python-downloads"))
        assert not any(word in argv for word in ("--editable", "--system", "--seed", "sync"))
    python_check = next(argv for argv, _ in calls if argv[0] == str(args.python))
    assert 'sys.prefix == sys.base_prefix' in python_check[-1]
    wheel_build = next(argv for argv, _ in calls if "build" in argv and "--wheel" in argv)
    assert wheel_build[-1].endswith(".tar.gz")
    install = next(argv for argv, _ in calls if "install" in argv)
    assert "--no-deps" in install and "--requirement" in install
    assert any("check" in argv and "pip" in argv for argv, _ in calls)
    export = next(argv for argv, _ in calls if "export" in argv)
    assert all(flag in export for flag in ("--frozen", "--no-dev", "--no-emit-project"))


@pytest.mark.parametrize("failure", ["install", "probe", "lock_changed", "timeout"])
def test_failed_step_never_reports_acceptance_or_retries_online(checker, tmp_path, monkeypatch, capsys, failure):
    args, calls, _ = fake_workflow(checker, tmp_path, monkeypatch, failure)
    assert checker.run(args) == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["ok"] is False
    assert report["fully_locked_install"] is False
    assert report["fresh_dependency_resolution_tested"] is False
    assert report["children_stopped"] is True
    assert report["owned_work_removed"] is True
    assert "NEVER-PUBLISH" not in output
    assert sum("install" in argv for argv, _ in calls) <= 1
    assert "failure" in report


def test_completed_processes_are_not_targeted_again_during_cleanup(checker, tmp_path, monkeypatch, capsys):
    args, _, _ = fake_workflow(checker, tmp_path, monkeypatch)
    stopped = []
    monkeypatch.setattr(checker, "stop_owned_group", lambda process: stopped.append(process.pid) or True)
    assert checker.run(args) == 0
    capsys.readouterr()
    assert stopped == []  # A reaped PID must never later target an unrelated reused process group.


def test_preflight_failure_keeps_private_external_report(checker, tmp_path, monkeypatch, capsys):
    args, calls, _ = fake_workflow(checker, tmp_path, monkeypatch)
    args.cache_dir = tmp_path / "absent-public-cache"
    assert checker.run(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failure"]["code"] == "existing_offline_cache_required"
    assert report["fully_locked_install"] is False
    assert not calls
    path = Path(report["report_dir"]) / "result.json"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert report["owned_work_removed"] is True


def test_private_json_exclusive_no_symlink_overwrite(checker, tmp_path):
    output = tmp_path / "result.json"
    checker.write_private_json(output, {"ok": False})
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        checker.write_private_json(output, {"ok": True})
    alias = tmp_path / "alias.json"
    alias.symlink_to(output)
    with pytest.raises(FileExistsError):
        checker.write_private_json(alias, {"ok": True})
    assert json.loads(output.read_text()) == {"ok": False}
