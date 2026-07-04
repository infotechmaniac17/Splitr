"""
Expense endpoints (M1 — manual expenses with equal or explicit splits).

All routes in this module require authentication via get_current_user;
the authenticated caller must also be an active member of the relevant
group (see _assert_active_group_members).

Splitting logic for M1:
  - If `shares` given: use exactly as-is (Pydantic already validated sum).
  - If `participants` given: compute equal split using allocate_largest_remainder.
    The paid_by user is automatically included in the participant list if not
    already present (their share is the remainder, no ledger entry for them).

At creation time, shares are stored in item_assignments.share_minor.
POST /expenses/{id}/confirm then reads those shares and posts to the ledger.
"""

from __future__ import annotations

import asyncio
import uuid
from fractions import Fraction
from typing import TYPE_CHECKING

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import (
    ExtractionEnqueuer,
    get_current_user,
    get_db,
    get_extraction_enqueuer,
    get_storage,
)
from app.api.schemas import (
    AssignmentResponse,
    AssignmentsPut,
    ExpenseCreate,
    ExpenseResponse,
    LineItemsCorrection,
    RawExtractionResponse,
    RefundCreate,
    SharesResponse,
)
from app.domain.ledger import (
    load_expense_shares,
    post_expense_to_ledger,
    post_refund_to_ledger,
)
from app.domain.models import (
    Expense,
    ExpenseLineItem,
    ExpenseSource,
    ExpenseStatus,
    GroupMember,
    ItemAssignment,
    LineItemKind,
    ParseStatus,
    User,
)
from app.domain.rounding import allocate_largest_remainder
from app.domain.splitting import (
    SplitError,
    SplitResult,
    compute_shares,
    lines_from_orm,
)
from app.extraction.schema import ExtractedInvoice, ExtractedLineItem
from app.extraction.validation import validate_extraction

if TYPE_CHECKING:
    from app.storage import PdfStorage

