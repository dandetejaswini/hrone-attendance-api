"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (loaded from .env; real environment variables take priority)

Phase 4 — operations, concurrency OCC, analytics, and explain endpoint implemented.
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, ObjectId, Timestamp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
_EPOCH_MIN = 100_000_000_000   # smallest valid epoch-ms (avoids seconds-era ints)
_EPOCH_MAX = 4_102_444_800_000  # 2100-01-01T00:00:00Z in ms

# ---------------------------------------------------------------------------
# Database client  (tz_aware=True so PyMongo returns aware datetimes)
# ---------------------------------------------------------------------------
_MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
_MONGO_DB  = os.getenv("MONGO_DB",  "attendance_db")
_MONGO_TIMEOUT_MS = int(os.getenv("MONGO_TIMEOUT_MS", "2000"))

client = MongoClient(_MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=_MONGO_TIMEOUT_MS)
db     = client[_MONGO_DB]

_indexes_created = False


# ---------------------------------------------------------------------------
# Startup: create indexes idempotently
# ---------------------------------------------------------------------------
def _create_indexes() -> None:
    """Create all required indexes. Safe to call repeatedly."""
    # employees -----------------------------------------------------------------
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="emp_code_unique",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="dept_empcode",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="dept_joined",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING)],
        name="joined_on",
    )

    # attendance_logs -----------------------------------------------------------
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="empcode_date_unique",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="date_desc_empcode",
    )
    # Additional index to support analytics queries efficiently
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
        name="empcode_date_status",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="date_status",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("emp_code", ASCENDING)],
        name="date_empcode",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)],
        name="date_late_minutes",
    )


def ensure_indexes() -> None:
    """
    Ensure all required indexes are created.
    Raises on failure so missing indexes are never silenced.
    """
    global _indexes_created
    if _indexes_created:
        return
    _create_indexes()
    _indexes_created = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except Exception:
        # If MongoDB is unavailable at launch, defer index creation to /health
        # probe so FastAPI starts and /health can report 503
        pass
    yield


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Helpers: epoch-ms ↔ datetime
# ---------------------------------------------------------------------------
def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure a datetime is UTC-aware (handling naive BSON datetimes from PyMongo)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_ms(dt: datetime) -> int:
    """Convert an aware UTC datetime to epoch milliseconds (integer)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC-aware datetime truncated to seconds (R1)."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.replace(microsecond=0)


# ---------------------------------------------------------------------------
# Helper: IST attendance date (R1 — overnight shift logic)
# ---------------------------------------------------------------------------
def attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    Return the attendance date string (YYYY-MM-DD, IST) for a punch-in timestamp.

    R1 rule: for overnight shifts (shift_end <= shift_start) a punch-in whose
    IST time is *earlier than* shift_end belongs to the shift that started the
    previous calendar day.
    """
    ist_dt = punch_in_utc.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    overnight = (eh * 60 + em) <= (sh * 60 + sm)   # shift_end <= shift_start
    if overnight:
        punch_ist_minutes = ist_dt.hour * 60 + ist_dt.minute
        end_minutes = eh * 60 + em
        if punch_ist_minutes < end_minutes:
            # This punch-in is in the early-morning tail of yesterday's shift
            return (ist_dt.date() - timedelta(days=1)).isoformat()

    return ist_dt.date().isoformat()


# ---------------------------------------------------------------------------
# Helpers: business-rule computations
# ---------------------------------------------------------------------------
def round_half_up(value: float, places: int = 2) -> float:
    """Round a float half-up to the given number of decimal places (R4, R8)."""
    quantizer = Decimal(10) ** -places
    return float(Decimal(str(value)).quantize(quantizer, rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, att_date: str) -> int:
    """
    R2: late only when strictly > 600 seconds after shift start (IST).
    Returns whole floor minutes since shift_start, else 0.
    punch_in_utc must already be truncated to seconds.
    """
    h, m = map(int, shift_start.split(":"))
    # Build the shift-start instant in IST for the given attendance date
    naive_start = datetime.fromisoformat(att_date).replace(hour=h, minute=m, second=0)
    start_ist = naive_start.replace(tzinfo=IST)
    delta_s = int((punch_in_utc - start_ist).total_seconds())
    if delta_s > 600:
        return delta_s // 60
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) / 3600, half-up to 2 decimal places."""
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    return round_half_up(seconds / 3600, 2)


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, att_date: str) -> int:
    """
    R3: whole floor minutes between shift_end and punch_out,
    counted only when >= 30; otherwise 0.

    For overnight shifts (shift_end <= shift_start), shift_end falls on
    att_date + 1 calendar day.
    """
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    att_day = datetime.fromisoformat(att_date)
    overnight = (eh * 60 + em) <= (sh * 60 + sm)
    shift_end_day = att_day + timedelta(days=1) if overnight else att_day

    shift_end_ist = shift_end_day.replace(hour=eh, minute=em, second=0, tzinfo=IST)
    delta_s = int((punch_out_utc - shift_end_ist).total_seconds())
    if delta_s < 0:
        return 0
    minutes = delta_s // 60
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    return work_hours < 4.50


# ---------------------------------------------------------------------------
# Response serialisation helpers
# ---------------------------------------------------------------------------
def _dt_to_ms(val) -> Optional[int]:
    """Convert a datetime to epoch ms, or return None for None."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return to_ms(val)
    return val  # already a plain value (int, str, etc.)


