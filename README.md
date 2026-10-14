# Employee Attendance & Analytics API

A REST API for employee management, attendance tracking, and workforce analytics built with FastAPI, PyMongo, and MongoDB.

---

## Prerequisites

- **Python:** 3.11+
- **MongoDB:** 6.0+ (required for `$densify` and `$setWindowFields` aggregation operators; tested on MongoDB Atlas v8.0.34)

---

## Installation & Setup

### 1. Create and Activate a Virtual Environment

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Create a `.env` file in the project root:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

*Note: For MongoDB Atlas, supply your cluster SRV URI in `MONGO_URI`. Never commit the `.env` file or credentials to version control.*

---

## Starting the Application

Start the API with Uvicorn:

```bash
uvicorn app.main:app --port 8000
```

Once running, access:
- **Health Check:** `http://127.0.0.1:8000/health` (pings MongoDB and ensures required indexes)
- **Interactive Documentation (Swagger UI):** `http://127.0.0.1:8000/docs`
- **Alternative Documentation (ReDoc):** `http://127.0.0.1:8000/redoc`

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health probe; pings MongoDB and verifies indexes (returns 200 or 503). |
| `POST` | `/employees` | Onboard an employee with shift times and validation. |
| `GET` | `/employees` | List employees with optional department filter and pagination. |
| `POST` | `/attendance/punch-in` | Record punch-in; enforces one record per day via unique index on `(emp_code, date)`. |
| `POST` | `/attendance/punch-out` | Record punch-out; computes work hours, overtime, and half-day flag. |
| `GET` | `/attendance` | Paginated attendance logs sorted by `date` DESC, `emp_code` ASC. |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularize attendance record with optimistic concurrency control and audit history. |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly attendance summary and dynamic working-day calculations. |
| `GET` | `/analytics/departments/summary` | Department summary metrics including zero-log employee headcount. |
| `GET` | `/analytics/leaderboard/late` | Late leaderboard with standard competition ranking (`$rank`) and tie handling. |
| `GET` | `/analytics/departments/{department}/trend` | Daily attendance trend with gap-filling (`$densify`) and 7-day moving averages. |
| `GET` | `/admin/explain/{endpoint}` | Execution plan stats via `explain("executionStats")` for production queries. |

---

## Testing Status & Reviewer Notes

- **Automated Verification:** All local test suites pass cleanly across CRUD operations, concurrency handling, analytics pipelines, and query plan builders.
- **MongoDB Atlas Verification:** Verified live against MongoDB Atlas v8.0.34. Index constraints (`emp_code_unique` and `empcode_date_unique`) enforce uniqueness, and query execution plans confirm `IXSCAN` index usage without unindexed collection scans (`COLLSCAN`).
- **Database Requirement:** Analytics routes rely on MongoDB 6.0+ aggregation operators (`$densify` and `$setWindowFields`). Connecting to MongoDB versions earlier than 6.0 will return error responses from those endpoints.
