"""
Saint's Gen - License Bot
Discord bot for managing subscription licenses.
"""
import discord
from discord import app_commands
from discord.ext import commands
import asyncio
from datetime import datetime
from typing import Optional

from config import DISCORD_TOKEN, ADMIN_IDS, SECRET_KEY
from database import (
    init_db, add_license, get_license_by_key, get_license_by_user,
    revoke_license, revoke_user_licenses, delete_license, delete_user_licenses,
    extend_license, extend_user_license, get_all_active_licenses, get_license_stats
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
        # Sync slash commands globally and to specific guild for instant availability
        await self.tree.sync()
        # Instant sync to your server
        guild = discord.Object(id=1290387028185448469)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print(f"Synced slash commands")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print(f"Admin IDs: {ADMIN_IDS}")
        print("------")


bot = LicenseBot()


def is_admin():
    """Check if user is an admin."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id in ADMIN_IDS
    return app_commands.check(predicate)


# ==================== ADMIN COMMANDS ====================

@bot.tree.command(name="generate", description="Generate a license key for a user")
@is_admin()
@app_commands.describe(
    user="The Discord user to generate a key for",
    days="Number of days until the license expires"
)
async def generate(interaction: discord.Interaction, user: discord.User, days: int):
    """Generate a new license key for a user."""
    if days < 1:
        await interaction.response.send_message("Days must be at least 1.", ephemeral=True)
        return

    if days > 365:
        await interaction.response.send_message("Maximum is 365 days.", ephemeral=True)
        return

    # Generate the key
    license_key, expires_at = generate_license_key(SECRET_KEY, str(user.id), days, user.name)

    # Store in database
    success = await add_license(
        license_key=license_key,
        discord_id=str(user.id),
        discord_name=str(user),
        expires_at=expires_at
    )

    if not success:
        await interaction.response.send_message(
            "Failed to create license (key collision). Try again.",
            ephemeral=True
        )
        return

    # Create embed response
    embed = discord.Embed(
        title="License Generated",
        color=discord.Color.green()
    )
    embed.add_field(name="User", value=f"{user.mention} ({user.id})", inline=False)
    embed.add_field(name="Duration", value=f"{days} days", inline=True)
    embed.add_field(name="Expires", value=expires_at.strftime("%Y-%m-%d %H:%M UTC"), inline=True)
    embed.add_field(name="License Key", value=f"```{license_key}```", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

    # Try to DM the user their key
    try:
        user_embed = discord.Embed(
            title="Your Saint's Gen License",
            description="Your license key has been generated!",
            color=discord.Color.blue()
        )
        user_embed.add_field(name="License Key", value=f"```{license_key}```", inline=False)
        user_embed.add_field(name="Expires", value=expires_at.strftime("%Y-%m-%d %H:%M UTC"), inline=False)
        user_embed.add_field(
            name="How to Activate",
            value="1. Open Saint's Gen\n2. Enter the license key when prompted\n3. Click Activate",
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


@bot.tree.command(name="extend", description="Add days to an existing license")
@is_admin()
@app_commands.describe(
    days="Number of days to add",
    key="The license key to extend (optional)",
    user="The user whose license to extend (optional)"
)
async def extend(
    interaction: discord.Interaction,
    days: int,
    key: Optional[str] = None,
    user: Optional[discord.User] = None
):
    """Add days to an existing license."""
    if not key and not user:
        await interaction.response.send_message(
            "Please provide either a license key or a user.",
            ephemeral=True
        )
        return

    if days < 1:
        await interaction.response.send_message(
            "Days must be at least 1.",
            ephemeral=True
        )
        return

    if key:
        new_expiry = await extend_license(key, days)
        if new_expiry:
            expiry_dt = datetime.fromisoformat(new_expiry)
            await interaction.response.send_message(
                f"License extended by **{days} days**.\nNew expiry: **{expiry_dt.strftime('%Y-%m-%d %H:%M UTC')}**",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "License not found.",
                ephemeral=True
            )
    else:
        new_expiry = await extend_user_license(str(user.id), days)
        if new_expiry:
            expiry_dt = datetime.fromisoformat(new_expiry)
            await interaction.response.send_message(
                f"Extended {user.mention}'s license by **{days} days**.\nNew expiry: **{expiry_dt.strftime('%Y-%m-%d %H:%M UTC')}**",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"{user.mention} has no license to extend.",
                ephemeral=True
            )


@bot.tree.command(name="list", description="List all active licenses")
@is_admin()
async def list_licenses(interaction: discord.Interaction):
    """List all active licenses."""
    licenses = await get_all_active_licenses()

    if not licenses:
        await interaction.response.send_message("No active licenses.", ephemeral=True)
        return

    embed = discord.Embed(
        title="Active Licenses",
        color=discord.Color.blue()
    )

    # Show up to 10 licenses in the embed
    for lic in licenses[:10]:
        expires = datetime.fromisoformat(lic["expires_at"])
        days_left = (expires - datetime.utcnow()).days
        embed.add_field(
            name=f"{lic['discord_name']}",
            value=f"Expires: {expires.strftime('%Y-%m-%d')} ({days_left}d left)",
            inline=True
        )

    if len(licenses) > 10:
        embed.set_footer(text=f"Showing 10 of {len(licenses)} active licenses")

    # Add stats
    stats = await get_license_stats()
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
        embed.add_field(name="Revoked", value="Yes" if db_info["revoked"] else "No", inline=True)
    else:
        embed.add_field(name="In Database", value="No", inline=True)

    if info["error"]:
        embed.add_field(name="Error", value=info["error"], inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==================== USER COMMANDS ====================

@bot.tree.command(name="mykey", description="Get your license key (sent via DM)")
async def mykey(interaction: discord.Interaction):
    """Send the user their license key via DM."""
    license_data = await get_license_by_user(str(interaction.user.id))

    if not license_data:
        await interaction.response.send_message(
            "You don't have an active license. Contact an admin to purchase one.",
            ephemeral=True
        )
        return

    # Check if expired
    expires = datetime.fromisoformat(license_data["expires_at"])
    if expires < datetime.utcnow():
        await interaction.response.send_message(
            "Your license has expired. Contact an admin to renew.",
            ephemeral=True
        )
        return

    # Try to DM the key
    try:
        embed = discord.Embed(
            title="Your Saint's Gen License",
            color=discord.Color.blue()
        )
        embed.add_field(name="License Key", value=f"```{license_data['license_key']}```", inline=False)
        embed.add_field(name="Expires", value=expires.strftime("%Y-%m-%d %H:%M UTC"), inline=False)

        await interaction.user.send(embed=embed)
        await interaction.response.send_message(
            "Your license key has been sent to your DMs!",
            ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I couldn't DM you. Please enable DMs from server members.",
            ephemeral=True
        )


@bot.tree.command(name="status", description="Check your subscription status")
async def status(interaction: discord.Interaction):
    """Check subscription status."""
    license_data = await get_license_by_user(str(interaction.user.id))

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
        expires = datetime.fromisoformat(license_data["expires_at"])
        now = datetime.utcnow()

        if expires < now:
            embed.color = discord.Color.red()
            embed.description = "Your license has **expired**."
            embed.add_field(name="Expired On", value=expires.strftime("%Y-%m-%d"), inline=True)
        else:
            days_left = (expires - now).days
            embed.color = discord.Color.green()
            embed.description = "Your license is **active**."
            embed.add_field(name="Expires", value=expires.strftime("%Y-%m-%d"), inline=True)
            embed.add_field(name="Days Left", value=str(days_left), inline=True)

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
