"""
PostgreSQL DDL strings for the append-only / financial-immutability guards.

Extracted here so both migration 0002 and the test conftest can reuse the
same strings without circular imports or numeric-prefixed module hackery.
"""

from __future__ import annotations

LEDGER_TRIGGER_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION guard_ledger_append_only()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'ledger_entries is append-only: DELETE is not permitted (id=%). '
            'Create a new adjustment entry instead.',
            OLD.id;
    ELSIF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION
            'ledger_entries is append-only: UPDATE is not permitted (id=%). '
            'Create a new adjustment entry instead.',
            OLD.id;
    END IF;
    RETURN NULL;
END;
$$;
"""

LEDGER_TRIGGER_DDL = """
CREATE TRIGGER trg_ledger_append_only
BEFORE UPDATE OR DELETE ON ledger_entries
FOR EACH ROW EXECUTE FUNCTION guard_ledger_append_only();
"""

EXPENSE_TRIGGER_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION guard_expense_financial_immutability()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.parse_status = 'confirmed' THEN
            RAISE EXCEPTION
                'Cannot DELETE a confirmed expense (id=%). '
                'Void it by setting status=''voided'' instead.',
                OLD.id;
        END IF;
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' THEN
        IF OLD.parse_status = 'confirmed' THEN
            IF (NEW.total_minor    IS DISTINCT FROM OLD.total_minor    OR
                NEW.subtotal_minor IS DISTINCT FROM OLD.subtotal_minor OR
                NEW.paid_by        IS DISTINCT FROM OLD.paid_by        OR
                NEW.currency       IS DISTINCT FROM OLD.currency       OR
                NEW.group_id       IS DISTINCT FROM OLD.group_id) THEN
                RAISE EXCEPTION
                    'Cannot mutate financial columns of confirmed expense (id=%). '
                    'Affected columns: total_minor, subtotal_minor, paid_by, '
                    'currency, group_id are immutable once confirmed.',
                    OLD.id;
            END IF;
        END IF;
        RETURN NEW;
    END IF;
    RETURN NULL;
END;
$$;
"""

EXPENSE_TRIGGER_DDL = """
CREATE TRIGGER trg_expense_immutability
BEFORE UPDATE OR DELETE ON expenses
FOR EACH ROW EXECUTE FUNCTION guard_expense_financial_immutability();
"""

ALL_TRIGGER_DDL: list[str] = [
    LEDGER_TRIGGER_FUNCTION_DDL,
    LEDGER_TRIGGER_DDL,
    EXPENSE_TRIGGER_FUNCTION_DDL,
    EXPENSE_TRIGGER_DDL,
]
