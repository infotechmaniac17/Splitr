"""add users.password_hash for real auth (register/login)

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-04 00:00:00.000000

Backend auth build-out: users can now register with a password (argon2id
hash, never the plaintext, never logged). Nullable because every M1-M4
test-fixture / seed user created via POST /users before this migration has
no password on file -- those accounts simply cannot log in via
POST /auth/login until a password is set for them (there is intentionally
no "set password for an existing passwordless user" endpoint in this pass;
out of scope -- flagged for a follow-up "claim account" flow).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("password_hash", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "password_hash")
