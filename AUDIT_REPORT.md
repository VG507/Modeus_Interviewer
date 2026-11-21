# Comprehensive Code Audit & Vulnerability Assessment: Modeus Interviewer

**Audit Date**: November 21, 2026  
**Audited Target**: Modeus Interviewer MVP  
**Stack**: Python 3.12+ (FastAPI, SQLAlchemy 2, Alembic, Pydantic v2, AnyIO, httpx, SQLite/PostgreSQL), React 18, TypeScript, Vite  
**Audit Standard**: Zero False Positives, Evidence-Grounded Static & Architectural Verification  

---

## 1. Executive Summary

### 1.1 Scope & Methodology
A comprehensive security, architectural, backend logic, and API specification audit of the **Modeus Interviewer** platform was conducted. The investigation encompassed the entire repository surface:
- **API Contracts & Specifications**: `Modeus_MVP_Technical_Contract.md`, `Modeus_Interview_MVP_Plan.md`, `README.md`, and migration scripts.
- **Backend Infrastructure & APIs**: FastAPI route definitions, AnyIO asynchronous worker pools, Pydantic request/response schemas, SQLAlchemy ORM models, session/cookie handling, error middleware, and CLI commands (`backend/`).
- **Frontend Client Architecture**: React 18 component trees, TypeScript API client bindings, state machines, and view components (`frontend/src/`).
- **Data Persistence & Concurrency**: Optimistic versioning, idempotency receipt tables, database constraints, and migration scripts (`migrations/`).

The audit methodology followed strict **Zero False Positive** criteria: every finding documented herein has been verified against the physical source code, exact file paths, line numbers, and architectural design contracts. No speculative or unverified claims are included.

### 1.2 High-Level Verdict
Modeus Interviewer demonstrates robust design patterns in several core areas, notably **zero raw SQL injection** due to consistent SQLAlchemy 2 parameterized queries, **zero cross-site scripting (XSS)** vulnerabilities, **strict server-side request forgery (SSRF) controls** with hardcoded target hosts, and an **optimistic concurrency control model** utilizing versioned commands and cryptographic idempotency receipts.

However, the audit revealed **critical resilience vulnerabilities** in the asynchronous background worker (where unhandled type and attribute errors cause total, unrecoverable worker crashes), **severe data integrity distortions** in the public catalog (hardcoding all open interview summaries to `python_backend`), **denial of service vectors** in the authentication flow (computing expensive scrypt password hashes prior to evaluating lockout status), **silent data erasure** when editing knowledge cards, and significant **discrepancies between the technical contract and the deployed HTTP API**.

### 1.3 Findings Summary Matrix

| Domain | Critical | High | Medium | Low / Info | Total Findings |
|---|:---:|:---:|:---:|:---:|:---:|
| **Backend Logic & Concurrency** | 2 | 4 | 5 | 3 | **14** |
| **API Contracts & Specification Alignment** | 1 | 4 | 3 | 4 | **12** |
| **Security & Authentication** | 0 | 1 | 3 | 4 | **8** |
| **Total Unique Findings** | **3** | **9** | **11** | **11** | **34** |

*Note: Overlapping cross-domain issues (such as the authentication lockout order, global 500 error handler, and session/receipt storage retention) are fully unified and cross-referenced in their respective sections.*

---

## 2. Critical Severity Findings

### CRIT-01: Background Worker Permanent Termination via Unhandled `TypeError` in `claim()`
- **Issue ID**: `LOGIC-01`
- **Domain**: Backend Logic & Concurrency / Availability
- **File Reference**: `backend/interview_worker.py:131`, `backend/interview_worker.py:280-296`, `backend/db.py:121`
- **Technical Root Cause**:
  In `backend/db.py:121`, `InterviewSession.job` is declared nullable (`job: Mapped[dict | None] = mapped_column(JSON)`). When an interview stage finishes, `clear_job(interview)` in `interview_worker.py:42` sets `interview.job = None`.
  When `claim(sessions, timeout)` claims a queued session or expired lease, line 131 executes:
  ```python
  interview.job = {**interview.job, "token": token}
  ```
  If `interview.job` is `None`, dictionary unpacking (`**None`) raises `TypeError: 'NoneType' object is not a mapping`.
  In `run_worker(sessions, settings)`, the main worker loop wraps `claim()` with:
  ```python
  try:
      job = claim(sessions, timeout)
      ...
  except SQLAlchemyError as exc:
      logger.warning("Interview worker database error: %s", type(exc).__name__)
  ```
  `TypeError` is not an instance of `SQLAlchemyError`. Consequently, the unhandled exception propagates out of the loop, terminating the `run_worker` asyncio coroutine.
- **Risk & Impact**:
  **Total Denial of Service for Background Processing.** Modeus relies on a single background worker task spawned at startup (`lifespan` in `backend/app.py`). Once terminated, no further questions, extractions, or card syntheses are processed for any user. The system hangs in a perpetual queued state until the entire server process is restarted.
- **Remediation Recommendation**:
  1. In `backend/interview_worker.py:131`, safely guard dictionary unpacking:
     ```python
     interview.job = {**(interview.job or {}), "token": token}
     ```
  2. In `run_worker()`, wrap the claim and execution cycle with a top-level `except Exception:` handler and backoff sleep to guarantee the worker coroutine never dies.

---

