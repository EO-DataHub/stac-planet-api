"""Tests for POST /search filter preservation.

Regression test for EO-DataHub/platform-bugs#226: CQL2 filters (date range,
cloud cover, etc.) were silently dropped when the post_search endpoint used
BaseSearchPostRequest instead of POST_REQUEST_MODEL.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from stac_fastapi.types.search import BaseSearchPostRequest

from stac_planet_api.api import app


def _make_mock_client() -> AsyncMock:
    """Create a mock httpx.AsyncClient that returns an empty Planet API response."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"features": [], "_links": {}}
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    return mock_client


def test_post_search_preserves_cql2_filter() -> None:
    """POST /search must preserve CQL2 filter fields (cloud_cover, datetime range)."""
    cql2_filter = {
        "op": "and",
        "args": [
            {
                "op": "<=",
                "args": [{"property": "cloud_cover"}, 10],
            },
            {
                "op": "between",
                "args": [
                    {"property": "datetime"},
                    "2024-01-01T00:00:00Z",
                    "2024-06-01T00:00:00Z",
                ],
            },
        ],
    }

    captured = {}

    def capture_stac_to_planet_request(stac_request: BaseSearchPostRequest) -> tuple[dict[str, Any], dict[str, Any]]:
        captured["filter"] = getattr(stac_request, "filter", None)
        return {}, {"filter": {}, "item_types": ["PSScene"]}

    with (
        patch(
            "stac_planet_api.api.stac_to_planet_request",
            side_effect=capture_stac_to_planet_request,
        ),
        patch(
            "stac_planet_api.api.get_authenticated_client",
            return_value=_make_mock_client(),
        ),
        patch(
            "stac_planet_api.api.planet_to_stac_response",
            return_value={"type": "FeatureCollection", "features": [], "links": []},
        ),
    ):
        client = TestClient(app)
        client.post(
            "/search",
            json={
                "collections": ["PSScene"],
                "filter": cql2_filter,
                "filter-lang": "cql2-json",
            },
            auth=("test-api-key", ""),
        )

    assert captured.get("filter") is not None, (
        "CQL2 filter was silently dropped — post_search must use POST_REQUEST_MODEL, "
        "not BaseSearchPostRequest, to preserve extension fields."
    )


def test_post_search_preserves_sortby() -> None:
    """POST /search must preserve the sortby extension field."""
    captured = {}

    def capture_stac_to_planet_request(stac_request: BaseSearchPostRequest) -> tuple[dict[str, Any], dict[str, Any]]:
        captured["sortby"] = getattr(stac_request, "sortby", None)
        return {}, {"filter": {}, "item_types": ["PSScene"]}

    with (
        patch(
            "stac_planet_api.api.stac_to_planet_request",
            side_effect=capture_stac_to_planet_request,
        ),
        patch(
            "stac_planet_api.api.get_authenticated_client",
            return_value=_make_mock_client(),
        ),
        patch(
            "stac_planet_api.api.planet_to_stac_response",
            return_value={"type": "FeatureCollection", "features": [], "links": []},
        ),
    ):
        client = TestClient(app)
        client.post(
            "/search",
            json={
                "collections": ["PSScene"],
                "sortby": [{"field": "datetime", "direction": "desc"}],
            },
            auth=("test-api-key", ""),
        )

    assert captured.get("sortby") is not None, (
        "sortby was silently dropped — post_search must use POST_REQUEST_MODEL, "
        "not BaseSearchPostRequest, to preserve extension fields."
    )
