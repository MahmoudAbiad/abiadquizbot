"""
Handlers package initialization.
Imports all new router modules to be included in dispatcher.
"""

from handlers.challenge_game import router as challenge_game_router
from handlers.challenge_live import router as challenge_live_router
from handlers.gifts import router as gifts_router
from handlers.start import router as start_router
from handlers.admin import router as admin_router
from handlers.files import files_router
from handlers.quiz_options import router as quiz_options_router
from handlers.quiz_runner import router as quiz_runner_router
from handlers.favorites import router as favorites_router
from handlers.sharing import router as sharing_router
from handlers.leaderboard import router as leaderboard_router
from handlers.export import export_router
from handlers.audio import audio_router
from handlers.quiz_delete import router as quiz_delete_router
from handlers.group_quiz import group_quiz_router
from handlers.payments import router as payments_router

__all__ = [
    "gifts_router",
    "challenge_game_router",
    "challenge_live_router",
    "start_router", 
    "admin_router", 
    "files_router", 
    "quiz_options_router",
    "quiz_runner_router", 
    "favorites_router", 
    "sharing_router",
    "leaderboard_router",
    "export_router",
    "audio_router",
    "quiz_delete_router",
    "group_quiz_router",
    "payments_router",
]
