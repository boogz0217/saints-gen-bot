"""Async PostgreSQL database operations for license management."""
import asyncpg
import ssl
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import os
import sys

# Database URL from environment (Railway provides this automatically)
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Connection pool
_pool: Optional[asyncpg.Pool] = None


def _parse_database_url(url: str) -> tuple:
    """Parse DATABASE_URL and extract SSL mode if present."""
    # Remove sslmode parameter from URL (asyncpg handles it separately)
    if '?sslmode=' in url:
        base_url, params = url.split('?', 1)
        # Check if SSL is required
        use_ssl = 'sslmode=require' in params or 'sslmode=verify' in params
        return base_url, use_ssl
    return url, False


async def get_pool() -> asyncpg.Pool:
    """Get or create the connection pool."""
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            print("ERROR: DATABASE_URL environment variable is not set!")
            print("Please add a PostgreSQL database to your Railway project.")
            sys.exit(1)

        # Parse URL and handle SSL
        clean_url, use_ssl = _parse_database_url(DATABASE_URL)

        if use_ssl:
            # Create SSL context for secure connection
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            _pool = await asyncpg.create_pool(clean_url, min_size=1, max_size=10, ssl=ssl_ctx)
        else:
            _pool = await asyncpg.create_pool(clean_url, min_size=1, max_size=10)
    return _pool


async def close_pool():
    """Close the connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def init_db():
    """Initialize the database and create tables if they don't exist."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                license_key TEXT PRIMARY KEY,
                discord_id TEXT NOT NULL,
                discord_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                revoked INTEGER DEFAULT 0,
                hwid TEXT,
                expiry_notified INTEGER DEFAULT 0,
                product TEXT DEFAULT 'saints-gen'
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_id ON licenses(discord_id)
        """)
        # Add columns if they don't exist (for existing databases)
        try:
            await conn.execute("ALTER TABLE licenses ADD COLUMN IF NOT EXISTS expiry_notified INTEGER DEFAULT 0")
        except:
            pass
        try:
            await conn.execute("ALTER TABLE licenses ADD COLUMN IF NOT EXISTS product TEXT DEFAULT 'saints-gen'")
        except:
            pass
        # Create product index after ensuring column exists
        try:
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_product ON licenses(product)
            """)
        except:
            pass
        # Add pending_days column for licenses that haven't been activated yet
        try:
            await conn.execute("ALTER TABLE licenses ADD COLUMN IF NOT EXISTS pending_days INTEGER DEFAULT NULL")
        except:
            pass

        # Create pending_orders table for Shopify orders without Discord ID
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_orders (
                id SERIAL PRIMARY KEY,
                email TEXT NOT NULL,
                order_number TEXT,
                customer_name TEXT,
                product TEXT NOT NULL,
                days INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                claimed INTEGER DEFAULT 0,
                claimed_by TEXT,
                claimed_at TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending_email ON pending_orders(email) WHERE claimed = 0
        """)


async def add_pending_order(
    email: str,
    product: str,
    days: int,
    order_number: str = None,
    customer_name: str = None
) -> int:
    """Add a pending order that needs Discord linking. Returns order ID."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO pending_orders (email, order_number, customer_name, product, days)
               VALUES ($1, $2, $3, $4, $5)
               RETURNING id""",
            email.lower().strip(), order_number, customer_name, product, days
        )
        return row["id"]


async def get_pending_order_by_email(email: str) -> Optional[Dict]:
    """Get unclaimed pending order by email."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM pending_orders
               WHERE email = $1 AND claimed = 0
               ORDER BY created_at DESC
               LIMIT 1""",
            email.lower().strip()
        )
        if row:
            return dict(row)
    return None


async def claim_pending_order(order_id: int, discord_id: str) -> bool:
    """Mark a pending order as claimed by a Discord user."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE pending_orders
               SET claimed = 1, claimed_by = $1, claimed_at = $2
               WHERE id = $3 AND claimed = 0""",
            discord_id, datetime.utcnow(), order_id
        )
        return "UPDATE 1" in result


async def init_linked_accounts_table():
    """Initialize the linked_accounts table for pre-purchase Discord linking."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS linked_accounts (
                email TEXT PRIMARY KEY,
                discord_id TEXT NOT NULL,
                discord_name TEXT,
                linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


async def save_linked_account(email: str, discord_id: str, discord_name: str = None) -> bool:
    """Save a pre-purchase Discord link (email -> Discord ID)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Upsert - update if exists, insert if not
        await conn.execute("""
            INSERT INTO linked_accounts (email, discord_id, discord_name, linked_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (email) DO UPDATE SET
                discord_id = $2,
                discord_name = $3,
                linked_at = $4
        """, email.lower().strip(), discord_id, discord_name, datetime.utcnow())
        return True


async def get_linked_discord_id(email: str) -> Optional[Dict]:
    """Get Discord ID for a linked email (pre-purchase linking)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM linked_accounts WHERE email = $1",
            email.lower().strip()
        )
        if row:
            return dict(row)
    return None


async def add_license(
    license_key: str,
    discord_id: str,
    discord_name: str,
    expires_at: datetime,
    product: str = "saints-gen",
    pending_days: int = None
) -> bool:
    """Add a new license to the database. If pending_days is set, countdown won't start until activation."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO licenses (license_key, discord_id, discord_name, expires_at, product, pending_days)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                license_key, discord_id, discord_name, expires_at, product, pending_days
            )
        return True
    except asyncpg.UniqueViolationError:
        return False  # Key already exists


async def get_license_by_key(license_key: str) -> Optional[Dict]:
    """Get license info by key."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM licenses WHERE license_key = $1",
            license_key
        )
        if row:
            return dict(row)
    return None


