"""
Centralized logging module for the quiz maker bot.
Provides consistent logging across all modules with proper formatting.
"""

import logging
import os
from datetime import datetime
from typing import Optional

# Create logs directory if it doesn't exist
LOGS_DIR = "logs"
if not os.path.exists(LOGS_DIR):
    os.makedirs(LOGS_DIR)

# Configure logging format
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Create log file name with date
log_file = os.path.join(LOGS_DIR, f"bot_{datetime.now().strftime('%Y-%m-%d')}.log")

# Configure root logger with UTF-8 encoding
logging.basicConfig(
    level=logging.DEBUG,
    format=LOG_FORMAT,
    datefmt=DATE_FORMAT,
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()  # Also print to console
    ]
)

def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for a specific module.
    
    Args:
        name: The module name (typically __name__)
        
    Returns:
        logging.Logger: Configured logger instance
    """
    return logging.getLogger(name)

def log_info(logger: logging.Logger, message: str) -> None:
    """Log info level message"""
    logger.info(message)

def log_warning(logger: logging.Logger, message: str) -> None:
    """Log warning level message"""
    logger.warning(message)

def log_error(logger: logging.Logger, message: str, exception: Optional[Exception] = None) -> None:
    """Log error level message with optional exception"""
    if exception:
        logger.error(message, exc_info=True)
    else:
        logger.error(message)

def log_debug(logger: logging.Logger, message: str) -> None:
    """Log debug level message"""
    logger.debug(message)

def log_critical(logger: logging.Logger, message: str, exception: Optional[Exception] = None) -> None:
    """Log critical level message with optional exception"""
    if exception:
        logger.critical(message, exc_info=True)
    else:
        logger.critical(message)
