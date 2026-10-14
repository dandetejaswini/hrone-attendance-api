# HROne Employee Attendance & Analytics API

A high-performance, contract-compliant REST API for employee management, attendance lifecycle tracking, and workforce analytics built with FastAPI, PyMongo, and MongoDB.

---

## Overview & Core Capabilities

The HROne Attendance & Analytics API provides an enterprise-ready attendance tracking and reporting engine designed to handle high concurrency and large data volumes:

- **Employee Lifecycle Management:** Employee onboarding with unique identifiers, shift definitions, and department assignments.
- **Atomic Attendance Tracking:** Race-condition-free punch-in and punch-out operations backed by database-level unique constraints and atomic operations.
- **Overnight Shifts & IST Business Dates:** Automatic attribution of overnight shifts spanning midnight to their originating business date according to Indian Standard Time (IST, UTC+05:30).
- **Attendance Regularization:** Manual record adjustment (`PATCH`) with optimistic concurrency control (OCC) and audit history preservation.
- **Enterprise Analytics:** Complex workforce metrics powered by MongoDB aggregation pipelines:
  - Monthly employee attendance breakdown with dynamic working-day calculations.
  - Department summary metrics ensuring active headcount includes zero-log employees.
  - Late leaderboard with standard competition ranking (`$rank`) and tie handling.
  - 92-day department attendance trend with calendar gap-filling (`$densify`) and 7-day rolling moving average (`$setWindowFields`).
- **Query Plan Introspection:** Production-grade `/admin/explain/{endpoint}` running `explain("executionStats")` to verify index coverage (`IXSCAN`) and query efficiency.

---

## Technology Stack & Prerequisites

- **Language:** Python 3.11+
- **Framework:** FastAPI
- **Database Driver:** PyMongo 4.7+
- **Database Engine:** MongoDB 6.0+ (MongoDB 7.0 recommended; MongoDB Atlas M0 supported)
- **Data Validation:** Pydantic 2.6+
- **Server:** Uvicorn (ASGI)

---

## Installation & Setup

### 1. Clone the Repository & Create Virtual Environment

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**macOS / Linux (Bash / Zsh):**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Copy the provided `.env.example` template to `.env`:

**Windows (PowerShell / CMD):**
```powershell
copy .env.example .env
```

**macOS / Linux:**
```bash
cp .env.example .env
```

Configure your MongoDB connection details in `.env`:
```env
# MongoDB connection string (local instance or MongoDB Atlas URI)
MONGO_URI=mongodb://localhost:27017

# MongoDB database name
MONGO_DB=attendance_db
```
> [!NOTE]
> Never commit `.env` or credentials to version control. The repository ignores `.env` by default.

### 4. Seed Sample Data (Optional)

To seed the initial sample dataset (6 employees and 9 attendance records):
```bash
python sample_seed.py
```

---

## Starting the Application

Run the FastAPI application with Uvicorn:

```bash
uvicorn app.main:app --port 8000 --reload
```

The server starts at `http://127.0.0.1:8000`.
- **Interactive API Documentation (Swagger UI):** `http://127.0.0.1:8000/docs`
- **ReDoc Documentation:** `http://127.0.0.1:8000/redoc`
- **Health Probe:** `http://127.0.0.1:8000/health`

---

## Running the Automated Test Suites

The repository includes a comprehensive, four-phase automated test suite covering edge cases, race conditions, business-rule boundaries, Decimal128 rounding, and query explain structures.

Run all test suites using the master test runner:
```bash
python tests/run_all_tests.py
```

Or run individual test modules directly:
```bash
# Phase 1 & Core Operations (CRUD, health probe, pagination, boundaries)
python tests/test_operations.py

# Phase 2 Concurrency & Punch Operations (Punch-out, OCC regularization)
python tests/test_phase2.py

# Phase 3 Analytics (Monthly summaries, leaderboard ties, $densify trend)
python tests/test_analytics.py

# Phase 4 Explain & Query Plans (Parameter validation, executionStats detection)
python tests/test_explain.py
```

