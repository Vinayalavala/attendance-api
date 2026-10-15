"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError, ServerSelectionTimeoutError

load_dotenv()  # override=False: real environment variables take priority
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration, database, indexes
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo hands back UTC-aware datetimes, so we never mix naive and aware values.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
NO_PUNCH = ("ABSENT", "LEAVE")
WEEKDAYS = [2, 3, 4, 5, 6]  # $dayOfWeek: 1 = Sunday ... 7 = Saturday  ->  Mon..Fri
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_LIMIT = Decimal("4.50")

# (collection, keys, options)
INDEXES = [
    # emp_code is the business key: uniqueness is guaranteed by the database, not by check-then-insert.
    ("employees", [("emp_code", 1)], {"unique": True, "name": "uq_emp_code"}),
    # GET /employees?department=  (sorted by emp_code) and the department trend's $match.
    ("employees", [("department", 1), ("emp_code", 1)], {"name": "dept_emp_code"}),
    # Headcount (R9): joined_on <= end of period, optionally inside one department.
    ("employees", [("joined_on", 1)], {"name": "joined_on"}),
    ("employees", [("department", 1), ("joined_on", 1)], {"name": "dept_joined_on"}),
    # At most one record per (emp_code, date): the punch-in race guard, the monthly summary,
    # the per-employee $lookup in the department summary, GET /attendance?emp_code=.
    ("attendance_logs", [("emp_code", 1), ("date", 1)], {"unique": True, "name": "uq_emp_date"}),
    # GET /attendance default sort (date desc, emp_code asc), date ranges, leaderboard and trend lookups.
    ("attendance_logs", [("date", -1), ("emp_code", 1)], {"name": "date_desc_emp"}),
    # GET /attendance?status=  with the same sort.
    ("attendance_logs", [("status", 1), ("date", -1), ("emp_code", 1)], {"name": "status_date_emp"}),
    # Punch-out: "latest record of this employee with punch_in <= t".
    ("attendance_logs", [("emp_code", 1), ("punch_in", -1)], {"name": "emp_punch_in"}),
]
_indexes_ready = False


def ensure_indexes() -> bool:
    """Idempotent. Never raises: a failing index must not stop the service from starting."""
    global _indexes_ready
    ok = True
    for coll, keys, opts in INDEXES:
        try:
            db[coll].create_index(keys, **opts)
        except ServerSelectionTimeoutError:
            return False
        except PyMongoError as exc:
            log.error("index %s on %s failed: %s", opts.get("name"), coll, exc)
            ok = False
    _indexes_ready = ok
    return ok


@asynccontextmanager
async def lifespan(_: FastAPI):
    deadline = time.monotonic() + 12
    while not ensure_indexes() and time.monotonic() < deadline:
        time.sleep(1)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def unprocessable(msg: str, loc: tuple = ("body",)):
    """Raise a 422 with FastAPI's default body shape ({"detail": [ ... ]})."""
    raise RequestValidationError([{"type": "value_error", "loc": list(loc), "msg": msg, "input": None}])


# --------------------------------------------------------------------------- #
# 2. Time and number helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: BSON dates are always UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def trunc(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def now_utc() -> datetime:
    """Server clock, whole seconds (used for punches that omit punched_at)."""
    return trunc(datetime.now(UTC))


def now_ms_precision() -> datetime:
    """Server clock at the precision MongoDB stores (milliseconds), so API and DB agree."""
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def hm_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(emp: dict) -> bool:
    return hm_minutes(emp["shift_end"]) <= hm_minutes(emp["shift_start"])


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: IST calendar date of the punch-in; overnight shifts: before shift_end -> previous day."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(emp):
        seconds_of_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_of_day < hm_minutes(emp["shift_end"]) * 60:
            d -= timedelta(days=1)
    return d.isoformat()


def _shift_instant(date_str: str, hhmm: str) -> datetime:
    d = Date.fromisoformat(date_str)
    h, m = hhmm.split(":")
    return datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=IST)


def shift_start_at(date_str: str, emp: dict) -> datetime:
    return _shift_instant(date_str, emp["shift_start"])


def shift_end_at(date_str: str, emp: dict) -> datetime:
    end = _shift_instant(date_str, emp["shift_end"])
    return end + timedelta(days=1) if is_overnight(emp) else end


def seconds_between(a: datetime, b: datetime) -> int:
    """Whole seconds from a to b (both already truncated to seconds)."""
    return (b - a) // timedelta(seconds=1)


