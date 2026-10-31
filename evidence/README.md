# Evidence

Raw output of real runs, unedited.

**The final run (31 October 2026)** started from a fresh clone of the repository and an empty Docker: no Fineract
image, no build cache, no data. `make up` built the patched Fineract from source, then every check below ran
against that stack, in this order. All passed.

| File | What it shows | Produced by |
| --- | --- | --- |
| `stack-up.log` | One command builds the patched Fineract from source (Gradle step: 14 min 3 s), starts the stack and configures Fineract; the bootstrap ends with `Fineract is configured`, exit code 0 | `make up` |
| `readme-walkthrough.log` | The README's eight walkthrough steps, run verbatim; output identical to the README | the commands in README § 3 |
| `python-tests.log` | Ruff, format check and `mypy --strict` clean; 129 integration-service and 22 Mock ERP tests pass, against real PostgreSQL | `make test-python` |
| `stack-tests.log` | All 16 simulations pass as automated tests against the running stack | `make test-stack` |
| `simulate.log` | Console output of the run that wrote [`../SIMULATIONS.md`](../SIMULATIONS.md) (16/16 PASS). | `make simulate` |
| `java-unit-tests.log` | 19 JUnit tests: the customization's 18 plus upstream's GL service test | `make test-java` |
| `java-integration-tests.log` | 10 JUnit integration tests against the running Fineract: replay, 409, 5 × 10 concurrent creates, permission denied, immutable external id, lookup | `make test-java-it` |
| `java-race-retries.txt` | 33 concurrent inserts lost the unique-constraint race during those tests; all were resolved by Fineract's command retry | Fineract log during `make test-java-it` |
| `java-regression-tests.log` | Fineract's own `GLAccountIntegrationTest` (4 tests) passes unchanged on the patched build, next to our 10 | `make test-java-regression` |
| `postman-run.log` | The Postman collection runs the lifecycle: 19 requests, 22 assertions, 0 failures | `npx newman run postman/collection.json` |
| `build-patched.log` | The same patch built with a local JDK 21 and Fineract's own Jib setup, the faster path (1 min 38 s here, with Gradle's cache warm from earlier builds) | `make fineract-image` |

**Later the same evening**, the repayment reversal was added. `python-tests.log`, `stack-tests.log`, `simulate.log`
and SIMULATIONS.md were then regenerated on the running stack (built from the final Dockerfile, the integration
service rebuilt with the reversal, the bootstrap re-run to grant `ADJUST_LOAN`). The Java side did not change.

**Earlier, kept for reference**

| File | What it shows | Produced by |
| --- | --- | --- |
| `build-baseline.log` | 27 October: unmodified Fineract 1.15.0 builds from source with the pinned JDK and Gradle, before any change | `./gradlew :fineract-provider:jibBuildTar` on the clean tag |

The simulation evidence itself, in the brief's nine-field template, is in [`../SIMULATIONS.md`](../SIMULATIONS.md).

The logs contain the local paths of the machine they ran on (`/Users/…`); nothing else was removed or changed.
