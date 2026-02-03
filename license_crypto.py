"""License key generation and verification using HMAC signatures."""
import hmac
import hashlib
import base64
import json
import time
import secrets
from datetime import datetime, timedelta
from typing import Optional, Tuple

def generate_license_key(
    secret_key: str,
    discord_id: str,
    days: int,
    discord_name: str = "",
    avatar_url: str = ""
) -> Tuple[str, datetime]:
    """
    Generate a signed license key.

    Returns:
        Tuple of (license_key, expiration_datetime)
    """
    # Calculate expiration
    expires_at = datetime.utcnow() + timedelta(days=days)
    expires_timestamp = int(expires_at.timestamp())

    # Create payload
    payload = {
        "uid": discord_id,
        "name": discord_name,
        "exp": expires_timestamp,
        "nonce": secrets.token_hex(8)  # Random nonce to make each key unique
    }

    # Add avatar URL if provided (shortened key for smaller license key)
    if avatar_url:
        payload["av"] = avatar_url

    # Encode payload as base64
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")

    # Create HMAC signature
    signature = hmac.new(
        secret_key.encode(),
        payload_b64.encode(),
        hashlib.sha256
    ).hexdigest()[:16]  # Use first 16 chars for shorter key

    # Format: SAINT-{payload}-{signature}
    license_key = f"SAINT-{payload_b64}-{signature}"

    return license_key, expires_at


def verify_license_key(secret_key: str, license_key: str) -> Tuple[bool, Optional[dict], str]:
    """
    Verify a license key.

    Returns:
        Tuple of (is_valid, payload_dict, error_message)
        - is_valid: True if key is valid and not expired
        - payload_dict: Decoded payload if valid, None otherwise
        - error_message: Description of why invalid, or "Valid" if valid
    """
    try:
        # Check format
        if not license_key.startswith("SAINT-"):
            return False, None, "Invalid key format"

        parts = license_key.split("-")
        if len(parts) != 3:
            return False, None, "Invalid key format"

        _, payload_b64, signature = parts

        # Verify signature
        expected_sig = hmac.new(
            secret_key.encode(),
            payload_b64.encode(),
            hashlib.sha256
        ).hexdigest()[:16]

        if not hmac.compare_digest(signature, expected_sig):
            return False, None, "Invalid signature"

        # Decode payload
        # Add padding if needed
        padding = 4 - (len(payload_b64) % 4)
        if padding != 4:
            payload_b64 += "=" * padding

        payload_json = base64.urlsafe_b64decode(payload_b64).decode()
        payload = json.loads(payload_json)

        # Check expiration
        expires_timestamp = payload.get("exp", 0)
        if time.time() > expires_timestamp:
            expires_dt = datetime.fromtimestamp(expires_timestamp)
            return False, payload, f"Key expired on {expires_dt.strftime('%Y-%m-%d %H:%M')}"

        return True, payload, "Valid"

    except Exception as e:
        return False, None, f"Verification error: {str(e)}"


def get_key_info(secret_key: str, license_key: str) -> dict:
    """
    Get detailed information about a license key.

    Returns dict with:
        - valid: bool
        - discord_id: str or None
        - expires_at: datetime or None
        - expired: bool
        - error: str or None
    """
    is_valid, payload, message = verify_license_key(secret_key, license_key)

    info = {
        "valid": is_valid,
        "discord_id": None,
        "expires_at": None,
        "expired": False,
        "error": None if is_valid else message
    }

    if payload:
        info["discord_id"] = payload.get("uid")
        exp_ts = payload.get("exp", 0)
        info["expires_at"] = datetime.fromtimestamp(exp_ts)
        info["expired"] = time.time() > exp_ts

    return info


# For testing
if __name__ == "__main__":
    test_secret = "my_test_secret_key_123"

    # Generate a key
    key, expires = generate_license_key(test_secret, "123456789", 30)
    print(f"Generated key: {key}")
    print(f"Expires: {expires}")

    # Verify it
    valid, payload, msg = verify_license_key(test_secret, key)
    print(f"Valid: {valid}, Message: {msg}")
    print(f"Payload: {payload}")

    # Test expired key (generate with -1 days)
    old_key, _ = generate_license_key(test_secret, "123456789", -1)
    valid, payload, msg = verify_license_key(test_secret, old_key)
    print(f"\nExpired key valid: {valid}, Message: {msg}")

    # Test invalid key
    valid, payload, msg = verify_license_key(test_secret, "SAINT-invalid-key")
    print(f"\nInvalid key valid: {valid}, Message: {msg}")
