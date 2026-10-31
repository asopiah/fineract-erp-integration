# Fineract ↔ ERP integration

[![CI](https://github.com/asopiah/fineract-erp-integration/actions/workflows/ci.yml/badge.svg)](https://github.com/asopiah/fineract-erp-integration/actions/workflows/ci.yml)

A financing partner sends loan requests. **Apache Fineract** (the core banking system) records the loans and the
money. An **ERP** (the accounting system) owns the chart of accounts and gets a balanced journal entry for every money
movement. The **integration service** in between keeps all three in step, and keeps the books right when requests are
duplicated, time out, arrive concurrently or fail half-way.

## How it fits together

```
                           Financing partner
                                  │  REST · bearer token · X-Correlation-ID
                                  ▼
              ┌───────────────────┴──────────────────┐
              │ Integration service   :58090 Python  │
              │ idempotency · state machine          │
              │ recovery · reconciliation            │
              └────────┬────────────────────┬────────┘
                     ① │                  ② │
                       ▼                    ▼
             ┌────────────────────┐ ┌────────────────────┐
             │ Apache Fineract    │ │ Mock ERP           │
             │ 1.15.0 + GL patch  │ │ chart of accounts  │
             │ loans and money    │ │ journal entries    │
             │ :58080 Java        │ │ :58001 Python      │
             └────────────────────┘ └────────────────────┘

 ① Look the operation up by its external id, submit it only if it isn't there, read back what Fineract recorded.
 ② Post a journal entry built from Fineract's record. (GL sync reads the ERP's chart of accounts the same way.)

 Everything runs in docker compose. Each service has its own database on one PostgreSQL server (:55432), and a
 one-shot bootstrap configures the empty Fineract.
```

Each step is saved before the next one starts, so after any failure the service knows exactly what's left to do,
and a background sweep finishes it.
[ARCHITECTURE.md](ARCHITECTURE.md) has the full picture.

## What's here

| Part | What it does |
| --- | --- |
| [`fineract-customization/`](fineract-customization/README.md) | A patch to Fineract (Java): GL accounts get an ERP external id. A repeated create returns the same account, a conflicting one gets 409, even under concurrent creates. |
| `mock-erp/` | The ERP (Python/FastAPI): chart of accounts and balanced journal entries, idempotent on `external_reference`. |
| `integration-service/` | The partner API (Python/FastAPI): clients, loans, approvals, disbursements, repayments, GL sync, recovery, reconciliation. Also the simulations. |

**Tiers completed**

| Tier | Status |
| --- | --- |
| 1. Core (A–G) | **Done.** Every part runs against the live stack. |
| 2. Reliability (H–K) | **Done.** I1–I3, J1–J5 and K run as automated tests and produce the evidence in [SIMULATIONS.md](SIMULATIONS.md). |
| 3. Stretch (L) | Partly. **Repayment reversal, end to end and live**: undone in Fineract, mirrored by a reversing ERP entry, matched by reconciliation (`make simulate-reversal`). Also the dead-letter view (`GET /api/admin/operations`), fee, penalty and overpayment lines in the accounting mapping (unit-tested) and the scheduled recovery sweep. |

## Run it

### 1. Prerequisites

| To… | You need |
| --- | --- |
| Run the stack and try the API | Docker with Compose v2, about 6 GB of memory for Docker, `make`, `curl` (and `jq` for the walkthrough below) |
| Run the Python tests and the simulations | Python 3.12 (`make` creates a virtualenv in `.venv/`) |
| Run the Java tests, or build Fineract faster | JDK 21 and `git` |

Ports used on your machine: 55432, 58001, 58080, 58090. See [Troubleshooting](#troubleshooting) if one is taken.

### 2. Start everything

```bash
cd fineract-erp-integration
make up
```

`make up` builds the images, starts PostgreSQL, Fineract, the Mock ERP and the integration service, waits until all
are healthy, then runs the one-shot bootstrap that configures the empty Fineract:

* office *Kigali Main Office* and currency RWF
* a least-privilege role and API user
* the GL accounts, synchronized from the ERP
* loan product *SME Asset Finance* (18 % a year, 12 monthly repayments, cash accounting)

**The first run takes 15–20 minutes**, because Docker builds the patched Fineract from source. Later runs take about
2–3 minutes. With JDK 21 installed, `make fineract-image` first builds the same image in about 8 minutes.

It is ready when the output ends with:

```
{"...", "service": "bootstrap", "event": "Fineract is configured", ...}
fineract-erp-bootstrap-1 exited with code 0
```

No `make`? Run the same steps by hand:

```bash
docker compose up -d --build --wait postgres fineract mock-erp integration-service
docker compose up bootstrap
```

### 3. Walk through a loan

Each step below is one call to the partner API. Run them in order in one terminal. The outputs shown are from a
fresh stack; if you ran the simulations first, the loan and entry numbers will be higher.

**Set up** two shell variables, so the commands stay short:

```bash
API=localhost:58090/api
AUTH=(-H 'Authorization: Bearer integration-local-token' -H 'Content-Type: application/json')
```

**Step 1 — Create a client.** The partner names the client with its own id (`client_uid`). Fineract's numeric ids
never appear in this API.

```bash
curl -s "${AUTH[@]}" $API/clients -d '{
  "external_reference": "CLI-0001", "client_uid": "SME-000001",
  "first_name": "Amina", "last_name": "Uwase", "activation_date": "2026-01-10"}' | jq -c
```
```
{"last_name":"Uwase","client_uid":"SME-000001","first_name":"Amina","external_reference":"CLI-0001"}
```

**Step 2 — Apply for a loan** of 5,000,000 RWF at 18 % over 12 months. The service issues a `loan_reference`; the
command saves it in `LOAN` for the next steps.

```bash
LOAN=$(curl -s "${AUTH[@]}" $API/loans -d '{
  "external_reference": "APP-0001", "client_uid": "SME-000001",
  "principal": "5000000", "annual_interest_rate": "18", "term_months": 12,
  "submitted_on": "2026-01-15", "expected_disbursement_on": "2026-01-15"}' | jq -r .loan_reference)
echo $LOAN
```
```
LN-2026-000001
```

**Step 3 — Approve it.**

```bash
curl -s "${AUTH[@]}" $API/loans/$LOAN/approve -d '{
  "external_reference": "APR-0001", "approved_on": "2026-01-15"}' | jq -c
```
```
{"approved_on":"2026-01-15","loan_reference":"LN-2026-000001","external_reference":"APR-0001"}
```

**Step 4 — Disburse it.** Fineract records the disbursement, then the ERP gets a journal entry built from Fineract's
record (`erp_entry_number`).

```bash
curl -s "${AUTH[@]}" $API/loans/$LOAN/disburse -d '{
  "external_reference": "DISB-0001", "amount": "5000000", "transaction_date": "2026-01-15"}' \
  | jq -c '{type, amount, outstanding_after, erp_entry_number}'
```
```
{"type":"DISBURSEMENT","amount":"5000000","outstanding_after":"5000000","erp_entry_number":"JE-000001"}
```

**Step 5 — Repay the first instalment.** Fineract decides how the payment splits into principal and interest; the
service copies that split into the ERP entry, never computing it itself.

```bash
curl -s "${AUTH[@]}" $API/loans/$LOAN/repayments -d '{
  "external_reference": "REP-0001", "amount": "458400", "transaction_date": "2026-02-15"}' \
  | jq -c '{amount, principal_portion, interest_portion, erp_entry_number}'
```
```
{"amount":"458400","principal_portion":"383400","interest_portion":"75000","erp_entry_number":"JE-000002"}
```

**Step 6 — Look at the ERP entry.** Debit Bank, credit Loan Receivable with the principal and Interest Income with
the interest:

```bash
curl -s -H 'Authorization: Bearer erp-local-token' localhost:58001/api/journal-entries/REP-0001 | jq -c .lines
```
```
[{"account_code":"1100","debit":"458400.000000","credit":"0.000000"},{"account_code":"1200","debit":"0.000000","credit":"383400.000000"},{"account_code":"4000","debit":"0.000000","credit":"75000.000000"}]
```

**Step 7 — Send the repayment again.** Same reference, same body: the service answers `200` with the original result
and records nothing new. Change the amount and it refuses with `409`.

```bash
curl -s -o /dev/null -w '%{http_code}\n' "${AUTH[@]}" $API/loans/$LOAN/repayments -d '{
  "external_reference": "REP-0001", "amount": "458400", "transaction_date": "2026-02-15"}'
curl -s "${AUTH[@]}" $API/loans/$LOAN/repayments -d '{
  "external_reference": "REP-0001", "amount": "500000", "transaction_date": "2026-02-15"}' | jq -r .error.code
```
```
200
IDEMPOTENCY_CONFLICT
```

**Step 8 — Check the loan and the books.** The loan's balances come live from Fineract; reconciliation compares
Fineract with the ERP, reference by reference.

```bash
curl -s "${AUTH[@]}" $API/loans/$LOAN | jq -c '{status, outstanding: .outstanding.total}'
curl -s "${AUTH[@]}" $API/reconciliation -d '{"external_references": ["DISB-0001", "REP-0001"]}' | jq -c .summary
```
```
{"status":"ACTIVE","outstanding":"5042399"}
{"MATCHED":2}
```

[API.md](API.md) documents every endpoint. Interactive docs: <http://localhost:58090/docs> (integration service) and
<http://localhost:58001/docs> (ERP).

| Service | URL | Credentials |
| --- | --- | --- |
| Integration service (partner API) | http://localhost:58090 | `Authorization: Bearer integration-local-token` |
| Mock ERP | http://localhost:58001 | `Authorization: Bearer erp-local-token` |
| Fineract | http://localhost:58080/fineract-provider/api/v1 | `mifos` / `password`, header `Fineract-Platform-TenantId: default` |
| PostgreSQL | localhost:55432 | `postgres` / `postgres` |

These are local development credentials.

### 4. Run the tests and the simulations

| Command | What runs | Needs |
| --- | --- | --- |
| `make test-python` | Ruff, `mypy --strict`, then the unit and component tests of both services, against real PostgreSQL | Python 3.12 |
| `make test-java` | JUnit unit tests of the Fineract patch | JDK 21 |
| `make test-java-it` | JUnit integration tests against the running Fineract: replay, 409, 10 concurrent creates × 5, permissions | JDK 21, stack up |
| `make test-java-regression` | Fineract's own GL tests plus ours, to show the patch broke nothing (restarts Fineract in its `test` profile) | JDK 21, stack up |
| `make test-stack` | Every simulation as an automated test against the running stack | Python 3.12, stack up |
| `make simulate` | Runs every simulation and **rewrites** [SIMULATIONS.md](SIMULATIONS.md) with the evidence | Python 3.12, stack up |

Every push also runs `make test-python` and `make test-java` in GitHub Actions ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

One simulation at a time: `make simulate-<name>` (`make simulations` lists them).

| Brief | Name | | Brief | Name |
| --- | --- | --- | --- | --- |
| Part A, the Fineract patch | `gl-patch` | | J1 ERP down | `erp-down` |
| Client identity | `client` | | J2 Fineract down | `fineract-down` |
| GL sync | `gl-sync` | | J3 Fineract timeout | `fineract-timeout` |
| Loan lifecycle | `lifecycle` | | J4 ERP timeout | `erp-timeout` |
| I1 Sequential duplicates | `duplicates` | | J5 Crash | `crash` |
| I2a Concurrent, same reference | `concurrent-same` | | K Reconciliation | `reconciliation` |
| I2b Concurrent, different references | `concurrent-different` | | Trace one reference | `trace` |
| I3 Concurrent repayments | `concurrent-repayments` | | Stretch: repayment reversal | `reversal` |

The Postman collection runs the lifecycle too: `npx newman run postman/collection.json`.

### 5. Stop

```bash
make down    # stops everything and deletes the data, so the next `make up` starts clean
```

## Troubleshooting

| Problem | Fix |
| --- | --- |
| A port is already taken | Override it, e.g. `INTEGRATION_HOST_PORT=59090 make up`. The variables are `POSTGRES_HOST_PORT`, `FINERACT_HOST_PORT`, `ERP_HOST_PORT`, `INTEGRATION_HOST_PORT`. The tests and simulations assume the defaults. |
| Fineract exits or restarts during the first start | Docker needs about 6 GB of memory (Docker Desktop → Settings → Resources). |
| A code change doesn't show up | `docker compose up -d --build --wait <service>`. For the Java patch: `make fineract-image`, then `docker compose up -d --wait fineract`. Compose doesn't rebuild an image it already has. |
| You want a clean slate | `make down && make up` |
| Docker runs out of disk | `docker builder prune` frees the build cache, including Gradle's (the next Fineract build is a full one again). |
| Something failed and you want to know why | `docker compose logs <service>`. Each log line is JSON with a `correlation_id`; every API response carries the same id in `X-Correlation-ID`. |

## Read next

| Document | What's in it |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, a disbursement step by step, the accounting mapping, the state machine, the concurrency safeguards |
| [API.md](API.md) | Every endpoint, the error format, the idempotency contract |
| [DECISIONS.md](DECISIONS.md) | Why each choice was made and what it costs, what wasn't built, known weaknesses, assumptions, how to trace a reference, AI disclosure |
| [SIMULATIONS.md](SIMULATIONS.md) | Each simulation with its evidence, in the brief's template |
| [fineract-customization/README.md](fineract-customization/README.md) | The Fineract patch in detail |
| [evidence/](evidence/README.md) | Raw logs of the builds and test runs |

## Repository layout

```
fineract-customization/   the Fineract patch, its Dockerfile and notes
mock-erp/                 the ERP service (src/, tests/, Dockerfile)
integration-service/      the partner API (src/, tests/, simulations/, Dockerfile)
docker/                   database setup, and the test-profile override for the regression run
evidence/                 build logs and test runs
postman/                  a collection that runs the lifecycle
upstream/fineract/        Fineract's source, cloned by `make` for Java builds; not committed
```

## Pinned versions

| Piece | Version |
| --- | --- |
| Apache Fineract | tag `1.15.0`, commit `d5636847ac556c30b437254c353f05526d172b97` (verified on every clone). Why not 1.16.x: see [DECISIONS.md](DECISIONS.md#fineract-1150-not-116x). |
| JDK | 21: Temurin 21.0.5 in the Docker build, OpenJDK 21.0.9 for local builds. Runtime image `azul/zulu-openjdk-alpine:21`, as in Fineract's own build. |
| Build tool | Gradle 8.14.5, the wrapper in the tag |
| PostgreSQL | 18.3 |
| Python | 3.12; FastAPI 0.115.6, SQLAlchemy 2.0.36, Alembic 1.14.0, httpx 0.28.1, pydantic 2.10.4 |
