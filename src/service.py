from . import domain, rules
from .domain import DomainError
from .repository import now_iso


class Service:
    def __init__(self, repository):
        self.repository = repository

    def ingest_alarm(self, payload, actor, role, region=None):
        """告警入口：归并、查重、冲突挂待核验都在这里完成。"""
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.ALARM_ROLES:
            raise DomainError("forbidden", "当前角色不能登记告警", 403)
        alarm = domain.normalize_alarm(payload)
        stable_key = alarm.pop("_stable_key")
        alarm["stable_key"] = stable_key
        result = self.repository.ingest_alarm(
            rules.ENTITY_TYPE,
            alarm,
            actor,
            role,
            rules.INITIAL_STATUS,
            lambda item, sources: rules.plan_review(item, sources, now_iso()),
        )
        return self._decorate(result["item"]), {"merged": result["merged"], "held": result["held"]}

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self._decorate(self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role,
            pipeline_id=normalized["pipeline_id"], segment_id=normalized["segment_id"],
        ))

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        if item["status"] in rules.CLOSED_STATUSES:
            raise DomainError("item_closed", "事件已经结束，不能再登记来源", 409)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
            review_planner=lambda candidate, sources: rules.plan_review(candidate, sources, now_iso()),
        )
        return self._decorate(result["item"])

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role, now=now_iso())
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self._decorate(self.get_item(item_id))

    def _decorate(self, item):
        item["sources"] = self.repository.list_sources(item["id"])
        item["audit"] = self.repository.audit_trail(item["id"])
        item["assessment"] = rules.assess(item["payload"])
        item["status_label"] = rules.STATUS_LABELS.get(item["status"], item["status"])
        item["source_summary"] = self._source_summary(item)
        item["next_step"] = rules.next_step(item)
        return item

    def _source_summary(self, item):
        """来源摘要：每条来源给出异味/压降/浓度三个维度的结论标记和驳回状态。

        告警建事件时初报同时作为第一条来源落库，因此初报行与该来源合并显示，
        避免调度台看到两条一模一样的记录。
        """
        dismissed = set(item["payload"].get("dismissed_source_ids", []))
        origin = item["payload"].get("origin") or {}
        origin_key = (origin.get("source_type"), origin.get("external_id"))
        shown = False
        summary = []
        for source in item["sources"]:
            is_origin = (source["source_type"], source["external_id"]) == origin_key and not shown
            if is_origin:
                shown = True
            summary.append({
                "source_id": source["id"],
                "source_type": source["source_type"],
                "external_id": source["external_id"],
                "observed_at": source["observed_at"],
                "label": ("初报 · %s/%s" % (source["source_type"], source["external_id"]))
                         if is_origin else "%s/%s" % (source["source_type"], source["external_id"]),
                "note": source["payload"].get("note", ""),
                "dismissed": source["id"] in dismissed,
                "is_origin": is_origin,
                "signals": rules.extract_source_signals(source),
            })
        if not shown and item["payload"].get("reported_at") is not None:
            summary.insert(0, {
                "source_id": None,
                "source_type": origin.get("source_type", "initial"),
                "external_id": origin.get("external_id", item["payload"].get("reporter", "initial")),
                "observed_at": item["payload"].get("reported_at"),
                "label": "初报（%s）" % item["payload"].get("reporter", "未知"),
                "note": "",
                "dismissed": False,
                "is_origin": True,
                "signals": rules.baseline_signals(item),
            })
        return summary

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        return self._decorate(item)

    def list_items(self, status=None):
        items = self.repository.list_items(status)
        for item in items:
            item["status_label"] = rules.STATUS_LABELS.get(item["status"], item["status"])
            item["assessment"] = rules.assess(item["payload"])
            item["next_step"] = rules.next_step(item)
            pending = item["payload"].get("pending_review")
            item["pending_review"] = pending
        return items

    def state(self):
        summary = self.repository.state_summary()
        summary["items"] = self.list_items()
        return summary