async def get_license_by_user(discord_id: str, product: str = None) -> Optional[Dict]:
    """Get the most recent active license for a user, optionally filtered by product."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if product:
            row = await conn.fetchrow(
                """SELECT * FROM licenses
                   WHERE discord_id = $1 AND revoked = 0 AND product = $2
                   ORDER BY expires_at DESC LIMIT 1""",
                discord_id, product
            )
        else:
            row = await conn.fetchrow(
                """SELECT * FROM licenses
                   WHERE discord_id = $1 AND revoked = 0
                   ORDER BY expires_at DESC LIMIT 1""",
                discord_id
            )
        if row:
            return dict(row)
    return None


async def get_all_licenses_for_user(discord_id: str) -> List[Dict]:
    """Get all licenses for a user."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM licenses WHERE discord_id = $1 ORDER BY created_at DESC",
            discord_id
        )
        return [dict(row) for row in rows]


async def revoke_license(license_key: str) -> bool:
    """Revoke a license by key."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE licenses SET revoked = 1 WHERE license_key = $1",
            license_key
        )
        return result != "UPDATE 0"


async def revoke_user_licenses(discord_id: str) -> int:
    """Revoke all licenses for a user. Returns count of revoked licenses."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE licenses SET revoked = 1 WHERE discord_id = $1 AND revoked = 0",
            discord_id
        )
        # Parse "UPDATE N" to get count
        return int(result.split()[-1]) if result else 0


async def delete_license(license_key: str) -> bool:
    """Permanently delete a license by key."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM licenses WHERE license_key = $1",
            license_key
        )
        return result != "DELETE 0"


async def delete_user_licenses(discord_id: str) -> int:
    """Permanently delete all licenses for a user. Returns count of deleted licenses."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM licenses WHERE discord_id = $1",
            discord_id
        )
        # Parse "DELETE N" to get count
        return int(result.split()[-1]) if result else 0


async def extend_license(license_key: str, days: int) -> Optional[str]:
    """Extend or reduce a license by adding/removing days. Returns new expiry date or None if not found."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT expires_at FROM licenses WHERE license_key = $1",
            license_key
        )
        if not row:
            return None

        # Parse current expiry and add days
        current_expiry = row["expires_at"]
        if isinstance(current_expiry, str):
            current_expiry = datetime.fromisoformat(current_expiry)
        now = datetime.utcnow()

        if days > 0:
            # If adding days and already expired, extend from now; otherwise extend from current expiry
            base = max(current_expiry, now)
        else:
            # If removing days, always use current expiry
            base = current_expiry

        new_expiry = base + timedelta(days=days)

        await conn.execute(
            "UPDATE licenses SET expires_at = $1, revoked = 0 WHERE license_key = $2",
            new_expiry, license_key
        )
        return new_expiry.isoformat()


async def extend_user_license(discord_id: str, days: int) -> Optional[str]:
    """Extend the most recent license for a user. Returns new expiry date or None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT license_key, expires_at FROM licenses WHERE discord_id = $1 ORDER BY expires_at DESC LIMIT 1",
            discord_id
        )
        if not row:
            return None

        return await extend_license(row["license_key"], days)


