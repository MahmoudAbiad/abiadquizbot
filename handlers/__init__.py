"""
Handlers package initialization.
Imports all new router modules to be included in dispatcher.
"""

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

__all__ = [
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
]