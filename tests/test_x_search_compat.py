from __future__ import annotations

import asyncio

from src.x_search_compat import install_twikit_search_post_patch


def test_search_timeline_uses_post_with_same_variables_and_features():
    from twikit.client import gql as twikit_gql

    assert install_twikit_search_post_patch() is True
    calls = []

    class FakeClient:
        async def gql_post(self, endpoint, variables, features):
            calls.append((endpoint, variables, features))
            return {"ok": True}

        async def gql_get(self, *_args, **_kwargs):
            raise AssertionError("SearchTimeline must not use GET")

    result = asyncio.run(
        twikit_gql.GQLClient.search_timeline(
            FakeClient(), "from:fabrizioromano baleba", "Latest", 20, "CURSOR"
        )
    )

    assert result == {"ok": True}
    assert len(calls) == 1
    endpoint, variables, features = calls[0]
    assert endpoint == twikit_gql.Endpoint.SEARCH_TIMELINE
    assert variables == {
        "rawQuery": "from:fabrizioromano baleba",
        "count": 20,
        "querySource": "typed_query",
        "product": "Latest",
        "cursor": "CURSOR",
    }
    assert features is twikit_gql.FEATURES


def test_search_timeline_omits_cursor_when_none():
    from twikit.client import gql as twikit_gql

    assert install_twikit_search_post_patch() is True
    calls = []

    class FakeClient:
        async def gql_post(self, endpoint, variables, features):
            calls.append((endpoint, variables, features))
            return []

    asyncio.run(
        twikit_gql.GQLClient.search_timeline(
            FakeClient(), "from:bendinnery saka", "Latest", 20, None
        )
    )
    assert "cursor" not in calls[0][1]


def test_patch_installation_is_idempotent():
    from twikit.client import gql as twikit_gql

    assert install_twikit_search_post_patch() is True
    patched = twikit_gql.GQLClient.search_timeline
    assert getattr(patched, "_fpl_vortex_search_post_patch", False) is True
    assert install_twikit_search_post_patch() is True
    assert twikit_gql.GQLClient.search_timeline is patched
