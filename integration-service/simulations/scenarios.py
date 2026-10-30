"""Every simulation the brief asks for, against the running stack. Each returns filled-in evidence whose checks
decide pass or fail; ``tests/stack`` asserts them, ``python -m simulations`` prints them."""

import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from simulations.evidence import Evidence
from simulations.harness import REPOSITORY, SUBMITTED, Stack, wait_until


def client_identity(stack: Stack) -> Evidence:
    evidence = Evidence("Client identity", "Part C (client_uid as Fineract externalId), Part F (client idempotency)")
    client_uid, reference = stack.reference("SME"), stack.reference("CLI")
    body = {
        "external_reference": reference,
        "client_uid": client_uid,
        "first_name": "Amina",
        "last_name": "Uwase",
        "activation_date": "2026-01-10",
    }
    evidence.initial_state = {"client_uid": client_uid, "in_fineract": _fineract_client(stack, client_uid) is not None}

    created = stack.post("/api/clients", body)
    replay = stack.post("/api/clients", body)
    taken = stack.post("/api/clients", {**body, "external_reference": stack.reference("CLI")})
    found = stack.get(f"/api/clients/{client_uid}")
    in_fineract = _fineract_client(stack, client_uid) or {}

    evidence.request = {"POST /api/clients": body, "then": "same request again; same client_uid under a new reference"}
    evidence.response = {
        "create": [created.status_code, created.json()],
        "replay": [replay.status_code, replay.json()],
        "new reference, same client_uid": [taken.status_code, taken.json()["error"]["code"]],
        f"GET /api/clients/{client_uid}": found,
    }
    evidence.fineract_state = {
        "externalId": in_fineract.get("externalId"),
        "displayName": in_fineract.get("displayName"),
        "active": in_fineract.get("active"),
    }
    evidence.integration_state = {
        "mapping rows for client_uid": stack.sql("SELECT count(*) FROM clients WHERE client_uid = %s", client_uid)[0][0]
    }
    evidence.expected = "201 then 200 with the same body; 409 for the reused client_uid; exists, active, correct name"
    evidence.check("created", created.status_code == 201)
    evidence.check("replayed", replay.status_code == 200 and replay.json() == created.json())
    evidence.check("client_uid unique", taken.status_code == 409)
    fineract_view = found["fineract"] or {}
    evidence.check(
        "exists, active, correct name", bool(fineract_view.get("active") and fineract_view.get("name_matches"))
    )
    evidence.check("no Fineract id in the API", "id" not in created.json() and "fineract_client_id" not in str(found))
    evidence.actual = _verdict(evidence)
    evidence.explanation = (
        "client_uid is stored as the Fineract client's externalId and is unique in the integration database. The "
        "lookup reads Fineract by external id, so Fineract's numeric id never reaches the partner."
    )
    return evidence


def gl_account_sync(stack: Stack) -> Evidence:
    evidence = Evidence("GL account sync", "Part A (Python side): create-only sync, retry while Fineract is down")
    code = f"7{int(stack.run, 16) % 1000:03d}"
    reference = f"GL-{code}-{stack.run}"
    account = {
        "code": code,
        "name": f"Equipment {stack.run}",
        "type": "ASSET",
        "currency": "RWF",
        "external_reference": reference,
    }

    erp_statuses = [stack.erp.post("/api/accounts", json=account).status_code for _ in range(4)]
    stack.integration_faults(fineract="unavailable", remaining=1)
    first_sync = stack.api.post("/api/accounts/sync", json={"external_references": [reference]}).json()
    stack.clear_faults()
    retried = wait_until(lambda: _sync_status(stack, reference, recover=True) == "SYNCED", timeout=30)
    later_syncs = [
        stack.api.post("/api/accounts/sync", json={"external_references": [reference]}).json() for _ in range(3)
    ]

    erp_accounts = [item for item in stack.erp.get("/api/accounts").json() if item["external_reference"] == reference]
    fineract_accounts = [
        item for item in stack.fineract.get("/glaccounts").json() if item.get("externalId") == reference
    ]
    mappings = stack.sql("SELECT status, attempts FROM gl_account_syncs WHERE external_reference = %s", reference)

    evidence.initial_state = "A new ERP account, unknown to Fineract"
    evidence.request = {
        "ERP POST /api/accounts x4": account,
        "POST /api/accounts/sync x4": {"external_references": [reference]},
    }
    evidence.response = {
        "ERP account creates": erp_statuses,
        "sync 1 (Fineract down)": first_sync,
        "syncs 2-4": [sync[0]["status"] for sync in later_syncs],
    }
    evidence.fineract_state = {
        "GL accounts with this externalId": len(fineract_accounts),
        "glCode": [a["glCode"] for a in fineract_accounts],
    }
    evidence.erp_state = {"accounts with this external_reference": len(erp_accounts)}
    evidence.integration_state = {"mapping rows": [list(row) for row in mappings]}
    evidence.expected = (
        "FAILED while Fineract is down, SYNCED after the retry; one ERP account, one GL account, one mapping"
    )
    evidence.check("ERP account created once", erp_statuses == [201, 200, 200, 200] and len(erp_accounts) == 1)
    evidence.check("failed while Fineract was down", first_sync[0]["status"] == "FAILED")
    evidence.check("synced by the retry", retried)
    evidence.check("one Fineract GL account", len(fineract_accounts) == 1)
    evidence.check("one mapping", len(mappings) == 1 and mappings[0][0] == "SYNCED")
    evidence.actual = _verdict(evidence)
    evidence.explanation = (
        "The sync creates the GL account with the ERP's external_reference as Fineract externalId. Fineract (patched) "
        "answers a replay with the existing account, so a retry after an outage can never create a second one; "
        "recovery retries FAILED accounts with backoff."
    )
    return evidence