def _serialise_history(history: list) -> list:
    """Serialise history entries: convert datetime values to epoch ms."""
    out = []
    for entry in history:
        serialised_changes = {}
        for field, change in entry.get("changes", {}).items():
            serialised_changes[field] = {
                "from": _dt_to_ms(change.get("from")),
                "to":   _dt_to_ms(change.get("to")),
            }
        out.append({
            "at":      _dt_to_ms(entry.get("at")),
            "by":      entry.get("by"),
            "reason":  entry.get("reason"),
            "changes": serialised_changes,
        })
    return out


def serialise_employee(doc: dict) -> dict:
    """Convert a raw MongoDB employee document to the API Employee schema."""
    return {
        "emp_code":    doc["emp_code"],
        "name":        doc["name"],
        "email":       doc["email"],
        "department":  doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end":   doc["shift_end"],
        "joined_on":   doc["joined_on"],
        "created_at":  _dt_to_ms(doc["created_at"]),
    }


def serialise_attendance(doc: dict) -> dict:
    """Convert a raw MongoDB attendance_log document to the API AttendanceRecord schema."""
    return {
        "emp_code":          doc["emp_code"],
        "date":              doc["date"],
        "status":            doc["status"],
        "punch_in":          _dt_to_ms(doc.get("punch_in")),
        "punch_out":         _dt_to_ms(doc.get("punch_out")),
        "work_hours":        doc.get("work_hours"),
        "late_minutes":      doc.get("late_minutes", 0),
        "overtime_minutes":  doc.get("overtime_minutes", 0),
        "half_day":          doc.get("half_day", False),
        "history":           _serialise_history(doc.get("history", [])),
    }


# ---------------------------------------------------------------------------
# Custom Pydantic types
# ---------------------------------------------------------------------------
EpochMillis = Annotated[
    int,
    Field(strict=True, ge=_EPOCH_MIN, le=_EPOCH_MAX, description="Epoch milliseconds"),
]

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
_SHIFT_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
_EMPCODE_PATTERN = r"^EMP\d{4,6}$"
_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class EmployeeIn(BaseModel):
    emp_code:    str = Field(pattern=_EMPCODE_PATTERN)
    name:        str = Field(min_length=1, max_length=100)
    email:       str = Field(max_length=120, pattern=_EMAIL_PATTERN)
    department:  str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=_SHIFT_PATTERN)
    shift_end:   str = Field(default="18:30", pattern=_SHIFT_PATTERN)
    joined_on:   str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_employee(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date (YYYY-MM-DD)")
        return self


class PunchInIn(BaseModel):
    emp_code:   str
    punched_at: Optional[EpochMillis] = None
    status:     Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code:   str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    reason:         str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status:         Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in:       Optional[EpochMillis] = None
    punch_out:      Optional[EpochMillis] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for field in ("status", "punch_in", "punch_out"):
                if field in data and data[field] is None:
                    raise ValueError(f"{field} cannot be null")
        return data


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """
    D-01: ping MongoDB and ensure required indexes exist;
    503 if unreachable or index creation fails.
    """
    try:
        client.admin.command("ping")
        ensure_indexes()
        return {"status": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"detail": "MongoDB unavailable"})


