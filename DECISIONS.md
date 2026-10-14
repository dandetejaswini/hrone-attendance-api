# DECISIONS.md — Architecture & Design Decisions

This document outlines the core architectural decisions implemented in the Employee Attendance & Analytics API, explaining the rationale, trade-offs, and contract implications for each.

---

### 1. Which indexes were created or rejected, and why?

**Indexes Created:**

- **`employees` collection:**
  - `{"emp_code": 1}` (`unique=True`): Enforces global employee uniqueness and provides fast point lookups during punch operations.
  - `{"department": 1, "emp_code": 1}`: Supports filtered employee queries (`GET /employees?department=...`) while satisfying the required sort order (`emp_code` ASC) without an in-memory sort.
  - `{"department": 1, "joined_on": 1}`: Supports department headcount calculations by allowing index filtering of active employees where `joined_on <= month_end`.
  - `{"joined_on": 1}`: Supports company-wide active employee evaluations across all departments.

- **`attendance_logs` collection:**
  - `{"emp_code": 1, "date": 1}` (`unique=True`): Primary business constraint guaranteeing at most one attendance record per employee per business day. Backs atomic punch-in operations.
  - `{"date": -1, "emp_code": 1}`: Powers default pagination and compound sorting (`date` DESC, `emp_code` ASC) for `GET /attendance`.
  - `{"date": 1, "emp_code": 1}`: Supports date-range analytics filtering and index-assisted `$lookup` joins.
  - `{"date": 1, "late_minutes": 1}`: Accelerates late leaderboard aggregation by indexing date filters alongside late minutes.
  - `{"date": 1, "status": 1}` and `{"emp_code": 1, "date": 1, "status": 1}`: Accelerate presence counts and monthly summary rollups.

**Indexes Rejected:**
- **Single-field index on `status`:** Rejected because `status` has low cardinality (only 5 discrete values: `PRESENT`, `ABSENT`, `LEAVE`, `WFH`, `ON_DUTY`). Low-cardinality single-field indexes offer poor selectivity in MongoDB and increase write amplification. Compound indexes such as `{"date": 1, "status": 1}` provide significantly better selectivity.
- **Single-field index on `date` alone:** Rejected in favor of compound indexes `{"date": -1, "emp_code": 1}` and `{"date": 1, "emp_code": 1}`. Queries that filter by date also sort or join on `emp_code`, which compound indexes satisfy directly.

**Trade-off:**
Additional compound indexes increase write overhead and storage consumption during punch-in/out, but guarantee bounded query times and eliminate in-memory sorting across high-volume collections.

---

### 2. How is the punch-in race condition prevented?

**Decision:**
Concurrency control is delegated to the database via MongoDB's unique compound index `{"emp_code": 1, "date": 1}` on `attendance_logs`, combined with atomic insertion (`insert_one`).

**Rationale:**
1. **Deterministic Attendance Date:** Both concurrent requests evaluate `attendance_date()` using UTC punch times converted to IST (UTC+05:30), applying Rule R1 for overnight shifts (`shift_end <= shift_start`). Both requests resolve to the identical `(emp_code, date)` tuple.
2. **Atomic Mutual Exclusion:** Rather than performing an application-level existence check (`find_one`) followed by an insert (which creates a race window), the API executes an atomic `insert_one`.
3. **Collision Handling:** If two requests arrive concurrently for the same employee on the same date, MongoDB serializes the writes: one insert succeeds with HTTP 201, while the duplicate triggers a `DuplicateKeyError` (error code 11000). The API catches this error and returns HTTP 409 Conflict (`{"detail": "already punched in for this date"}`).

**Trade-off:**
Relies on database constraint enforcement rather than distributed locks (e.g. Redis locks). This eliminates external coordination dependencies and network overhead while guaranteeing atomicity within MongoDB.

---

### 3. How are leaderboard ties handled?

