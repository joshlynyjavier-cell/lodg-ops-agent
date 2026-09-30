import csv
import unittest
from datetime import datetime
from pathlib import Path

from exception_monitor import build_queue, evaluate

NOW = datetime(2026, 9, 30, 9, 0)


def request(**overrides):
    """A clean, recent, fully handled Medium request; override to test a rule."""
    row = {
        "request_id": "T-1", "property": "Test Property", "unit": "1A",
        "issue": "Dishwasher not draining", "priority": "Medium",
        "created_at": "2026-09-30 08:00", "status": "Open",
        "vendor_assigned": "Test Vendor", "estimated_cost": "200",
        "last_resident_update": "2026-09-30 08:30", "scheduled_date": "",
        "cost_approved": "", "reported_by": "Resident", "vendor_confirmed": "Yes",
        "hold_reason": "", "hold_until": "",
    }
    row.update(overrides)
    return row


def rules(row):
    return {f.rule for f in evaluate(row, NOW)}


class RuleTests(unittest.TestCase):
    def test_clean_request_is_not_flagged(self):
        self.assertEqual(rules(request()), set())

    def test_completed_request_is_never_flagged(self):
        row = request(status="Completed", created_at="2026-01-01 00:00", estimated_cost="9000")
        self.assertEqual(rules(row), set())

    def test_emergency_without_vendor_after_grace(self):
        row = request(priority="Emergency", vendor_assigned="", vendor_confirmed="",
                      created_at="2026-09-30 08:30")
        self.assertIn("NO_CONFIRMED_DISPATCH", rules(row))

    def test_emergency_within_grace_is_not_flagged(self):
        row = request(priority="Emergency", vendor_assigned="", vendor_confirmed="",
                      created_at="2026-09-30 08:50", last_resident_update="")
        self.assertEqual(rules(row), set())

    def test_unconfirmed_vendor_counts_as_not_dispatched(self):
        row = request(priority="Emergency", vendor_confirmed="No", created_at="2026-09-30 08:30")
        self.assertIn("NO_CONFIRMED_DISPATCH", rules(row))

    def test_placeholder_vendor_counts_as_missing(self):
        row = request(priority="High", vendor_assigned="TBD", created_at="2026-09-30 00:00")
        flags = evaluate(row, NOW)
        self.assertIn("No vendor assigned", next(f.detail for f in flags if f.rule == "NO_CONFIRMED_DISPATCH"))

    def test_time_limits_depend_on_priority(self):
        three_days_old = "2026-09-27 09:00"
        self.assertIn("OVER_TIME_LIMIT", rules(request(created_at=three_days_old, last_resident_update="2026-09-30 08:00")))
        self.assertNotIn("OVER_TIME_LIMIT", rules(request(priority="Low", created_at=three_days_old)))

    def test_future_appointment_pauses_medium_clock(self):
        row = request(created_at="2026-09-27 09:00", last_resident_update="2026-09-30 08:00",
                      scheduled_date="2026-10-01 10:00")
        self.assertNotIn("OVER_TIME_LIMIT", rules(row))

    def test_future_appointment_does_not_pause_high_clock(self):
        row = request(priority="High", created_at="2026-09-28 09:00", scheduled_date="2026-10-01 10:00")
        self.assertIn("OVER_TIME_LIMIT", rules(row))

    def test_valid_hold_pauses_clock(self):
        row = request(status="On Hold", created_at="2026-09-20 09:00", last_resident_update="2026-09-30 08:00",
                      hold_reason="Waiting on parts", hold_until="2026-10-05")
        self.assertEqual(rules(row), set())

    def test_hold_without_reason_or_end_date_is_flagged(self):
        row = request(status="On Hold", created_at="2026-09-20 09:00", hold_until="2026-10-05")
        self.assertTrue({"INVALID_HOLD", "OVER_TIME_LIMIT"} <= rules(row))

    def test_expired_hold_is_flagged(self):
        row = request(status="On Hold", created_at="2026-09-20 09:00",
                      hold_reason="Waiting on parts", hold_until="2026-09-29")
        self.assertTrue({"HOLD_EXPIRED", "OVER_TIME_LIMIT"} <= rules(row))

    def test_emergency_cannot_be_on_hold(self):
        row = request(priority="Emergency", status="On Hold",
                      hold_reason="Waiting on parts", hold_until="2026-10-05")
        self.assertIn("INVALID_HOLD", rules(row))

    def test_high_cost_needs_review_unless_approved(self):
        self.assertIn("HIGH_COST_REVIEW", rules(request(estimated_cost="1500")))
        self.assertNotIn("HIGH_COST_REVIEW", rules(request(estimated_cost="1500", cost_approved="Yes")))
        self.assertNotIn("HIGH_COST_REVIEW", rules(request(estimated_cost="1000")))

    def test_missing_cost_flagged_only_after_grace(self):
        self.assertEqual(rules(request(estimated_cost="")), set())
        old = request(estimated_cost="", created_at="2026-09-29 06:00", last_resident_update="2026-09-30 08:00")
        self.assertIn("MISSING_INFO", rules(old))

    def test_never_updated_resident_is_measured_from_creation(self):
        row = request(priority="High", created_at="2026-09-29 06:00", last_resident_update="")
        self.assertIn("RESIDENT_UPDATE_OVERDUE", rules(row))

    def test_staff_reported_request_has_no_resident_to_update(self):
        row = request(created_at="2026-09-29 06:00", last_resident_update="", reported_by="Staff")
        self.assertNotIn("RESIDENT_UPDATE_OVERDUE", rules(row))

    def test_missing_blocking_fields_are_high_severity(self):
        for field in ("issue", "priority", "created_at"):
            flags = evaluate(request(**{field: ""}), NOW)
            self.assertIn(("MISSING_INFO", "high"), {(f.rule, f.severity) for f in flags}, field)

    def test_invalid_priority_is_treated_as_missing(self):
        flags = evaluate(request(priority="P1"), NOW)
        self.assertIn("invalid", flags[0].detail)

    def test_under_prioritized_safety_issue_is_flagged_not_changed(self):
        row = request(issue="Carbon monoxide alarm going off", priority="Low")
        flags = evaluate(row, NOW)
        self.assertIn(("PRIORITY_MISMATCH", "critical"), {(f.rule, f.severity) for f in flags})
        self.assertEqual(row["priority"], "Low")

    def test_over_prioritized_routine_issue_is_flagged(self):
        row = request(issue="Replace burned-out lightbulb", priority="Emergency")
        self.assertIn("PRIORITY_MISMATCH", rules(row))

    def test_mismatch_keywords_avoid_known_false_positives(self):
        self.assertNotIn("PRIORITY_MISMATCH", rules(request(issue="Carpet replacement after prior flood")))
        self.assertNotIn("PRIORITY_MISMATCH", rules(request(issue="Smoke detector chirping", priority="Low")))


class SampleDataTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name("maintenance_requests.csv")
        with open(path, newline="") as fh:
            self.queue = build_queue(list(csv.DictReader(fh)), NOW)
        self.flagged = {row["request_id"]: {f.rule for f in flags} for row, flags in self.queue}

    def test_expected_clean_requests_are_not_flagged(self):
        for request_id in ("MR-1002", "MR-1008", "MR-1010", "MR-1012", "MR-1014", "MR-1018"):
            self.assertNotIn(request_id, self.flagged)

    def test_carbon_monoxide_alarm_ranks_with_emergencies(self):
        order = [row["request_id"] for row, _ in self.queue]
        self.assertLess(order.index("MR-1023"), order.index("MR-1015"))
        self.assertIn("PRIORITY_MISMATCH", self.flagged["MR-1023"])


if __name__ == "__main__":
    unittest.main()
