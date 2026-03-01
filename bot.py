"""
Saint's Gen - License Bot
Discord bot for managing subscription licenses.
"""
import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import os
import time
from datetime import datetime, timedelta
from typing import Optional
import math

import aiohttp

from config import DISCORD_TOKEN, ADMIN_IDS, SECRET_KEY, GUILD_ID, SUBSCRIBER_ROLE_ID, SAINTS_SHOT_ROLE_ID, SAINTX_ROLE_ID, STORE_URL
from database import (
    init_db, add_license, get_license_by_key, get_license_by_user,
    revoke_license, revoke_user_licenses,
    extend_license, extend_user_license, get_all_active_licenses, get_license_stats,
    reset_hwid_by_key, reset_hwid_by_user, get_hwid_by_key,
    get_newly_expired_licenses, mark_expiry_notified, has_active_license,
    has_active_license_for_product, close_pool, init_notifications_table,
    get_pending_notifications, get_failed_notifications, init_referrals_table,
    get_referral_count_received, get_referral_count_given, has_been_referred_by,
    add_referral, get_referral_stats, extend_user_license_for_product,
    get_pending_order_by_email, claim_pending_order, init_linked_accounts_table,
    init_purchases_table, redeem_by_email, get_all_licenses_for_user,
    cleanup_duplicate_licenses, get_licenses_expiring_soon, mark_warning_notified
)
from license_crypto import generate_license_key, get_key_info


class LicenseBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True  # Needed to fetch member info
        intents.message_content = True  # Needed to read message content for auto-help
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Initialize database
        await init_db()
        await init_notifications_table()
        await init_referrals_table()
        await init_linked_accounts_table()
        await init_purchases_table()
        # Sync slash commands globally and to specific guild for instant availability
        await self.tree.sync()
        # Instant sync to your server
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        print(f"Synced slash commands")
        # Start background tasks
        self.check_expired_licenses.start()
        self.check_expiring_soon.start()
        self.process_shopify_notifications.start()

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print(f"Admin IDs: {ADMIN_IDS}")
        print(f"Guild ID: {GUILD_ID}")
        print(f"Subscriber Role ID: {SUBSCRIBER_ROLE_ID}")
        print(f"Saint's Shot Role ID: {SAINTS_SHOT_ROLE_ID}")
        print(f"SaintX Role ID: {SAINTX_ROLE_ID}")
        print("------")
        # Update status message on startup
        await update_status_message()

    async def close(self):
        """Clean up resources when bot shuts down."""
        await close_pool()
        await super().close()

    @tasks.loop(minutes=5)
    async def check_expired_licenses(self):
        """Background task to check for expired licenses and remove roles."""
        await self.wait_until_ready()

        if not GUILD_ID:
            return  # Role management not configured

        try:
            guild = self.get_guild(GUILD_ID)
            if not guild:
                print(f"Could not find guild {GUILD_ID}")
                return

            # Get newly expired licenses
            expired = await get_newly_expired_licenses()

            for lic in expired:
                discord_id = lic["discord_id"]
                product = lic.get("product", "saints-gen")

                # Determine which role to check based on product
                role_id = get_role_id_for_product(product)
                product_name = get_product_name(product)

                if not role_id:
                    # Mark as notified and skip if role not configured
                    await mark_expiry_notified(lic["license_key"])
                    continue

                role = guild.get_role(role_id)
                if not role:
                    print(f"Could not find role {role_id} for {product}")
                    await mark_expiry_notified(lic["license_key"])
                    continue

                # Check if user has any other active licenses for this product
                still_active = await has_active_license_for_product(discord_id, product)

                if not still_active:
                    # Remove role from user
                    try:
                        member = await guild.fetch_member(int(discord_id))
                        if member and role in member.roles:
                            await member.remove_roles(role, reason=f"{product_name} license expired")
                            print(f"Removed {product_name} role from {member} (license expired)")

                            # DM the user
                            try:
                                embed = discord.Embed(
                                    title="Subscription Expired",
                                    description=f"Your {product_name} license has expired.",
                                    color=discord.Color.red()
                                )
                                embed.add_field(
                                    name="Renew Your Subscription",
                                    value=f"To continue using {product_name}, please renew your subscription at:\n{STORE_URL}",
                                    inline=False
                                )
                                embed.set_footer(text=f"Thank you for using {product_name}!")
                                await member.send(embed=embed)
                                print(f"Sent expiry DM to {member}")
                            except discord.Forbidden:
                                print(f"Could not DM {member} (DMs disabled)")
                    except discord.NotFound:
                        print(f"Member {discord_id} not found in guild")
                    except Exception as e:
                        print(f"Error processing expired license for {discord_id}: {e}")

                # Mark as notified regardless
                await mark_expiry_notified(lic["license_key"])

        except Exception as e:
            print(f"Error in check_expired_licenses: {e}")

    @check_expired_licenses.before_loop
    async def before_check_expired(self):
        await self.wait_until_ready()

    @tasks.loop(hours=12)
    async def check_expiring_soon(self):
        """Background task to warn users about licenses expiring in 3 days."""
        await self.wait_until_ready()

        if not GUILD_ID:
            return

        try:
            # Get licenses expiring within 3 days
            expiring = await get_licenses_expiring_soon(days=3)

            for lic in expiring:
                discord_id = lic["discord_id"]
                product = lic.get("product", "saints-gen")
                expires_at = lic["expires_at"]
                product_name = get_product_name(product)

                # Calculate days remaining
                now = datetime.utcnow()
                if isinstance(expires_at, str):
                    expires_at = datetime.fromisoformat(expires_at)
                days_left = (expires_at - now).days

                # Try to DM the user
                try:
                    user = await self.fetch_user(int(discord_id))
                    if user:
                        embed = discord.Embed(
                            title="⚠️ License Expiring Soon",
                            description=f"Your **{product_name}** license expires in **{days_left} day{'s' if days_left != 1 else ''}**!",
                            color=discord.Color.orange()
                        )
                        embed.add_field(
                            name="Expires On",
                            value=expires_at.strftime("%B %d, %Y"),
                            inline=True
                        )
                        embed.add_field(
                            name="Renew Now",
                            value=f"Visit {STORE_URL} to renew your subscription",
                            inline=False
                        )
                        embed.set_footer(text="Renew before expiration to keep your access!")
                        await user.send(embed=embed)
                        print(f"Sent expiry warning to {user} for {product_name}")
                except discord.Forbidden:
                    print(f"Could not DM user {discord_id} (DMs disabled)")
                except discord.NotFound:
                    print(f"User {discord_id} not found")
                except Exception as e:
                    print(f"Error sending warning to {discord_id}: {e}")

                # Mark as warned regardless
                await mark_warning_notified(lic["license_key"])

        except Exception as e:
            print(f"Error in check_expiring_soon: {e}")

    @check_expiring_soon.before_loop
    async def before_check_expiring(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=10)
    async def process_shopify_notifications(self):
        """Background task to process pending Shopify order notifications."""
        await self.wait_until_ready()

        try:
            # Get pending notifications from the API (now stored in database!)
            port = int(os.getenv("PORT", 8080))
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://localhost:{port}/shopify/pending") as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            notifications = data.get("notifications", [])

            for notif in notifications:
                notification_id = notif.get("id")  # Database ID for tracking
                discord_id = notif.get("discord_id")
                license_key = notif.get("license_key")
                expires_at = notif.get("expires_at")
                product = notif.get("product", "saints-gen")
                customer_name = notif.get("customer_name", "Customer")
                order_number = notif.get("order_number", "Unknown")

                product_name = get_product_name(product)
                print(f"[NOTIF] Processing order #{order_number}: discord_id={discord_id}, product={product}")

                # Try to find the user and assign role
                user = None
                role_added = False
                delivery_success = False
                error_message = None

                # Check if discord_id is numeric (user ID) or username
                if discord_id and discord_id.isdigit():
                    try:
                        user = await self.fetch_user(int(discord_id))
                        print(f"[NOTIF] Found Discord user: {user} (ID: {user.id})")
                    except discord.NotFound:
                        error_message = f"User not found: {discord_id}"
                        print(f"[NOTIF] Could not find user with ID {discord_id}")
                    except Exception as e:
                        error_message = str(e)
                        print(f"[NOTIF] Error fetching user {discord_id}: {e}")
                else:
                    error_message = f"Invalid Discord ID format: {discord_id}"
                    print(f"[NOTIF] Invalid Discord ID format: {discord_id}")

                # Assign role if we have guild configured
                if user and GUILD_ID:
                    role_id = get_role_id_for_product(product)
                    print(f"[NOTIF] Attempting role assignment: GUILD_ID={GUILD_ID}, role_id={role_id}")
                    if role_id:
                        try:
                            guild = self.get_guild(GUILD_ID)
                            if guild:
                                print(f"[NOTIF] Found guild: {guild.name}")
                                member = await guild.fetch_member(user.id)
                                role = guild.get_role(role_id)
                                print(f"[NOTIF] Member: {member}, Role: {role}")
                                if member and role and role not in member.roles:
                                    await member.add_roles(role, reason=f"Shopify order #{order_number}")
                                    role_added = True
                                    print(f"[NOTIF] SUCCESS: Added {product_name} role to {user}")
                                elif member and role and role in member.roles:
                                    print(f"[NOTIF] User already has the role")
                                    role_added = True  # Already has it
                            else:
                                print(f"[NOTIF] Could not find guild with ID {GUILD_ID}")
                        except discord.NotFound:
                            print(f"[NOTIF] User {discord_id} not in guild (NotFound)")
                        except Exception as e:
                            print(f"[NOTIF] Error adding role to {discord_id}: {e}")
                    else:
                        print(f"[NOTIF] No role_id configured for {product}")
                else:
                    if not user:
                        print(f"[NOTIF] No user found, skipping role assignment")
                    if not GUILD_ID:
                        print(f"[NOTIF] GUILD_ID not configured")

                # Send DM with activation instructions
                if user:
                    try:
                        embed = discord.Embed(
                            title=f"Your {product_name} License",
                            description=f"Thank you for your purchase! Order #{order_number}",
                            color=discord.Color.green()
                        )
                        embed.add_field(
                            name="Your Discord ID",
                            value=f"```{discord_id}```",
                            inline=False
                        )
                        embed.add_field(
                            name="Expires",
                            value=expires_at.split("T")[0] if "T" in str(expires_at) else str(expires_at),
                            inline=True
                        )
                        embed.add_field(
                            name="How to Activate",
                            value=f"1. Open {product_name}\n2. Enter your Discord ID when prompted\n3. Click Activate",
                            inline=False
                        )
                        if role_added:
                            embed.set_footer(text=f"Your {product_name} role has been added!")

                        await user.send(embed=embed)
                        print(f"Sent license DM to {user} for order #{order_number}")
                        delivery_success = True
                    except discord.Forbidden:
                        error_message = "DMs disabled"
                        print(f"Could not DM {user} (DMs disabled)")
                        # Still mark as success since the license exists and role was added
                        delivery_success = True  # License is in DB, they can use /mykey
                    except Exception as e:
                        error_message = str(e)
                        print(f"Error sending DM to {user}: {e}")
                else:
                    print(f"Could not deliver license for order #{order_number} - Discord user not found: {discord_id}")

                # Mark notification as delivered or failed in the database
                if notification_id:
                    try:
                        async with aiohttp.ClientSession() as session:
                            if delivery_success:
                                await session.post(f"http://localhost:{port}/shopify/notification/{notification_id}/delivered")
                                print(f"Marked notification {notification_id} as delivered")
                            else:
                                await session.post(
                                    f"http://localhost:{port}/shopify/notification/{notification_id}/failed",
                                    params={"error": error_message or "Unknown error"}
                                )
                                print(f"Marked notification {notification_id} as failed: {error_message}")
                    except Exception as e:
                        print(f"Error updating notification status: {e}")

        except aiohttp.ClientError:
            pass  # API not ready yet, will retry
        except Exception as e:
            print(f"Error processing Shopify notifications: {e}")

    @process_shopify_notifications.before_loop
    async def before_shopify_notifications(self):
        await self.wait_until_ready()
        # Wait a bit for API to start
        await asyncio.sleep(5)


