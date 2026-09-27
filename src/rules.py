from datetime import datetime, timedelta

from .domain import DomainError, ConflictError

ENTITY_TYPE = "pipeline_leak"
INITIAL_STATUS = "reported"
MERGE_WINDOW = timedelta(minutes=30)
TERMINAL_STATUSES = {"restored", "cancelled"}
PENDING_VERIFICATION = "pending_verification"
CREATE_ROLES = {"dispatcher", "responder"}
SOURCE_ROLES = {"dispatcher", "responder", "patrol", "sensor"}
ACTION_ROLES = {
    "verify": {"dispatcher", "responder"},
    "isolate": {"supervisor", "responder"},
    "repair": {"technician"},
    "pressure_test": {"technician"},
    "restore": {"supervisor"},
    "cancel": {"supervisor"},
    "review": {"reviewer", "supervisor"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"isolate", "repair", "pressure_test", "restore", "cancel", "review"}

# 明显阳性阈值：达到该值才视为"有异味/有压降/有浓度"
PRESSURE_POSITIVE_KPA = 5.0
PPM_POSITIVE = 10.0
ODOR_POSITIVE = 1

CONFLICT_DIMENSIONS = (
    ("pressure_drop_kpa", "pressure", PRESSURE_POSITIVE_KPA, "压降"),
    ("sensor_value_ppm", "concentration", PPM_POSITIVE, "浓度"),
    ("odor_reports", "odor", ODOR_POSITIVE, "异味"),
)


def parse_dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def find_merge_candidate(item_rows, pipeline_id, segment_id, observed_at):
    """同管段、未结案、且最近一条告警来源在半小时内的事件。"""
    moment = parse_dt(observed_at)
    best = None
    best_last = None
    for row in item_rows:
        payload = row.get("payload", {})
        if payload.get("pipeline_id") != pipeline_id or payload.get("segment_id") != segment_id:
            continue
        if row.get("status") in TERMINAL_STATUSES:
            continue
        last_seen = payload.get("last_observed_at") or payload.get("reported_at")
        if not last_seen:
            continue
        delta = abs(moment - parse_dt(last_seen))
        if delta <= MERGE_WINDOW and (best_last is None or last_seen > best_last):
            best, best_last = row, last_seen
    return best


def _dimension_reading(source, key):
    if "payload" in source and isinstance(source.get("payload"), dict):
        value = source["payload"].get(key)
    else:
        value = source.get(key)
    return float(value) if value is not None else None


def detect_conflicts(existing_sources, new_source):
    """返回冲突维度说明；仅在"明确阳性 vs 明确归零"之间判为明显冲突。"""
    conflicts = []
    for payload_key, dim, threshold, label in CONFLICT_DIMENSIONS:
        incoming = _dimension_reading(new_source, payload_key)
        if incoming is None:
            continue
        existing_positive = []
        existing_zero = []
        for source in existing_sources:
            reading = _dimension_reading(source.get("payload", source), payload_key)
            if reading is None:
                continue
            if reading >= threshold:
                existing_positive.append(source)
            elif reading == 0:
                existing_zero.append(source)
        incoming_positive = incoming >= threshold
        incoming_zero = incoming == 0
        if incoming_positive and existing_zero:
            conflicts.append({
                "dimension": dim,
                "label": label,
                "new_says": "positive",
                "new_value": incoming,
                "conflicting_source_ids": [s["id"] for s in existing_zero if "id" in s],
            })
        elif incoming_zero and existing_positive:
            conflicts.append({
                "dimension": dim,
                "label": label,
                "new_says": "clear",
                "new_value": incoming,
                "conflicting_source_ids": [s["id"] for s in existing_positive if "id" in s],
            })
    return conflicts


def build_pending_record(source, conflicts):
    return {
        "source_id": source.get("id"),
        "source_type": source.get("source_type"),
        "external_id": source.get("external_id"),
        "observed_at": source.get("observed_at"),
        "conflicts": conflicts,
    }


def apply_conflicts(current, pending_record, previous_status):
    """把待核验记录并入事件载荷，挂起事件。返回事件载荷。"""
    current = dict(current)
    pending = list(current.get("pending_verifications", []))
    existing_ids = {entry.get("source_id") for entry in pending}
    existing_keys = {(entry.get("source_type"), entry.get("external_id")) for entry in pending}
    key = (pending_record.get("source_type"), pending_record.get("external_id"))
    if pending_record["source_id"] not in existing_ids and key not in existing_keys:
        pending.append(pending_record)
    current["pending_verifications"] = pending
    if "status_before_pending" not in current:
        current["status_before_pending"] = previous_status
    return current


def assess(payload):
    pressure = float(payload.get("pressure_drop_kpa", 0))
    ppm = float(payload.get("sensor_value_ppm", 0))
    odor = int(payload.get("odor_reports", 0))
    score = min(100.0, pressure * 2.0 + min(ppm, 500.0) * 0.1 + odor * 5.0)
    if score >= 75:
        level = "critical"
    elif score >= 45:
        level = "high"
    elif score >= 20:
        level = "medium"
    else:
        level = "low"
    return {"score": round(score, 2), "level": level}


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _block_if_pending(item, action):
    if item["status"] != PENDING_VERIFICATION:
        return
    current = item["payload"]
    pending = current.get("pending_verifications", [])
    details = {
        "action": action,
        "current_status": PENDING_VERIFICATION,
        "pending_verifications": pending,
        "message": "来源之间存在未核验冲突，等待复核员出具结论后才能继续",
    }
    raise ConflictError("source_conflict_pending", details["message"]).with_details(details)


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])

    if action == "review":
        _need_status(item, {PENDING_VERIFICATION})
        conclusion = _text(payload, "conclusion")
        decision = payload.get("decision", "confirm_leak")
        if decision not in ("confirm_leak", "dismiss", "escalate"):
            raise DomainError("invalid_decision", "复核结论必须是 confirm_leak、dismiss 或 escalate")
        reviews = list(current.get("reviews", []))
        review_entry = {
            "actor": actor,
            "conclusion": conclusion,
            "decision": decision,
            "resolved_source_ids": [entry.get("source_id") for entry in current.get("pending_verifications", [])],
        }
        reviews.append(review_entry)
        current["reviews"] = reviews
        if decision == "dismiss":
            current.pop("pending_verifications", None)
            current.pop("status_before_pending", None)
            current["cancellation"] = {"reason": conclusion, "actor": actor, "by_review": True}
            return "cancelled", current, {"review": review_entry}
        if decision == "escalate":
            # 升级上报：记录复核意见，事件继续停在待核验
            current["escalations"] = current.get("escalations", []) + [review_entry]
            return PENDING_VERIFICATION, current, {"review": review_entry, "escalated": True}
        resume_status = current.pop("status_before_pending", "reported")
        current.pop("pending_verifications", None)
        return resume_status, current, {"review": review_entry, "resume_status": resume_status}

    _block_if_pending(item, action)

    if action == "verify":
        _need_status(item, {"reported", "verified"})
        if current.get("valve_status_conflict"):
            raise DomainError("valve_status_conflict", "阀门状态存在冲突，不能完成核验", 409)
        confirmed = bool(payload.get("field_confirmed"))
        if not confirmed:
            raise DomainError("field_confirmation_required", "需要现场确认", 409)
        current["assessment"] = assess(current)
        current["verification"] = {"confirmed": True, "note": payload.get("note", "")}
        return "verified", current, {"assessment": current["assessment"], "verification": current["verification"]}

    if action == "isolate":
        _need_status(item, {"verified"})
        sequence = payload.get("valve_sequence")
        if not isinstance(sequence, list) or len(sequence) < 2:
            raise DomainError("valve_sequence_required", "至少需要提交两个阀门及顺序")
        if current.get("valve_status_conflict"):
            raise DomainError("valve_status_conflict", "阀门状态存在冲突，不能隔离", 409)
        if not all(isinstance(value, str) and value.strip() for value in sequence):
            raise DomainError("invalid_valve_sequence", "阀门顺序格式无效")
        current["valve_sequence"] = [value.strip() for value in sequence]
        return "isolated", current, {"valve_sequence": current["valve_sequence"]}

    if action == "repair":
        _need_status(item, {"isolated", "repaired"})
        work_order = _text(payload, "work_order")
        current["repair"] = {"work_order": work_order, "result": payload.get("result", "completed")}
        return "repaired", current, {"work_order": work_order}

    if action == "pressure_test":
        _need_status(item, {"repaired", "tested"})
        if not payload.get("test_passed"):
            raise DomainError("pressure_test_failed", "压力测试未通过，不能恢复供气", 409)
        pressure = float(payload.get("pressure_kpa", 0))
        minimum = float(payload.get("minimum_pressure_kpa", 100))
        if pressure < minimum:
            raise DomainError("pressure_below_threshold", "试验压力低于最低要求", 409)
        current["pressure_test"] = {"passed": True, "pressure_kpa": pressure, "minimum_pressure_kpa": minimum}
        return "tested", current, {"pressure_test": current["pressure_test"]}

    if action == "restore":
        _need_status(item, {"tested"})
        if not payload.get("hazards_clear"):
            raise DomainError("hazards_not_clear", "现场危险条件尚未解除", 409)
        if not current.get("pressure_test", {}).get("passed"):
            raise DomainError("pressure_test_missing", "缺少通过的压力测试", 409)
        current["hazards_clear"] = True
        current["restoration"] = {"actor": actor, "note": payload.get("note", "")}
        return "restored", current, {"restoration": current["restoration"]}

    if action == "cancel":
        _need_status(item, {"reported", "verified"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
