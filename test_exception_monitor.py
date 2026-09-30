import ast
import csv
import io
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from exception_monitor import (DEFAULT_CONFIG_PATH, ConfigError, build_queue, evaluate, load_config,
                               print_report, write_flags_csv)
from issue_categories import DEFAULT_CATEGORIES_PATH, CategoryError, classify, load_categories

NOW = datetime(2026, 9, 30, 9, 0)
CONFIG = load_config()
CATEGORIES = load_categories()


def request(**overrides):
    """A clean, recent, fully handled Medium request; override to test a rule."""
    row = {
        "request_id": "T-1", "property": "Test Property", "unit": "1A",
        "issue": "Dishwasher not draining", "priority": "Medium",
        "created_at": "2026-09-30 08:00", "status": "Open",
        "vendor_assigned": "Test Vendor", "estimated_cost": "200",
        "last_resident_update": "2026-09-30 08:30", "scheduled_date": "",
        "cost_approved": "", "reported_by": "Resident", "vendor_confirmed": "Yes",
        "hold_reason": "", "hold_until": "", "last_progress_at": "",
    }
    row.update(overrides)
    return row


def rules(row, config=CONFIG):
    return {f.rule for f in evaluate(row, NOW, config, CATEGORIES)}


def emergency(**overrides):
    """A dispatched Emergency with a confirmed vendor and a recent resident update."""
    fields = {"priority": "Emergency", "issue": "Roof leak", "created_at": "2026-09-30 06:00",
              "last_resident_update": "2026-09-30 08:30"}
    fields.update(overrides)
    return request(**fields)


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
        row = emergency(vendor_assigned="", vendor_confirmed="",
                        created_at="2026-09-30 08:50", last_resident_update="")
        self.assertEqual(rules(row), set())

    def test_unconfirmed_vendor_counts_as_not_dispatched(self):
        row = request(priority="Emergency", vendor_confirmed="No", created_at="2026-09-30 08:30")
        self.assertIn("NO_CONFIRMED_DISPATCH", rules(row))

    def test_placeholder_vendor_counts_as_missing(self):
        row = request(priority="High", vendor_assigned="TBD", created_at="2026-09-30 00:00")
        flags = evaluate(row, NOW, CONFIG, CATEGORIES)
        self.assertIn("No vendor assigned", next(f.detail for f in flags if f.rule == "NO_CONFIRMED_DISPATCH"))

    def test_untouched_emergency_has_no_response(self):
        flags = evaluate(emergency(), NOW, CONFIG, CATEGORIES)
        self.assertEqual({(f.rule, f.severity) for f in flags}, {("NO_RESPONSE", "critical")})

    def test_emergency_within_response_window_is_not_flagged(self):
        self.assertEqual(rules(emergency(created_at="2026-09-30 08:15")), set())

    def test_active_emergency_is_not_flagged_before_review_time(self):
        self.assertEqual(rules(emergency(last_progress_at="2026-09-30 08:00")), set())

    def test_stalled_emergency(self):
        flags = evaluate(emergency(last_progress_at="2026-09-30 04:30", created_at="2026-09-30 04:00"), NOW, CONFIG, CATEGORIES)
        self.assertEqual({(f.rule, f.severity) for f in flags}, {("STALLED_PROGRESS", "high")})

    def test_long_running_active_emergency_is_review_not_escalation(self):
        row = emergency(created_at="2026-09-28 09:00", last_progress_at="2026-09-30 08:00")
        flags = evaluate(row, NOW, CONFIG, CATEGORIES)
        self.assertEqual({(f.rule, f.severity) for f in flags}, {("LONG_RUNNING_EMERGENCY", "medium")})

    def test_emergencies_do_not_use_the_general_time_limit(self):
        row = emergency(created_at="2026-09-28 09:00", last_progress_at="2026-09-30 08:00")
        self.assertNotIn("OVER_TIME_LIMIT", rules(row))

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
            flags = evaluate(request(**{field: ""}), NOW, CONFIG, CATEGORIES)
            self.assertIn(("MISSING_INFO", "high"), {(f.rule, f.severity) for f in flags}, field)

    def test_invalid_priority_is_treated_as_missing(self):
        flags = evaluate(request(priority="P1"), NOW, CONFIG, CATEGORIES)
        self.assertIn("invalid", flags[0].detail)

    def test_under_prioritized_safety_issue_is_flagged_not_changed(self):
        row = request(issue="Carbon monoxide alarm going off", priority="Low")
        flags = evaluate(row, NOW, CONFIG, CATEGORIES)
        self.assertIn(("PRIORITY_MISMATCH", "critical"), {(f.rule, f.severity) for f in flags})
        self.assertEqual(row["priority"], "Low")

    def test_over_prioritized_routine_issue_is_flagged(self):
        row = request(issue="Replace burned-out lightbulb", priority="Emergency")
        self.assertIn("PRIORITY_MISMATCH", rules(row))

    def test_emergency_label_is_respected_despite_mismatch(self):
        row = emergency(issue="Replace burned-out lightbulb")
        flags = evaluate(row, NOW, CONFIG, CATEGORIES)
        self.assertIn(("NO_RESPONSE", "critical"), {(f.rule, f.severity) for f in flags})
        self.assertIn("PRIORITY_MISMATCH", {f.rule for f in flags})
        self.assertEqual(row["priority"], "Emergency")

    def test_mismatch_keywords_avoid_known_false_positives(self):
        self.assertNotIn("PRIORITY_MISMATCH", rules(request(issue="Carpet replacement after prior flood")))
        self.assertNotIn("PRIORITY_MISMATCH", rules(request(issue="Smoke detector chirping", priority="Low")))


