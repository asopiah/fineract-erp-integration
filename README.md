# Fineract ↔ ERP integration

Apache Fineract (core banking) connected to an ERP (accounting master) and to financing partners, and kept
financially correct under duplicates, timeouts, partial success and concurrency.

* **Fineract customization (Java/Spring Boot):** GL accounts get an ERP-owned `externalId`. Fineract enforces the
  rules itself: a replay is idempotent, a conflict gets HTTP 409, and concurrent creates are resolved through
  Fineract's command retry. See [`fineract-customization/`](fineract-customization/README.md).
* **Mock ERP (Python/FastAPI):** chart of accounts and balanced journal entries, idempotent on `external_reference`.
* **Integration service (Python/FastAPI):** the partner API. Every state-changing request goes through one
  persisted state machine that looks Fineract up before every submit. ERP journals are derived from Fineract's own
  transaction breakdown, and recovery finishes whatever a failure interrupted.

## Tiers completed

| Tier | Status |
| --- | --- |
| 1. Core (A–G) | **Done.** Every part is implemented and demonstrated against the live stack. |
| 2. Reliability (H–K) | **Done.** I1–I3, J1–J5 and K run as automated tests and produce the evidence in [SIMULATIONS.md](SIMULATIONS.md). |
| 3. Stretch (L) | Not taken as a tier. Some pieces exist because the core design needed them: a dead-letter view (`GET /api/admin/operations`), fee, penalty and overpayment lines in the accounting mapping (unit-tested, not demonstrated live), reversal-aware reconciliation (unit-tested), and a scheduled recovery sweep. |

## Quick start

Prerequisites: Docker with Compose v2 and about 6 GB of memory for Docker. A JDK 21 is optional and only speeds up
the Fineract build.

```bash
make fineract-image   # optional, ~8 min with a local JDK 21; otherwise compose builds it (longer) on first start
make up               # PostgreSQL, Fineract, Mock ERP, integration service, then the Fineract bootstrap
```

`make up` is `docker compose up` plus waiting. The one-shot `bootstrap` container configures the empty Fineract:

* office *Kigali Main Office* and currency RWF;
* a least-privilege role and API user;
* GL accounts synchronized from the ERP;
* loan product *SME Asset Finance* (18% a year, 12 monthly repayments, single disbursement, cash accounting).

Running it again is harmless.

| Service | URL | Credentials |
| --- | --- | --- |
| Integration service (partner API) | http://localhost:58090, docs at `/docs` | `Authorization: Bearer integration-local-token` |
| Mock ERP | http://localhost:58001, docs at `/docs` | `Authorization: Bearer erp-local-token` |
| Fineract | http://localhost:58080/fineract-provider/api/v1 | admin `mifos` / `password`; the service runs as `integration_api` |
| PostgreSQL | localhost:55432 | `postgres` / `postgres` (one database per service) |

The credentials are local development defaults. Ports are configurable in `docker-compose.yml`.

A first partner request:

```bash
curl -s -X POST localhost:58090/api/clients \
  -H 'Authorization: Bearer integration-local-token' -H 'Content-Type: application/json' \
  -d '{"external_reference": "CLI-0001", "client_uid": "SME-000001", "first_name": "Amina",
       "last_name": "Uwase", "activation_date": "2026-01-10"}'
```

[API.md](API.md) walks through the whole loan lifecycle.

## Pinned versions

| Piece | Version |
| --- | --- |
| Apache Fineract | tag `1.15.0`, commit `d5636847ac556c30b437254c353f05526d172b97` |
| JDK | 21 (Fineract's toolchain: `JavaLanguageVersion.of(21)`; built here with OpenJDK 21.0.9) |
| Build tool | Gradle 8.14.5 (the wrapper in the tag). The image is built with Jib (`make fineract-image`) or with `fineract-customization/Dockerfile`. |
| PostgreSQL | 18.3, one server: `fineract_tenants`, `fineract_default`, `mock_erp`, `integration` |
| Python | 3.12; FastAPI 0.115.6, SQLAlchemy 2.0.36, Alembic 1.14.0, httpx 0.28.1, pydantic 2.10.4 |

## Tests

| Language | Command | What runs |
| --- | --- | --- |
| Java | `make test-java` | Unit tests for the GL customization (Gradle/JUnit, no server needed) |
| Java | `make test-java-it` | Integration test against the running Fineract: replay, 409, 10 concurrent creates ×5, permissions |
| Python | `make test-python` | Ruff, `mypy --strict`, then the unit and component tests of both services (component tests use real PostgreSQL) |
| Both | `make test` | `test-python` and `test-java` |
| Stack | `make test-stack` | Every simulation as an automated test against the running stack |

## Simulations

```bash
make simulate               # all of them; writes SIMULATIONS.md
make simulations            # list the names
make simulate-duplicates    # one of them, e.g. I1
```

Each simulation reads every system directly (Fineract's API, the ERP's API, the integration database) and prints
the brief's nine-field evidence template.

## Repository layout

```
fineract-customization/   patch against Fineract 1.15.0, Dockerfile, notes
mock-erp/                 ERP service (src/, tests/, Dockerfile)
integration-service/      partner API (src/, tests/, simulations/, Dockerfile)
docker/postgres/          database and role creation
evidence/                 build logs, test runs, race and lifecycle evidence
postman/                  supporting collection (the simulations are the primary evidence)
upstream/fineract/        the upstream checkout used for building; not committed
```

## Documentation

* [ARCHITECTURE.md](ARCHITECTURE.md): components, data flow, data model, state machine, GL sync design.
* [API.md](API.md): endpoints, examples, errors, the idempotency contract.
* [SIMULATIONS.md](SIMULATIONS.md): every simulation, with its evidence.
* [DECISIONS.md](DECISIONS.md): trade-offs and rationale, known weaknesses, how to trace one reference, and the
  disclosure of AI assistance and references.