def loan_lifecycle(stack: Stack) -> Evidence:
    evidence = Evidence("Loan lifecycle", "Part D: application → approval → disbursement → repayments → Closed")
    client_uid = stack.reference("SME")
    stages: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []

    def capture(stage: str, response: httpx.Response, loan_reference: str, reference: str | None = None) -> None:
        stages.append({"stage": stage, "http": response.status_code, **stack.fineract_state(loan_reference)})
        if reference is not None:
            entry = stack.erp_entry(reference) or {}
            entries.append({"reference": reference, "entry": entry.get("entry_number"), "total": entry.get("total")})

    stack.post(
        "/api/clients",
        {
            "external_reference": stack.reference("CLI"),
            "client_uid": client_uid,
            "first_name": "Jean",
            "last_name": "Habimana",
            "activation_date": "2026-01-10",
        },
    )
    application = stack.post(
        "/api/loans",
        {
            "external_reference": stack.reference("APP"),
            "client_uid": client_uid,
            "principal": "5000000",
            "annual_interest_rate": "18",
            "term_months": 12,
            "submitted_on": SUBMITTED.isoformat(),
            "expected_disbursement_on": SUBMITTED.isoformat(),
        },
    )
    loan = str(application.json()["loan_reference"])
    capture("application", application, loan)
    capture("approval", stack.approve(loan), loan)
    disbursement = stack.reference("DISB")
    capture("disbursement", stack.disburse(loan, disbursement), loan, disbursement)
    for month in (2, 3, 4):
        reference = stack.reference("REP")
        capture(f"repayment {month - 1}", stack.repay(loan, reference, 458_400, date(2026, month, 15)), loan, reference)
    payoff = Decimal(str(stack.fineract_loan(loan)["summary"]["totalOutstanding"]))
    reference = stack.reference("REP")
    capture(f"final payoff ({payoff})", stack.repay(loan, reference, payoff, date(2026, 10, 27)), loan, reference)

    repayments = stack.fineract_transactions(loan, "repayment")
    evidence.initial_state = {"client_uid": client_uid, "loan_reference": loan}
    evidence.request = (
        "POST clients, loans, approve, disburse, 3 x repayments of 458400, payoff of Fineract's total outstanding"
    )
    evidence.response = [{"stage": stage["stage"], "http": stage["http"]} for stage in stages]
    evidence.fineract_state = stages
    evidence.erp_state = entries
    evidence.integration_state = {"loan_transactions rows": stack.integration_transactions(loan)}
    evidence.expected = "Every stage succeeds; the loan ends 'Closed (obligations met)' with nothing outstanding"
    evidence.check("all stages succeeded", all(stage["http"] == 201 for stage in stages))
    evidence.check(
        "closed", stages[-1]["status"] == "Closed (obligations met)" and stages[-1]["total_outstanding"] == 0
    )
    evidence.check("principal fully repaid", sum(Decimal(str(r["principalPortion"])) for r in repayments) == 5_000_000)
    evidence.check("one ERP entry per money movement", all(entry["entry"] for entry in entries) and len(entries) == 5)
    evidence.actual = _verdict(evidence)
    evidence.explanation = (
        "Statuses and balances above are read from Fineract's own API after each call. The payoff amount is Fineract's "
        "total outstanding; each ERP entry is derived from the principal/interest split Fineract recorded."
    )
    return evidence


