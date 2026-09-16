"""Compatibility shim for Twikit's X SearchTimeline transport.

Twikit 2.3.3 still sends SearchTimeline through GraphQL GET. X currently
returns 404 for that transport while accepting the same operation via GraphQL
POST. This module changes only that transport call; it does not alter search
queries, source selection, evidence matching, verification, or publication
policy.

If Twikit internals are unavailable or change incompatibly, installation returns
False. Callers still use the normal search path, whose exceptions are counted by
the existing fail-closed X health gate.
"""

from __future__ import annotations

from typing import Any


def install_twikit_search_post_patch() -> bool:
    """Make Twikit SearchTimeline use gql_post, idempotently and narrowly."""
    try:
        from twikit.client import gql as twikit_gql
    except Exception:
        return False

    gql_client = getattr(twikit_gql, "GQLClient", None)
    endpoint = getattr(twikit_gql, "Endpoint", None)
    features = getattr(twikit_gql, "FEATURES", None)
    if gql_client is None or endpoint is None or features is None:
        return False

    current = getattr(gql_client, "search_timeline", None)
    if current is None:
        return False
    if getattr(current, "_fpl_vortex_search_post_patch", False):
        return True

    async def _search_timeline_via_post(
        self: Any,
        query: str,
        product: str,
        count: int,
        cursor: str | None,
    ) -> Any:
        variables = {
            "rawQuery": query,
            "count": count,
            "querySource": "typed_query",
            "product": product,
        }
        if cursor is not None:
            variables["cursor"] = cursor
        return await self.gql_post(
            endpoint.SEARCH_TIMELINE,
            variables,
            features,
        )

    _search_timeline_via_post._fpl_vortex_search_post_patch = True
    gql_client.search_timeline = _search_timeline_via_post
    return True
