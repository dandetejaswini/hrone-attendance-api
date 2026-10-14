# REVIEW.md — Starter Code Defect Review

This document catalogues the 24 defects identified and resolved in the starter implementation, evaluated against the assignment specification, OpenAPI contract, and database requirements.

---

## Defect Inventory

| Defect & Location | Impact | Resolution | Verification |
|---|---|---|---|
| **D-01: Health check false positive**<br>`health()` | Returned static `{"status": "ok"}` without testing database connectivity, reporting healthy even if MongoDB was down. | Added `client.admin.command("ping")` and `ensure_indexes()`; returns HTTP 503 if MongoDB is unreachable. | `tests/test_operations.py` (Section 1) |
| **D-02: Off-by-one pagination offset**<br>`list_employees()` | Calculated offset as `skip = page * page_size`, skipping all page 1 records. | Corrected calculation to `skip = (page - 1) * page_size`. | `tests/test_operations.py` (Section 3) |
| **D-03: Filtered count ignored department**<br>`list_employees()` | Executed `count_documents({})`, returning company-wide total rather than filtered department count. | Replaced with `count_documents(q)` to match active filter criteria. | `tests/test_operations.py` (Section 3) |
| **D-04: Missing employee sort order**<br>`list_employees()` | Cursor lacked sorting, returning documents in arbitrary natural storage order across pagination boundaries. | Added deterministic `.sort("emp_code", ASCENDING)`. | `tests/test_operations.py` (Section 3) |
| **D-05: Naive local datetimes in punch-in**<br>`punch_in()` | Used `datetime.fromtimestamp()` without timezone info, causing timestamps to shift based on host server timezone. | Converted via `timezone.utc` truncated to whole seconds per Rule R1. | `tests/test_operations.py` (Section 4) |
| **D-06: Unhandled missing employee crash**<br>`punch_in()` | Dereferencing missing employee profile raised an unhandled 500 error (`TypeError`). | Added explicit check returning HTTP 404 if employee does not exist. | `tests/test_operations.py` (Section 4) |
| **D-07: Attendance date ignored overnight shifts**<br>`punch_in()` | Assigned attendance date from local server date, failing Rule R1 overnight shifts (`shift_end <= shift_start`). | Implemented `attendance_date()` converting UTC to IST and attributing early-morning overnight punches to previous calendar day. | `tests/test_operations.py` (Section 4) |
| **D-08: Check-then-insert race condition**<br>`punch_in()` | Queried `find_one()` prior to `insert_one()`, allowing concurrent requests to create duplicate punches for the same date. | Enforced unique compound index `(emp_code, date)` and handled `DuplicateKeyError` to return HTTP 409 Conflict. | `tests/test_operations.py` (Section 4) |
| **D-09: In-memory collection loading**<br>`list_attendance()` | Loaded all matching records into memory via `list(...)` before slicing with Python indices. | Moved pagination to database cursor via `.skip(skip).limit(limit)`. | `tests/test_operations.py` (Section 5) |
| **D-10: Incomplete attendance sorting**<br>`list_attendance()` | Sorted only by `date` in Python, leaving intraday employee ordering undefined across pages. | Added compound database sort: `[("date", DESCENDING), ("emp_code", ASCENDING)]`. | `tests/test_operations.py` (Section 5) |
| **D-11: Attendance total derived from slice**<br>`list_attendance()` | Calculated `total` from in-memory slice length rather than matching collection count. | Replaced with `db.attendance_logs.count_documents(q)`. | `tests/test_operations.py` (Section 5) |
| **D-12: Late minutes threshold flaw**<br>`compute_late_minutes()` | Evaluated `minutes > 10` on floored minutes, failing to trigger late calculation at 10m01s (601s). | Evaluated raw elapsed seconds: `delta_s > 600` strictly per Rule R2. | `tests/test_operations.py` (Section 2) |
| **D-13: Overtime shift-end date error**<br>`compute_overtime()` | Evaluated shift-end instant on `att_date` directly without advancing date for overnight shifts. | Advanced shift-end calendar date by +1 day when `shift_end <= shift_start`. | `tests/test_operations.py` (Section 2) |
| **D-14: Banker's rounding in work hours**<br>`compute_work_hours()` | Python `round()` rounded exact halfway fractions to even digits (e.g. 8.125 to 8.12). | Applied symmetric half-up rounding (`ROUND_HALF_UP`) via `Decimal` per Rule R4. | `tests/test_operations.py` (Section 2) |
| **D-15: Naive timestamp on employee creation**<br>`create_employee()` | Stored `created_at` with naive local `datetime.now()`. | Standardized to `datetime.now(timezone.utc).replace(microsecond=0)`. | `tests/test_operations.py` (Section 3) |
| **D-16: Unserialized datetime in response**<br>`create_employee()` | Returned raw datetime object that serialized to ISO string instead of integer epoch milliseconds. | Converted timestamps to integer epoch milliseconds via `_dt_to_ms()`. | `tests/test_operations.py` (Section 3) |
| **D-17: Spurious `id` field injected**<br>`list_attendance()` | Injected `id = str(_id)` into attendance objects where contract specifies identity solely by `(emp_code, date)`. | Excluded `_id` from projection (`{"_id": 0}`) and removed field injection. | `tests/test_operations.py` (Section 5) |
| **D-18: No indexes created at startup**<br>Database init | No indexes created, causing queries to trigger collection scans (`COLLSCAN`) and leaving uniqueness unenforced. | Added `ensure_indexes()` creating unique and compound query indexes on startup and health probe. | `tests/test_operations.py` (Section 1) |
| **D-19: Missing epoch-ms range validation**<br>`PunchInIn` / `punched_at` | Accepted arbitrary integers (such as seconds-era timestamps), producing corrupted calendar dates. | Created `EpochMillis` type validating integer range between `100_000_000_000` and `4_102_444_800_000`. | `tests/test_operations.py` (Section 4) |
| **D-20: Permitted zero-duration shifts**<br>`EmployeeIn` | Allowed `shift_start == shift_end`. | Added Pydantic validator rejecting identical shift times with HTTP 422. | `tests/test_operations.py` (Section 3) |
| **D-21: Unvalidated employee code format**<br>`EmployeeIn` / `emp_code` | Permitted arbitrary string formats. | Added strict regex validation: `^EMP\d{4,6}$`. | `tests/test_operations.py` (Section 3) |
| **D-22: Unvalidated inverted date ranges**<br>`list_attendance()` | Allowed `date_from > date_to` without validation, returning empty results silently. | Added validation raising HTTP 422 when `date_from > date_to`. | `tests/test_operations.py` (Section 5) |
| **D-23: Uncapped pagination page size**<br>`list_employees()`, `list_attendance()` | Permitted arbitrarily large `page_size`, creating denial-of-service vulnerability. | Added `Query(ge=1, le=100)` constraints on all paginated routes. | `tests/test_operations.py` (Sections 3 & 5) |
| **D-24: Raw BSON types leaking to client**<br>Response serializers | Leaked raw `ObjectId` and `Decimal128` types into API responses. | Added recursive serialization converting BSON types to standard JSON primitives. | `tests/test_operations.py` (Sections 3 & 5) |