async def get_all_active_licenses(product: str = None) -> List[Dict]:
    """Get all active (non-revoked, non-expired) licenses, optionally filtered by product."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        now = datetime.utcnow()
        if product:
            rows = await conn.fetch(
                """SELECT * FROM licenses
                   WHERE revoked = 0 AND expires_at > $1 AND product = $2
                   ORDER BY expires_at ASC""",
                now, product
            )
        else:
            rows = await conn.fetch(
                """SELECT * FROM licenses
                   WHERE revoked = 0 AND expires_at > $1
                   ORDER BY expires_at ASC""",
                now
            )
        return [dict(row) for row in rows]


async def get_license_stats(product: str = None) -> Dict:
    """Get license statistics, optionally filtered by product."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        now = datetime.utcnow()

        if product:
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM licenses WHERE product = $1", product
            )
            active = await conn.fetchval(
                "SELECT COUNT(*) FROM licenses WHERE revoked = 0 AND expires_at > $1 AND product = $2",
                now, product
            )
            revoked = await conn.fetchval(
                "SELECT COUNT(*) FROM licenses WHERE revoked = 1 AND product = $1", product
            )
            expired = await conn.fetchval(
                "SELECT COUNT(*) FROM licenses WHERE revoked = 0 AND expires_at <= $1 AND product = $2",
                now, product
            )
        else:
            total = await conn.fetchval("SELECT COUNT(*) FROM licenses")
            active = await conn.fetchval(
                "SELECT COUNT(*) FROM licenses WHERE revoked = 0 AND expires_at > $1",
                now
            )
            revoked = await conn.fetchval(
                "SELECT COUNT(*) FROM licenses WHERE revoked = 1"
            )
            expired = await conn.fetchval(
                "SELECT COUNT(*) FROM licenses WHERE revoked = 0 AND expires_at <= $1",
                now
            )

        return {
            "total": total,
            "active": active,
            "revoked": revoked,
            "expired": expired
        }


async def reset_hwid_by_key(license_key: str) -> bool:
    """Reset hardware ID binding for a license. Returns True if found and reset."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE licenses SET hwid = NULL WHERE license_key = $1",
            license_key
        )
        return result != "UPDATE 0"


async def reset_hwid_by_user(discord_id: str) -> int:
    """Reset hardware ID binding for all licenses of a user. Returns count reset."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE licenses SET hwid = NULL WHERE discord_id = $1",
            discord_id
        )
        # Parse "UPDATE N" to get count
        return int(result.split()[-1]) if result else 0


async def reset_all_hwids(product: str = None) -> int:
    """Reset hardware ID binding for all licenses, optionally filtered by product. Returns count reset."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if product:
            result = await conn.execute(
                "UPDATE licenses SET hwid = NULL WHERE hwid IS NOT NULL AND product = $1",
                product
            )
        else:
            result = await conn.execute(
                "UPDATE licenses SET hwid = NULL WHERE hwid IS NOT NULL"
            )
        # Parse "UPDATE N" to get count
        return int(result.split()[-1]) if result else 0


async def get_hwid_by_key(license_key: str) -> Optional[str]:
    """Get the hardware ID bound to a license."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT hwid FROM licenses WHERE license_key = $1",
            license_key
        )
        if row:
            return row["hwid"]
    return None


async def get_newly_expired_licenses() -> List[Dict]:
    """Get licenses that expired but haven't been notified yet."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        now = datetime.utcnow()
        rows = await conn.fetch(
            """SELECT * FROM licenses
               WHERE expires_at <= $1 AND revoked = 0 AND expiry_notified = 0""",
            now
        )
        return [dict(row) for row in rows]


async def mark_expiry_notified(license_key: str) -> bool:
    """Mark a license as having been notified about expiry."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE licenses SET expiry_notified = 1 WHERE license_key = $1",
            license_key
        )
        return result != "UPDATE 0"