def sequential_duplicates(stack: Stack) -> Evidence:
    evidence = Evidence("I1 Sequential duplicates", "Part I1: the same disbursement sent 4 times")
    loan, reference = stack.open_loan(), stack.reference("DISB")
    evidence.initial_state = stack.fineract_state(loan)
    responses = [stack.disburse(loan, reference) for _ in range(4)]

    counts = _counts(stack, loan, [reference], "disbursement")
    evidence.request = f"POST /api/loans/{loan}/disburse x4 with external_reference {reference}"
    evidence.response = [response.status_code for response in responses]
    evidence.fineract_state = stack.fineract_state(loan)
    evidence.erp_state = {"entries": counts["ERP journal entries"]}
    evidence.integration_state = {
        "transactions": counts["Integration transactions"],
        "operation": stack.operation_status(reference),
    }
    evidence.expected = (
        "Requests received: 4, Fineract disbursements: 1, ERP journal entries: 1, Integration transactions: 1"
    )
    evidence.actual = ", ".join(f"{name}: {value}" for name, value in {"Requests received": 4, **counts}.items())
    evidence.check("201 then replays", [r.status_code for r in responses] == [201, 200, 200, 200])
    evidence.check("same body every time", all(r.json() == responses[0].json() for r in responses))
    evidence.check("exactly one of each", list(counts.values()) == [1, 1, 1])
    evidence.explanation = (
        "The first request claims (DISBURSEMENT, reference) in the operations table and completes; the others find the "
        "completed operation and return its stored result without calling Fineract or the ERP."
    )
    return evidence


def concurrent_same_reference(stack: Stack) -> Evidence:
    evidence = Evidence("I2a Concurrent disbursements, same reference", "Part I2(a): 10 simultaneous requests")
    loan, reference = stack.open_loan(), stack.reference("DISB")
    statuses = _simultaneously(10, lambda _: stack.disburse(loan, reference).status_code)
    return _once_per_loan_evidence(evidence, stack, loan, [reference], statuses, "one 201; the rest 200 or 202")


def concurrent_different_references(stack: Stack) -> Evidence:
    evidence = Evidence("I2b Concurrent disbursements, different references", "Part I2(b): 10 simultaneous requests")
    loan = stack.open_loan()
    references = [stack.reference("DISB") for _ in range(10)]
    statuses = _simultaneously(10, lambda i: stack.disburse(loan, references[i]).status_code)
    return _once_per_loan_evidence(
        evidence, stack, loan, references, statuses, "one 201; nine 409 LOAN_ALREADY_DISBURSED"
    )


def concurrent_repayments(stack: Stack) -> Evidence:
    evidence = Evidence("I3 Concurrent repayments", "Part I3: 10 different references at once, plus duplicates of one")
    loan = stack.open_loan(disburse=True)
    before = stack.fineract_state(loan)
    references = [stack.reference("REP") for _ in range(10)]
    # Thirteen requests: ten distinct repayments of 100000, and three extra copies of the first one.
    requests = [*references, references[0], references[0], references[0]]
    statuses = _simultaneously(
        len(requests), lambda i: stack.repay(loan, requests[i], 100_000, date(2026, 2, 15)).status_code
    )

    after = stack.fineract_state(loan)
    recorded = [t for t in stack.fineract_transactions(loan, "repayment") if t.get("externalId") in references]
    principal = sum(Decimal(str(t["principalPortion"])) for t in recorded)
    interest = sum(Decimal(str(t["interestPortion"])) for t in recorded)
    erp_entries = [reference for reference in references if stack.erp_entry(reference)]

    evidence.initial_state = before
    evidence.request = f"13 simultaneous POST /api/loans/{loan}/repayments (10 references, the first sent 4 times)"
    evidence.response = sorted(statuses)
    evidence.fineract_state = {
        "after": after,
        "repayments recorded for these references": len(recorded),
        "distinct references": len({t["externalId"] for t in recorded}),
        "principal portions": str(principal),
        "interest portions": str(interest),
    }
    evidence.erp_state = {"entries": len(erp_entries)}
    evidence.integration_state = {"loan_transactions rows (incl. disbursement)": stack.integration_transactions(loan)}
    expected_principal = Decimal(str(before["principal_outstanding"])) - principal
    evidence.expected = (
        "10 repayments applied once each; outstanding principal = initial - principal portions recorded by Fineract"
    )
    evidence.actual = (
        f"{len(recorded)} repayments; principal outstanding {after['principal_outstanding']} "
        f"(initial {before['principal_outstanding']} - {principal} = {expected_principal})"
    )
    evidence.check("every request answered", all(status in (200, 201, 202) for status in statuses))
    evidence.check(
        "each reference applied once", len(recorded) == 10 and len({t["externalId"] for t in recorded}) == 10
    )
    evidence.check("principal reconciles", Decimal(str(after["principal_outstanding"])) == expected_principal)
    evidence.check(
        "total reconciles",
        Decimal(str(after["total_outstanding"])) == Decimal(str(before["total_outstanding"])) - principal - interest,
    )
    evidence.check("one ERP entry each", len(erp_entries) == 10)
    evidence.explanation = (
        "Different references are different operations, so all ten proceed in parallel; Fineract serializes them "
        "on the loan. The three copies of the first reference hit its operation row and never reach Fineract. The "
        "check uses Fineract's own breakdown, not the amounts the test sent."
    )
    return evidence


