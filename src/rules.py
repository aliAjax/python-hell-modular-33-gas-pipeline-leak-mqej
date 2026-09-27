from datetime import datetime, timedelta

from .domain import DomainError

ENTITY_TYPE = "pipeline_leak"
INITIAL_STATUS = "reported"
STATUS_PENDING_REVIEW = "pending_review"
CLOSED_STATUSES = {"cancelled", "restored"}
CREATE_ROLES = {"dispatcher", "responder"}
SOURCE_ROLES = {"dispatcher", "responder", "patrol", "sensor"}
ALARM_ROLES = {"dispatcher", "sensor"}
REVIEW_ROLES = {"reviewer", "supervisor"}
ACTION_ROLES = {
    "verify": {"dispatcher", "responder"},
    "isolate": {"supervisor", "responder"},
    "repair": {"technician"},
    "pressure_test": {"technician"},
    "restore": {"supervisor"},
    "cancel": {"supervisor"},
    "review": REVIEW_ROLES,
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"isolate", "repair", "pressure_test", "restore", "cancel", "review"}

# 同管段告警并入已有事件的时间窗口
ALARM_MERGE_WINDOW = timedelta(minutes=30)

# 信号冲突阈值：达到正向阈值视为“有泄漏迹象”，不超过反向阈值视为“未见异常”，
# 中间区间视为不明确，不参与冲突判定，避免测量抖动造成假冲突。
PRESSURE_POSITIVE_KPA = 10.0
PRESSURE_NEGATIVE_KPA = 2.0
PPM_POSITIVE = 50.0
PPM_NEGATIVE = 10.0
ODOR_POSITIVE = 1

DIMENSION_LABELS = {
    "pressure_drop": "压降",
    "concentration": "浓度",
    "odor": "异味",
}
STATUS_LABELS = {
    "reported": "已上报",
    "verified": "已核验",
    "isolated": "已隔离",
    "repaired": "已抢修",
    "tested": "已试压",
    "restored": "已恢复",
    "cancelled": "已驳回",
    STATUS_PENDING_REVIEW: "待核验",
}


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


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def _verdict(dimension, value):
    """把单个观测量映射成 positive / negative / None（不明确）。"""
    if value is None:
        return None
    if dimension == "pressure_drop":
        if value >= PRESSURE_POSITIVE_KPA:
            return "positive"
        if value <= PRESSURE_NEGATIVE_KPA:
            return "negative"
    elif dimension == "concentration":
        if value >= PPM_POSITIVE:
            return "positive"
        if value <= PPM_NEGATIVE:
            return "negative"
    elif dimension == "odor":
        # 异味只有 0（无报告）和 >=1（有报告）两档
        if value >= ODOR_POSITIVE:
            return "positive"
        return "negative"
    return None


def _signal(source_ref, dimension, value):
    verdict = _verdict(dimension, value)
    if verdict is None:
        return None
    return {
        "dimension": dimension,
        "dimension_label": DIMENSION_LABELS[dimension],
        "value": value,
        "verdict": verdict,
        "source_id": source_ref.get("source_id"),
        "source_type": source_ref.get("source_type"),
        "external_id": source_ref.get("external_id"),
        "label": source_ref.get("label"),
    }


def baseline_signals(item):
    """建事件的第一条告警作为基线结论（初报）。"""
    payload = item["payload"]
    origin = payload.get("origin") or {}
    ref = {
        "source_id": None,
        "source_type": origin.get("source_type"),
        "external_id": origin.get("external_id"),
        "label": "初报（%s）" % payload.get("reporter", "未知"),
    }
    values = (
        ("pressure_drop", payload.get("pressure_drop_kpa")),
        ("concentration", payload.get("sensor_value_ppm")),
        ("odor", payload.get("odor_reports")),
    )
    return [signal for dimension, value in values if (signal := _signal(ref, dimension, value))]


def extract_source_signals(source):
    """从一条来源记录提取异味/压降/浓度信号；缺测维度不产生信号。"""
    payload = source.get("payload", {})
    ref = {
        "source_id": source.get("id"),
        "source_type": source.get("source_type"),
        "external_id": source.get("external_id"),
        "label": "%s/%s" % (source.get("source_type"), source.get("external_id")),
    }
    values = (
        ("pressure_drop", payload.get("pressure_drop_kpa")),
        ("concentration", payload.get("sensor_value_ppm")),
        ("odor", payload.get("odor_reports")),
    )
    return [signal for dimension, value in values if (signal := _signal(ref, dimension, value))]


def detect_conflicts(signals):
    """同一维度同时出现“有泄漏迹象”和“未见异常”两边来源，即构成明显冲突。"""
    grouped = {}
    for signal in signals:
        grouped.setdefault(signal["dimension"], []).append(signal)
    conflicts = []
    for dimension in ("odor", "pressure_drop", "concentration"):
        sides = grouped.get(dimension)
        if not sides:
            continue
        positive = _unique_sources([s for s in sides if s["verdict"] == "positive"])
        negative = _unique_sources([s for s in sides if s["verdict"] == "negative"])
        if positive and negative:
            conflicts.append({
                "dimension": dimension,
                "dimension_label": DIMENSION_LABELS[dimension],
                "positive_sources": positive,
                "negative_sources": negative,
            })
    return conflicts


def _unique_sources(signals):
    """同一来源在一个维度上只会有一条信号；按来源身份去重，初报没有 source_id 用标签兜底。"""
    seen = set()
    result = []
    for signal in signals:
        key = signal["source_id"] if signal["source_id"] is not None else ("origin", signal["label"])
        if key in seen:
            continue
        seen.add(key)
        result.append(signal)
    return result


