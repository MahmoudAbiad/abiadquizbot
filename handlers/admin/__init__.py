# handlers/admin/__init__.py
from aiogram import Router
from .dashboard import router as dashboard_router
from .users import router as users_router
from .analytics import router as analytics_router
from .feedbacks import router as feedbacks_router
from .settings import router as settings_router
from .ai_control import router as ai_control_router
from .feature_flags import router as feature_flags_router
from .group_quiz_stats import router as group_quiz_stats_router
from .join_sources import router as join_sources_router
from .challenges import router as challenges_router
from .challenge_settings import router as challenge_settings_router

router = Router()
router.include_routers(
    dashboard_router,
    users_router,
    analytics_router,
    feedbacks_router,
    settings_router,
    ai_control_router,
    feature_flags_router,
    group_quiz_stats_router,
    join_sources_router,
    challenges_router,
    challenge_settings_router,
)