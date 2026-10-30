# Fineract ↔ ERP integration

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
| 3. Stretch (L) | Not taken as a tier. A few pieces exist because the core needed them: the dead-letter view (`GET /api/admin/operations`), fee, penalty and overpayment lines in the accounting mapping (unit-tested), reversal-aware reconciliation (unit-tested) and the scheduled recovery sweep. |

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

```bash
API=localhost:58090/api
AUTH=(-H 'Authorization: Bearer integration-local-token' -H 'Content-Type: application/json')

# A client, then a loan application for 5,000,000 RWF
curl -s "${AUTH[@]}" $API/clients -d '{"external_reference": "CLI-0001", "client_uid": "SME-000001",
  "first_name": "Amina", "last_name": "Uwase", "activation_date": "2026-01-10"}' | jq -c
LOAN=$(curl -s "${AUTH[@]}" $API/loans -d '{"external_reference": "APP-0001", "client_uid": "SME-000001",
  "principal": "5000000", "annual_interest_rate": "18", "term_months": 12,
  "submitted_on": "2026-01-15", "expected_disbursement_on": "2026-01-15"}' | jq -r .loan_reference)

# Approve, disburse, repay
curl -s "${AUTH[@]}" $API/loans/$LOAN/approve -d '{"external_reference": "APR-0001", "approved_on": "2026-01-15"}' | jq -c
curl -s "${AUTH[@]}" $API/loans/$LOAN/disburse \
  -d '{"external_reference": "DISB-0001", "amount": "5000000", "transaction_date": "2026-01-15"}' | jq -c
curl -s "${AUTH[@]}" $API/loans/$LOAN/repayments \
  -d '{"external_reference": "REP-0001", "amount": "458400", "transaction_date": "2026-02-15"}' | jq -c

# The loan as Fineract holds it, and the books compared
curl -s "${AUTH[@]}" $API/loans/$LOAN | jq -c '{status, outstanding}'
curl -s "${AUTH[@]}" $API/reconciliation -d '{"external_references": ["DISB-0001", "REP-0001"]}' | jq -c .summary
```

The repayment comes back with Fineract's split (principal and interest) and the ERP entry that mirrors it, and
reconciliation reports `{"MATCHED": 2}`. Send any of these requests again: it answers `200` with the same body
instead of doing it twice. [API.md](API.md) documents every endpoint. Interactive docs are at
<http://localhost:58090/docs> and <http://localhost:58001/docs>.

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

One simulation at a time: `make simulate-<name>` (`make simulations` lists them).

| Brief | Name | | Brief | Name |
| --- | --- | --- | --- | --- |
| Client identity | `client` | | J1 ERP down | `erp-down` |
| GL sync | `gl-sync` | | J2 Fineract down | `fineract-down` |
| Loan lifecycle | `lifecycle` | | J3 Fineract timeout | `fineract-timeout` |
| I1 Sequential duplicates | `duplicates` | | J4 ERP timeout | `erp-timeout` |
| I2a Concurrent, same reference | `concurrent-same` | | J5 Crash | `crash` |
| I2b Concurrent, different references | `concurrent-different` | | K Reconciliation | `reconciliation` |
| I3 Concurrent repayments | `concurrent-repayments` | | Trace one reference | `trace` |

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
| Apache Fineract | tag `1.15.0`, commit `d5636847ac556c30b437254c353f05526d172b97` (verified on every clone) |
| JDK | 21: Temurin 21.0.5 in the Docker build, OpenJDK 21.0.9 for local builds. Runtime image `azul/zulu-openjdk-alpine:21`, as in Fineract's own build. |
| Build tool | Gradle 8.14.5, the wrapper in the tag |
| PostgreSQL | 18.3 |
| Python | 3.12; FastAPI 0.115.6, SQLAlchemy 2.0.36, Alembic 1.14.0, httpx 0.28.1, pydantic 2.10.4 |
