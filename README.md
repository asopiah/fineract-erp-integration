# Fineract ↔ ERP integration

Take-home assessment: Apache Fineract customization (Java/Spring Boot) plus a Python Mock ERP and Integration
Service, kept financially correct under duplicates, timeouts, partial success and concurrency.

Status: work in progress. Tiers completed: none yet.

## Pinned versions

| Piece | Version |
| --- | --- |
| Apache Fineract | tag `1.15.0`, commit `d5636847ac556c30b437254c353f05526d172b97` |
| JDK | 21 (build host: Microsoft OpenJDK 21.0.9; Fineract 1.15.0 toolchain is `JavaLanguageVersion.of(21)`) |
| Build tool | Gradle 8.14.5 via the wrapper shipped in the `1.15.0` tag; image built with Jib (`azul/zulu-openjdk-alpine:21` base) |
| Database | PostgreSQL (Fineract's default; the 1.15.0 image ships the PostgreSQL driver) |

## Fineract source

The upstream checkout lives in `upstream/fineract` and is not committed. The customization ships as a patch in
`fineract-customization/`.

```bash
git clone --branch 1.15.0 --depth 1 https://github.com/apache/fineract.git upstream/fineract
```