def erp_down_after_fineract(stack: Stack) -> Evidence:
    evidence = Evidence("J1 ERP down after Fineract success", "Part J1")
    loan, reference = stack.open_loan(disburse=True), stack.reference("REP")
    stack.erp_faults(mode="unavailable")
    response = stack.repay(loan, reference, 458_400, date(2026, 2, 15))
    during = _snapshot(stack, loan, reference)
    stack.clear_faults()
    caught_up = _completed_by_recovery(stack, reference)
    replay = stack.repay(loan, reference, 458_400, date(2026, 2, 15))

    evidence.initial_state = "ERP unavailable (injected); loan active"
    evidence.request = f"POST /api/loans/{loan}/repayments {reference}"
    evidence.response = {
        "while ERP down": [response.status_code, response.json()],
        "replay afterwards": [replay.status_code, replay.json()],
    }
    evidence.fineract_state = {
        "during": during["fineract transactions"],
        "after": len(stack.fineract_transactions(loan, "repayment")),
    }
    evidence.erp_state = {
        "during": during["erp entry"],
        "after": (stack.erp_entry(reference) or {}).get("entry_number"),
    }
    evidence.integration_state = {"during": during["operation"], "after": stack.operation_status(reference)}
    evidence.expected = "202 ERP_PENDING; repayment in Fineract once; the ERP catches up exactly once when it returns"
    evidence.check(
        "caller told it is pending", response.status_code == 202 and response.json()["status"] == "ERP_PENDING"
    )
    evidence.check("in Fineract, not yet in ERP", during["fineract transactions"] == 1 and during["erp entry"] is None)
    evidence.check("ERP caught up", caught_up and stack.erp_entry(reference) is not None)
    evidence.check(
        "replay returns the result", replay.status_code == 200 and replay.json()["erp_entry_number"] is not None
    )
    evidence.check("still one repayment", len(stack.fineract_transactions(loan, "repayment")) == 1)
    evidence.actual = _verdict(evidence)
    evidence.explanation = (
        "The operation stays ERP_PENDING with a backoff schedule; recovery re-derives the same journal from the stored "
        "Fineract transaction and posts it. Double posting is impossible: Fineract is never called again (its step is "
        "done) and the ERP is idempotent on the external reference."
    )
    return evidence


