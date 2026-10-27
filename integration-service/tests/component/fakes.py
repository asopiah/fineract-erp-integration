"""In-memory stand-ins for Fineract and the ERP that can be told to fail in each way that matters.

They keep the invariants the real systems enforce (Fineract: unique external ids, one disbursement per loan, loan
state; ERP: idempotency on external_reference), because the integration relies on them as its last line of defence.
"""

import itertools
import threading
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from enum import Enum, auto

from integration_service.domain.accounting import JournalEntryDraft
from integration_service.domain.erp import ErpAccount, ErpJournalEntry
from integration_service.domain.fineract import (
    FineractClient,
    FineractLoan,
    FineractLoanTransaction,
    LoanBalances,
    LoanStatus,
    LoanTransactionType,
)
from integration_service.gateways.errors import NotAppliedError, OutcomeUnknownError, RejectedError

ZERO = Decimal(0)


class Fault(Enum):
    PASS = auto()  # behave normally (to place a fault on a later call)
    NOT_APPLIED = auto()  # nothing reached the system
    REJECTED = auto()  # processed and refused
    UNKNOWN_BEFORE = auto()  # answer lost, and in fact nothing happened
    UNKNOWN_AFTER = auto()  # answer lost after the effect was committed
    MISSING = auto()  # (lookups only) briefly does not see a committed record


class Scripted:
    """Per-method queues of faults; a call with an empty queue behaves normally."""

    def __init__(self, system: str) -> None:
        self._system = system
        self._faults: dict[str, deque[Fault]] = defaultdict(deque)
        self._lock = threading.RLock()
        self.calls: list[str] = []

    def fail(self, method: str, *faults: Fault) -> None:
        with self._lock:
            self._faults[method].extend(faults)

    def _command(self, method: str, apply: Callable[[], None]) -> None:
        with self._lock:
            fault = self._faults[method].popleft() if self._faults[method] else None
            if fault is Fault.NOT_APPLIED:
                raise NotAppliedError(self._system, f"{method}: injected outage")
            if fault is Fault.REJECTED:
                raise RejectedError(self._system, f"{method}: injected rejection", status_code=403, code="test.reject")
            if fault is Fault.UNKNOWN_BEFORE:
                raise OutcomeUnknownError(self._system, f"{method}: injected timeout before processing")
            apply()
            self.calls.append(method)
            if fault is Fault.UNKNOWN_AFTER:
                raise OutcomeUnknownError(self._system, f"{method}: injected timeout after processing")

    def _lookup[T](self, method: str, find: Callable[[], T | None]) -> T | None:
        with self._lock:
            fault = self._faults[method].popleft() if self._faults[method] else None
            if fault is Fault.NOT_APPLIED:
                raise NotAppliedError(self._system, f"{method}: injected outage")
            if fault is Fault.MISSING:
                return None
            return find()


