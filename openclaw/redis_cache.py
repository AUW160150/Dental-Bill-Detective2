"""
redis_cache.py — Redis-backed user state for dental bill detective.

Stores:
  - User's insurance plan (no TTL — persists until user updates)
  - Bill audit results (90-day TTL)
  - Bill history list (last 10 bills)
"""

import json
import os
from typing import Optional

import redis
from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
BILL_RESULT_TTL = 60 * 60 * 24 * 90  # 90 days in seconds

_client: Optional[redis.Redis] = None


def get_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(REDIS_URL, decode_responses=True)
    return _client


# ── Insurance Plan ─────────────────────────────────────────────────────────────

def get_user_plan(user_id: str) -> Optional[dict]:
    """Return the user's stored insurance plan, or None if not set."""
    r = get_client()
    raw = r.get(f"user:{user_id}:plan")
    return json.loads(raw) if raw else None


def set_user_plan(user_id: str, plan: dict) -> None:
    """
    Persist user's insurance plan with no TTL.
    plan should include: insurer, plan_name, plan_type (PPO/HMO/etc),
    member_id, group_id, network (in/out), annual_maximum, used_benefits.
    """
    r = get_client()
    r.set(f"user:{user_id}:plan", json.dumps(plan))


# ── Bill Results ───────────────────────────────────────────────────────────────

def store_bill_result(user_id: str, result: dict) -> None:
    """
    Cache a bill audit result for 90 days.
    Also prepends the bill_hash to the user's history list (capped at 10).
    """
    r = get_client()
    bill_hash = result.get("bill_hash", "unknown")

    # Store the full result
    r.setex(
        f"user:{user_id}:bill:{bill_hash}",
        BILL_RESULT_TTL,
        json.dumps(result),
    )

    # Update bill history list
    history_key = f"user:{user_id}:bill_history"
    r.lpush(history_key, bill_hash)
    r.ltrim(history_key, 0, 9)  # Keep last 10
    r.expire(history_key, BILL_RESULT_TTL)


def get_bill_result(user_id: str, bill_hash: str) -> Optional[dict]:
    """Retrieve a specific bill audit result by hash."""
    r = get_client()
    raw = r.get(f"user:{user_id}:bill:{bill_hash}")
    return json.loads(raw) if raw else None


def get_user_history(user_id: str) -> dict:
    """
    Return the user's insurance plan + list of past bill summaries.
    Used by the Claude tool-use loop to provide context.
    """
    r = get_client()
    plan = get_user_plan(user_id)

    history_key = f"user:{user_id}:bill_history"
    bill_hashes = r.lrange(history_key, 0, 9)

    bills = []
    for bh in bill_hashes:
        result = get_bill_result(user_id, bh)
        if result:
            # Return lightweight summary only
            summary = result.get("summary", {})
            bills.append({
                "bill_hash": bh,
                "total_billed": summary.get("total_billed"),
                "overcharge_amount": summary.get("overcharge_amount"),
                "overcharge_percent": summary.get("overcharge_percent"),
                "flags": summary.get("flags_found", []),
            })

    return {
        "user_id": user_id,
        "insurance_plan": plan,
        "bill_history": bills,
    }


if __name__ == "__main__":
    # Basic smoke test
    test_user = "test_user_123"
    set_user_plan(test_user, {"insurer": "Delta Dental", "plan_type": "PPO"})
    plan = get_user_plan(test_user)
    print(f"Plan: {plan}")

    store_bill_result(test_user, {
        "bill_hash": "abc123",
        "summary": {"total_billed": 1200, "overcharge_amount": 350, "overcharge_percent": 29.2},
    })
    history = get_user_history(test_user)
    print(f"History: {json.dumps(history, indent=2)}")