def category_of(issue):
    return [c.key for c in classify(issue, CATEGORIES).categories]


class CategoryTests(unittest.TestCase):
    def test_safety_category_wins_over_routine(self):
        match = classify("Gas smell reported near stove", CATEGORIES)
        self.assertEqual([c.key for c in match.categories], ["gas"])
        self.assertEqual([c.key for c in match.also_matched], ["appliance"])
        self.assertEqual(category_of("Burst pipe under kitchen sink"), ["flooding_water"])

    def test_new_categories(self):
        self.assertEqual(category_of("Balcony railing loose"), ["structural_fall_hazard"])
        self.assertEqual(category_of("Foundation crack in exterior wall"), ["structural_fall_hazard"])
        self.assertEqual(category_of("Mold spreading on bathroom ceiling"), ["mold_moisture"])

    def test_mold_outranks_routine(self):
        self.assertEqual(category_of("Mold under the sink"), ["mold_moisture"])

    def test_keywords_match_whole_words_and_respect_exclusions(self):
        self.assertEqual(category_of("Smoke detector chirping"), ["general_maintenance"])
        self.assertEqual(category_of("Water heater failure"), ["plumbing"])  # "heat" doesn't match "heater"
        self.assertEqual(category_of("Carpet replacement after prior flood"), ["general_maintenance"])

    def test_unmatched_or_ambiguous_issues_are_not_guessed(self):
        for issue in ("Cockroach infestation in kitchen", "Dishwasher leaking under sink"):
            match = classify(issue, CATEGORIES)
            self.assertEqual(match.categories, [], issue)
            self.assertTrue(match.review_reason, issue)

    def test_uncategorized_request_is_flagged_for_review(self):
        flags = evaluate(request(issue="Cockroach infestation in kitchen"), NOW, CONFIG, CATEGORIES)
        self.assertEqual({(f.rule, f.severity) for f in flags}, {("NEEDS_HUMAN_REVIEW", "medium")})

    def test_mold_request_gets_review_guidance(self):
        flags = evaluate(request(issue="Mildew in bathroom"), NOW, CONFIG, CATEGORIES)
        review = next(f for f in flags if f.rule == "NEEDS_HUMAN_REVIEW")
        self.assertIn("health concerns", review.action)

    def test_two_safety_categories_show_both_and_need_review(self):
        row = emergency(issue="Burning smell from outlet sparking", last_progress_at="2026-09-30 08:30")
        match = classify(row["issue"], CATEGORIES)
        self.assertEqual({c.key for c in match.categories}, {"fire_smoke", "electrical_hazard"})
        flags = evaluate(row, NOW, CONFIG, CATEGORIES)
        self.assertIn(("NEEDS_HUMAN_REVIEW", "high"), {(f.rule, f.severity) for f in flags})

    def test_blank_issue_is_missing_info_not_a_second_review_flag(self):
        self.assertNotIn("NEEDS_HUMAN_REVIEW", rules(request(issue="")))

    def test_safety_issue_below_minimum_priority_is_mismatch(self):
        self.assertIn("PRIORITY_MISMATCH", rules(request(issue="Balcony railing loose", priority="Medium")))
        self.assertNotIn("PRIORITY_MISMATCH", rules(request(issue="Balcony railing loose", priority="High")))

    def test_safety_dispatch_action_points_to_procedure_and_vendor_type(self):
        row = emergency(issue="Gas smell in kitchen", vendor_assigned="", vendor_confirmed="")
        action = next(f.action for f in evaluate(row, NOW, CONFIG, CATEGORIES) if f.rule == "NO_CONFIRMED_DISPATCH")
        self.assertTrue(action.startswith("Follow the escalation procedure above."))
        self.assertIn("licensed gas technician", action)

    def test_routine_dispatch_action_names_vendor_type(self):
        row = request(priority="High", issue="Refrigerator not working", vendor_assigned="",
                      vendor_confirmed="", created_at="2026-09-30 00:00")
        action = next(f.action for f in evaluate(row, NOW, CONFIG, CATEGORIES) if f.rule == "NO_CONFIRMED_DISPATCH")
        self.assertEqual(action, "Assign an appliance repair vendor and get confirmation of dispatch.")

    def test_category_file_is_labeled_as_prototype(self):
        self.assertIn("PROTOTYPE EXAMPLES ONLY", DEFAULT_CATEGORIES_PATH.read_text())

    def test_invalid_category_file_is_an_error(self):
        text = DEFAULT_CATEGORIES_PATH.read_text().replace('min_priority = "High"\nvendor = "the elevator', 'vendor = "the elevator', 1)
        tmp = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        tmp.write(text)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        with self.assertRaisesRegex(CategoryError, "min_priority"):
            load_categories(tmp.name)


