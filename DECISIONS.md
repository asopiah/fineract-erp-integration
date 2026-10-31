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
* **Race handling.** A create that loses the unique-constraint race throws a `ConcurrencyFailureException`, which
  Fineract's command retry already handles: it runs the request again in a fresh transaction, where it resolves to
  the winner. Retrying inside the failed transaction is impossible (it is rollback-only), and a `REQUIRES_NEW`
  sub-transaction would have split the account from its audit row.
* **409, not 403.** Fineract answers domain-rule exceptions with 403, the same status as "no permission". A dedicated
  mapper returns 409, so a client can tell "this id already means something else" from "you may not".
* **Not used: Fineract's `Idempotency-Key`.** It does not compare payloads, and it replays cached errors.

The details, including the edge cases, are in [fineract-customization/README.md](fineract-customization/README.md).

## Fineract 1.15.0, not 1.16.x

The brief asks for an official release tag, pinned. 1.15.0 was the newest release that had been out for a while when
the work started: 1.16.0 had been out for eight days, and 1.16.1 for one. 1.16 also moves the build to JDK 25 and
Gradle 9, and the brief warns that builds are version-sensitive. Everything here is verified on 1.15.0.

The patch is ready for the move: it applies cleanly to 1.16.1 (`git apply --check`). Its schema change sits in
Fineract's `db/custom-changelog` extension point, so it can't collide with upstream's numbered changelogs. Upgrading
means changing the tag, the commit and the JDK, then rerunning `make test-java`, `make test-java-regression` and
`make test-stack`.

## GL sync: an explicit call, create-only

The brief leaves the trigger open. The sync runs when something calls `POST /api/accounts/sync`: the bootstrap does,
before it creates the loan product, and an operator can at any time.

* **Why not an ERP webhook:** the mock ERP would need outbound delivery, retries and signatures, and a missed
  webhook still needs a sweep to catch up. The explicit call *is* that sweep.
* **Why not polling on a timer:** the chart changes rarely, and a new account matters only once a product maps it,
  which is a deliberate configuration step anyway.
* **Create-only**, as the brief says. Each account is a `gl_account_syncs` row with its own status, so one failing
  account doesn't block the others, and recovery retries transient failures, re-reading the account from the ERP.
* **One currency.** Fineract GL accounts carry no currency, so an ERP account in another currency than the loan
  product's (RWF) is refused: FAILED, not retried, with the reason. Otherwise it would sync silently and receive RWF
  postings.

## Synchronous calls, no queue

The partner gets the outcome in the same request when everything is up. That is the common case and the simplest
contract.

A broker (Kafka/RabbitMQ) or a worker (Celery) would add a second source of truth for in-flight work, with its own
delivery guarantees to reconcile against the database. Instead, the `operations` table *is* the durable queue:

* every status is committed before each external call
* a lease (owner token + expiry) names the current owner, and the row's version makes a former owner's writes fail
* `next_attempt_at` holds the backoff schedule

The recovery sweep is a thread in the service that runs at startup, every 5 s, and on demand. It executes due
operations with the same code the request used. This meets "recovery without a worker" and survives a crash (J5).
The cost is that the API process also does background work; at a much higher volume the sweep would move to its own
process, still reading the same table.

## Database constraints are the arbiter

Application checks give good error messages; constraints make them true under concurrency.

* `operations UNIQUE (type, external_reference)`: one operation per reference and type.
* A partial unique index allows one active `LOAN_APPROVAL` and one active `DISBURSEMENT` per loan. FAILED rows are
  excluded: Fineract either refused them or never received them, so they must not block a corrected request.
* `clients.client_uid`, `loans.loan_reference` and `loan_transactions.external_reference` are unique, and the claim
  inserts them **in the same transaction** as the operation row.
* The Mock ERP enforces its own uniqueness (`external_reference`, `code`) and refuses unbalanced entries. It also has
  CHECK constraints so no line can be both a debit and a credit.
* Fineract enforces unique `external_id` on clients, loans and loan transactions. I verified this on the PostgreSQL
  schema with `pg_indexes`, rather than trusting the JPA annotations.