# ---- Employees ------------------------------------------------------------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    """
    D-15: use UTC-aware datetime for created_at.
    D-16: serialise created_at as epoch ms in the response.
    D-20: shift_start != shift_end validated by model.
    D-21: emp_code pattern validated by model.
    D-08 equivalent: unique index ensures concurrent duplicate → one 201, rest 409.
    """
    ensure_indexes()
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return serialise_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page:       int           = Query(default=1, ge=1),
    page_size:  int           = Query(default=20, ge=1, le=100),  # D-23: max 100
):
    """
    D-02: correct skip = (page-1)*page_size.
    D-03: count_documents uses the same filter as the query.
    D-04: sorted by emp_code ascending.
    D-23: page_size capped at 100 (R10).
    """
    ensure_indexes()
    q = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)        # D-03 fixed
    skip  = (page - 1) * page_size                 # D-02 fixed
    items = list(
        db.employees
        .find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)               # D-04 fixed
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items":     [serialise_employee(i) for i in items],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


# ---- Attendance: punch-in -------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    """
    D-05: UTC-aware timestamps throughout.
    D-06: explicit 404 for unknown employee.
    D-07: IST attendance date with R1 overnight-shift rule.
    D-08: atomic insert; DuplicateKeyError → 409 (no check-then-insert).
    D-12: corrected late_minutes computation (seconds threshold + IST reference).
    D-19: punched_at validated as epoch-ms by EpochMillis type.
    D-24: no spurious 'id' field in response.
    """
    ensure_indexes()
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:                                    # D-06 fixed
        raise HTTPException(404, "employee not found")

    # Build truncated UTC-aware punch-in datetime (R1: truncate to seconds)
    if body.punched_at is not None:
        ts = from_ms(body.punched_at)              # D-05 fixed
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])  # D-07 fixed

    doc = {
        "emp_code":         body.emp_code,
        "date":             d,
        "status":           body.status,
        "punch_in":         ts,
        "punch_out":        None,
        "work_hours":       None,
        "late_minutes":     compute_late_minutes(ts, emp["shift_start"], d),  # D-12 fixed
        "overtime_minutes": 0,
        "half_day":         False,
        "history":          [],
    }

    try:
        db.attendance_logs.insert_one(doc)         # D-08 fixed: no pre-check
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    doc.pop("_id", None)
    return serialise_attendance(doc)              # D-16/D-24 fixed: epoch ms, no id


# ---- Attendance: punch-out ------------------------------------------------

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    """
    Record a punch-out.
    - Closes employee's most recent record whose punch_in <= punched_at.
    - Works across midnight for overnight shifts.
    - 404 if employee unknown or no punch-in found.
    - 409 if record is already punched out (atomic update guarantees concurrency safety).
    - 422 if punched_at <= punch_in or punched_at > punch_in + 24 hours.
    - Computes work_hours (R4), overtime_minutes (R3), half_day (R5).
    - No history appended.
    """
    ensure_indexes()
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts = from_ms(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the employee's most recent record with punch_in non-null and punch_in <= ts
    rec = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts, "$ne": None},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")

    punch_in_dt = ensure_utc(rec["punch_in"])
    if ts <= punch_in_dt:
        raise HTTPException(422, "punched_at must be strictly after punch_in")

    if (ts - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out for this record")

    work_hours = compute_work_hours(punch_in_dt, ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], rec["date"])
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": rec["date"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "already punched out for this record")

    updated.pop("_id", None)
    return serialise_attendance(updated)


# ---- Attendance: list ------------------------------------------------------

_DATE_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _validate_month_param(value: str) -> None:
    if not isinstance(value, str) or not _MONTH_PATTERN.match(value):
        raise HTTPException(422, "month must follow exact YYYY-MM format")


def _validate_date_param(param_name: str, value: Optional[str]) -> None:
    """Validate exact YYYY-MM-DD format and valid calendar date."""
    if value is None:
        return
    if not _DATE_PATTERN.match(value):
        raise HTTPException(422, f"{param_name} must follow exact YYYY-MM-DD format")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{param_name} is not a valid calendar date")


def _build_attendance_list_query(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[str] = None,
    page:      int = 1,
    page_size: int = 20,
) -> tuple[str, dict, list, int, int]:
    """Build query filter, sort, skip and limit for attendance_list."""
    _validate_date_param("date_from", date_from)
    _validate_date_param("date_to", date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    sort_spec = [("date", DESCENDING), ("emp_code", ASCENDING)]
    skip = (page - 1) * page_size
    limit = page_size
    return "attendance_logs", q, sort_spec, skip, limit


@app.get("/attendance")
def list_attendance(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page:      int           = Query(default=1, ge=1),
    page_size: int           = Query(default=20, ge=1, le=100),  # D-23: max 100
):
    """
    D-09: DB-side sort/skip/limit; never loads collection into memory.
    D-10: sort by date desc, emp_code asc.
    D-11: total from count_documents(q).
    D-17: no spurious 'id' field; _id projected out.
    D-22: date_from > date_to → 422.
    D-23: page_size capped at 100.
    """
    ensure_indexes()
    coll, q, sort_spec, skip, limit = _build_attendance_list_query(
        emp_code=emp_code,
        date_from=date_from,
        date_to=date_to,
        status=status,
        page=page,
        page_size=page_size,
    )

    total = db[coll].count_documents(q)  # D-11 fixed

    docs = list(
        db[coll]
        .find(q, {"_id": 0})                       # D-17 fixed: _id excluded
        .sort(sort_spec)                           # D-10 fixed
        .skip(skip)                                # D-09 fixed: DB-side
        .limit(limit)
    )

    return {
        "items":     [serialise_attendance(d) for d in docs],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


# ---- Attendance: regularize ------------------------------------------------

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    """
    Regularize (manually correct) one day's record.
    - Validates path date format.
    - 404 if employee does not exist or has no record for that date.
    - 422 if setting ABSENT/LEAVE while supplying punch_in or punch_out.
    - 422 if presence status lacks a punch_in (supplied or stored).
    - 422 if punch_in does not belong to the record's date under R1.
    - 422 if punch_out is not after punch_in or exceeds 24 hours.
    - 422 if request changes nothing.
    - Recomputes late_minutes, work_hours, overtime_minutes, half_day.
    - Appends exactly one history entry to history audit trail.
    - Optimistic concurrency control via history size check ensures 409 on lost race.
    """
    _validate_date_param("date", date)
    ensure_indexes()

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "attendance record not found")

    target_status = body.status if body.status is not None else rec["status"]

    cur_punch_in = ensure_utc(rec.get("punch_in"))
    cur_punch_out = ensure_utc(rec.get("punch_out"))

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch times cannot be supplied when status is ABSENT or LEAVE")
        new_punch_in = None
        new_punch_out = None
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_punch_in = from_ms(body.punch_in) if body.punch_in is not None else cur_punch_in
        if new_punch_in is None:
            raise HTTPException(422, "presence status requires a punch_in")

        if attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in does not belong to this attendance date under R1")

        new_punch_out = from_ms(body.punch_out) if body.punch_out is not None else cur_punch_out
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, "punch_out must be strictly after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Detect fields that actually changed
    changes = {}
    if target_status != rec["status"]:
        changes["status"] = {"from": rec["status"], "to": target_status}
    if new_punch_in != cur_punch_in:
        changes["punch_in"] = {"from": cur_punch_in, "to": new_punch_in}
    if new_punch_out != cur_punch_out:
        changes["punch_out"] = {"from": cur_punch_out, "to": new_punch_out}
    if new_late_minutes != rec.get("late_minutes", 0):
        changes["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": new_late_minutes}
    if new_work_hours != rec.get("work_hours"):
        changes["work_hours"] = {"from": rec.get("work_hours"), "to": new_work_hours}
    if new_overtime_minutes != rec.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": rec.get("overtime_minutes", 0), "to": new_overtime_minutes}
    if new_half_day != rec.get("half_day", False):
        changes["half_day"] = {"from": rec.get("half_day", False), "to": new_half_day}

    if not changes:
        raise HTTPException(422, "request changes nothing")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    cur_history_len = len(rec.get("history", []))
    match_filter = {
        "emp_code": emp_code,
        "date": date,
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes"),
        "overtime_minutes": rec.get("overtime_minutes"),
        "half_day": rec.get("half_day"),
        "history": {"$size": cur_history_len},
    }
    updated = db.attendance_logs.find_one_and_update(
        match_filter,
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "concurrent modification conflict on attendance record")

    updated.pop("_id", None)
    return serialise_attendance(updated)


# ---------------------------------------------------------------------------
# Phase 3: Analytics Endpoints
# ---------------------------------------------------------------------------

def _build_employee_monthly_pipeline(emp_code: str, month: str) -> tuple[str, list[dict], int, int]:
    """Build aggregation pipeline for employee_monthly endpoint."""
    _validate_month_param(month)
    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    start_str = f"{month}-01"
    end_str = f"{month}-{num_days:02d}"

    weekday_dates = [
        f"{year:04d}-{m_num:02d}-{d:02d}"
        for d in range(1, num_days + 1)
        if date(year, m_num, d).weekday() < 5
    ]

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_str, "$lte": end_str},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$date", weekday_dates]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]
    return "attendance_logs", pipeline, year, m_num


@app.get("/analytics/employees/{emp_code}/monthly")
def get_employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee backed by MongoDB aggregation.
    """
    ensure_indexes()
    if hasattr(month, "default"):
        month = month.default
    _validate_month_param(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, f"employee '{emp_code}' not found")

    coll, pipeline, year, m_num = _build_employee_monthly_pipeline(emp_code, month)
    num_days = calendar.monthrange(year, m_num)[1]

    # Calculate working_days (Mon-Fri) per R7
    emp_joined = date.fromisoformat(emp["joined_on"])
    first_month_day = date(year, m_num, 1)
    last_month_day = date(year, m_num, num_days)
    eff_start = max(first_month_day, emp_joined)

    if eff_start > last_month_day:
        working_days = 0
    else:
        working_days = sum(
            1 for i in range((last_month_day - eff_start).days + 1)
            if (eff_start + timedelta(days=i)).weekday() < 5
        )

    res = list(db[coll].aggregate(pipeline))
    if res:
        row = res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if present_days.is_integer():
        present_days = int(present_days)

    if working_days == 0:
        attendance_pct = None
    else:
        pct_dec = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal("100")
        attendance_pct = float(pct_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def _build_department_summary_pipeline(month: str, department: Optional[str] = None) -> tuple[str, list[dict]]:
    """Build aggregation pipeline for department_summary endpoint."""
    _validate_month_param(month)
    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    start_str = f"{month}-01"
    end_str = f"{month}-{num_days:02d}"

    weekday_dates = [
        f"{year:04d}-{m_num:02d}-{d:02d}"
        for d in range(1, num_days + 1)
        if date(year, m_num, d).weekday() < 5
    ]

    match_stage: dict = {"joined_on": {"$lte": end_str}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "month_logs": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$gte": ["$$l.date", start_str]},
                                {"$lte": ["$$l.date", end_str]},
                            ]
                        },
                    }
                },
            }
        },
        {
            "$unwind": {
                "path": "$month_logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$month_logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$month_logs.date", weekday_dates]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$month_logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$month_logs.late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$month_logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$month_logs.status", "LEAVE"]}, 1, 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$month_logs.status", "ON_DUTY"]}, 1, 0]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$month_logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$month_logs.work_hours", None]},
                                ]
                            },
                            "$month_logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$month_logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$month_logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]
    return "employees", pipeline