def fineract_down(stack: Stack) -> Evidence:
    evidence = Evidence("J2 Fineract down", "Part J2")
    loan, reference = stack.open_loan(), stack.reference("DISB")
    stack.integration_faults(fineract="unavailable")
    response = stack.disburse(loan, reference)
    during = _snapshot(stack, loan, reference, "disbursement")
    retryable = stack.sql("SELECT retryable FROM operations WHERE external_reference = %s", reference)[0][0]
    stack.clear_faults()
    retry = stack.disburse(loan, reference)

    evidence.initial_state = "Fineract unreachable (injected connection refused); loan approved"
    evidence.request = f"POST /api/loans/{loan}/disburse {reference}, then the same request again"
    evidence.response = {"while down": [response.status_code, response.json()], "retry": retry.status_code}
    evidence.fineract_state = {
        "during": during["fineract transactions"],
        "after": len(stack.fineract_transactions(loan, "disbursement")),
    }
    evidence.erp_state = {
        "during": during["erp entry"],
        "after": (stack.erp_entry(reference) or {}).get("entry_number"),
    }
    evidence.integration_state = {
        "during": [during["operation"], f"retryable={retryable}"],
        "after": stack.operation_status(reference),
    }
    evidence.expected = (
        "503 FINERACT_UNAVAILABLE; only the operation row persisted (FAILED, retryable); the retry succeeds once"
    )
    evidence.check("503", response.status_code == 503 and response.json()["error"]["code"] == "FINERACT_UNAVAILABLE")
    evidence.check("nothing in Fineract or ERP", during["fineract transactions"] == 0 and during["erp entry"] is None)
    evidence.check("failed, retryable", during["operation"] == "FAILED" and retryable is True)
    evidence.check(
        "retry succeeds once", retry.status_code == 201 and len(stack.fineract_transactions(loan, "disbursement")) == 1
    )
    evidence.actual = _verdict(evidence)
    evidence.explanation = (
        "A connection that never opened is classified 'not applied': nothing can have happened in Fineract, so the "
        "operation is FAILED with retryable=true and the same reference may be sent again. It frees the loan's "
        "disbursement slot, since FAILED operations are excluded from the once-per-loan index."
    )
    return evidence


def fineract_timeout_after_send(stack: Stack) -> Evidence:
    evidence = Evidence("J3 Fineract timeout after sending", "Part J3")
    loan, reference = stack.open_loan(), stack.reference("DISB")
    correlation_id = f"sim-j3-{stack.run}"
    stack.integration_faults(fineract="timeout_after_send", remaining=1)
    body = {"external_reference": reference, "amount": "5000000", "transaction_date": SUBMITTED.isoformat()}
    response = stack.post(f"/api/loans/{loan}/disburse", body, correlation_id)
    trail = [
        f"{line['status']}: {line['event']}"
        for line in stack.logs(correlation_id)
        if line["logger"].endswith("executor")
    ]

    evidence.initial_state = "Fineract processes the command but the answer is lost (injected read timeout)"
    evidence.request = f"POST /api/loans/{loan}/disburse {reference} (X-Correlation-ID {correlation_id})"
    evidence.response = [response.status_code, response.json()]
    evidence.fineract_state = {"disbursements": len(stack.fineract_transactions(loan, "disbursement"))}
    evidence.erp_state = {"entry": (stack.erp_entry(reference) or {}).get("entry_number")}
    evidence.integration_state = {"operation": stack.operation_status(reference), "executor log trail": trail}
    evidence.expected = (
        "Not resent blindly: the lookup by externalId finds the disbursement; one disbursement, one entry"
    )
    evidence.check("completed", response.status_code == 201)
    evidence.check("one disbursement", len(stack.fineract_transactions(loan, "disbursement")) == 1)
    evidence.check("found by lookup, not resubmitted", any("already in Fineract" in line for line in trail))
    evidence.actual = _verdict(evidence)
    evidence.explanation = (
        "The read timeout after sending is an unknown outcome. The next attempt starts with GET "
        "/loans/external-id/{loan}/transactions/external-id/{reference}, finds the transaction and continues; had the "
        "lookup failed too, the operation would stay FINERACT_PENDING for recovery — never FAILED."
    )
    return evidence


def erp_timeout_after_write(stack: Stack) -> Evidence:
    evidence = Evidence("J4 ERP timeout after the entry was written", "Part J4")
    loan, reference = stack.open_loan(disburse=True), stack.reference("REP")
    stack.erp_faults(mode="timeout_after_commit", delay_seconds=8, remaining=1)
    response = stack.repay(loan, reference, 458_400, date(2026, 2, 15))
    during = _snapshot(stack, loan, reference)
    completed = _completed_by_recovery(stack, reference)
    entries = [
        entry
        for entry in stack.erp.get(
            "/api/journal-entries", params={"from_date": "2026-02-15", "to_date": "2026-02-15"}
        ).json()
        if entry["external_reference"] == reference
    ]

    evidence.initial_state = "The ERP commits journal entries, then holds the answer past the integration's 5 s timeout"
    evidence.request = f"POST /api/loans/{loan}/repayments {reference}"
    evidence.response = [response.status_code, response.json()]
    evidence.fineract_state = {"repayments": len(stack.fineract_transactions(loan, "repayment"))}
    evidence.erp_state = {"during": during["erp entry"], "entries with this reference after the retry": len(entries)}
    evidence.integration_state = {"during": during["operation"], "after": stack.operation_status(reference)}
    evidence.expected = "202 ERP_PENDING; the retry is answered with the existing entry; exactly one journal entry"
    evidence.check("pending after the timeout", response.status_code == 202 and during["operation"] == "ERP_PENDING")
    evidence.check("entry was written", during["erp entry"] is not None)
    evidence.check("completed by the retry", completed)
    evidence.check("exactly one journal entry", len(entries) == 1)
    evidence.actual = _verdict(evidence)
    evidence.explanation = (
        "The ERP enforces uniqueness on external_reference and compares payloads. The retry sends the identical entry "
        "(derived from the stored Fineract transaction), so the ERP answers 200 with the entry it already has."
    )
    return evidence


