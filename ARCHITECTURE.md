# Application architecture and flow

This project is a course purchase and refund backend. It has two ways to enter
the application:

1. **FastAPI HTTP API** for direct REST requests.
2. **Google ADK chat workflow** for conversational registration and refund
   requests.

Both use the same PostgreSQL database models and refund service. The ADK
workflow is a separate process/interface; it is not started automatically
when the FastAPI server starts.

## Initial entry points

### FastAPI service

The HTTP application object is `app` in [`app/main.py`](./app/main.py). The
development server command is:

```powershell
python -m uvicorn app.main:app --reload --env-file .env
```

Uvicorn imports `app.main`, then serves its `app` object. The module creates
the FastAPI application and mounts the router from `app/api.py` under `/api`.
The health endpoint is mounted directly at `/health`.

When FastAPI starts, its lifespan handler in `app/main.py` opens a database
connection and creates any missing tables from the SQLAlchemy models. When
FastAPI shuts down, it disposes of the database engine.

### ADK chat workflow

The ADK workflow's root agent is `root_agent` in
[`adk_app/agent.py`](./adk_app/agent.py). Start the development UI with:

```powershell
adk web adk_app
```

Or run it in a terminal:

```powershell
adk run adk_app
```

ADK loads the `adk_app` package and finds its root agent. The root agent is an
`EmailRefundCoordinator`, which routes each user message to one of four
deterministic specialists: registration validation, refund eligibility,
refund processing, or refund status. General questions that do not select an
account/refund action are sent to the configured Groq model. The coordinator
and specialists are implemented in Python; refund decisions are made by
backend rules and PostgreSQL queries, not by an LLM.

### Database container

[`docker-compose.yml`](./docker-compose.yml) starts PostgreSQL only. It does
not start either application entry point. Start the database with:

```powershell
docker compose up -d db
```

The API and ADK workflow read `DATABASE_URL` and other settings from the
environment (normally `.env`). Database connection setup and the FastAPI
request-scoped session dependency are in [`app/database.py`](./app/database.py).

## Main components

| Component | Responsibility |
|---|---|
| [`app/main.py`](./app/main.py) | Creates the FastAPI app, initializes tables at startup, mounts routes, and provides `/health`. |
| [`app/api.py`](./app/api.py) | Validates and handles HTTP requests; converts service errors to HTTP responses. |
| [`app/schemas.py`](./app/schemas.py) | Defines and validates request and response data with Pydantic. |
| [`app/database.py`](./app/database.py) | Creates the async SQLAlchemy engine/session and yields a session per API request. |
| [`app/config.py`](./app/config.py) | Loads environment configuration, including database URL and payment mode. |
| [`app/models.py`](./app/models.py) | Defines the users, courses, purchases, and refunds database tables. |
| [`app/services/refunds.py`](./app/services/refunds.py) | Contains shared refund eligibility, lookup, and transactional processing logic. |
| [`app/services/email.py`](./app/services/email.py) | Builds and sends refund notification emails over SMTP. |
| [`adk_app/agent.py`](./adk_app/agent.py) | Extracts conversational context, routes requests, invokes refund services, and formats chat responses. |

## HTTP request lifecycle

For a normal API request, the flow is:

1. Uvicorn receives the request and dispatches it to the FastAPI app in
   `app/main.py`.
2. FastAPI matches the route registered in `app/api.py`. Request bodies and
   query parameters are parsed and validated against schemas in
   `app/schemas.py`.
3. Routes that access data request `get_db` through FastAPI dependency
   injection. It opens an async SQLAlchemy session for the request.
4. The route reads or updates SQLAlchemy models. Refund endpoints delegate
   eligibility and processing to `app/services/refunds.py`.
5. Pydantic response models shape the response. The database session is
   closed after request handling.

The principal REST routes are:

| Method and path | Behavior |
|---|---|
| `POST /api/users` | Creates a user; email is stored lowercase, and a duplicate email returns `409`. |
| `POST /api/courses` | Creates a course. |
| `GET /api/courses` | Lists active courses. |
| `POST /api/purchases` | Verifies the user and active course, then records a purchase as paid. This starter does not charge a payment method. |
| `GET /api/refund-eligibility?email=...` | Reports eligibility for the user's active (paid) purchases. |
| `POST /api/refunds` | Processes or records refund outcomes for all registrations associated with the email, then attempts one summary email. |
| `GET /api/refunds?email=...` | Reports outcomes for the user's registrations, including previously refunded registrations. |
| `GET /api/refunds/{refund_id}?user_id=...` | Looks up one refund, requiring the matching user ID. |
| `GET /health` | Returns `{"status": "ok"}`. |