### CRIT-02: Background Worker Crash on Missing Message Entities in `execute_stage()` and `mark_failed()`
- **Issue ID**: `LOGIC-02`
- **Domain**: Backend Logic & Concurrency / Fault Tolerance
- **File Reference**: `backend/interview_worker.py:170`, `backend/interview_worker.py:271-272`, `backend/interview_worker.py:286-293`
- **Technical Root Cause**:
  In `execute_stage()` line 170:
  ```python
  question = db.get(Message, message.reply_to_question_id).text if message else None
  ```
  If `message.reply_to_question_id` refers to a message that cannot be resolved (or is null), `db.get(Message, ...)` returns `None`, and accessing `.text` immediately raises `AttributeError`.
  Furthermore, in `mark_failed()` line 271–272:
  ```python
  if interview.processing_stage == "extraction":
      db.get(Message, interview.job["answer_id"]).status = "failed"
  ```
  If `interview.job` lacks `"answer_id"` or `db.get(Message, ...)` returns `None`, Python raises `KeyError` or `AttributeError`.
  In `run_worker()`, lines 286–292 only catch `TimeoutError`, `httpx.TimeoutException`, `InvalidModelOutput`, `ValidationError`, `RuntimeError`, and `httpx.RequestError`. Unhandled `AttributeError` or `KeyError` escapes the inner try-catch, bypasses `except SQLAlchemyError:`, and kills the worker coroutine.
- **Risk & Impact**:
  **Worker Termination and Queue Freeze.** A single corrupted message reference or missing answer entity permanently halts all interview processing across the entire deployment.
- **Remediation Recommendation**:
  1. Add defensive null guards in `execute_stage()`:
     ```python
     parent_q = db.get(Message, message.reply_to_question_id) if message and message.reply_to_question_id else None
     question = parent_q.text if parent_q else None
     ```
  2. Guard `mark_failed()`:
     ```python
     if interview.processing_stage == "extraction" and interview.job and "answer_id" in interview.job:
         if answer_msg := db.get(Message, interview.job["answer_id"]):
             answer_msg.status = "failed"
     ```
  3. In `run_worker()`, add a catch-all `except Exception as exc:` around `execute_stage()` to mark the job failed and continue processing other jobs.

---

### CRIT-03: Public Catalog Summary Hardcodes `profession_id: "python_backend"` for All Open Interviews
- **Issue ID**: `API-01` (Related: `LOGIC-15`)
- **Domain**: API Contracts & Data Integrity / Core Domain Logic
- **File Reference**: `backend/card_api.py:341-350`, `migrations/versions/0004_open_interview.py:20-24`
- **Contract Reference**: `Modeus_MVP_Technical_Contract.md`, Section 1 line 9; Section 4 line 52; Section 8 line 219; Section 6 lines 155–156 (`GET /catalog`, `GET /catalog/{id}`)
- **Technical Root Cause**:
  In `backend/card_api.py:341-350`:
  ```python
  def public_summary(db, card):
      session = db.get(InterviewSession, card.session_id)
      topic = db.get(Topic, session.topic_id)
      title = card.content["title"]
      return PublicSummary(
          id=card.id, title=title, profession_id=topic.profession_id,
          topic_title=title if topic.id == OPEN_TOPIC_ID else topic.title,
          reviewed_at=card.review["at"], author_name=card.author_name if card.show_author else None,
      )
  ```
  For open interviews, `session.topic_id` is initialized to `OPEN_TOPIC_ID` (`"open-interview"`). In migration `0004_open_interview.py:21`, the `"open-interview"` topic row is inserted with `profession_id: "python_backend"`.
  `public_summary()` directly reads `topic.profession_id` without inspecting `session.case_state.get("active_topic_id")` (as `interview_view` does in `backend/interview_api.py:45`) or matching the role in `session.context["role"]`.
- **Risk & Impact**:
  **Systemic Data Misclassification in Public Knowledge Base.** Every open interview knowledge card published to the catalog—regardless of whether the participant was a Frontend Engineer, QA Automation Engineer, DevOps Architect, or Data Scientist—is published with `profession_id: "python_backend"`. Catalog filtering, analytics, search indexing, and profile mapping for non-Python professions are completely broken.
- **Remediation Recommendation**:
  Update `public_summary()` to derive the profession dynamically:
  ```python
  active_topic = db.get(Topic, session.case_state.get("active_topic_id")) if session.case_state else None
  profession_id = active_topic.profession_id if active_topic and active_topic.id != OPEN_TOPIC_ID else resolve_profession(db, session.context.get("role"))
  ```

---

## 3. High Severity Findings

### HIGH-01: CPU Starvation & DoS via Scrypt Computation Prior to Account Lockout Check
- **Issue ID**: `SEC-01` / `LOGIC-11`
- **Domain**: Security / Authentication & Availability
- **File Reference**: `backend/app.py:92-102` (specifically lines 93–95)
- **Technical Root Cause**:
  In `POST /api/v1/auth/login`:
  ```python
  92: user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
  93: valid = check_password(body.password, user.password_hash if user else dummy_hash)
  94: if user and user.locked_until and user.locked_until > now():
  95:     raise HTTPException(429, "LOGIN_TEMPORARILY_LOCKED")
  ```
  Line 93 computes `check_password()`, which executes `hashlib.scrypt(..., n=16384, r=8, p=1)` (deliberately calibrated to consume significant CPU and memory). Line 94 checks whether the account is locked *after* executing scrypt.
  Furthermore, `login` is a synchronous `def` running inside AnyIO worker threads and lacks IP-level rate limiting.
- **Risk & Impact**:
  **Denial of Service (DoS) via Threadpool Starvation.** An unauthenticated attacker can flood `/api/v1/auth/login` with requests targeting a locked user account or non-existent accounts. Every request forces an expensive scrypt computation, saturating server CPU cores and exhausting worker threads, causing legitimate participant requests across the entire application to time out.
- **Remediation Recommendation**:
  1. Reorder the check: evaluate `user.locked_until and user.locked_until > now()` immediately on line 93 *before* executing `check_password()`.
  2. Implement an IP-based rate limiter (e.g. `slowapi` or reverse-proxy rate limiting) on `/api/v1/auth/login`.

---

