from . import rules

STATUS_LABELS = {
    "reported": "已接报",
    "verified": "已核验",
    "isolated": "已隔离",
    "repaired": "已抢修",
    "tested": "试压通过",
    "restored": "已恢复",
    "cancelled": "已取消",
    rules.PENDING_VERIFICATION: "待核验",
}

NEXT_STEPS = {
    "reported": {
        "action": "verify",
        "label": "现场核验",
        "need": "需要现场确认（field_confirmed=true），由调度/抢修角色完成",
        "roles": ["dispatcher", "responder"],
    },
    "verified": {
        "action": "isolate",
        "label": "安排隔离",
        "need": "提交至少两个阀门及顺序 valve_sequence，由值班长/抢修角色执行",
        "roles": ["supervisor", "responder"],
    },
    "isolated": {
        "action": "repair",
        "label": "安排抢修",
        "need": "提交抢修工单号 work_order，由抢修技师执行",
        "roles": ["technician"],
    },
    "repaired": {
        "action": "pressure_test",
        "label": "压力试验",
        "need": "试压通过且试验压力不低于最低要求，由抢修技师执行",
        "roles": ["technician"],
    },
    "tested": {
        "action": "restore",
        "label": "恢复供气",
        "need": "确认现场危险条件已解除（hazards_clear=true），由值班长执行",
        "roles": ["supervisor"],
    },
    "restored": {"action": None, "label": "流程结束", "need": "无", "roles": []},
    "cancelled": {"action": None, "label": "事件关闭", "need": "无", "roles": []},
    rules.PENDING_VERIFICATION: {
        "action": "review",
        "label": "等待复核结论",
        "need": "复核员必须写明结论（conclusion）并给出 decision：confirm_leak/dismiss/escalate；在此之前隔离、抢修等操作一律暂停",
        "roles": ["reviewer", "supervisor"],
    },
}


def status_label(status):
    return STATUS_LABELS.get(status, status)


def source_summary(sources):
    """每条来源一行摘要，并按维度给出取值范围，方便看出来源之间哪里打架。"""
    entries = []
    pressure_values = []
    ppm_values = []
    odor_values = []
    type_counts = {}
    for source in sources:
        data = source.get("payload", {})
        pressure = data.get("pressure_drop_kpa")
        ppm = data.get("sensor_value_ppm")
        odor = data.get("odor_reports")
        entries.append({
            "id": source.get("id"),
            "source_type": source.get("source_type"),
            "external_id": source.get("external_id"),
            "observed_at": source.get("observed_at"),
            "pressure_drop_kpa": pressure,
            "sensor_value_ppm": ppm,
            "odor_reports": odor,
            "note": data.get("note", ""),
        })
        if pressure is not None:
            pressure_values.append(pressure)
        if ppm is not None:
            ppm_values.append(ppm)
        if odor is not None:
            odor_values.append(odor)
        type_counts[source.get("source_type")] = type_counts.get(source.get("source_type"), 0) + 1

    return {
        "total": len(sources),
        "by_type": type_counts,
        "pressure_drop_range": _range(pressure_values),
        "sensor_ppm_range": _range(ppm_values),
        "odor_range": _range(odor_values),
        "entries": entries,
    }


def _range(values):
    if not values:
        return None
    return {"min": min(values), "max": max(values)}


def next_step(item):
    status = item["status"]
    step = dict(NEXT_STEPS.get(status, {"action": None, "label": status, "need": "未知状态", "roles": []}))
    step["status"] = status
    step["status_label"] = status_label(status)
    if status == rules.PENDING_VERIFICATION:
        pending = item["payload"].get("pending_verifications", [])
        step["pending_count"] = len(pending)
        step["conflict_sources"] = [
            {
                "source_id": entry.get("source_id"),
                "source_type": entry.get("source_type"),
                "external_id": entry.get("external_id"),
                "dimensions": [c.get("label") for c in entry.get("conflicts", [])],
            }
            for entry in pending
        ]
    return step
