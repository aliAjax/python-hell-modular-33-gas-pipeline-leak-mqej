# 燃气管线泄漏检测与隔离协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8333`。

- `app.py`：参数、依赖和服务生命周期。
- `src/domain.py`：管段、传感值和来源记录校验。
- `src/rules.py`：泄漏评分、阀门顺序、修复、试压、恢复状态机、告警归并窗口与来源冲突判定。
- `src/repository.py`：SQLite、重复保护、乐观版本和审计链。
- `src/service.py`：角色权限、告警归并编排和业务流程。
- `src/http_api.py`：JSON 接口与首页。
- `src/audit.py`：可校验的审计事件。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8333
python3 -m unittest discover -s tests -v
```

## 告警归并与来源冲突

调度台的告警统一从 `POST /api/alarms` 进入（不再手工建事件）：

- **半小时归并**：同一管线同一管段、距已有未关闭事件的任一来源观测时间 30 分钟内的告警，自动并入该事件（响应 `merged=true`），不再重复安排隔离；超窗或事件已关闭（已恢复/已驳回）则新建。
- **外部编号查重**：`source_type + external_id` 已存在时返回 409 `duplicate_source`，提示“已经登记”，并在 `details` 中给出所在事件 `item_id` 与管段。
- **冲突挂待核验**：并入的来源在异味、压降、浓度三个维度上与现有结论明显对立（如初报压降 35kPa/220ppm/有异味，传感器上报 0/0/0）时，事件进入 `pending_review`。阈值采用双阈值带死区：压降 ≥10kPa 或 ≤2kPa、浓度 ≥50ppm 或 ≤10ppm、异味 ≥1 或为 0，中间区间视为不明确，避免抖动误报。
- **处置锁定**：待核验期间除 `review` 外所有动作返回 409 `pending_review`，`details.conflicts` 列出冲突维度及异常/正常两侧的具体来源。
- **复核解锁**：新增 `reviewer` 角色（值班主任也可复核）。`review` 动作必须填写 `conclusion`，并选择 `decision=proceed`（采信继续，相反来源标记为已驳回，事件回到挂起前状态）或 `decision=cancel`（判为误报，事件驳回）。复核结论进入审计链，未复核不会自动解锁。
- **调度台页面**：首页列出当前事件及状态计数、每条事件的来源摘要（三个维度的异常/正常标记、驳回状态）、冲突双方和“下一步缺什么”（如待现场核验、缺阀门顺序、缺工单号、待复核结论等）。

接口包括 `GET /health`、`GET /api/state`、`GET /api/items/<id>`、`POST /api/items`（兼容手工建档）、`POST /api/alarms`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`（含 `review`）和审计查询。测试覆盖完整抢修流程、告警半小时归并、外部编号重复、来源冲突挂起、隔离/抢修锁定、复核继续与误报驳回、阀门顺序、试压阈值、现场危险条件、权限和版本冲突。模型不替代 SCADA、管网水力计算或正式应急预案。
