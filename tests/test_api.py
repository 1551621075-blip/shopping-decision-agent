"""In-process API tests: real Store/Actions/Service, never an HTTP listener."""
import asyncio
import importlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from shopping_advisor.provider import LocalHTTPProvider, ProviderError, ScriptedProvider
from shopping_advisor.schemas import Preferences
from shopping_advisor.storage import Store

ORIGIN = "http://127.0.0.1:8765"
PREFERENCES = Preferences(budget_cents=600000, use="coding").model_dump()
NODE = shutil.which("node")


def test_api_entrypoint_exists():
    assert importlib.util.find_spec("shopping_advisor.api") is not None, "API entrypoint is missing"


@pytest.fixture
def api(tmp_path):
    module = importlib.import_module("shopping_advisor.api")
    store, tokens = Store.initialize(tmp_path / "runtime", now=1800000000)
    provider = ScriptedProvider([{"tool": "ask_shopper", "arguments": {
        "question": "主要用于办公、编程、创作还是游戏？"}}])
    app = module.create_app(store, provider, origin=ORIGIN)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, store, tokens, provider, app


@pytest.mark.parametrize("same_provider", [False, True])
def test_factory_rejects_provider_and_service_together(api, same_provider):
    module = importlib.import_module("shopping_advisor.api")
    service = api[4].state.service
    provider = service.provider if same_provider else LocalHTTPProvider("http://127.0.0.1:9999", "explicit-test")
    with pytest.raises(ValueError, match="互斥"):
        module.create_app(api[1], provider=provider, service=service, origin=ORIGIN)
    assert api[3].seen_contexts == []


def test_factory_rejects_service_from_different_store(api, tmp_path):
    module = importlib.import_module("shopping_advisor.api")
    other_store, _ = Store.initialize(tmp_path / "other-runtime", now=1800000000)
    with pytest.raises(ValueError, match="同一Store"):
        module.create_app(other_store, service=api[4].state.service, origin=ORIGIN)
    assert api[3].seen_contexts == []


def test_service_only_injection_uses_actual_mode_and_preserves_caller_lifecycle(api):
    module = importlib.import_module("shopping_advisor.api")
    service = api[4].state.service
    application = module.create_app(api[1], service=service, origin=ORIGIN)
    with TestClient(application, base_url=ORIGIN) as client:
        assert client.get("/api/me", headers=headers(api)).json()["test_mode"] is True
        assert client.get("/api/health").json()["test_mode"] is True
        obj = client.post("/api/sessions", headers=headers(api, mutation=True),
                          json={"action_id": "external-service"}).json()
    # Closing the injected app must not close its caller-owned Service.
    result = post(api, "/api/sessions/" + obj["id"] + "/messages", {
        "action_id": "after-external-app", "expected_version": obj["version"], "text": "帮我选电脑"})
    assert result.status_code == 200
    service.close()
    assert len(api[3].seen_contexts) == 1
    assert api[3].seen_contexts[0]["mode"] == "TEST"


