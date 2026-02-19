"""
Saint's Gen - License Bot
Discord bot for managing subscription licenses.
"""
import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import os
from datetime import datetime
from typing import Optional

import aiohttp

from config import DISCORD_TOKEN, ADMIN_IDS, SECRET_KEY, GUILD_ID, SUBSCRIBER_ROLE_ID, SAINTS_SHOT_ROLE_ID, STORE_URL
from database import (
    init_db, add_license, get_license_by_key, get_license_by_user,
    revoke_license, revoke_user_licenses, delete_license, delete_user_licenses,
    extend_license, extend_user_license, get_all_active_licenses, get_license_stats,
    reset_hwid_by_key, reset_hwid_by_user, get_hwid_by_key,
    get_newly_expired_licenses, mark_expiry_notified, has_active_license,
    has_active_license_for_product, close_pool, init_notifications_table,
    get_pending_notifications, get_failed_notifications, init_referrals_table,
    get_referral_count_received, get_referral_count_given, has_been_referred_by,
    add_referral, get_referral_stats, extend_user_license_for_product,
    get_pending_order_by_email, claim_pending_order, init_linked_accounts_table
)
from license_crypto import generate_license_key, get_key_info


class LicenseBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True  # Needed to fetch member info
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Initialize database
        await init_db()
        await init_notifications_table()
        await init_referrals_table()
        await init_linked_accounts_table()
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
        self.process_shopify_notifications.start()

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print(f"Admin IDs: {ADMIN_IDS}")
        print(f"Guild ID: {GUILD_ID}")
        print(f"Subscriber Role ID: {SUBSCRIBER_ROLE_ID}")
        print(f"Saint's Shot Role ID: {SAINTS_SHOT_ROLE_ID}")
        print("------")

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
                role_id = SAINTS_SHOT_ROLE_ID if product == "saints-shot" else SUBSCRIBER_ROLE_ID
                product_name = "Saint's Shot" if product == "saints-shot" else "Saint's Gen"

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

                product_name = "Saint's Gen" if product == "saints-gen" else "Saint's Shot"
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
                    role_id = SAINTS_SHOT_ROLE_ID if product == "saints-shot" else SUBSCRIBER_ROLE_ID
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