### HIGH-02: Unhandled `StopIteration` & `ZeroDivisionError` in Topic Iteration (`advance_case`)
- **Issue ID**: `LOGIC-03`
- **Domain**: Backend Logic & Concurrency / State Machine Integrity
- **File Reference**: `backend/interview_worker.py:108-112`, `backend/interview_api.py:245`
- **Technical Root Cause**:
  When advancing to a subsequent case, `advance_case()` runs:
  ```python
  topics = list(db.scalars(select(Topic).where(
      Topic.profession_id == original.profession_id, Topic.id != OPEN_TOPIC_ID,
  ).order_by(Topic.id)))
  position = next(index for index, item in enumerate(topics) if item.id == current.id)
  following = topics[(position + 1) % len(topics)]
  ```
  If `current.id` is not present in `topics` (e.g., if a topic was deleted or changed), `next()` raises an uncaught `StopIteration`.
  If `topics` is empty (`len(topics) == 0`), `next()` fails, and line 112 evaluates `(position + 1) % 0`, raising `ZeroDivisionError`.
- **Risk & Impact**:
  When a user calls `POST /interviews/{id}/skip` on the final question of a case, the endpoint crashes with HTTP 500. When executed during background worker extraction, this unhandled exception kills the worker coroutine.
- **Remediation Recommendation**:
  Provide safe defaults and verify non-empty list:
  ```python
  if not topics:
      following = original
  else:
      pos = next((i for i, item in enumerate(topics) if item.id == current.id), -1)
      following = topics[(pos + 1) % len(topics)] if pos >= 0 else topics[0]
  ```

---

### HIGH-03: Missing Null Guards on `interview.job` and `answer_id` in `retry_interview`
- **Issue ID**: `LOGIC-04`
- **Domain**: Backend Logic & Concurrency / Error Recovery
- **File Reference**: `backend/interview_api.py:269-272`
- **Technical Root Cause**:
  In `retry_interview()` (`POST /api/v1/interviews/{id}/retry`):
  ```python
  job = {field: value for field, value in interview.job.items() if field != "token"}
  if interview.processing_stage == "extraction":
      db.get(Message, job["answer_id"]).status = "queued"
  queue(interview, interview.processing_stage, **job)
  ```
  If `interview.job` is `None`, line 269 raises `AttributeError: 'NoneType' object has no attribute 'items'`.
  If `processing_stage == "extraction"`, line 271 directly indexes `job["answer_id"]` (raising `KeyError` if absent) and accesses `.status` on `db.get(Message, ...)` (raising `AttributeError` if `db.get` returns `None`).
- **Risk & Impact**:
  Participants attempting to recover from a failed interview processing step encounter an unhandled HTTP 500 error, leaving the session permanently broken and unrecoverable.
- **Remediation Recommendation**:
  Guard unpacking and message resolution:
  ```python
  job = {k: v for k, v in (interview.job or {}).items() if k != "token"}
  if interview.processing_stage == "extraction" and "answer_id" in job:
      if msg := db.get(Message, job["answer_id"]):
          msg.status = "queued"
  ```

---

### HIGH-04: Unhandled `IndexError: list index out of range` in `submit_answer` and `skip_interview`
- **Issue ID**: `LOGIC-05`
- **Domain**: Backend Logic & Concurrency / State Transitions
- **File Reference**: `backend/interview_api.py:227`, `backend/interview_api.py:238`
- **Technical Root Cause**:
  In `submit_answer()`:
  ```python
  queue(interview, "extraction", answer_id=answer.id,
        current_intent=interview.case_state["asked_intents"][-1])
  ```
  In `skip_interview()`:
  ```python
  current = state["asked_intents"][-1]
  ```
  Both endpoints directly index `[-1]` of `asked_intents`. If an answer draft is submitted or skipped before `initial_question` appends to `asked_intents` (or after state re-initialization), indexing an empty list raises `IndexError`.
- **Risk & Impact**:
  Triggers an unhandled HTTP 500 Internal Server Error during normal answer submission or skip operations.
- **Remediation Recommendation**:
  Safely resolve the current intent:
  ```python
  intents = interview.case_state.get("asked_intents") or []
  current_intent = intents[-1] if intents else "get_case"
  ```

---

### HIGH-05: Non-Deterministic Cursor Pagination Due to Lexicographical Sorting of Random UUIDv4
- **Issue ID**: `LOGIC-06`
- **Domain**: Backend Logic & Database Design / Data Consistency
- **File Reference**: `backend/interview_api.py:177-178`, `backend/card_api.py:168-170`, `backend/db.py:109, 154`
- **Technical Root Cause**:
  `InterviewSession.id` and `KnowledgeCard.id` are generated using `uuid.uuid4()`. UUIDv4 strings are random and have no temporal ordering.
  In `list_interviews()` and `list_cards()` / `catalog()`, queries paginate using:
  ```python
  if cursor:
      query = query.where(Model.id < cursor)
  rows = list(db.scalars(query.order_by(Model.id.desc()).limit(limit + 1)))
  ```
  Sorting by `id.desc()` sorts by random hexadecimal characters rather than creation time (`created_at`).
- **Risk & Impact**:
  Newly created interviews or cards appear at random positions in lists rather than at the top. Paginating through the public catalog or card review queue causes records to be skipped entirely or returned repeatedly across subsequent pages.
- **Remediation Recommendation**:
  Order queries by `Model.created_at.desc(), Model.id.desc()`, and implement a composite cursor `(created_at, id)` or adopt monotonically increasing identifiers (UUIDv7 or ULID).

---

### HIGH-06: Vacancy Card Schema Mismatch Between Technical Contract and Backend/Frontend
- **Issue ID**: `API-02`
- **Domain**: API Contracts & Specifications
- **File Reference**: `backend/vacancy_parser.py:141-148, 227-232`, `backend/vacancy_api.py:190-194`, `frontend/src/Vacancies.tsx:26-30`
- **Contract Reference**: `Modeus_MVP_Technical_Contract.md`, Section 4 lines 70–73
- **Technical Root Cause**:
  Contract Section 4 line 71 specifies the vacancy record structure as:
  `{schema_version, profession_id, title, task, skills[], tools[], experience_band, level, conditions, evidence[]}`
  In the implementation:
  1. Contract specifies singular `task` ("Одна запись описывает одну рабочую задачу", line 75), but both backend and frontend implement `tasks: list[str]` (`tasks: string[]`).
  2. `tools[]` is specified in the contract, but does not exist in backend code or frontend interfaces (tools are either merged into `skills` or omitted).
  3. `schema_version` is declared in the contract, but is completely missing from the backend card object and frontend model.
  4. The backend introduces undocumented fields: `requirements: list[str]`, `hh_id`, `published_at`, `area_id`, and `employer_id`.
