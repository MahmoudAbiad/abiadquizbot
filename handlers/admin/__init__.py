# handlers/admin/__init__.py
from aiogram import Router
from .gifts import router as gifts_router
from .dashboard import router as dashboard_router
from .maintenance import router as maintenance_router
from . import users as _users_module
from .users import router as users_router
from . import direct_gift_charge as _direct_gift_charge_module
from .direct_gift_charge import router as direct_gift_charge_router
from .analytics import router as analytics_router
from .feedbacks import router as feedbacks_router
from .settings import router as settings_router
from .ai_control import router as ai_control_router
from .ai_providers import router as ai_providers_router
from .google_catalog import router as google_catalog_router
from .feature_flags import router as feature_flags_router
from .group_quiz_stats import router as group_quiz_stats_router
from .join_sources import router as join_sources_router
from .challenges import router as challenges_router
from .challenge_settings import router as challenge_settings_router

for _fn_name in (
    "_format_gift_validity_days",
    "process_direct_message_text",
    "process_direct_message_charge_amount",
    "show_direct_message_confirmation",
    "process_direct_message_charge_type",
    "toggle_direct_message_balance",
    "process_confirm_direct_message",
):
    setattr(_users_module, _fn_name, getattr(_direct_gift_charge_module, _fn_name))

router = Router()
router.include_routers(
    dashboard_router,
    maintenance_router,
    gifts_router,
    direct_gift_charge_router,
    users_router,
    analytics_router,
    feedbacks_router,
    settings_router,
    ai_control_router,
    ai_providers_router,
    google_catalog_router,
    feature_flags_router,
    group_quiz_stats_router,
    join_sources_router,
    challenges_router,
    challenge_settings_router,
)