def is_admin():
    """Check if user is an admin."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id in ADMIN_IDS
    return app_commands.check(predicate)


# ==================== ADMIN COMMANDS ====================

# Product choices for the generate command
PRODUCT_CHOICES = [
    app_commands.Choice(name="Saint's Gen", value="saints-gen"),
    app_commands.Choice(name="Saint's Shot", value="saints-shot"),
]


@bot.tree.command(name="generate", description="Give a user access to a product")
@is_admin()
@app_commands.describe(
    user="The Discord user to give access to",
    days="Number of days of access",
    product="Which product to give access for"
)
@app_commands.choices(product=PRODUCT_CHOICES)
async def generate(interaction: discord.Interaction, user: discord.User, days: int, product: str = "saints-gen"):
    """Give a user access to a product (they login with their Discord ID)."""
    if days < 1:
        await interaction.response.send_message("Days must be at least 1.", ephemeral=True)
        return

    if days > 36500:  # 100 years max
        await interaction.response.send_message("Maximum is 36500 days (100 years).", ephemeral=True)
        return

    # Generate internal key (user never sees this)
    license_key, expires_at = generate_license_key(SECRET_KEY, str(user.id), days, user.name, "")

    # Store in database
    success = await add_license(
        license_key=license_key,
        discord_id=str(user.id),
        discord_name=str(user),
        expires_at=expires_at,
        product=product
    )

    if not success:
        await interaction.response.send_message(
            "Failed to create subscription. Try again.",
            ephemeral=True
        )
        return

    # Give appropriate role based on product
    role_added = False
    role_id = SAINTS_SHOT_ROLE_ID if product == "saints-shot" else SUBSCRIBER_ROLE_ID
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

    # Product display name
    product_name = "Saint's Gen" if product == "saints-gen" else "Saint's Shot"

    # Create embed response (no license key shown)
    embed = discord.Embed(
        title="Subscription Added",
        color=discord.Color.green()
    )
    embed.add_field(name="Product", value=product_name, inline=True)
    embed.add_field(name="User", value=f"{user.mention}", inline=True)
    embed.add_field(name="Discord ID", value=f"`{user.id}`", inline=False)
    embed.add_field(name="Duration", value=f"{days} days", inline=True)
    embed.add_field(name="Expires", value=expires_at.strftime("%Y-%m-%d %H:%M UTC"), inline=True)
    if role_added:
        embed.add_field(name="Role", value="Added", inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)

    # DM the user (no key, just tell them to use Discord ID)
    try:
        user_embed = discord.Embed(
            title=f"{product_name} Access Granted!",
            description="You now have access to the product.",
            color=discord.Color.green()
        )
        user_embed.add_field(name="Your Discord ID", value=f"```{user.id}```", inline=False)
        user_embed.add_field(name="Expires", value=expires_at.strftime("%Y-%m-%d %H:%M UTC"), inline=False)
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
            "Please provide either a license key or a user.",
            ephemeral=True
        )
        return

    if key:
        success = await revoke_license(key)
        if success:
            await interaction.response.send_message(
                f"License `{key[:20]}...` has been revoked.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "License not found.",
                ephemeral=True
            )
    else:
        count = await revoke_user_licenses(str(user.id))
        await interaction.response.send_message(
            f"Revoked {count} license(s) for {user.mention}.",
            ephemeral=True
        )


@bot.tree.command(name="delete", description="Permanently delete a license by key or user")
@is_admin()
@app_commands.describe(
    key="The license key to delete (optional)",
    user="The user whose licenses to delete (optional)"
)
async def delete(
    interaction: discord.Interaction,
    key: Optional[str] = None,
    user: Optional[discord.User] = None
):
    """Permanently delete a license key or all keys for a user."""
    if not key and not user:
        await interaction.response.send_message(
            "Please provide either a license key or a user.",
            ephemeral=True
        )
        return

    if key:
        success = await delete_license(key)
        if success:
            await interaction.response.send_message(
                f"License `{key[:20]}...` has been **permanently deleted**.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "License not found.",
                ephemeral=True
            )
    else:
        count = await delete_user_licenses(str(user.id))
        await interaction.response.send_message(
            f"**Permanently deleted** {count} license(s) for {user.mention}.",
            ephemeral=True
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
            "Please provide either a license key or a user.",
            ephemeral=True
        )
        return

    if days == 0:
        await interaction.response.send_message(
            "Days cannot be 0.",
            ephemeral=True
        )
        return

    # Determine action word based on positive/negative
    action = "extended" if days > 0 else "reduced"
    days_display = f"+{days}" if days > 0 else str(days)

    if key:
        new_expiry = await extend_license(key, days)
        if new_expiry:
            expiry_dt = datetime.fromisoformat(new_expiry)
            await interaction.response.send_message(
                f"License {action} by **{days_display} days**.\nNew expiry: **{expiry_dt.strftime('%Y-%m-%d %H:%M UTC')}**",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "License not found.",
                ephemeral=True
            )
    else:
        # When using user, product is required
        if not product:
            await interaction.response.send_message(
                "Please select a product when modifying by user.",
                ephemeral=True
            )
            return

        new_expiry = await extend_user_license_for_product(str(user.id), days, product)
        product_name = "Saint's Gen" if product == "saints-gen" else "Saint's Shot"
        if new_expiry:
            expiry_dt = datetime.fromisoformat(new_expiry)
            await interaction.response.send_message(
                f"{action.capitalize()} {user.mention}'s **{product_name}** license by **{days_display} days**.\nNew expiry: **{expiry_dt.strftime('%Y-%m-%d %H:%M UTC')}**",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"{user.mention} has no **{product_name}** license to modify.",
                ephemeral=True
            )


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
        product_name = "Saint's Gen" if product == "saints-gen" else "Saint's Shot"
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
        prod_tag = "[Gen]" if lic.get("product") == "saints-gen" else "[Shot]"
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


@bot.tree.command(name="check", description="Check a license key's validity")
@is_admin()
@app_commands.describe(key="The license key to check")
async def check(interaction: discord.Interaction, key: str):
    """Check if a license key is valid."""
    info = get_key_info(SECRET_KEY, key)
    db_info = await get_license_by_key(key)

    embed = discord.Embed(
        title="License Check",
        color=discord.Color.green() if info["valid"] else discord.Color.red()
    )

    embed.add_field(name="Key Valid", value="Yes" if info["valid"] else "No", inline=True)

    if info["discord_id"]:
        embed.add_field(name="Discord ID", value=info["discord_id"], inline=True)

    if info["expires_at"]:
        embed.add_field(
            name="Expires",
            value=info["expires_at"].strftime("%Y-%m-%d %H:%M UTC"),
            inline=True
        )
        embed.add_field(name="Expired", value="Yes" if info["expired"] else "No", inline=True)

    if db_info:
        embed.add_field(name="In Database", value="Yes", inline=True)
        # Show product
        prod = db_info.get("product", "saints-gen")
        prod_name = "Saint's Gen" if prod == "saints-gen" else "Saint's Shot"
        embed.add_field(name="Product", value=prod_name, inline=True)
        embed.add_field(name="Revoked", value="Yes" if db_info["revoked"] else "No", inline=True)
        # Show hardware binding status
        hwid = db_info.get("hwid")
        if hwid:
            embed.add_field(name="Hardware Bound", value=f"Yes (`{hwid[:8]}...`)", inline=True)
        else:
            embed.add_field(name="Hardware Bound", value="No (not activated)", inline=True)
    else:
        embed.add_field(name="In Database", value="No", inline=True)

    if info["error"]:
        embed.add_field(name="Error", value=info["error"], inline=False)

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
            "Please provide either a license key or a user.",
            ephemeral=True
        )
        return

    if key:
        # Check current binding first
        current_hwid = await get_hwid_by_key(key)
        if not current_hwid:
            await interaction.response.send_message(
                "This license is not bound to any hardware yet.",
                ephemeral=True
            )
            return

        success = await reset_hwid_by_key(key)
        if success:
            await interaction.response.send_message(
                f"Hardware binding reset for license `{key[:20]}...`\n"
                f"Previous HWID: `{current_hwid[:12]}...`\n"
                f"The user can now activate on a new PC.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "License not found.",
                ephemeral=True
            )
    else:
        count = await reset_hwid_by_user(str(user.id))
        if count > 0:
            await interaction.response.send_message(
                f"Reset hardware binding for {count} license(s) for {user.mention}.\n"
                f"They can now activate on a new PC.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"{user.mention} has no licenses to reset.",
                ephemeral=True
            )


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


# ==================== USER COMMANDS ====================

@bot.tree.command(name="id", description="Get your Discord ID for checkout")
async def get_id(interaction: discord.Interaction):
    """Show the user their Discord ID for use at checkout."""
    user = interaction.user

    embed = discord.Embed(
        title="Your Discord ID",
        description="Use this ID when purchasing to receive your license automatically!",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="Your ID",
        value=f"```{user.id}```",
        inline=False
    )
    embed.add_field(
        name="How to Use",
        value="Copy the number above and paste it in the **Discord ID** field at checkout.",
        inline=False
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text="Your license key will be sent to you via DM after purchase!")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="status", description="Check your subscription status")
@app_commands.describe(product="Which product to check status for (optional)")
@app_commands.choices(product=PRODUCT_CHOICES)
async def status(interaction: discord.Interaction, product: str = None):
    """Check subscription status."""
    license_data = await get_license_by_user(str(interaction.user.id), product)

    embed = discord.Embed(title="Subscription Status")

    if not license_data:
        embed.color = discord.Color.red()
        embed.description = "You don't have an active license."
        embed.add_field(
            name="How to Get Access",
            value="Contact an admin to purchase a subscription.",
            inline=False
        )
    else:
        # Get product name
        prod = license_data.get("product", "saints-gen")
        prod_name = "Saint's Gen" if prod == "saints-gen" else "Saint's Shot"

        expires = license_data["expires_at"]
        if isinstance(expires, str):
            expires = datetime.fromisoformat(expires)
        now = datetime.utcnow()

        if expires < now:
            embed.color = discord.Color.red()
            embed.description = f"Your **{prod_name}** license has **expired**."
            embed.add_field(name="Expired On", value=expires.strftime("%Y-%m-%d"), inline=True)
        else:
            days_left = (expires - now).days
            embed.color = discord.Color.green()
            embed.description = f"Your **{prod_name}** license is **active**."
            embed.add_field(name="Expires", value=expires.strftime("%Y-%m-%d"), inline=True)
            embed.add_field(name="Days Left", value=str(days_left), inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="link", description="Link your Shopify purchase to your Discord account")
@app_commands.describe(email="The email you used for your Shopify purchase")
async def link_purchase(interaction: discord.Interaction, email: str):
    """Link a Shopify purchase to claim your license."""
    await interaction.response.defer(ephemeral=True)

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
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    product = pending["product"]
    days = pending["days"]
    order_number = pending.get("order_number", "Unknown")

    # Generate license for this user
    from datetime import timedelta
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
                role_id = SUBSCRIBER_ROLE_ID if product == "saints-gen" else SAINTS_SHOT_ROLE_ID
                if role_id:
                    role = guild.get_role(role_id)
                    if role:
                        await member.add_roles(role)
                        print(f"Added {product} role to {discord_name}")
    except Exception as e:
        print(f"Error assigning role: {e}")

    # Get product name
    prod_name = "Saint's Gen" if product == "saints-gen" else "Saint's Shot"

    embed = discord.Embed(
        title="Purchase Linked Successfully!",
        description=f"Your **{prod_name}** license has been activated!",
        color=discord.Color.green()
    )
    embed.add_field(name="Order", value=f"#{order_number}", inline=True)
    embed.add_field(name="Duration", value=f"{days} days", inline=True)
    embed.add_field(name="Expires", value=expires_at.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(
        name="How to Use",
        value="Open the app and login with your Discord. Your account is now authorized!",
        inline=False
    )
    embed.set_footer(text="Thank you for your purchase!")

    await interaction.followup.send(embed=embed, ephemeral=True)

    # Also try to DM them
    try:
        dm_embed = discord.Embed(
            title=f"{prod_name} License Activated!",
            description="Your purchase has been linked to your Discord account.",
            color=discord.Color.green()
        )
        dm_embed.add_field(name="Order", value=f"#{order_number}", inline=True)
        dm_embed.add_field(name="Expires", value=expires_at.strftime("%Y-%m-%d"), inline=True)
        dm_embed.add_field(
            name="Login",
            value="Just open the app - it will recognize your Discord account automatically!",
            inline=False
        )
        await interaction.user.send(embed=dm_embed)
    except:
        pass  # DMs might be disabled


@bot.tree.command(name="balance", description="Show your subscription balance publicly")
@app_commands.describe(product="Which product to show balance for")
@app_commands.choices(product=PRODUCT_CHOICES)
async def balance(interaction: discord.Interaction, product: str = None):
    """Show subscription balance publicly."""
    license_data = await get_license_by_user(str(interaction.user.id), product)

    if not license_data:
        embed = discord.Embed(
            title="No Active License",
            description=f"{interaction.user.mention} does not have an active license.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed)
        return

    # Get product name
    prod = license_data.get("product", "saints-gen")
    prod_name = "Saint's Gen" if prod == "saints-gen" else "Saint's Shot"

    expires = license_data["expires_at"]
    if isinstance(expires, str):
        expires = datetime.fromisoformat(expires)
    now = datetime.utcnow()

    if expires < now:
        embed = discord.Embed(
            title=f"{prod_name} Balance",
            description=f"{interaction.user.mention}'s license has **expired**.",
            color=discord.Color.red()
        )
        embed.add_field(name="Status", value="Expired", inline=True)
        embed.add_field(name="Expired On", value=expires.strftime("%Y-%m-%d"), inline=True)
    else:
        days_left = (expires - now).days
        hours_left = int((expires - now).total_seconds() // 3600) % 24

        # Color based on days left
        if days_left > 30:
            color = discord.Color.green()
        elif days_left > 7:
            color = discord.Color.gold()
        else:
            color = discord.Color.orange()

        embed = discord.Embed(
            title=f"{prod_name} Balance",
            description=f"{interaction.user.mention}'s subscription is **active**.",
            color=color
        )
        embed.add_field(name="Status", value="Active", inline=True)
        embed.add_field(name="Days Left", value=f"{days_left}d {hours_left}h", inline=True)
        embed.add_field(name="Expires", value=expires.strftime("%B %d, %Y"), inline=True)

    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)


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


@bot.tree.command(name="referral", description="[Admin] Record a referral between two users for Saint's Shot")
@is_admin()
@app_commands.describe(
    referrer="The user who referred someone",
    referred="The user who was referred"
)
async def referral(interaction: discord.Interaction, referrer: discord.User, referred: discord.User):
    """Record a referral between two users. Both get credit - referrer gets +1 to their given count, referred gets days."""
    product = "saints-shot"
    product_name = "Saint's Shot"

    # Can't refer yourself
    if referrer.id == referred.id:
        await interaction.response.send_message(
            "A user cannot refer themselves!",
            ephemeral=True
        )
        return

    # Check if referred user has an active Saint's Shot license
    has_license = await has_active_license_for_product(str(referred.id), product)
    if not has_license:
        await interaction.response.send_message(
            f"{referred.mention} needs an active **{product_name}** license to receive referral bonus.",
            ephemeral=True
        )
        return

    # Check if already referred by this person
    already_referred = await has_been_referred_by(str(referred.id), str(referrer.id), product)
    if already_referred:
        await interaction.response.send_message(
            f"{referred.mention} has already been referred by {referrer.mention}!",
            ephemeral=True
        )
        return

    # Check how many times the referred user has been referred (cap at 5)
    times_referred = await get_referral_count_received(str(referred.id), product)
    if times_referred >= MAX_REFERRALS:
        await interaction.response.send_message(
            f"{referred.mention} has reached the maximum of **{MAX_REFERRALS}** referrals.",
            ephemeral=True
        )
        return

    # Calculate days to award based on referral number
    referral_number = times_referred + 1
    days_awarded = REFERRAL_REWARDS.get(referral_number, 2)

    # Extend the referred user's license
    new_expiry = await extend_user_license_for_product(str(referred.id), days_awarded, product)
    if not new_expiry:
        await interaction.response.send_message(
            f"Could not extend {referred.mention}'s license.",
            ephemeral=True
        )
        return

    # Record the referral (this tracks both users)
    success = await add_referral(str(referrer.id), str(referred.id), days_awarded, product)
    if not success:
        await interaction.response.send_message(
            f"Failed to record referral.",
            ephemeral=True
        )
        return

    # Get stats for both users
    referrer_stats = await get_referral_stats(str(referrer.id), product)
    referred_stats = await get_referral_stats(str(referred.id), product)

    # Parse expiry date
    expiry_dt = datetime.fromisoformat(new_expiry)

    # Create success embed
    embed = discord.Embed(
        title="🎉 Referral Recorded!",
        description=f"{referrer.mention} referred {referred.mention}",
        color=discord.Color.green()
    )
    embed.add_field(name="Days Awarded", value=f"**+{days_awarded} days** to {referred.mention}", inline=False)
    embed.add_field(name="Referral #", value=f"{referral_number} of {MAX_REFERRALS}", inline=True)
    embed.add_field(name="New Expiry", value=expiry_dt.strftime("%Y-%m-%d %H:%M UTC"), inline=True)
    embed.add_field(
        name=f"{referrer.name}'s Stats",
        value=f"Referrals Given: **{referrer_stats['given']}**",
        inline=True
    )
    embed.add_field(
        name=f"{referred.name}'s Stats",
        value=f"Referrals Used: **{referred_stats['received']}/{MAX_REFERRALS}**\nTotal Days Earned: **+{referred_stats['total_days_earned']}**",
        inline=True
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)

    # Try to notify both users
    try:
        referrer_embed = discord.Embed(
            title="🎁 Referral Recorded!",
            description=f"You referred {referred.mention}!",
            color=discord.Color.blue()
        )
        referrer_embed.add_field(name="Your Total Referrals Given", value=str(referrer_stats['given']), inline=True)
        await referrer.send(embed=referrer_embed)
    except discord.Forbidden:
        pass

    try:
        referred_embed = discord.Embed(
            title="🎉 Referral Bonus!",
            description=f"You were referred by {referrer.mention}!",
            color=discord.Color.green()
        )
        referred_embed.add_field(name="Days Added", value=f"+{days_awarded} days", inline=True)
        referred_embed.add_field(name="New Expiry", value=expiry_dt.strftime("%Y-%m-%d"), inline=True)
        referred_embed.add_field(name="Referrals Used", value=f"{referred_stats['received']}/{MAX_REFERRALS}", inline=True)
        await referred.send(embed=referred_embed)
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
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==================== ERROR HANDLING ====================

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            "You don't have permission to use this command.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"An error occurred: {str(error)}",
            ephemeral=True
        )
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
