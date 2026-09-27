# 燃气管线泄漏检测与隔离协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8333`。

- `app.py`：参数、依赖和服务生命周期。
- `src/domain.py`：管段、传感值和来源记录校验。
- `src/rules.py`：泄漏评分、阀门顺序、修复、试压、恢复状态机。
- `src/repository.py`：SQLite、重复保护、乐观版本和审计链。
- `src/service.py`：角色权限和业务编排。
- `src/http_api.py`：JSON 接口与首页。
- `src/audit.py`：可校验的审计事件。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8333
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和审计查询。

调度去重与冲突处理：

- 同管段（`pipeline_id`+`segment_id`）半小时窗口内的新告警自动并入未结案事件，返回 `merged=true`，不再重复建单、重复隔离；已恢复/已取消事件不作为合并目标。
- 告警和来源都带 `external_id`，全库按来源类型+外部编号去重；重复提交返回 `409 duplicate_external_id`，并在 `details` 中给出已登记的事件与来源编号。
- 每条新来源按异味、压降、浓度三个维度与既有来源比对：仅在"明确阳性（压降≥5kPa / 浓度≥10ppm / 异味≥1起）与明确归零"之间判为明显冲突。冲突时事件停在 `pending_verification`，隔离、抢修等动作返回 `409 source_conflict_pending`，`details.pending_verifications` 列出冲突来源与维度。
- 复核员（`reviewer`，或值班长）调用 `review` 动作写明 `conclusion` 后事件才继续：`confirm_leak` 回到挂起前状态，`dismiss` 取消事件，`escalate` 留档但继续挂起。
- `GET /api/state` 与事件详情包含 `source_summary`（按类型计数、三维度取值范围、逐来源摘要）和 `next_step`（当前状态、下一步动作、缺什么、允许角色），首页据此列出当前事件、来源摘要和待办。

测试覆盖完整抢修流程、同管段半小时合并（窗口外/异管段/结案不合并）、外部编号重复、来源冲突挂起、隔离/抢修被挡并返回冲突来源、复核解锁与取消、阀门顺序、试压阈值、现场危险条件、权限和版本冲突。模型不替代 SCADA、管网水力计算或正式应急预案。
