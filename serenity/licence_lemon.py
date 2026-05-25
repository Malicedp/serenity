# Copyright © 2026 Daniel T Niamke. All rights reserved.
#
# Lemon Squeezy licence validation for Serenity.
# Replaces the old Flask licence server with direct Lemon Squeezy API calls.

import hashlib
import json
import os
import platform
import urllib.request
import urllib.parse
import urllib.error
import logging

log = logging.getLogger(__name__)

# ── Lemon Squeezy API ─────────────────────────────────────────────────────────
_LS_ACTIVATE_URL = "https://api.lemonsqueezy.com/v1/licenses/activate"
_LS_VALIDATE_URL = "https://api.lemonsqueezy.com/v1/licenses/validate"
_TIMEOUT         = 10

# ── Product name → tier mapping ───────────────────────────────────────────────
# Maps Lemon Squeezy product/variant names to internal tier names.
_PRODUCT_TIER_MAP = {
    "personal":       "personal",
    "solo":           "solo",
    "solo commercial":"solo",
    "small business": "small_business",
    "growth":         "growth",
    "enterprise":     "enterprise",
}


def get_machine_id() -> str:
    """Deterministic, privacy-safe machine fingerprint."""
    raw = platform.node() + platform.machine() + platform.system()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _post_form(url: str, data: dict) -> dict:
    """POST application/x-www-form-urlencoded to Lemon Squeezy."""
    body = urllib.parse.urlencode(data).encode()
    req  = urllib.request.Request(
        url,
        data=body,
        headers={"Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {}
        return {"error": body.get("message", f"HTTP {e.code}"), "code": e.code}
    except OSError as e:
        return {"offline": True, "error": str(e)}


def _tier_from_response(data: dict) -> str:
    """Extract tier from Lemon Squeezy response."""
    meta = data.get("meta", {})
    # Try product name first, then variant name
    for field in ["product_name", "variant_name"]:
        name = meta.get(field, "").lower()
        for keyword, tier in _PRODUCT_TIER_MAP.items():
            if keyword in name:
                return tier
    return "personal"


def activate_licence(key: str) -> dict:
    """
    Activate a licence key on this machine (first use).
    Returns: {"valid": bool, "tier": str, "instance_id": str, "reason": str}
    """
    machine_id = get_machine_id()
    data = _post_form(_LS_ACTIVATE_URL, {
        "license_key":   key.strip().upper(),
        "instance_name": f"Serenity-{machine_id}",
    })

    if data.get("offline"):
        return {"valid": False, "reason": "Cannot reach licence server", "offline": True}

    if data.get("error"):
        return {"valid": False, "reason": data["error"]}

    activated = data.get("activated", False)
    if not activated:
        return {"valid": False, "reason": data.get("error", "Activation failed")}

    instance_id = data.get("instance", {}).get("id", "")
    tier        = _tier_from_response(data)

    log.info("Licence activated: tier=%s instance=%s", tier, instance_id)
    return {
        "valid":       True,
        "tier":        tier,
        "instance_id": instance_id,
    }


def validate_licence(key: str, instance_id: str = "") -> dict:
    """
    Validate a licence key. Activates on first use if no instance_id.
    Returns: {"valid": bool, "tier": str, "instance_id": str, "reason": str}
    """
    if not key or not key.strip():
        return {"valid": False, "reason": "No key provided"}

    # First use — activate instead of validate
    if not instance_id:
        return activate_licence(key)

    data = _post_form(_LS_VALIDATE_URL, {
        "license_key": key.strip().upper(),
        "instance_id": instance_id,
    })

    if data.get("offline"):
        return {"valid": False, "reason": "Cannot reach licence server", "offline": True}

    if data.get("error"):
        return {"valid": False, "reason": data["error"]}

    valid  = data.get("valid", False)
    status = data.get("license_key", {}).get("status", "")

    if not valid or status != "active":
        reason_map = {
            "inactive":  "Licence inactive — contact serenitydev32@gmail.com",
            "expired":   "Licence expired — please renew",
            "disabled":  "Licence disabled — contact serenitydev32@gmail.com",
        }
        return {"valid": False, "reason": reason_map.get(status, "Invalid licence key")}

    tier = _tier_from_response(data)
    log.info("Licence valid: tier=%s", tier)
    return {
        "valid":       True,
        "tier":        tier,
        "instance_id": instance_id,
    }