- **Risk & Impact**:
  External consumers or automated clients built according to Section 4 fail schema validation when parsing vacancy cards.
- **Remediation Recommendation**:
  Update `Modeus_MVP_Technical_Contract.md` Section 4 to document `tasks: string[]`, `requirements: string[]`, and clarify that tools are merged into `skills`.

---

### HIGH-07: Entire Vacancy (`/vacancies/*`) and Discovery (`/discovery/*`) API Surface Omitted from Contract Section 6
- **Issue ID**: `API-03`
- **Domain**: API Contracts & Specifications
- **File Reference**: `backend/app.py:58-59, 79-86`, `backend/vacancy_api.py:18-210`, `backend/discovery_api.py:21-99`
- **Contract Reference**: `Modeus_MVP_Technical_Contract.md`, Section 1 line 11; Section 6 lines 130–157
- **Technical Root Cause**:
  Section 6 ("HTTP API v1") of `Modeus_MVP_Technical_Contract.md` defines a table of 21 endpoints. It omits:
  - `GET /api/v1/health`
  - `GET /api/v1/vacancies/overview`
  - `GET /api/v1/vacancies/candidates`
  - `POST /api/v1/vacancies/profiles`
  - `PUT /api/v1/vacancies/profiles/{profession_id}`
  - `POST /api/v1/vacancies/profiles/{profession_id}/review`
  - `GET /api/v1/vacancies`
  - `POST /api/v1/vacancies/{profession_id}/collect`
  - `POST /api/v1/discovery/collect`
  - `GET /api/v1/discovery/profiles`
  - `GET /api/v1/discovery`
  - `GET /api/v1/discovery/items/{item_id}`
  12 out of 33 routes (over 36% of the backend surface) have no contract specification for request schemas, response models, query parameters, or error statuses.
- **Risk & Impact**:
  The formal technical contract is severely incomplete, impeding integration testing and API governance.
- **Remediation Recommendation**:
  Add dedicated tables in Section 6 defining route paths, HTTP methods, RBAC requirements, and schema models for Vacancies, Discovery, and Health endpoints.

---

### HIGH-08: Card Text Edits via `PATCH /cards/{id}` Erase Structured Facts from Review Interface
- **Issue ID**: `API-04`
- **Domain**: API Contracts & Frontend Integration / Usability
- **File Reference**: `backend/card_api.py:260-270`, `frontend/src/Cards.tsx:176-178, 183, 219-221`, `frontend/src/api.ts:31-37`
- **Contract Reference**: `Modeus_MVP_Technical_Contract.md`, Section 8 lines 201–203; Section 6 line 151
- **Technical Root Cause**:
  When an author edits text in the web form and clicks "Save edits", `frontend/src/Cards.tsx:183` invokes `PATCH /cards/{id}` with `{ expected_version, title, text }` (omitting `content`).
  In `backend/card_api.py:263`:
  ```python
  title = body.title or card.content["title"]
  content = {"title": title} if body.text != card.text else {**card.content, "title": title}
  card.content = content
  ```
  Because `body.text != card.text`, `card.content` is replaced with `{"title": title}`, wiping all fact sections (`task`, `context`, `actions`, `decisions`, `verification`, `errors`, `limitations`).
  In `frontend/src/Cards.tsx:177, 220`:
  ```tsx
  const hasFacts = !!card.content.cases?.length || Object.keys(sections).some(key => card.content[key]?.length);
  ...
  {hasFacts && <details><summary>Структурированное содержание</summary><Facts content={card.content}/></details>}
  ```
  `hasFacts` becomes `false`. The structured facts disappear permanently from the review screen.
- **Risk & Impact**:
  Breaks the core editorial review workflow. An editor cannot review the original structured facts extracted by the LLM once the author performs a minor text edit in the UI.
- **Remediation Recommendation**:
  Preserve extracted facts in `card.content` (e.g. marked with a `text_overridden: true` metadata flag), or update `frontend/src/Cards.tsx` to read and display facts from `card.original` if `card.content` contains only the title.

---

### HIGH-09: Environment Dependency Version Mismatch Breaks Test and Script Execution
- **Issue ID**: `API-11`
- **Domain**: Runtime / Developer Experience & CI/CD
- **File Reference**: `requirements.lock:19-20`, `.venv` installed packages
- **Technical Root Cause**:
  `requirements.lock` specifies `pydantic==2.14.3` and `pydantic-core==2.50.4`. However, `.venv` contains `pydantic 2.14.4` without a compatible `pydantic-core`, causing any Python invocation that imports Pydantic (`uvicorn`, `pytest`, `alembic`, `scripts/smoke.py`) to fail with:
  `SystemError: The installed pydantic-core version (2.50.4) is incompatible with the current pydantic version, which requires 2.50.5.`
- **Risk & Impact**:
  Commands documented in `README.md` fail on fresh checkouts unless the environment is manually repaired.
- **Remediation Recommendation**:
  Reconcile `.venv` by running `.venv\Scripts\pip install -r requirements.lock`.

---

## 4. Medium Severity Findings

### MED-01: Active Sessions Remain Valid During Account Lockout
- **Issue ID**: `SEC-02`
- **Domain**: Security / Authentication Lifecycle
- **File Reference**: `backend/auth.py:6-15`, `backend/app.py:97-101`
- **Technical Root Cause**:
  When an account is locked after 5 failed login attempts (`user.locked_until = now() + timedelta(minutes=15)`), active session tokens in `login_sessions` are not revoked.
  The authentication dependency `current_user()` in `backend/auth.py` checks `session.expires_at <= now()`, but never checks `user.locked_until`.
