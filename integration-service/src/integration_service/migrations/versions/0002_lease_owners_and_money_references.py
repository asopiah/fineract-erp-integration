"""Lease owners, optimistic locking on operations, one namespace for money references.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY_TYPES = sa.text("type IN ('DISBURSEMENT', 'REPAYMENT')")


def upgrade() -> None:
    op.add_column("operations", sa.Column("lease_owner", sa.String(length=36), nullable=True))
    op.add_column("operations", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.create_index(
        "uq_operations_money_reference", "operations", ["external_reference"], unique=True, postgresql_where=MONEY_TYPES
    )


def downgrade() -> None:
    op.drop_index("uq_operations_money_reference", table_name="operations", postgresql_where=MONEY_TYPES)
    op.drop_column("operations", "version")
    op.drop_column("operations", "lease_owner")
