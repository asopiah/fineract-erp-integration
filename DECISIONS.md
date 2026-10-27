# Decisions

Trade-offs, the reasons behind them, what was deliberately not built, and what is weak. Assumptions made where the
brief was open are listed at the end, together with the disclosure of AI assistance and references.

## Fineract customization: extend the CREATE command

The options were a new endpoint, an extension of the existing GL command handler, or a Fineract-side component.

**Choice: extend `GLACCOUNT/CREATE`.**

* **What it keeps.** The create still runs through Fineract's command pipeline: the permission check
  (`CREATE_GLACCOUNT`), maker-checker, the audit row in `m_portfolio_command_source`, and the command retry. A new
  endpoint would have to rebuild all four, and a thin wrapper would enforce nothing inside Fineract.
* **Where the rules run.** `GLAccountWritePlatformServiceJpaRepositoryImpl.createGLAccount`, inside the command's
  transaction. The transaction boundary is `CommandSourceService.processCommandAndSaveResult` (`@Transactional`).
  The lookup, the insert and the unique constraint are therefore one unit.
* **Race handling.** Fineract's command retry wraps that transaction from the outside, so a retried attempt gets a
  fresh transaction. A create that loses the unique-constraint race throws a `ConcurrencyFailureException`, which
  that retry is configured for. The retry then resolves against the committed winner.

  Retrying inside the failed transaction is impossible: it is rollback-only, and on PostgreSQL it is aborted. A
  `REQUIRES_NEW` sub-transaction would have split the account from its audit row.
* **409, not 403.** Fineract maps domain-rule exceptions to 403, the same status as "no permission". A dedicated
  exception mapper returns 409, so a client can tell "this id already means something else" from "you may not".
* **The lookup order was found by a test.** Checking the GL code before the external id is what makes an account
  committed between the two lookups resolve as a replay. The parallel integration test caught the first version,
  which got 1 × 403 in 10 concurrent creates.
* **Not used: Fineract's `Idempotency-Key`.** It does not compare payloads (a different body replays the cached
  result), and it replays cached errors, which would pin a transient failure.

## Synchronous calls, no queue

The partner gets the outcome in the same request when everything is up. That is the common case and the simplest
contract.

A broker (Kafka/RabbitMQ) or a worker (Celery) would add a second source of truth for in-flight work, with its own
delivery guarantees to reconcile against the database. Instead, the `operations` table *is* the durable queue:

* every status is committed before each external call;
* a lease (owner token + expiry) names the current owner, and the row's version makes a former owner's writes fail;
* `next_attempt_at` holds the backoff schedule.

The recovery sweep is a thread in the service that runs at startup, every 5 s, and on demand. It executes due
operations with the same code the request used. This meets "recovery without a worker" and survives a crash (J5).
The cost is that the API process also does background work; at a much higher volume the sweep would move to its own
process, still reading the same table.

## Database constraints are the arbiter

Application checks give good error messages; constraints make them true under concurrency.

* `operations UNIQUE (type, external_reference)`: one operation per reference and type.
* A partial unique index allows one active `LOAN_APPROVAL` and one active `DISBURSEMENT` per loan. FAILED rows are
  excluded: they never reached Fineract, so they must not block a corrected request.
* `clients.client_uid`, `loans.loan_reference` and `loan_transactions.external_reference` are unique, and the claim
  inserts them **in the same transaction** as the operation row.
* The Mock ERP enforces its own uniqueness (`external_reference`, `code`) and refuses unbalanced entries. It also has
  CHECK constraints so no line can be both a debit and a credit.
* Fineract enforces unique `external_id` on clients, loans and loan transactions. I verified this on the PostgreSQL
  schema with `pg_indexes`, rather than trusting the JPA annotations.

No row lock is held across an HTTP call. Leases (an atomic conditional `UPDATE` with an owner token) plus optimistic
locking on the operation row give single ownership without one.

## Idempotency

| Question | Answer |
| --- | --- |
| What identifies an operation? | `(type, external_reference)`; the payload fingerprint decides replay versus conflict |
| What is a "same payload"? | SHA-256 of the canonical validated body; decimals are normalized (`5000000` = `5000000.00`) |
| What does a replay return? | The stored result body (200), or the stored error. Responses are built from persisted state, so a first response and its replay serialize identically. |
| What prevents double processing? | The four layers in [ARCHITECTURE.md](ARCHITECTURE.md#concurrency-and-the-exactly-once-layers). The last line is Fineract itself: a single-tranche loan cannot be disbursed twice, and a transaction external id cannot repeat. |
| Can a reference be reused after a rejection? | No: it replays the rejection. A corrected request uses a new reference, and keys reserved by the rejected one (a `client_uid`) are released. |

## Retries and partial failure

* **Never retry a financial call without first checking whether it happened.** Every Fineract attempt starts with a
  lookup by external id. A timeout (unknown outcome) can never end as FAILED: only certainty that nothing was
  delivered, or Fineract's rejection, can.
* **Classification decides safety.**
  * *Not applied* (connect error, 429, 503, a Fineract lock conflict): resend after backoff.
  * *Rejected* (4xx): final. But first look again, because Fineract's uniqueness may be refusing our own earlier
    attempt.
  * *Unknown* (read timeout, 500, 502, 504): look up, never resend blindly.
* **ERP after Fineract.** The ERP posting happens only after Fineract confirms, and it is derived from Fineract's
  stored record. A retry sends the identical entry, which the ERP answers with the entry it already holds. ERP
  outages therefore delay the journal (202 `ERP_PENDING`) but cannot duplicate it.
* **Budgets.** In-request: 3 Fineract attempts (0.5 s, then 1 s). Background: 20 attempts with exponential backoff
  from 2 s, capped at 300 s, so about an hour. Then NEEDS_RECONCILIATION, visible in the dead-letter view, where
  `POST /api/admin/operations/{type}/{ref}/resume` re-drives an ERP posting explicitly and idempotently.
* **A failed lookup proves nothing.** It means "not applied" only if nothing was submitted yet. Failing to confirm
  after a submit is an unknown outcome.
* **Contradictions go to a human.** If Fineract's record disagrees with the request (another amount, another type, a
  reversal), the operation becomes NEEDS_RECONCILIATION. The service never "fixes" money on its own.

