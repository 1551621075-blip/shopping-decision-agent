#!/usr/bin/env python3
"""Offline shopping wheel/sdist acceptance, never a model or fresh resolver test.

Build a shopping sdist, build its wheel, then install that wheel and the exported
frozen runtime closure non-editably in a new venv with an external cwd and HOME.
Missing public cached dependencies fail: there is no online retry. No old venv,
user configuration, production data, credential files or model runtime is used.
Only owned temporary work/processes are cleaned; private distribution artifacts
and a safe JSON report remain outside source. CLI initialization is deliberately
not run because it writes token files; API initialization keeps tokens in memory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
DEFAULT_PYTHON = Path('/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12')
DEFAULT_UV = DEFAULT_PYTHON.parent / 'uv'
DEFAULT_CACHE = Path.home() / '.cache' / 'uv'
DIST_NAME = 'shopping-decision-agent'
PACKAGE = 'shopping_advisor'
CONSOLE = 'shopping-advisor'
ASSETS = ('templates/index.html', 'static/app.js', 'static/style.css')
PROBE_KEYS = {
    'explicit_test_mode', 'noneditable_wheel', 'console_entry', 'package_version',
    'modules', 'resources', 'outside_source_cwd', 'runtime_versions_match_lock',
    'runtime_versions', 'cli_data_check', 'api_health', 'api_identity',
    'credentials_memory_only', 'api_initialization_tested',
}

# Execute ONLY with the installed fresh interpreter, never the project .venv.
# -I excludes cwd/user-site/PYTHONPATH; no source-path recovery is allowed.
INSTALLED_PROBE = r'''
import importlib
import importlib.metadata
import json
import os
import re
import stat
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

venv, source, data, output, manifest_path, lock_versions_path = map(Path, sys.argv[1:])
venv, source = venv.resolve(), source.resolve()

def require(value, code):
    if not value:
        raise RuntimeError(code)

def inside(file, parent):
    return Path(file).resolve().is_relative_to(parent)

require(Path(sys.prefix).resolve() == venv, 'wrong_venv_prefix')
require(sys.prefix != sys.base_prefix, 'not_a_new_venv')
require('PYTHONPATH' not in os.environ and 'VIRTUAL_ENV' not in os.environ, 'source_environment_leaked')
require(not inside(Path.cwd(), source), 'source_cwd')
require(not inside(Path.home(), source) and Path.home().is_dir(), 'source_home')
require(not any(p and inside(p, source) for p in sys.path), 'source_import_path')
require(not data.exists() and not data.is_symlink(), 'data_not_fresh')

modules = {}
for name in ('shopping_advisor', 'shopping_advisor.api', 'shopping_advisor.cli',
             'shopping_advisor.storage', 'shopping_advisor.demo_provider', 'fastapi', 'httpx', 'uvicorn'):
    module = importlib.import_module(name)
    require(module.__file__ and inside(module.__file__, venv), 'module_outside_new_venv')
    modules[name] = str(Path(module.__file__).resolve())
package = importlib.import_module('shopping_advisor')
require(all(inside(p, venv) for p in package.__path__), 'namespace_outside_venv')
dist = importlib.metadata.distribution('shopping-decision-agent')
require(inside(dist.locate_file(''), venv), 'metadata_outside_new_venv')
entry = [e for e in dist.entry_points if e.group == 'console_scripts' and e.name == 'shopping-advisor']
require(len(entry) == 1 and entry[0].value == 'shopping_advisor.cli:main' and callable(entry[0].load()),
        'broken_console_entry')
direct = dist.read_text('direct_url.json')
require(direct is not None, 'wheel_install_origin_missing')
direct = json.loads(direct)
require(not direct.get('dir_info', {}).get('editable'), 'editable_install')
require(urlsplit(direct['url']).path.endswith('.whl'), 'not_installed_from_wheel')

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

# Evaluate markers using the NEW interpreter, not the source environment.
lock_versions = json.loads(lock_versions_path.read_text(encoding='utf-8'))
expected = {}
for line in manifest_path.read_text(encoding='utf-8').splitlines():
    if not line.strip() or line.lstrip().startswith('#'):
        continue
    requirement = Requirement(line)
    require(requirement.url is None, 'nonregistry_runtime_requirement')
    if requirement.marker is not None and not requirement.marker.evaluate():
        continue
    name = canonicalize_name(requirement.name)
    specs = list(requirement.specifier)
    require(len(specs) == 1 and specs[0].operator == '==' and '*' not in specs[0].version,
            'runtime_requirement_not_exact')
    version = specs[0].version
    require(version in lock_versions.get(name, []), 'manifest_version_not_in_lock')
    require(name not in expected or expected[name] == version, 'conflicting_runtime_versions')
    expected[name] = version
require(expected and 'shopping-decision-agent' not in expected, 'invalid_runtime_manifest')
actual = {}
for installed in importlib.metadata.distributions():
    name = canonicalize_name(installed.metadata['Name'])
    require(name not in actual, 'duplicate_installed_distribution')
    require(inside(installed.locate_file(''), venv), 'dependency_outside_new_venv')
    actual[name] = installed.version
require(set(actual) == set(expected) | {'shopping-decision-agent'}, 'installed_dependency_set_mismatch')
require(all(actual[name] == version for name, version in expected.items()), 'installed_version_differs_from_lock')
require(actual['shopping-decision-agent'] in lock_versions['shopping-decision-agent'], 'project_version_not_locked')

from fastapi.testclient import TestClient
from shopping_advisor.api import create_app
from shopping_advisor.demo_provider import DemoProvider
from shopping_advisor.storage import Store

# Store.initialize persists only token hashes. Plain credentials stay in this
# process, never argv/env/files/stdout or the report. Do NOT use CLI init-demo.
store, tokens = Store.initialize(data, now=int(time.time()))
require(set(tokens) == {'alice', 'bob', 'operator'}, 'synthetic_identity_schema_changed')
require(all(isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token)
            for token in tokens.values()), 'synthetic_identity_schema_changed')
require(stat.S_IMODE(data.stat().st_mode) == 0o700, 'synthetic_data_not_private')
require(stat.S_IMODE((data / 'state.sqlite').stat().st_mode) == 0o600, 'synthetic_database_not_private')
require(not list(data.glob('*.token')), 'credential_file_created')
# Exercise the real installed console against the same new data, without tokens.
# Inherit the probe's owned session; the parent kills that group on interruption.
completed = subprocess.run([str(venv / 'bin/shopping-advisor'), 'check', '--data-dir', str(data)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30, check=False)
require(completed.returncode == 0, 'installed_cli_data_check_failed')
provider = DemoProvider()
require(getattr(provider, 'is_test_provider', False) is True, 'not_explicit_test_provider')
# TestClient is in-process ASGI, not a listener or a request to an existing port.
origin = 'http://127.0.0.1:18763'
app = create_app(store, provider, origin=origin)

class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.resources = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and 'src' in attrs:
            self.resources.append(attrs['src'])
        if tag == 'link' and attrs.get('rel') == 'stylesheet' and 'href' in attrs:
            self.resources.append(attrs['href'])

asset_results = []
asset_root = Path(package.__file__).resolve().parent
with TestClient(app, base_url=origin, follow_redirects=False) as client:
    health = client.get('/api/health')
    require(health.status_code == 200 and health.json() == {'status': 'ok', 'test_mode': True},
            'api_health_contract_failed')
    require(client.get('/api/me').status_code == 401, 'identity_not_authenticated')
    identity = client.get('/api/me', headers={'Authorization': 'Bearer ' + tokens['alice']})
    require(identity.status_code == 200 and identity.json() ==
            {'user_id': 'alice', 'role': 'customer', 'test_mode': True}, 'api_identity_contract_failed')
    routes = [('/', 'templates/index.html', 'text/html'),
              ('/static/app.js', 'static/app.js', 'javascript'),
              ('/static/style.css', 'static/style.css', 'text/css')]
    for route, relative, content_type in routes:
        file = asset_root / relative
        require(inside(file, venv) and file.is_file(), 'packaged_asset_missing')
        response = client.get(route)
        require(response.status_code == 200, 'static_resource_failed')
        require(content_type in response.headers.get('content-type', ''), 'static_content_type')
        require(response.content and response.content == file.read_bytes(), 'asset_not_from_installed_package')
        require(response.headers.get('x-content-type-options') == 'nosniff', 'missing_security_header')
        if route == '/':
            refs = References()
            refs.feed(response.text)
            require('/static/app.js' in refs.resources and '/static/style.css' in refs.resources,
                    'html_asset_references_missing')
            require(all(r.startswith('/static/') and not r.startswith('//')
                        and not urlsplit(r).netloc and not urlsplit(r).query and not urlsplit(r).fragment
                        for r in refs.resources), 'nonlocal_static_reference')
        asset_results.append({'route': route, 'package_path': str(file), 'bytes': len(response.content)})
    for route in set(refs.resources):
        require(client.get(route).status_code == 200, 'html_referenced_resource_missing')
require(not list(data.glob('*.token')), 'credential_file_created')
tokens.clear()
# Closed schema; no raw HTTP bodies, logs, chats, tokens or exception messages.
result = {'explicit_test_mode': True, 'noneditable_wheel': True,
          'console_entry': entry[0].value, 'package_version': dist.version,
          'modules': modules, 'resources': asset_results, 'outside_source_cwd': True,
          'runtime_versions_match_lock': True, 'runtime_versions': dict(sorted(expected.items())),
          'cli_data_check': True, 'api_health': True, 'api_identity': True,
          'credentials_memory_only': True, 'api_initialization_tested': True}
fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'w', encoding='utf-8') as stream:
    json.dump(result, stream, ensure_ascii=False)
    stream.write('\n')
'''


class CheckFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def require(value, code: str):
    if not value:
        raise CheckFailure(code)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uv', type=Path, default=DEFAULT_UV, help='absolute uv executable')
    parser.add_argument('--python', type=Path, default=DEFAULT_PYTHON, help='absolute Python 3.12 executable')
    parser.add_argument('--cache-dir', type=Path, default=DEFAULT_CACHE, help='existing public uv cache')
    parser.add_argument('--timeout-seconds', type=int, default=240, help='per-command timeout (30..1800)')
    return parser.parse_args(argv)


def read_lock(source: Path):
    project = tomllib.loads((source / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    require(project['name'] == DIST_NAME and project.get('scripts', {}).get(CONSOLE) == PACKAGE + '.cli:main',
            'not_independent_shopping_project')
    lock_bytes = (source / 'uv.lock').read_bytes()
    lock = tomllib.loads(lock_bytes.decode('utf-8'))
    roots = [p for p in lock['package'] if p['name'] == DIST_NAME]
    require(len(roots) == 1 and roots[0]['source'] == {'editable': '.'}
            and roots[0]['version'] == project['version'], 'not_current_shopping_lock')
    known = {}
    for package in lock['package']:
        name, version = package['name'], package['version']
        require(re.fullmatch(r'[a-z0-9][a-z0-9-]*', name)
                and re.fullmatch(r'[0-9][A-Za-z0-9.!+_-]*', version), 'unsafe_lock_name_or_version')
        if name != DIST_NAME:
            require(package.get('source') == {'registry': 'https://pypi.org/simple'},
                    'only_public_pypi_lock_sources_supported')
        known.setdefault(name, set()).add(version)
    return lock_bytes, known


def validate_manifest(text: str, known_versions: dict):
    count = 0
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = re.fullmatch(r'([a-z0-9][a-z0-9-]*)==([0-9][A-Za-z0-9.!+_-]*)(?:\s*;\s*[^\n]+)?', line)
        require(match is not None, 'unsupported_frozen_requirement')
        name, version = match.group(1, 2)
        require(name != DIST_NAME and version in known_versions.get(name, set()), 'manifest_not_locked_runtime')
        count += 1
    require(count, 'empty_runtime_manifest')


def clean_environment(work: Path, cache: Path) -> dict:
    # Whitelist, never copy the caller's environment or read shell/config files.
    home, temp = work / 'home', work / 'tmp'
    return {
        'PATH': '/usr/bin:/bin', 'HOME': str(home), 'TMPDIR': str(temp), 'LANG': 'en_US.UTF-8',
        'XDG_CONFIG_HOME': str(home), 'XDG_CACHE_HOME': str(temp), 'PYTHONNOUSERSITE': '1',
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1', 'UV_OFFLINE': '1', 'UV_NO_CONFIG': '1',
        'UV_PYTHON_DOWNLOADS': 'never', 'UV_CACHE_DIR': str(cache),
        'UV_PYTHON_INSTALL_DIR': str(work / 'uv-python'), 'UV_LINK_MODE': 'copy',
        'PIP_NO_INDEX': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'PIP_CONFIG_FILE': os.devnull,
        'NO_COLOR': '1', 'TERM': 'dumb',
    }


def check_archive(path: Path, kind: str):
    if kind == 'sdist':
        with tarfile.open(path, 'r:gz') as archive:
            files = [item.name for item in archive.getmembers() if item.isfile()]
        require(all(any(name.endswith('/src/' + PACKAGE + '/' + asset) for name in files)
                    for asset in ASSETS), 'sdist_missing_html_js_css')
    else:
        require(kind == 'wheel', 'unknown_artifact_kind')
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        require(all(PACKAGE + '/' + asset in names for asset in ASSETS), 'wheel_missing_html_js_css')
        require(not any(name.endswith('.pth') for name in names), 'wheel_contains_path_injection')


def same_directory(path: Path, identity):
    current = path.lstat()
    return stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == identity


def write_private_json(path: Path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop_owned_group(process: subprocess.Popen) -> bool:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not group_exists(process.pid):
            break
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            return False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            process.poll()
            if not group_exists(process.pid):
                break
            time.sleep(0.05)
    process.poll()
    return not group_exists(process.pid)


def dependency_diagnostic(stderr: bytes, known_versions: dict) -> dict:
    """Only public locked package names/reason enums; never raw stderr or URLs."""
    text = stderr.decode('utf-8', 'replace').lower()
    phrases = {
        'resolution_unsatisfiable': ('no solution found',),
        'offline_network_disabled': ('network is disabled', 'network access is disabled',
                                     'network connectivity is disabled'),
        'registry_metadata_or_artifact_uncached': ('needs to be downloaded from a registry',),
        'cache_entry_missing': ('not found in the cache', 'not cached', 'no cache entry'),
        'dependency_conflict': ('incompatible', 'requires a different version'),
        'permission_denied': ('permission denied',),
        'unsupported_option': ('unexpected argument',),
    }
    reasons = [reason for reason, options in phrases.items() if any(p in text for p in options)]
    mentioned = []
    for name, versions in sorted(known_versions.items()):
        if re.search(r'(?<![a-z0-9_.-])' + re.escape(name) + r'(?![a-z0-9_.-])', text):
            mentioned.append({'package': name, 'locked_versions': sorted(versions)})
    return {'reasons': reasons or ['unclassified_offline_command_failure'],
            'mentioned_locked_packages': mentioned}


def run(args) -> int:
    summary = {
        'kind': 'shopping_offline_dist_install_check', 'ok': False, 'llm_acceptance': False,
        'network_policy': 'offline_only_no_automatic_online_retry', 'steps': [],
        'install_strategy': 'offline_fully_locked_install', 'fully_locked_install': False,
        'fresh_dependency_resolution_tested': False, 'cli_initialization_tested': False,
        'cli_server_tested': False, 'api_initialization_tested': False,
        'validation_policy': 'frozen_runtime_no_deps_then_pip_check_and_exact_installed_lock_versions',
    }
    root = work = report = None
    identities = {}
    children = []
    phase = 'prepare'
    previous_handlers = {}

    def interrupted(signum, _frame):
        raise CheckFailure('interrupted_' + signal.Signals(signum).name)

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, interrupted)
        root = Path(tempfile.mkdtemp(prefix='shopping-install-', dir='/tmp')).resolve()
        require(not root.is_relative_to(SOURCE), 'temporary_directory_inside_source')
        root.chmod(0o700)
        info = root.lstat()
        identities[root] = (info.st_dev, info.st_ino)
        work, report = root / 'work', root / 'report'
        for directory in (work, report):
            directory.mkdir(mode=0o700)
            info = directory.lstat()
            identities[directory] = (info.st_dev, info.st_ino)
        for name in ('home', 'tmp', 'dist', 'outside-source-cwd'):
            (work / name).mkdir(mode=0o700)
        # Establish private reporting first, so even cache/path/lock failures leave
        # evidence instead of being mistaken for a successful or unattempted run.
        require(sys.version_info[:2] == (3, 12), 'run_script_with_python_3_12')
        require(sys.platform == 'darwin', 'this_check_targets_macos')
        require(30 <= args.timeout_seconds <= 1800, 'invalid_timeout')
        for executable in (args.uv, args.python):
            require(executable.is_absolute() and executable.is_file() and os.access(executable, os.X_OK),
                    'absolute_executable_missing')
            require(not executable.absolute().is_relative_to(SOURCE), 'executable_inside_source')
        require(args.cache_dir.is_absolute() and args.cache_dir.is_dir(), 'existing_offline_cache_required')
        lock_bytes, known_versions = read_lock(SOURCE)
        summary['lock_sha256'] = hashlib.sha256(lock_bytes).hexdigest()
        cwd, dist_dir = work / 'outside-source-cwd', work / 'dist'
        env = clean_environment(work, args.cache_dir)
        uv = [str(args.uv), '--offline', '--no-config', '--no-python-downloads',
              '--cache-dir', str(args.cache_dir)]

        def command(name, argv, *, capture=False, dependency_phase=False):
            nonlocal phase
            phase = name
            process = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.PIPE if dependency_phase else subprocess.DEVNULL,
                start_new_session=True,
            )
            children.append(process)
            try:
                output, stderr = process.communicate(timeout=args.timeout_seconds)
            except subprocess.TimeoutExpired:
                if stop_owned_group(process):
                    children.remove(process)
                raise CheckFailure(name + '_timeout') from None
            group_alive = group_exists(process.pid)
            if not group_alive:
                # Forget reaped groups promptly: their PID can be reused later.
                children.remove(process)
            if process.returncode != 0 and dependency_phase:
                summary['dependency_diagnostic'] = dependency_diagnostic(stderr or b'', known_versions)
            require(process.returncode == 0, name + '_exit_' + str(process.returncode))
            require(not group_alive, name + '_left_child_processes')
            summary['steps'].append(name)
            return output

        command('python_3_12_available', [str(args.python), '-I', '-B', '-c',
                'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) '
                'and sys.prefix == sys.base_prefix else 1)'])
        command('build_sdist_offline', uv + ['build', '--python', str(args.python), '--sdist',
                '--out-dir', str(dist_dir), str(SOURCE)], dependency_phase=True)
        sdists = list(dist_dir.glob('*.tar.gz'))
        require(len(sdists) == 1, 'expected_one_sdist')
        check_archive(sdists[0], 'sdist')
        summary['steps'].append('sdist_contains_html_js_css')
        command('build_wheel_from_sdist_offline', uv + ['build', '--python', str(args.python), '--wheel',
                '--out-dir', str(dist_dir), str(sdists[0])], dependency_phase=True)
        wheels = list(dist_dir.glob('*.whl'))
        require(len(wheels) == 1, 'expected_one_wheel')
        check_archive(wheels[0], 'wheel')
        summary['steps'].append('wheel_contains_html_js_css')
        summary['artifacts'] = {}
        for kind, artifact in (('sdist', sdists[0]), ('wheel', wheels[0])):
            kept = report / artifact.name
            artifact.chmod(0o600)
            shutil.copyfile(artifact, kept)
            kept.chmod(0o600)
            summary['artifacts'][kind] = {'path': str(kept), 'sha256': hashlib.sha256(kept.read_bytes()).hexdigest()}

        manifest, locked = work / 'frozen-runtime-requirements.txt', work / 'locked-versions.json'
        write_private_json(locked, {n: sorted(v) for n, v in known_versions.items()})
        command('export_frozen_runtime_manifest', uv + ['export', '--frozen', '--project', str(SOURCE),
                '--no-dev', '--no-emit-project', '--no-hashes', '--no-header', '--output-file', str(manifest)],
                dependency_phase=True)
        require((SOURCE / 'uv.lock').read_bytes() == lock_bytes, 'lock_changed_during_check')
        manifest.chmod(0o600)
        validate_manifest(manifest.read_text(encoding='utf-8'), known_versions)
        venv = work / 'fresh-venv'
        command('create_fresh_venv', uv + ['venv', '--python', str(args.python), str(venv)])
        python, cli = venv / 'bin/python', venv / ('bin/' + CONSOLE)
        # Exact frozen requirements can use cached artifacts without fresh registry
        # metadata. This checks the locked install, not independent re-resolution.
        command('install_wheel_and_frozen_runtime_offline', uv + ['pip', 'install', '--python', str(python),
                '--no-deps', '--requirement', str(manifest), str(wheels[0])], dependency_phase=True)
        command('installed_dependency_consistency', uv + ['pip', 'check', '--python', str(python)],
                dependency_phase=True)
        require(cli.is_file(), 'installed_console_script_missing')
        help_output = command('installed_cli_help_outside_source', [str(cli), '--help'], capture=True)
        require(b'init-demo' in help_output and b'serve' in help_output, 'console_help_missing_commands')
        init_help = command('installed_cli_init_help_outside_source', [str(cli), 'init-demo', '--help'], capture=True)
        require(b'--data-dir' in init_help, 'console_init_help_missing_data_dir')
        result_file = work / 'installed-probe.json'
        command('installed_new_data_cli_api_and_packaged_assets',
                [str(python), '-I', '-B', '-c', INSTALLED_PROBE, str(venv), str(SOURCE),
                 str(work / 'synthetic-data'), str(result_file), str(manifest), str(locked)])
        probe = json.loads(result_file.read_text(encoding='utf-8'))
        require(set(probe) == PROBE_KEYS, 'probe_schema_changed')
        for flag in PROBE_KEYS - {'console_entry', 'package_version', 'modules', 'resources', 'runtime_versions'}:
            require(probe[flag] is True, 'probe_validation_incomplete')
        require((SOURCE / 'uv.lock').read_bytes() == lock_bytes, 'lock_changed_during_check')
        summary['steps'].append('all_runtime_dependency_versions_match_frozen_lock')
        summary['installed'] = probe
        summary['api_initialization_tested'] = True
        summary['fully_locked_install'] = True
        summary['ok'] = True
    except CheckFailure as error:
        summary['failure'] = {'phase': phase, 'code': error.code}
    except Exception:
        # Do not serialize arbitrary exception text, stderr, response bodies or tokens.
        summary['failure'] = {'phase': phase, 'code': 'unexpected_local_error'}
    finally:
        for sig in previous_handlers:
            signal.signal(sig, signal.SIG_IGN)
        stopped = True
        for process in reversed(children):
            if not stop_owned_group(process):
                stopped = False
        summary['children_stopped'] = stopped
        summary['owned_work_removed'] = False
        if not stopped:
            summary['ok'] = False
            summary['cleanup_failure'] = 'owned_child_group_not_stopped'
        if work is not None and stopped:
            try:
                require(same_directory(root, identities[root]) and same_directory(work, identities[work]),
                        'cleanup_owner_identity_changed')
                require(work.resolve() == root / 'work', 'cleanup_path_changed')
                shutil.rmtree(work)  # Only our newly created inode/path, never a supplied directory.
                summary['owned_work_removed'] = True
            except Exception:
                summary['ok'] = False
                summary['cleanup_failure'] = 'owned_work_cleanup_failed'
        if not summary['ok']:
            summary['hint'] = ('No online retry was attempted. Check the safe failure phase, Python/uv paths, '
                               'public offline cache and completed shopping API/CLI/UI packaging. '
                               'Cache/network preparation requires separate user authorization.')
        if report is not None:
            summary['report_dir'] = str(report)
            try:
                require(same_directory(root, identities[root]) and same_directory(report, identities[report]),
                        'report_owner_identity_changed')
                write_private_json(report / 'result.json', summary)
            except Exception:
                summary['ok'] = False
                summary['report_failure'] = 'report_write_failed'
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(run(parse_args()))
