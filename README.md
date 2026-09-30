# Maintenance Exception Monitor (V1 prototype)

> **Synthetic data and prototype assumptions.** Every maintenance request in this repo is fictional. All thresholds, issue categories and safety procedures are assumptions I made for this prototype. They are **not Lodg policies**, and the escalation procedures must be replaced with company-approved procedures, contacts and jurisdiction-specific requirements before any real-world use.

## The operational problem

A property operations team manages a large queue of open maintenance requests across many buildings. The costly failures hide inside that queue: an emergency nobody dispatched, a vendor who never arrived, a resident who hasn't heard anything in days, a $5,000 repair nobody approved, or a gas smell logged as Low priority. Finding them by hand means rereading every ticket, every day.

## What the prototype does

It reads a CSV of maintenance requests, checks each one against a set of escalation rules, and produces a **ranked review queue**. For each flagged request, the queue shows the reason and a recommended next action.

It **surfaces and prioritizes; it never acts.** It doesn't contact vendors, residents or emergency services, change priorities or approve costs.

On the 25 sample requests, which are deliberately messy, it flags 20. The four emergencies nobody has responded to come first, followed by a carbon monoxide alarm mislabeled as Low.

```
python3 exception_monitor.py --as-of "2026-09-30 09:00"   # sample data is built around this time
python3 -m unittest test_exception_monitor                # 85 tests
```

Python 3.11+, standard library only.

## How the workflow works

1. **Load settings:** thresholds come from `monitor_config.toml`, and categories and procedures from `issue_categories.toml`. Both can be edited without touching code.
2. **Categorize each issue** using fixed keywords, in this order: confirmed safety category → hazard screen → human-review category → routine category. Anything unclear goes to a person.
3. **Apply the escalation rules** to every unresolved request.
4. **Rank** by severity (critical → low), then priority, then age.
5. **Report:** each request gets its reasons and next actions, with a safety procedure or standard action where one applies. Unapproved high-cost work that's already completed goes in a separate financial-review section.

## Escalation rules (prototype defaults)

| Check | Flags a request when |
|---|---|
| Dispatch | An Emergency has no confirmed vendor after 15 minutes (4 hours for High) |
| Emergency progress | No response within 1 hour, work stalled for 4 hours, or active work but still unresolved after 24 hours (review only) |
| Time limits | Unresolved past 24 hours (High), 48 hours (Medium) or 7 days (Low). A valid hold (reason and end date) or a future appointment pauses the clock |
| Resident updates | No update within 2 hours (Emergency), 24 hours (High), 48 hours (Medium) or 7 days (Low) |
| Cost | Estimate over $1,000 and not approved |
| Missing data | Blank, placeholder ("TBD") or invalid priority, issue, date, property or unit |
| Priority mismatch | A safety issue is labeled below its minimum priority, or a routine issue above its usual maximum |
| Financial review | Completed work over $1,000 without recorded approval (reported separately) |

## Human-review guardrails

- **Read-only.** It changes no data and makes no network calls; a test enforces the no-network rule. The report states that nothing has been acted on.
- **Priorities are never changed.** Mismatches are flagged for a supervisor. An assigned Emergency is always handled as an Emergency.
- **Safety procedures are predefined, never generated.** The six safety-critical categories are gas or carbon monoxide, fire, flooding, electrical, elevator and structural damage. Each shows a fixed procedure that can be reviewed in advance.
- **Hazard words override routine matches.** If an issue mentions a danger signal (smell, smoke, sparking, pouring water, sagging and similar) but no safety category is confirmed, it goes to human review rather than being treated as routine.
- **No guessing.** Mold, pests, unfamiliar or ambiguous issues go to human review. There is no AI model in the decision path.
- **Actions match reality.** It never tells an operator to contact a vendor that isn't assigned.

## Assumptions

- **The CSV is a current snapshot.** Timestamps are local time, written `YYYY-MM-DD HH:MM`.
- **The source system records these fields:** vendor confirmation, last progress, cost approval, reporter, scheduled date, and hold reason and end date. A real system may need mapping.
- **Status:** "Completed" means resolved; every other status is unresolved.
- **Staff-reported requests** don't require resident updates.
- **Thresholds, categories and procedures** are prototype placeholders, not Lodg policy.

## Current limitations

- **Keyword classification.** Only listed phrases (and simple plurals) are recognized, so unfamiliar wording can be missed. The hazard screen reduces this risk and deliberately leans toward false alarms.
- **Snapshot only.** Each run starts fresh, with no memory, so repeat alerts aren't suppressed. There is no duplicate or repeat-problem detection.
- **CSV input only,** with no system integration and a strict date format.
- **Tested only on synthetic data:** 25 sample requests plus 8 adversarial cases. One adversarial case caught a real failure: a probable gas leak labeled Low was confidently misclassified as plumbing. It was fixed and is now a regression test ([`CHALLENGE_TESTS.md`](CHALLENGE_TESTS.md)).

## What I would build next

1. **Validate with the operations team:** real thresholds, safety procedures reviewed by safety and legal, and a trial on a week of anonymized real tickets.
2. **Read-only integration** with the work-order system, plus a daily digest to the ops channel. People still take every action.
3. **Alert state:** acknowledge and snooze flags, suppress repeats, and measure time-to-resolution for flagged items.
4. **Duplicate and repeat-problem detection:** the same unit and issue reported again.
5. **AI-assisted categorization, as suggestions a person confirms,** for issues the keywords miss, measured against the challenge suite. Safety procedures would stay predefined.

## Files

| File | Purpose |
|---|---|
| `exception_monitor.py` | Escalation rules, ranking and report |
| `issue_categories.py` / `.toml` | Hazard screen, issue categories, procedures and standard actions |
| `monitor_config.toml` | All time and cost thresholds |
| `maintenance_requests.csv` | 25 synthetic sample requests |
| `challenge_cases.csv`, `CHALLENGE_TESTS.md` | 8 adversarial cases, with predictions written before running and the results |
| `test_exception_monitor.py` | 85 automated tests |
| `docs/technical_reference.md` | Full rule and configuration reference |
