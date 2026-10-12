# DECISIONS.md — Architecture & Design Decisions

This document outlines the key architectural and algorithmic decisions implemented in the HROne Employee Attendance & Analytics API, explaining the rationale, trade-offs, and contract implications.

---

### 1. Indexes Created and Rejected

**Indexes Created:**

- **`employees` collection:**
  - `{"emp_code": 1}` (`unique=True`): Enforces global employee uniqueness and guarantees O(1) point lookups for employee profile retrieval and validation during punch-in/out.
  - `{"department": 1, "emp_code": 1}`: Directly powers filtered employee queries (`GET /employees?department=...`) while satisfying the required sort order (`emp_code` ASC) without an in-memory sort pass.
  - `{"department": 1, "joined_on": 1}`: Supports department headcount calculations by allowing index-backed filtering of active employees where `joined_on <= month_end`.
  - `{"joined_on": 1}`: Accelerates company-wide headcount evaluations across all departments.

- **`attendance_logs` collection:**
  - `{"emp_code": 1, "date": 1}` (`unique=True`): The primary business constraint ensuring at most one attendance record exists per employee per business day. This index guarantees atomic punch-in and prevents duplicate punches under concurrent load.
  - `{"date": -1, "emp_code": 1}`: Supports default pagination and compound sorting (`date` DESC, `emp_code` ASC) for `GET /attendance`, streaming results directly from the index.
  - `{"date": 1, "emp_code": 1}`: Supports date-range analytics filtering and index-assisted `$lookup` joins between employees and attendance logs.
  - `{"date": 1, "late_minutes": 1}`: Accelerates the late leaderboard aggregation by indexing date-range filters alongside the primary ranking metric.
  - `{"date": 1, "status": 1}` and `{"emp_code": 1, "date": 1, "status": 1}`: Accelerate presence counts, daily attendance rate aggregations, and monthly summary rollups.

**Indexes Considered and Rejected:**

- **Single-field index on `status`:** Rejected because `status` has extremely low cardinality (only 5 discrete values: `PRESENT`, `HALF_DAY`, `ON_DUTY`, `ABSENT`, `LEAVE`). In MongoDB, low-cardinality single-field indexes offer poor selectivity and often cause the query planner to favor collection scans. Compound indexes such as `{"date": 1, "status": 1}` provide vastly superior selectivity.
- **Single-field index on `date` alone:** Rejected in favor of compound indexes `{"date": -1, "emp_code": 1}` and `{"date": 1, "emp_code": 1}`. Every query that filters by date in this system also sorts or groups by `emp_code` or `status`; compound indexes satisfy both the filter and the sort order simultaneously.

---

### 2. Punch-In Race Condition & Concurrency Control

When two identical punch-in requests for the same employee arrive at the exact same millisecond:

1. **Deterministic Attendance Date (IST & Overnight Shift Rule R1):**
   Both requests parse the incoming UTC timestamp and pass it to `attendance_date()`. The helper converts the UTC timestamp to Indian Standard Time (IST, UTC+05:30) and applies the business rule: if the employee has an overnight shift (`shift_end <= shift_start`, e.g., 22:00 to 06:00) and the punch occurs before `shift_end` in IST, the attendance date is attributed to the preceding calendar day (`punch_ist - 1 day`); otherwise, it belongs to the current calendar day. Both requests arrive at the exact same `(emp_code, date)` tuple.

2. **Atomic Insert vs. Flawed Check-Then-Insert:**
   Neither request relies on an application-level `find_one()` existence check to prevent duplicates. Instead, both requests validate employee existence and then directly issue an atomic `db.attendance_logs.insert_one(...)`.

3. **Database-Level Mutual Exclusion:**
   MongoDB's WiredTiger storage engine serializes document insertion under the unique compound index `{"emp_code": 1, "date": 1}`:
   - The winning thread successfully writes the document and returns `HTTP 201 Created` with the serialized record.
   - The concurrent thread encounters a unique index collision, raising `pymongo.errors.DuplicateKeyError` (error code 11000).

4. **HTTP 409 Conflict Response:**
   The code catches `DuplicateKeyError` and immediately raises `HTTPException(status_code=409, detail="Employee already punched in for this date")`. No duplicate records are created, no check-then-insert race window exists, and zero distributed locks are required.

---

### 3. Ties at the Leaderboard Cutoff

When two employees are tied on `total_late_minutes` at the cutoff of `limit`:

- The aggregation pipeline for `GET /analytics/leaderboard/late` implements standard competition ranking (1224 ranking) using MongoDB's `$setWindowFields` stage with the `$rank` operator, sorting by `total_late_minutes` DESC, `late_count` DESC, and `emp_code` ASC.
- If two or more employees share identical `total_late_minutes` and `late_count`, they receive the exact same integer rank (for example, ranks 1, 2, 2).
- The pipeline then filters qualifying records using `$match: {"rank": {"$lte": limit}}`.
- If `limit=2` and two employees share rank 2, all three employees (rank 1, rank 2, rank 2) are returned in the response array.
- **Rationale:** Standard competition ranking dictates that tied candidates cannot be arbitrarily separated. Truncating tied employees based on artificial pagination limits or arbitrary secondary attributes would introduce nondeterministic bias into HR compliance and disciplinary decisions, and would violate the explicit OpenAPI contract.

