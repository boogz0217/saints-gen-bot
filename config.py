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

# Role to give to active subscribers (Saint's Gen)
SUBSCRIBER_ROLE_ID = int(os.getenv("SUBSCRIBER_ROLE_ID", "0"))

# Role to give to Saint's Shot subscribers
SAINTS_SHOT_ROLE_ID = int(os.getenv("SAINTS_SHOT_ROLE_ID", "0"))

# Role to give to SaintX subscribers
SAINTX_ROLE_ID = int(os.getenv("SAINTX_ROLE_ID", "1475610259208016197"))

# Store URL for renewal
STORE_URL = os.getenv("STORE_URL", "https://saintservice.store/")

# Discord OAuth (for linking Shopify purchases)
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "")  # e.g. https://your-app.railway.app/auth/callback
APP_URL = os.getenv("APP_URL", "")  # Base URL of your Railway app

# Shopify Integration
SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")  # From Shopify Admin > Webhooks
SHOPIFY_PRODUCT_MAP = {
    # Map Shopify product titles/handles to your license products
    # The bot will match these strings (case-insensitive) against product title, handle, variant, or SKU

    # Saint's Gen - 30 days monthly
    "saint's gen": {"product": "saints-gen", "days": 30},
    "saints gen": {"product": "saints-gen", "days": 30},
    "saints-gen": {"product": "saints-gen", "days": 30},

    # Saint's Shot - Weekly (7 days)
    "shot weekly": {"product": "saints-shot", "days": 7},
    "shot 7": {"product": "saints-shot", "days": 7},
    "weekly": {"product": "saints-shot", "days": 7},

    # Saint's Shot - Monthly (30 days)
    "shot monthly": {"product": "saints-shot", "days": 30},
    "shot month": {"product": "saints-shot", "days": 30},
    "saint's shot": {"product": "saints-shot", "days": 30},  # Default to monthly if just "Saint's Shot"
    "saints shot": {"product": "saints-shot", "days": 30},
    "saints-shot": {"product": "saints-shot", "days": 30},
}
# Default license duration if product not in map (in days)
DEFAULT_LICENSE_DAYS = 30