Partner operations never hold a row lock across an HTTP call. Leases (an atomic conditional `UPDATE` with an owner
token) plus optimistic locking on the operation row give single ownership without one. The one exception is GL
sync, an operator action: it locks the account's sync row while it calls Fineract, so two syncs of the same account
take turns.

**Why leases, not `SELECT … FOR UPDATE SKIP LOCKED`:** a lock lives as long as a transaction, so holding one across a
Fineract call would keep a transaction open for up to the whole call (seconds, or a 15 s timeout). A lease is a
committed fact: it survives the transaction, it shows in the table who owns the work and until when, and it expires
on its own if the owner dies (J5).

**The numbers.** Lease 60 s by default (20 s in the compose stack, so the crash simulation recovers quickly); it is
renewed before each Fineract call. Recovery sweeps every 5 s. A request tries Fineract 3 times before handing over
to recovery, which then backs off exponentially from 2 s up to 300 s for at most 20 attempts. All of these are
settings (`INTEGRATION_*`).

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
  * *Not applied* (connect error, 408, 429, 503, a Fineract lock conflict, refused credentials): resend after
    backoff.
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
| Database | PostgreSQL: Fineract's default, and its 1.15 image ships the driver. One server with a database and an owner role per service keeps the stack light; nothing crosses databases, so splitting them later is a configuration change. |

## Python framework

**FastAPI** for both services:

| Why | |
| --- | --- |
| Validation and API docs from one definition | Pydantic request and response models generate the OpenAPI document |
| Thin routes | Dependency injection supplies the session, auth and services |
| Simple concurrency | Plain `def` endpoints run in a thread pool, which suits blocking SQLAlchemy and httpx |

Flask would need extensions for validation and OpenAPI; Django REST Framework brings an ORM and admin this service
doesn't need.

SQLAlchemy 2.0 is used with typed mappings, and Alembic for the integration service's schema. The Mock ERP uses
`create_all`: it is a stand-in with no upgrade path. Both services pass `mypy --strict` and Ruff.

## What happened to FIN-2026-000001?

Recorded in the final evidence run on 31 October 2026 (UTC), by the trace scenario of `make test-stack`; `make
simulate` repeats it under a suffixed reference in [SIMULATIONS.md](SIMULATIONS.md). The partner sent a repayment
with `X-Correlation-ID: partner-FIN-2026-000001` while the ERP was briefly unavailable.

**Step 1: one API call.** `GET /api/transactions/FIN-2026-000001` returns the status, Fineract's record, the ERP
entry, and the correlation id to search the logs for:

```json
{"external_reference": "FIN-2026-000001", "type": "REPAYMENT", "loan_reference": "LN-2026-000013",
 "status": "COMPLETED", "correlation_id": "partner-FIN-2026-000001", "attempts": 0, "error": null,
 "recorded": {"amount": "458400", "principal_portion": "383400", "interest_portion": "75000",
              "outstanding_after": "4616600", "erp_entry_number": "JE-000034"}}
```

**Step 2: the logs.** Filter every service's JSON logs on that correlation id (or on `external_reference`):

```bash
docker compose logs --no-log-prefix integration-service mock-erp | grep partner-FIN-2026-000001
```

| time (UTC) | service | event | status | fineract_transaction_id | erp_transaction_id |
| --- | --- | --- | --- | --- | --- |
| 00:47:41.425 | integration-service | operation fineract_pending | FINERACT_PENDING | | |
| 00:47:41.740 | integration-service | submitted to Fineract | FINERACT_PENDING | | |
| 00:47:41.742 | integration-service | operation fineract_done | FINERACT_DONE | 47 | |
| 00:47:41.744 | integration-service | operation erp_pending | ERP_PENDING | | |
| 00:47:41.748 | integration-service | deferred to recovery (attempt 1, next in 2.0s): ERP is unavailable | ERP_PENDING | | |
| 00:47:43.838 | mock-erp | journal entry posted | POSTED | | JE-000034 |
| 00:47:43.846 | integration-service | operation completed | COMPLETED | | JE-000034 |

**Reading it:**

* Fineract recorded the repayment in 0.3 s (its transaction 47, split 383,400 principal and 75,000 interest).
* The ERP was down, so the posting was deferred; the partner received 202 `ERP_PENDING`.
* 2.1 s later, recovery posted entry `JE-000034`.

