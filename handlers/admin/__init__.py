# handlers/admin/__init__.py
from aiogram import Router
from .dashboard import router as dashboard_router
from .users import router as users_router
from .analytics import router as analytics_router
from .feedbacks import router as feedbacks_router

router = Router()
router.include_routers(
    dashboard_router,
    users_router,
    analytics_router,
    feedbacks_router
)