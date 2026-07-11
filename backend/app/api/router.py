"""
Aggregate API router for M1 — all sub-routers registered here.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import auth, expenses, groups, settlements, users, vendor_discount_rules

router = APIRouter(prefix="/api/v1")

router.include_router(auth.router)
router.include_router(users.router)
router.include_router(groups.router)
router.include_router(expenses.router)
router.include_router(settlements.router)
router.include_router(vendor_discount_rules.router)