## Configuration decisions (Part B)

| Decision | Choice and reason |
| --- | --- |
| Accounting rule | **Cash-based.** Fineract posts at the same events the ERP mirrors (disbursement, repayment), so there are no accrual entries the ERP would have to learn to ignore. Accrual-based would add daily accrual postings that the brief's mapping does not cover. |
| Loan product | *SME Asset Finance*: RWF, whole francs, 18% a year declining balance, 12 monthly equal installments, single disbursement (`multiDisburseLoan=false`), mifos standard allocation, interest recalculation off. It maps 9 synced GL accounts: fund source, loan portfolio, transfers in suspense, overpayment liability, interest, fee, penalty and recovery income, and write-off. |
| Office | *Kigali Main Office* (`externalId KGL-MAIN`), under Head Office. Clients and the API user belong to it. |
| GL accounts | Created from the ERP with `manualEntriesAllowed=false`: the ERP owns the ledger, and Fineract posts only from loan transactions |
| Least privilege | Role *Integration API* has 13 permissions: read/create GL account, read office, loan product and payment type, read/create/activate client, read/create/approve/disburse/repay loan. The bootstrap re-applies exactly this set on each run. `ACTIVATE_CLIENT` was found missing by a 403 in testing (clients are created active). |
| Reference data | The service resolves office, product and payment type by business key (external id or name), never by a configured numeric id |
| Database | PostgreSQL: Fineract's default, and its 1.15 image ships the driver |

## Python framework

**FastAPI** for both services:

* typed request and response models (pydantic) give validation and the OpenAPI documentation from one definition;
* dependency injection keeps the routes thin;
* synchronous endpoints run in a thread pool, which suits blocking SQLAlchemy and httpx without async plumbing.

SQLAlchemy 2.0 is used with typed mappings, and Alembic for the integration service's schema. The Mock ERP uses
`create_all`: it is a stand-in with no upgrade path. Both services pass `mypy --strict` and Ruff.

## What happened to FIN-2026-000001?

Recorded in the final evidence run (simulation *Tracing FIN-2026-000001* in [SIMULATIONS.md](SIMULATIONS.md)). The
partner sent a repayment with `X-Correlation-ID: partner-FIN-2026-000001` while the ERP was briefly unavailable.

**Step 1: one API call.** `GET /api/transactions/FIN-2026-000001` returns the status, Fineract's record, the ERP
entry, and the correlation id to search the logs for:

```json
{"external_reference": "FIN-2026-000001", "type": "REPAYMENT", "loan_reference": "LN-2026-000012",
 "status": "COMPLETED", "correlation_id": "partner-FIN-2026-000001", "attempts": 0, "error": null,
 "recorded": {"amount": "458400", "principal_portion": "383400", "interest_portion": "75000",
              "outstanding_after": "4616600", "erp_entry_number": "JE-000032"}}
```

**Step 2: the logs.** Filter every service's JSON logs on that correlation id (or on `external_reference`):

```bash
docker compose logs --no-log-prefix integration-service mock-erp | grep partner-FIN-2026-000001
```

| time (UTC) | service | event | status | fineract_transaction_id | erp_transaction_id |
| --- | --- | --- | --- | --- | --- |
| 02:01:28.770 | integration-service | operation fineract_pending | FINERACT_PENDING | | |
| 02:01:29.391 | integration-service | submitted to Fineract | FINERACT_PENDING | | |
| 02:01:29.413 | integration-service | operation fineract_done | FINERACT_DONE | 44 | |
| 02:01:29.421 | integration-service | operation erp_pending | ERP_PENDING | | |
| 02:01:29.438 | integration-service | deferred to recovery (attempt 1, next in 2.0s): ERP is unavailable | ERP_PENDING | | |
| 02:01:31.675 | mock-erp | journal entry posted | POSTED | | JE-000032 |
| 02:01:31.681 | integration-service | operation completed | COMPLETED | | JE-000032 |

**Reading it:**