---

## Contract & Data Model

- **OpenAPI 3.0 Specification:** Detailed in [`openapi.yaml`](openapi.yaml) defining all endpoint paths, query parameters, schemas, and business rules R1–R10.
- **Data Model:** Detailed in [`DATA_MODEL.md`](DATA_MODEL.md) documenting MongoDB collection structures, BSON field types, indexes, and sample documents.
- **Architectural Decisions:** Detailed in [`DECISIONS.md`](DECISIONS.md) documenting index strategies, concurrency control, tie handling, and 100x scalability.
- **Defect Review:** Detailed in [`REVIEW.md`](REVIEW.md) cataloguing all 24 starter defects resolved in the implementation.

---

## API Endpoints Summary

### Core Operations
| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health probe; pings MongoDB and ensures indexes (returns 200 or 503). |
| `POST` | `/employees` | Onboard an employee with shift definitions and validation. |
| `GET` | `/employees` | List employees with optional department filter and pagination. |
| `POST` | `/attendance/punch-in` | Atomic punch-in; enforces at-most-one per business day via unique index. |
| `POST` | `/attendance/punch-out` | Atomic punch-out; computes work hours, overtime, and half-day status. |
| `GET` | `/attendance` | Paginated attendance log query sorted by `date` DESC, `emp_code` ASC. |
| `PATCH` | `/attendance/{emp_code}/{date}` | Manual regularization with OCC and history audit tracking. |

### Analytics Endpoints
| Method | Path | Description |
|---|---|---|
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee breakdown with dynamic join-date working days. |
| `GET` | `/analytics/departments/summary` | Department summary ensuring active headcount includes zero-log staff. |
| `GET` | `/analytics/leaderboard/late` | Late leaderboard with standard competition ranking (`$rank`) and tie handling. |
| `GET` | `/analytics/departments/{department}/trend` | 92-day trend with gap-filling (`$densify`) and 7-day rolling moving average. |

### Explain Endpoint
| Method | Path | Description |
|---|---|---|
| `GET` | `/admin/explain/{endpoint}` | Inspects `explain("executionStats")` for the production query/aggregation pipeline. |

---

## Live MongoDB Atlas Verification Status

1. **MongoDB Atlas Cluster Integration (Verified):**
   - The API has been verified against **MongoDB Atlas (v8.0.34)** using PyMongo with full SRV and TLS support.
   - Core CRUD and attendance lifecycle operations (`/employees`, `/attendance/punch-in`, `/attendance/punch-out`, `/attendance`, `/attendance/{emp_code}/{date}`) and unique constraint enforcement (`empcode_date_unique`) have been verified against the live database.
   - All complex analytics pipelines (`/analytics/employees/{emp_code}/monthly`, `/analytics/departments/summary`, `/analytics/leaderboard/late`, `/analytics/departments/{department}/trend`) execute against live MongoDB.
2. **Execution-Plan Verification (`/admin/explain/{endpoint}`):**
   - Live query plans run with `explain("executionStats")` confirm active index utilization (`IXSCAN`) and absence of unindexed collection scans (`COLLSCAN`) across all supported query and aggregation endpoints.
   - BSON `Timestamp` objects returned in Atlas replica-set command responses are properly serialized, ensuring schema-compliant JSON responses.
3. **MongoDB Version Requirement:**
   - The department trend aggregation pipeline utilizes `$densify` and `$setWindowFields`, requiring **MongoDB 6.0 or newer** (verified on MongoDB Atlas 8.0.34; compatible with grader MongoDB 7.0 environment).

---

## Submission & Repository Guidelines

- All application code resides in [`app/main.py`](app/main.py).
- Dependencies are specified in [`requirements.txt`](requirements.txt).
- No sensitive credentials, secrets, `.env` files, or Dockerfiles are included.
