from __future__ import annotations

from .schemas import (
    Command,
    DomainError,
    MessageCommand,
    Preferences,
    PreferencesCommand,
    ProductCommand,
    ProposalCommand,
    VersionedCommand,
    canonical,
    digest,
    identifier,
)
from .storage import Store

LIVE_RUNS = {"queued", "running"}
RELATED = ("proposal", "evidence", "evaluation", "recommendation", "run")


def check_version(obj, expected):
    if type(expected) is not int or obj["version"] != expected:
        raise DomainError("stale_version", "对象版本已变化，请刷新后重试", 409)


class Actions:
    def __init__(self, store: Store):
        self.store = store

    def _command(self, principal, op, target, command, model, execute):
        command = model.model_validate(command.model_dump())
        request = {"op": op, "target": target, **command.model_dump()}
        with self.store.transaction() as db:
            old = self.store.receipt(db, principal, command.action_id, request)
            if old is not None:
                return old
            result = execute(db, command)
            self.store.record_receipt(db, principal, command.action_id, request, result)
            self.store.audit(db, principal, op, target or result.get("id", "new"))
            return result

    def customer_session(self, db, principal, sid, *, bot=False):
        self.store.require(db, principal, "customer")
        sess = self.store.get(db, "session", sid, owner=principal.user_id)
        if bot and sess["payload"]["control"] != "bot":
            raise DomainError("closed", "会话已关闭，Agent不能继续操作", 409)
        return sess

    def create_session(self, principal, command):
        def execute(db, cmd):
            self.store.require(db, principal, "customer")
            if len(self.store.list(db, "session", owner=principal.user_id)) >= 30:
                raise DomainError("session_limit", "本地演示最多保留30个会话", 409)
            return self.store.create(db, "session", identifier("sess"), principal.user_id, {
                "control": "bot", "control_epoch": 1, "active_run": None,
                "messages": [], "tool_count": 0, "blocked_unknown": False,
                "preferences": None, "preferences_revision": 0,
            })
        return self._command(principal, "create_session", "", command, Command, execute)

    def _related(self, db, kind, sid, owner):
        return [obj for obj in self.store.list(db, kind, owner=owner)
                if obj["payload"].get("session_id") == sid]

    def _supersede(self, db, sid, owner):
        for kind, state in (("proposal", "pending_confirmation"), ("recommendation", "current")):
            for obj in self._related(db, kind, sid, owner):
                if obj["payload"]["state"] == state:
                    self.store.save(db, obj, {**obj["payload"], "state": "superseded"})

    def message(self, principal, sid, command):
        def execute(db, cmd):
            sess = self.customer_session(db, principal, sid, bot=True)
            check_version(sess, cmd.expected_version)
            payload = sess["payload"]
            outstanding = db.execute(
                "SELECT c.state FROM calls c JOIN objects r ON r.kind='run' AND r.id=c.run_id "
                "WHERE c.owner=? AND json_extract(r.payload,'$.session_id')=? "
                "AND c.state IN ('started','unknown')", (principal.user_id, sid)).fetchall()
            if payload["blocked_unknown"] or outstanding:
                raise DomainError("blocked_unknown", "有在途或未知模型调用，需等待返回或先对账", 409)
            if payload["active_run"]:
                raise DomainError("run_active", "当前轮次尚未结束，请等待或取消", 409)
            if len(payload["messages"]) >= 100:
                raise DomainError("message_limit", "本会话消息已达到上限，请新建会话", 409)
            self._supersede(db, sid, principal.user_id)
            run = self.store.create(db, "run", identifier("run"), principal.user_id, {
                "session_id": sid, "state": "queued", "control_epoch": payload["control_epoch"],
                "calls": 0, "tools": 0, "guard_rejections": 0, "trace": [],
                "pending_action": None, "started_at": self.store.now(db),
            })
            sess = self.store.save(db, sess, {
                **payload, "messages": [*payload["messages"], {"role": "customer", "text": cmd.text}],
                "active_run": run["id"],
            })
            return {"session": sess, "run": run}
        return self._command(principal, "message", sid, command, MessageCommand, execute)

    def _freeze(self, db, sess, control):
        payload = sess["payload"]
        if payload["active_run"]:
            run = self.store.get(db, "run", payload["active_run"], owner=sess["owner"])
            if run["payload"]["state"] in LIVE_RUNS:
                self.store.save(db, run, {**run["payload"], "state": "cancelled", "pending_action": None})
        return self.store.save(db, sess, {**payload, "control": control,
                                         "control_epoch": payload["control_epoch"] + 1,
                                         "active_run": None})

    def cancel(self, principal, sid, command):
        def execute(db, cmd):
            sess = self.customer_session(db, principal, sid)
            check_version(sess, cmd.expected_version)
            self._supersede(db, sid, principal.user_id)
            return self._freeze(db, sess, sess["payload"]["control"])
        return self._command(principal, "cancel", sid, command, VersionedCommand, execute)

    def _apply_preferences(self, db, sess, preferences):
        preferences = Preferences.model_validate(preferences).model_dump()
        self._supersede(db, sess["id"], sess["owner"])
        sess = self._freeze(db, sess, sess["payload"]["control"])
        return self.store.save(db, sess, {**sess["payload"], "preferences": preferences,
                                         "preferences_revision": sess["payload"]["preferences_revision"] + 1})

    def set_preferences(self, principal, sid, command):
        def execute(db, cmd):
            sess = self.customer_session(db, principal, sid, bot=True)
            check_version(sess, cmd.expected_version)
            return self._apply_preferences(db, sess, cmd.preferences.model_dump())
        return self._command(principal, "set_preferences", sid, command, PreferencesCommand, execute)

    def propose_preferences(self, db, principal, sid, preferences):
        sess = self.customer_session(db, principal, sid, bot=True)
        preferences = Preferences.model_validate(preferences).model_dump()
        if preferences == sess["payload"]["preferences"]:
            raise DomainError("unchanged_preferences", "这些条件已经确认，可直接查询")
        for obj in self._related(db, "proposal", sid, principal.user_id):
            if obj["payload"]["state"] == "pending_confirmation":
                self.store.save(db, obj, {**obj["payload"], "state": "superseded"})
        return self.store.create(db, "proposal", identifier("pref"), principal.user_id, {
            "session_id": sid, "state": "pending_confirmation", "preferences": preferences,
            "base_revision": sess["payload"]["preferences_revision"], "expires_at": self.store.now(db) + 900,
        })

    def decide_proposal(self, principal, proposal_id, command):
        def execute(db, cmd):
            self.store.require(db, principal, "customer")
            proposal = self.store.get(db, "proposal", proposal_id, owner=principal.user_id)
            check_version(proposal, cmd.expected_version)
            if proposal["content_hash"] != cmd.content_hash:
                raise DomainError("proposal_hash", "偏好草案内容已变化，请重新核对", 409)
            payload = proposal["payload"]
            sess = self.customer_session(db, principal, payload["session_id"], bot=True)
            if payload["state"] != "pending_confirmation":
                raise DomainError("proposal_state", "草案已处理或失效", 409)
            if self.store.now(db) >= payload["expires_at"]:
                raise DomainError("expired", "偏好草案已过期，请重新提出", 409)
            if payload["base_revision"] != sess["payload"]["preferences_revision"]:
                raise DomainError("stale_preferences", "偏好已变化，不能确认旧草案", 409)
            proposal = self.store.save(db, proposal, {
                **payload, "state": "confirmed" if cmd.decision == "confirm" else "declined"})
            if cmd.decision == "confirm":
                sess = self._apply_preferences(db, sess, payload["preferences"])
            return {"proposal": proposal, "session": sess,
                    "notice": "仅保存选购条件；没有下单或付款。发送新消息后继续。"}
        return self._command(principal, "decide_proposal", proposal_id, command, ProposalCommand, execute)

    def catalog_objects(self, db):
        return self.store.list(db, "product", owner="system")

    def catalog_hash(self, db):
        return digest([obj["content_hash"] for obj in self.catalog_objects(db)])

    def bindings(self, db, sess):
        return {"catalog_hash": self.catalog_hash(db),
                "preferences_revision": sess["payload"]["preferences_revision"]}

    def context_data(self, db, principal, sess, *, _path=()):
        now = self.store.now(db)
        proposals = self._related(db, "proposal", sess["id"], sess["owner"])
        recommendations = self._related(db, "recommendation", sess["id"], sess["owner"])
        for proposal in proposals:
            payload = proposal["payload"]
            proposal["display_valid"] = (payload["state"] == "pending_confirmation"
                                         and now < payload["expires_at"]
                                         and payload["base_revision"] == sess["payload"]["preferences_revision"])
        summaries = []
        for obj in recommendations:
            payload = obj["payload"]
            try:
                self.fresh(db, principal, sess["id"], obj, _path=_path)
                valid = payload["state"] == "current"
            except DomainError:
                valid = False
            summaries.append({"id": obj["id"], "state": payload["state"], "display_valid": valid})
        return {"preferences": sess["payload"]["preferences"],
                "preferences_revision": sess["payload"]["preferences_revision"],
                "catalog": [{"id": obj["id"], "name": obj["payload"]["name"]}
                            for obj in self.catalog_objects(db)],
                "proposals": proposals, "recommendations": summaries}

    def fresh(self, db, principal, sid, obj, *, _path=()):
        from .catalog import evaluate_products, filter_products

        sess = self.customer_session(db, principal, sid, bot=True)
        payload = obj["payload"]
        key = (obj["kind"], obj["id"])
        if key in _path or len(_path) >= 8:
            raise DomainError("invalid_evidence", "证据引用链异常")
        if obj["owner"] != principal.user_id or payload.get("session_id") != sid:
            raise DomainError("wrong_session", "证据不属于此会话")
        if payload["bindings"] != self.bindings(db, sess):
            raise DomainError("stale_evidence", "偏好或商品目录已变化，请重新查询")
        now = self.store.now(db)
        if not payload["captured_at"] <= now < payload["expires_at"]:
            raise DomainError("stale_evidence", "证据已过期，请重新查询")
        sources = []
        for source in payload.get("sources", []):
            if source["kind"] not in {"product", "proposal", "evidence", "evaluation", "recommendation"}:
                raise DomainError("invalid_evidence", "证据来源类型异常")
            owner = "system" if source["kind"] == "product" else principal.user_id
            current = self.store.get(db, source["kind"], source["id"], owner=owner)
            if current["content_hash"] != source["hash"]:
                raise DomainError("stale_evidence", "引用来源已变化，请重新查询")
            if (payload.get("tool") != "read_context"
                    and source["kind"] in {"evidence", "evaluation", "recommendation"}):
                self.fresh(db, principal, sid, current, _path=(*_path, key))
            elif source["kind"] == "proposal" and current["payload"]["session_id"] != sid:
                raise DomainError("wrong_session", "来源不属于此会话")
            sources.append(current)
        tool = payload.get("tool")
        if tool == "read_context":
            expected = self.context_data(db, principal, sess, _path=(*_path, key))
            if canonical({k: v for k, v in payload["data"].items() if k != "notice"}) != canonical(expected):
                raise DomainError("stale_evidence", "会话状态已变化，请重新读取")
        elif tool == "search_products":
            expected = filter_products([p["payload"] for p in self.catalog_objects(db)],
                                       payload["preferences"], now)
            if canonical(payload["data"]) != canonical(expected):
                raise DomainError("invalid_evidence", "查询结果与当前商品事实不一致")
        elif tool == "compare_products":
            if len(sources) != 1 or sources[0]["payload"].get("tool") != "search_products":
                raise DomainError("invalid_evidence", "比较缺少真实查询来源")
            search = sources[0]["payload"]
            available = {p["id"]: p for p in search["data"]["eligible"]}
            products = payload["data"]["products"]
            if (not 1 <= len(products) <= 6 or len({p["id"] for p in products}) != len(products)
                    or any(canonical(p) != canonical(available.get(p["id"])) for p in products)
                    or canonical(payload["preferences"]) != canonical(search["preferences"])
                    or payload["confirmed"] is not search["confirmed"]):
                raise DomainError("invalid_evidence", "比较内容与查询来源不一致")
        elif tool == "evaluate_shortlist":
            if len(sources) != 1 or sources[0]["payload"].get("tool") != "compare_products":
                raise DomainError("invalid_evidence", "评估缺少真实比较来源")
            comparison = sources[0]["payload"]
            prefs = sess["payload"]["preferences"]
            if prefs is None or not comparison["confirmed"] or prefs != comparison["preferences"]:
                raise DomainError("invalid_evidence", "正式评估缺少用户确认条件")
            expected = evaluate_products(comparison["data"]["products"], prefs, now)
            if (canonical(payload["preferences"]) != canonical(prefs)
                    or canonical(payload["data"]) != canonical(expected)):
                raise DomainError("invalid_evidence", "评估数据与后端计算不一致")
        elif obj["kind"] == "recommendation":
            from .recommendation import render_recommendation

            if len(sources) != 1 or sources[0]["kind"] != "evaluation":
                raise DomainError("invalid_evidence", "推荐缺少评估来源")
            expected = render_recommendation(payload.get("decision"), sources[0])
            if any(canonical(payload.get(field)) != canonical(value) for field, value in expected.items()):
                raise DomainError("invalid_evidence", "推荐与后端事实渲染不一致")
        return obj

    def view(self, principal, sid):
        with self.store.transaction() as db:
            sess = self.customer_session(db, principal, sid)
            result = {"session": sess, "now": self.store.now(db)}
            for kind in RELATED:
                values = self._related(db, kind, sid, principal.user_id)
                for obj in values:
                    if kind in {"evidence", "evaluation", "recommendation"}:
                        try:
                            self.fresh(db, principal, sid, obj)
                            valid = kind != "recommendation" or obj["payload"]["state"] == "current"
                            obj["display_valid"] = valid
                            obj["display_reason"] = None if valid else "superseded"
                        except DomainError as exc:
                            obj["display_valid"], obj["display_reason"] = False, exc.code
                    elif kind == "proposal":
                        obj["display_valid"] = (
                            obj["payload"]["state"] == "pending_confirmation"
                            and result["now"] < obj["payload"]["expires_at"]
                            and obj["payload"]["base_revision"] == sess["payload"]["preferences_revision"])
                result[kind + "s"] = values
            return result

    def catalog_view(self, principal):
        with self.store.transaction() as db:
            self.store.require(db, principal, "operator")
            return self.catalog_objects(db)

    def update_product(self, principal, product_id, command):
        def execute(db, cmd):
            from .catalog import validate_product

            self.store.require(db, principal, "operator")
            obj = self.store.get(db, "product", product_id, owner="system")
            check_version(obj, cmd.expected_version)
            payload = validate_product({**obj["payload"], "price_cents": cmd.price_cents,
                                        "stock": cmd.stock, "captured_at": self.store.now(db)})
            result = self.store.save(db, obj, payload)
            for sess in self.store.list(db, "session"):
                if sess["payload"]["active_run"]:
                    self._freeze(db, sess, sess["payload"]["control"])
            return result
        return self._command(principal, "update_product", product_id, command, ProductCommand, execute)