The ERP's own log line carries the same correlation id because the integration forwards it, and recovery logs under
the partner's original id because it is stored on the operation. Every line also carries `external_reference`,
`client_uid` and `loan_reference`; they are omitted from the table for width.

## Found in review

The finished code went through two review rounds, each by fresh AI reviewers with no access to my reasoning, and
every finding was reproduced before it was fixed. Each fix is pinned by a test
(`integration-service/tests/component/test_review_findings.py`, and `GLAccountExternalIdValidationTest` for the Java
patch).

**Round one:**

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
  * unexpected exceptions looped without counting attempts
  * a disbursement and a repayment could share a reference (Fineract and the ERP cannot)
  * reconciliation collapsed transactions that had no reference
  * two concurrent replays of a retryable failure could both answer 201

**Round two**, on the finished submission:

* **An unexpected error type still bypassed the retry budget.** The first fix only covered some exception types; an
  ERP answer that wasn't JSON, for example, still left the operation untouched, so recovery retried it every 5
  seconds forever. Any unexpected failure while an operation is in flight is now deferred with backoff and counts
  toward the 20-attempt budget.
* **A long `X-Correlation-ID` header broke every write** (the column holds 100 characters). Both services now accept
  only 1–100 safe characters and mint a new id otherwise.
* **Refused credentials looked like business rejections.** After a password rotation, every request would have
  failed permanently and released its reserved keys. A 401, 407 or Fineract's "not authorized" 403 is now "not
  applied": safe to resend once the configuration is fixed.
* **The operator repair trusted the stored record.** It now re-reads Fineract first and refuses if the transaction
  was reversed or changed since.
* **Smaller:**
  * amounts like `5E+6` reached Fineract in exponent form; they are now always sent as plain digits
  * the lease is renewed before each Fineract call, not once per attempt
  * an empty reconciliation request returned 500; it is now a 422
  * the Java patch silently treated a blank `externalId` as "none"; it is now rejected
  * one Java integration test would have passed even without the rule it tested

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
* **Security is local-stack grade.**
  * Plain HTTP inside the compose network.
  * Development credentials have defaults in the code and in `docker-compose.yml`. A production build should refuse
    to start without them.
  * One bearer token covers the partner API and the admin routes (recovery, resume, the operations view); production
    needs a separate operator credential.
  * Fineract's own error message is passed back to the partner in `FINERACT_REJECTED`. That is useful here, but it
    can expose internals.
* **Reconciliation compares type and amount**, not the entry date or the principal/interest split, and it doesn't
  tell "still being posted" (`ERP_PENDING`) apart from "lost". `GET /api/transactions/{ref}` answers the former.
* **Client creation and loan applications have no repair action.** If one ends in NEEDS_RECONCILIATION (Fineract
  never answered clearly within the retry budget), an operator has to check Fineract by its external id and fix the
  row. Only money movements have the `resume` repair.
* **Changes made directly in Fineract are trusted.** If a loan was approved in Fineract's UI, an approval request
  for it is answered from Fineract's record without comparing the approval date.
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

The brief allows AI assistants and online references, as long as their use is disclosed.

**AI assistance.** I used an AI coding assistant (Claude, in Claude Code) throughout:

| For | How |
| --- | --- |
| Learning | Exploring Fineract's source: the command pipeline, the retry configuration, error mapping, Liquibase |
| Code | Generating most of the code (the Java patch, both Python services, the simulations) from my design and instructions |
| Testing | Writing tests, running the suites and simulations, and investigating failures |
| Review | Independent review rounds whose findings are listed under [Found in review](#found-in-review) |
| Documentation | Drafting these documents |

I set the design and the approach: extending Fineract's own command, the operations table as the state machine and
work queue, lookup-before-submit, Fineract as the source of every amount. I directed each step, reviewed every
change, and can explain and modify any part. All evidence in `evidence/` and [SIMULATIONS.md](SIMULATIONS.md) comes
from real runs.

**References.**

* Apache Fineract's source at tag `1.15.0`, for every Fineract behaviour relied on here.
