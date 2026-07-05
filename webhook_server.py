"""
Webhook configuration and FastAPI setup for Azure deployment.
Handles HTTP server setup and webhook endpoint routing.
"""

import os
from fastapi import FastAPI, Request
from aiogram.types import Update
from config import bot, dp
from logger import get_logger
from constants import WEBHOOK_HOST, WEBHOOK_PORT, WEBHOOK_PATH

logger = get_logger(__name__)

# Initialize FastAPI app
app = FastAPI(title="Quiz Maker Bot", version="2.0")

# ==================== Health Check ====================
@app.get("/health")
async def health_check():
    """Health check endpoint for Azure monitoring"""
    return {"status": "ok", "bot": "running"}

# ==================== Webhook Endpoint ====================
@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    """
    Handle incoming Telegram updates via webhook.
    
    Args:
        request: FastAPI request object
        
    Returns:
        dict: Status response
    """
    try:
        # Parse Telegram update from request
        update_data = await request.json()
        update = Update(**update_data)
        
        # Process update through aiogram dispatcher
        await dp.feed_update(bot, update)
        
        return {"ok": True}
        
    except Exception as e:
        logger.error(f"Error processing webhook update: {e}", exception=e)
        return {"ok": False, "error": str(e)}

# ==================== Startup Event ====================
@app.on_event("startup")
async def on_startup():
    """
    Set bot webhook on startup.
    Called when FastAPI server starts.
    """
    try:
        # Get webhook URL from environment
        webhook_url = os.getenv("WEBHOOK_URL")
        if not webhook_url:
            logger.warning("WEBHOOK_URL not set in environment, using polling mode")
            return
        
        # Construct full webhook URL
        full_webhook_url = f"{webhook_url}{WEBHOOK_PATH}"
        
        # Set webhook
        await bot.set_webhook(
            url=full_webhook_url,
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query", "inline_query"]
        )
        
        logger.info(f"Webhook set successfully to: {full_webhook_url}")
        print(f"✅ تم تفعيل Webhook بنجاح على: {full_webhook_url}")
        
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}", exception=e)
        print(f"❌ فشل تفعيل Webhook: {e}")

# ==================== Shutdown Event ====================
@app.on_event("shutdown")
async def on_shutdown():
    """
    Clean up on shutdown.
    """
    try:
        await bot.session.close()
        logger.info("Bot session closed")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}", exception=e)

def run_webhook_server():
    """
    Run the webhook server using Uvicorn.
    """
    import uvicorn
    
    logger.info(f"Starting webhook server on {WEBHOOK_HOST}:{WEBHOOK_PORT}")
    print(f"🚀 بدء خادم Webhook على {WEBHOOK_HOST}:{WEBHOOK_PORT}")
    
    uvicorn.run(
        app,
        host=WEBHOOK_HOST,
        port=WEBHOOK_PORT,
        log_level="info"
    )
