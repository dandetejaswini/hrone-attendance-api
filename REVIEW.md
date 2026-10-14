# REVIEW.md — Starter Code Defect Review (Part A)

This document catalogues all 24 defects identified and resolved in the starter implementation (`app/main.py`), evaluated against the official HROne assignment specification, OpenAPI contract, and database requirements.

---

## Defect Inventory

| ID | Location | Defect and impact | Correction | Verification |
|---|---|---|---|---|
| **D-01** | `health()`, lines 72–74 | Returned `{"status": "ok"}` unconditionally without checking MongoDB connectivity, masking database outages from evaluators and health probes. | Added `client.admin.command("ping")` and `ensure_indexes()`. Returns `HTTP 503` with `{"status": "error", "database": "disconnected"}` when MongoDB is unreachable. | `tests/test_operations.py` (Section 1) and live Atlas probe |
| **D-02** | `list_employees()`, line 93 | Calculated skip offset as `skip = page * page_size`. For `page=1, page_size=20`, skipped the first 20 records, making page 1 permanently unreachable. | Corrected offset calculation to `skip = (page - 1) * page_size`. | `tests/test_operations.py` (Section 3) |
| **D-03** | `list_employees()`, line 94 | Total count used `count_documents({})`, returning company-wide headcount instead of matching department records (e.g., returned `total=6` instead of `2` when filtering Engineering). | Updated count query to `count_documents(q)` matching the applied filter criteria per Rule R10. | `tests/test_operations.py` (Section 3) |
| **D-04** | `list_employees()`, line 95 | Cursor omitted `.sort()`, returning employees in non-deterministic natural storage order across pagination boundaries. | Added deterministic database sorting: `.sort("emp_code", ASCENDING)`. | `tests/test_operations.py` (Section 3) |
| **D-05** | `punch_in()`, line 102 | Used `datetime.fromtimestamp(body.punched_at / 1000)` without timezone info. Punch timestamps shifted based on host machine's local timezone, breaking Rule R1 business-date resolution. | Converted timestamps using `datetime.fromtimestamp(..., tz=timezone.utc)` and truncated fractional seconds per Rule R1. | `tests/test_operations.py` (Section 4) |
| **D-06** | `punch_in()`, lines 101–113 | Looked up `db.employees.find_one(...)` without checking for `None`. Referencing `emp["shift_start"]` on missing employee raised an unhandled `TypeError` (HTTP 500). | Added explicit check returning `HTTP 404 Not Found` (`{"detail": "employee not found"}`) if employee does not exist. | `tests/test_operations.py` (Section 4) |
| **D-07** | `punch_in()`, line 103 | Assigned attendance date directly from local timestamp date, ignoring Rule R1 overnight shifts (`shift_end <= shift_start`). E.g., a `22:00–06:00` shift punch at `01:30 IST` on Oct 14 was assigned `2026-10-14` instead of `2026-10-13`. | Implemented `attendance_date()`: converts UTC punch time to IST (`Asia/Kolkata`) and attributes early-morning punches before `shift_end` to the previous calendar day. | `tests/test_operations.py` (Section 4) |
| **D-08** | `punch_in()`, lines 104–118 | Check-then-insert race condition: used application `find_one()` prior to `insert_one()`. Concurrent requests passed the read check simultaneously, creating duplicate punches for the same date. | Enforced unique compound index `{"emp_code": 1, "date": 1}`. Atomic `insert_one` catches `DuplicateKeyError` and returns `HTTP 409 Conflict`. | `tests/test_phase2.py` (concurrency suite) and live Atlas test |
| **D-09** | `list_attendance()`, lines 144–146 | Loaded all matching records into memory via `list(db.attendance_logs.find(q))` before sorting and slicing in Python, creating memory and latency exhaustion on large collections. | Delegated sorting, skipping, and limiting directly to the MongoDB cursor: `.sort(...).skip(skip).limit(limit)`. | `tests/test_operations.py` (Section 5) |
| **D-10** | `list_attendance()`, line 145 | Python sort used `date` only. Intraday ordering among employees on the same date was undefined across pagination boundaries. | Added compound index-backed sort: `[("date", DESCENDING), ("emp_code", ASCENDING)]`. | `tests/test_operations.py` (Section 5) |
| **D-11** | `list_attendance()`, line 146 | `total = len(docs)` derived count from in-memory slice length rather than executing a collection count query. | Used `db.attendance_logs.count_documents(q)`. | `tests/test_operations.py` (Section 5) |
| **D-12** | `compute_late_minutes()`, lines 33–37 | Floored elapsed minutes before evaluating `minutes > 10`. Under Rule R2, grace period is strictly 10m 00s (600s). Arrival at 10m 01s (601s, e.g. 09:25:01 for 09:15 shift) floored to 10m; `10 > 10` evaluated to `False`, waiving late penalty incorrectly. | Evaluated raw elapsed seconds: `delta_s = int(total_seconds)`. If `delta_s > 600`, returns `delta_s // 60`; otherwise `0`. | `tests/test_operations.py` (Section 2) |
| **D-13** | `compute_overtime()`, lines 44–47 | Built shift-end datetime on `att_date` directly without advancing the calendar day for overnight shifts (`shift_end <= shift_start`). E.g. for `22:00–06:00`, constructed `06:00` on the start date (16h before shift start), distorting overtime. | Advanced shift-end calendar date by +1 day whenever `shift_end <= shift_start`. | `tests/test_operations.py` (Section 2) |
| **D-14** | `compute_work_hours()`, line 41 | Used Python built-in `round()`, which applies banker's rounding (round-half-to-even). Exact halfway fractions rounded downward to even digits (e.g. `8.125` rounded to `8.12` instead of `8.13`). Rule R4 requires symmetric half-up. | Implemented symmetric half-up rounding via `Decimal(str(...)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)`. | `tests/test_operations.py` (Section 2) and `tests/test_phase2.py` |
| **D-15** | `create_employee()`, line 82 | Stored `created_at` with naive local `datetime.now()`, creating timezone-ambiguous creation timestamps. | Standardized to UTC: `datetime.now(timezone.utc).replace(microsecond=0)`. | `tests/test_operations.py` (Section 3) |
| **D-16** | `create_employee()`, lines 81–85 | Returned raw dict containing a Python `datetime` object, serializing to an ISO string instead of integer epoch milliseconds specified in the OpenAPI contract. | Added `_dt_to_ms()` conversion helper in the response serialization pipeline. | `tests/test_operations.py` (Section 3) |
| **D-17** | `list_attendance()`, line 149 | Executed `d["id"] = str(d.pop("_id"))`, injecting an extraneous `id` field. The contract defines attendance records as identified solely by `(emp_code, date)`. | Projected out `_id` at database level (`{"_id": 0}`) and removed field injection. | `tests/test_operations.py` (Section 5) |
| **D-18** | Application startup / module scope | No indexes created, causing queries to trigger collection scans (`COLLSCAN`) and leaving uniqueness unenforced. | Added `ensure_indexes()` creating unique indexes `emp_code_unique` and `empcode_date_unique`, plus compound sorting/analytics indexes on startup and `/health` probe. | `tests/test_operations.py` (Section 1) and live Atlas explain stats |
| **D-19** | `PunchInIn` / `PunchOutIn` schemas | Accepted arbitrary integers without range bounds, allowing seconds-era timestamps (e.g. `1718000000`) or negative numbers to corrupt attendance dates. | Introduced `EpochMillis` type validating integer range between `100_000_000_000` and `4_102_444_800_000` (returns `HTTP 422` on violation). | `tests/test_operations.py` (Section 4) |
| **D-20** | `EmployeeIn` schema | Permitted identical `shift_start` and `shift_end` times (e.g. `09:00` to `09:00`), leading to zero-duration shifts and potential divide-by-zero errors. | Added model validator rejecting identical shift times with `HTTP 422 Unprocessable Entity`. | `tests/test_operations.py` (Section 3) |
| **D-21** | `EmployeeIn` schema | Accepted arbitrary employee code string formats (e.g. `emp-123` or empty string). | Added regex pattern validation matching `^EMP\d{4,6}$` (returns `HTTP 422` on violation). | `tests/test_operations.py` (Section 3) |
| **D-22** | `list_attendance()` | Allowed `date_from > date_to` without validation, silently returning empty arrays instead of rejecting invalid date boundaries. | Added query parameter validation raising `HTTP 422` when `date_from > date_to`. | `tests/test_operations.py` (Section 5) |
| **D-23** | `list_employees()`, `list_attendance()` | Permitted arbitrarily large `page_size` (e.g. `1000000`), creating denial-of-service vulnerability via memory allocation. | Enforced `Query(ge=1, le=100)` constraints on all pagination parameters. | `tests/test_operations.py` (Sections 3 & 5) |
| **D-24** | Explain and record serializers | Unhandled BSON types (`ObjectId`, `Decimal128`, `Timestamp`) raised runtime `TypeError` during FastAPI JSON encoding (e.g. Atlas cluster metadata containing `bson.Timestamp`). | Implemented recursive `_serialise_bson_value()` converting BSON types to standard JSON-serializable primitives. | `tests/test_explain.py` (Section 4) and live Atlas explain test |