def _parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def choose_merge_item(candidates, sources_map, observed_at, window=ALARM_MERGE_WINDOW):
    """在同管段未关闭事件里，找到告警时间落在半小时窗口内的最近事件。"""
    observed = _parse_ts(observed_at)
    for item in candidates:  # 调用方按 id 倒序传入
        times = [_parse_ts(item["created_at"])]
        for source in sources_map.get(item["id"], []):
            times.append(_parse_ts(source["observed_at"]))
        if any(abs(observed - moment) <= window for moment in times):
            return item["id"]
    return None


def plan_review(item, sources, now):
    """来源并入后评估是否要挂到待核验。返回状态/载荷变更；无变化返回 None。

    已经在待核验时只刷新冲突双方清单，不能因为又来一条“正常”来源就自动解锁——
    必须由复核员写明结论。告警建事件时初报已作为第一条来源落库，直接参与判定；
    只有没有任何来源的旧手工事件才回退到 payload 基线。
    """
    payload = item["payload"]
    pending = payload.get("pending_review")
    dismissed = set(payload.get("dismissed_source_ids", []))
    dismissed.update((pending or {}).get("dismissed_source_ids", []))
    signals = []
    origin = payload.get("origin") or {}
    origin_matched = False
    for source in sources:
        if source["id"] in dismissed:
            continue
        signals.extend(extract_source_signals(source))
        if (source["source_type"], source["external_id"]) == (origin.get("source_type"), origin.get("external_id")):
            origin_matched = True
    # 初报没有作为来源落库（旧的手工建档）时，才用 payload 基线补一条
    if not sources and not origin_matched:
        signals.extend(baseline_signals(item))
    conflicts = detect_conflicts(signals)

    if conflicts and item["status"] != STATUS_PENDING_REVIEW:
        new_payload = dict(payload)
        new_payload["pending_review"] = {
            "status_snapshot": item["status"],
            "raised_at": now,
            "dismissed_source_ids": [],
            "conflicts": conflicts,
        }
        return {
            "status": STATUS_PENDING_REVIEW,
            "payload": new_payload,
            "event_type": "held_for_review",
            "event_payload": {"conflicts": conflicts, "snapshot": item["status"]},
        }
    if conflicts and pending is not None:
        if pending.get("conflicts") == conflicts:
            return None
        new_payload = dict(payload)
        new_payload["pending_review"] = dict(pending)
        new_payload["pending_review"]["conflicts"] = conflicts
        new_payload["pending_review"]["updated_at"] = now
        return {
            "status": STATUS_PENDING_REVIEW,
            "payload": new_payload,
            "event_type": "review_conflicts_updated",
            "event_payload": {"conflicts": conflicts},
        }
    return None


def _conflict_details(payload):
    return (payload.get("pending_review") or {}).get("conflicts", [])


def next_step(item):
    """告诉调度台当前事件下一步缺什么。"""
    status = item["status"]
    payload = item["payload"]
    if status == STATUS_PENDING_REVIEW:
        return {
            "action": "review",
            "label": "待复核：来源结论互相打架，需要复核员写明结论",
            "missing": "conclusion（复核结论）与 decision（proceed 继续处置 / cancel 判为误报）",
            "conflicts": _conflict_details(payload),
        }
    steps = {
        "reported": ("verify", "等待现场核验确认", "field_confirmed=true"),
        "verified": ("isolate", "等待安排隔离", "valve_sequence（至少两个阀门及顺序）"),
        "isolated": ("repair", "等待抢修", "work_order（抢修工单号）"),
        "repaired": ("pressure_test", "等待试压", "test_passed=true 与 pressure_kpa"),
        "tested": ("restore", "等待恢复供气", "hazards_clear=true（现场危险条件解除）"),
    }
    if status in steps:
        action, label, missing = steps[status]
        return {"action": action, "label": label, "missing": missing}
    return {"action": None, "label": "事件已结束", "missing": None}


def apply_action(item, action, payload, actor, role, now=None):
    status = item["status"]
    current = dict(item["payload"])

    # 待核验期间，除复核本身外的所有动作一律停住，并把冲突来源带回给调用方
    if status == STATUS_PENDING_REVIEW and action != "review":
        raise DomainError(
            "pending_review",
            "来源信号存在冲突，事件已停在待核验；复核员写明结论前不能隔离或抢修",
            409,
            details={"conflicts": _conflict_details(current), "next_step": "review"},
        )

    if action == "review":
        _need_status(item, {STATUS_PENDING_REVIEW})
        decision = payload.get("decision")
        if decision not in ("proceed", "cancel"):
            raise DomainError("decision_required", "decision 必须是 proceed（继续处置）或 cancel（判为误报）")
        conclusion = _text(payload, "conclusion")
        pending = current.get("pending_review") or {}
        record = {
            "decision": decision,
            "conclusion": conclusion,
            "actor": actor,
            "at": now,
            "conflicts": pending.get("conflicts", []),
        }
        current.setdefault("reviews", []).append(record)
        dismissed = set(current.get("dismissed_source_ids", []))
        if decision == "proceed":
            # 与“继续处置”结论相反一侧的外部来源标记为已驳回，后续不再触发冲突
            for conflict in pending.get("conflicts", []):
                for signal in conflict["negative_sources"]:
                    if signal["source_id"] is not None:
                        dismissed.add(signal["source_id"])
            current["dismissed_source_ids"] = sorted(dismissed)
            current.pop("pending_review", None)
            snapshot = pending.get("status_snapshot", "reported")
            return snapshot, current, {"review": record, "resumed_status": snapshot}
        current["cancellation"] = {"reason": conclusion, "actor": actor, "via": "review"}
        current.pop("pending_review", None)
        return "cancelled", current, {"review": record, "reason": conclusion}

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