---

### 4. Headcount and Zero-Log Employees in Department Summary

To ensure employees with zero attendance logs are accurately counted in department headcount:

- Aggregating directly from `attendance_logs` would silently omit employees who had zero punch records during the month (e.g., new joiners, employees on continuous leave, or unrecorded absences), causing headcount to be underreported and attendance percentages to be falsely inflated.
- Our `GET /analytics/departments/summary` pipeline starts from the **`employees` collection**:
  1. It filters active employees by `joined_on <= month_end` (and optional `department`), establishing the true active employee baseline eligible to work in that month.
  2. It performs a `$lookup` to join `attendance_logs` matching on `emp_code` and date within the target month `[month_start, month_end]`.
  3. Using `preserveNullAndEmptyArrays: true` (preserving empty log arrays for employees with zero logs), zero-log employees remain in the pipeline.
  4. The aggregation groups by `department`, computing `headcount = $sum: 1` over unique active employees, while aggregating present days, late minutes, and work hours from the joined logs.
- This ensures zero-log employees correctly contribute to the denominator `headcount` without incorrectly inflating presence counts or average hours.

---

### 5. Architectural Changes for 100x Data Scale

If this API had to serve 100x the data volume (~10 million attendance records and 50,000 employees), the following architectural changes would be implemented:

1. **Pre-aggregated Materialized Daily Rollups:**
   - *Problem:* Running complex aggregation pipelines (`$lookup`, `$densify`, `$setWindowFields`) on raw logs on-demand across millions of documents causes heavy CPU and RAM pressure.
   - *Solution:* Maintain a `daily_department_summary` collection materialized incrementally using MongoDB Change Streams or scheduled `$merge` pipelines. Monthly department summaries and 92-day trend queries would then query pre-aggregated daily summaries (reading only ~30–92 documents per department) in O(1) or O(30) index lookups rather than scanning millions of raw logs.

2. **Read-Write Splitting:**
   - Route analytical read queries (`/analytics/*` and `/admin/explain/*`) to secondary replica set nodes using `readPreference=secondaryPreferred`. This isolates write throughput (high-frequency punch-in/punch-out) on the primary node from analytical aggregation workloads.

3. **Horizontal Sharding & Time-Series Collections:**
   - Shard the `attendance_logs` collection across multiple MongoDB shards using a compound shard key `{"date": 1, "emp_code": 1}` to distribute write I/O across cluster nodes and ensure queries bounded by date range target specific shards.
   - For high-frequency audit logs and punch events, leverage MongoDB Time Series collections to achieve superior storage compression and query throughput.

---

### 6. Additional Business Rule & Implementation Decisions

- **Half-Up Rounding vs. Banker's Rounding (Rules R4 & R8):**
  Python's built-in `round()` and MongoDB's `$round` operator both execute round-half-to-even (banker's rounding). In payroll and HR analytics, rounding 8.125 down to 8.12 or moving average 0.16665 down to 0.1666 introduces systematic truncation bias.
  - In Python helper functions, we use `Decimal.quantize(ROUND_HALF_UP)` to enforce strict symmetric half-up rounding to two decimal places.
  - In MongoDB aggregation pipelines (for `attendance_rate` and `moving_avg_7d`), calculations are maintained in high-precision `Decimal128` and rounded to 4 decimal places via an exact aggregation half-up formula: multiplying by 10,000, adding `Decimal128("0.5")`, applying `$floor`, and dividing by 10,000. Values are converted to double only at the final JSON projection boundary.

- **Preserving Stored Derived Values in Analytics:**
  Analytics aggregation pipelines deliberately read stored fields (`work_hours`, `late_minutes`, `overtime_minutes`, `status`, `half_day`) rather than recalculating them dynamically from raw timestamps. When HR or managers execute a manual regularization via `PATCH /attendance/{emp_code}/{date}`, they may legitimately override status (e.g. marking `ON_DUTY` or waiving late minutes) with an audit entry in `history`. Recalculating metrics from punch timestamps would silently overwrite approved managerial adjustments and invalidate the audit trail.

- **MongoDB `$densify` for Department Trend Gap-Filling (MongoDB 6.0+ Requirement):**
  The department trend endpoint requires every calendar day in the requested date range to be present, even if no employee logged attendance. We utilize MongoDB's `$densify` aggregation stage with `unit: "day"` to fill missing dates in the pipeline before applying `$setWindowFields` to compute the 7-day moving average over non-null attendance rates. Because `$densify` requires MongoDB 6.0+, a narrow fallback simulation is provided solely for test environments (such as `mongomock`) that do not implement `$densify`, while ensuring all real database errors propagate transparently.
