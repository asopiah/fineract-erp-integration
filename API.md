# Integration API

Base URL `http://localhost:58090`. The OpenAPI document is served at `/openapi.json`, with an interactive version at
`/docs`.

## Conventions

| | |
| --- | --- |
| Authentication | `Authorization: Bearer <token>` on every `/api` call (401 otherwise) |
| Correlation | Send `X-Correlation-ID`; one is minted otherwise. It is echoed on every response, error bodies included. |
| Identifiers | Clients by `client_uid` (e.g. `SME-000001`), loans by `loan_reference` (`LN-2026-000001`, issued by this service). Fineract's numeric ids never appear. |
| Money | Decimal strings in whole Rwandan francs (`"5000000"`). Numbers are accepted on input; floats never appear on output. |
| Dates | ISO `YYYY-MM-DD` |
| References | `external_reference`: 1–100 characters from `[A-Za-z0-9._:-]`, starting with a letter or digit |

### Errors

Every error has one shape:

```json
{"error": {"code": "LOAN_ALREADY_DISBURSED", "message": "loan LN-2026-000001 is already disbursed by DISB-0001",
           "correlation_id": "6b2c…", "details": {}}}
```

| Status | Codes |
| --- | --- |
| 401 | `UNAUTHORIZED` |
| 404 | `NOT_FOUND` |
| 409 | `IDEMPOTENCY_CONFLICT`, `CLIENT_UID_TAKEN`, `LOAN_ALREADY_APPROVED`, `LOAN_ALREADY_DISBURSED`, `CLIENT_NOT_READY`, `LOAN_NOT_READY`, `NOT_RESUMABLE` |
| 422 | `VALIDATION_ERROR` (with `details` per field); `FINERACT_REJECTED` (Fineract refused it; `details.fineract_code` has Fineract's reason code) |
| 503 | `FINERACT_UNAVAILABLE` (nothing reached Fineract: safe to retry the same request), `ERP_UNAVAILABLE` |
| 500 | `INTERNAL_ERROR` (the logs carry the details under the returned correlation id) |

## Idempotency contract

Every state-changing request carries an `external_reference`. Uniqueness is per operation type.

| Case | Response |
| --- | --- |
| New reference | Processed once. `201` with the result. |
| Same reference, same payload, finished | `200` with the original result (the same body) |
| Same reference, same payload, still in progress | `202` with the current status. It is never executed a second time. |
| Same reference, different payload | `409 IDEMPOTENCY_CONFLICT` |
| Same reference after `503 FINERACT_UNAVAILABLE` | Executed again (nothing had reached Fineract) |
| Same reference after `422 FINERACT_REJECTED` | The same rejection. A corrected request needs a new reference. |

"Same payload" compares a canonical fingerprint of the validated body **and the loan in the URL**, so `5000000` and
`5000000.00` are the same, but reusing a repayment reference on another loan is a 409.

| Operation | Unique per |
| --- | --- |
| GL account sync | ERP `external_reference` (and ERP `code`) |
| Client creation | `external_reference`. `client_uid` is also unique: a second reference for it gets 409. |
| Loan application | `external_reference` |
| Approval | `external_reference`, and one approval per loan |
| Disbursement | `external_reference`, and **one disbursement per loan** whatever the reference |
| Repayment | `external_reference`. Any number of repayments per loan. |
| Disbursements and repayments together | One namespace (as in Fineract and the ERP): a repayment cannot reuse a disbursement's reference (409) |

A `202` body says what is pending:

```json
{"external_reference": "REP-0042", "operation": "REPAYMENT", "status": "ERP_PENDING",
 "detail": "recorded in Fineract; the ERP journal entry is being posted"}
```

Poll `GET /api/transactions/{external_reference}`, or simply repeat the request: once the operation has finished, the
repeat gets the `200` replay.

## Endpoints

### `POST /api/accounts/sync` — GL accounts from the ERP into Fineract

Body (optional): `{"external_references": ["GL-1200"]}` limits the sync to those accounts. The call is create-only;
repeating it is harmless.

```json
[{"external_reference": "GL-1200", "code": "1200", "name": "Loan Receivable", "erp_type": "ASSET",
  "status": "SYNCED", "attempts": 0, "last_error": null}]
```

`GET /api/accounts/sync` returns the same view for every account.

### `POST /api/clients`

```json
{"external_reference": "CLI-0001", "client_uid": "SME-000001", "first_name": "Amina", "last_name": "Uwase",
 "activation_date": "2026-01-10"}
```

`201` returns the body without `activation_date`. The client is created active in Fineract, with
`externalId = client_uid`.

### `GET /api/clients/{client_uid}`

The client as Fineract holds it:

```json
{"external_reference": "CLI-0001", "client_uid": "SME-000001", "first_name": "Amina", "last_name": "Uwase",
 "fineract": {"exists": true, "active": true, "status": "Active", "display_name": "Amina Uwase", "name_matches": true}}
```

### `POST /api/loans` — loan application

```json
{"external_reference": "APP-0001", "client_uid": "SME-000001", "principal": "5000000",
 "annual_interest_rate": "18", "term_months": 12, "submitted_on": "2026-01-15",
 "expected_disbursement_on": "2026-01-15"}
```

`201` returns the application with its issued `loan_reference`. Approval is a separate step, because it is a separate
business decision with its own reference.

### `POST /api/loans/{loan_reference}/approve`

`{"external_reference": "APR-0001", "approved_on": "2026-01-15"}` returns `201
{"external_reference", "loan_reference", "approved_on"}`. A second approval of the same loan gets
`409 LOAN_ALREADY_APPROVED`.

### `POST /api/loans/{loan_reference}/disburse`

`{"external_reference": "DISB-0001", "amount": "5000000", "transaction_date": "2026-01-15"}` returns a transaction:

```json
{"external_reference": "DISB-0001", "loan_reference": "LN-2026-000001", "type": "DISBURSEMENT",
 "transaction_date": "2026-01-15", "amount": "5000000", "principal_portion": "0", "interest_portion": "0",
 "fee_portion": "0", "penalty_portion": "0", "overpayment_portion": "0", "outstanding_after": "5000000",
 "erp_entry_number": "JE-000001"}
```

The portions are copied from Fineract's transaction, which reports `0` principal portion for a disbursement.

### `POST /api/loans/{loan_reference}/repayments`

`{"external_reference": "REP-0001", "amount": "458400", "transaction_date": "2026-02-15"}` returns a transaction with
Fineract's split, for example `"principal_portion": "383400", "interest_portion": "75000"`, and the ERP entry that
mirrors it.

### `GET /api/loans/{loan_reference}`

Status, balances and transactions, read live from Fineract:

```json
{"loan_reference": "LN-2026-000001", "status": "ACTIVE", "fineract_status": "Active",
 "outstanding": {"principal": "4616600", "interest": "425799", "fees": "0", "penalties": "0", "total": "5042399"},
 "transactions": [{"external_reference": "REP-0001", "type": "REPAYMENT", "fineract_type": "Repayment", "...": "..."}]}
```

### `GET /api/transactions/{external_reference}` — what happened to one money movement

One call returns:

* the lifecycle status, attempts and next attempt;
* any error;
* the request as received;
* Fineract's record (`recorded`) and the ERP entry number;
* the correlation id to search the logs for.

### `POST /api/reconciliation`

Give either `{"from_date": "2026-01-01", "to_date": "2026-12-31"}` or `{"external_references": ["DISB-0001",
"REP-0001"]}`. The response is a read-only report:

```json
{"criteria": {...}, "summary": {"MATCHED": 2, "MISSING_IN_ERP": 1},
 "items": [{"external_reference": "REP-0001", "loan_reference": "LN-2026-000001", "outcome": "MATCHED", "note": null,
            "fineract": {"type": "REPAYMENT", "transaction_date": "2026-02-15", "amount": "458400", "reversed": false},
            "erp": {"entry_number": "JE-000002", "type": "REPAYMENT", "entry_date": "2026-02-15", "amount": "458400"}}]}
```

The possible outcomes are:

* `MATCHED`;
* `MISSING_IN_ERP`;
* `MISSING_IN_FINERACT`;
* `AMOUNT_MISMATCH`;
* `TYPE_MISMATCH`, a fifth outcome so that a type difference is never reported as an amount difference.

### Admin

* `POST /api/admin/recovery`: run the recovery sweep now, instead of at its next scheduled run.
* `GET /api/admin/operations?status=NEEDS_RECONCILIATION`: operations waiting for a human (the dead-letter view).
* `POST /api/admin/operations/{type}/{external_reference}/resume`: the explicit, idempotent repair. It re-drives the
  ERP posting of a disbursement or repayment in NEEDS_RECONCILIATION whose Fineract side is recorded, for example
  after the ERP was down beyond the retry budget or rejected the entry. It refuses (`409 NOT_RESUMABLE`) when
  Fineract's side is not settled: that needs a decision in Fineract first.
* `GET|PUT /api/admin/faults`: fault injection, present only when `INTEGRATION_FAULT_INJECTION_ENABLED=true` (test
  stacks). The body is `{"fineract": "none|unavailable|timeout_after_send", "remaining": n, "crash_after_fineract":
  false}`.

## Mock ERP API

Base URL `http://localhost:58001`, with `Authorization: Bearer <token>`.

| Endpoint | Notes |
| --- | --- |
| `POST /api/accounts` | `{code, name, type, currency, external_reference}`. `201`, or `200` on replay; `409` for a reused reference with a different payload, or a taken code. |
| `GET /api/accounts` | The chart of accounts (seeded with 9 accounts, all RWF) |
| `GET /api/accounts/{code}/balance` | Debit and credit totals; the balance on the account's normal side |
| `POST /api/journal-entries` | `{external_reference, entry_date, transaction_type, memo, lines: [{account_code, debit, credit}]}`. Must balance (`422 UNBALANCED_ENTRY`). `201`, or `200` on replay; `409` on a different payload. |
| `GET /api/journal-entries/{external_reference}` | One entry, with its `entry_number` (`JE-000001`, the ERP's own id) |
| `GET /api/journal-entries?from_date=&to_date=` | Entries by date, for reconciliation |
| `GET|PUT /_faults` | Test stacks only: `{"mode": "none|unavailable|timeout_after_commit|rate_limited", "delay_seconds", "remaining"}` |
