"""
Combined entry point for Discord bot and FastAPI license API.
Runs both services in the same async event loop.
"""
import asyncio
import uvicorn
from bot import bot
from api import app
from config import DISCORD_TOKEN
import os

# Get port from environment (Railway sets this) or default to 8000
PORT = int(os.environ.get("PORT", 8000))


class Server(uvicorn.Server):
    """Custom uvicorn server that doesn't block."""
    def install_signal_handlers(self):
        pass

    async def serve_async(self):
        await self.startup()
        await self.main_loop()


async def main():
    """Start both the API server and Discord bot in the same event loop."""
    # Configure uvicorn
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = Server(config)

    # Run both concurrently
    await asyncio.gather(
        server.serve_async(),
        bot.start(DISCORD_TOKEN)
    )


if __name__ == "__main__":
    asyncio.run(main())
