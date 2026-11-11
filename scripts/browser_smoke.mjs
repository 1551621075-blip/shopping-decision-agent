#!/usr/bin/env node
/** TEST-only, dependency-free Chrome DOM acceptance. No real-model configuration/discovery. */
import {spawn} from 'node:child_process';
import {createHash} from 'node:crypto';
import {constants} from 'node:fs';
import {access, chmod, lstat, mkdir, mkdtemp, open, readFile, readdir, realpath, rm, writeFile} from 'node:fs/promises';
import {createServer} from 'node:http';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const PROJECT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PYTHON = path.join(PROJECT, '.venv/bin/python');
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const INITIAL_MESSAGE = '总预算6000元，用于办公，更看重价格。';
const NEXT_MESSAGE = '请按已确认的条件比较并推荐一款。';
const LOW_MESSAGE = '请严格按当前已确认的100元总预算重新查询，不要提高预算。';
const PREF_KEYS = ['budget_cents', 'use', 'priority', 'min_ram_gb', 'min_storage_gb',
  'max_weight_g', 'min_battery_minutes', 'needs_discrete_gpu', 'os'];
const BASE_PREFS = {budget_cents: 600000, use: 'office', priority: 'value', min_ram_gb: 8,
  min_storage_gb: 256, max_weight_g: null, min_battery_minutes: null, needs_discrete_gpu: false, os: 'any'};
class SmokeError extends Error { constructor(code) { super(code); this.code = code; } }
const check = (condition, code) => { if (!condition) throw new SmokeError(code); };
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const canonical = value => JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item)
  ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
const hash = value => createHash('sha256').update(canonical(value)).digest('hex');
const equal = (a, b) => canonical(a) === canonical(b);
const ident = stat => ({dev: stat.dev, ino: stat.ino});
const owned = (stat, id) => stat.isDirectory() && !stat.isSymbolicLink() && stat.uid === process.getuid()
  && (stat.mode & 0o777) === 0o700 && stat.dev === id.dev && stat.ino === id.ino;
const money = cents => (cents / 100).toFixed(2);
async function listenOwned(server, healthy, timeoutMs = 5000) {
  await new Promise((resolve, reject) => {
    let done = false;
    const finish = error => {
      if (done) return;
      done = true; clearTimeout(timeout); clearInterval(poll);
      server.off('error', onError); server.off('listening', onListening);
      if (error) reject(error); else resolve();
    };
    const onError = () => finish(new SmokeError('owned_proxy_listen_failed'));
    const onListening = () => {
      try { healthy(); finish(); } catch (error) { finish(error); }
    };
    const timeout = setTimeout(() => finish(new SmokeError('owned_proxy_listen_timeout')), timeoutMs);
    const poll = setInterval(() => { try { healthy(); } catch (error) { finish(error); } }, 10);
    server.once('error', onError); server.once('listening', onListening);
    try { healthy(); server.listen(0, '127.0.0.1'); } catch (error) {
      finish(error instanceof SmokeError ? error : new SmokeError('owned_proxy_listen_failed'));
    }
  });
}
function options() {
  const result = {dry: false, output: null, timeout: 240000};
  const args = process.argv.slice(2);
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--dry-run') result.dry = true;
    else if (args[i] === '--output-dir') {
      check(args[i + 1] && path.isAbsolute(args[i + 1]), 'output_requires_absolute_new_directory');
      result.output = path.resolve(args[++i]);
    } else if (args[i] === '--timeout-seconds') {
      check(/^[0-9]+$/.test(args[i + 1] || ''), 'invalid_timeout');
      const seconds = Number(args[++i]);
      check(seconds >= 30 && seconds <= 300, 'invalid_timeout'); result.timeout = seconds * 1000;
    } else if (args[i] === '--help') {
      process.stdout.write('Usage: node scripts/browser_smoke.mjs [--dry-run] [--output-dir /absolute/NEW-directory] [--timeout-seconds 30..300]\n' +
        'TEST only: fresh synthetic Store, owned loopback DemoProvider service and private Chrome profile.\n' +
        'No model discovery, credentials, existing runtime, external requests, or dependencies installed.\n' +
        'Dry run starts nothing and creates nothing. Default output: new /private/tmp/shopping-browser-* directory.\n' +
        'Retains report/result.json and post-login screenshots; removes only its owned work directory.\n');
      return null;
    } else throw new SmokeError('unsupported_argument');
  }
  return result;
}

