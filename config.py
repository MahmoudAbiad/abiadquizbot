"""
Bot configuration and FSM states initialization.
Handles bot setup, dispatcher configuration, and Finite State Machine states.
"""

import os
from typing import Optional
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from dotenv import load_dotenv
from logger import get_logger

# Load environment variables
load_dotenv()
logger = get_logger(__name__)

# ==================== Bot Initialization ====================
def _get_bot_token() -> str:
    """
    Retrieve bot token from environment.
    
    Returns:
        str: Bot token
        
    Raises:
        ValueError: If BOT_TOKEN is not set
    """
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise ValueError("BOT_TOKEN is not set in .env file")
    return token

def _get_admin_id() -> int:
    """
    Retrieve admin ID from environment.
    
    Returns:
        int: Admin user ID (0 if not set)
    """
    try:
        admin_id = os.getenv("ADMIN_ID", "0")
        return int(admin_id)
    except ValueError:
        logger.warning("Invalid ADMIN_ID in .env, defaulting to 0")
        return 0

# Initialize bot and dispatcher
try:
    bot = Bot(token=_get_bot_token(), request_timeout=300)
    dp = Dispatcher(storage=MemoryStorage())
    ADMIN_ID: int = _get_admin_id()
    logger.info(f"Bot initialized successfully. Admin ID: {ADMIN_ID if ADMIN_ID else 'Not set'}")
except Exception as e:
    logger.critical(f"Failed to initialize bot: {e}", exception=e)
    raise

# ==================== FSM States ====================
class QuizState(StatesGroup):
    """Finite State Machine states for quiz flow"""
    waiting_for_count = State()  # Waiting for user to input number of questions
    answering_quiz = State()  # Quiz is in progress
