import uuid

from . import domain, rules, present
from .domain import DomainError, ConflictError


def _auto_external_id():
    return "auto-%s" % uuid.uuid4().hex


READING_KEYS = ("pressure_drop_kpa", "sensor_value_ppm", "odor_reports")


class Service:
    def __init__(self, repository):
        self.repository = repository

    # ---- 告警入口：同管段半小时合并，外部编号重复提示 ----

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")

        source_type = "dispatch_alarm"
        external_id = normalized.pop("external_id") or _auto_external_id()
        observed_at = normalized["reported_at"]
        reading = {key: normalized.get(key) for key in READING_KEYS}

        duplicate = self.repository.find_source_by_external_id(source_type, external_id)
        if duplicate is not None:
            raise self._already_registered(source_type, external_id, duplicate)

        candidate = rules.find_merge_candidate(
            self.repository.list_items(),
            normalized["pipeline_id"],
            normalized["segment_id"],
            observed_at,
        )
        if candidate is not None:
            item, conflicts = self._register_source(
                candidate["id"], source_type, external_id, observed_at, reading, actor, role
            )
            item["merged"] = True
            if conflicts:
                item["message"] = "已并入同管段事件 #%s，但来源与现有结论冲突，事件停在待核验" % candidate["id"]
                item["conflicts"] = conflicts
            else:
                item["message"] = "已并入同管段事件 #%s（半小时窗口内）" % candidate["id"]
            return item

        normalized["last_observed_at"] = observed_at
        initial_source = {
            "source_type": source_type,
            "external_id": external_id,
            "observed_at": observed_at,
            "payload": reading,
        }
        item = self.repository.create_item(
            rules.ENTITY_TYPE,
            stable_key,
            rules.INITIAL_STATUS,
            normalized,
            actor,
            role,
            initial_source=initial_source,
        )
        return self.get_item(item["id"])

    def _already_registered(self, source_type, external_id, duplicate):
        return ConflictError(
            "duplicate_external_id",
            "外部编号 %s/%s 已经登记，请直接查看事件 #%s" % (
                source_type, external_id, duplicate["item_id"]
            ),
            details={
                "source_id": duplicate["id"],
                "item_id": duplicate["item_id"],
                "source_type": source_type,
                "external_id": external_id,
            },
        )

    # ---- 来源登记：异味/压降/浓度冲突时挂起到待核验 ----

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)

        source_type = normalized["source_type"]
        external_id = normalized["external_id"]
        observed_at = normalized["observed_at"]

        duplicate = self.repository.find_source_by_external_id(source_type, external_id)
        if duplicate is not None:
            raise self._already_registered(source_type, external_id, duplicate)

        reading = {key: normalized[key] for key in READING_KEYS if normalized.get(key) is not None}
        stored_payload = dict(reading)
        if normalized.get("note"):
            stored_payload["note"] = normalized["note"]

        item, conflicts = self._register_source(
            item_id, source_type, external_id, observed_at, reading, actor, role,
            stored_payload=stored_payload,
        )
        if conflicts:
            item["message"] = "来源与现有结论存在明显冲突，事件已停在待核验，等待复核员结论"
            item["conflicts"] = conflicts
        return item

    def _register_source(self, item_id, source_type, external_id, observed_at, reading, actor, role, stored_payload=None):
        """公共落库路径：检测冲突，必要时挂起事件；返回 (完整事件, 冲突列表)。"""
        item = self.repository.get_item(item_id)
        existing_sources = self.repository.list_sources(item_id)
        candidate = {"payload": reading}
        conflicts = rules.detect_conflicts(existing_sources, candidate)

        if conflicts:
            previous_status = item["status"]
            if previous_status == rules.PENDING_VERIFICATION:
                previous_status = item["payload"].get("status_before_pending", "reported")
            new_payload = self._absorb_reading(dict(item["payload"]), reading, observed_at)
            pending_record = rules.build_pending_record(
                {
                    "id": None,
                    "source_type": source_type,
                    "external_id": external_id,
                    "observed_at": observed_at,
                },
                conflicts,
            )
            new_payload = rules.apply_conflicts(new_payload, pending_record, previous_status)
            pending_update = {"status": rules.PENDING_VERIFICATION, "payload": new_payload}
        else:
            pending_update = {
                "status": item["status"],
                "payload": self._absorb_reading(dict(item["payload"]), reading, observed_at),
            }

        self.repository.add_source(
            item_id,
            source_type,
            external_id,
            stored_payload if stored_payload is not None else reading,
            observed_at,
            actor,
            role,
            pending_update=pending_update,
        )
        return self.get_item(item_id), conflicts

    def _absorb_reading(self, current, reading, observed_at):
        """无冲突来源刷新事件汇总读数与最近观测时间。"""
        for key in READING_KEYS:
            value = reading.get(key)
            if value is not None:
                current[key] = value
        if observed_at and observed_at > (current.get("last_observed_at") or ""):
            current["last_observed_at"] = observed_at
        return current

    # ---- 动作：待核验时隔离/抢修被挡并返回冲突来源；复核后继续 ----

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
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    # ---- 查询视图：当前事件、来源摘要、下一步缺什么 ----

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        item["source_summary"] = present.source_summary(item["sources"])
        item["next_step"] = present.next_step(item)
        return item

    def list_items(self, status=None):
        items = self.repository.list_items(status)
        for item in items:
            item["source_summary"] = present.source_summary(self.repository.list_sources(item["id"]))
            item["next_step"] = present.next_step(item)
        return items

    def state(self):
        summary = self.repository.state_summary()
        summary["items"] = self.list_items()
        return summary