class FakeFineract(Scripted):
    def __init__(self) -> None:
        super().__init__("fineract")
        self._ids = itertools.count(1)
        self.clients: dict[str, FineractClient] = {}
        self.loans: dict[str, FineractLoan] = {}
        self.transactions: dict[str, FineractLoanTransaction] = {}
        self.gl_accounts: dict[str, int] = {}

    def commands(self, method: str) -> int:
        return self.calls.count(method)

    def create_gl_account(self, account: ErpAccount) -> int:
        self._command(
            "create_gl_account", lambda: self.gl_accounts.setdefault(account.external_reference, next(self._ids))
        )
        return self.gl_accounts[account.external_reference]

    def find_client(self, client_uid: str) -> FineractClient | None:
        return self._lookup("find_client", lambda: self.clients.get(client_uid))

    def create_client(self, client_uid: str, first_name: str, last_name: str, activation_date: date) -> None:
        def apply() -> None:
            if client_uid in self.clients:
                raise RejectedError("fineract", "duplicate client external id", status_code=403)
            self.clients[client_uid] = FineractClient(
                next(self._ids), client_uid, f"{first_name} {last_name}", True, "Active"
            )

        self._command("create_client", apply)

    def find_loan(self, loan_reference: str) -> FineractLoan | None:
        return self._lookup("find_loan", lambda: self.loans.get(loan_reference))

    def submit_loan_application(
        self,
        loan_reference: str,
        fineract_client_id: int,
        principal: Decimal,
        annual_interest_rate: Decimal,
        term_months: int,
        submitted_on: date,
        expected_disbursement_on: date,
    ) -> None:
        def apply() -> None:
            balances = LoanBalances(ZERO, ZERO, ZERO, ZERO, ZERO)
            self.loans[loan_reference] = FineractLoan(
                next(self._ids), loan_reference, LoanStatus.SUBMITTED, "Submitted", principal, balances, ()
            )

        self._command("submit_loan_application", apply)

    def approve_loan(self, loan_reference: str, approved_on: date, expected_disbursement_on: date) -> None:
        def apply() -> None:
            loan = self.loans[loan_reference]
            if loan.status is not LoanStatus.SUBMITTED:
                raise RejectedError("fineract", "loan is not awaiting approval", status_code=403)
            self.loans[loan_reference] = replace(loan, status=LoanStatus.APPROVED)

        self._command("approve_loan", apply)

    def disburse_loan(self, loan_reference: str, external_reference: str, amount: Decimal, on: date) -> None:
        def apply() -> None:
            loan = self.loans[loan_reference]
            if loan.status is not LoanStatus.APPROVED:
                raise RejectedError("fineract", "loan cannot be disbursed in its current state", status_code=403)
            self._record(loan_reference, external_reference, LoanTransactionType.DISBURSEMENT, amount, on, ZERO, ZERO)
            self.loans[loan_reference] = replace(loan, status=LoanStatus.ACTIVE)

        self._command("disburse_loan", apply)

    def make_repayment(self, loan_reference: str, external_reference: str, amount: Decimal, on: date) -> None:
        def apply() -> None:
            interest = min(amount, Decimal(100))
            self._record(
                loan_reference,
                external_reference,
                LoanTransactionType.REPAYMENT,
                amount,
                on,
                amount - interest,
                interest,
            )

        self._command("make_repayment", apply)

    def find_loan_transaction(self, loan_reference: str, external_reference: str) -> FineractLoanTransaction | None:
        return self._lookup("find_loan_transaction", lambda: self.transactions.get(external_reference))

    def _record(
        self,
        loan_reference: str,
        external_reference: str,
        kind: LoanTransactionType,
        amount: Decimal,
        on: date,
        principal: Decimal,
        interest: Decimal,
    ) -> None:
        if external_reference in self.transactions:  # Fineract: unique index on m_loan_transaction.external_id
            raise RejectedError("fineract", "duplicate transaction external id", status_code=403)
        self.transactions[external_reference] = FineractLoanTransaction(
            internal_id=next(self._ids),
            external_reference=external_reference,
            type=kind,
            fineract_type=kind.value.capitalize(),
            transaction_date=on,
            amount=amount,
            principal=principal,
            interest=interest,
            fees=ZERO,
            penalties=ZERO,
            overpayment=ZERO,
            outstanding_after=ZERO,
            reversed=False,
        )


class FakeErp(Scripted):
    def __init__(self) -> None:
        super().__init__("erp")
        self.entries: dict[str, JournalEntryDraft] = {}

    def list_accounts(self) -> list[ErpAccount]:
        return []

    def post_journal_entry(self, draft: JournalEntryDraft) -> ErpJournalEntry:
        def apply() -> None:
            existing = self.entries.setdefault(draft.external_reference, draft)
            if existing != draft:
                raise RejectedError("erp", "external_reference reused with a different payload", status_code=409)

        self._command("post_journal_entry", apply)
        return self._entry(draft.external_reference)

    def find_journal_entry(self, external_reference: str) -> ErpJournalEntry | None:
        return self._entry(external_reference) if external_reference in self.entries else None

    def _entry(self, external_reference: str) -> ErpJournalEntry:
        draft = self.entries[external_reference]
        number = list(self.entries).index(external_reference) + 1
        total = sum((line.debit for line in draft.lines), ZERO)
        return ErpJournalEntry(f"JE-{number:06d}", external_reference, draft.entry_date, draft.transaction_type, total)
