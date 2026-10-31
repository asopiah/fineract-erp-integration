# Fineract customization: ERP-owned GL accounts

`gl-account-external-id.patch` applies to Apache Fineract tag `1.15.0`
(`d5636847ac556c30b437254c353f05526d172b97`). It lets the system that owns the chart of accounts (the ERP) create GL
accounts in Fineract safely: repeating a create is harmless, and a conflicting one is refused. Fineract enforces the
rules itself, in the same transaction as the write.

## What Fineract 1.15.0 already had

`acc_gl_account` has a unique `gl_code` and **no external reference**. Clients, loans and loan transactions already
carry a unique `external_id` and an `/external-id/{id}` lookup; GL accounts did not. The patch follows that existing
convention and reuses Fineract's own `ExternalId` type, its JPA converter and its JSON serializers.

## Behaviour

| # | Requirement | How it is enforced |
|---|---|---|
| 1 | External reference stored and unique | Column `acc_gl_account.external_id` + unique constraint `acc_gl_external_id` |
| 2 | Same reference, identical payload → existing account | `createGLAccount` finds the account and returns its id; nothing is inserted |
| 3 | Same reference, different payload → conflict | `GLAccountExternalIdConflictException`, answered **409** by its own mapper |
| 4 | Fineract permissions | Unchanged `CREATE_GLACCOUNT` command; the lookup needs `READ_GLACCOUNT` |
| 5 | Liquibase | `db/custom-changelog/0001_gl_account_external_id.xml`, Fineract's extension point for installation-specific changes |
| 6 | JUnit unit and integration tests | See [Tests](#tests) |

**"Identical"** means every attribute the caller supplies: GL code, name, type, usage, parent, tag, description and
whether manual entries are allowed (`GLAccount.hasSameDefinitionAs`). Fineract-owned state (id, hierarchy, disabled)
is not part of it. So an account disabled in Fineract still replays, and a replay never changes the stored account.

**Input rules**

* The external id is optional. Creates without one behave exactly as before.
* Up to 100 characters. A blank value is rejected (400) rather than treated as "none".
* It is set on create only. Any `PUT` that includes `externalId` is rejected with 400, even with the current value.
* Matching is exact and case-sensitive on PostgreSQL. MySQL/MariaDB compare with the column's collation, which is
  usually case-insensitive; only PostgreSQL was tested.

`GET /v1/glaccounts/external-id/{externalId}` returns the account. The integration service and the bootstrap use it
to find accounts by the ERP's reference instead of Fineract's numeric id.

## Mechanism and why

The patch extends the existing `GLACCOUNT/CREATE` command instead of adding an endpoint. A create still goes through
`PortfolioCommandSourceWritePlatformService`, so it keeps the permission check, maker-checker, the audit row in
`m_portfolio_command_source`, and Fineract's command retry. A new endpoint would have to rebuild all four, and a
wrapper outside Fineract could not enforce anything inside it.

```
POST /v1/glaccounts
  → GLAccountsApiResource.createGLAccount
  → PortfolioCommandSourceWritePlatformService.logCommandSource      permission CREATE_GLACCOUNT, maker-checker
  → SynchronousCommandProcessingService.executeWithRetry             retry: up to 3 attempts, 1 s then 2 s apart
  → CommandSourceService.processCommandAndSaveResult  @Transactional ← one transaction per attempt
  → CreateGLAccountCommandHandler
  → GLAccountWritePlatformServiceJpaRepositoryImpl.createGLAccount   ← external id rules run here
```

### Concurrent creates

Two identical creates can both pass the lookup and both insert. The unique constraints let exactly one commit. The
loser cannot recover inside its own transaction: it is rollback-only, and on PostgreSQL it is aborted.

So the loser throws `GLAccountExternalIdRaceException`, a `ConcurrencyFailureException`. Fineract's command retry is
configured for that type (`fineract.retry.instances.executeCommand.retryExceptions`). The request runs again in a
fresh transaction, sees the committed winner, and resolves: identical definition → the winner's id, anything else →
409. The retry costs the losing request about one second.

* **Only unique violations are retried**, recognised by SQL state (`23505` on PostgreSQL, `23000`/1062 on
  MySQL/MariaDB), not by constraint name. Other integrity errors keep Fineract's normal handling.
* **Which constraint fires does not matter.** Both racers insert the same GL code, so in practice PostgreSQL rejects
  the loser on `acc_gl_account_gl_code_key` before it checks `acc_gl_external_id`. That is why the code looks at the
  SQL state and not the constraint name.
* **The GL code is checked before the external id**, so an account committed between the two lookups is still found
  by its external id and treated as a replay.
* **If all retries were lost**, which needs three consecutive losses, the caller gets Fineract's generic 409
  concurrency error and can simply resend.

Evidence: 5 rounds of 10 concurrent identical creates. In the recorded run, 33 inserts lost the race and were
retried; all 50 requests returned 200, and each round produced exactly one account
(`../evidence/java-race-retries.txt`).

### Worth knowing

* **A replay is still a command.** It writes its own audit row and, like any successful create, would fire a
  `GLACCOUNT/CREATE` hook if one were configured. It never inserts a second account.
* **Maker-checker.** If GL creates need approval, each submission waits as its own pending command, and the rules
  above apply when it is approved.
* **Batch API.** `/batches?enclosingTransaction=true` skips the command retry, so a lost race inside such a batch gets
  the generic 409. The integration service does not use batches.
* **No external id is generated.** Fineract's global "auto-generate external id" setting is deliberately not applied
  to GL accounts. A random id would defeat replay.
* **Existing accounts can't be linked yet.** An account created without an external id can't get one later (`PUT`
  refuses it), and creating it again with one is a duplicate GL code. Allowing `PUT` to set the external id once,
  while it is empty, is the natural next step for onboarding an existing chart.
* **Liquibase lives in `db/custom-changelog`.** Fineract runs every file there on each tenant, after its own
  changelogs (`db.changelog-master.xml`, `includeAll path="db/custom-changelog"`). So the patch never edits upstream's
  numbered changelog, and an upgrade can't collide with it.
* **Upgrading.** The patch also applies cleanly to Fineract 1.16.1, the newest tag (checked with `git apply --check`;
  not built or tested there, since 1.16 moves to JDK 25 and Gradle 9).
* **An upstream quirk.** Fineract maps a duplicate GL code by searching the error text for `acc_gl_code`. On
  PostgreSQL the constraint is called `acc_gl_account_gl_code_key` (verified), so that match fails there. The patch
  doesn't depend on constraint names.

### Why not Fineract's `Idempotency-Key`

Fineract 1.15 caches command results by an `Idempotency-Key` header. It does not compare payloads, so a replay with a
different body returns the cached result instead of a conflict. It also replays a cached error, which would pin a
transient failure. A business key with a definition check is the right tool here.

## Files

| Layer | Files |
|---|---|
| Schema | `db/custom-changelog/0001_gl_account_external_id.xml` |
| Domain | `GLAccount` (field, `fromJson`, `hasSameDefinitionAs`), `GLAccountRepository.findOneByExternalId` |
| Input | `GLAccountJsonInputParams.EXTERNAL_ID`, `GLAccountCommand` (validation), `GLAccountCommandFromApiJsonDeserializer` |
| Write | `GLAccountWritePlatformServiceJpaRepositoryImpl`, `UniqueConstraintViolations` |
| Errors | `GLAccountExternalIdConflictException` (+ 409 mapper), `GLAccountExternalIdRaceException`, `GLAccountNotFoundException` |
| Read | `GLAccountData.externalId`, `GLAccountReadPlatformService(Impl).retrieveGLAccountByExternalId`, `GLAccountsApiResource`, Swagger |

## Build

`docker compose up` builds the image from `Dockerfile` when it is missing: it clones the tag, verifies the commit,
applies the patch and builds with Gradle (about 15–20 minutes the first time).

With JDK 21 installed, `make fineract-image` builds the same code faster with Fineract's own Jib setup (about 8
minutes). It clones the tag into `upstream/fineract`, applies the patch, and tags the image `fineract-gl-sync:1.15.0`.