- **Risk & Impact**:
  If an account lockout is triggered due to brute force or administrative action, an attacker possessing an active session token retains uninterrupted access to protected participant and card APIs.
- **Remediation Recommendation**:
  1. In `current_user()`, verify `if user.locked_until and user.locked_until > now(): raise HTTPException(429, "ACCOUNT_TEMPORARILY_LOCKED")`.
  2. When triggering lockout in `backend/app.py`, revoke all active sessions for that user (`DELETE FROM login_sessions WHERE user_id = user.id`).

---

### MED-02: Insecure Default Session Cookie Configuration (`COOKIE_SECURE=False`)
- **Issue ID**: `SEC-03`
- **Domain**: Security / Transport Hardening
- **File Reference**: `backend/settings.py:10`, `.env.example:3`, `backend/app.py:109-110`
- **Technical Root Cause**:
  `backend/settings.py:10` defaults `cookie_secure: bool = False`. In `backend/app.py:109-110`, the cookie is set with `secure=settings.cookie_secure`.
- **Risk & Impact**:
  Unless explicitly overridden in `.env`, session cookies are transmitted over unencrypted HTTP, allowing Man-in-the-Middle (MitM) attackers on local Wi-Fi or networks to intercept session tokens.
- **Remediation Recommendation**:
  Default `cookie_secure` to `True`, or enforce `secure=True` automatically whenever `app_origin` specifies HTTPS.

---

### MED-03: Unbounded Payload Structure & Memory Exhaustion in Card Editing
- **Issue ID**: `SEC-04`
- **Domain**: Security / Resource Exhaustion
- **File Reference**: `backend/card_api.py:17, 27-46, 252-273`
- **Technical Root Cause**:
  In `backend/card_api.py`, `ContentEdit` allows up to 100 cases, each having 7 sections of up to 300 facts, with each fact text up to 8,000 characters. There is no global HTTP request body size limit middleware in `backend/app.py`. A single crafted JSON request can submit hundreds of megabytes of nested structures, exhausting worker memory during Pydantic validation and `reconcile_content()`.
- **Risk & Impact**:
  Memory exhaustion (OOM) and worker crash.
- **Remediation Recommendation**:
  1. Add request body size limiting middleware (e.g. 2 MB max body size).
  2. Tighten `CaseEdit` bounds to reasonable limits (e.g. max 10 cases, max 25 facts per section).

---

### MED-04: Unbounded Growth in Command Receipts and Login Sessions
- **Issue ID**: `SEC-05` / `LOGIC-09`
- **Domain**: Security / Database Resource Management (Cross-Domain)
- **File Reference**: `backend/interview_api.py:105`, `backend/db.py:27-32, 144-150`, `backend/app.py:106-107`, `backend/auth.py:8-14`
- **Technical Root Cause**:
  Every mutating command inserts a `CommandReceipt` containing the full serialized JSON result. No TTL, expiration, or pruning routine exists for receipts. Similarly, `login_sessions` records are only deleted upon manual logout (`backend/app.py:123`) or CLI password reset (`backend/cli.py:181`). Expired sessions remain in the database indefinitely.
- **Risk & Impact**:
  Unbounded database disk and index bloat, causing degradation of database query performance over time.
- **Remediation Recommendation**:
  Implement a scheduled background pruning task that periodically purges expired `login_sessions` (`expires_at < now()`) and `CommandReceipt` records older than 7 days.

---

### MED-05: Missing Concurrency Limits on Interview Creation
- **Issue ID**: `SEC-06`
- **Domain**: Security / Resource Management & Queue Starvation
- **File Reference**: `backend/interview_api.py:155-169`, `backend/interview_worker.py:277-296`
- **Technical Root Cause**:
  `POST /api/v1/interviews` allows any authenticated user to create an unlimited number of interview sessions. Each creation enqueues a background LLM prompt job.
- **Risk & Impact**:
  A single authenticated participant can flood the worker queue by scripting interview creation, consuming excessive LLM token resources and starving other users.
- **Remediation Recommendation**:
  Enforce a quota on concurrent active interviews per participant (e.g. allowing at most 2 active sessions with status `"active"`).

---

### MED-06: Missing Catch-All 500 Handler Violates Unified JSON Error Contract
- **Issue ID**: `API-05` / `LOGIC-10`
- **Domain**: API Contracts & Error Handling (Cross-Domain)
- **File Reference**: `backend/app.py:61-74`, `backend/interview_api.py:115`
- **Contract Reference**: `Modeus_MVP_Technical_Contract.md`, Section 6 lines 166–172
- **Technical Root Cause**:
  The technical contract guarantees that all errors (including HTTP 500) return a unified JSON payload:
  `{"error": {"code": "...", "message": "...", "retryable": ..., "request_id": "..."}}`
  `backend/app.py` registers handlers only for `HTTPException` and `RequestValidationError`. When an unhandled exception occurs (such as a database failure, `AttributeError`, or re-raised `IntegrityError` in `interview_api.py:115`), Starlette's default `ServerErrorMiddleware` catches it and returns a plain text string `Internal Server Error`.
- **Risk & Impact**:
  Frontend and third-party clients attempting to call `response.json()` crash with JSON parse errors, breaking client-side error boundaries.
- **Remediation Recommendation**:
  Register a global exception handler in `backend/app.py`:
  ```python
  @app.exception_handler(Exception)
  async def unhandled_exception_handler(request: Request, exc: Exception):
      logger.exception("Unhandled server exception: %s", exc)
      return JSONResponse(status_code=500, content={"error": {
          "code": "INTERNAL_SERVER_ERROR", "message": "An unexpected server error occurred.",
          "retryable": False, "request_id": secrets.token_hex(8),
      }})
  ```

---

