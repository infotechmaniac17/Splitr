"""
Group endpoints + balance queries (M1).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.api.schemas import (
    GroupBalancesResponse,
    GroupCreate,
    GroupMemberAdd,
    GroupMemberResponse,
    GroupResponse,
    PairwiseBalance,
    SimplifiedDebtsResponse,
    SuggestedTransaction,
)
from app.domain.ledger import compute_group_balances
from app.domain.models import Group, GroupMember, User
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
