"""Shared Twikit runtime compatibility patch for X media/tweet delivery.

X periodically changes the client transaction JavaScript that Twikit parses.
The main news bot already carried this workaround inline; keeping it here gives
all publishers one implementation and avoids tactical/news drift.
"""
from __future__ import annotations

import re

_APPLIED = False


def apply_twikit_transaction_patch() -> bool:
    """Apply the repository's proven Twikit transaction parser workaround once."""
    global _APPLIED
    if _APPLIED:
        return True

    try:
        transaction = __import__(
            "twikit.x_client_transaction.transaction",
            fromlist=["ClientTransaction"],
        )
    except Exception as exc:
        print(f"[PATCH] twikit transaction module not found, skipping patch: {exc}")
        return False

    transaction.ON_DEMAND_FILE_REGEX = re.compile(
        r',(\d+):["\']ondemand\.s["\']',
        flags=(re.VERBOSE | re.MULTILINE),
    )
    transaction.ON_DEMAND_HASH_PATTERN = r',{}:"([0-9a-f]+)"'
    transaction.INDICES_REGEX = re.compile(
        r'(\(\w{1,2}\[(\d{1,2})\],\s*16\))+',
        flags=(re.VERBOSE | re.MULTILINE),
    )

    async def _patched_get_indices(self, home_page_response, session, headers):
        key_byte_indices = []
        response = self.validate_response(home_page_response) or self.home_page_response
        response_str = str(response)
        on_demand_file = transaction.ON_DEMAND_FILE_REGEX.search(response_str)
        if on_demand_file:
            index = on_demand_file.group(1)
            hash_regex = re.compile(transaction.ON_DEMAND_HASH_PATTERN.format(index))
            hash_match = hash_regex.search(response_str)
            if hash_match:
                filename = hash_match.group(1)
                url = (
                    "https://abs.twimg.com/responsive-web/client-web/"
                    f"ondemand.s.{filename}a.js"
                )
                js_response = await session.request(
                    method="GET",
                    url=url,
                    headers=headers,
                )
                matches = transaction.INDICES_REGEX.finditer(str(js_response.text))
                for item in matches:
                    key_byte_indices.append(item.group(2))
        if not key_byte_indices:
            raise Exception("Couldn't get KEY_BYTE indices")
        parsed = list(map(int, key_byte_indices))
        return parsed[0], parsed[1:]

    transaction.ClientTransaction.get_indices = _patched_get_indices
    _APPLIED = True
    print("[PATCH] twikit ClientTransaction.get_indices patched (shared runtime).")
    return True