// This launcher lives only inside this run's private work directory. Tokens never enter stdout.
const LAUNCHER = String.raw`import json, os, socket, sqlite3, sys
from pathlib import Path
os.umask(0o077)
work = Path(sys.argv[2]).resolve()
source = Path(sys.argv[3]).resolve()
sys.path.insert(0, str(source))
# All modes install this before project imports. Refuse DNS, new outbound TCP,
# UDP/raw sockets and datagram sends; accepted loopback stream responses remain usable.
denied = [0]
def socket_boundary(event, args):
    forbidden = event in {'socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname',
                          'socket.gethostbyaddr', 'socket.getnameinfo', 'socket.sendto', 'socket.sendmsg'}
    if event == 'socket.__new__':
        forbidden = args[1] not in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX) or args[2] != socket.SOCK_STREAM
    if event == 'socket.bind':
        address = args[1]
        forbidden = not (isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'))
    if forbidden:
        denied[0] += 1
        raise OSError('TEST Python outbound networking disabled')
sys.addaudithook(socket_boundary)
def private_json(name, value):
    fd = os.open(work / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out)
if sys.argv[1] == 'audit':
    from shopping_advisor.schemas import digest
    db = sqlite3.connect('file:' + str(work / 'data/state.sqlite') + '?mode=ro', uri=True)
    rows = db.execute('SELECT kind,id,owner,version,payload,content_hash FROM objects').fetchall()
    kinds, products, integrity = {}, [], True
    for kind, oid, owner, version, raw, content_hash in rows:
        payload = json.loads(raw)
        kinds[kind] = kinds.get(kind, 0) + 1
        integrity = integrity and digest([kind, oid, owner, version, payload]) == content_hash
        if kind == 'product':
            products.append({'id': oid, 'hash': content_hash, 'price_cents': payload['price_cents'],
                             'shipping_cents': payload['shipping_cents'], 'synthetic': payload['synthetic']})
    calls = db.execute('SELECT state,usage_kind,tokens FROM calls').fetchall()
    actions = dict(db.execute('SELECT action,count(*) FROM audit GROUP BY action').fetchall())
    result = {'calls': len(calls), 'non_test_calls': sum(c[1] != 'synthetic' for c in calls),
              'unresolved_calls': sum(c[0] in ('started','unknown') for c in calls),
              'synthetic_tokens': sum(c[2] or 0 for c in calls if c[1] == 'synthetic'),
              'object_kinds': kinds, 'audit_actions': actions, 'object_hashes_valid': integrity,
              'products': products, 'outbound_connect_attempts': denied[0]}
    db.close()
    print(json.dumps(result))
    raise SystemExit(0)
from shopping_advisor.storage import Store
from shopping_advisor.demo_provider import DemoProvider
from shopping_advisor.api import create_app
import uvicorn
store, tokens = Store.initialize(work / 'data', now=1900000000)
provider = DemoProvider()
assert provider.is_test_provider is True
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.bind(('127.0.0.1', 0))
sock.listen(128)
port = sock.getsockname()[1]
app = create_app(store, provider, origin=f'http://127.0.0.1:{port}')
private_json('bootstrap.json', {'port': port, 'alice': tokens['alice']})
tokens.clear()
# Uvicorn normally re-raises captured signals before returning; retain a bounded graceful
# exit so the owned launcher's final network summary is always written.
import signal
from contextlib import contextmanager
server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, log_level='critical',
                                      timeout_graceful_shutdown=4))
@contextmanager
def capture_signals():
    old = {sig: signal.signal(sig, server.handle_exit) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
server.capture_signals = capture_signals
try:
    server.run(sockets=[sock])
finally:
    sock.close()
    private_json('service-network.json', {'outbound_connect_attempts': denied[0]})
`;