### MED-07: Comma-Separated Multi-Origin Configuration Fails Origin Validation in Tests
- **Issue ID**: `API-06`
- **Domain**: API Contracts / Test Infrastructure
- **File Reference**: `backend/settings.py:9, 27-28`, `backend/app.py:54-55`, `.env:2`, `tests/test_discovery.py:158, 220`
- **Technical Root Cause**:
  `backend/settings.py` parses comma-separated origins into `app_origins: frozenset[str]`. However, `backend/app.py:54` sets `app.state.app_origin = settings.app_origin` as the raw comma-separated string (`"http://localhost:5173,http://127.0.0.1:5173/"`).
  When tests in `tests/test_discovery.py` send `headers = {"Origin": app.state.app_origin}`, the header contains commas, failing exact membership against `app_origins`, resulting in HTTP 403 `ORIGIN_REJECTED`.
- **Risk & Impact**:
  Automated tests in `test_discovery.py` fail immediately when `.env` specifies multiple origins.
- **Remediation Recommendation**:
  Set `app.state.app_origin` to the primary origin (e.g. `settings.app_origins_list[0]`), and update tests to pass a single valid origin from `app.state.app_origins`.

---

### MED-08: Undocumented Application Error Codes Returned by API Endpoints
- **Issue ID**: `API-07`
- **Domain**: API Contracts & Specifications
- **File Reference**: `backend/card_api.py:265`, `backend/auth.py:11`, `backend/vacancy_api.py:120, 126, 144, 161, 202`, `backend/discovery_api.py:41, 47, 54, 56, 60, 64`, `backend/app.py:86`
- **Contract Reference**: `Modeus_MVP_Technical_Contract.md`, Section 6 lines 164–172
- **Technical Root Cause**:
  Section 6 documents only 8 standard error codes (`VERSION_CONFLICT`, `IDEMPOTENCY_CONFLICT`, `PROCESSING_IN_PROGRESS`, `VALIDATION_ERROR`, `INVALID_STATE`, `LLM_UNAVAILABLE`, `INVALID_MODEL_OUTPUT`, `PROCESSING_TIMEOUT`).
  The implementation raises 14 additional domain-specific error codes (`CARD_UNCHANGED`, `UNAUTHENTICATED`, `DATABASE_UNAVAILABLE`, `PROFILE_INVALID_TITLE`, `PROFILE_EXISTS`, `HH_ALREADY_RUNNING`, `PROFILE_REVIEW_NOT_READY`, `HH_NOT_CONFIGURED`, `DISCOVERY_ARXIV_DISABLED`, `DISCOVERY_BUSY`, `DISCOVERY_RATE_LIMIT`, `DISCOVERY_TIMEOUT`, `DISCOVERY_QUERY_INVALID`, `DISCOVERY_UNAVAILABLE`).
- **Risk & Impact**:
  Client implementations relying on the formal technical contract cannot anticipate or properly branch on these error codes.
- **Remediation Recommendation**:
  Document the complete error code registry in Section 6 of `Modeus_MVP_Technical_Contract.md`.

---

### MED-09: Severe N+1 Database Query Antipattern in `list_interviews()` and `catalog()`
- **Issue ID**: `LOGIC-07`
- **Domain**: Backend Logic & Performance
- **File Reference**: `backend/interview_api.py:43-49, 179`, `backend/card_api.py:341-350, 368-369`
- **Technical Root Cause**:
  In `list_interviews()`, for every row returned (up to 100), `interview_view()` executes two separate SQL queries: a subquery for `KnowledgeCard.id` and a `db.get(Topic)`. In `catalog()`, for every card returned (up to 100), `public_summary()` executes two separate SQL queries: `db.get(InterviewSession)` and `db.get(Topic)`.
  A single page request fetching 100 items triggers 201 sequential database queries.
- **Risk & Impact**:
  Severe database latency amplification and connection pool exhaustion under modest concurrent traffic.
- **Remediation Recommendation**:
  Batch fetch related topics and cards using `where(Topic.id.in_(...))` or join the tables directly in the primary query.

---

### MED-10: Unbounded Table Scan and Memory Bloat in Candidates Endpoint
- **Issue ID**: `LOGIC-08`
- **Domain**: Backend Logic & Scalability
- **File Reference**: `backend/vacancy_api.py:105-112`
- **Technical Root Cause**:
  `GET /api/v1/vacancies/candidates` executes:
  ```python
  select(InterviewSession.context["role"].as_string()).where(InterviewSession.origin == "human")
  ```
  without a `LIMIT`, pagination, or date filter. The entire history of human interview roles is loaded into Python memory, where `profile_matches()` regex evaluation is executed row-by-row.
- **Risk & Impact**:
  As session history grows into thousands of records, this endpoint causes massive memory allocation and worker thread blocking.
- **Remediation Recommendation**:
  Add a time filter (e.g. `created_at >= now() - timedelta(days=90)`), apply a database `limit(1000)`, or maintain pre-aggregated role counters.

---

### MED-11: Background Task Dropping Leaves Vacancy Collections Locked for 62 Minutes
- **Issue ID**: `LOGIC-12`
- **Domain**: Backend Logic & Concurrency
- **File Reference**: `backend/vacancy_api.py:206-209`, `backend/vacancy_import.py:21-22, 215-217`
- **Technical Root Cause**:
  `POST /vacancies/{profession_id}/collect` acquires a lease via `claim_collection()` and schedules `run_collection` using FastAPI in-memory `BackgroundTasks`. If the web server process restarts or cycles worker processes while collection is pending or running, the in-memory task is aborted. However, `VacancyCollection.run_id` remains in the database for `RUN_LEASE = RUN_TIMEOUT + timedelta(minutes=2)` (62 minutes).
- **Risk & Impact**:
  Editors are completely blocked from triggering HeadHunter collections for 62 minutes with `409 HH_ALREADY_RUNNING` after any server restart.
- **Remediation Recommendation**:
  Add an administrative force-unlock capability or implement worker heartbeat timestamps in `VacancyCollection` to allow resetting runs that have made no progress for 5 minutes.

