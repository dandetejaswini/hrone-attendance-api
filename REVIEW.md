# REVIEW.md — Starter Code Defect Review (Part A)

This document details the genuine defects identified in the starter implementation (`app/main.py`), their technical impact, concrete observable symptoms, and the corrections implemented to satisfy the assignment contract.

---

## Starter Code Defects & Resolutions

### 1. Health Check False Positive
- **Location:** `health()`, lines 72–74 of starter code.
- **What is wrong & why it matters:** The endpoint returned `{"status": "ok"}` unconditionally without checking MongoDB connectivity, masking database outages from evaluators and container health probes.
- **Observable symptom:** Stopping the database or supplying an invalid connection string still resulted in `HTTP 200 {"status": "ok"}`.
- **Correction:** Added `client.admin.command("ping")` and `ensure_indexes()`. Returns `HTTP 503 Service Unavailable` with `{"status": "error", "database": "disconnected"}` when MongoDB is unreachable.

### 2. Off-by-One Pagination Offset in Employee Listing
- **Location:** `list_employees()`, line 93 of starter code.
- **What is wrong & why it matters:** Calculated skip offset as `skip = page * page_size`. For `page=1` and `page_size=20`, it skipped the first 20 records, making page 1 permanently unreachable.
- **Observable symptom:** Querying `GET /employees?page=1&page_size=10` omitted the first 10 employees in the database.
- **Correction:** Corrected offset calculation to `skip = (page - 1) * page_size`.

### 3. Filtered Total Ignored Department Filter
- **Location:** `list_employees()`, line 94 of starter code.
- **What is wrong & why it matters:** Calculated total count using `count_documents({})`, returning the total company headcount instead of matching department records, violating Rule R10.
- **Observable symptom:** Querying `GET /employees?department=Engineering` returned `total = 6` despite only 2 engineering employees existing.
- **Correction:** Updated count query to `count_documents(q)` matching the applied filter criteria.

### 4. Missing Employee Sort Order
- **Location:** `list_employees()`, line 95 of starter code.
- **What is wrong & why it matters:** The MongoDB cursor omitted `.sort()`, returning employees in non-deterministic natural storage order across pagination boundaries.
- **Observable symptom:** Inserting documents out of order caused paginated results to shift non-deterministically across pages.
- **Correction:** Added explicit database sorting: `.sort("emp_code", ASCENDING)`.

### 5. Naive Local Datetimes in Punch-In
- **Location:** `punch_in()`, line 102 of starter code.
- **What is wrong & why it matters:** Used `datetime.fromtimestamp(body.punched_at / 1000)` without timezone awareness. Punch timestamps shifted according to the host machine's local timezone, breaking Rule R1 business-date resolution.
- **Observable symptom:** An identical UTC timestamp evaluated to different attendance dates on machines in different timezones (e.g. UTC vs EST vs IST).
- **Correction:** Converted timestamps using `datetime.fromtimestamp(..., tz=timezone.utc)` and truncated fractional seconds per Rule R1.

### 6. Unhandled Missing Employee Crash
- **Location:** `punch_in()`, lines 101–113 of starter code.
- **What is wrong & why it matters:** Looked up `db.employees.find_one(...)` without checking for `None`. Referencing `emp["shift_start"]` raised an unhandled `TypeError: 'NoneType' object is not subscriptable`, causing an HTTP 500 error.
- **Observable symptom:** Punch-in for a non-existent `emp_code` returned `HTTP 500 Internal Server Error`.
- **Correction:** Added explicit check returning `HTTP 404 Not Found` (`{"detail": "employee not found"}`) if the employee does not exist.

### 7. Attendance Date Calculation Ignored Overnight Shifts
- **Location:** `punch_in()`, line 103 of starter code.
- **What is wrong & why it matters:** Extracted `date` directly from the local timestamp date, ignoring overnight shift attribution required by Rule R1 (`shift_end <= shift_start`).
- **Observable symptom:** An employee with a `22:00–06:00` shift punching in at `01:30 IST` on October 14 was assigned date `2026-10-14` instead of the shift's starting date `2026-10-13`.
- **Correction:** Implemented `attendance_date()`: converts UTC punch time to IST (`Asia/Kolkata`) and attributes early-morning punches before `shift_end` to the previous calendar day.

### 8. Check-Then-Insert Race Condition on Punch-In
- **Location:** `punch_in()`, lines 104–118 of starter code.
- **What is wrong & why it matters:** Used an application-level `find_one()` check before `insert_one()`. Concurrent requests for the same employee passed the read check simultaneously, creating duplicate attendance records for the same day.
- **Observable symptom:** Parallel HTTP requests with identical `(emp_code, punched_at)` created duplicate records in `attendance_logs`.
- **Correction:** Enforced a unique compound index `{"emp_code": 1, "date": 1}` in MongoDB. The API executes an atomic `insert_one` and catches `DuplicateKeyError` to return `HTTP 409 Conflict`.

