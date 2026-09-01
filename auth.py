from __future__ import annotations

import json
import logging
import os
import sys
import time

from schwab.auth import easy_client

logger = logging.getLogger("schwab_bot.auth")

# 6.5 days in seconds — refresh before Schwab's 7-day hard expiry.
MAX_TOKEN_AGE_SECONDS = 6.5 * 24 * 60 * 60


def _token_needs_login(token_path: str) -> bool:
    try:
        with open(token_path) as f:
            created = float(json.load(f).get("creation_timestamp") or 0)
    except Exception:
        return True  # missing / unreadable / corrupt -> login flow
    if created <= 0:
        return True
    return (time.time() - created) > MAX_TOKEN_AGE_SECONDS


def get_client(api_key: str, app_secret: str, callback_url: str, token_path: str):
    if _token_needs_login(token_path):
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise RuntimeError(
                f"Schwab login required (token at {token_path} is missing, "
                f"unreadable, or older than ~6.5 days), but this session has "
                f"no interactive terminal — the browser login flow would hang. "
                f"Re-authenticate on an interactive machine "
                f"(python run.py check) and copy the token file here."
            )
        logger.warning(
            "Schwab login required — a browser window will open for the login "
            "+ consent flow. Log in, approve, and you'll be redirected to "
            "your callback URL. (Token path: %s)",
            token_path,
        )

    client = easy_client(
        api_key=api_key,
        app_secret=app_secret,
        callback_url=callback_url,
        token_path=token_path,
        max_token_age=MAX_TOKEN_AGE_SECONDS,
        interactive=True,
    )
    logger.info("Authenticated. Token stored at %s", token_path)
    return client


def resolve_account_hash(client, account_index: int = 0) -> str:
    resp = client.get_account_numbers()
    resp.raise_for_status()
    accounts = resp.json()
    if not accounts:
        raise RuntimeError("No linked accounts returned by Schwab.")
    # Reject negatives explicitly: Python's list indexing would wrap them to
    # the END of the list and silently trade the wrong account.
    if account_index < 0 or account_index >= len(accounts):
        raise RuntimeError(
            f"SCHWAB_ACCOUNT_INDEX={account_index} is out of range: "
            f"{len(accounts)} account(s) are linked (valid: 0.."
            f"{len(accounts) - 1})."
        )
    entry = accounts[account_index]
    # Each entry looks like {"accountNumber": "...", "hashValue": "..."}.
    account_number = entry.get("accountNumber")
    hash_value = entry.get("hashValue")
    if not hash_value:
        raise RuntimeError(
            "Schwab account entry is missing 'hashValue' — cannot resolve the "
            "account hash needed for order/account endpoints."
        )
    masked = account_number[-4:] if account_number else "????"
    logger.info("Using account ...%s (index %d of %d).",
                masked, account_index, len(accounts))
    return hash_value
