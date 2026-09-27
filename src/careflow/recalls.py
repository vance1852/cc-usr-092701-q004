"""供应商召回通知、按批号快照的受影响病例与召回处置跟踪。"""

from __future__ import annotations

from typing import Any

from . import audit
from .db import Database
from .errors import Conflict, NotFound, ValidationError
from .ids import new_id
from .security import authorize, principal_for
from .validation import calendar_date, choice, integer, request_digest, require_match, text, timestamp

URGENCY_LEVELS = {"routine", "high", "urgent"}
URGENCY_RANK = {"routine": 0, "high": 1, "urgent": 2}
CONTACT_RESULTS = {"reached", "unreachable", "refused", "callback_requested"}
CASE_STAGES = {"pending", "contacted", "observing", "resolved", "closed"}
# contact 操作负责 待联络 → 已联络；advance 处理其后的推进以及无需联络的直接处置。
ADVANCE_TRANSITIONS = {
    "pending": {"resolved"},
    "contacted": {"observing", "resolved"},
    "observing": {"resolved"},
    "resolved": {"closed"},
}
ASSIGNABLE_ROLES = {"clinician", "nurse", "owner", "coordinator"}


def _case_event(connection, case_id: str, event_type: str, actor_id: str | None, note: str, now: str, *,
                from_stage: str | None = None, to_stage: str | None = None,
                from_urgency: str | None = None, to_urgency: str | None = None) -> None:
    sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM recall_case_events WHERE case_id=?",
                                  (case_id,)).fetchone()[0]
    connection.execute(
        "INSERT INTO recall_case_events(id,case_id,sequence,event_type,actor_id,note,from_stage,to_stage,from_urgency,to_urgency,occurred_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (new_id("rce"), case_id, sequence, event_type, actor_id, note, from_stage, to_stage, from_urgency, to_urgency, now))


def note_consumption_during_recall(connection, *, clinic_id: str, reservation, actor_id: str, now: str) -> None:
    """召回活动未关闭时发生的核销：记录保留在召回清单内，并标记病例进入人工复核。"""
    recall = connection.execute("SELECT * FROM recalls WHERE clinic_id=? AND lot_id=? AND state='open'",
                                (clinic_id, reservation["lot_id"])).fetchone()
    if recall is None:
        return
    item = connection.execute("SELECT case_id FROM recall_case_items WHERE reservation_id=?",
                              (reservation["id"],)).fetchone()
    if item is not None:
        case_id = item["case_id"]
        note = "召回期间完成快照内预留的核销，进入人工复核"
    else:
        case = connection.execute("SELECT id FROM recall_cases WHERE recall_id=? AND patient_id=?",
                                  (recall["id"], reservation["patient_id"])).fetchone()
        if case is None:
            case_id = new_id("rcs")
            connection.execute(
                "INSERT INTO recall_cases(id,recall_id,clinic_id,patient_id,stage,urgency,assigned_to,review_required,created_at,updated_at) "
                "VALUES(?,?,?,?,'pending',?,?,0,?,?)",
                (case_id, recall["id"], clinic_id, reservation["patient_id"], recall["urgency"], recall["opened_by"], now, now))
        else:
            case_id = case["id"]
        connection.execute(
            "INSERT INTO recall_case_items(id,case_id,reservation_id,appointment_id,quantity,created_at) VALUES(?,?,?,?,?,?)",
            (new_id("rci"), case_id, reservation["id"], reservation["appointment_id"], reservation["quantity"], now))
        note = "召回期间新增核销记录，进入人工复核"
    connection.execute("UPDATE recall_cases SET review_required=1,updated_at=?,version=version+1 WHERE id=?", (now, case_id))
    _case_event(connection, case_id, "consumed_during_recall", actor_id, note, now)
    audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=reservation["patient_id"],
                       aggregate_type="recall_case", aggregate_id=case_id, action="recall_case.consumed_during_recall",
                       occurred_at=now, payload={"recall_id": recall["id"], "reservation_id": reservation["id"]})