### 9. In-Memory Collection Loading in Attendance Listing
- **Location:** `list_attendance()`, lines 144–146 of starter code.
- **What is wrong & why it matters:** Loaded all matching records into memory via `list(db.attendance_logs.find(q))` before sorting and slicing in Python, creating severe memory and latency bottlenecks on large datasets.
- **Observable symptom:** Database execution plan showed `COLLSCAN` and memory usage scaled linearly with collection size.
- **Correction:** Delegated sorting, skipping, and limiting directly to the MongoDB cursor: `.sort(...).skip(skip).limit(limit)`.

### 10. Incomplete Attendance Sorting
- **Location:** `list_attendance()`, line 145 of starter code.
- **What is wrong & why it matters:** Python sort used `date` only. When multiple employees had records on the same date, intraday ordering was undefined.
- **Observable symptom:** Paginated pages showed non-deterministic ordering of employees within the same date.
- **Correction:** Added compound index-backed sort: `[("date", DESCENDING), ("emp_code", ASCENDING)]`.

### 11. Attendance Total Count Derived from In-Memory Slice
- **Location:** `list_attendance()`, line 146 of starter code.
- **What is wrong & why it matters:** `total = len(docs)` derived count from the in-memory array rather than executing a collection count query.
- **Observable symptom:** Inconsistent reporting when filters and limits interacted.
- **Correction:** Used `db.attendance_logs.count_documents(q)`.

### 12. Strict Late-Arrival Grace Period Calculation
- **Location:** `compute_late_minutes()`, lines 33–37 of starter code.
- **What is wrong & why it matters:** Floored elapsed minutes before evaluating `minutes > 10`. Under Rule R2, the grace period is strictly 10 minutes (600 seconds). An arrival at 10m 01s (601 seconds) floored to 10 minutes, causing `10 > 10` to evaluate to `False` and incorrectly waiving the late penalty.
- **Observable symptom:** Arrival at `09:25:01 IST` for a `09:15:00 IST` shift returned `0` late minutes instead of `10`.
- **Correction:** Evaluated raw elapsed seconds: `delta_s = int(total_seconds)`. If `delta_s > 600`, returned `delta_s // 60`; otherwise `0`.

### 13. Overtime Shift-End Calendar Date on Overnight Shifts
- **Location:** `compute_overtime()`, lines 44–47 of starter code.
- **What is wrong & why it matters:** Built shift-end datetime using `att_date` directly without advancing the calendar day for overnight shifts (`shift_end <= shift_start`).
- **Observable symptom:** For a `22:00–06:00` overnight shift, shift end was constructed as `06:00` on the start date (16 hours before shift start), resulting in negative or wildly distorted overtime calculations.
- **Correction:** Advanced the shift-end calendar date by +1 day whenever `shift_end <= shift_start`.

### 14. Banker's Rounding in Work Hours Calculation
- **Location:** `compute_work_hours()`, line 41 of starter code.
- **What is wrong & why it matters:** Used Python's built-in `round()`, which applies round-half-to-even (banker's rounding). Rule R4 strictly specifies 2-decimal symmetric half-up rounding.
- **Observable symptom:** Exact halfway values rounded downward to even numbers (e.g. `8.125` rounded to `8.12` instead of `8.13`).
- **Correction:** Implemented symmetric half-up rounding via `Decimal(str(...)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)`.

### 15. Naive Local Datetimes on Employee Creation
- **Location:** `create_employee()`, line 82 of starter code.
- **What is wrong & why it matters:** Recorded `created_at` using naive local `datetime.now()`.
- **Observable symptom:** Stored employee records contained timezone-ambiguous creation timestamps.
- **Correction:** Standardized to UTC: `datetime.now(timezone.utc).replace(microsecond=0)`.

### 16. Python Datetime Objects Leaking in API Responses
- **Location:** `create_employee()`, lines 81–85 of starter code.
- **What is wrong & why it matters:** Returned raw dictionary containing a Python `datetime` object, serializing to an ISO string instead of integer epoch milliseconds specified in the OpenAPI contract.
- **Observable symptom:** `POST /employees` response returned `"created_at": "2026-10-14T..."` instead of an integer like `1770960000000`.
- **Correction:** Added `_dt_to_ms()` conversion helper in the response serialization pipeline.

### 17. Spurious `id` Field Injected into Attendance Records
- **Location:** `list_attendance()`, line 149 of starter code.
- **What is wrong & why it matters:** Executed `d["id"] = str(d.pop("_id"))`, injecting an extraneous `id` field. The contract defines attendance records as identified solely by `(emp_code, date)`.
- **Observable symptom:** Attendance objects contained `"id": "670..."`, violating schema validation.
- **Correction:** Projected out `_id` at the database level (`{"_id": 0}`) and removed the dictionary modification.

### 18. Absence of Database Indexes at Startup
- **Location:** Application startup / module scope.
- **What is wrong & why it matters:** No indexes were defined or created, causing queries to perform full collection scans (`COLLSCAN`) and leaving unique constraints unenforced.
- **Observable symptom:** Query execution plans reported `COLLSCAN` and concurrent requests permitted duplicate employee codes and daily punches.
- **Correction:** Implemented idempotent `ensure_indexes()` creating compound and unique indexes on application startup and health check probes.

