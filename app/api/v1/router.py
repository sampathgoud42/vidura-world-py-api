from fastapi import APIRouter

from app.api.v1 import bots, kalshi, models_tennis, super, trades, users, wellness

api_router = APIRouter()
api_router.include_router(users.router)
api_router.include_router(kalshi.router)
api_router.include_router(bots.router)
api_router.include_router(trades.router)
api_router.include_router(wellness.router)
api_router.include_router(models_tennis.router)
api_router.include_router(super.router)
