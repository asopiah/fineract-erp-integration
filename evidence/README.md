# Evidence

Raw outputs of real runs. Everything here was produced by the commands named, on the stack described in the README.

The final run started from empty volumes (`make down && make up`) with the Fineract image built by
`fineract-customization/Dockerfile`, which is what a machine without a JDK gets. The Jib-built image (`make fineract-image`) was used during development; both build the same patched source.

| File | What it shows | Produced by |
| --- | --- | --- |
| `build-baseline.log` | Unmodified Fineract 1.15.0 builds from source with the pinned JDK and Gradle | `./gradlew :fineract-provider:jibBuildTar` on the clean tag |
| `build-patched.log` | The patched Fineract builds into the image `fineract-gl-sync:1.15.0` | `make fineract-image` |
| `build-dockerfile.log` | The same patched build inside Docker, for machines without a JDK | `docker build fineract-customization` |
| `java-unit-tests.log` | JUnit unit tests of the GL customization pass | `make test-java` |
| `java-integration-tests.log` | JUnit integration test against the running patched Fineract: replay, 409, 5 × 10 concurrent creates, permission denied, immutable external id, lookup | `make test-java-it` |
| `java-race-retries.txt` | How many concurrent GL inserts lost the unique-constraint race and were resolved by Fineract's command retry | Fineract logs during `make test-java-it` |
| `python-tests.log` | Ruff, `mypy --strict`, and unit and component tests of both Python services | `make test-python` |
| `stack-tests.log` | Every simulation as an automated test against the running stack | `make test-stack` |
| `stack-up.log` | One command starts and configures an empty stack | `make up` after `make down` |
| `simulate.log` | Console output of the final simulation run (the evidence itself is in SIMULATIONS.md) | `make simulate` |
| `postman-run.log` | The Postman collection runs the lifecycle end to end | `newman run postman/collection.json` |

The simulation evidence itself (the nine-field template per scenario) is in [`../SIMULATIONS.md`](../SIMULATIONS.md).