The interactive API reference is available at `/docs` while the FastAPI server
is running.

## Refund processing flow

The shared refund service uses a seven-day window measured from the recorded
purchase timestamp in UTC. The exact seven-day boundary is eligible; a
purchase timestamp in the future is not. A purchase must also still have
`paid` status.

For a refund request:

1. The service finds the user by case-insensitive email. A missing user results
   in a `404` error.
2. It loads the user's purchase/course registrations. The API processes all
   registrations for the email; the ADK flow passes a selected course title
   and processes only that active purchase.
3. It locks the user and purchase rows during processing and checks for an
   existing refund. A database uniqueness constraint also permits at most one
   refund row per purchase.
4. It records an outcome for each applicable purchase:
   - `processed`: eligible and `PAYMENT_GATEWAY_MODE=demo`; the purchase is
     marked `refunded`. This simulates a refund and does not move money.
   - `pending`: eligible and payment mode is not `demo`; provider confirmation
     is not implemented.
   - `rejected`: the seven-day window is not open; the purchase remains paid
     and the rejection is recorded.
   - `not_eligible`: a previously recorded or otherwise non-paid registration
     is reported as not eligible as applicable.
5. The database transaction commits before notification email is sent.
   Therefore, an SMTP failure does not undo a recorded refund outcome.
6. The API includes the email delivery result in `email_notification`. The ADK
   response similarly says whether notification delivery succeeded.

The REST refund request accepts an email and reason and can affect every
registration for that email. The conversational workflow uses the user's
registered email, lists active purchases, asks which course to cancel if the
message does not identify exactly one, and then processes that course. If the
chat request does not provide a reason, it uses
`Cancellation requested by user`.

## ADK conversation lifecycle

Each user turn enters `root_agent` in `adk_app/agent.py`:

1. The coordinator reads the current turn, extracts an email address, and
   classifies the request using keyword/rule matching.
2. If an email has not yet been validated in the conversation, registration
   validation runs first. Otherwise, selected account/refund actions are
   routed to the corresponding specialist.
3. A specialist opens its own SQLAlchemy session using `SessionLocal`, calls
   the shared refund service, and turns the result into a user-facing message.
   A newly processed cancellation also triggers a notification email.
4. If the user has already validated an email and is choosing a course, the
   coordinator prompts them to cancel a course or check status.
5. Otherwise, the current question is sent to Groq for a plain-language
   response. Email addresses are redacted before sending; without
   `GROQ_API_KEY`, the chat explains how to configure it.

The ADK flow does not use FastAPI routes or FastAPI's request-scoped database
dependency. It calls the same refund service directly and manages its database
session with `SessionLocal`.

## Data model

The SQLAlchemy models in `app/models.py` define:

- **User**: name, unique email, and creation timestamp.
- **Course**: title, description, price, active status, and creation timestamp.
- **Purchase**: user and course foreign keys, purchase timestamp, amount, and
  payment status.
- **Refund**: purchase and user foreign keys, request reason, decision/status,
  amount, request timestamp, and optional processing timestamp. A unique
  constraint prevents multiple refund records for a single purchase.

## Local end-to-end startup

From the project directory, create and activate a virtual environment, install
`requirements.txt`, configure `.env` using `.env.example`, and start the
database:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
docker compose up -d db
```

Run the API in one terminal:

```powershell
python -m uvicorn app.main:app --reload --env-file .env
```

Optionally run the ADK interface in another terminal:

```powershell
adk web adk_app
```

Create a user and course through the API, then create a purchase using their
returned IDs. You can then check refund eligibility or submit a refund. The
ADK workflow can be run separately to perform the conversational version of
the refund flow.

## Operational boundaries

- `PAYMENT_GATEWAY_MODE=demo` simulates both payment success at purchase time
  and refund processing; no real charge or refund occurs.
- SMTP is optional for local processing. Missing SMTP settings or delivery
  errors are returned as notification failures after the refund transaction.
- Email is used as an identifier, not as proof that the caller owns the
  account. Add authentication and email ownership verification before
  exposing email-based refund operations to real users.
- FastAPI startup currently uses SQLAlchemy `create_all` for this starter.
  Production deployments should use migrations and a real payment-provider
  integration.