def crash_before_erp(stack: Stack) -> Evidence:
    evidence = Evidence("J5 Crash between Fineract success and the ERP call", "Part J5")
    loan, reference = stack.open_loan(), stack.reference("DISB")
    restarts_before = _restart_count()
    stack.integration_faults(crash_after_fineract=True)
    try:
        crashed = stack.disburse(loan, reference).status_code
    except httpx.HTTPError as error:
        crashed = type(error).__name__
    during = _snapshot(stack, loan, reference, "disbursement")
    completed = wait_until(lambda: stack.operation_status(reference) == "COMPLETED", timeout=120, interval=2)

    evidence.initial_state = (
        "The integration service exits right after Fineract confirms (injected); compose restarts it"
    )
    evidence.request = f"POST /api/loans/{loan}/disburse {reference}"
    evidence.response = {"caller saw": crashed}
    evidence.fineract_state = {
        "during": during["fineract transactions"],
        "after": len(stack.fineract_transactions(loan, "disbursement")),
    }
    evidence.erp_state = {
        "during": during["erp entry"],
        "after": (stack.erp_entry(reference) or {}).get("entry_number"),
    }
    evidence.integration_state = {
        "during": during["operation"],
        "after": stack.operation_status(reference),
        "container restarts": _restart_count() - restarts_before,
    }
    evidence.expected = (
        "FINERACT_DONE survives the crash; after restart and lease expiry, recovery posts the ERP entry once"
    )
    evidence.check("process died", crashed != 201)
    evidence.check(
        "state persisted before the crash",
        during["operation"] == "FINERACT_DONE" and during["fineract transactions"] == 1,
    )
    evidence.check("recovered after restart", completed)
    evidence.check(
        "one disbursement, one entry",
        len(stack.fineract_transactions(loan, "disbursement")) == 1 and stack.erp_entry(reference) is not None,
    )
    evidence.actual = _verdict(evidence)
    evidence.explanation = (
        "Each status is committed before the next external call, so the restart finds FINERACT_DONE with the dead "
        "process's lease. When the lease expires, the recovery sweep runs the same executor: Fineract is already done, "
        "so only the ERP entry is posted."
    )
    return evidence


def reconciliation(stack: Stack) -> Evidence:
    evidence = Evidence("K Reconciliation", "Part K: all four outcomes, seeded deliberately; read-only")
    loan = stack.open_loan()
    disbursement, matched, missing_in_erp, missing_in_fineract, mismatch = (
        stack.reference("DISB"),
        stack.reference("REP"),
        stack.reference("TELLER"),
        stack.reference("MANUAL"),
        stack.reference("TELLER"),
    )
    stack.disburse(loan, disbursement)
    stack.repay(loan, matched, 458_400, date(2026, 2, 15))
    _fineract_direct_repayment(stack, loan, missing_in_erp, 100_000, "2026-03-15")  # a teller posts in Fineract only
    _erp_direct_entry(stack, missing_in_fineract, 75_000, "2026-03-15")  # an accountant posts in the ERP only
    _fineract_direct_repayment(stack, loan, mismatch, 100_000, "2026-03-20")
    _erp_direct_entry(stack, mismatch, 90_000, "2026-03-20")  # typed with the wrong amount

    references = [disbursement, matched, missing_in_erp, missing_in_fineract, mismatch]
    erp_before = [bool(stack.erp_entry(reference)) for reference in references]
    report = stack.api.post("/api/reconciliation", json={"external_references": references}).json()
    erp_after = [bool(stack.erp_entry(reference)) for reference in references]
    outcomes = {item["external_reference"]: item["outcome"] for item in report["items"]}

    evidence.initial_state = "Two transactions through the integration, plus three seeded directly in one system"
    evidence.request = {"POST /api/reconciliation": {"external_references": references}}
    evidence.response = report
    evidence.fineract_state = "repayments posted directly for the TELLER references"
    evidence.erp_state = {"entries before": erp_before, "entries after": erp_after}
    evidence.expected = "MATCHED x2, MISSING_IN_ERP, MISSING_IN_FINERACT, AMOUNT_MISMATCH; nothing written"
    evidence.check("matched", outcomes.get(disbursement) == outcomes.get(matched) == "MATCHED")
    evidence.check("missing in ERP", outcomes.get(missing_in_erp) == "MISSING_IN_ERP")
    evidence.check("missing in Fineract", outcomes.get(missing_in_fineract) == "MISSING_IN_FINERACT")
    evidence.check("amount mismatch", outcomes.get(mismatch) == "AMOUNT_MISMATCH")
    evidence.check("read-only", erp_before == erp_after)
    evidence.actual = _verdict(evidence)
    evidence.explanation = (
        "Each Fineract disbursement or repayment is matched to the ERP entry with the same external_reference. The "
        "report never writes. Repairs are separate, explicit actions: POST /api/admin/recovery retries pending "
        "postings now, and POST /api/admin/operations/{type}/{reference}/resume re-drives one that ran out of retries."
    )
    return evidence