bot = LicenseBot()


# ==================== AUTO-HELP FOR REDEMPTION ====================
# Keywords that suggest a user is confused about how to redeem
REDEEM_HELP_KEYWORDS = [
    "how do i activate",
    "how do i redeem",
    "how to activate",
    "how to redeem",
    "i bought",
    "i purchased",
    "just bought",
    "just purchased",
    "where do i",
    "what do i do",
    "how do i use",
    "how do i get my",
    "activate my license",
    "redeem my license",
    "i paid",
    "after buying",
    "after purchase",
    "now what",
]

# Cooldown tracking for auto-help (user_id -> last_response_time)
auto_help_cooldowns = {}
AUTO_HELP_COOLDOWN_SECONDS = 300  # 5 minutes


@bot.event
async def on_message(message: discord.Message):
    # Ignore bot messages
    if message.author.bot:
        return

    # Check if message contains any help keywords
    content_lower = message.content.lower()

    if any(keyword in content_lower for keyword in REDEEM_HELP_KEYWORDS):
        # Check cooldown
        user_id = message.author.id
        current_time = time.time()
        last_response = auto_help_cooldowns.get(user_id, 0)

        if current_time - last_response >= AUTO_HELP_COOLDOWN_SECONDS:
            # Update cooldown
            auto_help_cooldowns[user_id] = current_time

            embed = discord.Embed(
                title="How to Redeem Your Purchase",
                description="Thanks for your purchase! Follow these steps to activate your license:",
                color=discord.Color.blue()
            )
            embed.add_field(
                name="Step 1",
                value="Make sure you're using the **same email** you purchased with",
                inline=False
            )
            embed.add_field(
                name="Step 2",
                value="Use the command:\n```/redeem your@email.com```\nReplace `your@email.com` with your purchase email",
                inline=False
            )
            embed.set_footer(text="Still having issues? Contact support!")

            await message.reply(embed=embed, mention_author=False)

    # Process commands if any
    await bot.process_commands(message)