class RecallService:
    """召回活动按批号建立；病例快照只覆盖该批号的预留与核销记录，不混入同产品其他批号。"""

    def __init__(self, database: Database, clock):
        self.db = database
        self.clock = clock

    def now(self) -> str:
        return timestamp(self.clock.now())

    @staticmethod
    def _notice_result(row) -> dict[str, Any]:
        return {"id": row["id"], "notice_ref": row["notice_ref"], "revision": row["revision"],
                "urgency": row["urgency"], "summary": row["summary"], "received_at": row["received_at"]}

    @staticmethod
    def _case_result(row) -> dict[str, Any]:
        return {"id": row["id"], "recall_id": row["recall_id"], "patient_id": row["patient_id"],
                "stage": row["stage"], "urgency": row["urgency"], "assigned_to": row["assigned_to"],
                "contact_result": row["contact_result"], "next_review_on": row["next_review_on"],
                "review_required": bool(row["review_required"]), "version": row["version"],
                "updated_at": row["updated_at"]}

    def import_notice(self, clinic_id: str, actor_id: str, lot_id: str, notice_ref: str,
                      revision: int, urgency: str, summary: str, *, received_at: str | None = None) -> dict[str, Any]:
        notice_ref = text(notice_ref, "供应商通知编号", maximum=120)
        revision = integer(revision, "通知修订版本", minimum=1, maximum=1000000)
        urgency = choice(urgency, "紧急程度", URGENCY_LEVELS)
        summary = text(summary, "通知摘要", maximum=2000)
        received = timestamp(received_at, "通知接收时间") if received_at else self.now()
        now = self.now()
        request_hash = request_digest({"lot_id": lot_id, "notice_ref": notice_ref, "revision": revision,
                                       "urgency": urgency, "summary": summary})
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "incident:manage", clinic_id=clinic_id)
            lot = connection.execute(
                "SELECT l.id,l.lot_number FROM product_lots l JOIN products p ON p.id=l.product_id "
                "WHERE l.id=? AND p.clinic_id=?", (lot_id, clinic_id)).fetchone()
            if lot is None:
                raise NotFound("耗材批次不存在")
            recall = connection.execute("SELECT * FROM recalls WHERE clinic_id=? AND lot_id=?",
                                        (clinic_id, lot_id)).fetchone()
            if recall is not None:
                duplicate = connection.execute(
                    "SELECT * FROM recall_notices WHERE recall_id=? AND notice_ref=? AND revision=?",
                    (recall["id"], notice_ref, revision)).fetchone()
                if duplicate is not None:
                    if duplicate["request_hash"] != request_hash:
                        raise Conflict("相同通知编号与版本已用于其他内容")
                    return {"recall_id": recall["id"], "lot_id": lot_id, "state": recall["state"],
                            "notice": self._notice_result(duplicate), "cases_created": 0, "replayed": True}
                if recall["state"] != "open":
                    raise Conflict("召回已关闭，不能导入新的供应商修订")
                latest = connection.execute("SELECT MAX(revision) FROM recall_notices WHERE recall_id=?",
                                            (recall["id"],)).fetchone()[0]
                if revision <= latest:
                    raise Conflict("供应商修订版本必须高于已记录版本", details={"latest_revision": latest})
                previous = connection.execute("SELECT id FROM recall_notices WHERE recall_id=? ORDER BY revision DESC LIMIT 1",
                                              (recall["id"],)).fetchone()
                notice_id = new_id("rcn")
                connection.execute(
                    "INSERT INTO recall_notices(id,recall_id,notice_ref,revision,urgency,summary,request_hash,recorded_by,received_at,supersedes,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (notice_id, recall["id"], notice_ref, revision, urgency, summary, request_hash,
                     actor_id, received, previous["id"], now))
                # 供应商修订只更新召回活动本身，不重置任何病例的处置进度。
                connection.execute("UPDATE recalls SET summary=?,urgency=?,version=version+1 WHERE id=?",
                                   (summary, urgency, recall["id"]))
                audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                                   aggregate_type="recall", aggregate_id=recall["id"], action="recall.notice_revised",
                                   occurred_at=now, payload={"lot_id": lot_id, "notice_ref": notice_ref,
                                                             "revision": revision, "urgency": urgency})
                notice = connection.execute("SELECT * FROM recall_notices WHERE id=?", (notice_id,)).fetchone()
                return {"recall_id": recall["id"], "lot_id": lot_id, "state": "open",
                        "notice": self._notice_result(notice), "cases_created": 0, "replayed": False}
            recall_id = new_id("rcl")
            connection.execute(
                "INSERT INTO recalls(id,clinic_id,lot_id,state,urgency,summary,opened_by,opened_at,created_at) "
                "VALUES(?,?,?,'open',?,?,?,?,?)",
                (recall_id, clinic_id, lot_id, urgency, summary, actor_id, now, now))
            notice_id = new_id("rcn")
            connection.execute(
                "INSERT INTO recall_notices(id,recall_id,notice_ref,revision,urgency,summary,request_hash,recorded_by,received_at,supersedes,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,NULL,?)",
                (notice_id, recall_id, notice_ref, revision, urgency, summary, request_hash, actor_id, received, now))
            cases_created = self._snapshot_cases(connection, recall_id=recall_id, clinic_id=clinic_id,
                                                 lot_id=lot_id, urgency=urgency, actor_id=actor_id, now=now)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="recall", aggregate_id=recall_id, action="recall.opened",
                               occurred_at=now, payload={"lot_id": lot_id, "lot_number": lot["lot_number"],
                                                         "notice_ref": notice_ref, "revision": revision,
                                                         "urgency": urgency, "cases_created": cases_created})
            notice = connection.execute("SELECT * FROM recall_notices WHERE id=?", (notice_id,)).fetchone()
            return {"recall_id": recall_id, "lot_id": lot_id, "state": "open",
                    "notice": self._notice_result(notice), "cases_created": cases_created, "replayed": False}

    def _snapshot_cases(self, connection, *, recall_id: str, clinic_id: str, lot_id: str,
                        urgency: str, actor_id: str, now: str) -> int:
        rows = connection.execute(
            "SELECT id,appointment_id,patient_id,quantity,state FROM stock_reservations "
            "WHERE lot_id=? AND state IN ('reserved','consumed') ORDER BY created_at,id", (lot_id,)).fetchall()
        by_patient: dict[str, list] = {}
        for row in rows:
            by_patient.setdefault(row["patient_id"], []).append(row)
        for patient_id, items in by_patient.items():
            case_id = new_id("rcs")
            connection.execute(
                "INSERT INTO recall_cases(id,recall_id,clinic_id,patient_id,stage,urgency,assigned_to,review_required,created_at,updated_at) "
                "VALUES(?,?,?,?,'pending',?,?,0,?,?)",
                (case_id, recall_id, clinic_id, patient_id, urgency, actor_id, now, now))
            for item in items:
                connection.execute(
                    "INSERT INTO recall_case_items(id,case_id,reservation_id,appointment_id,quantity,created_at) VALUES(?,?,?,?,?,?)",
                    (new_id("rci"), case_id, item["id"], item["appointment_id"], item["quantity"], now))
            consumed = sum(1 for item in items if item["state"] == "consumed")
            _case_event(connection, case_id, "snapshot", actor_id,
                        f"召回快照登记 {len(items)} 笔记录，其中已核销 {consumed} 笔", now,
                        to_stage="pending", to_urgency=urgency)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                               aggregate_type="recall_case", aggregate_id=case_id, action="recall_case.snapshotted",
                               occurred_at=now, payload={"recall_id": recall_id,
                                                         "reservations": [item["id"] for item in items],
                                                         "consumed": consumed})
        return len(by_patient)

    def update_case(self, clinic_id: str, actor_id: str, case_id: str, action: str,
                    expected_version: int, *, note: str | None = None, assign_to: str | None = None,
                    contact_result: str | None = None, next_review_on: str | None = None,
                    to_stage: str | None = None, to_urgency: str | None = None) -> dict[str, Any]:
        action = choice(action, "病例处置操作", {"assign", "contact", "advance", "escalate", "review"})
        if note is not None:
            note = text(note, "处置说明", maximum=1000)
        required_notes = {"advance": "推进处置阶段必须填写说明",
                          "escalate": "升级紧急程度必须填写升级依据",
                          "review": "人工复核必须填写复核结论"}
        if action in required_notes and not note:
            raise ValidationError(required_notes[action])
        now = self.now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "followup:manage" if action == "contact" else "incident:manage", clinic_id=clinic_id)
            case = connection.execute("SELECT * FROM recall_cases WHERE id=? AND clinic_id=?",
                                      (case_id, clinic_id)).fetchone()
            if case is None:
                raise NotFound("召回病例不存在")
            require_match(case["version"], expected_version, "召回病例")
            recall_state = connection.execute("SELECT state FROM recalls WHERE id=?", (case["recall_id"],)).fetchone()["state"]
            if recall_state != "open":
                raise Conflict("召回已关闭，病例不能继续处置")
            review_on = calendar_date(next_review_on, "后续复核日期") if next_review_on else case["next_review_on"]
            if action == "assign":
                target = connection.execute("SELECT active,role FROM staff WHERE id=? AND clinic_id=?",
                                            (assign_to or "", clinic_id)).fetchone()
                if target is None or not target["active"] or target["role"] not in ASSIGNABLE_ROLES:
                    raise ValidationError("责任人必须是本诊所有效的临床或排班岗位")
                if case["stage"] == "closed":
                    raise Conflict("已关闭病例不能调整责任人")
                connection.execute("UPDATE recall_cases SET assigned_to=?,updated_at=?,version=version+1 WHERE id=?",
                                   (assign_to, now, case_id))
                _case_event(connection, case_id, "assigned", actor_id, note or "调整责任人", now)
                audit_action, payload = "recall_case.assigned", {"assigned_to": assign_to}
            elif action == "contact":
                if case["stage"] not in {"pending", "contacted", "observing"}:
                    raise Conflict("当前阶段不能登记联系结果", details={"stage": case["stage"]})
                result = choice(contact_result, "联系结果", CONTACT_RESULTS)
                if result == "callback_requested" and not review_on:
                    raise ValidationError("患者要求改期联系时必须设置后续复核日期")
                new_stage = "contacted" if case["stage"] == "pending" else case["stage"]
                connection.execute(
                    "UPDATE recall_cases SET stage=?,contact_result=?,next_review_on=?,updated_at=?,version=version+1 WHERE id=?",
                    (new_stage, result, review_on, now, case_id))
                _case_event(connection, case_id, "contacted", actor_id, note or f"登记联系结果：{result}", now,
                            from_stage=case["stage"], to_stage=new_stage)
                audit_action, payload = "recall_case.contacted", {"contact_result": result, "next_review_on": review_on}
            elif action == "advance":
                target_stage = choice(to_stage, "处置阶段", CASE_STAGES)
                if target_stage not in ADVANCE_TRANSITIONS.get(case["stage"], set()):
                    raise Conflict("病例当前阶段不允许此推进",
                                   details={"stage": case["stage"], "to_stage": target_stage})
                if case["review_required"] and target_stage in {"resolved", "closed"}:
                    raise Conflict("病例仍需人工复核，不能标记处置完成")
                if target_stage == "observing" and not review_on:
                    raise ValidationError("进入观察随访必须设置后续复核日期")
                connection.execute("UPDATE recall_cases SET stage=?,next_review_on=?,updated_at=?,version=version+1 WHERE id=?",
                                   (target_stage, review_on, now, case_id))
                _case_event(connection, case_id, "stage_advanced", actor_id, note, now,
                            from_stage=case["stage"], to_stage=target_stage)
                audit_action, payload = "recall_case.stage_advanced", {"from": case["stage"], "to": target_stage, "note": note}
            elif action == "escalate":
                if case["stage"] in {"resolved", "closed"}:
                    raise Conflict("已结束病例不能升级紧急程度")
                target_urgency = choice(to_urgency, "紧急程度", URGENCY_LEVELS)
                if URGENCY_RANK[target_urgency] <= URGENCY_RANK[case["urgency"]]:
                    raise Conflict("紧急程度只能升级到更高级别", details={"current": case["urgency"]})
                connection.execute("UPDATE recall_cases SET urgency=?,updated_at=?,version=version+1 WHERE id=?",
                                   (target_urgency, now, case_id))
                _case_event(connection, case_id, "escalated", actor_id, note, now,
                            from_urgency=case["urgency"], to_urgency=target_urgency)
                audit_action, payload = "recall_case.escalated", {"from": case["urgency"], "to": target_urgency, "basis": note}
            else:  # review
                if not case["review_required"]:
                    raise Conflict("该病例无需人工复核")
                connection.execute("UPDATE recall_cases SET review_required=0,updated_at=?,version=version+1 WHERE id=?",
                                   (now, case_id))
                _case_event(connection, case_id, "review_cleared", actor_id, note, now)
                audit_action, payload = "recall_case.review_cleared", {"note": note}
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=case["patient_id"],
                               aggregate_type="recall_case", aggregate_id=case_id, action=audit_action,
                               occurred_at=now, payload={**payload, "recall_id": case["recall_id"],
                                                         "version": expected_version + 1})
            updated = connection.execute("SELECT * FROM recall_cases WHERE id=?", (case_id,)).fetchone()
        return self._case_result(updated)

    def close_recall(self, clinic_id: str, actor_id: str, recall_id: str,
                     expected_version: int, *, note: str) -> dict[str, Any]:
        note = text(note, "关闭说明", maximum=1000)
        now = self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "incident:manage", clinic_id=clinic_id)
            recall = connection.execute("SELECT * FROM recalls WHERE id=? AND clinic_id=?",
                                        (recall_id, clinic_id)).fetchone()
            if recall is None:
                raise NotFound("召回活动不存在")
            require_match(recall["version"], expected_version, "召回活动")
            if recall["state"] != "open":
                raise Conflict("召回活动已关闭")
            remaining = connection.execute("SELECT COUNT(*) FROM recall_cases WHERE recall_id=? AND stage!='closed'",
                                           (recall_id,)).fetchone()[0]
            if remaining:
                raise Conflict("尚有未关闭的召回病例", details={"open_cases": remaining})
            flagged = connection.execute("SELECT COUNT(*) FROM recall_cases WHERE recall_id=? AND review_required=1",
                                         (recall_id,)).fetchone()[0]
            if flagged:
                raise Conflict("尚有病例等待人工复核", details={"review_required": flagged})
            connection.execute("UPDATE recalls SET state='closed',closed_at=?,version=version+1 WHERE id=?",
                               (now, recall_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="recall", aggregate_id=recall_id, action="recall.closed",
                               occurred_at=now, payload={"note": note})
        return {"id": recall_id, "state": "closed", "closed_at": now, "version": expected_version + 1}

    def list_recalls(self, clinic_id: str, actor_id: str) -> list[dict[str, Any]]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "incident:manage", clinic_id=clinic_id)
            rows = connection.execute(
                "SELECT r.id,r.state,r.urgency,r.opened_at,r.closed_at,r.lot_id,l.lot_number,p.name AS product_name,"
                "(SELECT COUNT(*) FROM recall_cases c WHERE c.recall_id=r.id) AS cases,"
                "(SELECT COUNT(*) FROM recall_cases c WHERE c.recall_id=r.id AND c.stage NOT IN ('resolved','closed')) AS pending "
                "FROM recalls r JOIN product_lots l ON l.id=r.lot_id JOIN products p ON p.id=l.product_id "
                "WHERE r.clinic_id=? ORDER BY r.opened_at DESC,r.id", (clinic_id,)).fetchall()
            return [dict(row) for row in rows]

    def overview(self, clinic_id: str, actor_id: str, recall_id: str) -> dict[str, Any]:
        """召回管理视图：批次总量、未处置人数与每例最后状态。"""
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "incident:manage", clinic_id=clinic_id)
            recall = connection.execute(
                "SELECT r.*,l.lot_number,l.supplier_ref,l.expires_on,l.state AS lot_state,l.product_id,"
                "p.name AS product_name,p.stock_unit "
                "FROM recalls r JOIN product_lots l ON l.id=r.lot_id JOIN products p ON p.id=l.product_id "
                "WHERE r.id=? AND r.clinic_id=?", (recall_id, clinic_id)).fetchone()
            if recall is None:
                raise NotFound("召回活动不存在")
            quantity_received = connection.execute(
                "SELECT COALESCE(SUM(quantity_delta),0) FROM stock_movements WHERE lot_id=? AND event_type='received'",
                (recall["lot_id"],)).fetchone()[0]
            notices = connection.execute("SELECT * FROM recall_notices WHERE recall_id=? ORDER BY revision",
                                         (recall_id,)).fetchall()
            cases = connection.execute(
                "SELECT c.*,p.display_name,p.external_ref FROM recall_cases c JOIN patients p ON p.id=c.patient_id "
                "WHERE c.recall_id=? ORDER BY c.created_at,c.id", (recall_id,)).fetchall()
            case_views = []
            by_stage: dict[str, int] = {}
            pending = flagged = 0
            for case in cases:
                by_stage[case["stage"]] = by_stage.get(case["stage"], 0) + 1
                if case["stage"] not in {"resolved", "closed"}:
                    pending += 1
                if case["review_required"]:
                    flagged += 1
                items = connection.execute(
                    "SELECT i.reservation_id,i.appointment_id,i.quantity,r.state AS reservation_state "
                    "FROM recall_case_items i JOIN stock_reservations r ON r.id=i.reservation_id "
                    "WHERE i.case_id=? ORDER BY i.created_at,i.id", (case["id"],)).fetchall()
                last = connection.execute(
                    "SELECT event_type,note,actor_id,occurred_at FROM recall_case_events WHERE case_id=? "
                    "ORDER BY sequence DESC LIMIT 1", (case["id"],)).fetchone()
                case_views.append({
                    "id": case["id"], "patient_id": case["patient_id"], "patient_name": case["display_name"],
                    "patient_ref": case["external_ref"], "stage": case["stage"], "urgency": case["urgency"],
                    "assigned_to": case["assigned_to"], "contact_result": case["contact_result"],
                    "next_review_on": case["next_review_on"], "review_required": bool(case["review_required"]),
                    "version": case["version"], "updated_at": case["updated_at"],
                    "items": [{"reservation_id": item["reservation_id"], "appointment_id": item["appointment_id"],
                               "quantity": item["quantity"], "reservation_state": item["reservation_state"]}
                              for item in items],
                    "last_event": {"event_type": last["event_type"], "note": last["note"],
                                   "actor_id": last["actor_id"], "occurred_at": last["occurred_at"]} if last else None})
            return {
                "recall": {"id": recall["id"], "lot_id": recall["lot_id"], "lot_number": recall["lot_number"],
                           "product_id": recall["product_id"], "product_name": recall["product_name"],
                           "state": recall["state"], "urgency": recall["urgency"], "summary": recall["summary"],
                           "opened_by": recall["opened_by"], "opened_at": recall["opened_at"],
                           "closed_at": recall["closed_at"], "version": recall["version"]},
                "lot": {"state": recall["lot_state"], "expires_on": recall["expires_on"],
                        "supplier_ref": recall["supplier_ref"], "quantity_received": quantity_received,
                        "unit": recall["stock_unit"]},
                "totals": {"cases": len(cases), "patients_pending": pending,
                           "review_required": flagged, "by_stage": by_stage},
                "notices": [self._notice_result(row) | {"recorded_by": row["recorded_by"]} for row in notices],
                "cases": case_views}

    def worklist(self, clinic_id: str, actor_id: str, recall_id: str) -> dict[str, Any]:
        """排班联络视图：只返回完成联络所需的信息，不含批次数量、供应商编号与复核标记。"""
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "followup:manage", clinic_id=clinic_id)
            recall = connection.execute(
                "SELECT r.id,r.state,r.summary,l.lot_number,p.name AS product_name "
                "FROM recalls r JOIN product_lots l ON l.id=r.lot_id JOIN products p ON p.id=l.product_id "
                "WHERE r.id=? AND r.clinic_id=?", (recall_id, clinic_id)).fetchone()
            if recall is None:
                raise NotFound("召回活动不存在")
            cases = connection.execute(
                "SELECT c.*,p.display_name,p.external_ref,p.phone_ciphertext FROM recall_cases c "
                "JOIN patients p ON p.id=c.patient_id WHERE c.recall_id=? AND c.stage IN ('pending','contacted','observing') "
                "ORDER BY CASE c.urgency WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,"
                "CASE WHEN c.next_review_on IS NULL THEN 1 ELSE 0 END,c.next_review_on,c.created_at,c.id",
                (recall_id,)).fetchall()
            items = []
            for case in cases:
                appointments = connection.execute(
                    "SELECT a.id,a.starts_at,a.kind FROM recall_case_items i JOIN appointments a ON a.id=i.appointment_id "
                    "WHERE i.case_id=? ORDER BY a.starts_at", (case["id"],)).fetchall()
                items.append({
                    "case_id": case["id"], "patient_id": case["patient_id"], "patient_name": case["display_name"],
                    "patient_ref": case["external_ref"], "phone_ciphertext": case["phone_ciphertext"],
                    "stage": case["stage"], "urgency": case["urgency"], "contact_result": case["contact_result"],
                    "next_review_on": case["next_review_on"], "assigned_to": case["assigned_to"],
                    "product_name": recall["product_name"], "lot_number": recall["lot_number"],
                    "summary": recall["summary"], "version": case["version"],
                    "appointments": [{"id": row["id"], "starts_at": row["starts_at"], "kind": row["kind"]}
                                     for row in appointments]})
            return {"recall_id": recall_id, "state": recall["state"], "items": items}

    def case_history(self, clinic_id: str, actor_id: str, case_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "incident:manage", clinic_id=clinic_id)
            case = connection.execute("SELECT * FROM recall_cases WHERE id=? AND clinic_id=?",
                                      (case_id, clinic_id)).fetchone()
            if case is None:
                raise NotFound("召回病例不存在")
            events = connection.execute("SELECT * FROM recall_case_events WHERE case_id=? ORDER BY sequence",
                                        (case_id,)).fetchall()
            return {"case": self._case_result(case),
                    "events": [{"sequence": row["sequence"], "event_type": row["event_type"],
                                "actor_id": row["actor_id"], "note": row["note"],
                                "from_stage": row["from_stage"], "to_stage": row["to_stage"],
                                "from_urgency": row["from_urgency"], "to_urgency": row["to_urgency"],
                                "occurred_at": row["occurred_at"]} for row in events]}