async function run(config) {
  const report = {kind: 'shopping_browser_smoke', mode: 'TEST', ok: false, terminal: 'failed',
    llm_acceptance: false, business_flow_passed: false, human_review: 'pending', token_provenance: 'fresh Store.initialize synthetic Alice token',
    usage_provenance: 'DemoProvider synthetic accounting, not real model billing',
    steps: [], screenshots: [], external_request_count: 0, children_stopped: false, owned_work_removed: false};
  if (config.dry) {
    Object.assign(report, {ok: true, terminal: 'dry_run', dry_run: true, side_effects: false,
      children_started: 0, cleanup_required: false, output_directory_policy: 'create new only; never reuse',
      flow: ['DOM login Alice', 'new session', '6000-yuan temporary search and proposal', 'exact DOM confirmation',
        'DOM message and valid recommendation', 'complete DOM preferences: 100 yuan', 'old card invalid',
        'DOM message and no_match', 'refresh plus DOM re-login: call count unchanged']});
    process.stdout.write(JSON.stringify(report, null, 2) + '\n'); return;
  }
  let root, work, reportDir, rootId, workId, reportId, token, origin, cdp, sessionId, proxy;
  let phase = 'prepare', stopping = false, cancelled = null, asyncFailure = null;
  const children = [], sockets = new Set(), mutations = [], networkFailures = [];
  const network = {page_requests: 0, foreign_page_requests: 0, denied_proxy_requests: 0,
    forwarded_proxy_requests: 0, proxy_destination_classes: {}, responses: {}};
  function deniedProxy(request) {
    network.denied_proxy_requests++;
    // Diagnostic categories only: never retain arbitrary hosts, paths, headers or URLs.
    const value = request.url || '';
    const category = /accounts\.google\.com/.test(value) ? 'google_accounts'
      : /clients[0-9]*\.google\.com/.test(value) ? 'google_clients'
      : /www\.google\.com/.test(value) ? 'google_www'
      : /optimizationguide-pa\.googleapis\.com/.test(value) ? 'google_optimization'
      : /update\.googleapis\.com/.test(value) ? 'google_update'
      : /googleapis\.com(?::\d+)?(?:\/|$)/.test(value) ? 'googleapis'
      : /google\.com(?::\d+)?(?:\/|$)/.test(value) ? 'google'
      : /gvt[12]\.com(?::\d+)?(?:\/|$)/.test(value) ? 'chrome_component'
      : /chromium\.org(?::\d+)?(?:\/|$)/.test(value) ? 'chromium' : 'other_blocked';
    network.proxy_destination_classes[category] = (network.proxy_destination_classes[category] || 0) + 1;
  }
  const onInt = () => { cancelled = 'cancelled_sigint'; };
  const onTerm = () => { cancelled = 'cancelled_sigterm'; };
  process.on('SIGINT', onInt); process.on('SIGTERM', onTerm);
  const timer = setTimeout(() => { cancelled = 'total_timeout'; }, config.timeout);
  function healthy() {
    if (cancelled) throw new SmokeError(cancelled);
    if (asyncFailure) throw new SmokeError(asyncFailure);
    for (const entry of children) if (!entry.expectedExit && (entry.failed || entry.exited)) {
      throw new SmokeError(entry.name + '_child_stopped_early');
    }
  }
  async function until(code, fn, duration = 30000) {
    const deadline = Date.now() + duration;
    while (Date.now() < deadline) {
      healthy(); const value = await fn(); if (value) return value; await sleep(100);
    }
    throw new SmokeError(code + '_timeout');
  }
  async function privateRead(name) {
    const handle = await open(path.join(work, name), constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const stat = await handle.stat();
      check(stat.isFile() && stat.uid === process.getuid() && (stat.mode & 0o777) === 0o600 && stat.size < 131072,
        'unsafe_private_file');
      return JSON.parse(await handle.readFile('utf8'));
    } finally { await handle.close(); }
  }
  function launch(name, executable, args, collect = false) {
    const child = spawn(executable, args, {cwd: work, detached: true,
      env: {PATH: '/usr/bin:/bin', HOME: path.join(work, 'home'), TMPDIR: path.join(work, 'tmp'),
        XDG_CONFIG_HOME: path.join(work, 'home'), XDG_CACHE_HOME: path.join(work, 'cache'),
        LANG: 'en_US.UTF-8', PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1', PYTHONUNBUFFERED: '1',
        NO_PROXY: '127.0.0.1', no_proxy: '127.0.0.1'},
      stdio: ['ignore', collect ? 'pipe' : 'ignore', 'pipe']});
    const entry = {name, child, expectedExit: collect, failed: false, exited: false, output: '', stderr: ''};
    children.push(entry);
    child.on('error', () => { entry.failed = true; });
    child.on('exit', (code, signal) => { entry.exited = true; entry.code = code; entry.signal = signal; });
    if (collect) child.stdout.on('data', chunk => {
      if (entry.output.length + chunk.length > 131072) asyncFailure = 'child_output_limit';
      else entry.output += chunk.toString('utf8');
    });
    child.stderr.on('data', chunk => {
      // Only extract our Chrome child's local CDP announcement; never publish raw stderr.
      if (name !== 'chrome' || entry.endpoint) return;
      entry.stderr = (entry.stderr + chunk.toString('utf8')).slice(-4096);
      const match = entry.stderr.match(/DevTools listening on (ws:\/\/127\.0\.0\.1:\d+\/devtools\/browser\/[A-Za-z0-9-]+)/);
      if (match) { entry.endpoint = match[1]; entry.stderr = ''; }
    });
    return entry;
  }
  async function audit() {
    const entry = launch('audit', PYTHON, [path.join(work, 'launcher.py'), 'audit', work, path.join(work, 'source')], true);
    await until('audit_exit', () => entry.exited || entry.failed, 10000);
    check(!entry.failed && entry.code === 0, 'audit_failed');
    const result = JSON.parse(entry.output);
    report.audit_outbound_attempts = (report.audit_outbound_attempts || 0) + result.outbound_connect_attempts;
    check(result.outbound_connect_attempts === 0, 'audit_outbound_attempted');
    return result;
  }
  async function connect(endpoint) {
    const socket = new WebSocket(endpoint), pending = new Map(); let sequence = 0;
    socket.addEventListener('error', () => { if (!stopping) asyncFailure = 'cdp_socket_error'; });
    socket.addEventListener('close', () => {
      for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new SmokeError('cdp_closed')); }
      pending.clear(); if (!stopping) asyncFailure = 'cdp_disconnected';
    });
    await until('cdp_open', () => socket.readyState === WebSocket.OPEN, 10000);
    const listeners = new Map();
    socket.addEventListener('message', event => {
      try {
        const value = JSON.parse(event.data);
        if (value.id) {
          const item = pending.get(value.id); if (!item) return;
          clearTimeout(item.timer); pending.delete(value.id);
          if (value.error) item.reject(new SmokeError('cdp_command_rejected')); else item.resolve(value.result);
        } else for (const listener of listeners.get(value.method) || []) {
          Promise.resolve(listener(value.params, value.sessionId)).catch(() => { asyncFailure = 'cdp_event_failed'; });
        }
      } catch { asyncFailure = 'cdp_protocol_error'; }
    });
    return {
      send(method, params = {}, session) {
        if (!stopping) healthy();
        return new Promise((resolve, reject) => {
          const id = ++sequence;
          const timer = setTimeout(() => { pending.delete(id); reject(new SmokeError('cdp_command_timeout')); }, 10000);
          pending.set(id, {resolve, reject, timer});
          try { socket.send(JSON.stringify({id, method, params, ...(session ? {sessionId: session} : {})})); }
          catch { clearTimeout(timer); pending.delete(id); reject(new SmokeError('cdp_send_failed')); }
        });
      },
      on(method, listener) { listeners.set(method, [...(listeners.get(method) || []), listener]); },
      close() { socket.close(); },
    };
  }
  const send = (method, params = {}) => cdp.send(method, params, sessionId);
  async function evaluate(expression) {
    const result = await send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    check(!result.exceptionDetails, 'dom_inspection_failed'); return result.result.value;
  }
  async function api(route) {
    check(/^\/api\/(health|sessions(?:\/[A-Za-z0-9_-]+)?)$/.test(route), 'readonly_route_not_allowed');
    const response = await fetch(origin + route, {method: 'GET', redirect: 'error',
      headers: token ? {Authorization: 'Bearer ' + token} : {}, signal: AbortSignal.timeout(5000)});
    check(response.ok, 'readonly_api_failed'); return response.json();
  }
  const calls = view => view.runs.reduce((sum, run) => sum + run.payload.calls, 0);
  function verifyObject(object) {
    check(object.content_hash === hash([object.kind, object.id, object.owner, object.version, object.payload]),
      'persistent_object_hash_mismatch');
  }
  async function settle(sid) {
    const view = await until('business_run', async () => {
      const value = await api('/api/sessions/' + sid);
      return value.session.payload.active_run === null ? value : false;
    });
    check(!view.session.payload.blocked_unknown, 'blocked_unknown_call');
    check(view.runs.every(run => ['completed', 'waiting_confirmation'].includes(run.payload.state)), 'unexpected_run_terminal');
    verifyObject(view.session);
    await until('dom_session_version', () => evaluate(`document.querySelector('#session-status').textContent.includes(${JSON.stringify('服务端版本 ' + view.session.version)}) && !document.querySelector('#send-message').disabled`));
    return view;
  }
  async function layout(selector, interactive = true) {
    return evaluate(`(() => { const nodes = document.querySelectorAll(${JSON.stringify(selector)});
      if (nodes.length !== 1) return null; const e = nodes[0]; const r = e.getBoundingClientRect();
      const s = getComputedStyle(e); const x = r.x + r.width / 2, y = r.y + r.height / 2;
      const hit = document.elementFromPoint(x, y);
      return r.width > 0 && r.height > 0 && s.visibility === 'visible' && s.display !== 'none'
        && ${interactive ? '!e.disabled && !e.readOnly &&' : ''} x > 0 && y > 0 && x < innerWidth && y < innerHeight
        && (hit === e || e.contains(hit)) ? {x,y,w:r.width,h:r.height} : null;
    })()`);
  }
  async function position(selector, interactive = true) {
    await until('dom_target', () => evaluate(`(() => { const n=document.querySelectorAll(${JSON.stringify(selector)}); return n.length===1 && !n[0].hidden && ${interactive ? '!n[0].disabled' : 'true'}; })()`));
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'center',inline:'center',behavior:'instant'}); true`);
    let last = null;
    return until('stable_layout_hit_test', async () => {
      await evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))');
      const current = await layout(selector, interactive);
      const stable = current && last && Object.keys(current).every(key => Math.abs(current[key] - last[key]) < 0.5);
      last = current; return stable ? current : false;
    });
  }
  async function click(selector) {
    const point = await position(selector), again = await layout(selector);
    check(again && Math.abs(point.x - again.x) < 0.5 && Math.abs(point.y - again.y) < 0.5, 'click_layout_changed');
    await send('Input.dispatchMouseEvent', {type: 'mouseMoved', x: point.x, y: point.y});
    await send('Input.dispatchMouseEvent', {type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1});
    await send('Input.dispatchMouseEvent', {type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1});
  }
  async function key(key, code, keyCode, extra = {}) {
    await send('Input.dispatchKeyEvent', {type: 'keyDown', key, code, windowsVirtualKeyCode: keyCode, ...extra});
    await send('Input.dispatchKeyEvent', {type: 'keyUp', key, code, windowsVirtualKeyCode: keyCode, modifiers: extra.modifiers || 0});
  }
  async function focused(selector) {
    await until('input_focus', () => evaluate(`document.activeElement === document.querySelector(${JSON.stringify(selector)})`));
  }
  async function fill(selector, value) {
    await click(selector); await focused(selector);
    // Native editor commands are delivered with a real CDP key event, never JS value assignment.
    await key('a', 'KeyA', 65, {modifiers: 4, commands: ['selectAll']});
    await key('Backspace', 'Backspace', 8);
    if (value) await send('Input.insertText', {text: value});
    check(await evaluate(`document.activeElement === document.querySelector(${JSON.stringify(selector)}) && document.activeElement.value === ${JSON.stringify(value)}`), 'input_not_delivered');
  }
  async function select(selector, value) {
    const details = await evaluate(`(() => { const e=document.querySelector(${JSON.stringify(selector)}); return {value:e.value,index:[...e.options].findIndex(o=>o.value===${JSON.stringify(value)})}; })()`);
    check(details.index >= 0, 'select_option_missing');
    if (details.value === value) return;
    await click(selector); await focused(selector);
    const trace = [];
    const record = async () => trace.push(await evaluate(`(() => {const e=document.querySelector(${JSON.stringify(selector)}); return {index:e.selectedIndex, options:e.options.length, focused:document.activeElement===e, disabled:e.disabled};})()`));
    await record();
    // A character key selects the exact option prefix through Chrome's native select.
    await key(value[0], 'Key' + value[0].toUpperCase(), value[0].toUpperCase().charCodeAt(0), {text: value[0], unmodifiedText: value[0]});
    await record(); await key('Enter', 'Enter', 13); await record();
    report.select_diagnostics = trace;
    await until('select_value', () => evaluate(`document.querySelector(${JSON.stringify(selector)}).value === ${JSON.stringify(value)}`), 5000);
  }
  async function card(object, valid, phrases = []) {
    verifyObject(object);
    const selector = `[data-kind="${object.kind}"][data-id="${object.id}"]`;
    await until('card_state', () => evaluate(`(() => { const e=document.querySelector(${JSON.stringify(selector)}); return e && e.dataset.valid===${JSON.stringify(String(valid))}
      && e.dataset.hash===${JSON.stringify(object.content_hash)} && e.dataset.version===${JSON.stringify(String(object.version))}
      && ${JSON.stringify(phrases)}.every(text=>e.innerText.includes(text)); })()`));
    return selector;
  }
  async function screenshot(name, selector) {
    check(await evaluate(`document.body.dataset.ready==='true' && document.querySelector('#token').value===''`), 'unsafe_screenshot_login_state');
    // No passwords, bearer values, model context or raw browser console are retained.
    check(await evaluate(`!document.body.innerText.includes(${JSON.stringify(token)}) && ![...document.querySelectorAll('input,textarea')].some(e=>e.value.includes(${JSON.stringify(token)}))`), 'unsafe_screenshot_secret');
    if (selector) {
      await evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'start',behavior:'instant'}); true`);
      await evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))');
    }
    const image = await send('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    check(owned(await lstat(reportDir), reportId), 'report_directory_changed');
    const filename = path.join(reportDir, name + '.png');
    await writeFile(filename, Buffer.from(image.data, 'base64'), {mode: 0o600, flag: 'wx'});
    report.screenshots.push(filename);
  }
  async function login() {
    await fill('#token', token); await click('#login');
    await until('login_ready', () => evaluate(`document.body.dataset.ready==='true' && document.querySelector('#token').value==='' && document.querySelector('#identity').textContent.includes('alice') && document.querySelector('#mode-banner').textContent.includes('TEST')`));
  }
  async function message(sid, text) {
    await until('message_enabled', () => evaluate(`!document.querySelector('#send-message').disabled`));
    await fill('#message-text', text); await click('#send-message');
    await until('message_persisted', async () => (await api('/api/sessions/' + sid)).session.payload.messages.some(m => m.role === 'customer' && m.text === text));
    return settle(sid);
  }
  async function quiet(sid, count) {
    const deadline = Date.now() + 2200;
    do {
      healthy(); const value = await api('/api/sessions/' + sid);
      check(calls(value) === count && !value.session.payload.active_run, 'readonly_operation_started_model');
      await sleep(250);
    } while (Date.now() < deadline);
    await until('dom_call_count', () => evaluate(`document.querySelector('#model-calls').textContent === ${JSON.stringify(String(count))}`));
  }
  function mutation(route) {
    const list = mutations.filter(item => item.route === route);
    check(list.length === 1, 'mutation_missing_or_repeated'); return list[0].body;
  }
  async function snapshotSource() {
    const source = path.join(PROJECT, 'src', 'shopping_advisor');
    const destination = path.join(work, 'source', 'shopping_advisor');
    await mkdir(path.dirname(destination), {mode: 0o700});
    const files = [];
    async function visit(relative = '') {
      const directory = path.join(source, relative);
      const stat = await lstat(directory);
      check(stat.isDirectory() && !stat.isSymbolicLink(), 'unsafe_source_directory');
      await mkdir(path.join(destination, relative), {mode: 0o700});
      for (const item of (await readdir(directory, {withFileTypes: true})).sort((a, b) => a.name.localeCompare(b.name))) {
        if (item.name === '__pycache__') continue;
        const name = path.join(relative, item.name);
        check(!item.isSymbolicLink(), 'source_symlink_rejected');
        if (item.isDirectory()) await visit(name);
        else {
          check(item.isFile() && /\.(py|html|js|css)$/.test(item.name), 'unexpected_source_file');
          const handle = await open(path.join(source, name), constants.O_RDONLY | constants.O_NOFOLLOW);
          let contents;
          try {
            const info = await handle.stat(); check(info.isFile() && info.size < 2097152, 'source_size_limit');
            contents = await handle.readFile();
          } finally { await handle.close(); }
          await writeFile(path.join(destination, name), contents, {mode: 0o600, flag: 'wx'});
          files.push({path: 'src/shopping_advisor/' + name, sha256: createHash('sha256').update(contents).digest('hex')});
        }
      }
    }
    await visit();
    // Fail closed on concurrent edits during capture; execute only this private snapshot.
    for (const item of files) {
      check(createHash('sha256').update(await readFile(path.join(PROJECT, item.path))).digest('hex') === item.sha256,
        'source_changed_during_snapshot');
    }
    report.source_snapshot = {script_sha256: createHash('sha256').update(await readFile(fileURLToPath(import.meta.url))).digest('hex'),
      manifest_sha256: hash(files), files, execution: 'owned_private_copy'};
  }
  try {
    await access(PYTHON, constants.X_OK); await access(CHROME, constants.X_OK);
    if (config.output) {
      const parent = await realpath(path.dirname(config.output));
      root = path.join(parent, path.basename(config.output));
      await mkdir(root, {mode: 0o700});
    } else root = await mkdtemp('/private/tmp/shopping-browser-');
    await chmod(root, 0o700); rootId = ident(await lstat(root));
    work = path.join(root, 'work'); reportDir = path.join(root, 'report');
    await mkdir(work, {mode: 0o700}); workId = ident(await lstat(work));
    await mkdir(reportDir, {mode: 0o700}); reportId = ident(await lstat(reportDir));
    for (const name of ['home', 'tmp', 'cache', 'chrome-profile']) await mkdir(path.join(work, name), {mode: 0o700});
    await mkdir(path.join(work, 'chrome-profile', 'Default'), {mode: 0o700});
    await writeFile(path.join(work, 'chrome-profile', 'Default', 'Preferences'), JSON.stringify({
      signin: {allowed: false, allowed_on_next_startup: false}, credentials_enable_service: false,
      profile: {password_manager_enabled: false}, safebrowsing: {enabled: false, enhanced: false},
      search: {suggest_enabled: false}, translate: {enabled: false},
      net: {network_prediction_options: 2}, browser: {check_default_browser: false},
    }), {mode: 0o600, flag: 'wx'});
    await writeFile(path.join(work, 'chrome-profile', 'Local State'), JSON.stringify({
      browser: {enabled_labs_experiments: []}, signin: {allowed: false},
      user_experience_metrics: {reporting_enabled: false},
    }), {mode: 0o600, flag: 'wx'});
    await writeFile(path.join(work, 'launcher.py'), LAUNCHER, {mode: 0o600, flag: 'wx'});
    await snapshotSource();
    phase = 'start_test_service';
    launch('service', PYTHON, [path.join(work, 'launcher.py'), 'serve', work, path.join(work, 'source')]);
    const bootstrap = await until('service_bootstrap', async () => {
      try { return await privateRead('bootstrap.json'); } catch (error) { if (error.code === 'ENOENT') return false; throw error; }
    });
    check(Number.isInteger(bootstrap.port) && bootstrap.port > 0 && bootstrap.port <= 65535
      && /^[A-Za-z0-9_-]{32,128}$/.test(bootstrap.alice), 'invalid_owned_bootstrap');
    token = bootstrap.alice; delete bootstrap.alice; origin = 'http://127.0.0.1:' + bootstrap.port;
    await until('test_service_health', async () => {
      try { const health = await api('/api/health'); return health.status === 'ok' && health.test_mode === true; }
      catch (error) { if (error instanceof TypeError) return false; throw error; }
    });
    // A rejecting local proxy also blocks browser background HTTP(S), without forwarding anything.
    proxy = createServer((request, response) => {
      deniedProxy(request); request.resume(); response.writeHead(403, {'Connection': 'close'}); response.end();
    });
    proxy.on('connect', (request, socket) => { deniedProxy(request); socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n'); });
    proxy.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
    proxy.on('error', () => { asyncFailure = 'owned_proxy_error'; });
    await listenOwned(proxy, healthy);
    const proxyPort = proxy.address().port;
    phase = 'start_private_chrome';
    const chrome = launch('chrome', CHROME, ['--headless=new', '--disable-background-networking', '--disable-sync',
      '--no-first-run', '--no-default-browser-check', '--disable-component-update', '--disable-default-apps',
      '--disable-domain-reliability', '--disable-client-side-phishing-detection', '--disable-breakpad',
      '--disable-crash-reporter', '--disable-extensions', '--disable-quic', '--disable-background-timer-throttling',
      '--disable-features=MediaRouter,OptimizationHints,OptimizationHintsFetching,OptimizationGuideModelDownloading,OptimizationTargetPrediction,NetworkTimeServiceQuerying,AutofillServerCommunication,CertificateTransparencyComponentUpdater,SigninAllowed,AccountConsistency,ChromeWhatsNewUI,SearchEnginePreconnect,SearchEnginePreconnect2',
      '--disable-background-mode', '--disable-gaia-services', '--disable-signin-scoped-device-id',
      '--disable-preconnect', '--disable-ipv6',
      '--disable-component-extensions-with-background-pages', '--disable-search-engine-choice-screen',
      '--disable-domain-reliability', '--disable-gpu', '--disable-software-rasterizer',
      '--disable-notifications', '--disable-default-browser-promo', '--no-service-autorun', '--test-type', '--enable-automation',
      '--safebrowsing-disable-auto-update', '--metrics-recording-only', '--no-pings', '--password-store=basic',
      '--use-mock-keychain', '--force-color-profile=srgb', '--window-size=1440,1100',
      `--proxy-server=http://127.0.0.1:${proxyPort}`, '--proxy-bypass-list=127.0.0.1',
      '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
      '--force-webrtc-ip-handling-policy=disable_non_proxied_udp', `--user-data-dir=${path.join(work, 'chrome-profile')}`,
      '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0', 'about:blank']);
    const endpoint = await until('owned_cdp_endpoint', () => chrome.endpoint);
    cdp = await connect(endpoint);
    const target = await cdp.send('Target.createTarget', {url: 'about:blank'});
    ({sessionId} = await cdp.send('Target.attachToTarget', {targetId: target.targetId, flatten: true}));
    cdp.on('Network.requestWillBeSent', (params, sid) => {
      if (sid !== sessionId || params.request.url === 'about:blank') return;
      network.page_requests++;
      const url = new URL(params.request.url);
      if (url.origin !== origin) { network.foreign_page_requests++; return; }
      if (params.request.method === 'POST') {
        check(/^\/api\/(sessions(?:\/[A-Za-z0-9_-]+\/(messages|preferences))?|proposals\/[A-Za-z0-9_-]+\/decision)$/.test(url.pathname), 'unexpected_browser_mutation');
        mutations.push({route: url.pathname, body: JSON.parse(params.request.postData || '{}')});
      }
    });
    cdp.on('Network.responseReceived', (params, sid) => {
      if (sid !== sessionId) return;
      const url = new URL(params.response.url);
      if (url.origin !== origin) return;
      const route = url.pathname.replace(/\/(sess|pref)-[A-Za-z0-9_-]+/g, '/:id');
      check(/^\/(?:api|static)\/|^\/$|^\/favicon.ico$/.test(route), 'unexpected_response_route');
      const name = `${route} ${params.response.status}`;
      network.responses[name] = (network.responses[name] || 0) + 1;
      if (params.response.status >= 400 && url.pathname !== '/favicon.ico') networkFailures.push(params.response.status);
    });
    cdp.on('Fetch.requestPaused', async (params, sid) => {
      if (sid !== sessionId) return;
      const url = new URL(params.request.url);
      if (url.origin !== origin) await send('Fetch.failRequest', {requestId: params.requestId, errorReason: 'BlockedByClient'});
      else await send('Fetch.continueRequest', {requestId: params.requestId});
    });
    cdp.on('Runtime.exceptionThrown', () => { asyncFailure = 'page_javascript_exception'; });
    await send('Page.enable'); await send('Runtime.enable'); await send('Network.enable');
    await send('Fetch.enable', {patterns: [{urlPattern: '*', requestStage: 'Request'}]});
    await send('Page.navigate', {url: origin + '/'});
    phase = 'dom_login_and_new_session'; await login(); await click('#new-session');
    const sid = await until('new_session_dom', () => evaluate(`(() => { const id=document.querySelector('#session-list').value;
      return id && document.querySelector('#session-status').textContent.includes(id) && !document.querySelector('#send-message').disabled ? id : false; })()`));
    check(/^sess-[A-Za-z0-9_-]+$/.test(sid), 'invalid_session_id');
    report.session_id = sid;
    check((await api('/api/sessions')).length === 1, 'fresh_store_not_single_session');
    report.steps.push('actual_dom_alice_login_and_new_session');
    phase = 'temporary_search_and_proposal';
    const draftView = await message(sid, INITIAL_MESSAGE);
    check(draftView.session.payload.preferences === null && draftView.session.payload.preferences_revision === 0,
      'preferences_applied_before_confirmation');
    const proposals = draftView.proposals.filter(p => p.display_valid && p.payload.state === 'pending_confirmation');
    check(proposals.length === 1, 'expected_one_pending_proposal'); const proposal = proposals[0];
    check(equal(proposal.payload.preferences, BASE_PREFS), 'draft_preferences_mismatch');
    const temporary = draftView.evidences.find(e => e.payload.tool === 'search_products' && e.payload.confirmed === false);
    check(temporary?.display_valid && temporary.payload.data.eligible.length > 0, 'temporary_search_missing');
    await card(temporary, true, ['临时条件，尚未经用户确认']);
    const proposalSelector = await card(proposal, true, ['6000.00', '办公', '价格']);
    const beforeConfirm = calls(draftView);
    const button = proposalSelector + ' [data-action="confirm"]';
    check(await evaluate(`document.querySelector(${JSON.stringify(button)}).textContent==='精确确认此草案（不启动模型）'`), 'not_exact_confirmation_button');
    phase = 'exact_dom_confirmation'; await click(button);
    const confirmed = await until('proposal_confirmed', async () => {
      const view = await api('/api/sessions/' + sid);
      return view.proposals.find(p => p.id === proposal.id)?.payload.state === 'confirmed' ? view : false;
    });
    check(equal(confirmed.session.payload.preferences, BASE_PREFS) && confirmed.session.payload.preferences_revision === 1,
      'confirmation_did_not_apply_exact_preferences');
    const confirmedProposal = confirmed.proposals.find(p => p.id === proposal.id);
    check(confirmedProposal.version === proposal.version + 1 && confirmedProposal.content_hash !== proposal.content_hash,
      'proposal_version_hash_not_changed'); verifyObject(confirmedProposal); verifyObject(confirmed.session);
    check(confirmed.session.version === draftView.session.version + 2 && confirmed.session.content_hash !== draftView.session.content_hash,
      'confirmation_session_version_hash_mismatch');
    const decision = mutation('/api/proposals/' + proposal.id + '/decision');
    check(decision.expected_version === proposal.version && decision.content_hash === proposal.content_hash && decision.decision === 'confirm',
      'browser_confirmation_binding_mismatch');
    await quiet(sid, beforeConfirm);
    report.steps.push('temporary_search_then_exact_dom_proposal_confirmation_no_autorun');
    phase = 'current_recommendation';
    const recommendationView = await message(sid, NEXT_MESSAGE);
    const recommendations = recommendationView.recommendations.filter(r => r.display_valid && r.payload.state === 'current');
    check(recommendations.length === 1, 'expected_one_current_recommendation'); const recommendation = recommendations[0];
    const selected = recommendation.payload.selection, product = selected.product;
    check(product.synthetic === true && Number.isSafeInteger(selected.total_cents)
      && selected.total_cents === product.price_cents + product.shipping_cents && selected.total_cents <= 600000,
    'recommendation_total_cost_invalid');
    check(equal(recommendation.payload.preferences, BASE_PREFS) && recommendation.payload.bindings.preferences_revision === 1
      && recommendation.payload.model_text_published === false, 'recommendation_preferences_or_rendering_invalid');
    const evaluation = recommendationView.evaluations.find(e => e.id === recommendation.payload.evaluation_id);
    check(evaluation?.display_valid && evaluation.payload.data.eligible.some(row => equal(row, selected)), 'recommendation_evaluation_missing');
    verifyObject(evaluation);
    const recSelector = await card(recommendation, true, ['当前有效', '商品 ¥' + money(product.price_cents),
      '运费 ¥' + money(product.shipping_cents), '总成本 ¥' + money(selected.total_cents), '未下单或付款']);
    await screenshot('01-current-recommendation', recSelector);
    report.recommendation = {id: recommendation.id, product_id: product.id, price_cents: product.price_cents,
      shipping_cents: product.shipping_cents, total_cents: selected.total_cents, budget_cents: 600000,
      preferences_revision: 1, content_hash: recommendation.content_hash};
    report.steps.push('current_dom_card_matches_verified_evaluation_price_plus_shipping');
    phase = 'complete_dom_preferences_edit';
    const beforeEdit = await settle(sid), beforeEditCalls = calls(beforeEdit);
    await fill('#budget-yuan', '100');
    await select('#use', BASE_PREFS.use); await select('#priority', BASE_PREFS.priority);
    await fill('#min-ram-gb', '8'); await fill('#min-storage-gb', '256');
    await fill('#max-weight-g', ''); await fill('#min-battery-minutes', '');
    await select('#os', 'any');
    if (await evaluate(`document.querySelector('#needs-discrete-gpu').checked`)) await click('#needs-discrete-gpu');
    const lowPrefs = {...BASE_PREFS, budget_cents: 10000};
    await click('#save-preferences');
    const edited = await until('edited_preferences', async () => {
      const view = await api('/api/sessions/' + sid);
      return view.session.payload.preferences_revision === 2 ? view : false;
    });
    check(equal(edited.session.payload.preferences, lowPrefs), 'complete_preferences_not_preserved');
    check(edited.session.version === beforeEdit.session.version + 2 && edited.session.content_hash !== beforeEdit.session.content_hash,
      'edit_session_version_hash_mismatch'); verifyObject(edited.session);
    const command = mutation('/api/sessions/' + sid + '/preferences');
    check(equal(Object.keys(command.preferences).sort(), [...PREF_KEYS].sort()) && equal(command.preferences, lowPrefs)
      && command.expected_version === beforeEdit.session.version, 'browser_complete_preferences_command_invalid');
    const old = edited.recommendations.find(r => r.id === recommendation.id);
    check(old && !old.display_valid && old.payload.state === 'superseded' && old.version === recommendation.version + 1
      && old.content_hash !== recommendation.content_hash, 'old_recommendation_not_invalidated');
    await card(old, false, ['历史记录 / 无效，不可作为当前建议']);
    await quiet(sid, beforeEditCalls); await screenshot('02-invalidated-recommendation', recSelector);
    report.preferences = {confirmed_revision: 1, edited_revision: 2,
      confirmed_hash: hash(BASE_PREFS), edited_hash: hash(lowPrefs),
      session_version_before_edit: beforeEdit.session.version, session_version_after_edit: edited.session.version,
      session_hash_before_edit: beforeEdit.session.content_hash, session_hash_after_edit: edited.session.content_hash,
      final_budget_cents: 10000, complete_field_count: PREF_KEYS.length};
    report.steps.push('complete_dom_form_changes_budget_to_100_old_card_visible_invalid_no_autorun');
    phase = 'no_match_without_budget_relaxation';
    const finalView = await message(sid, LOW_MESSAGE);
    check(equal(finalView.session.payload.preferences, lowPrefs) && finalView.session.payload.preferences_revision === 2,
      'budget_or_preferences_silently_relaxed');
    const noMatch = finalView.session.payload.messages.filter(m => m.kind === 'no_match');
    check(noMatch.length === 1 && noMatch[0].evidence_ids.length > 0, 'no_match_message_missing');
    for (const id of noMatch[0].evidence_ids) {
      const evidence = finalView.evidences.find(e => e.id === id);
      check(evidence?.display_valid && evidence.payload.tool === 'search_products' && evidence.payload.confirmed === true
        && equal(evidence.payload.preferences, lowPrefs) && evidence.payload.data.eligible.length === 0,
      'no_match_not_backed_by_current_empty_search');
      await card(evidence, true, ['100.00', '无合格商品；预算不会自动增加']);
    }
    check(finalView.recommendations.length === 1 && finalView.recommendations.every(r => !r.display_valid), 'unexpected_recommendation_after_no_match');
    await until('no_match_dom', () => evaluate(`document.querySelector('[data-message-kind="no_match"]')?.textContent.includes('预算不会自动提高')`));
    await screenshot('03-no-match', '[data-message-kind="no_match"]');
    report.steps.push('no_match_dom_and_current_empty_search_keep_exact_100_yuan_budget');
    phase = 'refresh_without_model_calls';
    const beforeRefresh = await audit();
    check(beforeRefresh.calls === calls(finalView), 'persisted_call_count_mismatch');
    await click('#refresh'); await quiet(sid, beforeRefresh.calls);
    await send('Page.reload', {ignoreCache: true});
    await until('reload_logged_out', () => evaluate(`document.readyState==='complete' && document.body.dataset.ready!=='true' && document.querySelector('#token')?.value===''`));
    await login();
    await until('restored_session_option', () => evaluate(`[...document.querySelector('#session-list').options].some(o=>o.value===${JSON.stringify(sid)})`));
    await select('#session-list', sid); await settle(sid); await quiet(sid, beforeRefresh.calls);
    const persisted = await api('/api/sessions/' + sid), afterRefresh = await audit();
    check(equal(persisted.session, finalView.session) && afterRefresh.calls === beforeRefresh.calls, 'refresh_mutated_state_or_calls');
    check(afterRefresh.non_test_calls === 0 && afterRefresh.unresolved_calls === 0 && afterRefresh.object_hashes_valid,
      'persistent_audit_failed');
    const originalProduct = afterRefresh.products.find(p => p.id === product.id);
    check(originalProduct?.synthetic && originalProduct.price_cents === product.price_cents
      && originalProduct.shipping_cents === product.shipping_cents, 'total_not_from_persistent_catalog');
    const allowedKinds = new Set(['product', 'session', 'run', 'proposal', 'evidence', 'evaluation', 'recommendation']);
    check(Object.keys(afterRefresh.object_kinds).every(kind => allowedKinds.has(kind)), 'unexpected_business_object_or_order');
    check(Object.keys(afterRefresh.audit_actions).every(action => ['create_session', 'message', 'decide_proposal', 'set_preferences'].includes(action)),
      'unexpected_audit_action_or_order');
    mutation('/api/sessions');
    const messages = mutations.filter(item => item.route === '/api/sessions/' + sid + '/messages');
    check(messages.length === 3 && equal(messages.map(m => m.body.text), [INITIAL_MESSAGE, NEXT_MESSAGE, LOW_MESSAGE]),
      'browser_message_sequence_invalid');
    check(mutations.length === 6 && new Set(mutations.map(m => m.body.action_id)).size === 6, 'unexpected_or_repeated_browser_actions');
    check(networkFailures.length === 0, 'browser_http_failure');
    await card(persisted.recommendations[0], false, ['历史记录 / 无效，不可作为当前建议']);
    report.calls = {before_refresh: beforeRefresh.calls, after_refresh: afterRefresh.calls,
      real_model_calls: afterRefresh.non_test_calls, synthetic_tokens: afterRefresh.synthetic_tokens};
    report.non_execution_checks = {no_order_objects: true, no_order_audit_actions: true, browser_post_count: mutations.length,
      original_synthetic_catalog_cost_verified: true, server_object_hashes_verified: true};
    report.steps.push('page_refresh_reload_dom_relogin_and_session_selection_do_not_add_calls');
    healthy(); report.business_flow_passed = true; report.ok = true; report.terminal = 'succeeded';
  } catch (error) {
    report.failure = {phase, code: error instanceof SmokeError ? error.code : 'unexpected_local_error'};
    report.terminal = cancelled?.startsWith('cancelled') ? 'cancelled' : cancelled === 'total_timeout' ? 'timeout' : 'failed';
  } finally {
    stopping = true; clearTimeout(timer); token = null;
    if (cdp) { try { cdp.close(); } catch { /* Stop the owned child group below. */ } }
    let stopped = true;
    for (const entry of [...children].reverse()) {
      if (!entry.child.pid) continue;
      const exists = () => {
        try { process.kill(-entry.child.pid, 0); return true; } catch (error) { return error.code !== 'ESRCH'; }
      };
      for (const signal of ['SIGTERM', 'SIGKILL']) {
        if (!exists()) break;
        try { process.kill(-entry.child.pid, signal); } catch (error) { if (error.code !== 'ESRCH') stopped = false; }
        const deadline = Date.now() + 5000;
        while (exists() && Date.now() < deadline) await sleep(100);
      }
      if (exists()) stopped = false;
    }
    if (proxy) {
      for (const socket of sockets) socket.destroy();
      proxy.closeAllConnections();
      await Promise.race([new Promise(resolve => proxy.close(resolve)), sleep(2000)]);
      if (proxy.listening) stopped = false;
    }
    report.children_stopped = stopped;
    report.children = children.map(entry => ({name: entry.name, exited: entry.exited, exit_code: entry.code ?? null, signal: entry.signal ?? null}));
    if (work && children.some(entry => entry.name === 'service')) {
      try { report.service_network = await privateRead('service-network.json'); }
      catch { report.service_network = {verification: 'unavailable'}; if (report.ok) report.failure = {phase: 'cleanup', code: 'service_network_audit_unavailable'}; report.ok = false; }
    }
    report.external_request_count = network.foreign_page_requests + network.denied_proxy_requests
      + (report.service_network?.outbound_connect_attempts || 0) + (report.audit_outbound_attempts || 0);
    report.network = network;
    if (report.external_request_count !== 0) {
      report.ok = false; report.network_failure = 'external_request_attempted';
      report.failure ??= {phase: 'network', code: 'external_request_attempted'};
    }
    if (!stopped) { report.ok = false; report.cleanup_failure = 'owned_child_group_not_stopped'; }
    if (work && stopped && workId) {
      try {
        check(owned(await lstat(root), rootId) && owned(await lstat(work), workId), 'cleanup_identity_changed');
        check(await realpath(work) === path.join(root, 'work'), 'cleanup_path_changed');
        await rm(work, {recursive: true, force: false}); report.owned_work_removed = true;
      } catch { report.ok = false; report.cleanup_failure = 'owned_work_cleanup_failed'; }
    }
    if (!report.ok && report.terminal === 'succeeded') report.terminal = 'failed';
    process.off('SIGINT', onInt); process.off('SIGTERM', onTerm);
    if (reportDir && reportId) {
      report.report_dir = reportDir;
      report.result_path = path.join(reportDir, 'result.json');
      try {
        check(owned(await lstat(root), rootId) && owned(await lstat(reportDir), reportId), 'report_identity_changed');
        await writeFile(report.result_path, JSON.stringify(report, null, 2) + '\n', {mode: 0o600, flag: 'wx'});
      } catch { report.ok = false; report.terminal = 'failed'; report.report_failure = 'report_write_failed'; }
    }
    process.stdout.write(JSON.stringify(report, null, 2) + '\n'); process.exitCode = report.ok ? 0 : 1;
  }
}
try { const config = options(); if (config) await run(config); }
catch (error) {
  process.stdout.write(JSON.stringify({kind: 'shopping_browser_smoke', mode: 'TEST', ok: false, terminal: 'failed',
    human_review: 'pending', llm_acceptance: false, failure: {code: error instanceof SmokeError ? error.code : 'unexpected_local_error'}}) + '\n');
  process.exitCode = 1;
}