TRAIL_FIELDS = (
    "timestamp",
    "service",
    "event",
    "correlation_id",
    "status",
    "fineract_transaction_id",
    "erp_transaction_id",
)


def trace_one_reference(stack: Stack) -> Evidence:
    evidence = Evidence(
        "Tracing FIN-2026-000001", "Section 9: what happened to one reference, from logs and one API call"
    )
    reference = "FIN-2026-000001" if stack.operation_status("FIN-2026-000001") is None else stack.reference("FIN-2026")
    loan = stack.open_loan(disburse=True)
    stack.erp_faults(mode="unavailable", remaining=1)
    first = stack.repay(loan, reference, 458_400, date(2026, 2, 15), correlation_id=f"partner-{reference}")
    _completed_by_recovery(stack, reference)
    lookup = stack.get(f"/api/transactions/{reference}")
    trail = [
        {key: line.get(key) for key in TRAIL_FIELDS}
        for service in ("integration-service", "mock-erp")
        for line in stack.logs(reference, service)
    ]

    evidence.initial_state = "ERP unavailable for one call, so the trail shows a deferral and recovery"
    evidence.request = f"POST /api/loans/{loan}/repayments {reference}, then GET /api/transactions/{reference}"
    evidence.response = {"first answer": [first.status_code, first.json()], "lookup": lookup}
    evidence.integration_state = {"log trail": sorted(trail, key=lambda line: str(line["timestamp"]))}
    evidence.expected = "One API call shows the outcome; the logs show each step under one correlation id"
    evidence.check("completed", lookup["status"] == "COMPLETED" and lookup["recorded"]["erp_entry_number"] is not None)
    evidence.check(
        "trail under one correlation id",
        len({line["correlation_id"] for line in trail if line["service"] == "integration-service"}) == 1,
    )
    evidence.actual = _verdict(evidence)
    evidence.explanation = (
        "Recovery re-binds the correlation id stored on the operation, so the deferred ERP posting is logged under "
        "the partner's original correlation id."
    )
    return evidence


SCENARIOS: dict[str, Callable[[Stack], Evidence]] = {
    "client": client_identity,
    "gl-sync": gl_account_sync,
    "lifecycle": loan_lifecycle,
    "duplicates": sequential_duplicates,
    "concurrent-same": concurrent_same_reference,
    "concurrent-different": concurrent_different_references,
    "concurrent-repayments": concurrent_repayments,
    "erp-down": erp_down_after_fineract,
    "fineract-down": fineract_down,
    "fineract-timeout": fineract_timeout_after_send,
    "erp-timeout": erp_timeout_after_write,
    "crash": crash_before_erp,
    "reconciliation": reconciliation,
    "trace": trace_one_reference,
}


# --- Helpers ---------------------------------------------------------------------------------------------------


def _simultaneously(count: int, call: Callable[[int], Any]) -> list[Any]:
    start = threading.Barrier(count)

    def released(index: int) -> Any:
        start.wait()
        return call(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(released, range(count)))


def _counts(stack: Stack, loan: str, references: list[str], kind: str) -> dict[str, int]:
    return {
        "Fineract disbursements" if kind == "disbursement" else "Fineract repayments": len(
            stack.fineract_transactions(loan, kind)
        ),
        "ERP journal entries": sum(1 for reference in references if stack.erp_entry(reference)),
        "Integration transactions": stack.integration_transactions(loan),
    }


