"""Initial schema: operations (idempotency + state machine), identity mappings, loan transactions, GL sync.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Issues the integration-level loan references (LN-<year>-<n>).
    op.execute(sa.schema.CreateSequence(sa.Sequence("loan_reference_seq")))
    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("client_uid", sa.String(length=64), nullable=False),
        sa.Column("external_reference", sa.String(length=100), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=False),
        sa.Column("last_name", sa.String(length=100), nullable=False),
        sa.Column("fineract_client_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clients")),
        sa.UniqueConstraint("client_uid", name=op.f("uq_clients_client_uid")),
        sa.UniqueConstraint("external_reference", name=op.f("uq_clients_external_reference")),
    )
    op.create_table(
        "gl_account_syncs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("external_reference", sa.String(length=100), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("erp_type", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.Enum("SYNC_PENDING", "SYNCED", "FAILED", name="syncstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fineract_gl_account_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gl_account_syncs")),
        sa.UniqueConstraint("code", name=op.f("uq_gl_account_syncs_code")),
        sa.UniqueConstraint("external_reference", name=op.f("uq_gl_account_syncs_external_reference")),
    )
    op.create_table(
        "operations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "CLIENT_CREATION",
                "LOAN_APPLICATION",
                "LOAN_APPROVAL",
                "DISBURSEMENT",
                "REPAYMENT",
                name="operationtype",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("external_reference", sa.String(length=100), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "RECEIVED",
                "FINERACT_PENDING",
                "FINERACT_DONE",
                "ERP_PENDING",
                "COMPLETED",
                "FAILED",
                "NEEDS_RECONCILIATION",
                name="operationstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("client_uid", sa.String(length=64), nullable=True),
        sa.Column("loan_reference", sa.String(length=32), nullable=True),
        sa.Column("correlation_id", sa.String(length=100), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operations")),
        sa.UniqueConstraint("type", "external_reference", name="uq_operations_type_external_reference"),
    )
    op.create_index(op.f("ix_operations_loan_reference"), "operations", ["loan_reference"], unique=False)
    op.create_index("ix_operations_status_next_attempt_at", "operations", ["status", "next_attempt_at"], unique=False)
    op.create_index(
        "uq_operations_one_active_per_loan",
        "operations",
        ["type", "loan_reference"],
        unique=True,
        postgresql_where=sa.text("type IN ('LOAN_APPROVAL', 'DISBURSEMENT') AND status <> 'FAILED'"),
    )
    op.create_table(
        "loans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("loan_reference", sa.String(length=32), nullable=False),
        sa.Column("external_reference", sa.String(length=100), nullable=False),
        sa.Column("client_uid", sa.String(length=64), nullable=False),
        sa.Column("principal", sa.Numeric(precision=19, scale=6), nullable=False),
        sa.Column("annual_interest_rate", sa.Numeric(precision=9, scale=4), nullable=False),
        sa.Column("term_months", sa.Integer(), nullable=False),
        sa.Column("submitted_on", sa.Date(), nullable=False),
        sa.Column("expected_disbursement_on", sa.Date(), nullable=False),
        sa.Column("fineract_loan_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_uid"], ["clients.client_uid"], name=op.f("fk_loans_client_uid_clients")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_loans")),
        sa.UniqueConstraint("external_reference", name=op.f("uq_loans_external_reference")),
        sa.UniqueConstraint("loan_reference", name=op.f("uq_loans_loan_reference")),
    )
    op.create_index(op.f("ix_loans_client_uid"), "loans", ["client_uid"], unique=False)
    op.create_table(
        "loan_transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("external_reference", sa.String(length=100), nullable=False),
        sa.Column("loan_reference", sa.String(length=32), nullable=False),
        sa.Column(
            "type",
            sa.Enum("DISBURSEMENT", "REPAYMENT", "OTHER", name="loantransactiontype", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=19, scale=6), nullable=False),
        sa.Column("principal_portion", sa.Numeric(precision=19, scale=6), nullable=False),
        sa.Column("interest_portion", sa.Numeric(precision=19, scale=6), nullable=False),
        sa.Column("fee_portion", sa.Numeric(precision=19, scale=6), nullable=False),
        sa.Column("penalty_portion", sa.Numeric(precision=19, scale=6), nullable=False),
        sa.Column("overpayment_portion", sa.Numeric(precision=19, scale=6), nullable=False),
        sa.Column("outstanding_after", sa.Numeric(precision=19, scale=6), nullable=False),
        sa.Column("fineract_transaction_id", sa.Integer(), nullable=False),
        sa.Column("erp_entry_number", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["loan_reference"], ["loans.loan_reference"], name=op.f("fk_loan_transactions_loan_reference_loans")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_loan_transactions")),
        sa.UniqueConstraint("external_reference", name=op.f("uq_loan_transactions_external_reference")),
    )
    op.create_index(op.f("ix_loan_transactions_loan_reference"), "loan_transactions", ["loan_reference"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_loan_transactions_loan_reference"), table_name="loan_transactions")
    op.drop_table("loan_transactions")
    op.drop_index(op.f("ix_loans_client_uid"), table_name="loans")
    op.drop_table("loans")
    op.drop_index(
        "uq_operations_one_active_per_loan",
        table_name="operations",
        postgresql_where=sa.text("type IN ('LOAN_APPROVAL', 'DISBURSEMENT') AND status <> 'FAILED'"),
    )
    op.drop_index("ix_operations_status_next_attempt_at", table_name="operations")
    op.drop_index(op.f("ix_operations_loan_reference"), table_name="operations")
    op.drop_table("operations")
    op.drop_table("gl_account_syncs")
    op.drop_table("clients")
    op.execute(sa.schema.DropSequence(sa.Sequence("loan_reference_seq")))
