import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    pipeline_id TEXT NOT NULL DEFAULT '',
                    segment_id TEXT NOT NULL DEFAULT '',
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_items_segment ON items(entity_type, pipeline_id, segment_id);
                CREATE INDEX IF NOT EXISTS idx_sources_external ON sources(source_type, external_id);
                """
            )
            self._ensure_columns(conn, {
                "items": {
                    "pipeline_id": "TEXT NOT NULL DEFAULT ''",
                    "segment_id": "TEXT NOT NULL DEFAULT ''",
                }
            })
        finally:
            conn.close()

    def _ensure_columns(self, conn, columns_by_table):
        for table, columns in columns_by_table.items():
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
            for name, definition in columns.items():
                if name not in existing:
                    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, definition))

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role, pipeline_id=None, segment_id=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at,pipeline_id,segment_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                        pipeline_id or "",
                        segment_id or "",
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role, review_planner=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            existing = self._find_source_conn(conn, source_type, external_id)
            if existing is not None:
                self._raise_duplicate_source(conn, existing)
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            item = self._row_to_item(row)
            held = None
            if review_planner:
                sources = [self._get_source_conn(conn, source_id)]
                held = self._apply_review_plan(conn, item, review_planner, sources, actor, role)
            conn.execute("COMMIT")
            result = self.get_item(item_id)
            return {"source_id": source_id, "item": result, "held": held}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _raise_duplicate_source(self, conn, existing):
        row = conn.execute("SELECT pipeline_id, segment_id FROM items WHERE id=?", (existing["item_id"],)).fetchone()
        details = {
            "source_id": existing["id"],
            "item_id": existing["item_id"],
            "source_type": existing["source_type"],
            "external_id": existing["external_id"],
        }
        if row is not None:
            details["pipeline_id"] = row["pipeline_id"]
            details["segment_id"] = row["segment_id"]
        raise ConflictError(
            "duplicate_source",
            "外部编号 %s/%s 已经登记，已并入事件 #%s，无需重复安排" % (
                existing["source_type"], existing["external_id"], existing["item_id"]
            ),
            details=details,
        )

    def _find_source_conn(self, conn, source_type, external_id):
        return conn.execute(
            "SELECT * FROM sources WHERE source_type=? AND external_id=? ORDER BY id LIMIT 1",
            (source_type, external_id),
        ).fetchone()

    def find_source(self, source_type, external_id):
        conn = self.connect()
        try:
            row = self._find_source_conn(conn, source_type, external_id)
            return self._source_row(row) if row is not None else None
        finally:
            conn.close()

    def _source_row(self, row):
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _get_source_conn(self, conn, source_id):
        return self._source_row(conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())

    def _apply_review_plan(self, conn, item, review_planner, extra_sources, actor, role):
        sources = []
        for row in conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id", (item["id"],)).fetchall():
            sources.append(self._source_row(row))
        sources.extend(extra_sources)
        plan = review_planner(item, sources)
        if plan is None:
            return None
        version = int(item["version"]) + 1
        conn.execute(
            "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
            (plan["status"], version, canonical_json(plan["payload"]), now_iso(), item["id"]),
        )
        self.append_audit(conn, item["id"], plan["event_type"], actor, role, plan["event_payload"])
        return {"status": plan["status"], "event_type": plan["event_type"], "conflicts": plan["event_payload"].get("conflicts", [])}

    def ingest_alarm(self, entity_type, alarm, actor, role, initial_status, review_planner):
        """告警入口的单事务：查重 → 找同管段半小时窗口内的事件 → 新建或并入 → 冲突检测。

        - 外部编号已登记：抛 duplicate_source（带所在事件）
        - 命中窗口：并入已有事件，返回 merged=True
        - 否则：新建事件并把这条告警作为初报来源，返回 merged=False
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = self._find_source_conn(conn, alarm["source_type"], alarm["external_id"])
            if existing is not None:
                self._raise_duplicate_source(conn, existing)

            candidates = [
                self._row_to_item(row)
                for row in conn.execute(
                    "SELECT * FROM items WHERE entity_type=? AND pipeline_id=? AND segment_id=? ORDER BY id DESC",
                    (entity_type, alarm["pipeline_id"], alarm["segment_id"]),
                ).fetchall()
            ]
            from . import rules as _rules
            open_candidates = [item for item in candidates if item["status"] not in _rules.CLOSED_STATUSES]
            sources_map = {}
            if open_candidates:
                placeholders = ",".join("?" for _ in open_candidates)
                for row in conn.execute(
                    "SELECT * FROM sources WHERE item_id IN (%s) ORDER BY id" % placeholders,
                    tuple(item["id"] for item in open_candidates),
                ).fetchall():
                    sources_map.setdefault(row["item_id"], []).append(self._source_row(row))
            merge_id = _rules.choose_merge_item(open_candidates, sources_map, alarm["observed_at"])

            timestamp = now_iso()
            if merge_id is None:
                payload = {
                    "pipeline_id": alarm["pipeline_id"],
                    "segment_id": alarm["segment_id"],
                    "reported_at": alarm["observed_at"],
                    "pressure_drop_kpa": alarm["pressure_drop_kpa"],
                    "sensor_value_ppm": alarm["sensor_value_ppm"],
                    "odor_reports": alarm["odor_reports"],
                    "reporter": alarm["reporter"],
                    "source_comparison": [],
                    "valve_sequence": [],
                    "hazards_clear": False,
                    "origin": {
                        "source_type": alarm["source_type"],
                        "external_id": alarm["external_id"],
                    },
                }
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at,pipeline_id,segment_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        alarm["stable_key"],
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        timestamp,
                        timestamp,
                        alarm["pipeline_id"],
                        alarm["segment_id"],
                    ),
                )
                item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                source_payload = {
                    "sensor_value_ppm": alarm["sensor_value_ppm"],
                    "pressure_drop_kpa": alarm["pressure_drop_kpa"],
                    "odor_reports": alarm["odor_reports"],
                    "note": alarm.get("note", ""),
                }
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, alarm["source_type"], alarm["external_id"], canonical_json(source_payload), alarm["observed_at"], timestamp),
                )
                source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                self.append_audit(conn, item_id, "created", actor, role, {
                    "stable_key": alarm["stable_key"],
                    "via": "alarm",
                    "source_type": alarm["source_type"],
                    "external_id": alarm["external_id"],
                })
                self.append_audit(conn, item_id, "source_recorded", actor, role, {
                    "source_id": source_id, "source_type": alarm["source_type"], "external_id": alarm["external_id"]
                })
                merged = False
                held = None
            else:
                item_id = merge_id
                source_payload = {
                    "sensor_value_ppm": alarm["sensor_value_ppm"],
                    "pressure_drop_kpa": alarm["pressure_drop_kpa"],
                    "odor_reports": alarm["odor_reports"],
                    "note": alarm.get("note", ""),
                }
                try:
                    conn.execute(
                        "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                        (item_id, alarm["source_type"], alarm["external_id"], canonical_json(source_payload), alarm["observed_at"], timestamp),
                    )
                except sqlite3.IntegrityError:
                    existing = self._find_source_conn(conn, alarm["source_type"], alarm["external_id"])
                    self._raise_duplicate_source(conn, existing)
                source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                self.append_audit(conn, item_id, "source_recorded", actor, role, {
                    "source_id": source_id,
                    "source_type": alarm["source_type"],
                    "external_id": alarm["external_id"],
                    "merged": True,
                    "window_minutes": 30,
                })
                merged_item = self._row_to_item(
                    conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
                )
                held = self._apply_review_plan(
                    conn, merged_item, review_planner,
                    [self._source_row(conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())],
                    actor, role,
                )
                merged = True
            conn.execute("COMMIT")
            return {"item": self.get_item(item_id), "merged": merged, "source_id": source_id, "held": held}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()
