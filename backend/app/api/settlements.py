"""
Settlement endpoints (M1).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.api.expenses import _assert_active_group_members
from app.api.schemas import SettlementCreate, SettlementResponse, UserBalanceResponse
from app.domain.ledger import compute_user_net_balance, post_settlement_to_ledger
from app.domain.models import SettlementMethod, User

router = APIRouter(tags=["settlements"])


@router.post(
    "/settlements",
    response_model=SettlementResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_settlement(
    payload: SettlementCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> object:
    """
    Record a payment from payer_id to payee_id and post the ledger entry.

    The settlement entry reverses the debt direction so pairwise netting
    correctly cancels out expense_share entries. The authenticated caller
    must be one of the two parties to the settlement (either the one who
    paid, or the one who received the payment) -- a third party cannot
    record a settlement between two other users.
    """
    if current_user.id not in (payload.payer_id, payload.payee_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be the payer or payee to record this settlement",
        )
    payer = await db.get(User, payload.payer_id)
    if payer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payer {payload.payer_id} not found",
        )
    payee = await db.get(User, payload.payee_id)
    if payee is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payee {payload.payee_id} not found",
        )

    # M1: enforce group membership when group_id is provided.
    if payload.group_id is not None:
        await _assert_active_group_members(
            db,
            payload.group_id,
            {payload.payer_id, payload.payee_id},
        )

    try:
        settlement, _ = await post_settlement_to_ledger(
            db=db,
            group_id=payload.group_id,
            payer_id=payload.payer_id,
            payee_id=payload.payee_id,
            amount_minor=payload.amount_minor,
            method=SettlementMethod(payload.method),
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    await db.commit()
    await db.refresh(settlement)
    return settlement


@router.get("/users/{user_id}/balance", response_model=UserBalanceResponse)
async def get_user_balance(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserBalanceResponse:
    """
    Net balance across ALL of this user's groups and personal expenses
    (see compute_user_net_balance) -- a cross-group financial summary, so
    only the user themself may view it (no "shared group" carve-out: being
    in one group together shouldn't expose someone's balance in every other
    group they're in).

    Non-self callers get a plain 404 regardless of whether user_id exists --
    never 403 -- so that "not found" and "not yours" are indistinguishable
    and a stranger cannot enumerate valid user UUIDs by observing 403 vs 404.
    """
    if user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    net = await compute_user_net_balance(db, user_id)
    return UserBalanceResponse(user_id=user_id, net_balance_minor=net)
