"""Configuration for the Discord bot - loads from environment variables."""
import os
from dotenv import load_dotenv

# Load .env file if it exists (for local development)
load_dotenv()

# Discord Bot Token
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

# Admin Discord User IDs (comma-separated)
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(id.strip()) for id in ADMIN_IDS_STR.split(",") if id.strip()]

# Secret key for HMAC signing of license keys
# IMPORTANT: This must match the key in the macro app
SECRET_KEY = os.getenv("SECRET_KEY", "84e1164ba91f2831011564f7883b7a73faadbe71f66c62089b61b8cedf997272")

# Database URL (PostgreSQL - Railway provides this automatically)
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Bot settings
BOT_PREFIX = "!"  # Not used for slash commands, but kept for potential future use

# Guild (Server) ID for role management
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# Role to give to active subscribers
SUBSCRIBER_ROLE_ID = int(os.getenv("SUBSCRIBER_ROLE_ID", "0"))

# Store URL for renewal
STORE_URL = os.getenv("STORE_URL", "https://saintservice.store/")
