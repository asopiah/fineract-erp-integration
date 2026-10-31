"""Repayment reversals: which reversal undid a repayment, and the ERP entry that reverses it.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("loan_transactions", sa.Column("reversed_by", sa.String(100), nullable=True))
    op.add_column("loan_transactions", sa.Column("reversal_entry_number", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("loan_transactions", "reversal_entry_number")
    op.drop_column("loan_transactions", "reversed_by")
