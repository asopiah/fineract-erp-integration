"""The partner's commands: validated request bodies.

A command is stored on its operation, so an interrupted operation can be resumed from the database alone.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# References become path segments in Fineract lookups (``/external-id/{reference}``): URL-safe characters only.
ExternalReference = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
]
ClientUid = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
LoanReference = Annotated[str, StringConstraints(pattern=r"^LN-\d{4}-\d{6,}$")]
PersonName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
# RWF has no minor unit in Fineract's currency configuration: amounts are whole francs.
Money = Annotated[Decimal, Field(gt=0, max_digits=15, decimal_places=0)]


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    external_reference: ExternalReference = Field(description="Unique per business operation; reuse means replay.")


class CreateClient(Command):
    client_uid: ClientUid = Field(examples=["SME-000001"])
    first_name: PersonName
    last_name: PersonName
    activation_date: date


class ApplyForLoan(Command):
    client_uid: ClientUid
    principal: Money = Field(examples=["5000000"])
    annual_interest_rate: Annotated[Decimal, Field(ge=0, le=100, max_digits=7, decimal_places=4)] = Field(
        examples=["18"]
    )
    term_months: int = Field(ge=1, le=360, examples=[12])
    submitted_on: date
    expected_disbursement_on: date

    @model_validator(mode="after")
    def disbursement_not_before_submission(self) -> "ApplyForLoan":
        if self.expected_disbursement_on < self.submitted_on:
            raise ValueError("expected_disbursement_on cannot be before submitted_on")
        return self


class ApproveLoan(Command):
    approved_on: date


class DisburseLoan(Command):
    amount: Money
    transaction_date: date


class MakeRepayment(Command):
    amount: Money
    transaction_date: date
