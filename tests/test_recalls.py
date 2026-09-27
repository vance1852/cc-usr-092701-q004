from __future__ import annotations

import json
import threading
import unittest
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

from careflow.errors import Conflict, Forbidden, ValidationError

from test_careflow import CareflowCase


class RecallCase(unittest.TestCase):
    def setUp(self):
        CareflowCase.setUp(self)
        self.p1 = self.patient["id"]
        self.p2 = self.app.create_patient(self.clinic, self.coordinator, "case-218", "陈先生")["id"]
        self.p3 = self.app.create_patient(self.clinic, self.coordinator, "case-219", "赵女士")["id"]
        self.product = self.app.supplies.register_product(self.clinic, self.owner, "注射用透明质酸", "injectable", "支")
        self.lot_a = self.app.supplies.receive_lot(self.clinic, self.owner, self.product["id"], "sup-x", "A-100",
                                                   10, "lot-a-in", expires_on="2027-01-01")["id"]
        self.lot_b = self.app.supplies.receive_lot(self.clinic, self.owner, self.product["id"], "sup-x", "B-200",
                                                   10, "lot-b-in", expires_on="2027-06-01")["id"]

    def tearDown(self):
        CareflowCase.tearDown(self)

    def visit(self, patient, key, hour):
        appointment = self.app.create_appointment(
            self.clinic, self.coordinator, patient, "注射",
            f"2026-09-29T{hour:02d}:00:00+08:00", f"2026-09-29T{hour:02d}:30:00+08:00", key,
            staff_id=self.clinician)
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 1, "book")
        arrived = self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 2, "arrive")
        return appointment["id"], arrived["version"]

    def reserve(self, appointment_id, quantity, key):
        return self.app.supplies.reserve(self.clinic, self.clinician, appointment_id,
                                         self.product["id"], quantity, key)

    def open_recall(self, lot_id=None, key="notice-1", urgency="high"):
        lot_id = lot_id or self.lot_a
        return self.app.recalls.open_recall(self.clinic, self.owner, lot_id, "sup-x", "NTC-001",
                                            "2026-09-27T09:00:00Z", urgency, "供应商通知微粒污染", key)

    def case_for(self, detail, patient_id):
        return next(case for case in detail["cases"] if case["patient_id"] == patient_id)

    def prepare_two_lot_a_cases(self):
        # p1：召回前已核销（正在接受处置/已领用）；p2：尚未使用的预约预留。
        a1, _ = self.visit(self.p1, "v-1", 9)
        r1 = self.reserve(a1, 2, "rsv-1")
        self.assertEqual({x["lot_id"] for x in r1["reservations"]}, {self.lot_a})
        consumed = self.app.supplies.consume_reservation(
            self.clinic, self.clinician, r1["reservations"][0]["id"], expected_version=1)
        a2, _ = self.visit(self.p2, "v-2", 10)
        r2 = self.reserve(a2, 1, "rsv-2")
        self.assertEqual({x["lot_id"] for x in r2["reservations"]}, {self.lot_a})
        return r1, r2, consumed

    def test_snapshot_is_scoped_to_lot_and_covers_consumed_reserved_and_patients(self):
        self.prepare_two_lot_a_cases()
        a3, _ = self.visit(self.p3, "v-3", 11)
        # lot_a 余 7，预留 8 会跨批：7 支 A-100 + 1 支 B-200。
        split = self.reserve(a3, 8, "rsv-3")
        lots = {x["lot_id"]: x["quantity"] for x in split["reservations"]}
        self.assertEqual(lots, {self.lot_a: 7.0, self.lot_b: 1.0})

        recall = self.open_recall()
        detail = self.app.recalls.recall_detail(self.clinic, self.owner, recall["id"])
        # 快照严格按批号：B-200 的预留绝不混入。
        self.assertEqual({case["snapshot"]["lot"]["id"] for case in detail["cases"]}, {self.lot_a})
        self.assertEqual({case["snapshot"]["lot"]["lot_number"] for case in detail["cases"]}, {"A-100"})
        reservations_in_snapshot = {case["reservation_id"] for case in detail["cases"]}
        lot_b_reservation = split["reservations"][1]["id"]
        self.assertNotIn(lot_b_reservation, reservations_in_snapshot)
        # 已核销与尚未使用的预约都在清单上，并保留核销证据。
        by_patient = {case["patient_id"]: case for case in detail["cases"]}
        self.assertEqual(by_patient[self.p1]["exposure_state"], "consumed")
        self.assertIsNotNone(by_patient[self.p1]["snapshot"]["consumption"])
        self.assertEqual(by_patient[self.p2]["exposure_state"], "reserved")
        self.assertIsNone(by_patient[self.p2]["snapshot"]["consumption"])
        self.assertIn(self.p3, by_patient)  # 跨批预约中使用 A-100 的部分仍受影响
        # 批次总量与未处置人数。
        self.assertEqual(detail["quantities"]["received_total"], 10)
        self.assertEqual(detail["patients_total"], 3)
        self.assertEqual(detail["patients_pending"], 3)
        self.assertEqual(detail["cases_pending"], 3)

    def test_notice_import_is_idempotent_and_conflicting_duplicate_is_rejected(self):
        self.prepare_two_lot_a_cases()
        first = self.open_recall(key="notice-x")
        replay = self.open_recall(key="notice-x-again")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["id"], first["id"])
        # 同通知编号但内容变化不能冒充重复导入。
        with self.assertRaises(Conflict):
            self.open_recall(key="notice-x-changed", urgency="urgent")

    def test_urgency_can_only_escalate_and_keeps_basis(self):
        self.prepare_two_lot_a_cases()
        recall = self.open_recall(urgency="moderate")
        with self.assertRaises(ValidationError):
            self.app.recalls.escalate_urgency(self.clinic, self.owner, recall["id"], "urgent", "短", 1)
        escalated = self.app.recalls.escalate_urgency(
            self.clinic, self.owner, recall["id"], "urgent", "两名患者当日报告局部红肿，需立即联络", 1)
        self.assertEqual(escalated["urgency"], "urgent")
        # 不能降回更低等级（人工或供应商修订都不行）。
        with self.assertRaises(Conflict):
            self.app.recalls.escalate_urgency(self.clinic, self.owner, recall["id"], "low", "供应商通知试图降级处理", 2)
        with self.assertRaises(Conflict):
            self.app.recalls.revise_recall(self.clinic, self.owner, recall["id"], "REV-1", "rev-1",
                                           "供应商称风险降低", urgency="low")
        detail = self.app.recalls.recall_detail(self.clinic, self.owner, recall["id"])
        basis = [event for event in detail["events"] if event["event_type"] == "recall.urgency_escalated"]
        self.assertEqual(basis[-1]["payload"]["from"], "moderate")
        self.assertIn("局部红肿", basis[-1]["note"])

    def test_supplier_revision_appends_but_never_erases_completed_handling(self):
        self.prepare_two_lot_a_cases()
        recall = self.open_recall()
        detail = self.app.recalls.recall_detail(self.clinic, self.owner, recall["id"])
        case = self.case_for(detail, self.p2)
        # 完成一例：仅预留未使用，可免联络，但必须安排复核日期。
        self.app.recalls.assign_case(self.clinic, self.owner, case["id"], self.nurse)
        with self.assertRaises(Conflict):
            self.app.recalls.complete_case(
                self.clinic, self.owner, case["id"], "预留未使用，无复核日期直接完成",
                case["version"] + 1, no_contact_required=True)
        done = self.app.recalls.complete_case(
            self.clinic, self.owner, case["id"], "预约尚未使用，取消并改期，无需联络患者",
            case["version"] + 1, no_contact_required=True, next_review_on="2026-10-05")
        self.assertEqual(done["stage"], "completed")
        self.assertEqual(done["next_review_on"], "2026-10-05")
        version_before = self.app.recalls.recall_detail(self.clinic, self.owner, recall["id"])["recall"]["version"]
        revised = self.app.recalls.revise_recall(
            self.clinic, self.owner, recall["id"], "REV-9", "rev-9", "供应商补充封存与回运指引",
            guidance="停用封存并等待回运")
        self.assertEqual(revised["version"], version_before + 1)
        detail = self.app.recalls.recall_detail(self.clinic, self.owner, recall["id"])
        self.assertEqual(detail["recall"]["guidance"], "停用封存并等待回运")
        case_after = self.case_for(detail, self.p2)
        self.assertEqual(case_after["stage"], "completed")  # 已完成处置未被修订抹掉
        self.assertEqual(detail["patients_pending"], 1)
        self.assertTrue(any(event["event_type"] == "recall.revised" for event in detail["events"]))

    def test_consumption_during_recall_goes_to_manual_review_and_completed_case_reopens(self):
        self.prepare_two_lot_a_cases()
        recall = self.open_recall()
        detail = self.app.recalls.recall_detail(self.clinic, self.owner, recall["id"])
        reserved_case = self.case_for(detail, self.p2)
        reservation_id = reserved_case["reservation_id"]
        # 召回批号默认拒绝核销。
        with self.assertRaises(Conflict):
            self.app.supplies.consume_reservation(self.clinic, self.clinician, reservation_id, expected_version=1)
        # 先把该例作为"未使用预约"完成处置。
        self.app.recalls.complete_case(
            self.clinic, self.owner, reserved_case["id"], "预留未使用，安排改期",
            reserved_case["version"], no_contact_required=True, next_review_on="2026-10-05")
        # 临床上实际仍开封使用：带书面确认核销，病例必须重新出现并进入人工复核，而非消失。
        consumed = self.app.supplies.consume_reservation(
            self.clinic, self.clinician, reservation_id, expected_version=1,
            recall_acknowledgement="患者已到院且知情同意，临床决定使用")
        self.assertTrue(consumed.get("recall_manual_review"))
        detail = self.app.recalls.recall_detail(self.clinic, self.owner, recall["id"])
        case = self.case_for(detail, self.p2)
        self.assertEqual(case["exposure_state"], "consumed")
        self.assertTrue(case["requires_manual_review"])
        self.assertNotEqual(case["stage"], "completed")
        self.assertEqual(detail["cases_pending"], 2)
        self.assertEqual(detail["manual_review_pending"], 1)
        # 快照必须保留召回期间核销的证据（核销流水、时间、记录人）。
        self.assertIsNotNone(case["snapshot"]["consumption"])
        self.assertEqual(case["snapshot"]["reservation"]["state"], "consumed")
        self.assertIn("召回批号临床确认", case["snapshot"]["consumption"]["record"])
        # 人工复核未清前不能完成；且只能由临床岗位/负责人清理。
        with self.assertRaises(Conflict):
            self.app.recalls.complete_case(self.clinic, self.owner, case["id"], "尝试直接完成处置", case["version"])
        with self.assertRaises(Forbidden):
            self.app.recalls.resolve_manual_review(self.clinic, self.nurse, case["id"], "护士尝试人工复核", case["version"])
        self.app.recalls.resolve_manual_review(
            self.clinic, self.clinician, case["id"], "核对病历与使用记录，患者无不适，安排复诊",
            case["version"], next_review_on="2026-10-05")
        self.app.recalls.record_contact(
            self.clinic, self.coordinator, case["id"], "reached", case["version"] + 1,
            result="已告知召回与复诊安排", next_review_on="2026-10-05")
        # 复核日期逾期后，诊断巡检报告该病例。
        self.clock.set(datetime(2026, 10, 20, 0, 0, tzinfo=UTC))
        findings = self.app.run_diagnostics(self.clinic, self.owner)["findings"]
        self.assertTrue(any(f["code"] == "recall.review_overdue" and f["aggregate_id"] == case["id"] for f in findings))

    def test_owner_sees_full_tracking_but_scheduling_staff_sees_only_contact_info(self):
        self.prepare_two_lot_a_cases()
        recall = self.open_recall()
        detail = self.app.recalls.recall_detail(self.clinic, self.owner, recall["id"])
        case = self.case_for(detail, self.p1)
        self.app.recalls.assign_case(self.clinic, self.owner, case["id"], self.nurse)
        self.app.recalls.record_contact(self.clinic, self.coordinator, case["id"], "unreachable",
                                        case["version"] + 1, next_review_on="2026-09-30")
        detail = self.app.recalls.recall_detail(self.clinic, self.owner, recall["id"])
        case = self.case_for(detail, self.p1)
        # 负责人视角：批次总量、未处置人数、每例最后状态。
        self.assertEqual(detail["quantities"]["received_total"], 10)
        self.assertEqual(detail["patients_pending"], 2)
        self.assertEqual(case["contact_state"], "unreachable")
        self.assertEqual(case["owner_name"], "护理人员")
        self.assertIn("snapshot", case)
        # 排班人员：最小联络队列，只含完成联络所需信息。
        queue = self.app.recalls.contact_queue(self.clinic, self.coordinator, recall["id"])
        self.assertEqual({item["case_id"] for item in queue["items"]},
                         {c["id"] for c in detail["cases"]})  # 两例均未完成
        item = next(item for item in queue["items"] if item["case_id"] == case["id"])
        self.assertEqual(set(item), {"case_id", "urgency", "stage", "contact_state", "next_review_on",
                                     "patient", "appointment"})
        self.assertEqual(set(item["patient"]), {"display_name", "phone_ciphertext"})
        self.assertNotIn("snapshot", item)
        self.assertNotIn("exposure_state", item)
        # 排班人员不能看负责人详情，也不能开召回或做人工复核。
        with self.assertRaises(Forbidden):
            self.app.recalls.recall_detail(self.clinic, self.coordinator, recall["id"])
        with self.assertRaises(Forbidden):
            self.app.recalls.open_recall(self.clinic, self.coordinator, self.lot_b, "sup-x", "NTC-002",
                                         "2026-09-27T09:00:00Z", "high", "协调员越权发起召回", "notice-by-coord")

    def test_completion_requires_review_date_and_real_contact_for_consumed_patients(self):
        self.prepare_two_lot_a_cases()
        recall = self.open_recall()
        detail = self.app.recalls.recall_detail(self.clinic, self.owner, recall["id"])
        consumed_case = self.case_for(detail, self.p1)
        reserved_case = self.case_for(detail, self.p2)
        # 没有复核日期不能完成。
        with self.assertRaises(Conflict):
            self.app.recalls.complete_case(self.clinic, self.owner, consumed_case["id"], "无复核日期尝试完成处置",
                                           consumed_case["version"])
        # 已实际使用的患者不能免联络。
        self.app.recalls.record_contact(self.clinic, self.coordinator, consumed_case["id"], "reached",
                                        consumed_case["version"], result="已联系", next_review_on="2026-10-01")
        with self.assertRaises(Conflict):
            self.app.recalls.complete_case(self.clinic, self.owner, consumed_case["id"], "尝试免联络",
                                           consumed_case["version"] + 1, no_contact_required=True)
        completed = self.app.recalls.complete_case(self.clinic, self.owner, consumed_case["id"],
                                                   "患者已告知并安排体检", consumed_case["version"] + 1)
        self.assertEqual(completed["stage"], "completed")
        # 未接通不能完成；预约预留可免联络。
        self.app.recalls.record_contact(self.clinic, self.coordinator, reserved_case["id"], "declined",
                                        reserved_case["version"], result="患者拒绝沟通", next_review_on="2026-10-01")
        with self.assertRaises(Conflict):
            self.app.recalls.complete_case(self.clinic, self.owner, reserved_case["id"], "未接通就完成",
                                           reserved_case["version"] + 1)

    def test_recall_acknowledgement_requires_clinician_and_released_reservation_stays_on_list(self):
        self.prepare_two_lot_a_cases()
        recall = self.open_recall()
        detail = self.app.recalls.recall_detail(self.clinic, self.owner, recall["id"])
        reserved_case = self.case_for(detail, self.p2)
        # 护士不能对召回批号作临床使用确认（预留仍有效时先触发岗位限制）。
        with self.assertRaises(Conflict):
            self.app.supplies.consume_reservation(
                self.clinic, self.nurse, reserved_case["reservation_id"], expected_version=1,
                recall_acknowledgement="护理人员尝试确认使用召回批次")
        # 召回后释放未使用预留：库存退回，但病例仍保留在召回清单上。
        released = self.app.supplies.release_reservation(
            self.clinic, self.coordinator, reserved_case["reservation_id"], "召回停用，释放备货", 1)
        self.assertEqual(released["state"], "released")
        detail = self.app.recalls.recall_detail(self.clinic, self.owner, recall["id"])
        self.assertTrue(any(c["id"] == reserved_case["id"] for c in detail["cases"]))

    def test_http_open_recall_and_contact_queue(self):
        self.prepare_two_lot_a_cases()
        token = self.app.login(self.clinic, self.owner, "LongPassphrase!2026")["access_token"]
        server = ThreadingHTTPServer(("127.0.0.1", 0), __import__("careflow.api", fromlist=["create_handler"]).create_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        headers = {"X-Clinic-ID": self.clinic, "Authorization": f"Bearer {token}",
                   "Content-Type": "application/json", "Idempotency-Key": "http-notice-1"}
        try:
            body = json.dumps({"supplier_ref": "sup-x", "notice_ref": "NTC-001",
                               "notice_at": "2026-09-27T09:00:00Z", "urgency": "high",
                               "summary": "供应商通知微粒污染"}).encode()
            request = Request(base + f"/stock/{self.lot_a}/recalls", data=body, method="POST", headers=headers)
            with urlopen(request, timeout=3) as response:
                recall = json.loads(response.read())
                self.assertEqual(response.status, 201)
                self.assertEqual(recall["case_count"], 2)
            request = Request(base + f"/recalls/{recall['id']}/contact-queue",
                              headers={"X-Clinic-ID": self.clinic, "Authorization": f"Bearer {token}"})
            with urlopen(request, timeout=3) as response:
                queue = json.loads(response.read())
                self.assertEqual(len(queue["items"]), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(RecallCase))
    return suite


if __name__ == "__main__":
    unittest.main()
