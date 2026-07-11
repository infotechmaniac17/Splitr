"""
Group endpoints + balance queries (M1).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.api.schemas import (
    ExpenseMemberShare,
    GroupBalancesResponse,
    GroupCreate,
    GroupExpensesBucket,
    GroupExpensesGroupedResponse,
    GroupExpenseSummary,
    GroupMemberAdd,
    GroupMemberInfo,
    GroupMemberResponse,
    GroupMembersResponse,
    GroupResponse,
    PairwiseBalance,
    SimplifiedDebtsResponse,
    SuggestedTransaction,
)
from app.domain.ledger import compute_group_balances
from app.domain.models import (
    Expense,
    ExpenseLineItem,
    ExpenseMemberAllocation,
    Group,
    GroupMember,
    ItemAssignment,
    User,
)
from app.domain.settlement_simplification import simplify_group_debts

router = APIRouter(prefix="/groups", tags=["groups"])


async def _assert_active_member(
    db: AsyncSession,
    group_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """
    Authorization gate for reading a group's data (detail, balances): the
    authenticated caller must be an active member of the group. Anyone else
    -- even a valid, logged-in user of the app -- is rejected with 403.
    Fixes the cross-group data leak finding: these read endpoints previously
    had no membership check at all.
    """
    membership = await db.get(GroupMember, (group_id, actor_id))
    if membership is None or membership.left_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not authorized to view this group",
        )


@router.post("", response_model=GroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: GroupCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Group:
    # The authenticated caller must be the group's creator -- a client can
    # no longer stand up a group "as" someone else.
    if payload.created_by != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="created_by must match the authenticated user",
        )

    group = Group(
        name=payload.name,
        created_by=payload.created_by,
        simplify_debts=payload.simplify_debts,
    )
    db.add(group)
    await db.flush()

    # Auto-add creator as admin.
    member = GroupMember(
        group_id=group.id,
        user_id=payload.created_by,
        role="admin",
    )
    db.add(member)

    await db.commit()
    await db.refresh(group)
    return group


@router.get("/{group_id}", response_model=GroupResponse)
async def get_group(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Group:
    group = await db.get(Group, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Group not found"
        )
    await _assert_active_member(db, group_id, current_user.id)
    return group


@router.post(
    "/{group_id}/members",
    response_model=GroupMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    group_id: uuid.UUID,
    payload: GroupMemberAdd,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> GroupMember:
    group = await db.get(Group, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Group not found"
        )

    # Only an existing active member may invite someone else in.
    actor_membership = await db.get(GroupMember, (group_id, current_user.id))
    if actor_membership is None or actor_membership.left_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an active member of this group may add new members",
        )

    user = await db.get(User, payload.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    # Check if already a member.
    existing = await db.get(GroupMember, (group_id, payload.user_id))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User is already a member of this group",
        )

    member = GroupMember(
        group_id=group_id,
        user_id=payload.user_id,
        role=payload.role,
    )
    db.add(member)
    await db.commit()
    await db.refresh(member)
    return member


@router.get("/{group_id}/members", response_model=GroupMembersResponse)
async def list_group_members(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> GroupMembersResponse:
    """
    Roster for this group (name/avatar only, matching UserPublicResponse's
    slim projection -- never email/phone). Frontend previously had no way
    to fetch this and fell back to a per-browser localStorage cache, which
    silently went empty for any member added from another device/session.
    """
    group = await db.get(Group, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Group not found"
        )
    await _assert_active_member(db, group_id, current_user.id)

    result = await db.execute(
        select(GroupMember, User)
        .join(User, User.id == GroupMember.user_id)
        .where(GroupMember.group_id == group_id, GroupMember.left_at.is_(None))
        .order_by(GroupMember.joined_at)
    )
    members = [
        GroupMemberInfo(
            user_id=user.id,
            name=user.name,
            avatar_url=user.avatar_url,
            role=member.role,
            joined_at=member.joined_at,
        )
        for member, user in result.all()
    ]
    return GroupMembersResponse(group_id=group_id, members=members)


@router.get("/{group_id}/balances", response_model=GroupBalancesResponse)
async def get_group_balances(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> GroupBalancesResponse:
    group = await db.get(Group, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Group not found"
        )
    await _assert_active_member(db, group_id, current_user.id)

    raw = await compute_group_balances(db, group_id)
    balances = [
        PairwiseBalance(debtor_id=d, creditor_id=c, net_amount_minor=a)
        for d, c, a in raw
    ]
    return GroupBalancesResponse(group_id=group_id, balances=balances)


@router.get(
    "/{group_id}/simplified-debts",
    response_model=SimplifiedDebtsResponse,
)
async def get_simplified_debts(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SimplifiedDebtsResponse:
    """
    Suggested (not recorded) settlement transactions for this group.

    If `group.simplify_debts` is True: runs greedy min-cash-flow over the
    group's pairwise balances and returns the minimal transaction set
    (<= n-1 entries) that would zero out every member's net balance --
    see `app.domain.settlement_simplification.simplify_group_debts`.

    If `group.simplify_debts` is False: the group has opted out of debt
    simplification (members want to see/settle exactly who-owes-whom from
    the actual expenses, not a netted proxy). In that case this endpoint
    returns `simplified=False` and `transactions` is simply the raw
    pairwise balances (same response shape, one entry per non-cancelling
    debtor/creditor pair) -- i.e. this endpoint never silently applies
    simplification the group has disabled; the response tells the caller
    which mode was used.

    This is read-only: it never posts to the ledger. Recording an actual
    payment still requires calling POST /settlements.
    """
    group = await db.get(Group, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Group not found"
        )
    await _assert_active_member(db, group_id, current_user.id)

    raw = await compute_group_balances(db, group_id)

    if not group.simplify_debts:
        transactions = [
            SuggestedTransaction(payer_id=d, payee_id=c, amount_minor=a)
            for d, c, a in raw
        ]
        return SimplifiedDebtsResponse(
            group_id=group_id, simplified=False, transactions=transactions
        )

    simplified = simplify_group_debts(raw)
    transactions = [
        SuggestedTransaction(payer_id=payer, payee_id=payee, amount_minor=amount)
        for payer, payee, amount in simplified
    ]
    return SimplifiedDebtsResponse(
        group_id=group_id, simplified=True, transactions=transactions
    )


async def _member_shares_for_expense(
    db: AsyncSession, expense_id: uuid.UUID
) -> list[ExpenseMemberShare]:
    """
    PERSISTED per-member share for one expense -- NEVER recomputed here
    (M6-M8 item 7a: GET /groups/{group_id}/expenses must read money, not
    derive it).

    expense_member_allocations rows exist for an item-5-confirmed expense
    (see app.domain.models.ExpenseMemberAllocation) -- prefer those,
    total_minor is each member's final owed amount.

    Otherwise falls back to the frozen item_assignments.share_minor values
    (the M1/explicit-shares flow, or any confirmed expense from before item
    5 existed -- see GET /expenses/{id}/allocation-preview's identical
    legacy-synthesis fallback), summed per user across every line item
    (share_minor is per-line, a user can have several lines). Rows with
    share_minor IS NULL (an unconfirmed, not-yet-frozen item-level draft) are
    excluded rather than guessed at -- such an expense simply reports no
    member shares yet.
    """
    alloc_result = await db.execute(
        select(ExpenseMemberAllocation).where(
            ExpenseMemberAllocation.expense_id == expense_id
        )
    )
    alloc_rows = list(alloc_result.scalars().all())
    if alloc_rows:
        return [
            ExpenseMemberShare(
                user_id=uuid.UUID(str(r.user_id)), share_minor=int(r.total_minor)
            )
            for r in alloc_rows
        ]

    assign_result = await db.execute(
        select(ItemAssignment.user_id, ItemAssignment.share_minor)
        .join(ExpenseLineItem, ItemAssignment.line_item_id == ExpenseLineItem.id)
        .where(ExpenseLineItem.expense_id == expense_id)
        .where(ItemAssignment.share_minor.is_not(None))
    )
    totals: dict[uuid.UUID, int] = {}
    for user_id, share_minor in assign_result:
        uid = uuid.UUID(str(user_id))
        totals[uid] = totals.get(uid, 0) + int(share_minor)
    return [
        ExpenseMemberShare(user_id=uid, share_minor=amount)
        for uid, amount in totals.items()
    ]


@router.get("/{group_id}/expenses", response_model=GroupExpensesGroupedResponse)
async def get_group_expenses_grouped(
    group_id: uuid.UUID,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    group_by: Literal["date"] = Query(default="date"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> GroupExpensesGroupedResponse:
    """
    Expenses for this group, grouped by `invoice_date` (NOT `created_at` --
    invoice_date is the date the purchase actually happened; created_at is
    only when it was uploaded/recorded, which can lag the invoice date by
    days). `from`/`to` are both INCLUSIVE date boundaries against
    invoice_date.

    NULL invoice_date bucket decision: an expense with no known invoice_date
    is placed in its own deterministic "undated" bucket (`date: null` in the
    response) rather than silently falling back to created_at's date (which
    would misrepresent an unknown purchase date as a known one) -- and such
    an expense is NEVER excluded by a `from`/`to` filter, since it has no
    date to test against and this endpoint does not guess one. A caller
    that only wants dated expenses in range can simply ignore the `date:
    null` bucket in the response.

    Per-expense per-member share summaries are read from PERSISTED rows only
    (see `_member_shares_for_expense`) -- this endpoint never recomputes
    money.

    Auth: requester must be an active member of the group (same gate as
    every other read endpoint in this router).
    """
    group = await db.get(Group, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Group not found"
        )
    await _assert_active_member(db, group_id, current_user.id)

    if from_date is not None and to_date is not None and from_date > to_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'from' must be <= 'to'",
        )

    stmt = select(Expense).where(Expense.group_id == group_id)
    date_conditions = []
    if from_date is not None:
        date_conditions.append(Expense.invoice_date >= from_date)
    if to_date is not None:
        date_conditions.append(Expense.invoice_date <= to_date)
    if date_conditions:
        # NULL invoice_date always bypasses the range filter -- see docstring.
        stmt = stmt.where(
            sa.or_(Expense.invoice_date.is_(None), sa.and_(*date_conditions))
        )
    stmt = stmt.order_by(
        Expense.invoice_date.is_(None), Expense.invoice_date, Expense.created_at
    )

    result = await db.execute(stmt)
    expenses = list(result.scalars().all())

    buckets: dict[date | None, list[GroupExpenseSummary]] = {}
    order: list[date | None] = []
    for exp in expenses:
        key = exp.invoice_date
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        member_shares = await _member_shares_for_expense(db, uuid.UUID(str(exp.id)))
        buckets[key].append(
            GroupExpenseSummary(
                id=uuid.UUID(str(exp.id)),
                vendor=exp.vendor,
                invoice_date=exp.invoice_date,
                total_minor=int(exp.total_minor),
                paid_by=uuid.UUID(str(exp.paid_by)),
                parse_status=exp.parse_status,
                member_shares=member_shares,
            )
        )

    response_buckets = [
        GroupExpensesBucket(date=key, expenses=buckets[key]) for key in order
    ]
    return GroupExpensesGroupedResponse(group_id=group_id, buckets=response_buckets)
