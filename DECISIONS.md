# Architectural & Design Decisions

### 1. Which indexes did you create and why?
On `employees`, we created a unique index on `emp_code` for point lookups and uniqueness, and a compound index `{"department": 1, "emp_code": 1}` for filtered listing without in-memory sorting. On `attendance_logs`, we created a unique compound index `{"emp_code": 1, "date": 1}` to strictly enforce one record per employee per day and block duplicate punch-ins. We added `{"date": -1, "emp_code": 1}` for sorted pagination, and compound indexes on `date` with `late_minutes` and `status` to accelerate monthly aggregations. Standalone low-cardinality indexes were rejected to avoid write amplification.

### 2. What happens when two identical punch-in requests arrive simultaneously?
Both requests resolve to the identical `(emp_code, date)` tuple in IST and attempt `insert_one`. MongoDB serializes the writes: the first succeeds with HTTP 201, and the second violates the unique compound index `(emp_code, date)`. The API intercepts `DuplicateKeyError` (error code 11000) and returns HTTP 409 Conflict (`already punched in for this date`). Database-enforced mutual exclusion prevents duplicate punches without distributed locks.

### 3. What does the leaderboard return when a tie occurs at the cutoff?
It implements standard competition ranking (`$rank` in `$setWindowFields`), sorting by `total_late_minutes` DESC, `late_count` DESC, and `emp_code` ASC. Tied employees receive the identical rank. The pipeline filters by `rank <= limit`. If a tie occurs at the cutoff boundary, all qualifying employees are returned (e.g. ranks 1, 2, 2 for `limit=2`), avoiding arbitrary truncation.

### 4. How does the department summary count employees with no attendance logs?
The pipeline originates from `employees` filtered by `joined_on <= month_end`, capturing true eligible headcount. A `$lookup` joins matching `attendance_logs` for the month. Employees with zero attendance logs are preserved with zero present days and null average hours, correctly reflecting organization headcount without artificially inflating presence metrics.

### 5. What would you change if the dataset became 100 times larger?
Pre-aggregate daily metrics via Change Streams or scheduled batch rollups rather than running on-demand `$lookup` and `$densify` aggregations across millions of raw logs. Shard `attendance_logs` on `{"date": 1, "emp_code": 1}` to distribute write I/O, and route read-heavy analytics queries to secondary replicas (`readPreference=secondaryPreferred`).
