"""
Combined entry point for Discord bot and FastAPI license API.
Runs both services in the same process.
"""
import asyncio
import threading
import uvicorn
from bot import bot
from api import app
from config import DISCORD_TOKEN
import os

# Get port from environment (Railway sets this) or default to 8000
PORT = int(os.environ.get("PORT", 8000))


def run_api():
    """Run the FastAPI server in a separate thread."""
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


async def main():
    """Start both the API server and Discord bot."""
    # Start API in a background thread
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    print(f"API server started on port {PORT}")

    # Run the Discord bot
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