---

## Non-Defects Investigated

1. **Naive BSON Datetimes from Driver**:
   - *Investigation:* Certain driver and mock query paths return naive UTC datetimes.
   - *Conclusion:* Handled cleanly by `ensure_utc(dt)` which attaches `timezone.utc` if `dt.tzinfo is None`, preserving UTC-aware arithmetic.

2. **Half-Day Attendance Calculation (Rule R5)**:
   - *Investigation:* Verification of whether `HALF_DAY` is an attendance status.
   - *Conclusion:* `HALF_DAY` is not an allowed status. Allowed statuses are strictly `PRESENT`, `ABSENT`, `LEAVE`, `WFH`, and `ON_DUTY`. Under Rule R5, `half_day` is an independent Boolean flag evaluated as `half_day = True` when rounded `work_hours < 4.50`, and `False` otherwise. Punch-out computes `work_hours` and `half_day` without altering the existing status.

3. **Analytics Consuming Stored Derived Fields**:
   - *Investigation:* Analytics aggregation pipelines read stored fields (`late_minutes`, `work_hours`, `half_day`, `status`) rather than recalculating from punch timestamps.
   - *Conclusion:* Required by contract. When attendance is regularized via `PATCH /attendance/{emp_code}/{date}`, managers may legitimately override status or adjust late minutes. Recalculating dynamically from punch timestamps would erase authorized managerial adjustments and invalidate the audit trail.

4. **Competition Ranking (1224) Returning Extra Items on Ties**:
   - *Investigation:* `GET /analytics/leaderboard/late?limit=2` returns 3 employees when positions 2 and 3 share identical late metrics.
   - *Conclusion:* Correct behavior. Standard competition ranking assigns tied ranks (e.g., 1, 2, 2) and the contract filters by `rank <= limit`. Arbitrarily dropping a tied candidate at the cutoff boundary would violate ranking rules.

---

## Live MongoDB Atlas Verification

The application was tested against a live **MongoDB Atlas cluster (MongoDB v8.0.34)**:
- **Connectivity & Health**: `GET /health` returned HTTP 200 OK after confirming admin ping and index creation over TLS.
- **Index Enforcement**: Unique indexes (`emp_code_unique` and `empcode_date_unique`) were confirmed active; duplicate punch-ins returned HTTP 409 Conflict.
- **Query Plans (`/admin/explain/{endpoint}`)**: Real execution stats were inspected across all five explain endpoints (`attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, and `department_trend`), confirming `IXSCAN` index usage and zero unindexed collection scans (`COLLSCAN`).
- **BSON Timestamp Handling**: Cluster metadata returned in Atlas command responses is serialized to ISO strings, ensuring valid JSON serialization.
