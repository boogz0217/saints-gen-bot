"""
Combined entry point for Discord bot and FastAPI license API.
"""
import asyncio
import os
import threading
import uvicorn

# Get port from environment (Railway sets this) or default to 8000
PORT = int(os.environ.get("PORT", 8000))


def run_api():
    """Run the FastAPI server."""
    from api import app
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


def run_bot():
    """Run the Discord bot."""
    from bot import bot
    from config import DISCORD_TOKEN
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    # Start API in a separate thread
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    print(f"API server starting on port {PORT}")

    # Run bot in main thread
    run_bot()
