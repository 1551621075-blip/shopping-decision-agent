from __future__ import annotations

from .actions import Actions
from .catalog import evaluate_products, filter_products
from .recommendation import NOTICE, render_recommendation
from .schemas import DomainError, Preferences, identifier, validate_action

QUESTIONS = (
    "你的最高总预算是多少元？请把商品价格和运费都算在内。",
    "主要用于办公、编程、创作还是游戏？",
    "对内存、存储、重量、续航、独显或操作系统有没有必须满足的条件？",
    "更看重价格、性能、便携还是续航？",
    "你愿意明确调整哪些条件？预算不会自动提高。",
    "请核对偏好卡或编辑表单并点击确认；聊天中的“好的”不会修改已确认预算。",
)


class Tools:
    def __init__(self, actions: Actions):
        self.actions, self.store = actions, actions.store

    def _say(self, db, principal, sid, message):
        sess = self.actions.customer_session(db, principal, sid)
        self.store.save(db, sess, {**sess["payload"], "messages": [*sess["payload"]["messages"], message]})

    def _issue(self, db, principal, sess, tool, data, sources, *, kind="evidence", extra=None, expires=None):
        now = self.store.now(db)
        return self.store.create(db, kind, identifier("eval" if kind == "evaluation" else "ev"),
                                 principal.user_id, {
            "session_id": sess["id"], "tool": tool, "data": data,
            "bindings": self.actions.bindings(db, sess), "captured_at": now,
            "expires_at": min(now + 900, expires) if expires is not None else now + 900,
            "sources": [{"kind": obj["kind"], "id": obj["id"], "hash": obj["content_hash"]}
                        for obj in sources], **(extra or {}),
        })

    def _evidence(self, db, principal, sid, object_id, tool, *, kind="evidence"):
        obj = self.store.get(db, kind, object_id, owner=principal.user_id)
        self.actions.fresh(db, principal, sid, obj)
        if obj["payload"]["tool"] != tool:
            raise DomainError("wrong_evidence", "引用不是所需的工具结果")
        return obj

    def execute(self, db, principal, sid, action):
        name, args = validate_action(action)
        sess = self.actions.customer_session(db, principal, sid, bot=True)
        prefs = sess["payload"]["preferences"]
        if name == "read_context":
            products = self.actions.catalog_objects(db)
            proposals = self.actions._related(db, "proposal", sid, principal.user_id)
            recommendations = self.actions._related(db, "recommendation", sid, principal.user_id)
            data = {**self.actions.context_data(db, principal, sess), "notice": NOTICE}
            obj = self._issue(db, principal, sess, name, data, [*products, *proposals, *recommendations])
        elif name == "propose_preferences":
            obj = self.actions.propose_preferences(db, principal, sid, args["preferences"])
            self._say(db, principal, sid, {"role": "assistant", "kind": "proposal",
                                         "text": QUESTIONS[-1], "proposal_id": obj["id"]})
            return {"result": obj, "terminal": "waiting_confirmation"}
        elif name == "ask_shopper":
            if args["question"] not in QUESTIONS:
                raise DomainError("question_template", "请选择allowed_questions中的原文问题")
            self._say(db, principal, sid, {"role": "assistant", "kind": "question", "text": args["question"]})
            return {"result": {"question": args["question"]}, "terminal": "waiting_customer"}
        elif name == "search_products":
            supplied = args["preferences"]
            if prefs is not None and supplied is not None and supplied != prefs:
                raise DomainError("unconfirmed_change", "修改硬条件须由用户明确确认，不能暗中放宽")
            search_prefs = prefs if prefs is not None else supplied
            if search_prefs is None:
                raise DomainError("missing_preferences", "请先询问预算和用途，或使用已有信息临时查询")
            search_prefs = Preferences.model_validate(search_prefs).model_dump()
            products = self.actions.catalog_objects(db)
            data = filter_products([obj["payload"] for obj in products], search_prefs, self.store.now(db))
            expires = min((p["captured_at"] + 86400 for p in data["eligible"]), default=self.store.now(db) + 900)
            obj = self._issue(db, principal, sess, name, data, products, expires=expires,
                              extra={"preferences": search_prefs, "confirmed": prefs is not None})
        elif name == "compare_products":
            search = self._evidence(db, principal, sid, args["search_id"], "search_products")
            eligible = {p["id"]: p for p in search["payload"]["data"]["eligible"]}
            if not all(pid in eligible for pid in args["product_ids"]):
                raise DomainError("not_in_search", "只能比较这次查询中满足硬条件的商品")
            products = [eligible[pid] for pid in args["product_ids"]]
            obj = self._issue(db, principal, sess, name, {"products": products, "notice": NOTICE}, [search],
                              expires=search["payload"]["expires_at"], extra={
                                  "preferences": search["payload"]["preferences"],
                                  "confirmed": search["payload"]["confirmed"],
                              })
        elif name == "evaluate_shortlist":
            comparison = self._evidence(db, principal, sid, args["comparison_id"], "compare_products")
            if prefs is None or not comparison["payload"]["confirmed"] or prefs != comparison["payload"]["preferences"]:
                raise DomainError("confirmation_required", "正式评估推荐前请先确认偏好卡")
            products = comparison["payload"]["data"]["products"]
            data = evaluate_products(products, prefs, self.store.now(db))
            obj = self._issue(db, principal, sess, name, data, [comparison], kind="evaluation",
                              expires=comparison["payload"]["expires_at"], extra={"preferences": prefs})
        elif name == "recommend":
            evaluation = self._evidence(db, principal, sid, args["evaluation_id"],
                                        "evaluate_shortlist", kind="evaluation")
            if prefs is None or prefs != evaluation["payload"]["preferences"]:
                raise DomainError("confirmation_required", "推荐条件必须与当前已确认偏好一致")
            rendered = render_recommendation(args, evaluation)
            for old in self.actions._related(db, "recommendation", sid, principal.user_id):
                if old["payload"]["state"] == "current":
                    self.store.save(db, old, {**old["payload"], "state": "superseded"})
            obj = self.store.create(db, "recommendation", identifier("rec"), principal.user_id, {
                "session_id": sid, "state": "current", "evaluation_id": evaluation["id"],
                "bindings": self.actions.bindings(db, sess), "preferences": prefs,
                "captured_at": self.store.now(db), "expires_at": evaluation["payload"]["expires_at"],
                "sources": [{"kind": "evaluation", "id": evaluation["id"], "hash": evaluation["content_hash"]}],
                **rendered,
            })
            self._say(db, principal, sid, {"role": "assistant", "kind": "recommendation",
                                         "text": "已按确认条件生成推荐，请查看商品与取舍卡。" + NOTICE,
                                         "recommendation_id": obj["id"]})
            return {"result": obj, "terminal": "completed"}
        elif name == "respond":
            evidence = [self.store.get(db, "evidence", eid, owner=principal.user_id) for eid in args["evidence_ids"]]
            for item in evidence:
                self.actions.fresh(db, principal, sid, item)
            if args["kind"] != "guidance" and not evidence:
                raise DomainError("missing_evidence", "事实或无解回答须引用当前查询")
            if args["kind"] == "no_match" and not all(
                    item["payload"]["tool"] == "search_products"
                    and not item["payload"]["data"]["eligible"] for item in evidence):
                raise DomainError("not_empty", "只有实际无合格商品的查询才可说明无解")
            text = {"facts": "查询结果见引用卡，条件与数据版本以卡片为准。",
                    "no_match": "当前没有满足全部硬条件的商品。" + QUESTIONS[4],
                    "guidance": "我可以按预算和用途筛选、比较合成笔记本。"}[args["kind"]]
            text += NOTICE
            self._say(db, principal, sid, {"role": "assistant", "kind": args["kind"], "text": text,
                                         "evidence_ids": args["evidence_ids"]})
            return {"result": {"text": text, "evidence_ids": args["evidence_ids"],
                               "model_text_published": False}, "terminal": "completed"}
        else:
            raise DomainError("invalid_tool", "工具不存在")
        return {"result": obj, "terminal": None}