---

## 5. Low & Informational Severity Findings

### LOW-01: Public Exposure of OpenAPI Schema and Interactive Documentation
- **Issue ID**: `SEC-07`
- **Domain**: Security / Information Disclosure
- **File Reference**: `backend/app.py:50`
- **Description**: FastAPI is initialized with default documentation routes (`/docs`, `/redoc`, `/openapi.json`), which are publicly accessible without authentication or origin checks, revealing complete internal API schemas, enums, and data boundaries.
- **Remediation**: Disable or restrict documentation endpoints in production (`docs_url=None if settings.is_production else "/docs"`).

---

### LOW-02: Fragile Password Hash Splitting in `check_password` Causing 500 Errors
- **Issue ID**: `SEC-08`
- **Domain**: Security / Defensive Coding
- **File Reference**: `backend/security.py:12-15`
- **Description**: `check_password()` splits stored hashes using `_, salt, expected = stored.split("$")`. If stored hash data is malformed or empty, Python raises `ValueError: not enough values to unpack`, resulting in an unhandled 500 error instead of a graceful 401 failure.
- **Remediation**: Validate split length:
  ```python
  parts = stored.split("$")
  if len(parts) != 3 or parts[0] != "scrypt":
      return False
  ```

---

### LOW-03: In-Memory `threading.Lock` Ineffective Across Multi-Worker Deployments
- **Issue ID**: `SEC-09`
- **Domain**: Security & Concurrency / Multi-Process Deployments
- **File Reference**: `backend/discovery_api.py:24, 44-46`
- **Description**: Candidate discovery providers use `collection_locks = {provider: Lock() ...}` instantiated in Python process memory. When deployed with multiple Uvicorn workers (`uvicorn --workers 4`), each worker process maintains an independent lock, allowing concurrent sweeps and triggering third-party API rate limits.
- **Remediation**: Use database-backed concurrency windows (like `HeadHunterRequestWindow`) or distributed locks rather than in-memory `threading.Lock`.

---

### LOW-04: Unnecessary Cryptographic Hash and Database Lookup for Missing Session Cookie
- **Issue ID**: `SEC-10`
- **Domain**: Performance & Code Hygiene (Informational)
- **File Reference**: `backend/auth.py:9-10`, `backend/app.py:121`
- **Description**: When `modeus_session` is missing from request cookies, `request.cookies.get("modeus_session", "")` evaluates to `""`. The application computes `token_hash("")` (SHA-256) and issues a `db.get(LoginSession, ...)` lookup on every single unauthenticated request.
- **Remediation**: Immediately raise 401 if cookie is absent before computing hashes or accessing the database.

---

### LOW-05: Machine-Duplicated Error Messages in API Responses
- **Issue ID**: `API-08`
- **Domain**: API Contracts / Developer Experience
- **File Reference**: `backend/app.py:61-66`, `backend/card_api.py:145, 157, 265`
- **Description**: In `backend/app.py:63`, string exception details default to `{"code": exc.detail, "message": exc.detail}`. Endpoints raising `HTTPException(403, "EDITOR_REQUIRED")` return `message: "EDITOR_REQUIRED"` instead of descriptive human-readable text.
- **Remediation**: Provide descriptive default messages for standard application error codes.

---

### LOW-06: Missing Catalog Filter Implementation Promised in MVP Plan
- **Issue ID**: `API-09`
- **Domain**: API Contracts & Roadmap Alignment
- **File Reference**: `backend/card_api.py:364-370`, `frontend/src/Cards.tsx:28-31`, `Modeus_Interview_MVP_Plan.md:31`
- **Description**: `Modeus_Interview_MVP_Plan.md` specifies that the public catalog provides a filter by topic. `GET /api/v1/catalog` currently supports only `limit` and `cursor`. Neither backend query parameters (`topic_id`, `profession_id`) nor frontend UI controls for filtering are implemented.
- **Remediation**: Implement optional `topic_id` and `profession_id` query filters on `GET /api/v1/catalog`.

---

### LOW-07: Frontend Ignores `unknown_fields` and Omits Card Gap Visuals
- **Issue ID**: `API-10`
- **Domain**: API Contracts & Frontend Completeness
- **File Reference**: `backend/card_api.py:36, 354-355`, `frontend/src/api.ts:27-30`, `frontend/src/Cards.tsx:98-110`
- **Description**: Backend synthesis produces `unknown_fields: list[str]` in card content to indicate knowledge gaps, but the TypeScript `CardContent` interface omits this field and `frontend/src/Cards.tsx` never renders gaps.
- **Remediation**: Add `unknown_fields?: string[]` to `frontend/src/api.ts` and render knowledge gaps in `Cards.tsx`.

---

### LOW-08: Discovery Candidate Review & Assessment Missing from Web API
- **Issue ID**: `API-12`
- **Domain**: API Contracts & Workflow Completeness
- **File Reference**: `backend/discovery_api.py:1-99`, `backend/cli.py:99-108`, `frontend/src/Discovery.tsx:1-234`
- **Description**: `README.md` documents assessing and reviewing discovery candidates via CLI (`assess-discovery`, `review-discovery`). However, `backend/discovery_api.py` exposes no HTTP review endpoints, and `frontend/src/Discovery.tsx` provides no review UI. The workflow can only be completed via terminal.
- **Remediation**: Expose `POST /api/v1/discovery/items/{id}/review` and add approval buttons to the frontend Discovery interface.

---

### LOW-09: Validation Error in Discovery API Returns 502 Bad Gateway
- **Issue ID**: `LOGIC-13`
- **Domain**: Backend Logic & Error Semantics
- **File Reference**: `backend/discovery.py:117-118`, `backend/discovery_api.py:60-67`
- **Description**: When a user calls `POST /api/v1/discovery/collect` for a non-Python profession with empty values, `discovery.py` raises `CollectionError("Specify repositories, queries or topics for this profession")`. In `discovery_api.py:67`, this unmapped message falls through to `raise HTTPException(502, "DISCOVERY_UNAVAILABLE")`, returning a misleading 502 server gateway error for client-side validation failures.
- **Remediation**: Map `"Specify repositories, queries or topics"` to `HTTPException(422, "DISCOVERY_QUERY_INVALID")`.