To change the Java code: edit it in `upstream/fineract`, then run `make fineract-save-patch` to write the edits back
into the patch file, and `make fineract-image` to rebuild.

## Tests

```bash
make test-java             # unit tests (Gradle, no server needed)
make test-java-it          # integration tests against the running stack (`make up` first)
make test-java-regression  # upstream's GL integration tests + ours (restarts Fineract in its "test" profile)
```

| Kind | Class | Covers |
|---|---|---|
| Unit | `GLAccountDefinitionTest` (fineract-core) | what counts as the same definition |
| Unit | `GLAccountExternalIdCreateTest` | replay, conflict, GL code clash, the lookup window, race hand-off, other integrity errors |
| Unit | `GLAccountExternalIdValidationTest` | blank and over-long external ids, external id on update |
| Unit | `UniqueConstraintViolationsTest` | SQL-state detection for PostgreSQL and MySQL |
| Integration | `GLAccountExternalIdIntegrationTest` | replay, 409, 10 concurrent creates × 5, permission denied (403), immutable external id, 404 lookup |
| Regression | `GLAccountIntegrationTest` (upstream, unchanged) | create, update, delete, and refusing to delete an account with children, a product mapping or journal entries |

The integration tests log in as Fineract's default admin (`mifos`); the permission test creates its own read-only
user.

### Regression: proving nothing else broke

Our tests prove the new behaviour; they can't prove the old behaviour survived. The patch touches the code every GL
create and update goes through, and the table every journal entry points at. So `make test-java-regression` also
runs Fineract's own GL integration tests, unchanged. They never send an external id, so they cover exactly what
existing Fineract users depend on.

Upstream's tests share a base class that calls internal endpoints, which Fineract only creates under its Spring
profile `test`. The target restarts Fineract with that profile (`docker/fineract-test-profile.yml`) for the run, and
always restarts it normally afterwards, even when a test fails.

Scope: the GL area the patch touches, not Fineract's whole suite (thousands of tests, hours).

A scripted demo of behaviours 1–4 against the running Fineract, with the 403 and the API user's least privilege, is
the *Fineract GL patch* simulation (`make simulate-gl-patch`, evidence in [SIMULATIONS.md](../SIMULATIONS.md)).

Recorded runs: `../evidence/java-unit-tests.log`, `../evidence/java-integration-tests.log`,
`../evidence/java-regression-tests.log`, and the from-source build in `../evidence/stack-up.log`.