**Decision:**
The `GET /analytics/leaderboard/late` aggregation pipeline implements standard competition ranking (1224 ranking) using MongoDB's `$setWindowFields` stage with the `$rank` operator, sorting by `total_late_minutes` DESC, `late_count` DESC, and `emp_code` ASC.

**Rationale:**
- Candidates with identical `total_late_minutes` and `late_count` receive the exact same integer rank.
- The pipeline filters qualifying entries using `$match: {"rank": {"$lte": limit}}`.
- If `limit=2` and two employees share rank 2, all qualifying employees (rank 1, rank 2, rank 2) are returned in the response array.
- Standard competition ranking avoids arbitrary truncation of tied candidates at page boundaries, maintaining fairness for HR auditing.

**Trade-off:**
Responses may return more items than requested by `limit` when ties occur at the rank boundary. This is an intentional contract requirement.

---

### 4. Why does department headcount include employees with no attendance logs?

**Decision:**
The `GET /analytics/departments/summary` pipeline starts from the **`employees` collection**, rather than from `attendance_logs`.

**Rationale:**
- Aggregating directly from `attendance_logs` would omit employees who had zero punch records during the month (such as new joiners, employees on extended leave, or unrecorded absences). This would falsely reduce the denominator and inflate attendance percentages.
- The pipeline filters active employees by `joined_on <= month_end` (and optional `department`), establishing the true eligible headcount.
- It performs a `$lookup` into `attendance_logs` matching on `emp_code` within the month, preserving employees with zero logs.
- Active employee count is computed over unique employees, ensuring zero-log staff contribute to the headcount baseline without inflating presence figures.

**Trade-off:**
Querying from `employees` and performing a `$lookup` join requires slightly more processing than scanning `attendance_logs` alone, but guarantees accurate headcount and attendance statistics.

---

### 5. What architectural changes would be implemented at 100x data volume?

If volume scaled to ~10 million attendance records and 50,000 employees:

1. **Pre-aggregated Daily Rollups:**
   - Instead of running on-demand `$lookup`, `$densify`, and `$setWindowFields` aggregations across millions of raw logs, maintain an incrementally updated daily summary collection via MongoDB Change Streams or scheduled batch rollups.
   - Monthly and 92-day trend queries would query pre-aggregated daily summaries (reading only ~30–92 documents per department) rather than scanning millions of raw records.

2. **Read-Write Splitting:**
   - Direct reporting and query explain calls to secondary replica set members via `readPreference=secondaryPreferred`, isolating heavy aggregation workloads from write-critical punch-in/out paths on the primary.

3. **Horizontal Sharding:**
   - Shard the `attendance_logs` collection using a compound shard key `{"date": 1, "emp_code": 1}` to distribute write I/O across cluster shards and ensure date-scoped queries target specific shards.

---

### 6. Implementation Notes

- **Symmetric Half-Up Rounding (Rules R4 & R8):**
  Python's `round()` and MongoDB's `$round` implement round-half-to-even (banker's rounding). In payroll calculations, rounding 8.125 to 8.12 introduces downward truncation bias. In Python helpers, we use `Decimal.quantize(ROUND_HALF_UP)`. In MongoDB aggregation pipelines, calculations use `Decimal128` precision with an exact half-up formula (`floor(x * 10000 + 0.5) / 10000`) before final projection.

- **Preserving Stored Derived Fields in Analytics:**
  Analytics pipelines aggregate stored fields (`late_minutes`, `work_hours`, `half_day`, `status`) rather than re-computing from raw punch timestamps. When records are adjusted via `PATCH /attendance/{emp_code}/{date}`, managers may legitimately override status (e.g. `ON_DUTY`) or waive late minutes. Aggregating stored values preserves approved managerial adjustments and maintains the audit trail.

- **MongoDB `$densify` for Trend Gap-Filling:**
  The department trend endpoint requires every calendar day in the requested date range to be represented, even if no employee punched in. We utilize MongoDB's `$densify` stage (`unit: "day"`) to fill date gaps before computing rolling 7-day moving averages with `$setWindowFields`.