class NoExternalActionsTests(unittest.TestCase):
    """The prototype only surfaces issues; it must not contact anyone or anything."""

    NETWORK_MODULES = {"socket", "urllib", "http", "requests", "smtplib", "email", "ftplib",
                       "telnetlib", "subprocess", "webbrowser", "xmlrpc"}

    def test_no_network_or_process_imports(self):
        for name in ("exception_monitor.py", "issue_categories.py"):
            tree = ast.parse(Path(__file__).with_name(name).read_text())
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertFalse(imported & self.NETWORK_MODULES, name)

    def test_report_states_nothing_was_acted_on_and_procedures_are_prototypes(self):
        queue = build_queue([emergency(issue="Gas smell in kitchen")], NOW, CONFIG, CATEGORIES)
        out = io.StringIO()
        print_report([emergency()], queue, NOW, CATEGORIES, out=out)
        text = out.getvalue()
        self.assertIn("Nothing below has been changed or acted on", text)
        self.assertIn("PROTOTYPE EXAMPLE - not approved for real-world use", text)


class ConfigTests(unittest.TestCase):
    def write_config(self, text):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        tmp.write(text)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return tmp.name

    def default_text(self):
        return DEFAULT_CONFIG_PATH.read_text()

    def test_default_config_loads(self):
        self.assertEqual(CONFIG.dispatch_grace["Emergency"], timedelta(minutes=15))
        self.assertEqual(CONFIG.emergency_stalled_after, timedelta(hours=4))
        self.assertEqual(CONFIG.high_cost_threshold, 1000)

    def test_changing_a_threshold_changes_the_result(self):
        row = request(estimated_cost="1500")
        self.assertIn("HIGH_COST_REVIEW", rules(row))
        raised = load_config(self.write_config(
            self.default_text().replace("high_cost_threshold = 1000", "high_cost_threshold = 2000")))
        self.assertNotIn("HIGH_COST_REVIEW", rules(row, raised))

    def test_missing_setting_is_an_error(self):
        path = self.write_config(self.default_text().replace("stalled_after = 4\n", ""))
        with self.assertRaisesRegex(ConfigError, "stalled_after"):
            load_config(path)

    def test_misspelled_setting_is_an_error(self):
        path = self.write_config(self.default_text().replace("High = 24\nMedium", "Hgh = 24\nMedium", 1))
        with self.assertRaisesRegex(ConfigError, "Hgh"):
            load_config(path)

    def test_non_positive_setting_is_an_error(self):
        path = self.write_config(self.default_text().replace("response = 1", "response = 0"))
        with self.assertRaisesRegex(ConfigError, "positive"):
            load_config(path)


class SampleDataTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name("maintenance_requests.csv")
        with open(path, newline="") as fh:
            self.queue = build_queue(list(csv.DictReader(fh)), NOW, CONFIG, CATEGORIES)
        self.flagged = {row["request_id"]: {f.rule for f in flags} for row, flags in self.queue}

    def test_every_flag_has_a_reason_and_recommended_action(self):
        for row, flags in self.queue:
            for f in flags:
                self.assertTrue(f.detail and f.action, (row["request_id"], f.rule))

    def test_flags_csv_has_requested_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "exceptions.csv")
            write_flags_csv(self.queue, path, CATEGORIES)
            with open(path, newline="") as fh:
                rows = list(csv.DictReader(fh))
        for column in ("request_id", "property", "issue", "priority", "reason", "recommended_action"):
            self.assertIn(column, rows[0])
        self.assertEqual({r["request_id"] for r in rows}, set(self.flagged))

    def test_monitor_does_not_modify_requests(self):
        path = Path(__file__).with_name("maintenance_requests.csv")
        with open(path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        before = [dict(r) for r in rows]
        build_queue(rows, NOW, CONFIG, CATEGORIES)
        self.assertEqual(rows, before)

    def test_emergency_stages_in_sample_data(self):
        self.assertIn("NO_RESPONSE", self.flagged["MR-1001"])
        self.assertIn("LONG_RUNNING_EMERGENCY", self.flagged["MR-1007"])
        self.assertNotIn("NO_RESPONSE", self.flagged["MR-1007"])
        self.assertIn("STALLED_PROGRESS", self.flagged["MR-1016"])

    def test_expected_clean_requests_are_not_flagged(self):
        for request_id in ("MR-1002", "MR-1008", "MR-1010", "MR-1014", "MR-1018"):
            self.assertNotIn(request_id, self.flagged)

    def test_uncategorized_request_is_flagged_even_when_otherwise_clean(self):
        # MR-1012 (cockroaches) is on a valid hold with nothing else wrong.
        self.assertEqual(self.flagged["MR-1012"], {"NEEDS_HUMAN_REVIEW"})

    def test_carbon_monoxide_alarm_ranks_with_emergencies(self):
        order = [row["request_id"] for row, _ in self.queue]
        self.assertLess(order.index("MR-1023"), order.index("MR-1015"))
        self.assertIn("PRIORITY_MISMATCH", self.flagged["MR-1023"])


if __name__ == "__main__":
    unittest.main()