router = APIRouter(prefix="/expenses", tags=["expenses"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_equal_shares(
    total_minor: int,
    participants: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Equal split using largest-remainder rounding."""
    n = len(participants)
    ratios: dict[uuid.UUID, Fraction] = {uid: Fraction(1, n) for uid in participants}
    return allocate_largest_remainder(total_minor, ratios)


def _build_shares(payload: ExpenseCreate) -> dict[uuid.UUID, int]:
    """Resolve the split specification into a {user_id: share_minor} dict."""
    if payload.shares is not None:
        return dict(payload.shares)

    # Equal split.
    participants = list(payload.participants or [])
    # Include paid_by if not already listed.
    if payload.paid_by not in participants:
        participants.append(payload.paid_by)
    return _compute_equal_shares(payload.total_minor, participants)


async def _load_lines_with_assignments(
    db: AsyncSession,
    expense_id: uuid.UUID,
) -> list[ExpenseLineItem]:
    result = await db.execute(
        select(ExpenseLineItem)
        .options(selectinload(ExpenseLineItem.assignments))
        .where(ExpenseLineItem.expense_id == expense_id)
        .order_by(ExpenseLineItem.line_no)
    )
    return list(result.scalars().all())


async def _resolve_shares(
    db: AsyncSession,
    expense: Expense,
) -> tuple[dict[uuid.UUID, int], SplitResult | None]:
    """
    Resolve the final {user_id: share_minor} split for an expense.

    M1 path: every assignment already carries a frozen share_minor (written
    at create time) — reuse those.
    M2 path: at least one assignment has no share_minor — run the splitting
    engine over the line items.  Returns the SplitResult so the caller can
    freeze per-line shares at confirmation.
    """
    expense_id = uuid.UUID(str(expense.id))
    lines = await _load_lines_with_assignments(db, expense_id)
    all_assignments = [a for li in lines for a in li.assignments]
    if not all_assignments:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No item assignments found for this expense",
        )

    if all(a.share_minor is not None for a in all_assignments):
        return await load_expense_shares(db, expense_id), None

    try:
        result = compute_shares(lines_from_orm(lines), int(expense.total_minor))
    except SplitError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return result.shares, result


def _last_known_invoice_total(expense: Expense) -> int | None:
    """
    The invoice total the extraction pipeline actually read off the PDF,
    even though the run failed validation overall (needs_review).

    `expense.total_minor` itself is NOT usable here: for a needs_review PDF
    expense it is still the create-time placeholder (see
    app/extraction/tasks.py:_persist_pipeline_result — total_minor is only
    overwritten once parse_status='parsed'). The correction UI needs
    something to reconcile corrected line items against; the last attempt's
    raw model output is the best (and only) available source for that
    number. Returns None if no attempt ever produced one (e.g. every
    attempt's provider call itself failed).
    """
    raw = expense.raw_extraction
    if not raw:
        return None
    attempts = raw.get("attempts") or []
    for attempt in reversed(attempts):
        raw_payload = attempt.get("raw")
        if isinstance(raw_payload, dict):
            total = raw_payload.get("invoice_total_minor")
            if isinstance(total, int):
                return total
    return None


async def _assert_actor_authorized_for_expense(
    db: AsyncSession,
    expense: Expense,
    actor_id: uuid.UUID,
) -> None:
    """
    Authorization gate for money-mutating actions on an existing expense
    (confirm, assignments, refunds, line-item corrections) AND for reading
    an expense's financial data (detail, pdf, raw-extraction, shares): the
    authenticated caller must be either the person who paid, or an active
    member of the expense's group. Anyone else -- even a valid, logged-in
    user of the app -- is rejected with 403. This is also the fix for the
    cross-group data leak finding: these read endpoints previously had no
    membership check at all, so any authenticated (or unauthenticated)
    caller who knew/guessed an expense_id could read another group's
    financial data.
    """
    if uuid.UUID(str(expense.paid_by)) == actor_id:
        return
    if expense.group_id is not None:
        result = await db.execute(
            select(GroupMember.user_id)
            .where(GroupMember.group_id == expense.group_id)
            .where(GroupMember.user_id == actor_id)
            .where(GroupMember.left_at.is_(None))
        )
        if result.scalar_one_or_none() is not None:
            return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You are not authorized to act on this expense",
    )


async def _assert_active_group_members(
    db: AsyncSession,
    group_id: uuid.UUID,
    user_ids: set[uuid.UUID],
) -> None:
    """
    Raise HTTP 422 if any user_id is not an active member of group_id.

    M1: enforced on expense create and confirm, and on settlement create.
    """
    result = await db.execute(
        select(GroupMember.user_id)
        .where(GroupMember.group_id == group_id)
        .where(GroupMember.left_at.is_(None))
    )
    member_ids: set[uuid.UUID] = {uuid.UUID(str(row.user_id)) for row in result}
    non_members = user_ids - member_ids
    if non_members:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Users {[str(u) for u in non_members]} are not active members "
                f"of group {group_id}."
            ),
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", response_model=ExpenseResponse, status_code=status.HTTP_201_CREATED)
async def create_expense(
    payload: ExpenseCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Expense:
    """
    Create a manual expense.

    The expense is stored with parse_status='parsed'.
    Use POST /expenses/{id}/confirm to post to the ledger.
    """
    # The authenticated caller must be the person recording themselves as
    # having paid -- a client can no longer create an expense with someone
    # else's paid_by id (that would forge a debt owed to a third party).
    if payload.paid_by != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="paid_by must match the authenticated user",
        )

    # M2 item-level flow: line items now, assignments later via
    # PUT /expenses/{id}/assignments. Shares are computed at confirm time.
    item_level_flow = payload.participants is None and payload.shares is None

    shares: dict[uuid.UUID, int] = {}
    if not item_level_flow:
        # Build shares before writing anything so we fail fast on bad input.
        shares = _build_shares(payload)
        total = sum(shares.values())
        if total != payload.total_minor:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Computed shares sum to {total}, expected {payload.total_minor}",
            )

    # M1: enforce group membership when group_id is provided.
    if payload.group_id is not None:
        all_user_ids: set[uuid.UUID] = set(shares.keys())
        all_user_ids.add(payload.paid_by)
        await _assert_active_group_members(db, payload.group_id, all_user_ids)

    expense = Expense(
        group_id=payload.group_id,
        paid_by=payload.paid_by,
        vendor=payload.vendor,
        invoice_date=payload.invoice_date,
        invoice_number=payload.invoice_number,
        currency=payload.currency,
        total_minor=payload.total_minor,
        subtotal_minor=payload.total_minor,  # for manual, subtotal == total
        source=ExpenseSource.manual,
        parse_status=ParseStatus.parsed,  # M6: explicit override of 'queued' default
        status=ExpenseStatus.active,
    )
    db.add(expense)
    await db.flush()  # populate expense.id

    # Create line items if provided; otherwise create a single "whole expense" item.
    if payload.line_items:
        # Two passes so parent_line_no can reference any line in the payload.
        created_by_line_no: dict[int, ExpenseLineItem] = {}
        for idx, li_in in enumerate(payload.line_items, start=1):
            li = ExpenseLineItem(
                expense_id=expense.id,
                line_no=li_in.line_no or idx,
                kind=li_in.kind,
                description=li_in.description,
                quantity=li_in.quantity,
                unit_price_minor=li_in.unit_price_minor,
                total_minor=li_in.total_minor,
                allocation=li_in.allocation,
                discount_scope=li_in.discount_scope,
            )
            db.add(li)
            created_by_line_no[li.line_no] = li
        await db.flush()  # populate IDs before wiring parents
        for li_in in payload.line_items:
            if li_in.parent_line_no is not None:
                parent = created_by_line_no.get(li_in.parent_line_no)
                if parent is None:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"parent_line_no {li_in.parent_line_no} not found",
                    )
                created_by_line_no[li_in.line_no].parent_line_id = parent.id
    else:
        # Synthetic whole-expense line item for assignment storage.
        li = ExpenseLineItem(
            expense_id=expense.id,
            line_no=1,
            kind=LineItemKind.item,
            description=payload.vendor or "Expense",
            quantity=1,
            unit_price_minor=payload.total_minor,
            total_minor=payload.total_minor,
        )
        db.add(li)

    await db.flush()  # populate line item IDs

    if not item_level_flow:
        # Reload line items to get IDs.
        result = await db.execute(
            select(ExpenseLineItem)
            .where(ExpenseLineItem.expense_id == expense.id)
            .order_by(ExpenseLineItem.line_no)
        )
        line_items = result.scalars().all()
        # Use the first (or only) line item for assignments.
        primary_line = line_items[0]

        # Store shares as item_assignments.
        for user_id, share_minor in shares.items():
            assignment = ItemAssignment(
                line_item_id=primary_line.id,
                user_id=user_id,
                weight=1,
                share_minor=share_minor,
            )
            db.add(assignment)

    await db.commit()

    # Return with line_items loaded.
    result2 = await db.execute(
        select(Expense)
        .options(selectinload(Expense.line_items))
        .where(Expense.id == expense.id)
    )
    return result2.scalar_one()


@router.post(
    "/upload",
    response_model=ExpenseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_expense_pdf(
    file: UploadFile = File(...),
    paid_by: uuid.UUID = Form(...),
    group_id: uuid.UUID | None = Form(None),
    vendor_hint: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
    storage: PdfStorage = Depends(get_storage),
    enqueue: ExtractionEnqueuer = Depends(get_extraction_enqueuer),
    current_user: User = Depends(get_current_user),
) -> Expense:
    """
    Upload an invoice PDF (M4). API_CONTRACT.md §1: "Expense row created
    (PDF uploaded), extraction not yet run." — creates the expense with
    parse_status='queued', stores the raw PDF bytes (S3-compatible object
    storage, or local filesystem in dev/test — app/storage), and enqueues
    the M3 extraction pipeline (app.extraction.tasks) as a Celery task. This
    endpoint never runs extraction inline; poll GET /expenses/{id} (or wait
    for a push notification, per ARCHITECTURE.md §1.2) for parse_status to
    flip to 'parsed' or 'needs_review'.
    """
    if paid_by != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="paid_by must match the authenticated user",
        )
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded file is empty",
        )
    looks_like_pdf = (file.filename or "").lower().endswith(".pdf") or (
        file.content_type == "application/pdf"
    )
    if not looks_like_pdf:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only PDF uploads are supported",
        )

    if group_id is not None:
        await _assert_active_group_members(db, group_id, {paid_by})

    expense = Expense(
        group_id=group_id,
        paid_by=paid_by,
        currency="INR",
        # Placeholder — the expenses.total_minor CHECK constraint requires
        # > 0, but the real total isn't known until extraction runs. The
        # pipeline overwrites this once parse_status='parsed' (never for
        # needs_review — see _persist_pipeline_result); matches the
        # placeholder convention in tests/test_extraction_pipeline.py.
        total_minor=1,
        source=ExpenseSource.pdf,
        parse_status=ParseStatus.queued,
        status=ExpenseStatus.active,
    )
    db.add(expense)
    await db.flush()  # populate expense.id

    pdf_object_key = f"expenses/{expense.id}.pdf"
    # File I/O is sync even for LocalFilesystemStorage; offload so it never
    # blocks the event loop.
    await asyncio.to_thread(storage.save, pdf_object_key, pdf_bytes)
    expense.pdf_object_key = pdf_object_key

    await db.commit()

    await enqueue(uuid.UUID(str(expense.id)), pdf_bytes, vendor_hint)

    result = await db.execute(
        select(Expense)
        .options(selectinload(Expense.line_items))
        .where(Expense.id == expense.id)
    )
    return result.scalar_one()


@router.get("/{expense_id}/pdf")
async def get_expense_pdf(
    expense_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    storage: PdfStorage = Depends(get_storage),
    current_user: User = Depends(get_current_user),
) -> Response:
    """
    Stream the original uploaded PDF (API_CONTRACT.md §4 point 1 —
    "PDF reference ... wired in M4"). Used by the needs-review side-by-side
    correction screen (PDF left, editable table right).
    """
    result = await db.execute(select(Expense).where(Expense.id == expense_id))
    expense = result.scalar_one_or_none()
    if expense is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found"
        )
    await _assert_actor_authorized_for_expense(db, expense, current_user.id)
    if not expense.pdf_object_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No PDF stored for this expense",
        )
    try:
        pdf_bytes = await asyncio.to_thread(storage.load, str(expense.pdf_object_key))
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Stored PDF not found"
        ) from exc
    return Response(content=pdf_bytes, media_type="application/pdf")


@router.get("/{expense_id}/raw-extraction", response_model=RawExtractionResponse)
async def get_raw_extraction(
    expense_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RawExtractionResponse:
    """
    Expose the raw_extraction JSONB (API_CONTRACT.md §3-4) — deliberately
    excluded from the default ExpenseResponse for size, but needed by the
    needs_review correction/audit UI to render
    `attempts[-1].validation.issues`.
    """
    result = await db.execute(select(Expense).where(Expense.id == expense_id))
    expense = result.scalar_one_or_none()
    if expense is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found"
        )
    await _assert_actor_authorized_for_expense(db, expense, current_user.id)
    if not expense.raw_extraction:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No extraction data available for this expense yet",
        )
    return RawExtractionResponse.model_validate(expense.raw_extraction)


@router.put("/{expense_id}/line-items", response_model=ExpenseResponse)
async def correct_line_items(
    expense_id: uuid.UUID,
    payload: LineItemsCorrection,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Expense:
    """
    Accept corrected line items for a needs_review expense (API_CONTRACT.md
    §4 — "M4 will need something like PUT /expenses/{id}/line-items that
    re-runs validate_extraction server-side ... before allowing the
    transition"). Re-runs the same deterministic validation engine used by
    the M3 pipeline; on success the expense transitions needs_review ->
    parsed and line items are fully replaced (matches the pipeline's own
    "a re-parse replaces, never patches" contract). The ledger is never
    touched here (invariant #2 — confirmation is a separate, later step).
    """
    result = await db.execute(
        select(Expense).where(Expense.id == expense_id).with_for_update()
    )
    expense = result.scalar_one_or_none()
    if expense is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found"
        )
    await _assert_actor_authorized_for_expense(db, expense, current_user.id)
    if expense.status == ExpenseStatus.voided:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot correct line items of a voided expense",
        )
    if expense.parse_status != ParseStatus.needs_review:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Line-item corrections are only accepted while parse_status="
                f"'needs_review' (current: {expense.parse_status.value})"
            ),
        )

    line_nos = [li_no.line_no for li_no in payload.line_items]
    if len(set(line_nos)) != len(line_nos):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="line_no values must be unique",
        )
    for li_check in payload.line_items:
        if (
            li_check.parent_line_no is not None
            and li_check.parent_line_no not in line_nos
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"parent_line_no {li_check.parent_line_no} does not match any line in this payload",
            )

    invoice_total_minor = _last_known_invoice_total(expense)
    if invoice_total_minor is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "No known invoice total to validate corrected line items "
                "against (every extraction attempt's provider call failed). "
                "Use manual expense entry (POST /expenses) instead."
            ),
        )

    candidate_invoice = ExtractedInvoice(
        vendor=expense.vendor,
        invoice_date=expense.invoice_date.isoformat() if expense.invoice_date else None,
        invoice_number=expense.invoice_number,
        currency=str(expense.currency) or "INR",
        line_items=[
            ExtractedLineItem(
                line_no=li_in.line_no,
                kind=li_in.kind,
                description=li_in.description,
                quantity=li_in.quantity,
                unit_price_minor=li_in.unit_price_minor,
                total_minor=li_in.total_minor,
            )
            for li_in in payload.line_items
        ],
        invoice_total_minor=invoice_total_minor,
    )
    validation = validate_extraction(candidate_invoice)
    if not validation.ok:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Corrected line items failed validation",
                "issues": [
                    {"code": i.code, "message": i.message, "line_no": i.line_no}
                    for i in validation.issues
                ],
            },
        )

    # Passed validation — replace line items (full replace, not a patch) and
    # transition needs_review -> parsed.
    existing = await db.execute(
        select(ExpenseLineItem).where(ExpenseLineItem.expense_id == expense_id)
    )
    for existing_li in existing.scalars().all():
        await db.delete(existing_li)
    await db.flush()

    created_by_line_no: dict[int, ExpenseLineItem] = {}
    for li_in in payload.line_items:
        li = ExpenseLineItem(
            expense_id=expense.id,
            line_no=li_in.line_no,
            kind=li_in.kind,
            description=li_in.description,
            quantity=li_in.quantity,
            unit_price_minor=li_in.unit_price_minor,
            total_minor=li_in.total_minor,
            allocation=li_in.allocation,
            discount_scope=li_in.discount_scope,
        )
        db.add(li)
        created_by_line_no[li.line_no] = li
    await db.flush()  # populate IDs before wiring parents
    for li_in in payload.line_items:
        if li_in.parent_line_no is not None:
            created_by_line_no[li_in.line_no].parent_line_id = created_by_line_no[
                li_in.parent_line_no
            ].id

    expense.total_minor = invoice_total_minor
    expense.subtotal_minor = sum(
        li.total_minor for li in payload.line_items if li.kind == LineItemKind.item
    )
    expense.parse_status = ParseStatus.parsed

    await db.commit()

    result2 = await db.execute(
        select(Expense)
        .options(selectinload(Expense.line_items))
        .where(Expense.id == expense_id)
    )
    return result2.scalar_one()


@router.post(
    "/{expense_id}/confirm",
    response_model=ExpenseResponse,
    status_code=status.HTTP_200_OK,
)
async def confirm_expense(
    expense_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Expense:
    """
    Confirm an expense: validate shares, post to ledger, freeze status.

    C1: Uses SELECT ... FOR UPDATE to lock the expense row before checking
    parse_status.  This prevents two concurrent confirms from both seeing
    status='parsed' and double-posting ledger entries.  SQLite ignores FOR
    UPDATE (single-writer anyway); Postgres enforces the row lock.

    Idempotent if already confirmed (returns current state without re-posting).
    """
    # C1: lock the row so concurrent confirms serialize correctly.
    result = await db.execute(
        select(Expense).where(Expense.id == expense_id).with_for_update()
    )
    expense = result.scalar_one_or_none()

    if expense is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found"
        )

    await _assert_actor_authorized_for_expense(db, expense, current_user.id)

    if expense.parse_status == ParseStatus.confirmed:
        # Already confirmed — idempotent return without re-posting.
        result2 = await db.execute(
            select(Expense)
            .options(selectinload(Expense.line_items))
            .where(Expense.id == expense_id)
        )
        return result2.scalar_one()

    if expense.status == ExpenseStatus.voided:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot confirm a voided expense",
        )

    # Only expenses that passed validation (or manual expenses, which are
    # created as 'parsed') may be confirmed.  queued/needs_review/failed
    # expenses must go through the validation/review flow first.
    if expense.parse_status != ParseStatus.parsed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Expense is not in a confirmable state",
        )

    # M1: enforce group membership on confirm too (a member may have left since
    # the expense was created).
    if expense.group_id is not None:
        # We need to check paid_by + all participants (from assignments).
        # Load the paid_by first so we can include it in the check even before
        # shares are loaded.
        group_id: uuid.UUID = uuid.UUID(str(expense.group_id))
        paid_by_id: uuid.UUID = uuid.UUID(str(expense.paid_by))
        users_to_check: set[uuid.UUID] = {paid_by_id}

        # Also gather participant IDs from assignments.
        assign_result = await db.execute(
            select(ItemAssignment.user_id)
            .join(ExpenseLineItem, ItemAssignment.line_item_id == ExpenseLineItem.id)
            .where(ExpenseLineItem.expense_id == expense_id)
        )
        for row in assign_result:
            users_to_check.add(uuid.UUID(str(row.user_id)))

        await _assert_active_group_members(db, group_id, users_to_check)

    # Resolve shares: frozen M1 shares, or M2 splitting engine over line items.
    shares, split_result = await _resolve_shares(db, expense)

    # post_expense_to_ledger validates sum == total_minor and posts entries.
    try:
        await post_expense_to_ledger(db, expense, shares)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    # M2: freeze per-line computed shares on the assignment rows (audit trail;
    # historical balances never shift if splitting rules change later).
    # Lines that were allocated without explicit assignment rows (cart fees,
    # discounts, inherited refunds) get audit rows so the frozen shares of an
    # expense always sum to total_minor.
    if split_result is not None:
        lines = await _load_lines_with_assignments(db, expense_id)
        for li in lines:
            allocation = split_result.line_allocations.get(uuid.UUID(str(li.id)), {})
            existing_users: set[uuid.UUID] = set()
            for a in li.assignments:
                user_id = uuid.UUID(str(a.user_id))
                existing_users.add(user_id)
                a.share_minor = allocation.get(user_id, 0)
            for user_id, amount in allocation.items():
                if user_id not in existing_users:
                    db.add(
                        ItemAssignment(
                            line_item_id=li.id,
                            user_id=user_id,
                            weight=1,
                            share_minor=amount,
                        )
                    )

    await db.commit()

    result3 = await db.execute(
        select(Expense)
        .options(selectinload(Expense.line_items))
        .where(Expense.id == expense_id)
    )
    return result3.scalar_one()


@router.put(
    "/{expense_id}/assignments",
    response_model=list[AssignmentResponse],
    status_code=status.HTTP_200_OK,
)
async def put_assignments(
    expense_id: uuid.UUID,
    payload: AssignmentsPut,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ItemAssignment]:
    """
    Replace ALL item assignments of an expense (M2 item-level flow).

    Only allowed before confirmation. Weights are per-line relative shares
    (e.g. Alice weight 2, Bob weight 1 → Alice pays 2/3 of that line).
    Assigning to a subgroup is UI sugar — clients expand it to one row per
    member before calling this endpoint.
    """
    result = await db.execute(select(Expense).where(Expense.id == expense_id))
    expense = result.scalar_one_or_none()
    if expense is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found"
        )
    await _assert_actor_authorized_for_expense(db, expense, current_user.id)
    if expense.parse_status == ParseStatus.confirmed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot modify assignments of a confirmed expense",
        )
    if expense.status == ExpenseStatus.voided:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot modify assignments of a voided expense",
        )

    lines = await _load_lines_with_assignments(db, expense_id)
    line_ids = {uuid.UUID(str(li.id)) for li in lines}
    unknown = {a.line_item_id for a in payload.assignments} - line_ids
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Line items {[str(x) for x in unknown]} do not belong to this expense",
        )

    seen: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for a in payload.assignments:
        key = (a.line_item_id, a.user_id)
        if key in seen:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Duplicate assignment for user {a.user_id} on line {a.line_item_id}",
            )
        seen.add(key)

    if expense.group_id is not None:
        await _assert_active_group_members(
            db,
            uuid.UUID(str(expense.group_id)),
            {a.user_id for a in payload.assignments},
        )

    # Replace: delete existing rows (pre-confirmation, so no frozen audit data
    # is lost — share_minor is only written at confirm time in this flow).
    for li in lines:
        for existing in list(li.assignments):
            await db.delete(existing)
    await db.flush()

    created: list[ItemAssignment] = []
    for a in payload.assignments:
        row = ItemAssignment(
            line_item_id=a.line_item_id,
            user_id=a.user_id,
            weight=a.weight,
            share_minor=None,
        )
        db.add(row)
        created.append(row)

    await db.commit()
    return created


@router.get("/{expense_id}/shares", response_model=SharesResponse)
async def get_shares(
    expense_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SharesResponse:
    """
    Preview the computed split without posting anything.

    Frozen expenses (M1 or confirmed) return the frozen shares; otherwise the
    splitting engine runs over the current line items and assignments.
    """
    result = await db.execute(select(Expense).where(Expense.id == expense_id))
    expense = result.scalar_one_or_none()
    if expense is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found"
        )
    await _assert_actor_authorized_for_expense(db, expense, current_user.id)

    shares, _ = await _resolve_shares(db, expense)
    return SharesResponse(expense_id=expense_id, shares=shares)


@router.post(
    "/{expense_id}/refunds",
    response_model=ExpenseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_refund(
    expense_id: uuid.UUID,
    payload: RefundCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Expense:
    """
    Record a refund against one item line of a CONFIRMED expense.

    Appends a kind='refund' line (negative total) whose assignments copy the
    original item's ratios, and posts refund_reversal ledger entries so money
    flows back along exactly the path it came.  The original expense and its
    ledger entries are never mutated (append-only).
    """
    result = await db.execute(
        select(Expense).where(Expense.id == expense_id).with_for_update()
    )
    expense = result.scalar_one_or_none()
    if expense is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found"
        )
    await _assert_actor_authorized_for_expense(db, expense, current_user.id)
    if expense.status == ExpenseStatus.voided:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot refund a voided expense",
        )
    if expense.parse_status != ParseStatus.confirmed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Refunds can only be recorded against a confirmed expense. "
            "Before confirmation, add a refund line item instead.",
        )

    lines = await _load_lines_with_assignments(db, expense_id)

    # Idempotency: same key on the same expense → the refund was already
    # recorded; return current state without re-posting (the expense row is
    # locked above, so concurrent duplicates serialize through this check).
    if payload.idempotency_key is not None:
        already = next(
            (li for li in lines if li.idempotency_key == payload.idempotency_key),
            None,
        )
        if already is not None:
            result_idem = await db.execute(
                select(Expense)
                .options(selectinload(Expense.line_items))
                .where(Expense.id == expense_id)
            )
            return result_idem.scalar_one()

    parent = next(
        (li for li in lines if uuid.UUID(str(li.id)) == payload.parent_line_id),
        None,
    )
    if parent is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="parent_line_id does not belong to this expense",
        )
    if parent.kind != LineItemKind.item:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Refunds must target an item line, not kind={parent.kind}",
        )
    if not parent.assignments:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Parent line has no assignments to copy ratios from",
        )

    # Cap: cumulative refunds cannot exceed what was actually paid for the
    # line — its NET total after item-scoped discounts (reviewer CRITICAL:
    # a gross cap lets a refund exceed the amount collected, manufacturing
    # debt out of nowhere).  Children = discount/refund lines pointing at
    # this parent.
    net_line_total = int(parent.total_minor)
    prior_refunds = 0
    for li in lines:
        if (
            li.parent_line_id is None
            or uuid.UUID(str(li.parent_line_id)) != payload.parent_line_id
        ):
            continue
        if li.kind == LineItemKind.refund:
            prior_refunds += -int(li.total_minor)
        elif li.kind == LineItemKind.discount:
            net_line_total += int(li.total_minor)  # negative
    if prior_refunds + payload.amount_minor > net_line_total:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Refund {payload.amount_minor} plus prior refunds "
                f"{prior_refunds} exceeds the net line total {net_line_total} "
                "(line total minus item-scoped discounts)"
            ),
        )

    # Refund shares copy the parent's assignment ratios (weight-based).
    ratios = {
        uuid.UUID(str(a.user_id)): Fraction(a.weight)
        for a in sorted(parent.assignments, key=lambda a: str(a.user_id))
    }
    weight_sum = sum(ratios.values())
    ratios = {u: w / weight_sum for u, w in ratios.items()}
    refund_shares = allocate_largest_remainder(payload.amount_minor, ratios)

    refund_line = ExpenseLineItem(
        expense_id=expense.id,
        line_no=max(li.line_no for li in lines) + 1,
        kind=LineItemKind.refund,
        description=payload.description or f"Refund: {parent.description or 'item'}",
        quantity=1,
        unit_price_minor=-payload.amount_minor,
        total_minor=-payload.amount_minor,
        parent_line_id=parent.id,
        idempotency_key=payload.idempotency_key,
    )
    db.add(refund_line)
    await db.flush()

    # Audit trail: assignment rows on the refund line with negative frozen
    # shares mirroring the reversal amounts.
    for a in sorted(parent.assignments, key=lambda a: str(a.user_id)):
        user_id = uuid.UUID(str(a.user_id))
        db.add(
            ItemAssignment(
                line_item_id=refund_line.id,
                user_id=user_id,
                weight=a.weight,
                share_minor=-refund_shares[user_id],
            )
        )

    try:
        await post_refund_to_ledger(db, expense, refund_shares)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    await db.commit()

    result2 = await db.execute(
        select(Expense)
        .options(selectinload(Expense.line_items))
        .where(Expense.id == expense_id)
    )
    return result2.scalar_one()


@router.get("/{expense_id}", response_model=ExpenseResponse)
async def get_expense(
    expense_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Expense:
    result = await db.execute(
        select(Expense)
        .options(selectinload(Expense.line_items))
        .where(Expense.id == expense_id)
    )
    expense = result.scalar_one_or_none()
    if expense is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found"
        )
    await _assert_actor_authorized_for_expense(db, expense, current_user.id)
    return expense
