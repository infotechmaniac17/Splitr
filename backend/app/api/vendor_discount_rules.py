"""
Vendor discount rule CRUD endpoints (M6 item 3).

Two families of routes:

  Group-scoped rules  -- /groups/{group_id}/vendor-discount-rules[/...]
    list:      any active member of the group.
    create/update/deactivate: an ADMIN member of the group only
    (GroupMemberRole.admin, per the schema's only two roles -- there is no
    'owner' role in this codebase, see app/domain/models.py:GroupMemberRole).

  Creator-global rules -- /vendor-discount-rules/global[/...]
    (group_id=None; usable by their creator across ANY of their groups --
    see app.domain.vendor_discount.find_matching_rule's global-rule
    fallback). list/create/update/deactivate: only the rule's own creator.
    Chosen over a `?global=true` query-param filter on the group-scoped
    list endpoint because global rules aren't scoped to any one group at
    all -- a dedicated top-level path better reflects that they don't
    belong under /groups/{group_id}/... in the first place.

Soft delete only: rules are never hard-deleted (expenses.discount_rule_id
has an ON DELETE SET NULL FK to this table for historical lineage -- see
migration 0009's docstring) -- "delete" here always means
PATCH .../deactivate setting active=false.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.api.schemas import (
    VendorDiscountRuleCreate,
    VendorDiscountRuleResponse,
    VendorDiscountRulesListResponse,
    VendorDiscountRuleUpdate,
)
from app.domain.models import (
    Group,
    GroupMember,
    GroupMemberRole,
    User,
    VendorDiscountRule,
)

router = APIRouter(tags=["vendor-discount-rules"])


# ---------------------------------------------------------------------------
# Authorization helpers
# ---------------------------------------------------------------------------


async def _assert_active_member(
    db: AsyncSession, group_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    membership = await db.get(GroupMember, (group_id, actor_id))
    if membership is None or membership.left_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not authorized to view this group",
        )


async def _assert_group_admin(
    db: AsyncSession, group_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    """
    Admin-role gate for create/update/deactivate of a group-scoped vendor
    discount rule. There is no 'owner' role (GroupMemberRole has only
    'admin' and 'member' -- see app/domain/models.py); admin is the gate.
    """
    membership = await db.get(GroupMember, (group_id, actor_id))
    if (
        membership is None
        or membership.left_at is not None
        or membership.role != GroupMemberRole.admin
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a group admin may manage vendor discount rules for this group",
        )


async def _get_group_or_404(db: AsyncSession, group_id: uuid.UUID) -> Group:
    group = await db.get(Group, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Group not found"
        )
    return group


async def _get_rule_or_404(db: AsyncSession, rule_id: uuid.UUID) -> VendorDiscountRule:
    rule = await db.get(VendorDiscountRule, rule_id)
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor discount rule not found",
        )
    return rule


def _apply_update(rule: VendorDiscountRule, payload: VendorDiscountRuleUpdate) -> None:
    if payload.vendor_pattern is not None:
        rule.vendor_pattern = payload.vendor_pattern
    if payload.min_order_total_minor is not None:
        rule.min_order_total_minor = payload.min_order_total_minor
    if payload.discount_type is not None:
        rule.discount_type = payload.discount_type
        rule.discount_value_minor = payload.discount_value_minor
        rule.discount_percent = payload.discount_percent
    if payload.active is not None:
        rule.active = payload.active


# ---------------------------------------------------------------------------
# Group-scoped rules
# ---------------------------------------------------------------------------


@router.get(
    "/groups/{group_id}/vendor-discount-rules",
    response_model=VendorDiscountRulesListResponse,
)
async def list_group_rules(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> VendorDiscountRulesListResponse:
    await _get_group_or_404(db, group_id)
    await _assert_active_member(db, group_id, current_user.id)

    result = await db.execute(
        select(VendorDiscountRule)
        .where(VendorDiscountRule.group_id == group_id)
        .order_by(VendorDiscountRule.created_at)
    )
    return VendorDiscountRulesListResponse(rules=list(result.scalars().all()))


@router.post(
    "/groups/{group_id}/vendor-discount-rules",
    response_model=VendorDiscountRuleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_group_rule(
    group_id: uuid.UUID,
    payload: VendorDiscountRuleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> VendorDiscountRule:
    await _get_group_or_404(db, group_id)
    await _assert_group_admin(db, group_id, current_user.id)

    if payload.group_id is not None and payload.group_id != group_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="payload.group_id, if set, must match the path group_id",
        )

    rule = VendorDiscountRule(
        group_id=group_id,
        created_by=current_user.id,
        vendor_pattern=payload.vendor_pattern,
        min_order_total_minor=payload.min_order_total_minor,
        discount_type=payload.discount_type,
        discount_value_minor=payload.discount_value_minor,
        discount_percent=payload.discount_percent,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.put(
    "/groups/{group_id}/vendor-discount-rules/{rule_id}",
    response_model=VendorDiscountRuleResponse,
)
async def update_group_rule(
    group_id: uuid.UUID,
    rule_id: uuid.UUID,
    payload: VendorDiscountRuleUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> VendorDiscountRule:
    await _get_group_or_404(db, group_id)
    await _assert_group_admin(db, group_id, current_user.id)

    rule = await _get_rule_or_404(db, rule_id)
    if rule.group_id != group_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor discount rule not found in this group",
        )

    _apply_update(rule, payload)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.delete(
    "/groups/{group_id}/vendor-discount-rules/{rule_id}",
    response_model=VendorDiscountRuleResponse,
)
async def deactivate_group_rule(
    group_id: uuid.UUID,
    rule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> VendorDiscountRule:
    """
    Soft delete: sets active=false. Never a hard DELETE (expenses may
    reference this rule via discount_rule_id for historical lineage).
    """
    await _get_group_or_404(db, group_id)
    await _assert_group_admin(db, group_id, current_user.id)

    rule = await _get_rule_or_404(db, rule_id)
    if rule.group_id != group_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor discount rule not found in this group",
        )

    rule.active = False
    await db.commit()
    await db.refresh(rule)
    return rule


# ---------------------------------------------------------------------------
# Creator-global rules
# ---------------------------------------------------------------------------


@router.get(
    "/vendor-discount-rules/global",
    response_model=VendorDiscountRulesListResponse,
)
async def list_global_rules(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> VendorDiscountRulesListResponse:
    """Only the caller's OWN global rules -- never another user's."""
    result = await db.execute(
        select(VendorDiscountRule)
        .where(
            VendorDiscountRule.group_id.is_(None),
            VendorDiscountRule.created_by == current_user.id,
        )
        .order_by(VendorDiscountRule.created_at)
    )
    return VendorDiscountRulesListResponse(rules=list(result.scalars().all()))


@router.post(
    "/vendor-discount-rules/global",
    response_model=VendorDiscountRuleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_global_rule(
    payload: VendorDiscountRuleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> VendorDiscountRule:
    if payload.group_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="group_id must be null for a global rule "
            "(use POST /groups/{group_id}/vendor-discount-rules instead)",
        )

    rule = VendorDiscountRule(
        group_id=None,
        created_by=current_user.id,
        vendor_pattern=payload.vendor_pattern,
        min_order_total_minor=payload.min_order_total_minor,
        discount_type=payload.discount_type,
        discount_value_minor=payload.discount_value_minor,
        discount_percent=payload.discount_percent,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


async def _assert_global_rule_owner(
    db: AsyncSession, rule_id: uuid.UUID, actor_id: uuid.UUID
) -> VendorDiscountRule:
    rule = await _get_rule_or_404(db, rule_id)
    if rule.group_id is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rule is not a global rule",
        )
    if rule.created_by != actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the creator of a global rule may modify it",
        )
    return rule


@router.put(
    "/vendor-discount-rules/global/{rule_id}",
    response_model=VendorDiscountRuleResponse,
)
async def update_global_rule(
    rule_id: uuid.UUID,
    payload: VendorDiscountRuleUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> VendorDiscountRule:
    rule = await _assert_global_rule_owner(db, rule_id, current_user.id)
    _apply_update(rule, payload)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.delete(
    "/vendor-discount-rules/global/{rule_id}",
    response_model=VendorDiscountRuleResponse,
)
async def deactivate_global_rule(
    rule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> VendorDiscountRule:
    rule = await _assert_global_rule_owner(db, rule_id, current_user.id)
    rule.active = False
    await db.commit()
    await db.refresh(rule)
    return rule
