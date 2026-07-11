# Ghost Members — Design Decision

Status: **DESIGN ONLY.** No migration, no scaffolding, no code in this
change. Per the M6/M8 plan (item 2), this document exists so item 5
(compute_shares extension) and item 6 (implementation) build against a
decided identity model instead of churning later.

**Revision note:** this is a revision of the original draft, in place,
after a five-point verification by the reviewing coordinator found gaps
in points 1–3 (each required a stop) and partial gaps in points 4–5.
Sections 1–3 and 4.1–4.3/4.5 (architecture, options, target schema core,
migration strategy, compute_shares impact) were confirmed sound and
stand as originally written except where a gap explicitly required a
change to them (§4.1's CHECK constraint, §4.3's snapshot coverage).
Section 7 is new: a self-verification of this revision against the same
five points, done the same way the original draft was verified.

## 1. Problem

`item_assignments`, `group_members`, `ledger_entries`, and `settlements`
all hard-FK to `users.id`. Every person who can be assigned an item or
accrue a balance must already have a `users` row *and*, implicitly, the
ability to eventually log in. Product need: let a group admin add a
"ghost member" — e.g. a flatmate who hasn't signed up yet — assign them
items, have them accrue real ledger balances, and later let that person
**claim** their identity on signup, with all historical assignments,
ledger entries, and settlements preserved and correctly attributed.

## 2. Current schema (read directly from `app/domain/models.py`)

```
users             id PK, name, email UNIQUE NOT NULL, password_hash NULL,
                  phone, avatar_url, default_currency, created_at

groups            id PK, name, created_by FK users, simplify_debts, created_at

group_members     PK (group_id, user_id)   <-- COMPOSITE PK, NO surrogate id
                  group_id FK groups, user_id FK users, role, joined_at, left_at

subgroups         id PK, group_id FK groups, name
subgroup_members  PK (subgroup_id, user_id) <-- also composite, also FK users

expenses          id PK, group_id FK groups NULL, paid_by FK users, ...
                  -- group_id IS NULL ⇒ "personal" expense (no group at all)

expense_line_items id PK, expense_id FK expenses, ...

item_assignments  id PK, line_item_id FK expense_line_items,
                  user_id FK users, weight, share_minor
                  UNIQUE (line_item_id, user_id)

ledger_entries    id PK, group_id FK groups NULL, expense_id FK NULL,
                  settlement_id FK NULL,
                  debtor_id FK users, creditor_id FK users,
                  amount_minor CHECK > 0, entry_type, created_at
                  -- APPEND-ONLY: DB trigger + ORM guard forbid UPDATE/DELETE

settlements       id PK, group_id FK groups NULL,
                  payer_id FK users, payee_id FK users, amount_minor, ...
```

Three facts from reading the actual code (not assumed) that materially
change the recommendation below:

1. **`group_id` is nullable on `expenses`, `ledger_entries`, and
   `settlements`** — "personal" mode, exercised by
   `test_m1_no_group_id_skips_membership_check`
   (`tests/test_hardening.py`). There is no `group_members` row at all in
   this mode; `debtor_id`/`creditor_id`/`payer_id`/`payee_id` still resolve
   straight to `users.id`.
2. **`compute_user_net_balance` (`app/domain/ledger.py:307-330`) aggregates
   a user's net balance GLOBALLY, across every group and every personal
   expense, by filtering `ledger_entries` directly on a single `user_id`**
   — there is no group-scoping in that query at all.
3. **`compute_shares` / `LineInput.assignments`
   (`app/domain/splitting.py`) is already identity-opaque**: it takes
   `tuple[tuple[uuid.UUID, Fraction], ...]` and does not care what table
   that UUID is a primary key of. The splitting engine itself needs zero
   changes under any of the options below — only the *adapter*
   (`lines_from_orm`) that produces those UUIDs is in scope.
4. **No FK in this schema specifies `ondelete`** (grepped every
   migration under `alembic/versions/` and every `sa.ForeignKey(...)` call
   in `app/domain/models.py` — none pass `ondelete=`). Postgres's default
   FK action is `NO ACTION`, which behaves like a hard `RESTRICT` for our
   purposes: `DELETE FROM users WHERE id = ...` fails outright with a
   foreign-key-violation error if *any* row in `item_assignments`,
   `ledger_entries`, `settlements`, or `group_members` references that
   user. This is relevant to §4.8 (ghost lifecycle) below — read that
   section for why this means there is **no** pre-existing cascade-delete
   hazard to flag, independent of ghost members.

Facts 1 and 2 are the crux of the recommendation: any identity anchor that
is *scoped to a group* (i.e. lives in `group_members`) cannot be the sole
key ledger/settlement rows point to, because a meaningful fraction of
ledger rows have no group at all, and the one global cross-group balance
query that already exists in production code assumes a single,
group-independent identity per person.

## 3. Options evaluated

### Option A — `group_members` becomes the identity anchor

`group_members(id PK, group_id, user_id NULLABLE, display_name, claimed_at NULLABLE)`;
`item_assignments`, `ledger_entries`, `settlements` FK to `group_members.id`
instead of `users.id`.

**Rejected.** Reasons, in order of severity:

- **Breaks personal (groupless) expenses outright.** `group_id` is
  nullable specifically so `ledger_entries`/`settlements` can exist with
  no group at all. There is no `group_members` row to anchor to in that
  case, so `debtor_id`/`creditor_id` would need a *second*, parallel
  identity path anyway (e.g. "FK group_members.id OR FK users.id,
  exactly one populated") — this reintroduces exactly the
  two-identity-types complexity Option A was supposed to avoid, just one
  layer deeper.
- **Breaks `compute_user_net_balance`'s global aggregation.** A single
  real person who is in two groups has two different `group_members.id`
  values. The existing "net balance across all groups" query would need
  to first resolve every `group_members.id` back to a `user_id` and
  re-aggregate — for every group the user is or ever was a member of —
  before it could group by the *real* person again. This is strictly more
  work than Option A was meant to save, and it means every future feature
  that wants "this person's balance" (not "this membership row's
  balance") pays a join-and-refold tax forever.
- **`group_members` currently has no surrogate `id` at all** — it's a
  pure composite-PK association row (`PrimaryKeyConstraint("group_id",
  "user_id")`, `app/domain/models.py:171-195`). Making it an identity
  anchor means first adding a surrogate key to an association table
  that was never designed to be referenced by three other tables, on top
  of the FK rewrite itself.
- **Churns already-shipped M1/M2 code for no architectural gain.**
  `compute_shares`, the ledger posting functions, and every API response
  shape that currently returns `{user_id: share_minor}` would need to
  become member-ID-aware (see §4.5) — a large, wide-reaching change to
  working code, to solve a problem (letting an un-registered person
  accrue a balance) that doesn't actually require moving the identity
  anchor at all (see Option B).

### Option B — Placeholder ghost users (`users.is_ghost` flag), FKs unchanged

A ghost is a normal `users` row: `is_ghost BOOLEAN NOT NULL DEFAULT false`,
`password_hash` stays `NULL` (already nullable and already means
"cannot log in" per migration `0005`'s own docstring), `claimed_at
TIMESTAMPTZ NULL`. **No FK on any other table changes.**

This is the recommended option — see §4 for the full design. (Note,
added in this revision: Option B is *not* free of the "join-and-refold
tax" criticism leveled at Option A above — it pays a bounded version of
the same tax, deliberately, only for the rare explicitly-merged
population. See §4.7's new "acknowledging the trade-off" note for the
honest accounting.)

### Option C — Polymorphic assignee (`member_type` + `member_id`)

**Rejected**, without a prototype, for reasons independent of A vs. B:

- Postgres cannot enforce a polymorphic FK. `member_type IN ('user',
  'ghost_member')` + `member_id UUID` is only checkable with a trigger
  (yet another bespoke guard alongside the ones already added for the
  confirm-immutability/state-machine work) or, worse, not checked at the
  DB level at all — a silent invariant the append-only ledger explicitly
  tries to avoid elsewhere (every other FK in this schema is a real,
  DB-enforced FK; this project's own CLAUDE.md invariants lean hard on
  DB-level enforcement over app-level trust).
- Every join that currently does `JOIN users ON ledger_entries.debtor_id
  = users.id` becomes a conditional join keyed on `member_type` —
  `compute_group_balances`, `compute_user_net_balance`, every "who does
  X user owe" query, every API serializer that resolves a display name.
  This is strictly more surface area than Option B's "it's just a
  `users` row with a flag," for the same end capability.
- It doesn't solve anything Option B doesn't: a "ghost_member" row under
  Option C still needs its own claim/merge story identical to §4.4/§4.6
  below. Polymorphism buys type-safety at the type-system level in some
  stacks; in a schema-first Postgres app enforced by triggers and CHECK
  constraints, it buys nothing but a second code path everywhere.

## 4. Recommended design: Option B

### 4.1 Target schema

```sql
-- Additive only. No existing column, FK, or row changes meaning.
ALTER TABLE users ADD COLUMN is_ghost   BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN claimed_at TIMESTAMPTZ NULL;

-- CHECK: a claimed timestamp only makes sense for a (formerly) ghost row,
-- and a non-ghost row was never "claimed" (it was just created real).
ALTER TABLE users ADD CONSTRAINT ck_claimed_at_only_after_ghost
  CHECK (claimed_at IS NULL OR is_ghost = false);
  -- i.e. once claimed_at is set, is_ghost must already be false (claim
  -- flips both in the same UPDATE — see §4.4).

-- NEW in this revision: a ghost row is now PHYSICALLY incapable of
-- holding credentials, enforced at the DB level, not just by app-layer
-- convention. The original draft only had an app-layer login guard
-- (still present below, but now the SECOND layer, not the primary one).
ALTER TABLE users ADD CONSTRAINT ck_ghost_no_credentials
  CHECK (NOT is_ghost OR password_hash IS NULL);
  -- Reads as: "if is_ghost, then password_hash must be NULL." A ghost row
  -- cannot simultaneously be is_ghost=true and have a non-null
  -- password_hash, full stop, no matter which code path tried to set it.
  -- The claim flow's single atomic UPDATE (password_hash = <hash>,
  -- is_ghost = false, ... in the SAME statement, §4.4) always satisfies
  -- this: the row transitions from (is_ghost=true, password_hash=NULL) to
  -- (is_ghost=false, password_hash=<hash>) in one indivisible write, never
  -- passing through a state the CHECK would reject. Any attempt to set
  -- password_hash on a row while leaving is_ghost=true is rejected by
  -- Postgres itself before the app-layer login guard (§4.1a) is ever
  -- relevant.

-- New: records an EXPLICIT merge of one identity into another, for the
-- rare case where a ghost cannot be auto-activated in place (§4.4 Case 2).
-- This table is the ONLY place identity linkage lives; it never causes a
-- write to item_assignments / ledger_entries / settlements.
CREATE TABLE user_merges (
    id            UUID PRIMARY KEY,
    ghost_user_id UUID NOT NULL UNIQUE REFERENCES users(id),
    real_user_id  UUID NOT NULL REFERENCES users(id),
    merged_by     UUID NOT NULL REFERENCES users(id),  -- who confirmed the merge
    merged_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (ghost_user_id <> real_user_id)
);
CREATE INDEX ix_user_merges_real_user_id ON user_merges(real_user_id);

-- New (§4.4 Case 1): staging area for a claim-in-progress. Holds
-- SUBMITTED credentials for a not-yet-verified/not-yet-consented claim of
-- a ghost row -- deliberately NEVER the ghost's live password_hash (which
-- stays NULL, enforced by ck_ghost_no_credentials above, until the claim
-- is actually accepted, §4.4c). Exactly one pending claim per ghost.
CREATE TABLE ghost_claim_requests (
    id                       UUID PRIMARY KEY,
    ghost_user_id            UUID NOT NULL UNIQUE REFERENCES users(id),
    submitted_password_hash  TEXT NOT NULL,
    submitted_name           TEXT NULL,
    verification_token_hash  TEXT NOT NULL,
    expires_at               TIMESTAMPTZ NOT NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- NEW in this revision (§4.7c): block any NEW row in the four tables
-- below from referencing a user_id that has already been merged away.
-- Reuses the SAME generic-trigger-function convention established in
-- M6 item 1 (app/domain/pg_guards.py's parameterized
-- reject_mutation_if_expense_confirmed pattern) rather than inventing a
-- new one: one function, parameterized via trigger arguments for which
-- column to check, attached per table (and per column, for settlements'
-- two user-facing columns).
CREATE OR REPLACE FUNCTION reject_reference_to_merged_ghost()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    fk_column text := TG_ARGV[0];
    fk_value  uuid := (to_jsonb(NEW) ->> fk_column)::uuid;
BEGIN
    IF fk_value IS NOT NULL AND EXISTS (
        SELECT 1 FROM user_merges WHERE ghost_user_id = fk_value
    ) THEN
        RAISE EXCEPTION
            'Cannot reference merged-away ghost user (id=%) via %.%: '
            'this identity was merged into a real account (see '
            'user_merges) -- use the real user id instead.',
            fk_value, TG_TABLE_NAME, fk_column;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_item_assignments_reject_merged_ghost
BEFORE INSERT OR UPDATE ON item_assignments
FOR EACH ROW EXECUTE FUNCTION reject_reference_to_merged_ghost('user_id');

CREATE TRIGGER trg_expenses_reject_merged_ghost_payer
BEFORE INSERT OR UPDATE ON expenses
FOR EACH ROW EXECUTE FUNCTION reject_reference_to_merged_ghost('paid_by');

CREATE TRIGGER trg_settlements_reject_merged_ghost_payer
BEFORE INSERT OR UPDATE ON settlements
FOR EACH ROW EXECUTE FUNCTION reject_reference_to_merged_ghost('payer_id');

CREATE TRIGGER trg_settlements_reject_merged_ghost_payee
BEFORE INSERT OR UPDATE ON settlements
FOR EACH ROW EXECUTE FUNCTION reject_reference_to_merged_ghost('payee_id');

CREATE TRIGGER trg_group_members_reject_merged_ghost
BEFORE INSERT OR UPDATE ON group_members
FOR EACH ROW EXECUTE FUNCTION reject_reference_to_merged_ghost('user_id');

-- ledger_entries deliberately NOT triggered here for INSERT-blocking on
-- debtor_id/creditor_id: by the time a merge exists, all NEW ledger
-- postings originate from post_expense_to_ledger/post_settlement_to_ledger
-- reading shares that were already computed from item_assignments/
-- settlements -- which are themselves already guarded above. Attaching a
-- redundant trigger directly to ledger_entries is not wrong, but is not
-- required to close the gap 3c asks for; flagged here for item 6 to
-- decide whether defense-in-depth on ledger_entries itself is worth the
-- extra trigger, rather than silently omitting the reasoning.
```

`item_assignments`, `group_members`, `ledger_entries`, `settlements`:
**zero column or FK changes.** They continue to reference `users.id`
exactly as today, whether that row is a ghost or not.

A ghost is created by **a group admin only** (see §4.8 — this revision
restricts ghost-creation from "any group member" to the group's admin
role) via (future, item 6) `POST /groups/{id}/ghost-members` — server-side,
this is just `INSERT INTO users (email, name, is_ghost) VALUES (...)`
followed by the existing `POST /groups/{id}/members`-equivalent insert
into `group_members` — no new membership mechanism, no new assignment
mechanism. `item_assignments.user_id` for a ghost is the ghost's
`users.id`, indistinguishable at the schema level from any other
assignment.

**Email handling for a ghost with no known email yet:** `users.email` is
`NOT NULL UNIQUE` today (`app/domain/models.py:124`) and this document
does not propose relaxing that constraint (would ripple into
login/lookup code far outside this item's scope). A ghost created
without a known real email gets a synthetic placeholder in a reserved,
un-registrable domain, e.g. `ghost+<uuid>@ghosts.splitr.invalid` — this
keeps the existing constraint intact and is trivially distinguishable in
the UI (never surfaced, `is_ghost=true` rows never show their email
unless it's a real invite address). The SAME placeholder-swap mechanism
is reused by the decline path in §4.4c.

#### 4.1a Credential-adjacent paths — enumerated

The `ck_ghost_no_credentials` CHECK constraint above is the PRIMARY
control. Every credential-adjacent code path is enumerated here so none
of them can quietly reintroduce a ghost-can-authenticate bug via a path
that doesn't literally try to write `password_hash` (which the CHECK
alone wouldn't catch):

- **`POST /auth/register` (`app/api/auth.py:58-84`).** Must never set
  `password_hash` on a ghost row directly outside the claim flow — see
  §4.4's full rewrite. Enforced by the CHECK regardless of what the route
  code does.
- **`POST /auth/login` (`app/api/auth.py:87-115`).** Already requires
  `user.password_hash` to be truthy (a ghost's is always `NULL`, enforced
  now at the DB level), and already returns the *identical* 401 for "no
  such user," "wrong password," and "user has no password set" — a ghost
  attempting to log in gets exactly this same generic response, with no
  behavior change needed. This is unaffected by ghosts existing; it was
  already correct.
- **Password-reset.** **Does not exist in this codebase today** — grepped
  `app/api/` and `app/domain/auth.py`; there is no reset-request or
  reset-confirm endpoint at all currently. Ruled here for whenever it is
  built: a reset-request for a ghost's email **must return the identical
  response as a nonexistent email** (never "this account has no password
  yet, did you mean to claim it?"), or the response shape itself becomes
  a ghost-enumeration oracle — an attacker could probe a wordlist of
  emails against the reset endpoint to discover which ones are pre-bound
  as ghosts in *some* group, without ever knowing which group or being a
  member of it. Any future reset-token issuance is also independently
  blocked from ever writing credentials to a ghost by the CHECK
  constraint.
- **Future OAuth / social login.** Same rule: an OAuth callback whose
  verified email matches a ghost must go through the exact same
  verification-then-consent claim flow in §4.4, never silently link and
  grant login capability just because the provider itself already
  verified the email — Splitr's own claim consent step is still required
  (a provider verifying the email proves mailbox control, which §4.4
  needs anyway, but does not substitute for the explicit "claim or
  decline" prompt, which is about consent to inherit a specific balance,
  not about email ownership).
- **Future magic-link login.** Same rule again: a magic link grants a
  session without a password, so it is exactly as credential-adjacent as
  a password — must not be issuable for `is_ghost = true` rows outside
  the claim flow, for the same enumeration reasons as password-reset.
- **General rule, stated once, referenced by all of the above:** any code
  path that would let a ghost row authenticate must, at the moment it
  does so, either (a) *be* the claim flow itself (flips `is_ghost` to
  `false` in the same statement that grants credentials, satisfying the
  CHECK atomically), or (b) treat `is_ghost = true` as indistinguishable
  from "no such account" if it's a lookup/response-shaping path that
  never writes credentials at all (reset-request, "does this email
  exist" checks, etc.).

### 4.2 Migration strategy from current state

Because no existing table's FK meaning changes, the migration is
**purely additive**:

1. `ALTER TABLE users ADD COLUMN is_ghost ... DEFAULT false` — every
   existing row is a real, non-ghost user; the default makes this a
   metadata-only change (Postgres 11+ handles a non-volatile-default
   `ADD COLUMN` as a fast, in-place catalog change, no table rewrite).
2. `ALTER TABLE users ADD COLUMN claimed_at ... NULL` — same, trivial.
3. `ALTER TABLE users ADD CONSTRAINT ck_ghost_no_credentials ...` — a
   `CHECK` added to an existing table requires Postgres to validate it
   against all existing rows unless `NOT VALID` is used; either way this
   is cheap here because **every existing row has `is_ghost = false`**
   (just set by step 1's default), so `NOT is_ghost OR password_hash IS
   NULL` reduces to `true OR ...` = `true` for every single pre-existing
   row — the constraint is trivially satisfied by construction, not
   something that needs a data-cleanup pass first.
4. `CREATE TABLE user_merges (...)` — new, empty table.
5. `CREATE TABLE ghost_claim_requests (...)` — new, empty table (§4.4
   Case 1's claim-in-progress staging area).
6. `CREATE FUNCTION reject_reference_to_merged_ghost` + the five
   `CREATE TRIGGER` statements (§4.1) — all reference `user_merges`,
   which is empty at migration time, so every trigger is a guaranteed
   no-op against all existing data until the first real merge happens.

**No `item_assignments`, `ledger_entries`, or `settlements` row is
touched, updated, or reassigned by this migration.** There is no "existing
assignments/ledger rows" data migration to write, because nothing about
what those rows point to changes.

### 4.3 Balance-equivalence proof strategy (concrete, testable)

Precisely because this option is additive-only, the proof is a
**regression test**, not a transformation proof — but the plan explicitly
asked for the literal procedure, so here it is, written so it is exactly
as rigorous whether or not that intuition is later found wrong (e.g. if a
future change to this design turns out to require touching existing
rows after all):

```python
# tests/test_ghost_members_migration_balance_equivalence.py (future, item 6)

async def snapshot_all_balances(db: AsyncSession) -> dict:
    """
    Canonical, order-independent snapshot of every balance-bearing
    aggregate in the system, to the paisa.

    Covers all three read-model layers: raw pairwise net balances,
    per-user global net balances, AND (added in this revision) each
    group's simplified-debts suggestion -- simplification is a pure,
    deterministic function of the pairwise balances
    (app/domain/settlement_simplification.py: simplify_group_debts), so
    it must reconcile identically too; omitting it left a read-model gap
    in the original draft.
    """
    all_group_ids = (await db.execute(select(Group.id))).scalars().all()
    all_user_ids = (await db.execute(select(User.id))).scalars().all()

    group_balances = {
        gid: await compute_group_balances(db, gid) for gid in all_group_ids
    }

    return {
        "group_balances": {
            str(gid): sorted(balances)
            for gid, balances in group_balances.items()
        },
        "group_simplified_debts": {
            # simplify_group_debts is deterministic given the same input
            # list, but its greedy heap-based tie-breaking can still order
            # equal-priority suggested transactions differently across
            # runs with equal inputs in a different insertion order --
            # sort the OUTPUT too so the comparison is order-independent,
            # not just relying on determinism of the algorithm itself.
            str(gid): sorted(simplify_group_debts(balances))
            for gid, balances in group_balances.items()
        },
        "user_net_balances": {
            str(uid): await compute_user_net_balance(db, uid)
            for uid in all_user_ids
        },
        # Paisa-exact total-money-conserved check: every expense_share entry
        # must still net to zero across all debtor/creditor pairs combined.
        "grand_total_ledger_amount": (
            await db.scalar(select(func.sum(LedgerEntry.amount_minor)))
        ),
    }

async def test_ghost_members_migration_preserves_all_balances(pg_engine):
    # 1. Seed a realistic, randomized dataset PRE-migration: N groups,
    #    M users, random expenses (manual + item-level), random refunds,
    #    random settlements -- reuse the existing property-test generators
    #    already used for compute_shares (tests/test_splitting.py) so this
    #    isn't a hand-picked happy path.
    seed_random_dataset(pg_engine, groups=10, users=40, expenses=200)

    before = await snapshot_all_balances(session)

    # 2. Run ONLY this migration (alembic upgrade <this-revision>).
    run_migration("000X_ghost_members_schema")

    # 3. Re-snapshot with the identical queries.
    after = await snapshot_all_balances(session)

    # 4. Byte-for-byte / paisa-exact equality. Not "approximately equal",
    #    not "same shape" -- literally the same dict.
    assert before == after
```

If a later revision of this design ever needs an actual identity
*re-key* (e.g. some future requirement forces touching existing FK
values), the same procedure applies unchanged except step 4 becomes
"assert equality **after** resolving each row's identity through
whatever mapping the migration defines" (i.e. compare
`resolve(before_id) == resolve(after_id)` pairs) — the snapshot-diff
methodology doesn't change, only whether an identity-resolution function
is inserted between snapshot and compare. Recording this here so item 6
doesn't have to redesign the proof strategy if the recommendation is ever
revisited.

### 4.4 Claim flow (revised — email match alone NEVER activates a ghost)

**What Splitr's auth actually does today (read directly from
`app/api/auth.py`, not assumed):** `POST /auth/register` performs **no
email verification whatsoever**. It inserts the `User` row and
immediately returns a live access/refresh token pair in the same
request/response (`_issue_token_pair`, `app/api/auth.py:46-52,84`).
There is no verification-token concept, no `email_verified` column, no
confirmation email, anywhere in the current codebase. This is a genuine
gap relative to what the claim flow needs, not something already solved
elsewhere: **this design introduces verification as a new, additive
concept, scoped specifically to the ghost-claim path** — ordinary
registration for an email that does *not* match any ghost is **left
exactly as it is today** (still no verification). Broadening verification
to all signups universally is a separate, larger product decision not
in scope here and not assumed to be wanted.

**Case 1 — the common case: ghost was invited with a real, known email.**

Because a ghost already holds that email under the `UNIQUE` constraint,
a "normal" `INSERT` for a second `users` row with the same email is
impossible — so the flow cannot simply "create a real user like any
other registration." Instead:

a. `POST /auth/register` with an email matching an `is_ghost = true` row
   does **not** touch the ghost row's live `password_hash`/`is_ghost` at
   all (doing so would be exactly the unconditional auto-claim this
   revision removes, and would also violate `ck_ghost_no_credentials` if
   attempted without flipping `is_ghost` in the same statement). Instead
   it creates a **staging row** — a new, additive table,
   `ghost_claim_requests(id, ghost_user_id FK users, submitted_password_hash,
   submitted_name, verification_token_hash, expires_at, created_at)` —
   holding the submitted credentials and a single-use, time-limited,
   hashed verification token. **No token pair is issued at this point.**
   The response tells the caller to check their email; this alone is the
   behavior change that kills unconditional auto-claim, since no session
   exists until verification succeeds.
b. The person clicks the verification link (proves mailbox control —
   how the token itself is generated/compared is an implementation
   detail for item 6, not re-derived here beyond "single-use, hashed,
   short-lived, proves the clicker controls this exact mailbox").
   Verification success does **not** claim anything yet either. It
   unlocks an explicit, human-readable prompt:

   > "You've been added to **{group_name}** by **{inviter_display_name}**
   > with a pending balance of **₹{Y}**. Claim this identity, or decline
   > and create a separate account with this email instead."

   Claim is only ever executed by an explicit, logged, consensual click
   on this exact prompt — never automatically, never as a side effect of
   verification alone. The `claimed_at` timestamp (already in §4.1's
   schema) is the durable record that this consensual step occurred, as
   distinct from a row that was simply born non-ghost; this is a
   metadata event on the `users` row, not a financial one, so no ledger
   entry is needed to record it.
c. **Claim accepted:** exactly the single atomic `UPDATE` from the
   original draft — `password_hash = <staged hash>, name =
   COALESCE(<staged name>, name), is_ghost = false, claimed_at = now()
   WHERE id = :ghost_id` (satisfies `ck_ghost_no_credentials` atomically,
   §4.1) — plus deleting the now-resolved `ghost_claim_requests` row, plus
   issuing the token pair for the first time. Because
   `item_assignments.user_id`, `ledger_entries.debtor_id`/`creditor_id`,
   `settlements.payer_id`/`payee_id`, and `group_members.user_id` were
   always this exact same `users.id` value, **every historical row is
   already correctly attributed — zero rows outside `users` (and the
   now-deleted staging row) are touched.**
d. **Claim declined:** the ghost's `email` is swapped to a synthetic
   placeholder (the exact same `ghost+<uuid>@ghosts.splitr.invalid`
   mechanism as §4.1's "no known email yet" case), freeing the real
   address. The ghost row **remains in the group, unclaimed**,
   `is_ghost` stays `true`, and every historical row referencing it is
   completely unaffected (this is a metadata-only email swap, not a
   financial mutation). A **brand-new, independent `users` row** is then
   created using the real email + the staged credentials/name from
   `ghost_claim_requests` (this is, in effect, the ordinary registration
   the person originally asked for, now unblocked because the email is
   free) and logged in normally. The staging row is deleted (or marked
   `declined` for audit, item 6's call). **The group admin who created
   the ghost is notified the invite was declined** (email/in-app,
   mechanism TBD in item 6) so they know to reconcile the ghost's
   outstanding balance with the real person out-of-band (ask them to
   actually join for real, or settle up manually) — the ghost's balance
   itself is not touched by a decline.

**Case 2 — the person already has (or separately creates) a distinct,
different `users.id`** (e.g. the ghost was created with a placeholder
email because the inviter didn't know it, and the real person signs up
under their actual email — unrelated to any ghost — or already had an
account from before joining this group). Two distinct `users` rows now
exist for one real person, and historical `item_assignments`/
`ledger_entries`/`settlements` rows point at the **ghost's** `users.id`.

This is where the hard constraint bites: **the append-only ledger must
never be rewritten.** The design does not touch `ledger_entries.debtor_id`
/`creditor_id` (or any other historical FK) at all. Instead:

1. The real user (or a group admin, with the real user's confirmation —
   exact UX is item 6's call, not this doc's) explicitly claims the ghost:
   `POST /users/me/claim-ghost {ghost_user_id}`. This is also a
   consensual, logged action — the real user must be authenticated
   (already has credentials, unlike Case 1) and must explicitly initiate
   this; there is no automatic matching by name/email similarity.
2. This inserts exactly one row into `user_merges`
   `(ghost_user_id, real_user_id, merged_by, merged_at)`. **No other
   table is written.**
3. From that point on, every balance/read query resolves identity through
   `user_merges` before aggregating — e.g.
   `compute_group_balances`/`compute_user_net_balance` group by
   `COALESCE(user_merges.real_user_id, ledger_entries.debtor_id)` (a
   `LEFT JOIN user_merges ON user_merges.ghost_user_id =
   ledger_entries.debtor_id`, same on the creditor side) instead of raw
   `debtor_id`/`creditor_id`. The historical rows still literally say
   "the ghost owed/was owed this" — the *display and aggregation* layer
   is what folds that into the real person's balance going forward. This
   is the same pattern the append-only ledger already uses for
   corrections (new rows change the picture; old rows are never edited),
   just applied to identity instead of amount. See §4.7 for the
   trade-off this join costs, stated honestly.
4. New activity going forward (new item assignments, new expenses) uses
   `real_user_id` directly — and, as of this revision, is DB-enforced:
   §4.1's `reject_reference_to_merged_ghost` triggers make it impossible
   to INSERT (or UPDATE into) a new `item_assignments`/`expenses.paid_by`/
   `settlements.payer_id`/`settlements.payee_id`/`group_members.user_id`
   row referencing the merged-away ghost id at all, going forward. The
   group's member list should show the real, claimed identity from that
   point on — `group_members.user_id` for that person's row can be
   updated to `real_user_id` (`group_members` is a *membership* record,
   not a financial ledger, so it is not subject to the append-only
   constraint the way `ledger_entries` is; this one `UPDATE` is safe).

#### 4.4d Abuse analysis

**Attack scenario:** a malicious (or merely careless) group admin
creates a ghost using a victim's real email — without the victim's
knowledge or consent — and assigns the victim item shares designed to
look like a real debt (e.g. to socially pressure them into paying, or as
outright harassment). The concern: does the victim's *unrelated* act of
signing up for Splitr later (for a totally different reason) somehow
inherit this fabricated debt?

**Why §4.4b+c neutralizes the core risk:**

- **Nothing happens silently.** Verification (proving mailbox control)
  and the explicit claim-or-decline prompt are both required before any
  linkage occurs. An attacker cannot force a claim on the victim's
  behalf; the victim must affirmatively click "claim."
- **Full context is shown before any decision is asked for.** The prompt
  names the specific group, the specific inviter, and the exact pending
  balance — a victim with no relationship to that group or person can
  immediately recognize "this isn't me" and decline at zero cost: the
  ghost keeps its (now-placeholder) email, the victim's real account is
  created fresh and totally independent, and no financial row is ever
  attributed to the victim's real `users.id`.
- **Declining is free and total.** Unlike the original (unconditional
  auto-claim) draft, there is no window in which the fabricated debt is
  ever attached to the victim's real identity, even transiently.

**Residual risk (stated honestly, not claimed as zero):**

- **Social engineering is not eliminated, only converted to a
  user-decision surface.** The claim prompt shows exactly what the
  ghost's creator supplied — group name, inviter display name, claimed
  balance — none of which Splitr can independently verify as truthful.
  A sufficiently convincing fabricated context (a group named to sound
  like something the victim trusts, an inviter name that mimics someone
  they know) could still social-engineer a victim into clicking "claim"
  for a group they don't actually recognize as suspicious. This is a
  real, non-zero residual risk. What this design *does* achieve is
  converting an automatic, silent identity-linking bug into a
  well-defined, informed, user-initiated decision — a strictly smaller
  and better-understood risk category than the original draft's
  unconditional auto-claim, but not literally zero risk.
- **A secondary, smaller residual risk: email-liveness disclosure.** The
  arrival of a verification/claim-prompt email at all (regardless of
  whether the recipient ultimately declines) confirms to whoever
  controls the inviting group that this email address is real and
  monitored — an oracle in the same family as the password-reset
  enumeration risk flagged in §4.1a, just reached via invite-then-claim
  instead of reset-request. Possible mitigations (documented here for
  item 6 to size, not designed in full in this doc): rate-limit
  ghost-creation per admin account; consider not immediately revealing to
  the *ghost's creator* whether their invite email was ever opened/
  verified. This risk is not resolved by this document; it is flagged
  honestly as unresolved.

### 4.5 Impact on `compute_shares` inputs

**None.** `LineInput.assignments: tuple[tuple[uuid.UUID, Fraction], ...]`
(`app/domain/splitting.py:92`) already receives opaque UUIDs; a ghost's
`users.id` flows through `lines_from_orm` exactly like a real user's,
because it *is* a `users.id`. `compute_shares`, the largest-remainder
rounding, and the `SplitResult.shares: dict[uuid.UUID, int]` return type
are all untouched. This is the single biggest practical argument for
Option B: the M2 splitting engine, already shipped and tested with
randomized property tests, needs zero changes.

(Contrast: under Option A, `LineInput.assignments` would need to carry
`group_members.id` values, and every call site that currently treats a
"user_id" as globally meaningful — API response bodies, the ledger poster,
balance computation — would need an explicit `group_members.id ->
users.id` resolution step before it could show a name, check "is this the
payer", or fold cross-group balances. That churn is the concrete cost
Option A avoids nothing by paying.)

### 4.6 Impact on auth dependencies

The actual function names in this codebase (there is no literal
`require_group_member`; the equivalent logic is `_assert_actor_authorized_for_expense`
and `_assert_active_group_members` in `app/api/expenses.py`, both reading
`GroupMember.user_id`/`.left_at`):

- **`get_current_user` (`app/api/deps.py:29`) is completely unaffected.**
  It resolves a JWT to a `users` row. A ghost, by definition, never has
  credentials (`password_hash IS NULL`, enforced at the DB level as of
  this revision — §4.1's `ck_ghost_no_credentials` — not just by
  convention) and therefore can never successfully authenticate as
  itself — this dependency doesn't need to know ghosts exist at all.
- **`_assert_active_group_members` / `_assert_actor_authorized_for_expense`
  are unaffected in shape.** They already just check "is this `user_id`
  an active row in `group_members` for this group" — a ghost's `user_id`
  passes that check exactly like a real member's, which is precisely the
  desired behavior (a ghost is a full group member for assignment
  purposes; it simply can't be the *authenticated caller* of any request).
- **A NEW admin-only check is needed for ghost creation specifically**
  (item 6, not this doc, but the rule is decided here per §4.8): the
  future `POST /groups/{id}/ghost-members` endpoint must check the
  calling user's `GroupMemberRole` is `admin` for that group (the nearest
  existing analog to "owner" — see §4.8 for why there is no separate
  "owner" role in the current schema), using the exact same
  `group_members` row lookup `_assert_active_group_members` already does,
  just with an added role check. This is additive to the existing
  authorization pattern, not a new one.
- **One additive guard remains from the original draft, now demoted to
  SECOND layer, not primary:** `POST /auth/login` (and anywhere else a
  token gets minted) should still explicitly reject `is_ghost = true`
  rows as defense in depth, even though `ck_ghost_no_credentials` now
  makes the scenario it guards against (a ghost somehow holding a
  password hash) physically impossible at the DB level. Belt-and-
  suspenders, matching this session's own established pattern of not
  trusting a single layer alone (the confirm-immutability guard work).

### 4.7 Uniqueness / merge edge cases

- **Ghost with the same email as an existing real user.** Structurally
  prevented by the existing `users.email UNIQUE NOT NULL` constraint —
  an `INSERT` for a new ghost with an email that already belongs to a
  real user simply fails at the DB level. The *invite* flow (item 6) must
  check "does a real (non-ghost) user already own this email?" before
  deciding to create a ghost at all, and if so, add that existing real
  user to the group directly instead — no ghost row, no claim flow
  needed, this is the easy case.
- **Two ghosts claimed by one real user.** Fully supported:
  `user_merges.ghost_user_id` is `UNIQUE` (a given ghost can only ever be
  merged once — prevents a ghost being "claimed" by two different real
  people), but `real_user_id` is **not** unique, so one real person can
  have arbitrarily many `user_merges` rows pointing at them (e.g. they
  were ghosted in three different groups under three different
  placeholder emails before ever signing up). All balance-resolution
  queries `LEFT JOIN user_merges` on the ghost side, so fan-in is exactly
  the case that join needs to handle and does, with no special-casing.
- **A ghost is claimed (Case 2, explicit merge) but a group's
  `group_members` row for that ghost is never updated to `real_user_id`.**
  Not a correctness bug — historical financial rows are already correct
  by construction (§4.4 Case 2, point 3); this only means the group's
  member list still shows the ghost's placeholder name until someone
  updates that one membership row (a UI/product follow-up, not a
  data-integrity risk).

#### 4.7a Acknowledging the refold-cost trade-off explicitly

§3 rejected Option A partly because "any future feature that wants this
person's balance... pays a join-and-refold tax forever." **The
`LEFT JOIN user_merges` this design adds to every balance query (§4.4
Case 2, point 3) is, honestly, the same *kind* of tax** — it is not free,
and this document should not imply otherwise. The difference that makes
it acceptable where Option A's version was not: Option A's join-and-refold
would have applied **universally**, to every single group membership for
every user in every group, on every balance query, forever, as the
*primary* identity-resolution path. Option B's join applies only against
`user_merges`, a table that only ever gains a row when an *actual,
explicit, rare* Case-2 merge happens — the overwhelming majority of users
(everyone who was never a ghost, and every ghost successfully claimed via
the common Case-1 same-row-activation path, which needs no merge row at
all) cause this `LEFT JOIN` to match zero rows, at which point
`COALESCE` degenerates to the original id with no behavioral or
meaningful cost difference from not having the join at all. **This is a
bounded, opt-in-by-circumstance cost paid only where an identity merge
actually, unavoidably happened — not a universal tax paid by every query
regardless of whether ghosts are involved at all.** Stating this plainly
rather than leaving it as an implicit assumption.

#### 4.7b Merges are one-directional and irreversible by design

There is **no un-merge mechanism** in this design, and none should be
added. A `user_merges` row, once written, is never updated or deleted —
`user_merges` is effectively append-only, mirroring `ledger_entries`'
own philosophy. If a merge is later discovered to have been made in
error (wrong ghost linked to wrong real account), **the correction is
compensating ledger entries** (new `adjustment`-type `LedgerEntry` rows
that move the balance back to where it should be), **never an identity
rollback**. Unwinding a merge after any further activity may have
occurred under the merged identity is far more ambiguous and dangerous
than simply correcting the money with new, signed entries — exactly the
reasoning that already justifies the ledger's own append-only design
elsewhere in this system, applied consistently to identity linkage too.

#### 4.7c/d Co-occurrence: pre-merge (legal) vs. post-merge (blocked)

**Pre-merge:** a ghost's `user_id` and the eventual real person's
(separate, not-yet-merged) `user_id` can legally co-occur as two
different `item_assignments` rows on two different line items of the
**same expense** — e.g. the ghost was assigned some items before the
real person (already independently registered for an unrelated reason)
was assigned others in that same expense, before anyone ever merges
them. This is legal and causes no constraint violation:
`UNIQUE(line_item_id, user_id)` is scoped per line item, not per expense,
so two distinct `user_id` values on two distinct line items of one
expense is always fine, ghosts or not. At balance-read time, §4.4 Case
2's `LEFT JOIN user_merges` correctly folds the ghost's contributions
into the real user's totals once (and only once) a merge row exists —
resolution happens at aggregation time, never at storage time, so this
pre-merge co-occurrence requires no special handling.

**Post-merge:** once a `user_merges` row exists for a given ghost, §4.1's
`reject_reference_to_merged_ghost` triggers make it impossible to create
**any new** row in `item_assignments`, `expenses` (`paid_by`),
`settlements` (`payer_id`/`payee_id`), or `group_members` that references
the merged-away ghost id — so post-merge co-occurrence (a *new* row
still using the old ghost id after a merge exists) cannot happen going
forward; it can only ever be a historical, pre-merge artifact, which is
exactly what the resolution join is for.

### 4.8 Ghost lifecycle

**Creation — restricted to group admins only** (revised from the
original draft's "any existing group member"). The current schema has no
role distinct from `admin`/`member` (`GroupMemberRole`,
`app/domain/models.py:31-33`) — there is no separate "owner" concept
today (the closest per-group distinguished identity is `groups.created_by`,
which records who created the group but is not itself a `GroupMemberRole`
value and is not re-checked after creation). This design maps the
requested "owner-only" restriction onto the **existing** `role = 'admin'`
value: only a group member with `role = GroupMemberRole.admin` may create
a ghost member in that group. If a future, more granular distinction
between "the original creator" and "promoted admins" is introduced, this
restriction should tighten specifically to that narrower role at that
time — this document does not invent a role that doesn't exist today.

**Deletion.** Per §2 point 4, **no FK in this schema specifies
`ondelete`**, so Postgres's default `NO ACTION` behavior already means:
`DELETE FROM users WHERE id = <any user, ghost or not>` fails outright
with a foreign-key-violation error if that id is referenced by even one
row in `item_assignments`, `ledger_entries`, `settlements`, **or**
`group_members` (a ghost is, by definition, a `group_members` row as
long as it remains an unclaimed member of the group). This means:

- **An unclaimed ghost with zero references in `item_assignments`,
  `ledger_entries`, and `settlements` is already safely hard-deletable
  today, with no new mechanism required** — but only after first removing
  its `group_members` row (since that FK would otherwise still block the
  delete too). Two steps, both already enforced safely by existing FK
  behavior: remove from the group, then delete the `users` row.
- **A referenced ghost is never deletable, only claimable — this is the
  terminal rule.** The instant any row in `item_assignments`,
  `ledger_entries`, or `settlements` references a ghost's `user_id`, the
  existing default-`RESTRICT`-like FK behavior already makes hard deletion
  of that `users` row impossible; no new trigger or constraint is needed
  to enforce this, it falls out of the schema's existing (and, per §2
  point 4, previously unexamined) FK defaults. The only path forward for
  such a ghost is the claim flow (§4.4) — never deletion. This holds
  **even after a Case-2 merge**: the ghost's `users.id` row still
  literally is referenced by its historical `item_assignments`/
  `ledger_entries`/`settlements` rows (§4.4 Case 2 never rewrites them),
  so the same FK-enforced restriction continues to apply post-merge, with
  no special-casing needed.
- **No pre-existing cascade-delete hazard was found.** This document
  explicitly checked (§2 point 4) rather than assumed: since nothing in
  this schema uses `ondelete=CASCADE`, deleting *any* in-use `users` row
  today — ghost or not, before or after this feature ships — already
  cannot silently wipe financial history. This is independent of ghost
  members and was true before this design; it is recorded here because
  it was explicitly asked to be verified, not left as an assumption.

## 5. Recommendation (one paragraph)

Adopt **Option B**: ghosts are ordinary `users` rows with an `is_ghost`
flag, a nullable `claimed_at`, and (as of this revision) a DB-level
`CHECK` constraint making credential-holding physically impossible for a
ghost row — with **no FK changes anywhere else in the schema**:
`item_assignments`, `group_members`, `ledger_entries`, and `settlements`
keep referencing `users.id` exactly as they do today. This is not merely
the least-invasive option; it is the only one of the three that doesn't
conflict with two things already true of this codebase today — nullable
`group_id` (personal, groupless expenses) and `compute_user_net_balance`'s
cross-group aggregation by raw `user_id` — both of which an identity
anchor scoped to `group_members` (Option A) would break outright. The
genuinely hard problem the plan calls out — claiming an identity without
ever rewriting the append-only ledger — is solved for the common case
(ghost invited by real email) by a claim that is a same-row metadata
flip with zero other tables touched, gated behind mandatory email
verification and an explicit, informed, consensual claim-or-decline
prompt (never an automatic email-match activation, which this revision
removed entirely as unsafe), and for the rarer case (two genuinely
distinct `users.id` rows for one real person) by a small, additive,
irreversible-by-design `user_merges` table that balance/read queries
resolve through — at an honestly-acknowledged, but narrowly bounded,
join-and-refold cost — and that a DB-level trigger (reusing this
project's existing generic-guard-function convention) prevents any new
activity from ever referencing again, never by mutating a single
historical `item_assignments`, `ledger_entries`, or `settlements` row.

## 6. Self-verification against the five review points

Verifying this revision against the exact same five points the
coordinator's review used, quoting the section that now covers each one
(matching the format the coordinator used against the original draft),
before reporting back.

**Point 1 — ghost credential lockout at DB level.**
COVERED. §4.1's `ck_ghost_no_credentials` CHECK constraint
(`CHECK (NOT is_ghost OR password_hash IS NULL)`) makes it a DB-level
physical impossibility, not an app-layer convention; §4.1a enumerates
every credential-adjacent path (register, login, password-reset —
confirmed not to exist yet by reading `app/api/`/`app/domain/auth.py` —
future OAuth, future magic-link) against the single rule stated once at
the end of §4.1a. §4.6 explicitly demotes the original app-layer login
guard to "second layer, not primary." Self-critique: the CHECK is stated
as a single `ALTER TABLE ... ADD CONSTRAINT`, and §4.2 point 3 explains
why it validates trivially against all pre-existing rows (all
`is_ghost = false` already) — I checked this reasoning rather than
asserting it works "because it's additive," since a `CHECK` (unlike a
plain `ADD COLUMN ... DEFAULT`) *does* get validated against existing
rows unless `NOT VALID` is used, and I wanted to confirm that validation
is actually cheap/trivial here rather than silently gloss over it.

**Point 2 — claim flow replacing unconditional auto-claim.**
COVERED. §4.4 states directly, with a citation to the actual
`app/api/auth.py` code, that Splitr currently has **no email
verification of any kind** at signup, and scopes the new verification
requirement specifically to the ghost-claim path (not all signups) per
the instruction's explicit permission to do so if verification doesn't
already exist universally. §4.4 Case 1 now requires: (a) a staging table
(`ghost_claim_requests`) instead of touching the live row, (b) mailbox
verification, (c) an explicit human-readable claim-or-decline prompt
naming the group/inviter/pending balance, (d) a fully-specified decline
path (placeholder-email swap on the ghost, independent new-account
creation for the real email, group-admin notification). §4.4d is a new
abuse-analysis subsection walking through the attacker-pre-binds-victim
scenario and stating a residual risk honestly (social engineering is
converted to a decision surface, not eliminated; email-liveness
disclosure is a secondary, unresolved residual risk) rather than
claiming zero risk. Self-critique caught mid-verification and fixed
before finalizing (not left as a residual gap): §4.4's Case 1 claim flow
narratively introduces `ghost_claim_requests` as a staging table, but my
first pass of this revision only added it there and not to §4.1's
consolidated schema block or §4.2's migration step list — an internal
inconsistency (schema implied by §4.4 but not actually specified as
schema). Caught this on self-review and folded `ghost_claim_requests`
into §4.1's DDL block and §4.2's step list (now step 5) so the schema is
fully consolidated in one place rather than partially narrated in §4.4.
I did not, deliberately, invent a full token/crypto design for the
verification mechanism itself — that remains implementation detail for
item 6, correctly out of scope for a design-only doc.

**Point 3 — `user_merges` hardening.**
COVERED, four sub-points: (a) §4.7a explicitly names the
`LEFT JOIN user_merges` cost as "the same *kind* of tax" leveled against
Option A in §3, and explains the bounding argument (applies only to the
actually-merged population, not universally) rather than leaving the
parallel implicit. (b) §4.7b states one-directional/irreversible by
design, no un-merge mechanism, correction-via-compensating-ledger-entries
only. (c) §4.1 adds `reject_reference_to_merged_ghost()`, explicitly
following the SAME parameterized-trigger-function convention as item 1's
`reject_mutation_if_expense_confirmed` (named in a code comment,
citing `app/domain/pg_guards.py`), attached to `item_assignments`,
`expenses.paid_by`, `settlements.payer_id`, `settlements.payee_id`,
`group_members.user_id` — with an explicit, reasoned note on why
`ledger_entries` itself is not also directly triggered (upstream tables
are already guarded; flagged as an open question for item 6 rather than
silently omitted). (d) §4.7c/d states both halves explicitly: pre-merge
co-occurrence is legal and needs no special handling (resolved at
read-time); post-merge co-occurrence is DB-blocked going forward by the
new triggers.

**Point 4 — extend `snapshot_all_balances`.**
COVERED. §4.3's `snapshot_all_balances` now includes
`"group_simplified_debts"`, computed via
`app.domain.settlement_simplification.simplify_group_debts` fed from the
same per-group `compute_group_balances` result already being snapshotted
(no duplicate query), with its output explicitly `sorted(...)` and a
comment explaining why sorting the *output* is necessary for
order-independent comparison (the greedy heap-based algorithm is
deterministic given fixed input order, but doesn't guarantee a canonical
tie-break ordering across different runs/insertion orders of
equal-priority participants) rather than relying on determinism alone.

**Point 5 — ghost lifecycle.**
COVERED. §4.8: creation restricted to `GroupMemberRole.admin` (with an
explicit, honest note that there is no separate "owner" role in the
current schema, and this restriction maps onto the nearest existing
analog rather than inventing a role); §4.6 cross-references the new
admin-only authorization check needed for the future ghost-creation
endpoint. Deletion: §2 point 4 and §4.8 both state, from having actually
grepped every migration and model FK, that **no `ondelete` is specified
anywhere in this schema**, so Postgres's default `NO ACTION` behavior
already prevents deleting any referenced `users` row (ghost or not) —
explicitly reported as "no pre-existing cascade hazard found," not
assumed. The terminal rule ("a referenced ghost is never deletable, only
claimable") is stated explicitly, including the post-merge case (still
blocked, since historical rows still reference the ghost id even after
a Case-2 merge).

**Overall self-critique:** the one genuine inconsistency I found while
self-verifying — `ghost_claim_requests` specified narratively in §4.4
but missing from §4.1's consolidated schema DDL and §4.2's migration
step list — was caught and fixed in this same pass (see Point 2 above),
not left as a residual gap. Everything else I checked against the five
points is present and, as far as I can tell on this pass, internally
consistent with the rest of the document (in particular: the new
triggers in §4.1 don't contradict §4.4's claim flow, since Case-1
activation never goes through `user_merges` at all — only Case-2 does —
so the merged-ghost-blocking triggers correctly never fire for the
common Case-1 path). The two residual risks that remain genuinely
unresolved by design, and are reported as such rather than papered over,
are both in §4.4d: social-engineering of the claim prompt itself, and
the email-liveness-disclosure side channel — both are product/ops
mitigations for item 6 to size, not something a design-only document can
close out.