---

## Confirmed Contract Behaviors

1. **Attendance Status Enum vs Half-Day Boolean:**
   - Allowed attendance statuses are strictly `["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]`. There is no `HALF_DAY` status enum.
   - Per Rule R5, `half_day` is an independent Boolean flag (`work_hours < 4.50`). Punch-out computes derived work hours, overtime, and `half_day: bool` without altering the employee's existing status.

2. **Analytics Consumes Stored Derived Fields:**
   - Analytics pipelines aggregate stored fields (`late_minutes`, `work_hours`, `half_day`, `status`) rather than re-computing from punch timestamps.
   - When records are regularized via `PATCH /attendance/{emp_code}/{date}`, managers may legitimately override status or adjust late minutes. Aggregating stored values preserves approved managerial adjustments and maintains the audit trail.

3. **Standard Competition Ranking (1224 Ranking) Ties:**
   - The late leaderboard preserves all tied candidates satisfying `rank <= limit`. If a tie occurs at rank 2 for `limit=2`, returning ranks 1, 2, and 2 maintains fairness without arbitrary truncation at cutoff boundaries.

---

## Verification Summary

- **Automated Tests:** All 4 test suites (`test_operations.py`, `test_phase2.py`, `test_analytics.py`, `test_explain.py`) pass with exit code 0.
- **MongoDB Atlas v8.0.34:** Verified live against Atlas cluster:
  - Unique index enforcement confirmed on `(emp_code, date)` and `emp_code`.
  - Query execution stats via `/admin/explain/{endpoint}` confirmed `IXSCAN` index utilization and zero unindexed collection scans (`COLLSCAN`).
