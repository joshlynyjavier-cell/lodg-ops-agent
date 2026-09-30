import ast
import csv
import io
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from exception_monitor import (DEFAULT_CONFIG_PATH, ConfigError, build_financial_review, build_queue,
                               category_lines, evaluate, load_config, print_report, write_flags_csv)
from issue_categories import DEFAULT_CATEGORIES_PATH, CategoryError, classify, load_categories
from todays_exceptions import exception_line, headline_flag, render_today

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


class VendorAwareActionTests(unittest.TestCase):
    """Actions must never send an operator to a vendor that doesn't exist."""

    CONTACT_WORDING = ("the vendor", "assigned vendor", "Get a status from", "Contact ", "Call ")

    def assert_no_vendor_contact(self, flags):
        for f in flags:
            if f.rule in ("NO_RESPONSE", "STALLED_PROGRESS", "OVER_TIME_LIMIT", "LONG_RUNNING_EMERGENCY"):
                for wording in self.CONTACT_WORDING:
                    self.assertNotIn(wording, f.action, (f.rule, f.action))

    def test_emergency_without_vendor_gets_escalation_not_vendor_contact(self):
        row = emergency(issue="Gas smell in kitchen", vendor_assigned="", vendor_confirmed="")
        flags = evaluate(row, NOW, CONFIG, CATEGORIES)
        self.assert_no_vendor_contact(flags)
        action = next(f.action for f in flags if f.rule == "NO_RESPONSE")
        self.assertIn("No vendor is assigned", action)
        self.assertIn("licensed gas technician", action)

    def test_overdue_request_without_vendor_gets_dispatch_step(self):
        row = request(priority="High", issue="Refrigerator not working", vendor_assigned="", vendor_confirmed="",
                      created_at="2026-09-28 09:00")
        flags = evaluate(row, NOW, CONFIG, CATEGORIES)
        self.assert_no_vendor_contact(flags)
        action = next(f.action for f in flags if f.rule == "OVER_TIME_LIMIT")
        self.assertIn("Assign an appliance repair vendor", action)

    def test_stalled_and_long_running_without_vendor(self):
        stalled = emergency(vendor_assigned="", created_at="2026-09-30 00:00", last_progress_at="2026-09-30 02:00")
        long_running = emergency(vendor_assigned="", created_at="2026-09-28 09:00", last_progress_at="2026-09-30 08:30")
        for row in (stalled, long_running):
            self.assert_no_vendor_contact(evaluate(row, NOW, CONFIG, CATEGORIES))

    def test_assigned_vendor_is_named(self):
        row = request(priority="High", vendor_assigned="AquaFlow Plumbing", issue="Leaking faucet",
                      created_at="2026-09-28 09:00")
        action = next(f.action for f in evaluate(row, NOW, CONFIG, CATEGORIES) if f.rule == "OVER_TIME_LIMIT")
        self.assertIn("Get a status from AquaFlow Plumbing", action)

    def test_no_sample_action_contacts_a_missing_vendor(self):
        path = Path(__file__).with_name("maintenance_requests.csv")
        with open(path, newline="") as fh:
            rows = [r for r in csv.DictReader(fh) if not r["vendor_assigned"].strip()]
        for row in rows:
            self.assert_no_vendor_contact(evaluate(row, NOW, CONFIG, CATEGORIES))


class ConflictingStandardActionTests(unittest.TestCase):
    def test_routine_action_withheld_when_priority_is_higher(self):
        match = classify("Replace burned-out bedroom lightbulb", CATEGORIES)
        text = "\n".join(category_lines(match, "Emergency"))
        self.assertNotIn("next available routine slot", text)
        self.assertIn("conflicts with the assigned Emergency priority", text)

    def test_routine_action_shown_within_usual_priority(self):
        match = classify("Replace burned-out bedroom lightbulb", CATEGORIES)
        self.assertIn("next available routine slot", "\n".join(category_lines(match, "Low")))

    def test_mismatch_still_flagged_and_priority_respected(self):
        row = emergency(issue="Replace burned-out bedroom lightbulb")
        flags = evaluate(row, NOW, CONFIG, CATEGORIES)
        self.assertIn("PRIORITY_MISMATCH", {f.rule for f in flags})
        self.assertIn(("NO_RESPONSE", "critical"), {(f.rule, f.severity) for f in flags})
        self.assertEqual(row["priority"], "Emergency")