@app.get("/analytics/departments/summary")
def get_department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
):
    """
    Department summary for a month via MongoDB aggregation across both collections.
    """
    ensure_indexes()
    if hasattr(month, "default"):
        month = month.default
    if hasattr(department, "default"):
        department = department.default
    _validate_month_param(month)

    coll, pipeline = _build_department_summary_pipeline(month, department)

    res = list(db[coll].aggregate(pipeline))
    items = []
    for row in res:
        headcount = len(row.get("emp_codes", []))
        if headcount == 0:
            continue

        present_days = float(row.get("present_days", 0.0))
        if present_days.is_integer():
            present_days = int(present_days)

        wh_count = int(row.get("work_hours_count", 0))
        if wh_count == 0:
            avg_work_hours = None
        else:
            avg_dec = Decimal(str(row.get("work_hours_sum", 0.0))) / Decimal(str(wh_count))
            avg_work_hours = float(avg_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        items.append({
            "department": row["_id"],
            "headcount": headcount,
            "present_days": present_days,
            "avg_work_hours": avg_work_hours,
            "late_count": int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "leave_count": int(row.get("leave_count", 0)),
            "on_duty_count": int(row.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


def _build_late_leaderboard_pipeline(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
) -> tuple[str, list[dict], str, str]:
    """Build aggregation pipeline for late_leaderboard endpoint."""
    _validate_month_param(month)
    if not isinstance(limit, int) or limit < 1 or limit > 50:
        raise HTTPException(422, "limit must be between 1 and 50")
    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    start_str = f"{month}-01"
    end_str = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_str, "$lte": end_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ])
    return "attendance_logs", pipeline, start_str, end_str


@app.get("/analytics/leaderboard/late")
def get_late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = Query(default=None),
):
    """
    Ranks employees by total late_minutes in the month (only total > 0).
    Standard competition ranking (1, 2, 2, 4). limit applies after ranking.
    """
    ensure_indexes()
    if hasattr(month, "default"):
        month = month.default
    if hasattr(limit, "default"):
        limit = limit.default
    if hasattr(department, "default"):
        department = department.default
    _validate_month_param(month)
    if not isinstance(limit, int) or limit < 1 or limit > 50:
        raise HTTPException(422, "limit must be between 1 and 50")

    coll, pipeline, start_str, end_str = _build_late_leaderboard_pipeline(month, limit, department)

    _is_mock = type(db).__module__.startswith("mongomock")
    if not _is_mock:
        raw_items = list(db.attendance_logs.aggregate(pipeline))
    else:
        try:
            raw_items = list(db.attendance_logs.aggregate(pipeline))
        except NotImplementedError:
            # Compatibility path strictly for test environments using mongomock lacking $setWindowFields
            base_pipe = [
                {
                    "$match": {
                        "date": {"$gte": start_str, "$lte": end_str},
                        "late_minutes": {"$gt": 0},
                    }
                },
                {
                    "$group": {
                        "_id": "$emp_code",
                        "total_late_minutes": {"$sum": "$late_minutes"},
                        "late_count": {"$sum": 1},
                    }
                },
                {
                    "$lookup": {
                        "from": "employees",
                        "localField": "_id",
                        "foreignField": "emp_code",
                        "as": "emp",
                    }
                },
                {"$unwind": "$emp"},
            ]
            if department is not None:
                base_pipe.append({"$match": {"emp.department": department}})
            base_pipe.append({
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            })
            unranked = list(db.attendance_logs.aggregate(base_pipe))
            unranked.sort(key=lambda x: (-x["total_late_minutes"], x["emp_code"]))
            raw_items = []
            cur_rank = 1
            last_late = None
            for idx, doc in enumerate(unranked):
                if last_late is not None and doc["total_late_minutes"] == last_late:
                    doc["rank"] = cur_rank
                else:
                    cur_rank = idx + 1
                    doc["rank"] = cur_rank
                    last_late = doc["total_late_minutes"]
                if doc["rank"] <= limit:
                    raw_items.append(doc)

    items = []
    for doc in raw_items:
        items.append({
            "rank": int(doc["rank"]),
            "emp_code": doc["emp_code"],
            "name": doc["name"],
            "department": doc["department"],
            "total_late_minutes": int(doc["total_late_minutes"]),
            "late_count": int(doc["late_count"]),
        })

    return {"month": month, "items": items}


def _build_department_trend_pipeline(
    department: str,
    from_date: str,
    to_date: str,
) -> tuple[str, list[dict], date, date, int]:
    """Build aggregation pipeline for department_trend endpoint."""
    _validate_date_param("from", from_date)
    _validate_date_param("to", to_date)

    from_d = date.fromisoformat(from_date)
    to_d = date.fromisoformat(to_date)

    if to_d < from_d:
        raise HTTPException(422, "'to' date cannot be before 'from' date")

    num_days = (to_d - from_d).days + 1
    if num_days > 92:
        raise HTTPException(422, "date range exceeds 92 days")

    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    to_dt_plus_1 = (datetime.fromisoformat(to_date) + timedelta(days=1)).replace(tzinfo=timezone.utc)

    # Full MongoDB aggregation pipeline using $densify and $setWindowFields
    pipeline = [
        # 1. Match attendance logs in the date range
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        # 2. Join with employees to filter by department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        # 3. Group matching records by date
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
            }
        },
        # 4. Union with boundary dates to guarantee lower and upper bounds for $densify
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "boundaries": [from_date, to_date],
                        }
                    },
                    {"$unwind": "$boundaries"},
                    {
                        "$project": {
                            "_id": "$boundaries",
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        # 5. Group again so boundary dates merge with existing date logs
        {
            "$group": {
                "_id": "$_id",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        # 6. Convert string date to BSON Date for $densify
        {
            "$addFields": {
                "date_d": {
                    "$dateFromString": {"dateString": "$_id"}
                }
            }
        },
        # 7. Densify requested date range so every calendar day is represented
        {
            "$densify": {
                "field": "date_d",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        # 8. Project date string back, coalescing missing values from densified days
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$date_d"}
                },
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"date": 1}},
        # 9. Determine is_working_day (Mon-Fri = True)
        {
            "$addFields": {
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6],
                    ]
                }
            }
        },
        # 10. Correlated lookup to calculate dynamic headcount (employees with joined_on <= date)
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$current_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]}
            }
        },
        # 11. Calculate attendance_rate as Decimal128 half-up to 4 decimals (null on weekends or headcount == 0)
        {
            "$addFields": {
                "attendance_rate_dec": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            {"$toDecimal": "$present_count"},
                                                            {"$toDecimal": "$headcount"},
                                                        ]
                                                    },
                                                    {"$toDecimal": 10000},
                                                ]
                                            },
                                            {"$toDecimal": "0.5"},
                                        ]
                                    }
                                },
                                {"$toDecimal": 10000},
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        # 12. 7-day moving average using MongoDB window aggregation over Decimal128 rates
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d_raw_dec": {
                        "$avg": "$attendance_rate_dec",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        # 13. Round moving_avg_7d half-up to 4 decimals in Decimal128
        {
            "$addFields": {
                "moving_avg_7d_dec": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d_raw_dec", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$moving_avg_7d_raw_dec",
                                                    {"$toDecimal": 10000},
                                                ]
                                            },
                                            {"$toDecimal": "0.5"},
                                        ]
                                    }
                                },
                                {"$toDecimal": 10000},
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        # 14. Project final API document, converting Decimal128 to double only at API boundary
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$ne": ["$attendance_rate_dec", None]},
                        {"$toDouble": "$attendance_rate_dec"},
                        None,
                    ]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d_dec", None]},
                        {"$toDouble": "$moving_avg_7d_dec"},
                        None,
                    ]
                },
            }
        },
    ]
    return "attendance_logs", pipeline, from_d, to_d, num_days


