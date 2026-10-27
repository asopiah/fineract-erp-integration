from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from integration_service.commands import ApplyForLoan, DisburseLoan
from integration_service.domain.fingerprint import fingerprint
from integration_service.domain.references import is_loan_reference, loan_reference


def test_loan_references_are_zero_padded_by_year() -> None:
    assert loan_reference(2026, 1) == "LN-2026-000001"
    assert loan_reference(2026, 1234567) == "LN-2026-1234567"
    assert is_loan_reference("LN-2026-000001")
    assert not is_loan_reference("LN-26-1")


def test_loan_reference_sequence_starts_at_one() -> None:
    with pytest.raises(ValueError, match="starts at 1"):
        loan_reference(2026, 0)


def disbursement(amount: str) -> DisburseLoan:
    return DisburseLoan(external_reference="DISB-1", amount=Decimal(amount), transaction_date=date(2026, 1, 15))


def test_fingerprint_ignores_how_an_amount_is_written() -> None:
    assert fingerprint(disbursement("5000000")) == fingerprint(disbursement("5000000.00"))


def test_fingerprint_changes_with_the_payload() -> None:
    assert fingerprint(disbursement("5000000")) != fingerprint(disbursement("5000001"))


@pytest.mark.parametrize("reference", ["DISB/1", "../loans", "has space", "", "x" * 101])
def test_references_must_be_url_safe(reference: str) -> None:
    with pytest.raises(ValidationError):
        DisburseLoan(external_reference=reference, amount=Decimal(1), transaction_date=date(2026, 1, 15))


def test_amounts_are_positive_whole_francs() -> None:
    with pytest.raises(ValidationError):
        disbursement("0")
    with pytest.raises(ValidationError):
        disbursement("100.50")


def test_disbursement_cannot_be_expected_before_submission() -> None:
    with pytest.raises(ValidationError, match="cannot be before"):
        ApplyForLoan(
            external_reference="APP-1",
            client_uid="SME-000001",
            principal=Decimal(5000000),
            annual_interest_rate=Decimal(18),
            term_months=12,
            submitted_on=date(2026, 1, 15),
            expected_disbursement_on=date(2026, 1, 14),
        )