async def has_active_license(discord_id: str) -> bool:
    """Check if a user has any active (non-expired, non-revoked) license."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        now = datetime.utcnow()
        count = await conn.fetchval(
            """SELECT COUNT(*) FROM licenses
               WHERE discord_id = $1 AND revoked = 0 AND expires_at > $2""",
            discord_id, now
        )
        return count > 0


async def has_active_license_for_product(discord_id: str, product: str) -> bool:
    """Check if a user has any active (non-expired, non-revoked) license for a specific product."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        now = datetime.utcnow()
        count = await conn.fetchval(
            """SELECT COUNT(*) FROM licenses
               WHERE discord_id = $1 AND product = $2 AND revoked = 0 AND expires_at > $3""",
            discord_id, product, now
        )
        return count > 0


# ==================== SHOPIFY NOTIFICATIONS ====================

async def init_notifications_table():
    """Create the shopify_notifications table if it doesn't exist."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shopify_notifications (
                id SERIAL PRIMARY KEY,
                discord_id TEXT NOT NULL,
                license_key TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                product TEXT NOT NULL,
                customer_name TEXT,
                email TEXT,
                order_number TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                delivered INTEGER DEFAULT 0,
                delivery_attempts INTEGER DEFAULT 0,
                last_attempt_at TIMESTAMP,
                error_message TEXT
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_notifications_pending
            ON shopify_notifications(delivered) WHERE delivered = 0
        """)


async def add_notification(
    discord_id: str,
    license_key: str,
    expires_at: datetime,
    product: str,
    customer_name: str = None,
    email: str = None,
    order_number: str = None
) -> int:
    """Add a new notification to the queue. Returns the notification ID."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO shopify_notifications
               (discord_id, license_key, expires_at, product, customer_name, email, order_number)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               RETURNING id""",
            discord_id, license_key, expires_at, product, customer_name, email, order_number
        )
        return row["id"]


async def get_pending_notifications(limit: int = 50) -> List[Dict]:
    """Get pending notifications that haven't been delivered yet."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM shopify_notifications
               WHERE delivered = 0 AND delivery_attempts < 5
               ORDER BY created_at ASC
               LIMIT $1""",
            limit
        )
        return [dict(row) for row in rows]


async def mark_notification_delivered(notification_id: int) -> bool:
    """Mark a notification as successfully delivered."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE shopify_notifications
               SET delivered = 1, last_attempt_at = $1
               WHERE id = $2""",
            datetime.utcnow(), notification_id
        )
        return result != "UPDATE 0"


async def mark_notification_failed(notification_id: int, error: str = None) -> bool:
    """Mark a notification attempt as failed (will retry later)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE shopify_notifications
               SET delivery_attempts = delivery_attempts + 1,
                   last_attempt_at = $1,
                   error_message = $2
               WHERE id = $3""",
            datetime.utcnow(), error, notification_id
        )
        return result != "UPDATE 0"


async def get_failed_notifications() -> List[Dict]:
    """Get notifications that failed to deliver after max attempts."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM shopify_notifications
               WHERE delivered = 0 AND delivery_attempts >= 5
               ORDER BY created_at DESC"""
        )
        return [dict(row) for row in rows]


# ==================== REFERRALS ====================

async def init_referrals_table():
    """Create the referrals table if it doesn't exist."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id SERIAL PRIMARY KEY,
                referrer_id TEXT NOT NULL,
                referred_id TEXT NOT NULL,
                product TEXT NOT NULL DEFAULT 'saints-shot',
                days_awarded INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(referrer_id, referred_id, product)
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_referrals_referred ON referrals(referred_id)
        """)


async def get_referral_count_received(discord_id: str, product: str = "saints-shot") -> int:
    """Get how many times a user has been referred (received referrals)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM referrals WHERE referred_id = $1 AND product = $2",
            discord_id, product
        )
        return count or 0


async def get_referral_count_given(discord_id: str, product: str = "saints-shot") -> int:
    """Get how many referrals a user has given (referred others)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = $1 AND product = $2",
            discord_id, product
        )
        return count or 0