@pytest.mark.parametrize("origin", ["http://127.0.0.1", "http://127.0.0.1:80"])
def test_default_http_port_normalizes_configuration_not_request_headers(tmp_path, origin):
    module = importlib.import_module("shopping_advisor.api")
    store, tokens = Store.initialize(tmp_path / "runtime", now=1800000000)
    app = module.create_app(store, ScriptedProvider([]), origin=origin)
    with TestClient(app, base_url="http://127.0.0.1:80") as client:
        auth = {"Authorization": "Bearer " + tokens["alice"]}
        assert client.get("/api/me", headers=auth).status_code == 200
        mutation = {**auth, "Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
        assert client.post("/api/sessions", headers=mutation, json={"action_id": "port-80"}).status_code == 200
        # Configuration is canonicalized once; uncanonical or mismatched incoming headers stay rejected.
        assert client.get("/api/me", headers={**auth, "Host": "127.0.0.1:80"}).status_code == 400
        for bad_origin in ("http://127.0.0.1:80", "http://127.0.0.1:81", "http://localhost"):
            assert client.post("/api/sessions", headers={**mutation, "Origin": bad_origin},
                               json={"action_id": "bad-origin"}).status_code == 403


def headers(api, user="alice", *, mutation=False):
    value = {"Authorization": "Bearer " + api[2][user]}
    if mutation:
        value.update({"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
                      "Sec-Fetch-Dest": "empty", "Content-Type": "application/json"})
    return value


def post(api, path, body, user="alice"):
    return api[0].post(path, json=body, headers=headers(api, user, mutation=True))


def session(api):
    response = post(api, "/api/sessions", {"action_id": "create-one"})
    assert response.status_code == 200
    return response.json()


def test_identity_health_and_read_only_polling(api):
    client, store, _, provider, _ = api
    assert client.get("/api/health").json() == {"status": "ok", "test_mode": True}
    assert client.get("/api/me", headers=headers(api)).json() == {
        "user_id": "alice", "role": "customer", "test_mode": True}
    obj = session(api)
    for _ in range(3):
        view = client.get("/api/sessions/" + obj["id"], headers=headers(api)).json()
        assert view["session"] == obj
        assert set(view) >= {"proposals", "evidences", "evaluations", "recommendations", "runs"}
    assert provider.seen_contexts == []
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM calls").fetchone()[0] == 0


def test_customers_and_operator_are_separated(api):
    obj = session(api)
    assert api[0].get("/api/sessions/" + obj["id"], headers=headers(api, "bob")).status_code == 404
    assert api[0].get("/api/sessions/" + obj["id"], headers=headers(api, "operator")).status_code == 403
    assert post(api, "/api/sessions", {"action_id": "operator-create"}, "operator").status_code == 403
    assert api[0].get("/api/sessions", headers=headers(api, "bob")).json() == []
    assert api[0].get("/api/sessions", headers=headers(api, "operator")).status_code == 403
    for path in ("/api/catalog", "/api/usage/unknown"):
        assert api[0].get(path, headers=headers(api)).status_code == 403
        assert api[0].get(path, headers=headers(api, "operator")).status_code == 200


def test_complete_preferences_return_authoritative_version_and_do_not_call_model(api):
    obj = session(api)
    path = "/api/sessions/" + obj["id"] + "/preferences"
    body = {"action_id": "prefs", "expected_version": obj["version"], "preferences": PREFERENCES}
    result = post(api, path, body)
    assert result.status_code == 200
    assert result.json()["version"] == obj["version"] + 2
    assert result.json()["payload"]["preferences"] == PREFERENCES
    assert post(api, path, body).json() == result.json()
    assert api[3].seen_contexts == []
    partial = {**body, "action_id": "partial", "preferences": {"budget_cents": 600000, "use": "coding"}}
    assert post(api, path, partial).status_code == 422


def test_proposal_decisions_bind_exact_id_version_hash_and_preserve_history(api):
    obj = session(api)
    actions = api[4].state.service.actions
    principal = api[1].authenticate(api[2]["alice"])
    with api[1].transaction() as db:
        proposal = actions.propose_preferences(db, principal, obj["id"], PREFERENCES)
    path = "/api/proposals/" + proposal["id"] + "/decision"
    body = {"action_id": "confirm", "expected_version": proposal["version"],
            "content_hash": proposal["content_hash"], "decision": "confirm"}
    for change in ({"content_hash": "0" * 64}, {"expected_version": 9}):
        assert post(api, path, {**body, **change}).status_code == 409
    assert post(api, path, body, "bob").status_code == 404
    assert post(api, path, body, "operator").status_code == 403
    response = post(api, path, body)
    assert response.status_code == 200
    assert post(api, path, body).json() == response.json()
    assert response.json()["session"]["version"] == obj["version"] + 2
    assert api[3].seen_contexts == []
    with api[1].transaction() as db:
        second = actions.propose_preferences(db, principal, obj["id"], {**PREFERENCES, "budget_cents": 500000})
    declined = post(api, "/api/proposals/" + second["id"] + "/decision", {
        "action_id": "decline", "expected_version": second["version"],
        "content_hash": second["content_hash"], "decision": "decline"})
    assert declined.json()["proposal"]["payload"]["state"] == "declined"
    view = api[0].get("/api/sessions/" + obj["id"], headers=headers(api)).json()
    assert len(view["proposals"]) == 2 and not any(p["display_valid"] for p in view["proposals"])


def test_message_service_and_cancel_keep_idempotent_receipts(api):
    obj = session(api)
    path = "/api/sessions/" + obj["id"]
    body = {"action_id": "message", "expected_version": obj["version"], "text": "帮我选择笔记本"}
    result = post(api, path + "/messages", body)
    assert result.status_code == 200 and result.json()["run"]["id"]
    assert post(api, path + "/messages", body).json() == result.json()
    # Closing joins the in-process test provider; no sleeps or network polling.
    api[4].state.service.close()
    assert len(api[3].seen_contexts) == 1
    assert all(token not in json.dumps(api[3].seen_contexts) for token in api[2].values())
    current = api[0].get(path, headers=headers(api)).json()["session"]
    cancel = {"action_id": "cancel", "expected_version": current["version"]}
    assert post(api, path + "/cancel", cancel).status_code == 200
    assert post(api, path + "/cancel", cancel).status_code == 200
    assert post(api, path + "/cancel", {**cancel, "expected_version": 1}).status_code == 409


def test_catalog_update_is_operator_only_and_versioned(api):
    product = api[0].get("/api/catalog", headers=headers(api, "operator")).json()[0]
    path = "/api/catalog/" + product["id"]
    body = {"action_id": "price", "expected_version": product["version"], "price_cents": 123456, "stock": 0}
    assert post(api, path, body).status_code == 403
    updated = post(api, path, body, "operator")
    assert updated.status_code == 200 and updated.json()["payload"]["price_cents"] == 123456
    assert post(api, path, body, "operator").json() == updated.json()
    assert post(api, path, {**body, "action_id": "old-version"}, "operator").status_code == 409


@pytest.mark.parametrize("host", ["localhost:8765", "127.1:8765", "2130706433:8765", "evil.test", "127.0.0.1:80"])
def test_numeric_exact_host(api, host):
    response = api[0].get("/api/me", headers={**headers(api), "Host": host})
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("change", [{"Origin": "null"}, {"Origin": "http://localhost:8765"},
    {"Origin": "http://evil.test"}, {"Origin": None}, {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": None}, {"Sec-Fetch-Mode": "navigate"}, {"Sec-Fetch-Dest": "iframe"}])
def test_mutation_origin(api, change):
    request_headers = {k: v for k, v in {**headers(api, mutation=True), **change}.items() if v is not None}
    assert api[0].post("/api/sessions", headers=request_headers, json={"action_id": "no"}).status_code == 403


@pytest.mark.parametrize("name", ["Host", "Authorization", "Origin", "Content-Type", "Content-Length",
                                  "Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest", "Cookie"])
def test_duplicate_sensitive_headers(api, name):
    value = {**headers(api, mutation=True), "Host": "127.0.0.1:8765", "Content-Length": "2", "Cookie": "x=y"}
    response = api[0].post("/api/sessions", content=b"{}", headers=[*value.items(), (name.lower(), value[name])])
    assert response.status_code == 400


@pytest.mark.parametrize("body", [b'[]', b'null', b'\xff', b'{"a":1,"a":2}',
    b'{"a":{"b":1,"b":2}}', b'{"x":NaN}', b'{"x":1e999}', b'{"x":"\\ud800"}',
    b'{"x":' + b'[' * 30 + b'1' + b']' * 30 + b'}'])
def test_strict_json_without_raw_echo(api, body):
    response = api[0].post("/api/sessions", content=body, headers=headers(api, mutation=True))
    assert response.status_code == 400
    assert set(response.json()) == {"error"}
    assert set(response.json()["error"]) == {"code", "message"}


@pytest.mark.parametrize("path", ["/api/me?token=secret", "/api/%6de", "/api//me"])
def test_no_url_tokens_or_ambiguous_paths(api, path):
    assert api[0].get(path, headers=headers(api)).status_code == 400


def test_auth_validation_and_response_headers(api):
    for value in ({}, {"Authorization": "Basic secret"}, {"Cookie": "token=secret"}):
        assert api[0].get("/api/me", headers=value).status_code == 401
    response = post(api, "/api/sessions", {"action_id": "invalid secret<>"})
    assert response.status_code == 422 and "secret" not in response.text
    response = api[0].get("/api/me", headers={**headers(api), "X-Forwarded-Host": "evil"})
    assert response.status_code == 400
    for path in ("/api/health", "/not-found"):
        response = api[0].get(path)
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "access-control-allow-origin" not in response.headers


def test_streamed_body_is_bounded_before_dispatch(api):
    module = importlib.import_module("shopping_advisor.api")
    assert module.MAX_BODY_BYTES == 128 * 1024
    reached, output = [], []

    async def inner(scope, receive, send):
        reached.append(True)

    async def exercise():
        chunks = iter([b"x" * 70000, b"x" * 70000])

        async def receive():
            return {"type": "http.request", "body": next(chunks), "more_body": True}

        async def send(message):
            output.append(message)

        raw_headers = {**headers(api, mutation=True), "Host": "127.0.0.1:8765"}
        scope = {"type": "http", "method": "POST", "path": "/api/sessions", "raw_path": b"/api/sessions",
                 "query_string": b"", "headers": [(k.lower().encode(), v.encode()) for k, v in raw_headers.items()]}
        await module.LocalBoundary(inner, origin=ORIGIN)(scope, receive, send)

    asyncio.run(exercise())
    assert not reached
    assert output[0]["status"] == 413


def test_unknown_reconciliation_requires_explicit_usage_and_basis(tmp_path):
    module = importlib.import_module("shopping_advisor.api")
    store, tokens = Store.initialize(tmp_path / "unknown", now=1800000000)
    finished = Event()

    class UnknownProvider:
        is_test_provider = True

        def decide(self, context, schema):
            finished.set()
            raise ProviderError("provider_unknown")

    app = module.create_app(store, UnknownProvider(), origin=ORIGIN)
    with TestClient(app, base_url=ORIGIN) as client:
        fixture = client, store, tokens, None, app
        obj = session(fixture)
        result = post(fixture, "/api/sessions/" + obj["id"] + "/messages", {
            "action_id": "unknown-message", "expected_version": 1, "text": "选电脑"})
        assert result.status_code == 200
        assert finished.wait(5)
        app.state.service.close()
        rows = client.get("/api/usage/unknown", headers=headers(fixture, "operator")).json()
        assert len(rows) == 1 and rows[0]["tokens"] is None
        assert "context" not in rows[0] and "response" not in rows[0]
        body = {"action_id": "reconcile", "call_id": rows[0]["id"], "outcome": "no_usable_result",
                "note": "本地调用记录核实为128个合成token；没有可用结果", "tokens": 128}
        assert post(fixture, "/api/usage/reconcile", body).status_code == 403
        assert post(fixture, "/api/usage/reconcile", {**body, "tokens": None}, "operator").status_code == 400
        assert post(fixture, "/api/usage/reconcile", {**body, "note": "  "}, "operator").status_code == 422
        result = post(fixture, "/api/usage/reconcile", body, "operator")
        assert result.status_code == 200 and result.json()["resumed"] is False
        assert post(fixture, "/api/usage/reconcile", body, "operator").json() == result.json()


@pytest.mark.skipif(NODE is None, reason="Node 24 is required on PATH for JS checks")
def test_frontend_money_ordering_and_retry_identifiers():
    script = Path(__file__).resolve().parents[1] / "src/shopping_advisor/static/app.js"
    assert script.is_file(), "Frontend behavior module is missing"
    checks = r'''
const assert = require('node:assert/strict');
const {yuanToCents, integerInput, orderRecords, createOperation, placeAt} = require(process.argv[1]);
assert.equal(yuanToCents('6000.01'), 600001);
assert.equal(yuanToCents('0'), 0);
assert.equal(yuanToCents('100000.00'), 10000000);
for (const value of ['1e3', '-1', '1.001', '1.', 'NaN', '', ' 20', '9007199254740992']) {
  assert.throws(() => yuanToCents(value));
}
assert.equal(integerInput('', 30, 1440, true), null);
assert.throws(() => integerInput('1.5', 1, 20));
assert.throws(() => integerInput('', 0, 100));
const records = [
 {id:'rec-a', kind:'recommendation', payload:{captured_at:10}},
 {id:'rec-z', kind:'recommendation', payload:{captured_at:10}},
 {id:'rec-no-reference', kind:'recommendation', payload:{captured_at:9}}
];
const messages = [{recommendation_id:'rec-z'}, {recommendation_id:'rec-a'}];
assert.deepEqual(orderRecords(records, messages).map(x => x.id), ['rec-no-reference','rec-z','rec-a']);
assert.deepEqual(orderRecords(records, []).map(x => x.id), ['rec-no-reference','rec-a','rec-z']);
const data = {expected_version:7, preferences:{budget_cents:600001}};
const op = createOperation('/api/test', data, 'stable-action');
data.preferences.budget_cents = 1;
assert.equal(op.body.preferences.budget_cents, 600001);
assert.equal(op.body.action_id, 'stable-action');
assert.equal(op.body.expected_version, 7);
assert.equal(JSON.stringify(op), JSON.stringify(op));
const first = {}, second = {};
const parent = {children:[first, second], insertBefore() { throw new Error('Polling must not move unchanged buttons'); }};
placeAt(parent, first, 0);
placeAt(parent, second, 1);
let moved;
placeAt({children:[second], insertBefore(node, before) { moved = [node, before]; }}, first, 0);
assert.deepEqual(moved, [first, second]);
'''
    result = subprocess.run([NODE, "-e", checks, str(script)],
                            capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="Node 24 is required on PATH for JS checks")
@pytest.mark.parametrize("scenario", ["customer-edit-fence", "operator-conflict-reload", "failed-poll-validity"])
def test_frontend_live_handlers_preserve_edits_and_fail_closed(scenario):
    """Execute the shipped JS with minimal DOM/fetch boundaries, without a browser or network."""
    assets = Path(__file__).resolve().parents[1] / "src/shopping_advisor"
    checks = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {randomUUID} = require('node:crypto');
const scenario = process.argv[3];
const camel = text => text.replace(/-([a-z])/g, (_, char) => char.toUpperCase());
class Node {
  constructor(tag) {
    this.tagName = tag.toLowerCase(); this.children = []; this.parentNode = null;
    this.dataset = {}; this.listeners = {}; this.value = ''; this.defaultValue = '';
    this.checked = false; this.defaultChecked = false; this.disabled = false; this.hidden = false;
    this._text = ''; this.id = '';
  }
  set textContent(value) { this._text = String(value); this.replaceChildren(); }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
  append(...nodes) { for (const node of nodes) this.insertBefore(node, null); }
  insertBefore(node, before) {
    if (node.parentNode) node.parentNode.children.splice(node.parentNode.children.indexOf(node), 1);
    node.parentNode = this;
    this.children.splice(before ? this.children.indexOf(before) : this.children.length, 0, node);
  }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parentNode = null;
    this.children = []; this.append(...nodes);
  }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  matches(selector) {
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    const attr = selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
    if (attr) {
      const value = attr[1].startsWith('data-') ? this.dataset[camel(attr[1].slice(5))] : this[attr[1]];
      return attr[2] === undefined ? value !== undefined : String(value) === attr[2];
    }
    return this.tagName === selector.toLowerCase();
  }
  querySelectorAll(selector) {
    const selectors = selector.split(',').map(item => item.trim()), found = [];
    const visit = node => { for (const child of node.children) {
      if (selectors.some(item => child.matches(item))) found.push(child);
      visit(child);
    }};
    visit(this); return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parentNode?.closest(selector); }
  get options() { return this.children.filter(node => node.tagName === 'option'); }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  async fire(type) { await Promise.all((this.listeners[type] || []).map(fn => fn({target:this, preventDefault(){}}))); }
  reset() { for (const node of this.querySelectorAll('input, select, textarea')) {
    node.value = node.defaultValue; node.checked = node.defaultChecked;
  }}
}
const document = new Node('document');
document.createElement = tag => new Node(tag);
document.getElementById = id => document.querySelector('#' + id);
document.activeElement = null; document.hidden = false;
const stack = [document];
const html = fs.readFileSync(process.argv[2], 'utf8');
for (const match of html.matchAll(/<\/?([a-z][a-z0-9-]*)\b[^>]*>/gi)) {
  const token = match[0], tag = match[1].toLowerCase();
  if (token.startsWith('</')) { if (stack.at(-1).tagName === tag) stack.pop(); continue; }
  const node = new Node(tag);
  for (const attr of token.matchAll(/([a-z][a-z0-9-]*)="([^"]*)"/gi)) {
    if (attr[1].startsWith('data-')) node.dataset[camel(attr[1].slice(5))] = attr[2];
    else node[attr[1]] = attr[2];
  }
  node.defaultValue = node.value;
  node.hidden = /\bhidden(?:\s|>)/.test(token); node.disabled = /\bdisabled(?:\s|>)/.test(token);
  stack.at(-1).append(node);
  if (!['meta','link','input','br','hr'].includes(tag)) stack.push(node);
}
document.body = document.querySelector('body');
for (const select of document.querySelectorAll('select')) select.value = select.defaultValue = select.options[0]?.value || '';
const $ = id => { const node = document.getElementById(id); assert.ok(node, `Missing control #${id}`); return node; };
const prefs = {budget_cents:600000,use:'office',priority:'value',min_ram_gb:8,min_storage_gb:256,
  max_weight_g:null,min_battery_minutes:null,needs_discrete_gpu:false,os:'any'};
let session = {kind:'session',id:'sess-one',version:5,payload:{preferences:{...prefs},preferences_revision:1,
  active_run:null,blocked_unknown:false,messages:[]}};
const proposal = {kind:'proposal',id:'prop-one',version:1,content_hash:'a'.repeat(64),display_valid:true,
  payload:{preferences:{...prefs},state:'pending_confirmation',expires_at:2000}};
const recommendation = {kind:'recommendation',id:'rec-one',version:1,content_hash:'b'.repeat(64),display_valid:true,
  payload:{state:'current',captured_at:1000,expires_at:1900,facts:[],tradeoffs:[],scoring_version:'test'}};
let product = {kind:'product',id:'demo-one',version:1,payload:{name:'合成测试',price_cents:100000,stock:4}};
let failPoll = false;
const posts = [], intervals = [];
const clone = value => JSON.parse(JSON.stringify(value));
const reply = (body, status=200) => ({ok:status === 200,status,json:async () => clone(body)});
const fetch = async (path, options) => {
  if (options.method === 'POST') {
    const body = JSON.parse(options.body); posts.push({path,body});
    if (path === '/api/sessions/sess-one/preferences') {
      if (body.expected_version !== session.version) return reply({error:{message:'版本冲突'}},409);
      session = {...session,version:session.version+2,payload:{...session.payload,
        preferences:body.preferences,preferences_revision:session.payload.preferences_revision+1}};
      return reply(session);
    }
    if (path === '/api/catalog/demo-one') {
      if (body.expected_version !== product.version) return reply({error:{message:'版本冲突'}},409);
      product = {...product,version:product.version+1,payload:{...product.payload,
        price_cents:body.price_cents,stock:body.stock}};
      return reply(product);
    }
    throw new Error('Unexpected POST ' + path);
  }
  if (failPoll) throw new Error('Simulated offline boundary');
  if (path === '/api/me') return reply({user_id:'alice',role:scenario.startsWith('operator')?'operator':'customer',test_mode:true});
  if (path === '/api/sessions') return reply([session]);
  if (path === '/api/sessions/sess-one') return reply({session,proposals:[proposal],evidences:[],evaluations:[],recommendations:[recommendation],runs:[]});
  if (path === '/api/catalog') return reply([product]);
  if (path === '/api/usage/unknown') return reply([]);
  throw new Error('Unexpected GET ' + path);
};
const context = {document,window:new Node('window'),fetch,crypto:{randomUUID},AbortController,
  setTimeout:() => 1,clearTimeout(){},setInterval:fn => {intervals.push(fn); return 1;},console};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context, {filename:process.argv[1]});
const settle = async () => { for (let i=0;i<8;i++) await new Promise(resolve => setImmediate(resolve)); };
const poll = async () => { for (const tick of intervals) tick(); await settle(); };
(async () => {
  $('token').value = 'in-memory-test-token'; await $('login-form').fire('submit'); await settle();
  assert.equal(document.body.dataset.ready, 'true');
  if (!scenario.startsWith('operator')) {
    $('session-list').value = 'sess-one'; await $('session-list').fire('change'); await settle();
    assert.match($('session-status').textContent, /sess-one/);
  }
  if (scenario === 'customer-edit-fence') {
    $('budget-yuan').value = '5500.00'; await $('preferences-form').fire('input');
    session = {...session,version:7,payload:{...session.payload,preferences:{...prefs,budget_cents:400000},preferences_revision:2}};
    await poll(); assert.equal($('budget-yuan').value, '5500.00');
    await $('preferences-form').fire('submit'); await settle();
    assert.equal(session.payload.preferences.budget_cents,400000,'Dirty form must not silently adopt polled version and overwrite another edit');
    assert.equal($('budget-yuan').value,'5500.00','Conflict must preserve typed input');
    const count = posts.length;
    await $('reload-preferences').fire('click'); await settle();
    assert.equal(posts.length,count,'Explicit reload must be read-only');
    assert.equal($('budget-yuan').value,'4000.00');
    $('budget-yuan').value = '4500.00'; await $('preferences-form').fire('input');
    session = {...session,version:8}; // A concurrent non-preference update still cannot rebase this edit.
    await poll();
    await $('preferences-form').fire('submit'); await settle();
    assert.equal(posts.at(-1).body.expected_version,7);
    assert.equal(session.payload.preferences.budget_cents,400000);
    assert.equal($('budget-yuan').value,'4500.00');
    await $('reload-preferences').fire('click'); await settle();
    $('budget-yuan').value = '4500.00'; await $('preferences-form').fire('input');
    await $('preferences-form').fire('submit'); await settle();
    assert.equal(session.payload.preferences.budget_cents,450000);
    assert.equal(posts.at(-1).body.expected_version,8);
  } else if (scenario === 'operator-conflict-reload') {
    const form = $('catalog').children[0];
    $('price-demo-one').value = '1111.00'; await form.fire('input');
    product = {...product,version:2,payload:{...product.payload,price_cents:222200}};
    await form.fire('submit'); await settle();
    assert.equal(posts.at(-1).body.expected_version,1);
    assert.equal($('price-demo-one').value,'1111.00','409 refresh must preserve edits');
    const reload = form.querySelector('[data-action="reload-product"]');
    assert.ok(reload,'Operator needs an explicit way to leave the stale-version retry loop');
    const count = posts.length;
    await reload.fire('click'); await settle();
    assert.equal(posts.length,count,'Reload must not mutate catalog');
    assert.equal($('price-demo-one').value,'2222.00');
    $('price-demo-one').value = '3333.00'; await form.fire('input');
    await form.fire('submit'); await settle();
    assert.equal(posts.at(-1).body.expected_version,2);
    assert.equal(product.payload.price_cents,333300);
  } else {
    const rec = $('recommendations').children[0], prop = $('proposals').children[0];
    const confirm = prop.querySelector('[data-action="confirm"]');
    assert.equal(rec.dataset.valid,'true'); assert.equal(confirm.disabled,false);
    failPoll = true; await poll();
    assert.equal(rec.dataset.valid,'false','Failed polling must not keep stale recommendation marked current');
    assert.equal(rec.dataset.verified,'false');
    assert.match(rec.querySelector('[data-part="validity"]').textContent,/未核验/);
    assert.equal(confirm.disabled,true,'Offline proposal confirmation must be disabled');
    failPoll = false; recommendation.display_valid = false; proposal.display_valid = false;
    await poll();
    assert.equal(rec.dataset.verified,'true'); assert.equal(rec.dataset.valid,'false');
    assert.equal(confirm.disabled,true);
    recommendation.display_valid = true; proposal.display_valid = true;
    await poll();
    assert.equal(rec.dataset.valid,'true'); assert.equal(confirm.disabled,false);
    assert.equal(posts.length,0,'Read-only polling must never submit an action');
  }
})().catch(error => { console.error(error); process.exitCode=1; });
'''
    result = subprocess.run([
        NODE, "-e", checks,
        str(assets / "static/app.js"), str(assets / "templates/index.html"), scenario,
    ], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=20)
    assert result.returncode == 0, result.stderr


def test_chinese_assets_expose_stable_controls_and_no_persistent_token(api):
    page = api[0].get("/")
    assert page.status_code == 200
    for selector in ("token", "login-form", "logout", "mode-banner", "new-session", "session-list", "message-form",
                     "message-text", "cancel-run", "preferences-form", "budget-yuan", "use", "priority",
                     "min-ram-gb", "min-storage-gb", "max-weight-g", "min-battery-minutes", "needs-discrete-gpu",
                     "os", "proposals", "evidences", "evaluations", "recommendations", "catalog", "unknown-calls"):
        assert f'id="{selector}"' in page.text
    assert 'type="password"' in page.text and "TEST" in page.text and "zh-CN" in page.text
    script = api[0].get("/static/app.js")
    assert script.status_code == 200
    for unsafe in ("innerHTML", "localStorage", "sessionStorage", "document.cookie", "insertAdjacentHTML"):
        assert unsafe not in script.text
    assert api[0].get("/static/style.css").status_code == 200
    assert not (Path(__file__).resolve().parents[1] / "runtime").exists()