* Fineract recorded the repayment in 0.6 s (its transaction 44, split 383,400 principal and 75,000 interest).
* The ERP was down, so the posting was deferred; the partner received 202 `ERP_PENDING`.
* 2.2 s later, recovery posted entry `JE-000032`.

The ERP's own log line carries the same correlation id because the integration forwards it, and recovery logs under
the partner's original id because it is stored on the operation. Every line also carries `external_reference`,
`client_uid` and `loan_reference`; they are omitted from the table for width.

## Found in review

An independent correctness review of the finished code found the following. Each is now fixed and pinned by a test
(`integration-service/tests/component/test_review_findings.py`).

* **Critical:** a failed confirmation lookup after a successful submit was classified "not applied". An applied
  repayment could then be reported as "not processed" (503), so the ERP never got the entry. It is now an unknown
  outcome and is deferred to recovery.
* **NEEDS_RECONCILIATION was a dead end** (the repair transition existed but nothing used it), and an ERP outage of
  about 5 minutes exhausted the retry budget. Fixed with the explicit resume repair and a 20-attempt budget.
* **The fingerprint ignored the loan in the URL:** the same repayment reference on another loan replayed the first
  loan's result. The loan is now part of the fingerprint.
* **Leases did not guarantee a single executor.** The expiry used the transaction's start time, writes were
  unconditional, and a former owner could release the new owner's lease. Fixed with owner tokens,
  `clock_timestamp()`, renewal before each call and optimistic locking.
* **Minor:**
  * unexpected exceptions looped without counting attempts;
  * a disbursement and a repayment could share a reference (Fineract and the ERP cannot);
  * reconciliation collapsed transactions that had no reference;
  * two concurrent replays of a retryable failure could both answer 201.

## What I chose not to build

* **Stretch tier (L).** Only the pieces the core needed: the dead-letter view, fee, penalty and overpayment lines in
  the mapping, and reversal-aware reconciliation.
* **Automatic reversal postings.** A reversal in Fineract shows in reconciliation as `AMOUNT_MISMATCH`, with a note.
  Posting the reversing entry (`{reference}:REVERSAL` with opposite lines, derived from Fineract's reversal
  transaction) is the documented next step.
* **ERP master data updates.** The sync is create-only, as the brief says. A renamed ERP account is not propagated.
* **Multi-currency, fees and penalties in the product, and multi-tranche loans.**
* **Partner webhooks, metrics, a message broker.** See *Synchronous calls, no queue*.
* **Upstream fixes outside the brief.** Fineract maps a duplicate GL code by searching the error text for
  `acc_gl_code`, which never matches on PostgreSQL (`acc_gl_account_gl_code_key`). The external-id path does not
  depend on it; the plain create path is left as upstream has it.

## Known weaknesses

* **One integration instance is assumed.** Migrations run at container start. Leases make concurrent executors safe
  per operation, but several instances were not tested.
* **Reconciliation reads every originated loan from Fineract** (one call per loan). That is fine for this volume; at
  scale it would read Fineract's transaction reports, or consume its business events, by date.
* **Reconciliation only sees loans the integration originated.** Transactions on loans created directly in Fineract
  are out of its scope.
* **The lookup-before-submit relies on Fineract's read-after-write.** If a lookup missed a committed transaction,
  resubmitting would be refused by Fineract's unique external id, and the executor would look again. That case is
  covered by a test, but it is the last line of defence, not the first.
* **Fault injection is mounted in the compose stack** (`FAULT_INJECTION_ENABLED=true`) so the simulations can run.
  It is off by default in the services' own settings and must stay off in production.
* **Plain HTTP inside the compose network, static bearer tokens, development credentials in `docker-compose.yml`.**
  Acceptable for a local assessment stack only.
* **Interest is whatever Fineract computes.** The lifecycle uses back-dated monthly repayments and a payoff of
  Fineract's total outstanding, which includes interest scheduled for future periods.
* **The Java integration test runs against an already running Fineract** (`-PcargoDisabled`), not Fineract's own
  Cargo harness.

## Assumptions

* Amounts are whole RWF: Fineract's RWF currency has 0 decimal places, and amounts with decimals are refused with 422.
* The partner supplies business dates (submission, approval, transaction). Fineract refuses future-dated
  transactions.
* A loan application is approved by a separate call with its own reference (the brief allowed either).
* Disbursement amount: Fineract decides whether it may differ from the approved principal. The integration passes it
  through.
* "Same payload" is decided on the validated, normalized body, not the raw bytes.

## AI assistance and references

The brief allows AI assistants and online references, as long as they are disclosed.

* **AI assistance.** I built this with an AI coding assistant (Claude, in Claude Code) as a pair programmer. It
  helped with:
  * exploring Fineract's command pipeline, retry and error-mapping code;
  * drafting the Java patch, the Python services, the tests, the simulations and these documents.

  I directed the design, and reviewed, ran and verified everything. All evidence in `evidence/` and
  [SIMULATIONS.md](SIMULATIONS.md) comes from real runs.
* Apache Fineract source (tag `1.15.0`) was the primary reference for every Fineract behaviour relied on here.
