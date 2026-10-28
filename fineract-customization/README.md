# Fineract customization: ERP-owned GL accounts

`gl-account-external-id.patch` applies to Apache Fineract tag `1.15.0`
(`d5636847ac556c30b437254c353f05526d172b97`). It makes GL accounts safe to synchronize from the system that owns
the chart of accounts. Every rule below is enforced inside Fineract, in the same transaction as the write.

## What Fineract 1.15.0 already had

`acc_gl_account` has a unique `gl_code` and **no external reference**. Clients, loans and loan transactions already
carry a unique `external_id` (a database index on each) and an `/external-id/{id}` lookup. GL accounts did not. The
patch brings them in line with that existing convention and reuses Fineract's own `ExternalId` value type, its JPA
converter and its JSON serializers.

## Behaviour

| # | Requirement | How it is enforced |
|---|---|---|
| 1 | External reference persisted and unique | `acc_gl_account.external_id` + unique constraint `acc_gl_external_id` (Liquibase `0242`) |
| 2 | Same reference, identical payload → existing account | `GLAccountWritePlatformServiceJpaRepositoryImpl.createGLAccount` resolves the existing account and returns its id; no row is inserted |
| 3 | Same reference, different payload → conflict | `GLAccountExternalIdConflictException`, answered with **HTTP 409** by its own mapper |
| 4 | Protected by Fineract's permission model | Unchanged `CREATE_GLACCOUNT` command, so the command pipeline checks the permission; the lookup checks `READ_GLACCOUNT` |
| 5 | Schema change as Liquibase | `db/changelog/tenant/parts/0242_add_gl_account_external_id.xml` |
| 6 | JUnit unit and integration tests | See [Tests](#tests) |

"Identical" means every attribute the caller supplies: GL code, name, type, usage, parent, tag, description and
whether manual entries are allowed (`GLAccount.hasSameDefinitionAs`). Fineract-owned state (id, hierarchy, disabled
flag) is not part of it, so an account disabled in Fineract still replays.

The external id is set on create only. A `PUT` that tries to change it is rejected with 400. Creates without an
external id behave exactly as before.

`GET /v1/glaccounts/external-id/{externalId}` returns the account. The integration service uses it to resolve GL
accounts by the ERP's reference instead of Fineract's numeric id.

## Mechanism and why

The patch extends the existing `GLACCOUNT/CREATE` command instead of adding an endpoint. A create still goes through
`PortfolioCommandSourceWritePlatformService`, so it keeps:

- the permission check;
- maker-checker;
- the audit row in `m_portfolio_command_source`;
- Fineract's command retry.

A new endpoint would have to rebuild all four, and a Python-side wrapper could not enforce anything inside Fineract.

```
POST /v1/glaccounts
  → GLAccountsApiResource.createGLAccount
  → PortfolioCommandSourceWritePlatformService.logCommandSource      permission CREATE_GLACCOUNT, maker-checker
  → SynchronousCommandProcessingService.executeWithRetry             resilience4j retry, fresh attempt each time
  → CommandSourceService.processCommandAndSaveResult  @Transactional ← transaction boundary
  → CreateGLAccountCommandHandler
  → GLAccountWritePlatformServiceJpaRepositoryImpl.createGLAccount   ← external id rules run here
```

### Concurrent creates

Two identical creates can both pass the lookup and both insert. The unique constraint lets exactly one commit. The
loser cannot recover inside its own transaction: it is rollback-only, and on PostgreSQL it is aborted outright.

So the loser throws `GLAccountExternalIdRaceException`, a `ConcurrencyFailureException`. Fineract's command retry is
configured for that exception type (`fineract.retry.instances.executeCommand.retryExceptions`), so the request runs
again in a fresh transaction. That attempt sees the committed winner and resolves deterministically: an identical
definition returns the winner's id, anything else gets 409.

Only unique violations are retried, recognized by SQL state (`23505` on PostgreSQL, `23000`/1062 on
MySQL/MariaDB). Other integrity errors, such as a value that is too long, keep Fineract's normal error handling.

The lookup order matters. The GL code is checked before the external id, so an account committed between the two
lookups is still found by its external id and treated as a replay. The first version checked them the other way
round. The parallel integration test caught it: one of ten concurrent creates got 403 "duplicate GL code".

Evidence: in 5 rounds of 10 concurrent identical creates, 28 inserts lost the race in the recorded run and were retried. All 50 requests
returned 200 and each round produced exactly one account (`../evidence/java-race-retries.txt`).

One gap: Fineract's batch API with an enclosing transaction (`/batches?enclosingTransaction=true`) skips the
command retry. A losing create inside such a batch therefore gets Fineract's generic 409 concurrency conflict instead
of resolving to the winner. The integration service does not use batches.

A note on upstream code: Fineract maps a duplicate GL code by searching the error message for `acc_gl_code`. On
PostgreSQL that constraint is called `acc_gl_account_gl_code_key`, so the match never succeeds there. The patch
does not depend on constraint names.

### Why not Fineract's `Idempotency-Key`

Fineract 1.15 caches command results by an `Idempotency-Key` header. That cache does not compare payloads: a replay
with a different body returns the cached result instead of a conflict. It also replays a cached error, which would
pin a transient failure. A business key with a definition check is the right tool here.

## Files

| Layer | Files |
|---|---|
| Schema | `0242_add_gl_account_external_id.xml`, `changelog-tenant.xml` |
| Domain | `GLAccount` (field, `fromJson`, `hasSameDefinitionAs`), `GLAccountRepository.findOneByExternalId` |
| Input | `GLAccountJsonInputParams.EXTERNAL_ID`, `GLAccountCommand` (validation), `GLAccountCommandFromApiJsonDeserializer` |
| Write | `GLAccountWritePlatformServiceJpaRepositoryImpl`, `UniqueConstraintViolations` |
| Errors | `GLAccountExternalIdConflictException` (+ 409 mapper), `GLAccountExternalIdRaceException`, `GLAccountNotFoundException` |
| Read | `GLAccountData.externalId`, `GLAccountReadPlatformService(Impl).retrieveGLAccountByExternalId`, `GLAccountsApiResource`, Swagger |

## Build

From the repository root, with JDK 21:

```bash
make fineract-image
```

That target clones tag `1.15.0` into `upstream/fineract`, applies the patch and builds the image
`fineract-gl-sync:1.15.0` with the project's own Jib setup.

## Tests

```bash
make test-java             # unit tests (Gradle, no server needed)
make test-java-it          # integration tests against the running stack (needs `docker compose up`)
make test-java-regression  # upstream's GL integration tests + ours (restarts Fineract in its "test" profile)
```

| Kind | Class | Covers |
|---|---|---|
| Unit | `GLAccountDefinitionTest` (fineract-core) | what counts as the same definition |
| Unit | `GLAccountExternalIdCreateTest` (fineract-accounting) | replay, conflict, code clash, lookup window, race hand-off, non-unique errors |
| Unit | `UniqueConstraintViolationsTest` | SQL-state detection for PostgreSQL and MySQL |
| Integration | `GLAccountExternalIdIntegrationTest` (integration-tests) | replay, 409, 10 concurrent creates ×5, permission denied (403), immutable external id, 404 lookup |
| Regression | `GLAccountIntegrationTest` (upstream, unchanged) | create, update, delete, and refusing to delete an account with children, a product mapping or journal entries |

### Regression: proving nothing else broke

The tests above prove the new behaviour. They can't prove that the old behaviour survived, and the patch changes
code every GL account create and update goes through, plus the `acc_gl_account` table every journal entry points
at. So `make test-java-regression` runs Fineract's own GL integration tests on the patched build, unchanged, next to
ours. Those tests never send an external id, so they exercise the paths existing Fineract users depend on.

Upstream's tests share a base class (`BaseLoanIntegrationTest`) that calls internal endpoints which Fineract only
creates under its Spring profile `test`. Without that profile, all four fail in setup with 404 before testing
anything. The target therefore restarts Fineract with `docker/fineract-test-profile.yml` for the run. Afterwards it
always restarts Fineract in its normal profile, even when tests fail, and still exits with the tests' result.

Scope: the GL area the patch touches, not Fineract's whole integration suite (thousands of tests, hours).

Recorded runs: `../evidence/java-unit-tests.log`, `../evidence/java-integration-tests.log`,
`../evidence/java-regression-tests.log` (14 tests: upstream's 4 + our 10, 0 failures), and the image build in
`../evidence/build-patched.log`.