def compute_late_minutes(punch_in: datetime, date_str: str, emp: dict) -> int:
    """R2: strictly more than 10:00 after shift_start -> whole minutes since shift_start, else 0."""
    secs = seconds_between(shift_start_at(date_str, emp), punch_in)
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600 rounded to 2 decimals HALF-UP, computed exactly with Decimal."""
    secs = seconds_between(punch_in, punch_out)
    return (Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_overtime(punch_out: datetime, date_str: str, emp: dict) -> int:
    """R3: whole minutes after shift_end, counted only when >= 30."""
    secs = seconds_between(shift_end_at(date_str, emp), punch_out)
    minutes = secs // 60 if secs > 0 else 0
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive(emp: dict, date_str: str, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All four derived fields from the final status and (second-truncated) punch times."""
    out = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return out
    out["late_minutes"] = compute_late_minutes(punch_in, date_str, emp)
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)  # already rounded half-up
        out["work_hours"] = float(hours)
        out["half_day"] = hours < HALF_DAY_LIMIT  # R5 uses the ROUNDED value
        out["overtime_minutes"] = compute_overtime(punch_out, date_str, emp)
    return out


def half_up(numerator: int, denominator: int, places: str) -> float:
    """numerator / denominator rounded half-up, exactly (R8)."""
    q = Decimal(numerator) / Decimal(denominator)
    return float(q.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def month_bounds(month: str) -> tuple[Date, Date]:
    y, m = int(month[:4]), int(month[5:7])
    return Date(y, m, 1), Date(y, m, calendar.monthrange(y, m)[1])


def count_weekdays(first: Date, last: Date) -> int:
    """Mon-Fri days in [first, last] (R7)."""
    if first > last:
        return 0
    days = (last - first).days + 1
    full_weeks, extra = divmod(days, 7)
    count = full_weeks * 5
    for i in range(extra):
        if (first + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# 3. Validated scalar types and request models
# --------------------------------------------------------------------------- #
def _check_date(v: str) -> str:
    try:
        Date.fromisoformat(v)
    except ValueError:
        raise ValueError("not a valid calendar date")
    return v


def _check_month(v: str) -> str:
    if int(v[:4]) < 1:
        raise ValueError("not a valid month")
    return v


DateStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_check_date)]
MonthStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"), AfterValidator(_check_month)]
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
# Epoch MILLISECONDS only: StrictInt rejects floats / strings / bools; the lower bound rejects seconds.
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")  # created_at etc. from a client are ignored
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: DateStr

    @field_validator("name", "department")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")  # derived fields sent by a client are ignored
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")  # work_hours, late_minutes ... are ignored if sent
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 4. Serialisation: BSON -> API
# --------------------------------------------------------------------------- #
def serialize_employee(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": dt_to_ms(d.get("created_at")),
    }


def _history_value(v: Any) -> Any:
    return dt_to_ms(v) if isinstance(v, datetime) else v


def serialize_log(d: dict) -> dict:
    """Never exposes _id; fills the defaults for legacy documents (missing half_day / history ...)."""
    history = []
    for h in d.get("history") or []:
        history.append(
            {
                "at": dt_to_ms(h.get("at")),
                "by": h.get("by"),
                "reason": h.get("reason"),
                "changes": {
                    k: {"from": _history_value(c.get("from")), "to": _history_value(c.get("to"))}
                    for k, c in (h.get("changes") or {}).items()
                },
            }
        )
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")),
        "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": int(d.get("late_minutes") or 0),
        "overtime_minutes": int(d.get("overtime_minutes") or 0),
        "half_day": bool(d.get("half_day", False)),
        "history": history,
    }


SHIFT_PROJECTION = {"_id": 0, "emp_code": 1, "shift_start": 1, "shift_end": 1, "joined_on": 1, "department": 1}


