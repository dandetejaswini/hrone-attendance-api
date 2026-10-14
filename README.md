# Employee Attendance & Analytics API

A REST API for employee management, attendance tracking, and workforce analytics built with FastAPI, PyMongo, and MongoDB.

---

## Overview

The API provides core employee attendance tracking and reporting capabilities:
- **Employee Management:** Onboard and list employees with unique employee codes, shift schedules, and department associations.
- **Attendance Operations:** Record punch-in and punch-out events with unique index enforcement on `(emp_code, date)` to prevent duplicate punches on the same business date.
- **IST & Overnight Shift Support:** Punch timestamps are recorded in UTC and mapped to Indian Standard Time (IST, UTC+05:30) business dates, correctly attributing overnight shifts spanning midnight to the shift's starting calendar date.
- **Attendance Regularization:** Update historical attendance records with optimistic concurrency control and audit history tracking.
- **Workforce Analytics:** MongoDB aggregation pipelines computing monthly employee summaries, department headcounts and attendance rates, late-arrival leaderboards with competition tie-ranking, and department trends with rolling moving averages.
- **Query Plan Introspection:** `/admin/explain/{endpoint}` route running `explain("executionStats")` to verify index coverage.

---

## Technology Stack & Prerequisites

- **Language:** Python 3.11+
- **Framework:** FastAPI
- **Database Driver:** PyMongo 4.7+
- **Database Engine:** MongoDB 6.0+ (requires `$densify` and `$setWindowFields` support; tested on MongoDB Atlas v8.0)
- **ASGI Server:** Uvicorn

---

## Installation & Setup

### 1. Create a Virtual Environment

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
# MongoDB connection string (local MongoDB instance or MongoDB Atlas SRV URI)
MONGO_URI=mongodb://localhost:27017

# Target database name
MONGO_DB=attendance_db
```

Ensure `.env` remains excluded from version control.

---

## Starting the Application

Start the API with Uvicorn:

```bash
uvicorn app.main:app --port 8000
```

Once running:
- **Health Check:** `http://127.0.0.1:8000/health` (pings MongoDB and ensures required indexes)
- **Interactive Documentation (Swagger UI):** `http://127.0.0.1:8000/docs`
- **Alternative Documentation (ReDoc):** `http://127.0.0.1:8000/redoc`

---

## Endpoints

### Core Operations
| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health probe; pings MongoDB and ensures indexes (returns 200 or 503). |
| `POST` | `/employees` | Onboard an employee with shift times and validation. |
| `GET` | `/employees` | List employees with optional department filter and pagination. |
| `POST` | `/attendance/punch-in` | Punch-in; enforces at most one record per business day via unique index. |
| `POST` | `/attendance/punch-out` | Punch-out; computes work hours, overtime, and half-day flag. |
| `GET` | `/attendance` | Paginated attendance log query sorted by `date` DESC, `emp_code` ASC. |
| `PATCH` | `/attendance/{emp_code}/{date}` | Manual record regularization with OCC and history audit logging. |

### Analytics
| Method | Path | Description |
|---|---|---|
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee attendance breakdown and dynamic working days. |
| `GET` | `/analytics/departments/summary` | Department summary metrics including zero-log employee headcount. |
| `GET` | `/analytics/leaderboard/late` | Late leaderboard with standard competition ranking (`$rank`) and tie handling. |
| `GET` | `/analytics/departments/{department}/trend` | Attendance trend with date gap-filling (`$densify`) and 7-day moving average. |

### Query Explain
| Method | Path | Description |
|---|---|---|
| `GET` | `/admin/explain/{endpoint}` | Runs `explain("executionStats")` for the production query or aggregation pipeline. |

---

## Database Verification

- The application was verified against a live **MongoDB Atlas cluster (MongoDB v8.0.34)**.
- Indexes were confirmed active on Atlas, including unique constraints `emp_code_unique` on `employees` and `empcode_date_unique` on `attendance_logs`.
- Query execution plans via `/admin/explain/{endpoint}` confirmed active `IXSCAN` index usage across all supported endpoints.
- BSON `Timestamp` values returned by MongoDB replica sets are serialized to ISO strings in explain responses.

---

## Deliverables & Repository Structure

- Application entry point: [`app/main.py`](app/main.py)
- Dependencies: [`requirements.txt`](requirements.txt)
- Design decisions: [`DECISIONS.md`](DECISIONS.md)
- Defect review: [`REVIEW.md`](REVIEW.md)
- Repository guide: [`README.md`](README.md)