### 19. Unvalidated Epoch-Millisecond Timestamps
- **Location:** `PunchInIn` / `PunchOutIn` schemas.
- **What is wrong & why it matters:** Accepted arbitrary integers without range bounds, allowing seconds-era timestamps or negative values to corrupt attendance dates.
- **Observable symptom:** Punching in with `1718000000` (seconds instead of milliseconds) produced invalid dates in year 1970.
- **Correction:** Introduced `EpochMillis` type validating integer range between `100_000_000_000` and `4_102_444_800_000` (`HTTP 422` on violation).

### 20. Permitted Zero-Duration Shifts
- **Location:** `EmployeeIn` schema.
- **What is wrong & why it matters:** Permitted identical `shift_start` and `shift_end` times (e.g. `09:00` to `09:00`).
- **Observable symptom:** An employee could be registered with zero shift duration, causing divide-by-zero or undefined behavior in overtime calculations.
- **Correction:** Added model validator rejecting identical shift start and end times with `HTTP 422 Unprocessable Entity`.

### 21. Unvalidated Employee Code Format
- **Location:** `EmployeeIn` schema.
- **What is wrong & why it matters:** Accepted arbitrary string identifiers, allowing malformed codes to enter the database.
- **Observable symptom:** Employee codes like `emp-123` or empty strings were accepted with HTTP 201.
- **Correction:** Added regex pattern validation matching `^EMP\d{4,6}$`.

### 22. Unvalidated Inverted Date Ranges
- **Location:** `list_attendance()`.
- **What is wrong & why it matters:** Allowed `date_from > date_to` without validation, silently returning empty arrays instead of rejecting invalid queries.
- **Observable symptom:** `GET /attendance?date_from=2026-07-10&date_to=2026-07-01` returned HTTP 200 with empty items.
- **Correction:** Added query validation raising `HTTP 422` when `date_from > date_to`.

### 23. Uncapped Pagination Limits
- **Location:** `list_employees()`, `list_attendance()`.
- **What is wrong & why it matters:** Permitted arbitrarily large `page_size`, creating denial-of-service vulnerabilities through large memory allocations.
- **Observable symptom:** Requests with `page_size=1000000` attempted to read and serialize huge document sets.
- **Correction:** Enforced `Query(ge=1, le=100)` on all pagination parameters.

### 24. Raw BSON Types Leaking in Responses
- **Location:** Explain and record serialization helpers.
- **What is wrong & why it matters:** Unhandled BSON types (`ObjectId`, `Decimal128`, `Timestamp`) raised runtime `TypeError` during FastAPI JSON encoding.
- **Observable symptom:** Atlas explain plan queries failed with serialization errors when cluster metadata returned `bson.Timestamp`.
- **Correction:** Implemented recursive `_serialise_bson_value()` converting BSON types to standard JSON-serializable primitives.

---

## Areas Inspected and Confirmed Correct

1. **Attendance Status Enum vs Half-Day Boolean:**
   - Evaluated whether `HALF_DAY` is an attendance status.
   - Confirmed allowed attendance statuses are strictly `["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]`.
   - Per Rule R5, `half_day` is an independent Boolean flag (`work_hours < 4.50`). Punch-out derives `work_hours`, `overtime_minutes`, and `half_day: bool` without altering the employee's existing status.

2. **Analytics Aggregation Consumes Stored Derived Fields:**
   - Evaluated whether analytics pipelines should re-derive work hours and late minutes from raw punch timestamps or use stored document fields.
   - Confirmed analytics must aggregate stored document values (`late_minutes`, `work_hours`, `half_day`, `status`). When records are modified via `PATCH /attendance/{emp_code}/{date}`, managers may legitimately regularize status or waive late minutes. Re-computing from punch timestamps would overwrite managerial adjustments and destroy the audit trail.

3. **Standard Competition Ranking (1224 Ranking):**
   - Evaluated whether leaderboard endpoints should arbitrarily truncate tied candidates at the `limit` boundary.
   - Confirmed standard competition ranking (`$rank`) returns all candidates satisfying `rank <= limit`. If a tie occurs at rank 2 for `limit=2`, returning ranks 1, 2, and 2 preserves ranking integrity.

---

## Verification Summary

- **Automated Tests:** All 4 local test suites (`test_operations.py`, `test_phase2.py`, `test_analytics.py`, `test_explain.py`) pass cleanly (exit code 0).
- **MongoDB Atlas v8.0.34:** Verified against a live Atlas cluster:
  - Connectivity probe confirmed via `GET /health`.
  - Unique index enforcement confirmed on `(emp_code, date)` and `emp_code`.
  - Query execution stats inspected via `/admin/explain/{endpoint}`, confirming `IXSCAN` index utilization and zero unindexed collection scans (`COLLSCAN`).
