"""
Handlers package initialization.
Imports all new router modules to be included in dispatcher.
"""

from handlers.start import router as start_router
from handlers.admin import router as admin_router
from handlers.files import files_router
from handlers.execution import router as execution_router
from handlers.favorites import router as favorites_router
from handlers.sharing import router as sharing_router

__all__ = [
    "start_router", 
    "admin_router", 
    "files_router", 
    "execution_router", 
    "favorites_router", 
    "sharing_router"
]