def is_admin():
    """Check if user is an admin."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id in ADMIN_IDS
    return app_commands.check(predicate)


# ==================== HELPER FUNCTIONS ====================

def get_role_id_for_product(product: str) -> int:
    """Get the role ID for a given product."""
    if product == "saints-shot":
        return SAINTS_SHOT_ROLE_ID
    elif product == "saintx":
        return SAINTX_ROLE_ID
    else:  # saints-gen or default
        return SUBSCRIBER_ROLE_ID


def get_product_name(product: str) -> str:
    """Get display name for a product."""
    names = {
        "saints-gen": "Saint's Gen",
        "saints-gen-gen": "Saint's Gen - Gen Mode",
        "saints-gen-xp": "Saint's Gen - XP Mode",
        "saints-shot": "Saint's Shot",
        "saintx": "SaintX"
    }
    return names.get(product, product)


# Audit log channel
AUDIT_LOG_CHANNEL_ID = 1290509478445322292

# Status channel
STATUS_CHANNEL_ID = 1476776091963228303
STATUS_MESSAGE_ID = None  # Will be set when bot sends/finds the status message

# ==================== PRODUCT STATUS ====================
# Status values: "undetected", "risky", "detected", "maintenance"
PRODUCT_STATUS = {
    "saints-gen-gen": "risky",      # Saint's Gen - Gen Mode
    "saints-gen-xp": "undetected",  # Saint's Gen - XP Mode
    "saints-shot": "undetected",
    "saintx": "undetected"  # SaintX is now live
}

STATUS_DISPLAY = {
    "undetected": {"emoji": "🟢", "label": "Undetected", "color": discord.Color.green()},
    "risky": {"emoji": "🟡", "label": "Use At Your Own Risk", "color": discord.Color.gold()},
    "detected": {"emoji": "🔴", "label": "Detected", "color": discord.Color.red()},
    "maintenance": {"emoji": "⚠️", "label": "Under Maintenance", "color": discord.Color.orange()}
}


def build_status_embed() -> discord.Embed:
    """Build the status embed showing all product statuses."""
    embed = discord.Embed(
        title="🛡️ Product Status",
        description="Current detection status for all products",
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow()
    )

    # Saint's Gen with sub-modes
    gen_mode_status = PRODUCT_STATUS.get("saints-gen-gen", "undetected")
    xp_mode_status = PRODUCT_STATUS.get("saints-gen-xp", "undetected")
    gen_mode_info = STATUS_DISPLAY.get(gen_mode_status, STATUS_DISPLAY["undetected"])
    xp_mode_info = STATUS_DISPLAY.get(xp_mode_status, STATUS_DISPLAY["undetected"])

    embed.add_field(
        name="Saint's Gen",
        value=f"Gen Mode: {gen_mode_info['emoji']} {gen_mode_info['label']}\nXP Mode: {xp_mode_info['emoji']} {xp_mode_info['label']}",
        inline=False
    )

    # Saint's Shot
    shot_status = PRODUCT_STATUS.get("saints-shot", "undetected")
    shot_info = STATUS_DISPLAY.get(shot_status, STATUS_DISPLAY["undetected"])
    embed.add_field(
        name="Saint's Shot",
        value=f"{shot_info['emoji']} {shot_info['label']}",
        inline=False
    )

    # SaintX
    saintx_status = PRODUCT_STATUS.get("saintx", "undetected")
    saintx_info = STATUS_DISPLAY.get(saintx_status, STATUS_DISPLAY["undetected"])
    embed.add_field(
        name="SaintX",
        value=f"{saintx_info['emoji']} {saintx_info['label']}",
        inline=False
    )

    embed.set_footer(text="Last updated")
    return embed


async def update_status_message():
    """Update or create the status message in the status channel."""
    global STATUS_MESSAGE_ID

    channel = bot.get_channel(STATUS_CHANNEL_ID)
    if not channel:
        print(f"Could not find status channel {STATUS_CHANNEL_ID}")
        return

    embed = build_status_embed()

    # Try to edit existing message
    if STATUS_MESSAGE_ID:
        try:
            message = await channel.fetch_message(STATUS_MESSAGE_ID)
            await message.edit(embed=embed)
            print("Updated status message")
            return
        except discord.NotFound:
            STATUS_MESSAGE_ID = None
        except Exception as e:
            print(f"Error editing status message: {e}")

    # Look for existing bot message in channel
    try:
        async for message in channel.history(limit=50):
            if message.author == bot.user and message.embeds:
                if message.embeds[0].title == "🛡️ Product Status":
                    STATUS_MESSAGE_ID = message.id
                    await message.edit(embed=embed)
                    print(f"Found and updated existing status message: {STATUS_MESSAGE_ID}")
                    return
    except Exception as e:
        print(f"Error searching for status message: {e}")

    # Send new message
    try:
        message = await channel.send(embed=embed)
        STATUS_MESSAGE_ID = message.id
        print(f"Sent new status message: {STATUS_MESSAGE_ID}")
    except Exception as e:
        print(f"Error sending status message: {e}")


async def send_audit_log(title: str, description: str, admin: discord.User, color: discord.Color = discord.Color.blue(), fields: list = None):
    """Send an audit log entry to the audit channel."""
    try:
        channel = bot.get_channel(AUDIT_LOG_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title=title,
                description=description,
                color=color,
                timestamp=datetime.utcnow()
            )
            embed.set_author(name=f"{admin.display_name}", icon_url=admin.display_avatar.url)
            if fields:
                for field in fields:
                    embed.add_field(name=field["name"], value=field["value"], inline=field.get("inline", True))
            embed.set_footer(text=f"Admin ID: {admin.id}")
            await channel.send(embed=embed)
    except Exception as e:
        print(f"Failed to send audit log: {e}")


# ==================== ADMIN COMMANDS ====================

# Product choices for the generate command
PRODUCT_CHOICES = [
    app_commands.Choice(name="Saint's Gen", value="saints-gen"),
    app_commands.Choice(name="Saint's Shot", value="saints-shot"),
    app_commands.Choice(name="SaintX", value="saintx"),
]


@bot.tree.command(name="generate", description="Give a user access to a product (adds to existing subscription)")
@is_admin()
@app_commands.describe(
    user="The Discord user to give access to",
    days="Number of days of access (adds to existing subscription if they have one)",
    product="Which product to give access for"
)
@app_commands.choices(product=PRODUCT_CHOICES)
async def generate(interaction: discord.Interaction, user: discord.User, days: int, product: str = "saints-gen"):
    """Give a user access to a product (adds to existing subscription if they have one)."""
    if days < 1:
        await interaction.response.send_message("Days must be at least 1.", ephemeral=True)
        return

    if days > 36500:  # 100 years max
        await interaction.response.send_message("Maximum is 36500 days (100 years).", ephemeral=True)
        return

    # Product display name
    product_name = get_product_name(product)
    discord_id = str(user.id)

    # Check if user already has an active license for this product
    existing_license = await get_license_by_user(discord_id, product)
    extended = False

    if existing_license and not existing_license.get("revoked"):
        # User has existing license - extend it
        new_expiry = await extend_user_license_for_product(discord_id, days, product)
        if new_expiry:
            expires_at = datetime.fromisoformat(new_expiry)
            extended = True
        else:
            await interaction.response.send_message(
                f"Failed to extend {user.mention}'s existing subscription. Try again.", ephemeral=True)
            return
    else:
        # No existing license - create new one
        from datetime import timedelta
        expires_at = datetime.utcnow() + timedelta(days=days)

        # Generate internal key (user never sees this)
        license_key, _ = generate_license_key(SECRET_KEY, discord_id, days, user.name, "")

        # Store in database
        success = await add_license(
            license_key=license_key,
            discord_id=discord_id,
            discord_name=str(user),
            expires_at=expires_at,
            product=product
        )

        if not success:
            await interaction.response.send_message(
                "Failed to create subscription. Try again.", ephemeral=True)
            return

    # Give appropriate role based on product
    role_added = False
    role_id = get_role_id_for_product(product)
    if GUILD_ID and role_id:
        try:
            guild = bot.get_guild(GUILD_ID)
            if guild:
                member = await guild.fetch_member(user.id)
                role = guild.get_role(role_id)
                if member and role and role not in member.roles:
                    await member.add_roles(role, reason=f"Subscription added for {product}")
                    role_added = True
        except Exception as e:
            print(f"Could not add role to {user}: {e}")

    # Create embed response (no license key shown)
    embed = discord.Embed(
        title="Subscription Extended" if extended else "Subscription Added",
        color=discord.Color.green()
    )
    embed.add_field(name="Product", value=product_name, inline=True)
    embed.add_field(name="User", value=f"{user.mention}", inline=True)
    embed.add_field(name="Discord ID", value=f"`{user.id}`", inline=False)
    embed.add_field(name="Days Added", value=f"+{days} days", inline=True)
    embed.add_field(name="Expires", value=expires_at.strftime("%Y-%m-%d %H:%M UTC"), inline=True)
    if role_added:
        embed.add_field(name="Role", value="Added", inline=True)
    if extended:
        embed.set_footer(text="Extended existing subscription")

    await interaction.response.send_message(embed=embed, ephemeral=True)

    # Audit log
    await send_audit_log(
        title="License Extended" if extended else "License Generated",
        description=f"{'Extended' if extended else 'Generated'} **{product_name}** license for {user.mention}",
        admin=interaction.user,
        color=discord.Color.green(),
        fields=[
            {"name": "User", "value": f"{user} (`{user.id}`)", "inline": True},
            {"name": "Product", "value": product_name, "inline": True},
            {"name": "Days Added", "value": f"+{days}", "inline": True},
            {"name": "Expires", "value": expires_at.strftime("%Y-%m-%d"), "inline": True},
        ]
    )

    # DM the user (no key, just tell them to use Discord ID)
    try:
        user_embed = discord.Embed(
            title=f"{product_name} {'Extended' if extended else 'Access Granted'}!",
            description=f"{'Your subscription has been extended!' if extended else 'You now have access to the product.'}",
            color=discord.Color.green()
        )
        user_embed.add_field(name="Days Added", value=f"+{days} days", inline=True)
        user_embed.add_field(name="Expires", value=expires_at.strftime("%Y-%m-%d %H:%M UTC"), inline=True)
        user_embed.add_field(name="Your Discord ID", value=f"```{user.id}```", inline=False)
        user_embed.add_field(
            name="How to Activate",
            value=f"1. Open {product_name}\n2. Enter your Discord ID\n3. Click Activate",
            inline=False
        )
        await user.send(embed=user_embed)
    except discord.Forbidden:
        pass  # User has DMs disabled


@bot.tree.command(name="revoke", description="Revoke a license by key or user")
@is_admin()
@app_commands.describe(
    key="The license key to revoke (optional)",
    user="The user whose licenses to revoke (optional)"
)
async def revoke(
    interaction: discord.Interaction,
    key: Optional[str] = None,
    user: Optional[discord.User] = None
):
    """Revoke a license key or all keys for a user."""
    if not key and not user:
        await interaction.response.send_message(
            "Please provide either a license key or a user.", ephemeral=True)
        return

    if key:
        success = await revoke_license(key)
        if success:
            await interaction.response.send_message(
                f"License `{key[:20]}...` has been revoked.", ephemeral=True)
            await send_audit_log(
                title="License Revoked",
                description=f"Revoked license by key",
                admin=interaction.user,
                color=discord.Color.red(),
                fields=[{"name": "Key", "value": f"`{key[:20]}...`", "inline": False}]
            )
        else:
            await interaction.response.send_message(
                "License not found.", ephemeral=True)
    else:
        count = await revoke_user_licenses(str(user.id))
        await interaction.response.send_message(
            f"Revoked {count} license(s) for {user.mention}.", ephemeral=True)
        if count > 0:
            await send_audit_log(
                title="Licenses Revoked",
                description=f"Revoked all licenses for {user.mention}",
                admin=interaction.user,
                color=discord.Color.red(),
                fields=[
                    {"name": "User", "value": f"{user} (`{user.id}`)", "inline": True},
                    {"name": "Count", "value": str(count), "inline": True}
                ]
            )


@bot.tree.command(name="extend", description="Add or remove days from a license (use negative to remove)")
@is_admin()
@app_commands.describe(
    days="Number of days to add (use negative to remove days, e.g. -5)",
    key="The license key to modify (optional)",
    user="The user whose license to modify (optional)",
    product="Which product's license to modify (required when using user)"
)
@app_commands.choices(product=PRODUCT_CHOICES)
async def extend(
    interaction: discord.Interaction,
    days: int,
    key: Optional[str] = None,
    user: Optional[discord.User] = None,
    product: Optional[str] = None
):
    """Add or remove days from a license. Use negative days to reduce."""
    if not key and not user:
        await interaction.response.send_message(
            "Please provide either a license key or a user.", ephemeral=True)
        return

    if days == 0:
        await interaction.response.send_message(
            "Days cannot be 0.", ephemeral=True)
        return

    # Determine action word based on positive/negative
    action = "extended" if days > 0 else "reduced"
    days_display = f"+{days}" if days > 0 else str(days)

    if key:
        new_expiry = await extend_license(key, days)
        if new_expiry:
            expiry_dt = datetime.fromisoformat(new_expiry)
            await interaction.response.send_message(
                f"License {action} by **{days_display} days**.\nNew expiry: **{expiry_dt.strftime('%Y-%m-%d %H:%M UTC')}**", ephemeral=True)
            await send_audit_log(
                title=f"License {action.capitalize()}",
                description=f"Modified license by key",
                admin=interaction.user,
                color=discord.Color.gold(),
                fields=[
                    {"name": "Key", "value": f"`{key[:20]}...`", "inline": True},
                    {"name": "Days", "value": days_display, "inline": True},
                    {"name": "New Expiry", "value": expiry_dt.strftime('%Y-%m-%d'), "inline": True}
                ]
            )
        else:
            await interaction.response.send_message(
                "License not found.", ephemeral=True)
    else:
        # When using user, product is required
        if not product:
            await interaction.response.send_message(
                "Please select a product when modifying by user.", ephemeral=True)
            return

        new_expiry = await extend_user_license_for_product(str(user.id), days, product)
        product_name = get_product_name(product)
        if new_expiry:
            expiry_dt = datetime.fromisoformat(new_expiry)
            await interaction.response.send_message(
                f"{action.capitalize()} {user.mention}'s **{product_name}** license by **{days_display} days**.\nNew expiry: **{expiry_dt.strftime('%Y-%m-%d %H:%M UTC')}**", ephemeral=True)
            await send_audit_log(
                title=f"License {action.capitalize()}",
                description=f"Modified {user.mention}'s **{product_name}** license",
                admin=interaction.user,
                color=discord.Color.gold(),
                fields=[
                    {"name": "User", "value": f"{user} (`{user.id}`)", "inline": True},
                    {"name": "Product", "value": product_name, "inline": True},
                    {"name": "Days", "value": days_display, "inline": True},
                    {"name": "New Expiry", "value": expiry_dt.strftime('%Y-%m-%d'), "inline": True}
                ]
            )
        else:
            await interaction.response.send_message(
                f"{user.mention} has no **{product_name}** license to modify.", ephemeral=True)


@bot.tree.command(name="list", description="List all active licenses")
@is_admin()
@app_commands.describe(product="Filter by product (optional)")
@app_commands.choices(product=PRODUCT_CHOICES)
async def list_licenses(interaction: discord.Interaction, product: str = None):
    """List all active licenses."""
    licenses = await get_all_active_licenses(product)

    if not licenses:
        await interaction.response.send_message("No active licenses.", ephemeral=True)
        return

    title = "Active Licenses"
    if product:
        product_name = get_product_name(product)
        title = f"Active {product_name} Licenses"

    embed = discord.Embed(
        title=title,
        color=discord.Color.blue()
    )

    # Show up to 10 licenses in the embed
    for lic in licenses[:10]:
        expires = lic["expires_at"]
        if isinstance(expires, str):
            expires = datetime.fromisoformat(expires)
        days_left = (expires - datetime.utcnow()).days
        hwid_status = "🔒" if lic.get("hwid") else "🔓"
        # Product tag
        prod = lic.get("product", "saints-gen")
        if prod == "saints-gen":
            prod_tag = "[Gen]"
        elif prod == "saints-shot":
            prod_tag = "[Shot]"
        else:
            prod_tag = "[X]"
        embed.add_field(
            name=f"{hwid_status} {prod_tag} {lic['discord_name']}",
            value=f"Expires: {expires.strftime('%Y-%m-%d')} ({days_left}d left)",
            inline=True
        )

    if len(licenses) > 10:
        embed.set_footer(text=f"Showing 10 of {len(licenses)} active licenses | 🔒=bound 🔓=unbound")
    else:
        embed.set_footer(text="🔒 = hardware bound | 🔓 = not yet activated")

    # Add stats
    stats = await get_license_stats(product)
    embed.description = f"**Stats:** {stats['active']} active, {stats['expired']} expired, {stats['revoked']} revoked"

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stats", description="[Admin] View license and referral statistics")
@is_admin()
async def stats(interaction: discord.Interaction):
    """View comprehensive statistics for all products."""
    await interaction.response.defer(ephemeral=True)

    # Get stats for each product
    gen_stats = await get_license_stats("saints-gen")
    shot_stats = await get_license_stats("saints-shot")
    all_stats = await get_license_stats()

    embed = discord.Embed(
        title="📊 License Statistics",
        color=discord.Color.gold(),
        timestamp=datetime.utcnow()
    )

    # Overall stats
    embed.add_field(
        name="📈 Overall",
        value=f"**Total:** {all_stats['total']}\n"
              f"**Active:** {all_stats['active']}\n"
              f"**Expired:** {all_stats['expired']}\n"
              f"**Revoked:** {all_stats['revoked']}",
        inline=True
    )

    # Saint's Gen stats
    embed.add_field(
        name="🎮 Saint's Gen",
        value=f"**Total:** {gen_stats['total']}\n"
              f"**Active:** {gen_stats['active']}\n"
              f"**Expired:** {gen_stats['expired']}\n"
              f"**Revoked:** {gen_stats['revoked']}",
        inline=True
    )

    # Saint's Shot stats
    embed.add_field(
        name="🏀 Saint's Shot",
        value=f"**Total:** {shot_stats['total']}\n"
              f"**Active:** {shot_stats['active']}\n"
              f"**Expired:** {shot_stats['expired']}\n"
              f"**Revoked:** {shot_stats['revoked']}",
        inline=True
    )

    # Get referral stats from database
    try:
        from database import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            total_referrals = await conn.fetchval("SELECT COUNT(*) FROM referrals")
            total_days_given = await conn.fetchval("SELECT COALESCE(SUM(days_awarded), 0) FROM referrals")
            unique_referrers = await conn.fetchval("SELECT COUNT(DISTINCT referrer_id) FROM referrals")
            unique_referred = await conn.fetchval("SELECT COUNT(DISTINCT referred_id) FROM referrals")

        embed.add_field(
            name="🤝 Referrals",
            value=f"**Total Referrals:** {total_referrals}\n"
                  f"**Days Awarded:** {total_days_given}\n"
                  f"**Unique Referrers:** {unique_referrers}\n"
                  f"**Users Referred:** {unique_referred}",
            inline=True
        )
    except Exception as e:
        print(f"Error getting referral stats: {e}")

    # Get redemption stats
    try:
        async with pool.acquire() as conn:
            total_purchases = await conn.fetchval("SELECT COUNT(*) FROM purchases")
            redeemed_purchases = await conn.fetchval("SELECT COUNT(*) FROM purchases WHERE redeemed = true")
            pending_purchases = total_purchases - redeemed_purchases

        embed.add_field(
            name="💳 Purchases",
            value=f"**Total:** {total_purchases}\n"
                  f"**Redeemed:** {redeemed_purchases}\n"
                  f"**Pending:** {pending_purchases}",
            inline=True
        )
    except Exception as e:
        print(f"Error getting purchase stats: {e}")

    embed.set_footer(text="Stats updated")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="check", description="[Admin] Check a user's subscription status")
@is_admin()
@app_commands.describe(user="The Discord user to check")
async def check(interaction: discord.Interaction, user: discord.User):
    """Check a user's subscription status (admin only)."""
    discord_id = str(user.id)
    now = datetime.utcnow()

    # Get all licenses for user
    all_licenses = await get_all_licenses_for_user(discord_id)

    # Build embed
    embed = discord.Embed(
        title="User License Check",
        color=discord.Color.blue()
    )
    embed.set_author(name=f"{user.display_name} ({user.id})", icon_url=user.display_avatar.url)
    embed.set_thumbnail(url=user.display_avatar.url)

    if not all_licenses:
        embed.description = f"{user.mention} has no licenses (past or present)."
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Group licenses by product
    products = ["saints-gen", "saints-shot", "saintx"]

    for prod in products:
        prod_name = get_product_name(prod)
        prod_licenses = [
            lic for lic in all_licenses
            if lic.get("product") == prod
        ]

        if not prod_licenses:
            embed.add_field(
                name=f"⚫ {prod_name}",
                value="No license history",
                inline=False
            )
            continue

        # Find the best active license
        active_licenses = [
            lic for lic in prod_licenses
            if not lic.get("revoked")
        ]

        if active_licenses:
            best = max(active_licenses, key=lambda x: x["expires_at"] if isinstance(x["expires_at"], datetime) else datetime.fromisoformat(str(x["expires_at"])))
            expires = best["expires_at"]
            if isinstance(expires, str):
                expires = datetime.fromisoformat(expires)

            hwid = best.get("hwid")
            hwid_status = f"`{hwid[:12]}...`" if hwid else "Not bound"

            if expires > now:
                days_left = (expires - now).days
                if days_left > 30:
                    status_emoji = "🟢"
                elif days_left > 7:
                    status_emoji = "🟡"
                else:
                    status_emoji = "🟠"

                embed.add_field(
                    name=f"{status_emoji} {prod_name}",
                    value=f"**Status:** Active\n**Days Left:** {days_left}\n**Expires:** {expires.strftime('%b %d, %Y')}\n**HWID:** {hwid_status}",
                    inline=False
                )
            else:
                embed.add_field(
                    name=f"🔴 {prod_name}",
                    value=f"**Status:** Expired\n**Expired:** {expires.strftime('%b %d, %Y')}\n**HWID:** {hwid_status}",
                    inline=False
                )
        else:
            # All licenses revoked
            embed.add_field(
                name=f"⛔ {prod_name}",
                value="**Status:** Revoked",
                inline=False
            )

    # Add total license count
    embed.set_footer(text=f"Total licenses: {len(all_licenses)}")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="reset-hwid", description="Reset hardware binding for a license (allows activation on new PC)")