---

### LOW-10: Rigid Exact Day Match in Weekly Star Change Metric
- **Issue ID**: `LOGIC-14`
- **Domain**: Backend Logic & Analytics
- **File Reference**: `backend/discovery.py:183-185`
- **Description**: In `candidate_output()`, previous repository observations are looked up using the exact primary key `(item.id, (observed - timedelta(days=7)).isoformat())`. If collections were run on day 6 or day 8, the lookup returns `None` and trend data is discarded.
- **Remediation**: Query for the closest observation within a 6–8 day window.

---

### LOW-11: Missing Defensive Null Checks in `public_summary()`
- **Issue ID**: `LOGIC-15`
- **Domain**: Backend Logic & Defensive Programming
- **File Reference**: `backend/card_api.py:341-348`
- **Description**: `public_summary()` accesses `topic.profession_id` and `topic.title` without checking whether `topic` or `session` exists. If a card references a deleted or orphan topic, viewing the public catalog crashes with HTTP 500.
- **Remediation**: Add defensive null checks: `profession_id = topic.profession_id if topic else "unknown"`.

---

## 6. Architecture & Defense-in-Depth Highlights

A thorough audit must also highlight genuine engineering strengths. Modeus Interviewer exhibits commendable architectural rigor in several core subsystems:

1. **Zero SQL Injection (Parameterized SQLAlchemy ORM)**:
   Across the entire backend codebase, zero raw SQL string concatenation was identified. All database operations strictly use parameterized SQLAlchemy 2 Core statements (`select(User).where(...)`), completely eliminating SQL injection risks.
2. **Zero Cross-Site Scripting (XSS)**:
   The React 18 frontend strictly utilizes native JSX escaping. There are zero instances of `dangerouslySetInnerHTML`. Hyperlinks rendered from card evidence strictly enforce protocol validation (`http:`, `https:`), preventing `javascript:` URI attacks.
3. **Strict SSRF Defenses on External Crawlers**:
   External API collectors (`backend/vacancy_import.py`, `backend/discovery.py`) interact exclusively with hardcoded, trusted endpoint domains (`api.hh.ru`, `api.github.com`, `export.arxiv.org`). All parameters undergo strict alphanumeric/regex validation, redirect following is monitored, and arbitrary user-supplied URLs cannot be fetched by the server.
4. **Versioned Optimistic Concurrency Control**:
   Mutating operations across interviews and knowledge cards use a rigorous versioning model (`VersionedRequest.expected_version`). Atomic version increments combined with `StaleDataError` handling prevent race conditions and lost updates under concurrent edits.
5. **Cryptographic Command Idempotency**:
   Mutating API endpoints require `Idempotency-Key` headers. The server records SHA-256 digests of command request bodies in `interview_command_receipts`, returning cached receipts for duplicate deliveries and raising `409 IDEMPOTENCY_CONFLICT` on mismatched payloads.
6. **Object-Level Access Control (IDOR Prevention)**:
   Participant endpoints rigorously scope operations to the authenticated user via `owned()` in `backend/interview_api.py` and `accessible_card()` in `backend/card_api.py`. Multi-tenant data leakage is effectively prevented.
7. **Strict Cross-Origin Request Validation**:
   Mutating endpoints enforce origin validation via `require_origin()` in `backend/app.py`, comparing incoming `Origin` headers against a configured whitelist to mitigate CSRF attacks on session cookies.

---

## 7. Consolidated Remediation Roadmap

The findings have been consolidated into a prioritized, actionable remediation roadmap:

```
+-----------------------------------------------------------------------------------+
| PRIORITY 1: IMMEDIATE CRITICAL PATCHES (Sprint 1)                                |
| - Patch worker crash: Add safe dictionary unpack in interview_worker.py:131      |
| - Patch worker crash: Add null guards in execute_stage and mark_failed           |
| - Fix catalog profession: Resolve active topic/role in card_api.py:346           |
| - Fix login DoS: Move locked_until check before check_password in app.py:93       |
| - Sync virtual environment: Run pip install -r requirements.lock                 |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| PRIORITY 2: STATE MACHINE & CONCURRENCY REPAIR (Sprint 1 - 2)                     |
| - Add fallback in advance_case() to prevent StopIteration and ZeroDivisionError  |
| - Add null guards in retry_interview() for job and answer_id                      |
| - Safe intent indexing in submit_answer() and skip_interview()                   |
| - Fix pagination ordering: order_by created_at desc, id desc                     |
| - Prevent fact erasure: Retain structured content on text edits in card_api.py   |
| - Add global 500 JSON exception handler in app.py                                |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| PRIORITY 3: ARCHITECTURAL HARDENING & CLEANUP (Sprint 2 - 3)                      |
| - Implement periodic pruning of expired login_sessions and command receipts      |
| - Revoke active sessions upon account lockout and check lockout in current_user  |
| - Configure COOKIE_SECURE=True by default in production                          |
| - Add request body size limit middleware (max 2 MB) and tighten Pydantic limits  |
| - Batch database queries to eliminate N+1 latency in list_interviews & catalog   |
| - Add pagination or time bounds to GET /vacancies/candidates                     |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| PRIORITY 4: SPECIFICATION & CONTRACT SYNCHRONIZATION (Sprint 3)                  |
| - Synchronize Modeus_MVP_Technical_Contract.md Section 4 with vacancy tasks[]    |
| - Document all 12 missing routes (Vacancies, Discovery, Health) in Section 6     |
| - Publish complete application error code registry in technical contracts         |
| - Implement missing catalog topic filter in API and UI                           |
+-----------------------------------------------------------------------------------+
```

---
*Report compiled and certified by Forensic Audit Team.*