def get_employee_or_404(emp_code: str, projection: Optional[dict] = None) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, projection or SHIFT_PROJECTION)
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 5. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    if not _indexes_ready:
        ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_ms_precision()
    try:
        db.employees.insert_one(doc)  # the unique index decides; no check-then-insert race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    search: Optional[str] = Query(default=None, description="Case-insensitive search across employee code, name, email, and department"),
    page: Page = 1,
    page_size: PageSize = 20,
):
    q: dict = {}
    if department is not None:
        q["department"] = department

    # Escape user input so characters such as "." or "*" are treated literally.
    # MongoDB's "i" option makes matching case-insensitive (e.g. VINAY == Vinay == vinay).
    if search is not None and search.strip():
        term = re.escape(search.strip().lower())
        q["$or"] = [
            {"emp_code": {"$regex": term, "$options": "i"}},
            {"name": {"$regex": term, "$options": "i"}},
            {"email": {"$regex": term, "$options": "i"}},
            {"department": {"$regex": term, "$options": "i"}},
        ]

    total = db.employees.count_documents(q)  # count only the filtered results
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    date_str = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, date_str, body.status, ts, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one of N racers wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_log(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent record whose punch_in <= punched_at (works across midnight: no date arithmetic).
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = trunc(rec["punch_in"])
    delta = seconds_between(p_in, ts)
    if delta <= 0:
        unprocessable("punched_at must be after punch_in", ("body", "punched_at"))
    if delta > 24 * 3600:
        unprocessable("punched_at is more than 24 hours after punch_in", ("body", "punched_at"))
    d = derive(emp, rec["date"], rec["status"], p_in, ts)
    # Compare-and-set on punch_out: of two simultaneous requests only one matches.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": {"punch_out": ts, "work_hours": d["work_hours"], "overtime_minutes": d["overtime_minutes"], "half_day": d["half_day"]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_log(updated)


def attendance_list_query(emp_code, date_from, date_to, status, page, page_size):
    """Shared by GET /attendance and its explain: (filter, sort, skip, limit)."""
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q, [("date", -1), ("emp_code", 1)], (page - 1) * page_size, page_size


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if date_from is not None and date_to is not None and date_from > date_to:
        unprocessable("date_from must not be after date_to", ("query", "date_from"))
    q, sort, skip, limit = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q, {"_id": 0}).sort(sort).skip(skip).limit(limit)  # paging in the DB
    return {"items": [serialize_log(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


_CHANGE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def plan_regularization(emp: dict, rec: dict, body: RegularizeIn) -> tuple[dict, dict]:
    """Returns (new field values, changes). Raises 422 for every rule violation."""
    cur_status, cur_in, cur_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or cur_status

    if new_status in NO_PUNCH:
        if body.punch_in is not None or body.punch_out is not None:
            unprocessable(f"status {new_status} clears punch times; do not supply punch_in/punch_out")
        fin = fout = None
    else:
        fin = ms_to_dt(body.punch_in) if body.punch_in is not None else cur_in
        if fin is None:
            unprocessable("a presence status requires a punch_in")
        fout = ms_to_dt(body.punch_out) if body.punch_out is not None else cur_out
        calc_in = trunc(fin)
        if attendance_date(calc_in, emp) != rec["date"]:
            unprocessable("punch_in must stay on the record's attendance date", ("body", "punch_in"))
        if fout is not None:
            delta = seconds_between(calc_in, trunc(fout))
            if delta <= 0 or delta > 24 * 3600:
                unprocessable("punch_out must be after punch_in and within 24 hours", ("body", "punch_out"))

    derived = derive(emp, rec["date"], new_status, trunc(fin) if fin else None, trunc(fout) if fout else None)
    new = {"status": new_status, "punch_in": fin, "punch_out": fout, **derived}
    old = {
        "status": cur_status,
        "punch_in": cur_in,
        "punch_out": cur_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": int(rec.get("late_minutes") or 0),
        "overtime_minutes": int(rec.get("overtime_minutes") or 0),
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old[k], "to": new[k]} for k in _CHANGE_FIELDS if old[k] != new[k]}
    if not changes:
        unprocessable("the request changes nothing")
    return new, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = get_employee_or_404(emp_code)
    for _ in range(4):  # optimistic concurrency: re-read and re-plan if somebody got in first
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        new, changes = plan_regularization(emp, rec, body)
        entry = {"at": now_ms_precision(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        guard = {"_id": rec["_id"], "status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out")}
        guard["history"] = {"$size": len(rec["history"])} if "history" in rec else {"$exists": False}
        updated = db.attendance_logs.find_one_and_update(
            guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
        )
        if updated is not None:
            return serialize_log(updated)
    raise HTTPException(409, "record was modified concurrently; retry")


# --------------------------------------------------------------------------- #
# 6. Analytics: pipelines (built once, shared with the explain endpoint)
# --------------------------------------------------------------------------- #
def _dow(path: str = "$date") -> dict:
    return {"$dayOfWeek": {"$dateFromString": {"dateString": path, "format": "%Y-%m-%d", "onError": None}}}


def _present_halves(p: str, weekdays_only: bool) -> dict:
    """Half-units of presence for one record: full day = 2, half day = 1, otherwise 0.
    Integers keep every later division exact. p is the field prefix ("$" or "$logs.")."""
    present = {"$in": [p + "status", PRESENCE]}
    cond = {"$and": [present, {"$in": [p + "dow", WEEKDAYS]}]} if weekdays_only else present
    is_half = {"$eq": [{"$ifNull": [p + "half_day", False]}, True]}
    return {"$cond": [cond, {"$cond": [is_half, 1, 2]}, 0]}


def _late_flag(p: str) -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": [p + "late_minutes", 0]}, 0]}, 1, 0]}


def _late_minutes(p: str) -> dict:
    return {"$ifNull": [p + "late_minutes", 0]}


def _hu_div(num: Any, den: Any) -> dict:
    """floor((2*num + den) / (2*den)) = num / den rounded HALF-UP, for non-negative integers.
    ($round in MongoDB is banker's rounding, which R8 forbids.)"""
    return {"$floor": {"$divide": [{"$add": [{"$multiply": [2, num]}, den]}, {"$multiply": [2, den]}]}}


def monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$addFields": {"dow": _dow()}},
        {
            "$group": {
                "_id": None,
                "present_halves": {"$sum": _present_halves("$", True)},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": _late_flag("$")},
                "total_late_minutes": {"$sum": _late_minutes("$")},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    """Starts on employees (so people with zero logs still count) and joins the month's logs."""
    match: dict = {"joined_on": {"$lte": end}}  # R9
    if department is not None:
        match["department"] = department
    has_hours = {"$and": [{"$in": ["$logs.status", PRESENCE]}, {"$isNumber": "$logs.work_hours"}]}
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "work_hours": 1}},
                    {"$addFields": {"dow": _dow()}},
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "emps": {"$addToSet": "$emp_code"},
                "present_halves": {"$sum": _present_halves("$logs.", True)},
                # work_hours are 2-decimal numbers: summing them as integer hundredths keeps the mean exact.
                "wh_cents": {"$sum": {"$cond": [has_hours, {"$round": [{"$multiply": ["$logs.work_hours", 100]}, 0]}, 0]}},
                "wh_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
                "late_count": {"$sum": _late_flag("$logs.")},
                "total_late_minutes": {"$sum": _late_minutes("$logs.")},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emps"},
                "present_halves": 1,
                "avg_cents": {"$cond": [{"$gt": ["$wh_n", 0]}, _hu_div("$wh_cents", "$wh_n"), None]},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def trend_pipeline(department: str, first: Date, last: Date) -> list:
    """One row per calendar day. The calendar is generated in the database ($range + $unwind) from the
    department's employees, so every day appears even when there are no logs at all."""
    n_days = (last - first).days + 1
    first_dt = datetime(first.year, first.month, first.day, tzinfo=UTC)
    window = {"documents": [-6, 0]}
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joins": {"$push": "$joined_on"}}},
        {"$project": {"_id": 0, "codes": 1, "joins": 1, "offset": {"$range": [0, n_days]}}},
        {"$unwind": "$offset"},
        {"$addFields": {"dt": {"$add": [first_dt, {"$multiply": ["$offset", 86400000]}]}}},
        {"$addFields": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt"}}, "dow": {"$dayOfWeek": "$dt"}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date", "codes": "$codes"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$d"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
                    {"$group": {"_id": None, "halves": {"$sum": _present_halves("$", False)}, "late": {"$sum": _late_flag("$")}}},
                ],
                "as": "agg",
            }
        },
        {
            "$addFields": {
                "is_wd": {"$in": ["$dow", WEEKDAYS]},
                "headcount": {"$size": {"$filter": {"input": "$joins", "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}},
                "halves": {"$ifNull": [{"$arrayElemAt": ["$agg.halves", 0]}, 0]},
                "late": {"$ifNull": [{"$arrayElemAt": ["$agg.late", 0]}, 0]},
            }
        },
        {
            "$addFields": {
                # attendance_rate in units of 1e-4, rounded half-up: halves/2/headcount * 10000
                "rate_n": {
                    "$cond": [
                        {"$and": ["$is_wd", {"$gt": ["$headcount", 0]}]},
                        _hu_div({"$multiply": ["$halves", 10000]}, {"$multiply": [2, "$headcount"]}),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"dt": 1},
                "output": {
                    "rate_sum": {"$sum": "$rate_n", "window": window},
                    "rate_cnt": {"$sum": {"$cond": [{"$isNumber": "$rate_n"}, 1, 0]}, "window": window},
                },
            }
        },
        {"$sort": {"dt": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": "$is_wd",
                "headcount": 1,
                "halves": 1,
                "late": 1,
                "rate_n": 1,
                "moving_n": {"$cond": [{"$gt": ["$rate_cnt", 0]}, _hu_div("$rate_sum", "$rate_cnt"), None]},
            }
        },
    ]


def validate_trend_range(first: str, last: str) -> tuple[Date, Date]:
    d1, d2 = Date.fromisoformat(first), Date.fromisoformat(last)
    if d2 < d1:
        unprocessable("'to' must not be before 'from'", ("query", "to"))
    if (d2 - d1).days + 1 > 92:
        unprocessable("the range must not exceed 92 days", ("query", "to"))
    return d1, d2


# --------------------------------------------------------------------------- #
# Analytics: endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[MonthStr, Query()]):
    emp = get_employee_or_404(emp_code)
    m_first, m_last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, m_first.isoformat(), m_last.isoformat())))
    r = rows[0] if rows else {}
    halves = int(r.get("present_halves", 0))
    # R7: working days run from joined_on for a mid-month joiner (and are 0 before the employee joined).
    working_days = count_weekdays(max(m_first, Date.fromisoformat(emp["joined_on"])), m_last)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": halves / 2,
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        # present_days / working_days * 100 = halves * 50 / working_days, exact then rounded half-up
        "attendance_pct": half_up(halves * 50, working_days, "0.01") if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[MonthStr, Query()], department: Optional[str] = None):
    m_first, m_last = month_bounds(month)
    pipeline = summary_pipeline(m_first.isoformat(), m_last.isoformat(), department)
    items = []
    for r in db.employees.aggregate(pipeline, allowDiskUse=True):
        items.append(
            {
                "department": r["department"],
                "headcount": int(r["headcount"]),
                "present_days": int(r["present_halves"]) / 2,
                "avg_work_hours": None if r.get("avg_cents") is None else int(r["avg_cents"]) / 100,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[MonthStr, Query()],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    m_first, m_last = month_bounds(month)
    pipeline = leaderboard_pipeline(m_first.isoformat(), m_last.isoformat(), limit, department)
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in db.attendance_logs.aggregate(pipeline, allowDiskUse=True)
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[DateStr, Query(alias="from")],
    to: Annotated[DateStr, Query()],
):
    first, last = validate_trend_range(from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = []
    for r in db.employees.aggregate(trend_pipeline(department, first, last), allowDiskUse=True):
        rate_n, moving_n = r.get("rate_n"), r.get("moving_n")
        items.append(
            {
                "date": r["date"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": int(r["headcount"]),
                "present_count": int(r["halves"]) / 2,
                "late_count": int(r["late"]),
                "attendance_rate": None if rate_n is None else int(rate_n) / 10000,
                "moving_avg_7d": None if moving_n is None else int(moving_n) / 10000,
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 7. Admin: explain
# --------------------------------------------------------------------------- #
def _run_explain(collection: str, *, pipeline: Optional[list] = None, find: Optional[dict] = None) -> dict:
    if pipeline is not None:
        cmd = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}
    else:
        cmd = {"find": collection, **find}
    result = db.command({"explain": cmd, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result))  # BSON types (dates, timestamps ...) -> plain JSON


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    def need(value, name):
        if value is None:
            unprocessable(f"query parameter '{name}' is required for {endpoint}", ("query", name))
        return value

    if endpoint == "attendance_list":
        if date_from is not None and date_to is not None and date_from > date_to:
            unprocessable("date_from must not be after date_to", ("query", "date_from"))
        q, sort, skip, lim = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
        coll = "attendance_logs"
        out = _run_explain(coll, find={"filter": q, "sort": dict(sort), "skip": skip, "limit": lim})
    elif endpoint == "employee_monthly":
        code, mon = need(emp_code, "emp_code"), need(month, "month")
        m_first, m_last = month_bounds(mon)
        coll = "attendance_logs"
        out = _run_explain(coll, pipeline=monthly_pipeline(code, m_first.isoformat(), m_last.isoformat()))
    elif endpoint == "department_summary":
        m_first, m_last = month_bounds(need(month, "month"))
        coll = "employees"
        out = _run_explain(coll, pipeline=summary_pipeline(m_first.isoformat(), m_last.isoformat(), department))
    elif endpoint == "late_leaderboard":
        m_first, m_last = month_bounds(need(month, "month"))
        coll = "attendance_logs"
        out = _run_explain(coll, pipeline=leaderboard_pipeline(m_first.isoformat(), m_last.isoformat(), limit, department))
    else:  # department_trend
        dept, f, t = need(department, "department"), need(from_, "from"), need(to, "to")
        first, last = validate_trend_range(f, t)
        coll = "employees"
        out = _run_explain(coll, pipeline=trend_pipeline(dept, first, last))
    return {"endpoint": endpoint, "collection": coll, "explain": out}