@is_admin()
@app_commands.describe(
    key="The license key to reset (optional)",
    user="The user whose license to reset (optional)"
)
async def reset_hwid(
    interaction: discord.Interaction,
    key: Optional[str] = None,
    user: Optional[discord.User] = None
):
    """Reset hardware ID binding so the license can be activated on a new machine."""
    if not key and not user:
        await interaction.response.send_message(
            "Please provide either a license key or a user.", ephemeral=True)
        return

    if key:
        # Check current binding first
        current_hwid = await get_hwid_by_key(key)
        if not current_hwid:
            await interaction.response.send_message(
                "This license is not bound to any hardware yet.", ephemeral=True)
            return

        success = await reset_hwid_by_key(key)
        if success:
            await interaction.response.send_message(
                f"Hardware binding reset for license `{key[:20]}...`\n"
                f"Previous HWID: `{current_hwid[:12]}...`\n"
                f"The user can now activate on a new PC.", ephemeral=True)
            await send_audit_log(
                title="HWID Reset",
                description="Reset hardware binding by key",
                admin=interaction.user,
                color=discord.Color.orange(),
                fields=[
                    {"name": "Key", "value": f"`{key[:20]}...`", "inline": True},
                    {"name": "Previous HWID", "value": f"`{current_hwid[:12]}...`", "inline": True}
                ]
            )
        else:
            await interaction.response.send_message(
                "License not found.", ephemeral=True)
    else:
        count = await reset_hwid_by_user(str(user.id))
        if count > 0:
            await interaction.response.send_message(
                f"Reset hardware binding for {count} license(s) for {user.mention}.\n"
                f"They can now activate on a new PC.", ephemeral=True)
            await send_audit_log(
                title="HWID Reset",
                description=f"Reset hardware binding for {user.mention}",
                admin=interaction.user,
                color=discord.Color.orange(),
                fields=[
                    {"name": "User", "value": f"{user} (`{user.id}`)", "inline": True},
                    {"name": "Licenses Reset", "value": str(count), "inline": True}
                ]
            )
        else:
            await interaction.response.send_message(
                f"{user.mention} has no licenses to reset.", ephemeral=True)


@bot.tree.command(name="pending-orders", description="View pending/failed Shopify order notifications")
@is_admin()
async def pending_orders(interaction: discord.Interaction):
    """View pending and failed Shopify order notifications."""
    pending = await get_pending_notifications()
    failed = await get_failed_notifications()

    embed = discord.Embed(
        title="Shopify Order Notifications",
        color=discord.Color.blue()
    )

    if pending:
        pending_text = ""
        for notif in pending[:5]:
            pending_text += f"Order #{notif.get('order_number', 'N/A')} - <@{notif['discord_id']}> (Attempts: {notif.get('delivery_attempts', 0)})\n"
        if len(pending) > 5:
            pending_text += f"... and {len(pending) - 5} more"
        embed.add_field(name=f"Pending ({len(pending)})", value=pending_text or "None", inline=False)
    else:
        embed.add_field(name="Pending", value="No pending notifications", inline=False)

    if failed:
        failed_text = ""
        for notif in failed[:5]:
            error = notif.get('error_message', 'Unknown')[:50]
            failed_text += f"Order #{notif.get('order_number', 'N/A')} - {notif['discord_id']}\nError: {error}\n\n"
        if len(failed) > 5:
            failed_text += f"... and {len(failed) - 5} more"
        embed.add_field(name=f"Failed ({len(failed)})", value=failed_text or "None", inline=False)
    else:
        embed.add_field(name="Failed", value="No failed notifications", inline=False)

    embed.set_footer(text="Pending notifications retry automatically every 10 seconds")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="cleanup-duplicates", description="[Admin] Remove duplicate licenses, keeping the one with most days")
