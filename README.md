# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in a local .env
uvicorn app.main:app --port 8000
```
Indexes are created at startup (idempotent). `GET /health` returns 200 once MongoDB answers.

## Status 
All endpoints in `openapi.yaml` are implemented, including `/admin/explain/{endpoint}`.

## Choices where the contract is open to reading
- `moving_avg_7d` averages the *reported* (4-decimal, half-up) daily rates.
- A trend range of 92 days **inclusive** is accepted; 93 or more is 422.
- Logs are counted by their `date` falling in the month, even if that is before the employee's `joined_on`.
- `PATCH` with `ABSENT`/`LEAVE` as the final status and a supplied `punch_in`/`punch_out` is 422, even when `status` itself was omitted from the request.
- A `PATCH` that loses an optimistic-concurrency race is retried up to 4 times, then answered with 409.
- Percentages, averages and rates are rounded half-up using integer arithmetic (MongoDB's `$round` is banker's rounding).

## Testing note
Written and checked without a MongoDB server available. Business-rule helpers and the HTTP layer were tested in memory; the aggregation pipelines were checked with a small evaluator against an independent Python implementation, not against a real `mongod`. Run it against MongoDB 6/7 before relying on it.