@app.get("/analytics/departments/{department}/trend")
def get_department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    """
    Daily attendance trend for a department with gap-filling ($densify) and 7-day moving average.

    Pipeline stages defined in _build_department_trend_pipeline:
    - "$densify": densifies calendar dates across date range
    - "$setWindowFields": calculates "$avg": "$attendance_rate_dec" with "window": [-6, "current"]
    - "attendance_rate_dec": calculated using "$toDecimal": half-up rounding
    - "moving_avg_7d_dec": rounded using "$toDecimal": half-up rounding
    - Projected via "$toDouble": at API boundary
    """
    ensure_indexes()
    if hasattr(from_date, "default"):
        from_date = from_date.default
    if hasattr(to_date, "default"):
        to_date = to_date.default
    dept_count = db.employees.count_documents({"department": department})
    if dept_count == 0:
        raise HTTPException(404, f"department '{department}' not found")

    coll, pipeline, from_d, to_d, num_days = _build_department_trend_pipeline(department, from_date, to_date)

    _is_mock = type(db).__module__.startswith("mongomock")
    if not _is_mock:
        raw_items = list(db.attendance_logs.aggregate(pipeline))
    else:
        try:
            raw_items = list(db.attendance_logs.aggregate(pipeline))
        except NotImplementedError:
            # Compatibility path strictly for test environments using mongomock
            log_pipe = [
                {
                    "$match": {
                        "date": {"$gte": from_date, "$lte": to_date},
                    }
                },
                {
                    "$lookup": {
                        "from": "employees",
                        "localField": "emp_code",
                        "foreignField": "emp_code",
                        "as": "emp",
                    }
                },
                {"$unwind": "$emp"},
                {"$match": {"emp.department": department}},
                {
                    "$group": {
                        "_id": "$date",
                        "present_count": {
                            "$sum": {
                                "$cond": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            }
                        },
                        "late_count": {
                            "$sum": {
                                "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                            }
                        },
                    }
                },
            ]
            log_rows = list(db[coll].aggregate(log_pipe))
            logs_by_date = {r["_id"]: r for r in log_rows}

            # Query department employees' joined_on for headcount
            dept_emps_cur = db.employees.find({"department": department}, {"joined_on": 1, "_id": 0})
            dept_emps = list(dept_emps_cur)

            rates_dec = []
            raw_items = []
            for i in range(num_days):
                cur_d = from_d + timedelta(days=i)
                d_str = cur_d.isoformat()
                is_working_day = cur_d.weekday() < 5
                headcount = sum(1 for e in dept_emps if e["joined_on"] <= d_str)

                day_log = logs_by_date.get(d_str)
                present_count = float(day_log["present_count"]) if day_log else 0.0
                late_count = int(day_log["late_count"]) if day_log else 0

                if is_working_day and headcount > 0:
                    rate_dec = (Decimal(str(present_count)) / Decimal(str(headcount))).quantize(
                        Decimal("0.0001"), rounding=ROUND_HALF_UP
                    )
                else:
                    rate_dec = None

                rates_dec.append(rate_dec)

                window_start = max(0, i - 6)
                window_rates = [r for r in rates_dec[window_start : i + 1] if r is not None]
                if window_rates:
                    avg_dec = (sum(window_rates) / Decimal(len(window_rates))).quantize(
                        Decimal("0.0001"), rounding=ROUND_HALF_UP
                    )
                    moving_avg_7d = float(avg_dec)
                else:
                    moving_avg_7d = None

                raw_items.append({
                    "date": d_str,
                    "is_working_day": is_working_day,
                    "headcount": headcount,
                    "present_count": present_count,
                    "late_count": late_count,
                    "attendance_rate": float(rate_dec) if rate_dec is not None else None,
                    "moving_avg_7d": moving_avg_7d,
                })

    items = []
    for doc in raw_items:
        present_count = float(doc.get("present_count", 0.0))
        if present_count.is_integer():
            present_count = int(present_count)

        att_rate = doc.get("attendance_rate")
        if att_rate is not None:
            att_rate = float(Decimal(str(att_rate)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

        mov_avg = doc.get("moving_avg_7d")
        if mov_avg is not None:
            mov_avg = float(Decimal(str(mov_avg)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

        items.append({
            "date": doc["date"],
            "is_working_day": bool(doc["is_working_day"]),
            "headcount": int(doc["headcount"]),
            "present_count": present_count,
            "late_count": int(doc.get("late_count", 0)),
            "attendance_rate": att_rate,
            "moving_avg_7d": mov_avg,
        })

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Phase 4: Query Explanation Endpoint
# ---------------------------------------------------------------------------

ExplainEndpointName = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]


def _serialise_bson_value(val: Any) -> Any:
    """Recursively convert BSON and Decimal types to JSON-serialisable Python primitives."""
    if isinstance(val, dict):
        return {k: _serialise_bson_value(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_serialise_bson_value(x) for x in val]
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, Decimal128):
        d = val.to_decimal()
        return float(d) if "." in str(d) else int(d)
    if isinstance(val, Decimal):
        return float(val) if "." in str(val) else int(val)
    if isinstance(val, ObjectId):
        return str(val)
    if isinstance(val, Timestamp):
        return val.as_datetime().isoformat()
    if not isinstance(val, (int, float, str, bool, type(None))):
        return str(val)
    return val


def find_plan_stages(explain_data: Any) -> list[str]:
    """
    Recursively extract all stage names (e.g. IXSCAN, COLLSCAN, FETCH)
    present in an explain output document or stage tree.
    """
    stages: list[str] = []
    if isinstance(explain_data, dict):
        for k, v in explain_data.items():
            if k == "stage" and isinstance(v, str):
                stages.append(v)
            else:
                stages.extend(find_plan_stages(v))
    elif isinstance(explain_data, list):
        for item in explain_data:
            stages.extend(find_plan_stages(item))
    return stages


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpointName,
    emp_code: Optional[str] = Query(default=None),
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(default=None),
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = Query(default=None, alias="to"),
    page: Optional[int] = Query(default=1, ge=1),
    page_size: Optional[int] = Query(default=20, ge=1, le=100),
):
    """
    Phase 4: Runs MongoDB explain('executionStats') for the requested endpoint's
    main query or aggregation pipeline.
    Reuses the exact query/pipeline builder logic as the corresponding endpoint.
    Missing required parameters return HTTP 422.
    """
    if hasattr(month, "default"):
        month = month.default
    if hasattr(department, "default"):
        department = department.default
    if hasattr(emp_code, "default"):
        emp_code = emp_code.default
    if hasattr(limit, "default"):
        limit = limit.default
    if hasattr(date_from, "default"):
        date_from = date_from.default
    if hasattr(date_to, "default"):
        date_to = date_to.default
    if hasattr(status, "default"):
        status = status.default
    if hasattr(from_, "default"):
        from_ = from_.default
    if hasattr(to, "default"):
        to = to.default
    if hasattr(page, "default"):
        page = page.default
    if hasattr(page_size, "default"):
        page_size = page_size.default

    if endpoint == "attendance_list":
        coll, q, sort_spec, skip, limit_val = _build_attendance_list_query(
            emp_code=emp_code,
            date_from=date_from,
            date_to=date_to,
            status=status,
            page=page or 1,
            page_size=page_size or 20,
        )
        explain_cmd = {
            "find": coll,
            "filter": q,
            "projection": {"_id": 0},
            "sort": dict(sort_spec),
            "skip": skip,
            "limit": limit_val,
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(422, "month is required for employee_monthly")
        coll, pipeline, _, _ = _build_employee_monthly_pipeline(emp_code, month)
        explain_cmd = {
            "aggregate": coll,
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        coll, pipeline = _build_department_summary_pipeline(month, department)
        explain_cmd = {
            "aggregate": coll,
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        coll, pipeline, _, _ = _build_late_leaderboard_pipeline(month, limit or 10, department)
        explain_cmd = {
            "aggregate": coll,
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, "department is required for department_trend")
        from_val = from_ or date_from
        to_val = to or date_to
        if not from_val:
            raise HTTPException(422, "'from' date is required for department_trend")
        if not to_val:
            raise HTTPException(422, "'to' date is required for department_trend")
        coll, pipeline, _, _, _ = _build_department_trend_pipeline(department, from_val, to_val)
        explain_cmd = {
            "aggregate": coll,
            "pipeline": pipeline,
            "cursor": {},
        }

    else:
        raise HTTPException(422, f"unsupported endpoint '{endpoint}'")

    # Ensure required indexes exist before executing explain against MongoDB
    ensure_indexes()

    # Run explain command on MongoDB with executionStats verbosity
    raw_explain = db.command({
        "explain": explain_cmd,
        "verbosity": "executionStats",
    })

    serialised_explain = _serialise_bson_value(raw_explain)

    return {
        "endpoint": endpoint,
        "collection": coll,
        "explain": serialised_explain,
    }

