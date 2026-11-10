# Course purchase and refund multi-agent starter

A downloadable starter backend using FastAPI, PostgreSQL, SQLAlchemy async, and
Google ADK. It records users, courses, course purchases, and refund requests.
Refund requests use an email to find the user's registrations, and apply a
strict seven-day window to each registration.

## What is included

- `users`, `courses`, `purchases`, and `refunds` PostgreSQL tables.
- FastAPI endpoints for user/course creation, purchases, eligibility, refund
  requests, and refund status.
- ADK sequential agents for user/purchase validation, policy checking, refund
  processing, and status lookup (`adk_app/agent.py`).
- Email and refund-policy checks run in the backend; policy and ownership are
  rechecked during the transaction that records refund outcomes.
- Email-only requests apply to every course registration belonging to that
  email. The response reports each course separately.
- The default `demo` payment mode simulates a successful payment/refund. It
  does not connect to a real payment provider or move money. Any other mode
  leaves eligible refunds as `pending` until you integrate and confirm a
  payment-provider refund.

## Requirements

- Python 3.10+
- Docker Desktop (or another PostgreSQL 16 instance)
- No LLM key is required for refund processing. The ADK specialists run
  deterministic PostgreSQL-backed checks and return stored refund IDs/statuses.

## Setup on Windows (PowerShell)

From this extracted project directory:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
docker compose up -d db
```

Edit `.env` if needed. Keep `PAYMENT_GATEWAY_MODE=demo` only for local testing.
The refund workflow uses deterministic backend logic and does not require
`GROQ_API_KEY`. The optional Groq settings are retained in `.env` for future
LLM-powered features. Set `POSTGRES_USER`, `POSTGRES_PASSWORD`, and
`POSTGRES_DB`; make the same credentials and database name match the
`DATABASE_URL` line. For example, if you change the database password to
`my_password`, also change `course_app:change_this_local_password` in
`DATABASE_URL` to `course_app:my_password`. Docker Compose reads the
PostgreSQL settings from `.env`. For passwords containing URL-special
characters, URL-encode the password in `DATABASE_URL`. Do not commit `.env`.

### Gmail refund notifications

The `POST /api/refunds` endpoint and ADK cancellation workflow send one
professional HTML-and-plain-text email to the registered address after the
refund results have been committed. It includes each course's refund status,
ID, amount, and reason, and the AI-generated-response disclaimer.

Set `SMTP_USER`, `SMTP_PASSWORD`, and `SMTP_FROM_EMAIL` in `.env`. For Gmail,
create a Google App Password and use it as `SMTP_PASSWORD` (and as
`IMAP_PASSWORD` only if you later enable inbound IMAP access); do not use your
normal Google account password. SMTP uses STARTTLS on port 587 by default.
The supplied `IMAP_*`, `MAX_EMAILS`, and `MAX_CHARS_PER_EMAIL` values are
available for future inbound mail handling; IMAP is not required to send
refund notifications.

If SMTP delivery fails or the app password is not configured, the refund
database transaction remains committed and the API response reports
`email_notification.sent: false` with the delivery detail. The ADK response
also clearly reports that the email was not sent. Configure SMTP before
expecting notifications to arrive.

Run the API:

```powershell
python -m uvicorn app.main:app --reload --env-file .env
```

Open `http://127.0.0.1:8000/docs` for the interactive API. The database tables
are created automatically on startup for this starter; use Alembic migrations
instead of `create_all` for a production deployment.

To open the ADK development UI, first activate the project virtual environment
and run the command from the project directory. ADK and the API load settings
from `.env`:

```powershell
adk web adk_app
```

Select `adk_app` in the UI. You can also run the workflow in a terminal with:

```powershell
adk run adk_app
```

The ADK workflow first asks for the registered email. It reports when the
user does not exist; otherwise it lists the user's active (paid) course
purchases and asks whether to cancel a course or check refund status.
Previously refunded courses are not offered as active purchases. A cancellation
must name one of the listed courses, and only that course's registration is
processed.
The workflow sends one email after processing and includes the refund ID,
status, amount, and reason. To check status, it lists recorded refunds or says
there is no refund request. The FastAPI refund endpoint remains a separate
email-based batch API and accepts an explicit user-provided reason.

## Try the API

Use the Swagger UI at `/docs`, or PowerShell's `Invoke-RestMethod`. Create a
user and course, then buy the course with their returned IDs:

```powershell
$user = Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/api/users `
  -ContentType "application/json" `
  -Body '{"name":"Sam Example","email":"sam@example.test"}'

$course = Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/api/courses `
  -ContentType "application/json" `
  -Body '{"title":"Python Basics","description":"An introduction","price":"49.00"}'

$purchase = Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/api/purchases `
  -ContentType "application/json" `
  -Body (@{user_id=$user.id; course_id=$course.id} | ConvertTo-Json)
```

Check all registrations for an email, then submit a cancellation/refund
request. The request applies to every registration associated with that email:

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/refund-eligibility?email=$([uri]::EscapeDataString($user.email))"

$refunds = Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/api/refunds `
  -ContentType "application/json" `
  -Body (@{
    email=$user.email
    reason="I no longer need this course"
  } | ConvertTo-Json)

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/refunds?email=$([uri]::EscapeDataString($user.email))"
```

Possible refund statuses are `processed` (simulated in demo mode), `pending`
(eligible, but no provider confirmation), and `rejected` (outside the policy
window; the reason and decision are stored). Invalid users or purchases return
an HTTP error and do not create a refund.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/users` | Create a user |
| POST | `/api/courses` | Create a course |
| GET | `/api/courses` | List active courses |
| POST | `/api/purchases` | Record a course purchase |
| GET | `/api/refund-eligibility?email=...` | Check all registrations and their eligibility by email |
| POST | `/api/refunds` | Process or record refund outcomes for all registrations belonging to the email |
| GET | `/api/refunds?email=...` | List registration refund statuses by email |
| GET | `/api/refunds/{id}?user_id=...` | Read a user's refund status |
| GET | `/health` | Health check |

## Business rules and production notes

- The seven-day window is measured from the UTC purchase timestamp, inclusive
  of the exact seven-day boundary.
- The email must match an existing user. Each registration must have `paid`
  status and be within the seven-day window to be eligible.
- Only one refund record can exist for a purchase; the database also enforces
  this uniqueness to prevent concurrent duplicates.
- Rejections outside the time window are saved with the user's reason and
  `rejected` status. The purchase remains paid.
- The sample purchase endpoint marks purchases paid without charging a card,
  and demo refunds are simulated. Replace these with payment-provider calls
  and verify provider webhooks before using this for real transactions.
- Email is an identifier, not proof of account ownership. The email-based
  refund endpoint is for local/demo use only; it can affect every eligible
  registration associated with that email. Do not expose it publicly or use it
  for real accounts or money until you add authentication and verify that the
  caller controls the email (for example, login or a one-time email code),
  rate limits, secrets management, migrations, and a real payment integration.

Run the focused rules tests with:

```powershell
python -m pytest
```
