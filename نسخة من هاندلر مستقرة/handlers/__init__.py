"""
Handlers package initialization.
Imports all router modules to be included in dispatcher.
"""

from handlers.start import router as start_router
from handlers.admin import router as admin_router
from handlers.quiz import router as quiz_router

__all__ = ["start_router", "admin_router", "quiz_router"]