def _once_per_loan_evidence(
    evidence: Evidence, stack: Stack, loan: str, references: list[str], statuses: list[int], expected_statuses: str
) -> Evidence:
    counts = _counts(stack, loan, references, "disbursement")
    evidence.initial_state = {"loan": loan, "status": "Approved", "disbursements": 0}
    evidence.request = f"{len(statuses)} simultaneous POST /api/loans/{loan}/disburse"
    evidence.response = sorted(statuses)
    evidence.fineract_state = stack.fineract_state(loan)
    evidence.erp_state = {"entries": counts["ERP journal entries"]}
    evidence.integration_state = {"transactions": counts["Integration transactions"]}
    evidence.expected = f"Exactly one disbursement everywhere; {expected_statuses}"
    evidence.actual = f"statuses {sorted(statuses)}; " + ", ".join(f"{k}: {v}" for k, v in counts.items())
    evidence.check("one 201", statuses.count(201) == 1)
    evidence.check("exactly one of each", list(counts.values()) == [1, 1, 1])
    evidence.explanation = (
        "Layers: (1) unique (type, external_reference) — same reference waits on one operation (202/200); "
        "(2) partial unique index, one active DISBURSEMENT per loan — different references lose with 409; "
        "(3) Fineract externalId lookup before every submit and a unique index on m_loan_transaction.external_id; "
        "(4) last line of defence: Fineract's loan state refuses a second disbursement of a single-tranche loan. "
        "(1) misses different references, (2) lives in our database only, (3) only dedupes one reference; (4) is "
        "the system of record for the money."
    )
    return evidence


def _snapshot(stack: Stack, loan: str, reference: str, kind: str = "repayment") -> dict[str, Any]:
    return {
        "fineract transactions": len(stack.fineract_transactions(loan, kind)),
        "erp entry": stack.erp_entry_number_in_database(reference),
        "operation": stack.operation_status(reference),
    }


def _completed_by_recovery(stack: Stack, reference: str, timeout: float = 60) -> bool:
    """Waits for the operation to complete, nudging recovery instead of waiting for its next scheduled sweep."""

    def completed() -> bool:
        if stack.operation_status(reference) == "COMPLETED":
            return True
        stack.recover()
        return False

    return wait_until(completed, timeout, interval=2)


def _sync_status(stack: Stack, reference: str, *, recover: bool) -> str | None:
    if recover:
        stack.recover()
    rows = stack.sql("SELECT status FROM gl_account_syncs WHERE external_reference = %s", reference)
    return str(rows[0][0]) if rows else None


def _fineract_client(stack: Stack, client_uid: str) -> dict[str, Any] | None:
    response = stack.fineract.get(f"/clients/external-id/{client_uid}")
    return None if response.status_code == 404 else response.json()


def _fineract_direct_repayment(stack: Stack, loan: str, reference: str, amount: int, on: str) -> None:
    payment_type = next(
        item["id"] for item in stack.fineract.get("/paymenttypes").json() if item["name"] == "Bank Transfer"
    )
    stack.fineract.post(
        f"/loans/external-id/{loan}/transactions",
        params={"command": "repayment"},
        json={
            "transactionDate": on,
            "transactionAmount": amount,
            "externalId": reference,
            "paymentTypeId": payment_type,
            "dateFormat": "yyyy-MM-dd",
            "locale": "en",
        },
    ).raise_for_status()


def _erp_direct_entry(stack: Stack, reference: str, amount: int, on: str) -> None:
    stack.erp.post(
        "/api/journal-entries",
        json={
            "external_reference": reference,
            "entry_date": on,
            "transaction_type": "REPAYMENT",
            "memo": "Posted directly in the ERP",
            "lines": [{"account_code": "1100", "debit": str(amount)}, {"account_code": "1200", "credit": str(amount)}],
        },
    ).raise_for_status()


def _restart_count() -> int:
    container = subprocess.run(
        ["docker", "compose", "ps", "-q", "integration-service"],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    inspected = subprocess.run(
        ["docker", "inspect", "-f", "{{.RestartCount}}", container], capture_output=True, text=True, check=True
    ).stdout.strip()
    return int(inspected or 0)


def _verdict(evidence: Evidence) -> str:
    return "\n".join(f"- {'✔' if ok else '✘ FAILED:'} {name}" for name, ok in evidence.checks.items())