class HazardScreeningTests(unittest.TestCase):
    """A routine keyword must never override a possible danger signal."""

    def test_hazard_words_override_routine_match(self):
        match = classify("Water pouring through ceiling light fixture", CATEGORIES)
        self.assertEqual(match.categories, [])
        self.assertIn("pouring", match.hazard_indicators)
        self.assertEqual([c.key for c in match.also_matched], ["general_maintenance"])

    def test_confirmed_safety_category_still_wins(self):
        self.assertEqual(category_of("Electrical outlet sparking"), ["electrical_hazard"])

    def test_possible_hazard_labeled_low_is_critical_review(self):
        flags = evaluate(request(issue="Burning odor near dryer", priority="Low"), NOW, CONFIG, CATEGORIES)
        self.assertIn(("NEEDS_HUMAN_REVIEW", "critical"), {(f.rule, f.severity) for f in flags})
        self.assertNotIn("PRIORITY_MISMATCH", {f.rule for f in flags})

    def test_hazard_review_never_challenges_emergency_label(self):
        row = emergency(issue="Water pouring through ceiling light fixture")
        flags = evaluate(row, NOW, CONFIG, CATEGORIES)
        self.assertNotIn("PRIORITY_MISMATCH", {f.rule for f in flags})
        self.assertIn(("NEEDS_HUMAN_REVIEW", "high"), {(f.rule, f.severity) for f in flags})
        self.assertEqual(row["priority"], "Emergency")

    def test_hazard_review_withholds_routine_standard_action(self):
        match = classify("Water pouring through ceiling light fixture", CATEGORIES)
        text = "\n".join(category_lines(match, "Emergency"))
        self.assertIn("possible hazard", text)
        self.assertNotIn("Standard action", text)

    def test_hazard_exclusion(self):
        self.assertEqual(category_of("Smoke detector chirping"), ["general_maintenance"])

    def test_plural_forms_match(self):
        self.assertEqual(category_of("Rotten eggs smell in hallway"), ["gas"])
        self.assertEqual(category_of("Broken pipes under sink"), ["plumbing"])
        self.assertEqual(category_of("Kitchen cabinets falling apart"), ["general_maintenance"])


class FinancialReviewTests(unittest.TestCase):
    def completed(self, **overrides):
        fields = {"status": "Completed", "estimated_cost": "4000", "cost_approved": ""}
        fields.update(overrides)
        return request(**fields)

    def test_completed_unapproved_high_cost_is_flagged_separately(self):
        row = self.completed()
        self.assertEqual(evaluate(row, NOW, CONFIG, CATEGORIES), [])  # not in the maintenance queue
        review = build_financial_review([row], CONFIG)
        self.assertEqual([f.rule for _, f in review], ["FINANCIAL_REVIEW"])

    def test_explicitly_not_approved_is_flagged(self):
        self.assertEqual(len(build_financial_review([self.completed(cost_approved="No")], CONFIG)), 1)

    def test_approved_low_cost_or_open_work_is_not_flagged(self):
        rows = [self.completed(cost_approved="Yes"), self.completed(estimated_cost="900"),
                request(estimated_cost="4000")]
        self.assertEqual(build_financial_review(rows, CONFIG), [])

    def test_report_prints_financial_section_separately(self):
        out = io.StringIO()
        row = self.completed()
        print_report([row], [], NOW, CATEGORIES, build_financial_review([row], CONFIG), out=out)
        text = out.getvalue()
        self.assertIn("Financial review: completed work (1)", text)
        self.assertIn("not active maintenance issues", text)


class ApplianceWordingTests(unittest.TestCase):
    def test_refrigerator_guidance_only_for_refrigerators(self):
        dishwasher = "\n".join(category_lines(classify("Dishwasher door latch broken", CATEGORIES), "Medium"))
        fridge = "\n".join(category_lines(classify("Refrigerator not cooling", CATEGORIES), "High"))
        self.assertNotIn("food loss", dishwasher)
        self.assertIn("food loss", fridge)


