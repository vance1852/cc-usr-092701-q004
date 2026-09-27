"""批次召回通知、受影响病例快照与召回处置流程。

召回范围严格按批号（lot_id）快照：同产品其他批号的预约与核销不会进入清单。
通知允许重复导入与供应商修订；修订只追加信息，不抹掉已完成的病例处置。
"""

from __future__ import annotations

from typing import Any

from . import audit
from .db import Database, decode_json, encode_json
from .errors import Conflict, Forbidden, NotFound, ValidationError
from .ids import new_id, require_idempotency_key
from .security import ROLE_PERMISSIONS, authorize, principal_for
from .validation import calendar_date, choice, request_digest, text, timestamp

URGENCY_LEVELS = ("low", "moderate", "high", "urgent")
URGENCY_RANK = {value: index for index, value in enumerate(URGENCY_LEVELS)}
CONTACT_STATES = {"reached", "unreachable", "declined"}


class RecallService:
    """召回通知与病例处置；所有写操作在单个事务内完成并留审计。"""

    def __init__(self, database: Database, clock):
        self.db = database
        self.clock = clock

    def now(self) -> str:
        return timestamp(self.clock.now())

    @staticmethod
    def _authorize_contact(principal) -> None:
        """联络工作：排班/临床岗位（followup:manage）或召回负责人（incident:manage）。"""
        perms = ROLE_PERMISSIONS.get(principal.role, set())
        if "followup:manage" not in perms and "incident:manage" not in perms:
            raise Forbidden("当前岗位无权处理召回联络")

    # ------------------------------------------------------------------ 开放召回

    def open_recall(self, clinic_id: str, actor_id: str, lot_id: str, supplier_ref: str,
                    notice_ref: str, notice_at: str, urgency: str, summary: str,
                    idempotency_key: str, *, guidance: str | None = None) -> dict[str, Any]:
        supplier_ref = text(supplier_ref, "供应商编号", maximum=120)
        notice_ref = text(notice_ref, "供应商通知编号", maximum=120)
        notice_at = timestamp(notice_at, "供应商通知时间")
        urgency = choice(urgency, "紧急程度", set(URGENCY_LEVELS))
        summary = text(summary, "召回通知摘要", maximum=2000)
        guidance = text(guidance, "供应商处置指引", maximum=2000) if guidance else None
        key = require_idempotency_key(idempotency_key)
        request_hash = request_digest({"lot_id": lot_id, "supplier_ref": supplier_ref, "notice_ref": notice_ref,
                                       "notice_at": notice_at, "urgency": urgency, "summary": summary, "guidance": guidance})
        now = self.now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "incident:manage", clinic_id=clinic_id)
            old = connection.execute("SELECT response_json,request_hash FROM idempotency WHERE scope='recall_open' AND key=?",
                                     (key,)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise Conflict("召回通知幂等编号已用于其他内容")
                return {**decode_json(old["response_json"]), "replayed": True}
            lot = connection.execute(
                "SELECT l.*,p.clinic_id AS clinic_id,p.name AS product_name,p.category,p.stock_unit "
                "FROM product_lots l JOIN products p ON p.id=l.product_id WHERE l.id=?", (lot_id,)).fetchone()
            if lot is None or lot["clinic_id"] != clinic_id:
                raise NotFound("耗材批次不存在")
            duplicate = connection.execute("SELECT id,lot_id,notice_ref,urgency,state,version,request_hash FROM lot_recalls WHERE lot_id=? AND notice_ref=?",
                                           (lot_id, notice_ref)).fetchone()
            if duplicate:
                # 同一供应商通知重复导入：内容一致即回放，不一致要求走修订编号。
                if duplicate["request_hash"] != request_hash:
                    raise Conflict("该供应商通知编号已存在且内容不同；供应商更新请使用修订接口")
                persist = {k: duplicate[k] for k in ("id", "lot_id", "notice_ref", "urgency", "state", "version")}
                connection.execute("INSERT INTO idempotency(scope,key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                                   ("recall_open", key, request_hash, encode_json(persist), now))
                return {**persist, "replayed": True}

            recall_id = new_id("rcl")
            previous_state = lot["state"]
            connection.execute(
                "INSERT INTO lot_recalls(id,clinic_id,lot_id,notice_ref,supplier_ref,notice_at,urgency,state,summary,guidance,request_hash,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?, 'open',?,?,?,?,?,?)",
                (recall_id, clinic_id, lot_id, notice_ref, supplier_ref, notice_at, urgency, summary, guidance,
                 request_hash, actor_id, now, now))
            if previous_state != "recalled":
                connection.execute("UPDATE product_lots SET state='recalled',version=version+1 WHERE id=?", (lot_id,))
                alert_id = new_id("alt")
                previous_alert = connection.execute("SELECT id FROM lot_alerts WHERE lot_id=? ORDER BY created_at DESC LIMIT 1",
                                                    (lot_id,)).fetchone()
                connection.execute(
                    "INSERT INTO lot_alerts(id,lot_id,alert_type,reason,actor_id,created_at,supersedes) VALUES(?,?, 'recall',?,?,?,?)",
                    (alert_id, lot_id, f"供应商召回通知 {notice_ref}：{summary}", actor_id, now,
                     previous_alert["id"] if previous_alert else None))
                self._lot_movement(connection, lot_id, f"recall:{recall_id}", f"供应商召回通知 {notice_ref}", actor_id, now)

            case_rows = connection.execute(
                "SELECT * FROM stock_reservations WHERE lot_id=? AND state IN ('reserved','consumed') ORDER BY created_at,id",
                (lot_id,)).fetchall()
            cases = []
            for reservation in case_rows:
                case_id = self._insert_case(connection, recall_id=recall_id, clinic_id=clinic_id, lot=lot,
                                            reservation=reservation, exposure_state=reservation["state"],
                                            manual_review=False, created_by=actor_id, now=now)
                cases.append(case_id)
                self._event(connection, recall_id, "case.added", actor_id,
                            "导入召回通知时纳入病例", {"case_id": case_id, "exposure_state": reservation["state"]}, now)

            self._event(connection, recall_id, "recall.opened", actor_id, summary,
                        {"notice_ref": notice_ref, "supplier_ref": supplier_ref, "notice_at": notice_at,
                         "urgency": urgency, "previous_lot_state": previous_state, "cases": cases,
                         "guidance": guidance}, now)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="lot_recall", aggregate_id=recall_id, action="recall.opened",
                               occurred_at=now, payload={"lot_id": lot_id, "notice_ref": notice_ref,
                                                         "urgency": urgency, "case_count": len(cases)})
            result = {"id": recall_id, "lot_id": lot_id, "notice_ref": notice_ref,
                      "urgency": urgency, "state": "open", "version": 1, "case_count": len(cases)}
            connection.execute("INSERT INTO idempotency(scope,key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                               ("recall_open", key, request_hash, encode_json(result), now))
        return {**result, "replayed": False}

    # ------------------------------------------------------------------ 供应商修订

    def revise_recall(self, clinic_id: str, actor_id: str, recall_id: str, revision_ref: str,
                      idempotency_key: str, note: str, *, summary: str | None = None,
                      guidance: str | None = None, urgency: str | None = None) -> dict[str, Any]:
        revision_ref = text(revision_ref, "供应商修订编号", maximum=120)
        note = text(note, "修订说明", maximum=2000)
        key = require_idempotency_key(idempotency_key)
        summary = text(summary, "召回通知摘要", maximum=2000) if summary else None
        guidance = text(guidance, "供应商处置指引", maximum=2000) if guidance else None
        if urgency is not None:
            urgency = choice(urgency, "紧急程度", set(URGENCY_LEVELS))
        if summary is None and guidance is None and urgency is None:
            raise ValidationError("修订至少需要包含新摘要、新指引或调整后的紧急程度")
        request_hash = request_digest({"recall_id": recall_id, "revision_ref": revision_ref, "note": note,
                                       "summary": summary, "guidance": guidance, "urgency": urgency})
        now = self.now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "incident:manage", clinic_id=clinic_id)
            old = connection.execute("SELECT response_json,request_hash FROM idempotency WHERE scope='recall_revision' AND key=?",
                                     (key,)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise Conflict("召回修订幂等编号已用于其他内容")
                return {**decode_json(old["response_json"]), "replayed": True}
            recall = connection.execute("SELECT * FROM lot_recalls WHERE id=? AND clinic_id=?", (recall_id, clinic_id)).fetchone()
            if recall is None:
                raise NotFound("召回通知不存在")
            previous = connection.execute("SELECT request_hash FROM lot_recall_revisions WHERE recall_id=? AND revision_ref=?",
                                          (recall_id, revision_ref)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise Conflict("该修订编号已用于其他内容")
                result = {"id": recall_id, "revision_ref": revision_ref, "replayed_duplicate": True,
                          "version": recall["version"]}
                connection.execute("INSERT INTO idempotency(scope,key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                                   ("recall_revision", key, request_hash, encode_json(result), now))
                return {**result, "replayed": True}
            if urgency is not None and URGENCY_RANK[urgency] <= URGENCY_RANK[recall["urgency"]]:
                raise Conflict("供应商修订中的紧急程度只能升级；降级或保持请省略该字段")
            new_summary, new_guidance = summary or recall["summary"], guidance if guidance is not None else recall["guidance"]
            new_urgency = urgency or recall["urgency"]
            revision_id = new_id("rcr")
            connection.execute(
                "INSERT INTO lot_recall_revisions(id,recall_id,revision_ref,request_hash,summary,guidance,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (revision_id, recall_id, revision_ref, request_hash, summary, guidance, actor_id, now))
            connection.execute(
                "UPDATE lot_recalls SET summary=?,guidance=?,urgency=?,updated_at=?,version=version+1 WHERE id=?",
                (new_summary, new_guidance, new_urgency, now, recall_id))
            # 修订只追加信息：绝不改写任何已完成或进行中的病例处置。
            self._event(connection, recall_id, "recall.revised", actor_id, note,
                        {"revision_ref": revision_ref, "summary_changed": summary is not None,
                         "guidance_changed": guidance is not None, "urgency": new_urgency}, now)
            if urgency is not None:
                self._event(connection, recall_id, "recall.urgency_escalated", actor_id,
                            f"供应商修订 {revision_ref}：{note}",
                            {"from": recall["urgency"], "to": new_urgency, "basis_source": "supplier_revision",
                             "revision_ref": revision_ref}, now)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="lot_recall", aggregate_id=recall_id, action="recall.revised",
                               occurred_at=now, payload={"revision_ref": revision_ref, "urgency": new_urgency})
            result = {"id": recall_id, "revision_ref": revision_ref, "urgency": new_urgency,
                      "state": recall["state"], "version": recall["version"] + 1}
            connection.execute("INSERT INTO idempotency(scope,key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                               ("recall_revision", key, request_hash, encode_json(result), now))
        return {**result, "replayed": False}

    # ------------------------------------------------------------------ 紧急程度升级

    def escalate_urgency(self, clinic_id: str, actor_id: str, recall_id: str, urgency: str,
                         basis: str, expected_version: int) -> dict[str, Any]:
        urgency = choice(urgency, "紧急程度", set(URGENCY_LEVELS))
        basis = text(basis, "升级依据", minimum=5, maximum=2000)
        now = self.now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "incident:manage", clinic_id=clinic_id)
            recall = connection.execute("SELECT * FROM lot_recalls WHERE id=? AND clinic_id=?", (recall_id, clinic_id)).fetchone()
            if recall is None:
                raise NotFound("召回通知不存在")
            if recall["version"] != expected_version:
                raise Conflict("召回通知已被更新", details={"expected_version": expected_version,
                                                           "actual_version": recall["version"]})
            if URGENCY_RANK[urgency] <= URGENCY_RANK[recall["urgency"]]:
                raise Conflict("紧急程度只能向更高等级升级")
            connection.execute("UPDATE lot_recalls SET urgency=?,updated_at=?,version=version+1 WHERE id=?",
                               (urgency, now, recall_id))
            self._event(connection, recall_id, "recall.urgency_escalated", actor_id, basis,
                        {"from": recall["urgency"], "to": urgency, "basis_source": "manual"}, now)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="lot_recall", aggregate_id=recall_id, action="recall.urgency_escalated",
                               occurred_at=now, payload={"from": recall["urgency"], "to": urgency})
        return {"id": recall_id, "urgency": urgency, "version": expected_version + 1, "updated_at": now}

    # ------------------------------------------------------------------ 病例处置

    def assign_case(self, clinic_id: str, actor_id: str, case_id: str, owner_id: str) -> dict[str, Any]:
        now = self.now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "incident:manage", clinic_id=clinic_id)
            case, recall = self._load_case(connection, clinic_id, case_id)
            target = connection.execute("SELECT id,active FROM staff WHERE id=? AND clinic_id=?", (owner_id, clinic_id)).fetchone()
            if target is None or not target["active"]:
                raise ValidationError("责任人必须是本诊所有效员工")
            connection.execute("UPDATE lot_recall_cases SET owner_id=?,updated_at=?,version=version+1 WHERE id=?",
                               (owner_id, now, case_id))
            self._event(connection, recall["id"], "case.assigned", actor_id,
                        f"责任人由 {case['owner_id'] or '未分配'} 变更为 {owner_id}",
                        {"case_id": case_id, "from": case["owner_id"], "to": owner_id}, now)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=case["patient_id"],
                               aggregate_type="lot_recall_case", aggregate_id=case_id, action="recall_case.assigned",
                               occurred_at=now, payload={"owner_id": owner_id})
        return {"id": case_id, "owner_id": owner_id, "version": case["version"] + 1}

    def record_contact(self, clinic_id: str, actor_id: str, case_id: str, contact_state: str,
                       expected_version: int, *, result: str | None = None,
                       next_review_on: str | None = None) -> dict[str, Any]:
        contact_state = choice(contact_state, "联系结果", CONTACT_STATES)
        result = text(result, "联系情况记录", maximum=2000) if result else None
        review_date = calendar_date(next_review_on, "后续复核日期") if next_review_on else None
        now = self.now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            self._authorize_contact(principal)
            case, recall = self._load_case(connection, clinic_id, case_id)
            if case["version"] != expected_version:
                raise Conflict("病例已被其他操作更新", details={"expected_version": expected_version,
                                                              "actual_version": case["version"]})
            if case["stage"] == "completed":
                raise Conflict("已完成处置的病例不能再登记联系结果")
            if contact_state == "reached" and not result:
                raise ValidationError("联系成功时需要记录沟通情况")
            stage = "contacted" if contact_state == "reached" else "identified"
            connection.execute(
                "UPDATE lot_recall_cases SET contact_state=?,contact_result=?,last_contact_at=?,"
                "next_review_on=COALESCE(?,next_review_on),stage=?,updated_at=?,version=version+1 WHERE id=?",
                (contact_state, result, now, review_date, stage, now, case_id))
            self._event(connection, recall["id"], "case.contact_recorded", actor_id, result or contact_state,
                        {"case_id": case_id, "contact_state": contact_state, "next_review_on": review_date,
                         "stage": stage}, now)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=case["patient_id"],
                               aggregate_type="lot_recall_case", aggregate_id=case_id, action="recall_case.contacted",
                               occurred_at=now, payload={"contact_state": contact_state})
        return {"id": case_id, "contact_state": contact_state, "stage": stage,
                "next_review_on": review_date or case["next_review_on"], "version": expected_version + 1}

    def resolve_manual_review(self, clinic_id: str, actor_id: str, case_id: str, note: str,
                              expected_version: int, *, next_review_on: str | None = None) -> dict[str, Any]:
        note = text(note, "人工复核结论", minimum=5, maximum=2000)
        review_date = calendar_date(next_review_on, "后续复核日期") if next_review_on else None
        now = self.now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "incident:manage", clinic_id=clinic_id)
            if principal.role not in {"clinician", "owner"}:
                raise Forbidden("召回核销的人工复核只能由临床岗位或负责人完成")
            case, recall = self._load_case(connection, clinic_id, case_id)
            if case["version"] != expected_version:
                raise Conflict("病例已被其他操作更新", details={"expected_version": expected_version,
                                                              "actual_version": case["version"]})
            if not case["requires_manual_review"]:
                raise Conflict("该病例没有待处理的人工复核项")
            connection.execute(
                "UPDATE lot_recall_cases SET requires_manual_review=0,"
                "next_review_on=COALESCE(?,next_review_on),updated_at=?,version=version+1 WHERE id=?",
                (review_date, now, case_id))
            self._event(connection, recall["id"], "case.manual_review_resolved", actor_id, note,
                        {"case_id": case_id, "next_review_on": review_date}, now)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=case["patient_id"],
                               aggregate_type="lot_recall_case", aggregate_id=case_id,
                               action="recall_case.manual_review_resolved", occurred_at=now,
                               payload={"note": note})
        return {"id": case_id, "requires_manual_review": False,
                "next_review_on": review_date or case["next_review_on"], "version": expected_version + 1}

    def complete_case(self, clinic_id: str, actor_id: str, case_id: str, note: str,
                      expected_version: int, *, no_contact_required: bool = False,
                      next_review_on: str | None = None) -> dict[str, Any]:
        note = text(note, "完成处置说明", minimum=5, maximum=2000)
        if not isinstance(no_contact_required, bool):
            raise ValidationError("免联络标记必须为布尔值")
        review_date = calendar_date(next_review_on, "后续复核日期") if next_review_on else None
        now = self.now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "incident:manage", clinic_id=clinic_id)
            case, recall = self._load_case(connection, clinic_id, case_id)
            if case["version"] != expected_version:
                raise Conflict("病例已被其他操作更新", details={"expected_version": expected_version,
                                                              "actual_version": case["version"]})
            if case["stage"] == "completed":
                raise Conflict("病例已完成处置")
            if case["requires_manual_review"]:
                raise Conflict("召回期间核销的病例必须先完成人工复核")
            if review_date:
                connection.execute("UPDATE lot_recall_cases SET next_review_on=? WHERE id=?", (review_date, case_id))
            effective_review_on = review_date or case["next_review_on"]
            if not effective_review_on:
                raise Conflict("完成处置前必须安排后续复核日期")
            contact_state = case["contact_state"]
            if no_contact_required:
                if case["exposure_state"] != "reserved":
                    raise Conflict("已实际使用该批号的患者必须完成联络，不能标记免联络")
                contact_state = "no_contact_required"
                connection.execute(
                    "UPDATE lot_recall_cases SET contact_state='no_contact_required' WHERE id=? AND contact_state='pending'",
                    (case_id,))
            if contact_state not in {"reached", "no_contact_required"}:
                raise Conflict("患者尚未成功联络，不能完成处置；可继续联络并记录结果",
                               details={"contact_state": contact_state})
            connection.execute("UPDATE lot_recall_cases SET stage='completed',updated_at=?,version=version+1 WHERE id=?",
                               (now, case_id))
            self._event(connection, recall["id"], "case.completed", actor_id, note,
                        {"case_id": case_id, "contact_state": contact_state,
                         "next_review_on": effective_review_on}, now)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=case["patient_id"],
                               aggregate_type="lot_recall_case", aggregate_id=case_id, action="recall_case.completed",
                               occurred_at=now, payload={"note": note, "contact_state": contact_state})
        return {"id": case_id, "stage": "completed", "contact_state": contact_state,
                "next_review_on": effective_review_on, "version": expected_version + 1, "completed_at": now}

    # ------------------------------------------------------------------ 查询视图

    def recall_detail(self, clinic_id: str, actor_id: str, recall_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "incident:manage", clinic_id=clinic_id)
            return self._recall_detail(connection, recall_id, clinic_id=clinic_id)

    def list_for_lot(self, clinic_id: str, actor_id: str, lot_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "incident:manage", clinic_id=clinic_id)
            lot = connection.execute(
                "SELECT l.id FROM product_lots l JOIN products p ON p.id=l.product_id WHERE l.id=? AND p.clinic_id=?",
                (lot_id, clinic_id)).fetchone()
            if lot is None:
                raise NotFound("耗材批次不存在")
            rows = connection.execute(
                "SELECT id,notice_ref,supplier_ref,notice_at,urgency,state,created_at,version "
                "FROM lot_recalls WHERE lot_id=? ORDER BY created_at,id", (lot_id,)).fetchall()
            return {"lot_id": lot_id, "items": [dict(row) for row in rows]}

    def contact_queue(self, clinic_id: str, actor_id: str, recall_id: str) -> dict[str, Any]:
        """排班/联络人员的最小视图：只含完成联络所必需的信息。"""
        with self.db.transaction(write=False) as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            self._authorize_contact(principal)
            recall = connection.execute("SELECT id,urgency FROM lot_recalls WHERE id=? AND clinic_id=?",
                                        (recall_id, clinic_id)).fetchone()
            if recall is None:
                raise NotFound("召回通知不存在")
            rows = connection.execute(
                "SELECT c.id,c.patient_id,c.contact_state,c.next_review_on,c.stage,c.requires_manual_review,"
                "p.display_name,p.phone_ciphertext,c.snapshot_json "
                "FROM lot_recall_cases c JOIN patients p ON p.id=c.patient_id "
                "WHERE c.recall_id=? AND c.stage!='completed' ORDER BY c.created_at,c.id", (recall_id,)).fetchall()
            items = []
            for row in rows:
                snapshot = decode_json(row["snapshot_json"])
                items.append({
                    "case_id": row["id"],
                    "urgency": recall["urgency"],
                    "stage": row["stage"],
                    "contact_state": row["contact_state"],
                    "next_review_on": row["next_review_on"],
                    "patient": {"display_name": row["display_name"], "phone_ciphertext": row["phone_ciphertext"]},
                    "appointment": {"kind": snapshot["appointment"]["kind"],
                                    "starts_at": snapshot["appointment"]["starts_at"],
                                    "ends_at": snapshot["appointment"]["ends_at"]},
                })
            return {"recall_id": recall_id, "urgency": recall["urgency"], "items": items}

    # ------------------------------------------------------------------ 内部辅助

    def _load_case(self, connection, clinic_id: str, case_id: str):
        row = connection.execute(
            "SELECT c.*,r.state AS recall_state FROM lot_recall_cases c JOIN lot_recalls r ON r.id=c.recall_id "
            "WHERE c.id=? AND c.clinic_id=?", (case_id, clinic_id)).fetchone()
        if row is None:
            raise NotFound("召回病例不存在")
        recall = connection.execute("SELECT * FROM lot_recalls WHERE id=?", (row["recall_id"],)).fetchone()
        return row, recall

    def _event(self, connection, recall_id: str, event_type: str, actor_id: str, note: str,
               payload: dict[str, Any], now: str) -> None:
        sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM lot_recall_events WHERE recall_id=?",
                                      (recall_id,)).fetchone()[0]
        connection.execute(
            "INSERT INTO lot_recall_events(id,recall_id,sequence,event_type,actor_id,note,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (new_id("rce"), recall_id, sequence, event_type, actor_id, note, encode_json(payload), now))

    def _lot_movement(self, connection, lot_id: str, key: str, reason: str, actor_id: str, now: str) -> None:
        sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM stock_movements WHERE lot_id=?",
                                      (lot_id,)).fetchone()[0]
        connection.execute(
            "INSERT INTO stock_movements(id,lot_id,event_type,quantity_delta,appointment_id,patient_id,actor_id,reason,idempotency_key,created_at,sequence) "
            "VALUES(?,?,'recalled',0,NULL,NULL,?,?,?,?,?)",
            (new_id("mov"), lot_id, actor_id, reason, key, now, sequence))

    def _insert_case(self, connection, *, recall_id: str, clinic_id: str, lot, reservation,
                     exposure_state: str, manual_review: bool, created_by: str, now: str) -> str:
        case_id = new_id("rcc")
        snapshot = self._build_snapshot(connection, lot=lot, reservation=reservation)
        connection.execute(
            "INSERT INTO lot_recall_cases(id,recall_id,clinic_id,lot_id,reservation_id,appointment_id,patient_id,"
            "exposure_state,snapshot_json,stage,requires_manual_review,contact_state,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'identified',?,'pending',?,?,?)",
            (case_id, recall_id, clinic_id, lot["id"], reservation["id"], reservation["appointment_id"],
             reservation["patient_id"], exposure_state, encode_json(snapshot), int(manual_review),
             created_by, now, now))
        return case_id

    def _build_snapshot(self, connection, *, lot, reservation) -> dict[str, Any]:
        """按批号快照预约、核销记录与患者；查询严格限定在该 lot_id。"""
        appointment = connection.execute(
            "SELECT id,patient_id,plan_id,staff_id,kind,starts_at,ends_at,state FROM appointments WHERE id=?",
            (reservation["appointment_id"],)).fetchone()
        patient = connection.execute("SELECT id,external_ref,display_name FROM patients WHERE id=?",
                                     (reservation["patient_id"],)).fetchone()
        consumption = None
        if reservation["state"] == "consumed":
            movement = connection.execute(
                "SELECT id,actor_id,reason,created_at FROM stock_movements WHERE lot_id=? AND idempotency_key=?",
                (lot["id"], f"reservation:{reservation['id']}:consume")).fetchone()
            if movement:
                consumption = {"movement_id": movement["id"], "consumed_at": movement["created_at"],
                               "recorded_by": movement["actor_id"], "record": movement["reason"]}
        return {
            "product": {"id": lot["product_id"], "name": lot["product_name"], "category": lot["category"],
                        "stock_unit": lot["stock_unit"]},
            "lot": {"id": lot["id"], "lot_number": lot["lot_number"], "supplier_ref": lot["supplier_ref"],
                    "expires_on": lot["expires_on"]},
            "appointment": dict(appointment) if appointment else None,
            "patient": {"id": patient["id"], "external_ref": patient["external_ref"],
                        "display_name": patient["display_name"]} if patient else None,
            "reservation": {"id": reservation["id"], "quantity": reservation["quantity"],
                            "state": reservation["state"], "reserved_by": reservation["reserved_by"],
                            "created_at": reservation["created_at"]},
            "consumption": consumption,
        }

    def _recall_detail(self, connection, recall_id: str, *, clinic_id: str | None = None) -> dict[str, Any]:
        recall = connection.execute("SELECT * FROM lot_recalls WHERE id=?", (recall_id,)).fetchone()
        if recall is None or (clinic_id is not None and recall["clinic_id"] != clinic_id):
            raise NotFound("召回通知不存在")
        lot = connection.execute(
            "SELECT l.*,p.id AS product_id,p.name AS product_name,p.category,p.stock_unit "
            "FROM product_lots l JOIN products p ON p.id=l.product_id WHERE l.id=?", (recall["lot_id"],)).fetchone()
        received = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN event_type='received' THEN quantity_delta ELSE 0 END),0) FROM stock_movements WHERE lot_id=?",
            (lot["id"],)).fetchone()[0]
        balance = connection.execute(
            "SELECT COALESCE(SUM(quantity_delta),0) FROM stock_movements WHERE lot_id=?", (lot["id"],)).fetchone()[0]
        consumed = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN state='consumed' THEN quantity ELSE 0 END),0) FROM stock_reservations WHERE lot_id=?",
            (lot["id"],)).fetchone()[0]
        reserved_open = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN state='reserved' THEN quantity ELSE 0 END),0) FROM stock_reservations WHERE lot_id=?",
            (lot["id"],)).fetchone()[0]

        case_rows = connection.execute("SELECT * FROM lot_recall_cases WHERE recall_id=? ORDER BY created_at,id",
                                       (recall_id,)).fetchall()
        cases = [self._case_view(connection, row) for row in case_rows]
        pending_cases = [row for row in case_rows if row["stage"] != "completed"]
        patients_total = len({row["patient_id"] for row in case_rows})
        patients_pending = len({row["patient_id"] for row in pending_cases})
        events = connection.execute("SELECT sequence,event_type,actor_id,note,payload_json,created_at "
                                    "FROM lot_recall_events WHERE recall_id=? ORDER BY sequence",
                                    (recall_id,)).fetchall()
        revisions = connection.execute("SELECT revision_ref,summary,guidance,created_by,created_at "
                                       "FROM lot_recall_revisions WHERE recall_id=? ORDER BY created_at,id",
                                       (recall_id,)).fetchall()
        return {
            "recall": {"id": recall["id"], "state": recall["state"], "notice_ref": recall["notice_ref"],
                       "supplier_ref": recall["supplier_ref"], "notice_at": recall["notice_at"],
                       "urgency": recall["urgency"], "summary": recall["summary"], "guidance": recall["guidance"],
                       "created_at": recall["created_at"], "updated_at": recall["updated_at"],
                       "version": recall["version"]},
            "lot": {"lot_id": lot["id"], "lot_number": lot["lot_number"], "supplier_ref": lot["supplier_ref"],
                    "expires_on": lot["expires_on"], "state": lot["state"], "product_id": lot["product_id"],
                    "product_name": lot["product_name"], "category": lot["category"], "stock_unit": lot["stock_unit"]},
            "quantities": {"received_total": received, "current_balance": max(0.0, balance),
                           "consumed_total": consumed, "reserved_open_total": reserved_open},
            "cases_total": len(case_rows),
            "cases_pending": len(pending_cases),
            "manual_review_pending": sum(1 for row in case_rows if row["requires_manual_review"]),
            "patients_total": patients_total,
            "patients_pending": patients_pending,
            "revisions": [dict(row) for row in revisions],
            "events": [{"sequence": row["sequence"], "event_type": row["event_type"], "actor_id": row["actor_id"],
                        "note": row["note"], "payload": decode_json(row["payload_json"]),
                        "created_at": row["created_at"]} for row in events],
            "cases": cases,
        }

    def _case_view(self, connection, row) -> dict[str, Any]:
        owner = connection.execute("SELECT display_name FROM staff WHERE id=?", (row["owner_id"],)).fetchone() if row["owner_id"] else None
        return {"id": row["id"], "patient_id": row["patient_id"], "appointment_id": row["appointment_id"],
                "reservation_id": row["reservation_id"], "exposure_state": row["exposure_state"],
                "stage": row["stage"], "requires_manual_review": bool(row["requires_manual_review"]),
                "owner_id": row["owner_id"], "owner_name": owner["display_name"] if owner else None,
                "contact_state": row["contact_state"], "contact_result": row["contact_result"],
                "last_contact_at": row["last_contact_at"], "next_review_on": row["next_review_on"],
                "version": row["version"], "created_at": row["created_at"], "updated_at": row["updated_at"],
                "snapshot": decode_json(row["snapshot_json"])}


    def flag_consumption_after_consume(self, connection, *, clinic_id: str, lot_id: str, reservation,
                                       actor_id: str, now: str, acknowledgement: str | None) -> bool:
        """核销完成后调用：该批号每条进行中的召回都将病例纳入人工复核，绝不从清单消失。

        须在核销事务的同一连接内调用。返回是否命中了任一进行中的召回。
        """
        recalls = connection.execute("SELECT * FROM lot_recalls WHERE lot_id=? AND state='open'", (lot_id,)).fetchall()
        if not recalls:
            return False
        lot = connection.execute(
            "SELECT l.*,p.clinic_id,p.name AS product_name,p.category,p.stock_unit "
            "FROM product_lots l JOIN products p ON p.id=l.product_id WHERE l.id=?", (lot_id,)).fetchone()
        for recall in recalls:
            self._flag_single_recall(connection, recall=recall, clinic_id=clinic_id, lot=lot, reservation=reservation,
                                     actor_id=actor_id, now=now, acknowledgement=acknowledgement)
        return True

    def _flag_single_recall(self, connection, *, recall, clinic_id: str, lot, reservation,
                            actor_id: str, now: str, acknowledgement: str | None) -> None:
        case = connection.execute("SELECT * FROM lot_recall_cases WHERE recall_id=? AND reservation_id=?",
                                  (recall["id"], reservation["id"])).fetchone()
        if case is None:
            current = connection.execute("SELECT * FROM stock_reservations WHERE id=?", (reservation["id"],)).fetchone()
            case_id = self._insert_case(connection, recall_id=recall["id"], clinic_id=clinic_id, lot=lot,
                                        reservation=current, exposure_state="consumed", manual_review=True,
                                        created_by=actor_id, now=now)
            self._event(connection, recall["id"], "case.added", actor_id,
                        "召回期间完成核销，自动纳入并转人工复核",
                        {"case_id": case_id, "exposure_state": "consumed"}, now)
            return
        if case["requires_manual_review"] and case["exposure_state"] == "consumed" and case["stage"] != "completed":
            return
        reopened = case["stage"] == "completed"
        current = connection.execute("SELECT * FROM stock_reservations WHERE id=?", (reservation["id"],)).fetchone()
        snapshot = self._build_snapshot(connection, lot=lot, reservation=current)
        connection.execute(
            "UPDATE lot_recall_cases SET exposure_state='consumed',requires_manual_review=1,snapshot_json=?,"
            "stage=CASE WHEN stage='completed' THEN 'contacted' ELSE stage END,updated_at=?,version=version+1 WHERE id=?",
            (encode_json(snapshot), now, case["id"]))
        self._event(connection, recall["id"], "case.consumption_flagged", actor_id,
                    acknowledgement or "召回期间完成核销，转人工复核",
                    {"case_id": case["id"], "acknowledgement": acknowledgement, "reopened": reopened}, now)
        audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=case["patient_id"],
                           aggregate_type="lot_recall_case", aggregate_id=case["id"],
                           action="recall_case.consumption_flagged", occurred_at=now,
                           payload={"acknowledgement": acknowledgement, "reopened": reopened})