@is_admin()
async def cleanup_duplicates(interaction: discord.Interaction):
    """Find and delete duplicate licenses, keeping only the one with the most days remaining."""
    await interaction.response.defer(ephemeral=True)

    result = await cleanup_duplicate_licenses()

    if result["total_deleted"] == 0:
        embed = discord.Embed(
            title="No Duplicates Found",
            description="All users have only one license per product.",
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed)
        return

    embed = discord.Embed(
        title="Duplicate Cleanup Complete",
        description=f"Deleted **{result['total_deleted']}** duplicate license(s)",
        color=discord.Color.green()
    )

    # Show affected users (up to 10)
    for i, user_info in enumerate(result["affected_users"][:10]):
        expires = user_info["kept_expiry"]
        if isinstance(expires, str):
            expires = datetime.fromisoformat(expires)

        embed.add_field(
            name=f"{user_info['discord_name']} - {user_info['product']}",
            value=f"Deleted: {user_info['deleted_count']} | Kept expiry: {expires.strftime('%Y-%m-%d')}",
            inline=False
        )

    if len(result["affected_users"]) > 10:
        embed.set_footer(text=f"... and {len(result['affected_users']) - 10} more users")

    await interaction.followup.send(embed=embed)

    # Audit log
    await send_audit_log(
        title="Duplicate Licenses Cleaned",
        description=f"Removed {result['total_deleted']} duplicate licenses",
        admin=interaction.user,
        color=discord.Color.orange(),
        fields=[
            {"name": "Users Affected", "value": str(len(result["affected_users"])), "inline": True},
            {"name": "Licenses Deleted", "value": str(result["total_deleted"]), "inline": True}
        ]
    )


# ==================== USER COMMANDS ====================

@bot.tree.command(name="id", description="Get your Discord ID for activation")
async def get_id(interaction: discord.Interaction):
    """Show the user their Discord ID for use in programs."""
    user = interaction.user

    embed = discord.Embed(
        title="Your Discord ID",
        description="Use this ID to activate your subscription in the program.",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="Your ID",
        value=f"```{user.id}```",
        inline=False
    )
    embed.set_thumbnail(url=user.display_avatar.url)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="status", description="Check your subscription status")