class ChallengeCaseTests(unittest.TestCase):
    """The eight V1 challenge cases in challenge_cases.csv (see CHALLENGE_TESTS.md)."""

    @classmethod
    def setUpClass(cls):
        with open(Path(__file__).with_name("challenge_cases.csv"), newline="") as fh:
            cls.rows = {r["request_id"]: r for r in csv.DictReader(fh)}
        cls.flags = {rid: {(f.rule, f.severity) for f in evaluate(r, NOW, CONFIG, CATEGORIES)}
                     for rid, r in cls.rows.items()}
        cls.financial = {r["request_id"] for r, _ in build_financial_review(cls.rows.values(), CONFIG)}

    def test_ch01_emergency_no_vendor_possible_water_electrical_hazard(self):
        f = self.flags["CH-01"]
        self.assertTrue({("NO_CONFIRMED_DISPATCH", "critical"), ("NO_RESPONSE", "critical"),
                         ("NEEDS_HUMAN_REVIEW", "high")} <= f)
        self.assertNotIn("PRIORITY_MISMATCH", {rule for rule, _ in f})

    def test_ch02_emergency_actively_worked_is_not_flagged(self):
        self.assertEqual(self.flags["CH-02"], set())

    def test_ch03_routine_labeled_emergency(self):
        self.assertEqual(self.flags["CH-03"], {("NO_RESPONSE", "critical"), ("PRIORITY_MISMATCH", "medium")})

    def test_ch04_possible_gas_leak_labeled_low(self):
        self.assertEqual(category_of(self.rows["CH-04"]["issue"]), ["gas"])
        self.assertIn(("PRIORITY_MISMATCH", "critical"), self.flags["CH-04"])

    def test_ch05_unfamiliar_issue(self):
        self.assertEqual(self.flags["CH-05"], {("NEEDS_HUMAN_REVIEW", "medium")})

    def test_ch06_completed_high_cost_goes_to_financial_review_only(self):
        self.assertEqual(self.flags["CH-06"], set())
        self.assertEqual(self.financial, {"CH-06"})

    def test_ch07_missing_critical_data(self):
        self.assertEqual(self.flags["CH-07"], {("MISSING_INFO", "high"), ("MISSING_INFO", "medium"),
                                                ("NEEDS_HUMAN_REVIEW", "medium")})

    def test_ch08_valid_hold_pauses_time_limit_not_resident_updates(self):
        self.assertEqual(self.flags["CH-08"], {("RESIDENT_UPDATE_OVERDUE", "low")})


class TodaysExceptionsTests(unittest.TestCase):
    """The concise view is presentation only, on top of the unchanged V1 queue."""

    @classmethod
    def setUpClass(cls):
        with open(Path(__file__).with_name("maintenance_requests.csv"), newline="") as fh:
            cls.rows = list(csv.DictReader(fh))
        cls.queue = build_queue(cls.rows, NOW, CONFIG, CATEGORIES)
        cls.text = render_today(cls.queue, NOW)
        cls.lines = {row["request_id"]: exception_line(row, flags, NOW) for row, flags in cls.queue}

    def test_header_counts_match_v1_severities(self):
        self.assertIn("20 requests need attention   🔴 11 Critical   🟠 7 High   🟡 2 Medium", self.text)

    def test_marks_data_as_synthetic(self):
        self.assertIn("SYNTHETIC DATA", self.text)

    def test_one_line_per_exception_in_v1_order(self):
        ids = [line.split(" ")[1] for line in self.text.splitlines() if line[:1] in "🔴🟠🟡⚪" and " · " in line]
        self.assertEqual(ids, [row["request_id"] for row, _ in self.queue])

    def test_only_unresolved_exceptions_are_listed(self):
        for request_id in ("MR-1010", "MR-1018", "MR-1002", "MR-1008", "MR-1014"):
            self.assertNotIn(request_id, self.text)

    def test_headline_is_the_most_useful_reason(self):
        self.assertIn("CoolAir HVAC hasn't confirmed", self.lines["MR-1022"])
        self.assertIn("No vendor assigned", self.lines["MR-1004"])
        self.assertIn("Possible safety issue labeled Low", self.lines["MR-1023"])
        self.assertIn("Work stalled — last progress 23 hrs ago", self.lines["MR-1016"])
        self.assertIn("Missing: unit", self.lines["MR-1005"])
        self.assertIn("open time unknown", self.lines["MR-1013"])

    def test_headline_is_one_of_the_v1_flags_at_top_severity(self):
        for _, flags in self.queue:
            chosen = headline_flag(flags)
            self.assertIn(chosen, flags)
            self.assertEqual(chosen.severity, flags[0].severity)

    def test_other_reasons_are_counted_not_hidden(self):
        self.assertTrue(self.lines["MR-1022"].endswith("(+3 more)"))
        self.assertIn("--details", self.text)

    def test_view_does_not_change_v1_results(self):
        again = build_queue(self.rows, NOW, CONFIG, CATEGORIES)
        self.assertEqual([(r["request_id"], f) for r, f in again], [(r["request_id"], f) for r, f in self.queue])


class NoExternalActionsTests(unittest.TestCase):
    """The prototype only surfaces issues; it must not contact anyone or anything."""

    NETWORK_MODULES = {"socket", "urllib", "http", "requests", "smtplib", "email", "ftplib",
                       "telnetlib", "subprocess", "webbrowser", "xmlrpc"}

    def test_no_network_or_process_imports(self):
        for name in ("exception_monitor.py", "issue_categories.py", "todays_exceptions.py"):
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
