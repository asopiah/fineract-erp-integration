"""Drives the running stack (docker compose) and observes each system directly.

Observations never go through the integration service alone: Fineract is read with its own API, the ERP with its
own, and the integration service's tables with SQL. Every run uses fresh references, so scenarios can be repeated
against the same stack.
"""

import itertools
import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg

REPOSITORY = Path(__file__).resolve().parents[2]

INTEGRATION_URL = os.environ.get("STACK_INTEGRATION_URL", "http://localhost:58090")
ERP_URL = os.environ.get("STACK_ERP_URL", "http://localhost:58001")
FINERACT_URL = os.environ.get("STACK_FINERACT_URL", "http://localhost:58080/fineract-provider/api/v1")
INTEGRATION_DB = os.environ.get("STACK_INTEGRATION_DB", "postgresql://postgres:postgres@localhost:55432/integration")
ERP_DB = os.environ.get("STACK_ERP_DB", "postgresql://postgres:postgres@localhost:55432/mock_erp")

ACTIVATION = date(2026, 1, 10)
SUBMITTED = date(2026, 1, 15)


class Stack:
    def __init__(self) -> None:
        self.api = httpx.Client(
            base_url=INTEGRATION_URL, headers={"Authorization": "Bearer integration-local-token"}, timeout=90
        )
        self.erp = httpx.Client(base_url=ERP_URL, headers={"Authorization": "Bearer erp-local-token"}, timeout=30)
        self.fineract = httpx.Client(
            base_url=FINERACT_URL,
            auth=("mifos", "password"),
            headers={"Fineract-Platform-TenantId": "default"},
            timeout=30,
        )
        self.run = uuid.uuid4().hex[:6].upper()
        self._sequence = itertools.count(1)

    def reference(self, prefix: str) -> str:
        return f"{prefix}-{self.run}-{next(self._sequence):03d}"

    # --- Partner API -------------------------------------------------------------------------------------------

    def post(self, path: str, body: dict[str, Any], correlation_id: str | None = None) -> httpx.Response:
        headers = {"X-Correlation-ID": correlation_id} if correlation_id else {}
        return self.api.post(path, json=body, headers=headers)

    def get(self, path: str) -> dict[str, Any]:
        response = self.api.get(path)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def open_loan(self, principal: int = 5_000_000, *, disburse: bool = False) -> str:
        """A new client with an approved (optionally disbursed) loan. Returns the loan reference."""
        client_uid = self.reference("SME")
        _expect(
            self.post(
                "/api/clients",
                {
                    "external_reference": self.reference("CLI"),
                    "client_uid": client_uid,
                    "first_name": "Amina",
                    "last_name": "Uwase",
                    "activation_date": ACTIVATION.isoformat(),
                },
            ),
            201,
        )
        application = _expect(
            self.post(
                "/api/loans",
                {
                    "external_reference": self.reference("APP"),
                    "client_uid": client_uid,
                    "principal": str(principal),
                    "annual_interest_rate": "18",
                    "term_months": 12,
                    "submitted_on": SUBMITTED.isoformat(),
                    "expected_disbursement_on": SUBMITTED.isoformat(),
                },
            ),
            201,
        )
        loan_reference = str(application["loan_reference"])
        _expect(self.approve(loan_reference), 201)
        if disburse:
            _expect(self.disburse(loan_reference, self.reference("DISB"), principal), 201)
        return loan_reference

    def approve(self, loan_reference: str) -> httpx.Response:
        body = {"external_reference": self.reference("APR"), "approved_on": SUBMITTED.isoformat()}
        return self.post(f"/api/loans/{loan_reference}/approve", body)

    def disburse(self, loan_reference: str, reference: str, amount: int = 5_000_000) -> httpx.Response:
        body = {"external_reference": reference, "amount": str(amount), "transaction_date": SUBMITTED.isoformat()}
        return self.post(f"/api/loans/{loan_reference}/disburse", body)

    def repay(
        self, loan_reference: str, reference: str, amount: int | Decimal, on: date, correlation_id: str | None = None
    ) -> httpx.Response:
        body = {"external_reference": reference, "amount": str(amount), "transaction_date": on.isoformat()}
        return self.post(f"/api/loans/{loan_reference}/repayments", body, correlation_id)

    def recover(self) -> dict[str, Any]:
        response = self.api.post("/api/admin/recovery")
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    # --- Faults ------------------------------------------------------------------------------------------------

    def integration_faults(self, **config: Any) -> None:
        self.api.put("/api/admin/faults", json=config).raise_for_status()

    def erp_faults(self, **config: Any) -> None:
        self.erp.put("/_faults", json=config).raise_for_status()

    def clear_faults(self) -> None:
        self.integration_faults()
        self.erp_faults()

    # --- Observations ------------------------------------------------------------------------------------------

    def fineract_loan(self, loan_reference: str) -> dict[str, Any]:
        response = self.fineract.get(f"/loans/external-id/{loan_reference}", params={"associations": "transactions"})
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def fineract_state(self, loan_reference: str) -> dict[str, Any]:
        loan = self.fineract_loan(loan_reference)
        summary = loan.get("summary") or {}
        return {
            "status": loan["status"]["value"],
            "principal_outstanding": summary.get("principalOutstanding", 0),
            "interest_outstanding": summary.get("interestOutstanding", 0),
            "total_outstanding": summary.get("totalOutstanding", 0),
            "disbursements": len(self.fineract_transactions(loan_reference, "disbursement")),
            "repayments": len(self.fineract_transactions(loan_reference, "repayment")),
        }

    def fineract_transactions(self, loan_reference: str, kind: str) -> list[dict[str, Any]]:
        """Non-reversed transactions of one kind ("disbursement", "repayment") as Fineract recorded them."""
        return [
            transaction
            for transaction in self.fineract_loan(loan_reference).get("transactions") or []
            if transaction["type"].get(kind) and not transaction.get("manuallyReversed")
        ]

    def erp_entry(self, reference: str) -> dict[str, Any] | None:
        response = self.erp.get(f"/api/journal-entries/{reference}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def erp_balance(self, code: str) -> str:
        return str(self.erp.get(f"/api/accounts/{code}/balance").json()["balance"])

    def erp_entry_number_in_database(self, reference: str) -> str | None:
        """Reads the ERP's tables directly: works while the ERP's API is down (that is when it is needed)."""
        rows = self.sql("SELECT id FROM journal_entries WHERE external_reference = %s", reference, database=ERP_DB)
        return f"JE-{rows[0][0]:06d}" if rows else None

    def sql(self, query: str, *params: Any, database: str = INTEGRATION_DB) -> list[tuple[Any, ...]]:
        with psycopg.connect(database) as connection:
            return connection.execute(query, params).fetchall()

    def operation_status(self, reference: str) -> str | None:
        rows = self.sql("SELECT status FROM operations WHERE external_reference = %s ORDER BY id DESC", reference)
        return str(rows[0][0]) if rows else None

    def integration_transactions(self, loan_reference: str) -> int:
        return int(self.sql("SELECT count(*) FROM loan_transactions WHERE loan_reference = %s", loan_reference)[0][0])

    def logs(self, needle: str, service: str = "integration-service") -> list[dict[str, Any]]:
        """JSON log lines of a compose service that mention ``needle`` (a reference or a correlation id)."""
        output = subprocess.run(
            ["docker", "compose", "logs", "--no-log-prefix", service],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        lines = []
        for line in output.splitlines():
            if needle in line and line.startswith("{"):
                lines.append(json.loads(line))
        return lines


def wait_until(condition: Callable[[], bool], timeout: float, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if condition():
                return True
        except httpx.HTTPError:
            pass  # the service may be restarting
        time.sleep(interval)
    return False


def _expect(response: httpx.Response, status: int) -> dict[str, Any]:
    if response.status_code != status:
        raise AssertionError(f"expected {status}, got {response.status_code}: {response.text}")
    result: dict[str, Any] = response.json()
    return result
