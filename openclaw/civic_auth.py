"""
civic_auth.py — Civic identity verification wrapper.

All PHI (bill data, user insurance plan) must pass through verify_user()
before being stored in Redis or uploaded to Contextual AI.

Civic provides a sybil-resistant identity layer that verifies a user is a
unique human without requiring PII like SSN or DOB.
"""

import os
import time
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

CIVIC_API_KEY = os.environ.get("CIVIC_API_KEY", "")
CIVIC_API_BASE = "https://api.civic.com/sip/prod"


def verify_user(civic_token: str, civic_api_key: Optional[str] = None) -> dict:
    """
    Verify a user's identity via Civic's SIP (Secure Identity Platform).

    Args:
        civic_token: The JWT token received from the Civic client SDK on the user's device.
        civic_api_key: Override the env var if needed.

    Returns:
        dict with keys:
          - verified: bool
          - user_id: str (stable hashed identifier, safe to use as Redis key)
          - error: str (only present if verified=False)
    """
    key = civic_api_key or CIVIC_API_KEY
    if not key:
        # Dev mode: skip verification if no key configured
        print("WARNING: CIVIC_API_KEY not set — skipping verification (dev mode only)")
        return {"verified": True, "user_id": f"dev_{civic_token[:16]}", "dev_mode": True}

    if not civic_token:
        return {"verified": False, "error": "No Civic token provided"}

    try:
        resp = requests.post(
            f"{CIVIC_API_BASE}/scoperequest/jwt",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={"jwtToken": civic_token},
            timeout=10,
        )

        if resp.status_code == 200:
            data = resp.json()
            # Civic returns userId as a stable hash of the user's identity
            user_id = data.get("userId") or data.get("data", {}).get("userId", "")
            if user_id:
                return {"verified": True, "user_id": user_id}
            return {"verified": False, "error": "Civic returned no userId"}

        return {
            "verified": False,
            "error": f"Civic API error: {resp.status_code} {resp.text[:200]}",
        }

    except requests.RequestException as e:
        return {"verified": False, "error": f"Civic request failed: {e}"}


def require_verified(civic_token: str) -> str:
    """
    Verify user or raise PermissionError. Returns stable user_id on success.
    Use this as a gate before any PHI storage or upload.

    Example:
        user_id = require_verified(telegram_civic_token)
        redis_cache.store_bill_result(user_id, result)
    """
    result = verify_user(civic_token)
    if not result.get("verified"):
        raise PermissionError(f"Civic identity verification failed: {result.get('error')}")
    return result["user_id"]


if __name__ == "__main__":
    # Test with a dummy token in dev mode
    result = verify_user("test_token_12345")
    print(result)