async def has_been_referred_by(referred_id: str, referrer_id: str, product: str = "saints-shot") -> bool:
    """Check if a user has already been referred by a specific referrer."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM referrals WHERE referred_id = $1 AND referrer_id = $2 AND product = $3",
            referred_id, referrer_id, product
        )
        return count > 0


async def add_referral(referrer_id: str, referred_id: str, days_awarded: int, product: str = "saints-shot") -> bool:
    """Add a new referral record. Returns True if successful."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO referrals (referrer_id, referred_id, product, days_awarded)
                   VALUES ($1, $2, $3, $4)""",
                referrer_id, referred_id, product, days_awarded
            )
        return True
    except Exception:
        return False  # Duplicate or other error


async def get_referral_stats(discord_id: str, product: str = "saints-shot") -> Dict:
    """Get referral statistics for a user."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        given = await conn.fetchval(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = $1 AND product = $2",
            discord_id, product
        )
        received = await conn.fetchval(
            "SELECT COUNT(*) FROM referrals WHERE referred_id = $1 AND product = $2",
            discord_id, product
        )
        total_days_earned = await conn.fetchval(
            "SELECT COALESCE(SUM(days_awarded), 0) FROM referrals WHERE referred_id = $1 AND product = $2",
            discord_id, product
        )
        return {
            "given": given or 0,
            "received": received or 0,
            "total_days_earned": total_days_earned or 0
        }


async def extend_user_license_for_product(discord_id: str, days: int, product: str) -> Optional[str]:
    """Extend the most recent license for a user for a specific product. Returns new expiry date or None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT license_key, expires_at FROM licenses WHERE discord_id = $1 AND product = $2 ORDER BY expires_at DESC LIMIT 1",
            discord_id, product
        )
        if not row:
            return None

        return await extend_license(row["license_key"], days)


# ==================== PURCHASES (Email-based redemption) ====================

async def init_purchases_table():
    """Create the purchases table if it doesn't exist."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id SERIAL PRIMARY KEY,
                email TEXT NOT NULL,
                customer_name TEXT,
                product TEXT NOT NULL,
                days INTEGER NOT NULL,
                order_number TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                redeemed INTEGER DEFAULT 0,
                redeemed_by TEXT,
                redeemed_at TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_purchases_email
            ON purchases(email) WHERE redeemed = 0
        """)


async def add_purchase(
    email: str,
    product: str,
    days: int,
    customer_name: str = None,
    order_number: str = None
) -> int:
    """Add a new purchase. Returns purchase ID."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO purchases (email, customer_name, product, days, order_number)
               VALUES ($1, $2, $3, $4, $5)
               RETURNING id""",
            email.lower().strip(), customer_name, product, days, order_number
        )
        return row["id"]


async def redeem_by_email(email: str, discord_id: str) -> Optional[Dict]:
    """
    Redeem a purchase by email.
    Returns the purchase info if successful, None if not found or already redeemed.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Get unredeemed purchase for this email
        row = await conn.fetchrow(
            """SELECT * FROM purchases
               WHERE LOWER(email) = LOWER($1) AND redeemed = 0
               ORDER BY created_at DESC
               LIMIT 1""",
            email.strip()
        )
        if not row:
            return None

        # Mark as redeemed
        await conn.execute(
            """UPDATE purchases
               SET redeemed = 1, redeemed_by = $1, redeemed_at = $2
               WHERE id = $3""",
            discord_id, datetime.utcnow(), row["id"]
        )

        return dict(row)


async def cleanup_duplicate_licenses() -> Dict:
    """
    Find and delete duplicate licenses, keeping only the one with the most days remaining.
    Returns stats about what was cleaned up.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        now = datetime.utcnow()

        # Find all duplicates: users with more than 1 active license for the same product
        duplicates = await conn.fetch("""
            SELECT discord_id, product, COUNT(*) as count
            FROM licenses
            WHERE revoked = 0
            GROUP BY discord_id, product
            HAVING COUNT(*) > 1
        """)

        total_deleted = 0
        affected_users = []

        for dup in duplicates:
            discord_id = dup["discord_id"]
            product = dup["product"]

            # Get all licenses for this user+product, ordered by expiry (best first)
            licenses = await conn.fetch("""
                SELECT license_key, expires_at, discord_name
                FROM licenses
                WHERE discord_id = $1 AND product = $2 AND revoked = 0
                ORDER BY expires_at DESC
            """, discord_id, product)

            if len(licenses) <= 1:
                continue

            # Keep the first one (highest expiry), delete the rest
            to_keep = licenses[0]
            to_delete = licenses[1:]

            for lic in to_delete:
                await conn.execute(
                    "DELETE FROM licenses WHERE license_key = $1",
                    lic["license_key"]
                )
                total_deleted += 1

            affected_users.append({
                "discord_id": discord_id,
                "discord_name": to_keep["discord_name"],
                "product": product,
                "kept_expiry": to_keep["expires_at"],
                "deleted_count": len(to_delete)
            })

        return {
            "total_deleted": total_deleted,
            "affected_users": affected_users
        }