async def status(interaction: discord.Interaction):
    """Check subscription status for all products."""
    user = interaction.user
    discord_id = str(user.id)
    now = datetime.utcnow()

    # Get all licenses for user
    all_licenses = await get_all_licenses_for_user(discord_id)

    # Filter to get best license per product (active, not revoked, latest expiry)
    products = ["saints-gen", "saints-shot", "saintx"]
    active_subs = {}

    for prod in products:
        prod_licenses = [
            lic for lic in all_licenses
            if lic.get("product") == prod and not lic.get("revoked")
        ]
        if prod_licenses:
            # Get the one with latest expiry
            best = max(prod_licenses, key=lambda x: x["expires_at"] if isinstance(x["expires_at"], datetime) else datetime.fromisoformat(str(x["expires_at"])))
            expires = best["expires_at"]
            if isinstance(expires, str):
                expires = datetime.fromisoformat(expires)

            # Check if this is a pending activation license
            pending_days = best.get("pending_days")
            if pending_days:
                active_subs[prod] = {"pending": True, "pending_days": pending_days}
            elif expires > now:
                active_subs[prod] = {"expires": expires, "days_left": (expires - now).days}

    # Build embed
    if active_subs:
        # Has at least one active subscription
        embed = discord.Embed(
            title=f"Subscription Status",
            color=discord.Color.gold()
        )
        embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
        embed.set_thumbnail(url=user.display_avatar.url)

        # Add each active subscription
        for prod in products:
            prod_name = get_product_name(prod)

            if prod in active_subs:
                sub = active_subs[prod]

                # Check if pending activation
                if sub.get("pending"):
                    embed.add_field(
                        name=f"🔵 {prod_name}",
                        value=f"**{sub['pending_days']}** days ready\nActivate by opening the program",
                        inline=True
                    )
                else:
                    days = sub["days_left"]
                    expires = sub["expires"]

                    # Status emoji and color indicator
                    if days > 30:
                        status_emoji = "🟢"
                    elif days > 7:
                        status_emoji = "🟡"
                    else:
                        status_emoji = "🟠"

                    embed.add_field(
                        name=f"{status_emoji} {prod_name}",
                        value=f"**{days}** days remaining\nExpires: {expires.strftime('%b %d, %Y')}",
                        inline=True
                    )
            else:
                embed.add_field(
                    name=f"⚫ {prod_name}",
                    value="Not subscribed",
                    inline=True
                )

    else:
        # No active subscriptions
        embed = discord.Embed(
            title="Subscription Status",
            description="You don't have any active subscriptions.",
            color=discord.Color.dark_gray()
        )
        embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
        embed.set_thumbnail(url=user.display_avatar.url)

        # Show all products as not subscribed
        for prod in products:
            prod_name = get_product_name(prod)
            embed.add_field(
                name=f"⚫ {prod_name}",
                value="Not subscribed",
                inline=True
            )

        embed.add_field(
            name="Get Access",
            value=f"Visit {STORE_URL} to purchase a subscription!",
            inline=False
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="link", description="Link your Shopify purchase to your Discord account")
@app_commands.describe(email="The email you used for your Shopify purchase")
async def link_purchase(interaction: discord.Interaction, email: str):
    """Link a Shopify purchase to claim your license."""
    await interaction.response.defer()

    email = email.lower().strip()
    discord_id = str(interaction.user.id)
    discord_name = str(interaction.user)

    # Check for pending order with this email
    pending = await get_pending_order_by_email(email)

    if not pending:
        embed = discord.Embed(
            title="No Order Found",
            description=f"No pending order found for **{email}**.\n\n"
                        "Make sure you're using the exact email from your Shopify purchase.",
            color=discord.Color.red()
        )
        embed.add_field(
            name="Already Linked?",
            value="If you've already linked your order, use `/status` to check your subscription.",
            inline=False
        )
        await interaction.followup.send(embed=embed)
        return

    product = pending["product"]
    days = pending["days"]
    order_number = pending.get("order_number", "Unknown")

    from datetime import timedelta

    # Check if user already has a license for this product - extend it if so
    existing_license = await get_license_by_user(discord_id, product)
    extended = False

    if existing_license and not existing_license.get("revoked"):
        # Extend existing license
        new_expiry = await extend_user_license_for_product(discord_id, days, product)
        if new_expiry:
            expires_at = datetime.fromisoformat(new_expiry)
            extended = True
        else:
            embed = discord.Embed(
                title="Error",
                description="Failed to extend license. Please contact support.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return
    else:
        # Create new license
        expires_at = datetime.utcnow() + timedelta(days=days)
        license_key, _ = generate_license_key(SECRET_KEY, discord_id, days, discord_name)

        # Add license to database
        await add_license(license_key, discord_id, discord_name, expires_at, product)

    # Mark pending order as claimed
    await claim_pending_order(pending["id"], discord_id)

    # Assign role
    try:
        guild = bot.get_guild(GUILD_ID)
        if guild:
            member = guild.get_member(interaction.user.id) or await guild.fetch_member(interaction.user.id)
            if member:
                role_id = get_role_id_for_product(product)
                if role_id:
                    role = guild.get_role(role_id)
                    if role:
                        await member.add_roles(role)
                        print(f"Added {product} role to {discord_name}")
    except Exception as e:
        print(f"Error assigning role: {e}")

    # Get product name
    prod_name = get_product_name(product)

    embed = discord.Embed(
        title="License Extended!" if extended else "Purchase Linked Successfully!",
        description=f"Your **{prod_name}** license has been {'extended' if extended else 'activated'}!",
        color=discord.Color.green()
    )
    embed.add_field(name="Order", value=f"#{order_number}", inline=True)
    embed.add_field(name="Days Added", value=f"+{days} days", inline=True)
    embed.add_field(name="Expires", value=expires_at.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(
        name="How to Use",
        value="Open the app and login with your Discord. Your account is now authorized!",
        inline=False
    )
    embed.set_footer(text="Thank you for your purchase!")

    await interaction.followup.send(embed=embed)

    # Also try to DM them
    try:
        dm_embed = discord.Embed(
            title=f"{prod_name} License {'Extended' if extended else 'Activated'}!",
            description=f"Your {'subscription has been extended' if extended else 'purchase has been linked to your Discord account'}.",
            color=discord.Color.green()
        )
        dm_embed.add_field(name="Order", value=f"#{order_number}", inline=True)
        dm_embed.add_field(name="Days Added", value=f"+{days} days", inline=True)
        dm_embed.add_field(name="Expires", value=expires_at.strftime("%Y-%m-%d"), inline=True)
        dm_embed.add_field(
            name="Login",
            value="Just open the app - it will recognize your Discord account automatically!",
            inline=False
        )
        await interaction.user.send(embed=dm_embed)
    except:
        pass  # DMs might be disabled


# ==================== REDEMPTION SYSTEM ====================

@bot.tree.command(name="redeem", description="Redeem your purchase using your email")
@app_commands.describe(email="The email you used for your Shopify purchase")
async def redeem(interaction: discord.Interaction, email: str):
    """Redeem a purchase using your email to get your license and role."""
    await interaction.response.defer(ephemeral=True)

    # Try to redeem by email
    purchase = await redeem_by_email(email.strip(), str(interaction.user.id))

    if not purchase:
        embed = discord.Embed(
            title="No Purchase Found",
            description=f"No unredeemed purchase found for **{email}**\n\n"
                        "Make sure you're using the exact email from your Shopify purchase.\n"
                        "If you already redeemed, use `/status` to check your license.",
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed)
        return

    # Purchase found - check if user already has a license for this product
    product = purchase["product"]
    days = purchase["days"]
    customer_name = purchase.get("customer_name") or interaction.user.display_name

    from datetime import timedelta

    # Check for existing license
    existing_license = await get_license_by_user(str(interaction.user.id), product)

    if existing_license and not existing_license.get("revoked"):
        # User already has a license - extend it
        new_expiry = await extend_user_license_for_product(str(interaction.user.id), days, product)
        if new_expiry:
            expires_at = datetime.fromisoformat(new_expiry)
            extended = True
            pending_activation = False
        else:
            embed = discord.Embed(
                title="Error",
                description="Failed to extend license. Please contact support.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return
    else:
        # No existing license - create new one with pending activation
        # Countdown doesn't start until they use the program
        extended = False
        pending_activation = True
        # Set a placeholder far-future date - real expiry set on first program activation
        expires_at = datetime(2099, 12, 31, 23, 59, 59)

        license_key, _ = generate_license_key(
            SECRET_KEY,
            str(interaction.user.id),
            days,
            customer_name
        )

        # Save license to database with pending_days
        success = await add_license(
            license_key=license_key,
            discord_id=str(interaction.user.id),
            discord_name=interaction.user.display_name,
            expires_at=expires_at,
            product=product,
            pending_days=days
        )

        if not success:
            embed = discord.Embed(
                title="Error",
                description="Failed to create license. Please contact support.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return

    # Assign role
    role_assigned = False
    role_name = ""
    if GUILD_ID:
        try:
            guild = bot.get_guild(GUILD_ID)
            if guild:
                member = guild.get_member(interaction.user.id)
                if member:
                    role_id = get_role_id_for_product(product)
                    if role_id:
                        role = guild.get_role(role_id)
                        if role:
                            await member.add_roles(role, reason=f"Redeemed purchase: {email}")
                            role_assigned = True
                            role_name = role.name
        except Exception as e:
            print(f"Error assigning role: {e}")

    # Product name for display
    product_name = get_product_name(product)

    # Send success embed
    if extended:
        embed = discord.Embed(
            title="License Extended!",
            description=f"**+{days} days** added to your **{product_name}** license!",
            color=discord.Color.green()
        )
        embed.add_field(name="Product", value=product_name, inline=True)
        embed.add_field(name="Days Added", value=f"+{days} days", inline=True)
        embed.add_field(name="Expires", value=expires_at.strftime("%B %d, %Y"), inline=True)
    else:
        embed = discord.Embed(
            title="Purchase Redeemed!",
            description=f"Your **{product_name}** license is ready!",
            color=discord.Color.green()
        )
        embed.add_field(name="Product", value=product_name, inline=True)
        embed.add_field(name="Duration", value=f"{days} days", inline=True)
        embed.add_field(name="Activation", value="Countdown starts when you open the program", inline=False)

    if role_assigned:
        embed.add_field(name="Role", value=f"✅ {role_name} assigned", inline=False)

    # Product-specific instructions link
    if product == "saints-gen":
        instructions_link = "https://discordapp.com/channels/1290387028185448469/1467010934613737516"
    else:
        instructions_link = "https://discordapp.com/channels/1290387028185448469/1469757937382723727"

    embed.add_field(
        name="Next Steps",
        value=f"Go to {instructions_link} for further instructions",
        inline=False
    )

    await interaction.followup.send(embed=embed)

    # Also DM the user their license info
    try:
        if extended:
            dm_embed = discord.Embed(
                title=f"{product_name} License Extended!",
                description=f"**+{days} days** added to your license!",
                color=discord.Color.green()
            )
            dm_embed.add_field(name="Product", value=product_name, inline=True)
            dm_embed.add_field(name="Days Added", value=f"+{days} days", inline=True)
            dm_embed.add_field(name="Expires", value=expires_at.strftime("%B %d, %Y"), inline=True)
        else:
            dm_embed = discord.Embed(
                title=f"{product_name} License Ready!",
                description="Thank you for your purchase!",
                color=discord.Color.green()
            )
            dm_embed.add_field(name="Product", value=product_name, inline=True)
            dm_embed.add_field(name="Duration", value=f"{days} days", inline=True)
            dm_embed.add_field(name="Activation", value="Your countdown starts when you first open the program", inline=False)
        dm_embed.add_field(
            name="Next Steps",
            value=f"Go to {instructions_link} for further instructions",
            inline=False
        )
        await interaction.user.send(embed=dm_embed)
    except:
        pass  # DMs might be disabled

    print(f"Purchase redeemed by {interaction.user} ({interaction.user.id}) - {email} - {product_name} {days} days")

    # Log to redemption log channel
    try:
        log_channel = bot.get_channel(1290509478445322292)
        if log_channel:
            log_embed = discord.Embed(
                title="License Extended" if extended else "New License",
                color=discord.Color.blue()
            )
            log_embed.add_field(name="User", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
            log_embed.add_field(name="Product", value=product_name, inline=True)
            log_embed.add_field(name="Days", value=f"+{days}", inline=True)
            log_embed.add_field(name="Expires", value=expires_at.strftime("%B %d, %Y"), inline=True)
            log_embed.add_field(name="Email", value=f"||{email}||", inline=False)
            log_embed.set_footer(text=f"Order: {purchase.get('order_number', 'N/A')}")
            log_embed.timestamp = datetime.utcnow()
            await log_channel.send(embed=log_embed)
    except Exception as e:
        print(f"Failed to log redemption: {e}")


# ==================== REFERRAL SYSTEM ====================

# Referral rewards for Saint's Shot (based on how many times the user has been referred)
REFERRAL_REWARDS = {
    1: 7,  # 1st referral: 7 days
    2: 2,  # 2nd referral: 2 days
    3: 2,  # 3rd referral: 2 days
    4: 2,  # 4th referral: 2 days
    5: 2,  # 5th referral: 2 days (cap)
}
MAX_REFERRALS = 5


@bot.tree.command(name="referral", description="[Admin] Record a mutual referral between two users for Saint's Shot")
@is_admin()
@app_commands.describe(
    user1="First user in the referral exchange",
    user2="Second user in the referral exchange"
)
async def referral(interaction: discord.Interaction, user1: discord.User, user2: discord.User):
    """Record a mutual referral between two users. Both users get days and referral counts updated."""
    product = "saints-shot"
    product_name = "Saint's Shot"

    # Can't refer yourself
    if user1.id == user2.id:
        await interaction.response.send_message(
            "A user cannot refer themselves!", ephemeral=True)
        return

    # Check if both users have active Saint's Shot licenses
    user1_has_license = await has_active_license_for_product(str(user1.id), product)
    user2_has_license = await has_active_license_for_product(str(user2.id), product)

    if not user1_has_license and not user2_has_license:
        await interaction.response.send_message(
            f"Both users need an active **{product_name}** license to receive referral bonus.", ephemeral=True)
        return
    elif not user1_has_license:
        await interaction.response.send_message(
            f"{user1.mention} needs an active **{product_name}** license to receive referral bonus.", ephemeral=True)
        return
    elif not user2_has_license:
        await interaction.response.send_message(
            f"{user2.mention} needs an active **{product_name}** license to receive referral bonus.", ephemeral=True)
        return

    # Check if they've already referred each other
    user1_referred_by_user2 = await has_been_referred_by(str(user1.id), str(user2.id), product)
    user2_referred_by_user1 = await has_been_referred_by(str(user2.id), str(user1.id), product)

    if user1_referred_by_user2 and user2_referred_by_user1:
        await interaction.response.send_message(
            f"{user1.mention} and {user2.mention} have already exchanged referrals!", ephemeral=True)
        return

    # Check referral counts for both users
    user1_times_referred = await get_referral_count_received(str(user1.id), product)
    user2_times_referred = await get_referral_count_received(str(user2.id), product)

    # Track what we'll do
    user1_gets_days = False
    user2_gets_days = False
    user1_days = 0
    user2_days = 0
    user1_referral_num = 0
    user2_referral_num = 0

    # Process user1 receiving referral from user2 (if not already done and not at cap)
    if not user1_referred_by_user2 and user1_times_referred < MAX_REFERRALS:
        user1_referral_num = user1_times_referred + 1
        user1_days = REFERRAL_REWARDS.get(user1_referral_num, 2)
        user1_gets_days = True

    # Process user2 receiving referral from user1 (if not already done and not at cap)
    if not user2_referred_by_user1 and user2_times_referred < MAX_REFERRALS:
        user2_referral_num = user2_times_referred + 1
        user2_days = REFERRAL_REWARDS.get(user2_referral_num, 2)
        user2_gets_days = True

    if not user1_gets_days and not user2_gets_days:
        await interaction.response.send_message(
            f"Both users have either reached the maximum referrals ({MAX_REFERRALS}) or already exchanged referrals.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    results = []
    user1_new_expiry = None
    user2_new_expiry = None

    # Award user1
    if user1_gets_days:
        user1_new_expiry = await extend_user_license_for_product(str(user1.id), user1_days, product)
        if user1_new_expiry:
            await add_referral(str(user2.id), str(user1.id), user1_days, product)
            results.append(f"✅ {user1.mention}: **+{user1_days} days** (Referral #{user1_referral_num})")
        else:
            results.append(f"❌ {user1.mention}: Failed to extend license")

    # Award user2
    if user2_gets_days:
        user2_new_expiry = await extend_user_license_for_product(str(user2.id), user2_days, product)
        if user2_new_expiry:
            await add_referral(str(user1.id), str(user2.id), user2_days, product)
            results.append(f"✅ {user2.mention}: **+{user2_days} days** (Referral #{user2_referral_num})")
        else:
            results.append(f"❌ {user2.mention}: Failed to extend license")

    # Get updated stats
    user1_stats = await get_referral_stats(str(user1.id), product)
    user2_stats = await get_referral_stats(str(user2.id), product)

    # Create success embed
    embed = discord.Embed(
        title="🎉 Mutual Referral Recorded!",
        description=f"Referral exchange between {user1.mention} and {user2.mention}",
        color=discord.Color.green()
    )
    embed.add_field(name="Results", value="\n".join(results), inline=False)
    embed.add_field(
        name=f"{user1.name}'s Stats",
        value=f"Given: **{user1_stats['given']}** | Used: **{user1_stats['received']}/{MAX_REFERRALS}**\nDays Earned: **+{user1_stats['total_days_earned']}**",
        inline=True
    )
    embed.add_field(
        name=f"{user2.name}'s Stats",
        value=f"Given: **{user2_stats['given']}** | Used: **{user2_stats['received']}/{MAX_REFERRALS}**\nDays Earned: **+{user2_stats['total_days_earned']}**",
        inline=True
    )

    await interaction.followup.send(embed=embed)

    # Audit log
    await send_audit_log(
        title="Mutual Referral Recorded",
        description=f"Referral exchange between {user1.mention} and {user2.mention}",
        admin=interaction.user,
        color=discord.Color.purple(),
        fields=[
            {"name": "User 1", "value": f"{user1} (`{user1.id}`)\n+{user1_days} days" if user1_gets_days else f"{user1} (skipped)", "inline": True},
            {"name": "User 2", "value": f"{user2} (`{user2.id}`)\n+{user2_days} days" if user2_gets_days else f"{user2} (skipped)", "inline": True},
        ]
    )

    # DM both users
    if user1_gets_days and user1_new_expiry:
        try:
            user1_expiry_dt = datetime.fromisoformat(user1_new_expiry)
            dm_embed = discord.Embed(
                title="🎉 Referral Bonus!",
                description=f"You received a referral from {user2.mention}!",
                color=discord.Color.green()
            )
            dm_embed.add_field(name="Days Added", value=f"+{user1_days} days", inline=True)
            dm_embed.add_field(name="New Expiry", value=user1_expiry_dt.strftime("%Y-%m-%d"), inline=True)
            dm_embed.add_field(name="Referrals Used", value=f"{user1_stats['received']}/{MAX_REFERRALS}", inline=True)
            await user1.send(embed=dm_embed)
        except discord.Forbidden:
            pass

    if user2_gets_days and user2_new_expiry:
        try:
            user2_expiry_dt = datetime.fromisoformat(user2_new_expiry)
            dm_embed = discord.Embed(
                title="🎉 Referral Bonus!",
                description=f"You received a referral from {user1.mention}!",
                color=discord.Color.green()
            )
            dm_embed.add_field(name="Days Added", value=f"+{user2_days} days", inline=True)
            dm_embed.add_field(name="New Expiry", value=user2_expiry_dt.strftime("%Y-%m-%d"), inline=True)
            dm_embed.add_field(name="Referrals Used", value=f"{user2_stats['received']}/{MAX_REFERRALS}", inline=True)
            await user2.send(embed=dm_embed)
        except discord.Forbidden:
            pass


@bot.tree.command(name="referral-stats", description="Check your referral statistics")
async def referral_stats(interaction: discord.Interaction):
    """Check your referral statistics for Saint's Shot."""
    user = interaction.user
    product = "saints-shot"
    product_name = "Saint's Shot"

    stats = await get_referral_stats(str(user.id), product)

    embed = discord.Embed(
        title=f"📊 Your {product_name} Referral Stats",
        color=discord.Color.blue()
    )
    embed.add_field(name="Referrals Given", value=str(stats['given']), inline=True)
    embed.add_field(name="Referrals Used", value=f"{stats['received']} / {MAX_REFERRALS}", inline=True)
    embed.add_field(name="Total Days Earned", value=f"+{stats['total_days_earned']} days", inline=True)

    # Show remaining referrals
    remaining = MAX_REFERRALS - stats['received']
    if remaining > 0:
        next_reward = REFERRAL_REWARDS.get(stats['received'] + 1, 2)
        embed.add_field(
            name="Referrals Available",
            value=f"You can use **{remaining}** more referral(s)\nNext referral: +{next_reward} days",
            inline=False
        )
    else:
        embed.add_field(
            name="Referrals Available",
            value="You have used all 5 referrals!",
            inline=False
        )

    embed.set_thumbnail(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed)


# ==================== EXCHANGE SYSTEM ====================

# Exchange rates based on pricing: Saint's Shot $10/week, SaintX $15/week
# Saint's Shot -> SaintX: 10/15 = 2/3 (0.6667)
# SaintX -> Saint's Shot: 15/10 = 3/2 (1.5)
EXCHANGE_RATE_SHOT_TO_SAINTX = 2 / 3
EXCHANGE_RATE_SAINTX_TO_SHOT = 3 / 2


def format_time_duration(total_seconds: float) -> str:
    """Format seconds into a readable duration string."""
    days = int(total_seconds // 86400)
    remaining = total_seconds % 86400
    hours = int(remaining // 3600)
    remaining = remaining % 3600
    minutes = int(remaining // 60)

    parts = []
    if days > 0:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes > 0 and days == 0:  # Only show minutes if less than a day
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")

    return ", ".join(parts) if parts else "less than a minute"


class ExchangeConfirmView(discord.ui.View):
    """Confirmation view for exchange - Yes/No buttons"""

    def __init__(self, user: discord.User, source_product: str, target_product: str,
                 source_seconds: float, target_seconds: float, source_license: dict, target_license: dict):
        super().__init__(timeout=60)
        self.user = user
        self.source_product = source_product
        self.target_product = target_product
        self.source_seconds = source_seconds
        self.target_seconds = target_seconds
        self.source_license = source_license
        self.target_license = target_license

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This is not your exchange request.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, Exchange", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        # Disable buttons
        for item in self.children:
            item.disabled = True

        discord_id = str(self.user.id)
        from datetime import timedelta

        # Subtract time from source license
        source_key = self.source_license.get("license_key")
        source_days_to_subtract = self.source_seconds / 86400

        if source_key:
            new_source_expiry = await extend_license(source_key, -source_days_to_subtract)
            if not new_source_expiry:
                embed = discord.Embed(
                    title="Exchange Failed",
                    description=f"Failed to update {self.source_product} license. Please contact support.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed, view=None)
                return

        # Calculate remaining time on source after exchange
        source_expires = self.source_license["expires_at"]
        if isinstance(source_expires, str):
            source_expires = datetime.fromisoformat(source_expires)
        remaining_source_seconds = (source_expires - datetime.utcnow()).total_seconds() - self.source_seconds

        # Add time to target license
        target_days_to_add = self.target_seconds / 86400
        target_product_key = "saintx" if self.target_product == "SaintX" else "saints-shot"
        source_product_key = "saintx" if self.source_product == "SaintX" else "saints-shot"

        if self.target_license and not self.target_license.get("revoked"):
            # Extend existing license
            new_target_expiry = await extend_user_license_for_product(discord_id, target_days_to_add, target_product_key)
            if new_target_expiry:
                target_expires = datetime.fromisoformat(new_target_expiry)
            else:
                # Restore source days
                if source_key:
                    await extend_license(source_key, source_days_to_subtract)
                embed = discord.Embed(
                    title="Exchange Failed",
                    description=f"Failed to extend {self.target_product} license. Please contact support.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed, view=None)
                return
        else:
            # Create new license
            target_expires = datetime.utcnow() + timedelta(seconds=self.target_seconds)

            license_key, _ = generate_license_key(
                SECRET_KEY,
                discord_id,
                int(target_days_to_add) + 1,  # Round up for key generation
                self.user.display_name
            )

            success = await add_license(
                license_key=license_key,
                discord_id=discord_id,
                discord_name=self.user.display_name,
                expires_at=target_expires,
                product=target_product_key
            )

            if not success:
                # Restore source days
                if source_key:
                    await extend_license(source_key, source_days_to_add)
                embed = discord.Embed(
                    title="Exchange Failed",
                    description=f"Failed to create {self.target_product} license. Please contact support.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed, view=None)
                return

        # Handle roles
        role_changes = []
        if GUILD_ID:
            try:
                guild = bot.get_guild(GUILD_ID)
                if guild:
                    member = guild.get_member(self.user.id) or await guild.fetch_member(self.user.id)
                    if member:
                        # Remove source role if no time left
                        if remaining_source_seconds <= 0:
                            source_role_id = SAINTX_ROLE_ID if source_product_key == "saintx" else SAINTS_SHOT_ROLE_ID
                            if source_role_id:
                                source_role = guild.get_role(source_role_id)
                                if source_role and source_role in member.roles:
                                    await member.remove_roles(source_role, reason=f"Exchanged all time for {self.target_product}")
                                    role_changes.append(f"{self.source_product} role removed")

                        # Add target role
                        target_role_id = SAINTX_ROLE_ID if target_product_key == "saintx" else SAINTS_SHOT_ROLE_ID
                        if target_role_id:
                            target_role = guild.get_role(target_role_id)
                            if target_role and target_role not in member.roles:
                                await member.add_roles(target_role, reason=f"Exchanged from {self.source_product}")
                                role_changes.append(f"{self.target_product} role added")
            except Exception as e:
                print(f"Error updating roles during exchange: {e}")

        # Success embed
        embed = discord.Embed(
            title="Exchange Complete!",
            description=f"{self.user.mention} exchanged {self.source_product} for {self.target_product}!",
            color=discord.Color.green()
        )
        embed.add_field(name=f"{self.source_product} Used", value=f"-{format_time_duration(self.source_seconds)}", inline=True)
        embed.add_field(name=f"{self.target_product} Received", value=f"+{format_time_duration(self.target_seconds)}", inline=True)

        if remaining_source_seconds > 0:
            embed.add_field(name=f"{self.source_product} Remaining", value=format_time_duration(remaining_source_seconds), inline=True)

        embed.add_field(name=f"{self.target_product} Expires", value=target_expires.strftime("%B %d, %Y at %I:%M %p UTC"), inline=True)

        if role_changes:
            embed.add_field(name="Roles", value=", ".join(role_changes), inline=False)

        if target_product_key == "saintx":
            embed.add_field(name="Next Steps", value="Head to <#1475702262729936979> to get started with SaintX!", inline=False)

        embed.set_thumbnail(url=self.user.display_avatar.url)
        await interaction.edit_original_response(embed=embed, view=None)

        # DM the user
        try:
            dm_embed = discord.Embed(
                title=f"{self.target_product} Exchange Complete!",
                description=f"You exchanged **{format_time_duration(self.source_seconds)}** of {self.source_product} for **{format_time_duration(self.target_seconds)}** of {self.target_product}!",
                color=discord.Color.green()
            )
            dm_embed.add_field(name="Your Discord ID", value=f"```{self.user.id}```", inline=False)
            dm_embed.add_field(name=f"{self.target_product} Expires", value=target_expires.strftime("%B %d, %Y at %I:%M %p UTC"), inline=True)
            if remaining_source_seconds > 0:
                dm_embed.add_field(name=f"{self.source_product} Remaining", value=format_time_duration(remaining_source_seconds), inline=True)
            dm_embed.add_field(
                name="How to Activate",
                value=f"1. Open {self.target_product}\n2. Enter your Discord ID\n3. Click Activate",
                inline=False
            )
            await self.user.send(embed=dm_embed)
        except discord.Forbidden:
            pass

        print(f"Exchange completed: {self.user} ({self.user.id}) - {format_time_duration(self.source_seconds)} {self.source_product} -> {format_time_duration(self.target_seconds)} {self.target_product}")

    @discord.ui.button(label="No, Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Exchange Cancelled",
            description="Your exchange request has been cancelled.",
            color=discord.Color.grey()
        )
        await interaction.response.edit_message(embed=embed, view=None)


class ExchangeDaysModal(discord.ui.Modal):
    """Modal popup to ask how many days to exchange"""

    days_input = discord.ui.TextInput(
        label="Time to Exchange",
        placeholder="Enter days (e.g., 5 or 3.5 for 3 days 12 hours)",
        required=True,
        max_length=10
    )

    def __init__(self, user: discord.User, target_product: str, source_product: str,
                 available_seconds: float, source_license: dict, target_license: dict):
        super().__init__(title=f"Exchange to {target_product}")
        self.user = user
        self.target_product = target_product
        self.source_product = source_product
        self.available_seconds = available_seconds
        self.source_license = source_license
        self.target_license = target_license

    async def on_submit(self, interaction: discord.Interaction):
        try:
            days = float(self.days_input.value.strip())
            if days <= 0:
                raise ValueError("Days must be positive")
        except ValueError:
            embed = discord.Embed(
                title="Invalid Input",
                description="Please enter a valid number of days (e.g., 5 or 3.5).",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        requested_seconds = days * 86400

        if requested_seconds > self.available_seconds:
            available_days = self.available_seconds / 86400
            embed = discord.Embed(
                title="Not Enough Time",
                description=f"You only have **{format_time_duration(self.available_seconds)}** available to exchange.",
                color=discord.Color.red()
            )
            embed.add_field(name="Requested", value=f"{days} days", inline=True)
            embed.add_field(name="Available", value=f"{available_days:.2f} days", inline=True)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Calculate target time based on exchange rate
        if self.target_product == "SaintX":
            target_seconds = requested_seconds * EXCHANGE_RATE_SHOT_TO_SAINTX
            rate_display = "2/3 (Saint's Shot is $10/week, SaintX is $15/week)"
        else:
            target_seconds = requested_seconds * EXCHANGE_RATE_SAINTX_TO_SHOT
            rate_display = "3/2 (SaintX is $15/week, Saint's Shot is $10/week)"

        # Show confirmation
        embed = discord.Embed(
            title="Confirm Exchange",
            description="Are you sure you want to make this exchange?",
            color=discord.Color.gold()
        )
        embed.add_field(name=f"{self.source_product} to Use", value=format_time_duration(requested_seconds), inline=True)
        embed.add_field(name=f"{self.target_product} to Receive", value=format_time_duration(target_seconds), inline=True)
        embed.add_field(name="Exchange Rate", value=rate_display, inline=False)

        remaining_seconds = self.available_seconds - requested_seconds
        if remaining_seconds > 0:
            embed.add_field(name=f"{self.source_product} Remaining After", value=format_time_duration(remaining_seconds), inline=False)
        else:
            embed.add_field(name="Note", value=f"This will use all your {self.source_product} time.", inline=False)

        confirm_view = ExchangeConfirmView(
            user=self.user,
            source_product=self.source_product,
            target_product=self.target_product,
            source_seconds=requested_seconds,
            target_seconds=target_seconds,
            source_license=self.source_license,
            target_license=self.target_license
        )

        await interaction.response.send_message(embed=embed, view=confirm_view)


class ExchangeSelectView(discord.ui.View):
    """Initial view with buttons to select exchange direction"""

    def __init__(self, user: discord.User, shot_license: dict, saintx_license: dict,
                 shot_available_seconds: float, saintx_available_seconds: float):
        super().__init__(timeout=120)
        self.user = user
        self.shot_license = shot_license
        self.saintx_license = saintx_license
        self.shot_available_seconds = shot_available_seconds
        self.saintx_available_seconds = saintx_available_seconds

        # Disable SaintX button if user doesn't have Saint's Shot
        if not shot_license or shot_available_seconds <= 0:
            self.to_saintx.disabled = True
            self.to_saintx.label = "Exchange to SaintX (No Saint's Shot)"

        # Disable Saint's Shot button if user doesn't have SaintX
        if not saintx_license or saintx_available_seconds <= 0:
            self.to_shot.disabled = True
            self.to_shot.label = "Exchange to Saint's Shot (No SaintX)"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This is not your exchange request.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Exchange to SaintX", style=discord.ButtonStyle.primary, row=0)  # Purple/Blurple
    async def to_saintx(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = ExchangeDaysModal(
            user=self.user,
            target_product="SaintX",
            source_product="Saint's Shot",
            available_seconds=self.shot_available_seconds,
            source_license=self.shot_license,
            target_license=self.saintx_license
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Exchange to Saint's Shot", style=discord.ButtonStyle.success, row=0)  # Green
    async def to_shot(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = ExchangeDaysModal(
            user=self.user,
            target_product="Saint's Shot",
            source_product="SaintX",
            available_seconds=self.saintx_available_seconds,
            source_license=self.saintx_license,
            target_license=self.shot_license
        )
        await interaction.response.send_modal(modal)


@bot.tree.command(name="exchange", description="Exchange subscription days between Saint's Shot and SaintX")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def exchange(interaction: discord.Interaction):
    """Exchange subscription days between Saint's Shot and SaintX."""
    user = interaction.user
    discord_id = str(user.id)

    # Get both licenses
    shot_license = await get_license_by_user(discord_id, "saints-shot")
    saintx_license = await get_license_by_user(discord_id, "saintx")

    now = datetime.utcnow()

    # Calculate available time for each
    shot_available_seconds = 0
    if shot_license and not shot_license.get("revoked"):
        shot_expires = shot_license["expires_at"]
        if isinstance(shot_expires, str):
            shot_expires = datetime.fromisoformat(shot_expires)
        if shot_expires > now:
            shot_available_seconds = (shot_expires - now).total_seconds()

    saintx_available_seconds = 0
    if saintx_license and not saintx_license.get("revoked"):
        saintx_expires = saintx_license["expires_at"]
        if isinstance(saintx_expires, str):
            saintx_expires = datetime.fromisoformat(saintx_expires)
        if saintx_expires > now:
            saintx_available_seconds = (saintx_expires - now).total_seconds()

    # Check if user has any active license
    if shot_available_seconds <= 0 and saintx_available_seconds <= 0:
        embed = discord.Embed(
            title="No Active Subscriptions",
            description=f"{user.mention} - You don't have any active subscriptions to exchange.",
            color=discord.Color.red()
        )
        embed.add_field(
            name="How to Get Started",
            value="Purchase a subscription from the store!",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Show selection view
    embed = discord.Embed(
        title="Exchange Subscription Time",
        description="Select which product you want to exchange **to**:",
        color=discord.Color.blue()
    )

    if shot_available_seconds > 0:
        embed.add_field(
            name="Saint's Shot Available",
            value=f"{format_time_duration(shot_available_seconds)}",
            inline=True
        )

    if saintx_available_seconds > 0:
        embed.add_field(
            name="SaintX Available",
            value=f"{format_time_duration(saintx_available_seconds)}",
            inline=True
        )

    embed.add_field(
        name="Exchange Rates",
        value="**Saint's Shot → SaintX:** 2/3 (you get fewer days)\n**SaintX → Saint's Shot:** 3/2 (you get more days)\n\n*Based on pricing: Saint's Shot $10/week, SaintX $15/week*",
        inline=False
    )

    view = ExchangeSelectView(
        user=user,
        shot_license=shot_license,
        saintx_license=saintx_license,
        shot_available_seconds=shot_available_seconds,
        saintx_available_seconds=saintx_available_seconds
    )

    await interaction.response.send_message(embed=embed, view=view)


# ==================== STATUS COMMAND ====================

STATUS_CHOICES = [
    app_commands.Choice(name="🟢 Undetected", value="undetected"),
    app_commands.Choice(name="🟡 Use At Your Own Risk", value="risky"),
    app_commands.Choice(name="🔴 Detected", value="detected"),
    app_commands.Choice(name="⚠️ Under Maintenance", value="maintenance"),
]

PRODUCT_CHOICES_STATUS = [
    app_commands.Choice(name="Saint's Gen - Gen Mode", value="saints-gen-gen"),
    app_commands.Choice(name="Saint's Gen - XP Mode", value="saints-gen-xp"),
    app_commands.Choice(name="Saint's Shot", value="saints-shot"),
    app_commands.Choice(name="SaintX", value="saintx"),
]


@bot.tree.command(name="setstatus", description="Set the detection status for a product")
@is_admin()
@app_commands.describe(
    product="Which product to set status for",
    status="The new status"
)
@app_commands.choices(product=PRODUCT_CHOICES_STATUS, status=STATUS_CHOICES)
async def setstatus(interaction: discord.Interaction, product: str, status: str):
    """Set the detection status for a product (admin only)."""
    old_status = PRODUCT_STATUS.get(product, "undetected")
    PRODUCT_STATUS[product] = status

    product_name = get_product_name(product)
    old_info = STATUS_DISPLAY.get(old_status, STATUS_DISPLAY["undetected"])
    new_info = STATUS_DISPLAY.get(status, STATUS_DISPLAY["undetected"])

    # Update the status message in the channel
    await update_status_message()

    # Respond to admin
    embed = discord.Embed(
        title="Status Updated",
        description=f"**{product_name}** status has been updated.",
        color=new_info["color"]
    )
    embed.add_field(name="Previous", value=f"{old_info['emoji']} {old_info['label']}", inline=True)
    embed.add_field(name="New", value=f"{new_info['emoji']} {new_info['label']}", inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)

    # Audit log
    await send_audit_log(
        title="Product Status Changed",
        description=f"Updated **{product_name}** status",
        admin=interaction.user,
        color=new_info["color"],
        fields=[
            {"name": "Product", "value": product_name, "inline": True},
            {"name": "Old Status", "value": f"{old_info['emoji']} {old_info['label']}", "inline": True},
            {"name": "New Status", "value": f"{new_info['emoji']} {new_info['label']}", "inline": True},
        ]
    )


# ==================== ERROR HANDLING ====================

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            "You don't have permission to use this command."        )
    else:
        await interaction.response.send_message(
            f"An error occurred: {str(error)}"        )
        raise error


# ==================== RUN BOT ====================

def run_api():
    """Run the FastAPI server in a separate thread."""
    import uvicorn
    from api import app
    import os

    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


def main():
    import threading
    import os

    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN not set!")
        print("Please set the DISCORD_TOKEN environment variable or create a .env file.")
        return

    if not ADMIN_IDS:
        print("WARNING: No ADMIN_IDS configured. No one will be able to use admin commands.")

    if SECRET_KEY == "CHANGE_THIS_TO_A_SECURE_RANDOM_STRING":
        print("WARNING: Using default SECRET_KEY. Please set a secure key for production!")

    # Start API server in background thread
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    print(f"API server started on port {os.getenv('PORT', 8080)}")

    # Run Discord bot (blocking)
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
