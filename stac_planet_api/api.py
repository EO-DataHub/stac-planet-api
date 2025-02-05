import json
import logging
import os
import re
from datetime import datetime
from typing import Annotated, Optional
from urllib.parse import unquote_plus

import fastapi
import fastapi.security
import httpx
import orjson
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pygeofilter.backends.cql2_json import to_cql2
from pygeofilter.parsers.cql2_text import parse as parse_cql2_text
from stac_fastapi.api.models import create_post_request_model
from stac_fastapi.extensions.core import (
    FieldsExtension,
    FilterExtension,
    QueryExtension,
    SortExtension,
    TokenPaginationExtension,
)
from stac_fastapi.types.rfc3339 import DateTimeType
from stac_fastapi.types.search import BaseSearchPostRequest
from stac_pydantic import Item, ItemCollection
from starlette.middleware.base import BaseHTTPMiddleware

from stac_planet_api.config import Settings
from stac_planet_api.request_adaptor import stac_to_planet_request
from stac_planet_api.response_adaptor import (
    get_quertables,
    map_item,
    planet_to_stac_response,
)

settings = Settings()

FERNET = Fernet(settings.fernet_key)

extensions = [
    FieldsExtension(),
    QueryExtension(),
    SortExtension(),
    TokenPaginationExtension(),
    FilterExtension(),
]

POST_REQUEST_MODEL = create_post_request_model(extensions)

logger = logging.getLogger(__name__)

root_path = os.environ.get("ROOT_PATH", "/")
default_base_url = os.environ.get("BASE_URL")

app = FastAPI(root_path=root_path)

class HeaderMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = f"max-age={os.environ.get('CACHE_LENGTH', '3600')}"
        return response

app.add_middleware(HeaderMiddleware)

security = HTTPBasic(auto_error=False)

MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "10"))


def get_base_url(request):
    global default_base_url

    if default_base_url:
        if not default_base_url.endswith("/"):
            default_base_url = default_base_url + "/"
        return default_base_url
    else:
        return str(request.base_url)


def get_auth(credentials) -> httpx.BasicAuth:
    """Create a httpx auth for the planet apis."""
    # Use the api key if available, otherwise pass through basic credentials from the user.
    api_key = os.environ.get("PLANET_API_KEY", None)
    if api_key is not None:
        auth = httpx.BasicAuth(username=api_key, password="")
    elif credentials is not None:
        auth = httpx.BasicAuth(
            username=credentials.username, password=credentials.password
        )
    else:
        raise fastapi.HTTPException(
            status_code=401, detail="Credentials were not provided."
        )

    return auth


def get_authenticated_client(credentials) -> httpx.Client:
    """Create a httpx client with correct auth for the planet apis."""

    auth = get_auth(credentials)

    return httpx.AsyncClient(
        auth=auth,
        verify=False,
        timeout=180,
    )


def format_datetime_range(date_tuple: DateTimeType) -> str:
    """
    Convert a tuple of datetime objects or None into a formatted string for API requests.

    Args:
        date_tuple (tuple): A tuple containing two elements, each can be a datetime object or None.

    Returns:
        str: A string formatted as 'YYYY-MM-DDTHH:MM:SS.sssZ/YYYY-MM-DDTHH:MM:SS.sssZ', with '..' used if any element is None.
    """

    def format_datetime(dt):
        """Format a single datetime object to the ISO8601 extended format with 'Z'."""
        return datetime.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if dt else ".."

    start, end = date_tuple
    return f"{format_datetime(start)}/{format_datetime(end)}"


@app.get("/queryables")
async def get_queryables(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
) -> dict:
    """GET queryables for catalog.

    Returns:
        dict: Queryables for the catalog.
    """
    return get_quertables()


@app.get("/collections/{collection_id}/queryables")
async def get_collection_queryables(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    collection_id: str,
) -> dict:
    """GET queryables for collection.

    Returns:
        dict: Queryables for the catalog.
    """
    return get_quertables(collection_id=collection_id)


@app.get("/search")
async def get_search(
    request: Request,
    credentials: Annotated[
        fastapi.security.HTTPBasicCredentials, fastapi.Depends(security)
    ],
    collections: Optional[str] = None,
    ids: Optional[str] = None,
    bbox: Optional[str] = None,
    datetime: Optional[str] = None,
    limit: Optional[int] = 10,
    query: Optional[str] = None,
    token: Optional[str] = None,
    fields: Optional[str] = None,
    sortby: Optional[str] = None,
    intersects: Optional[str] = None,
    filter: Optional[str] = None,
    filter_lang: Optional[str] = None,
) -> ItemCollection:
    """GET Search planet items.

    Args:
        collections str: comma seperated list of collections.
        ids str: comma separated list of collections.
        bbox: str: bounding box.
        datetime: str: datetime bounds.
        limit: int: number of items to return.
        query: str: search query.
        token: str: next/prev token.
        fields: str: returned fields.
        sortby: str: sort on fields.
        intersects: str: geometry intersects.
        filter: str: filter.
        filter_lang: str: filter language.

    Returns:
        ItemCollection: The items.
    """

    search_request = {
        "collections": collections.split(",") if collections else None,
        "ids": ids.split(",") if ids else None,
        "bbox": bbox.split(",") if bbox else None,
        "limit": limit,
        "token": token,
        "query": json.loads(query) if query else query,
    }

    # this is borrowed from stac-fastapi-pgstac
    # Kludgy fix because using factory does not allow alias for filter-lan
    query_params = str(request.query_params)
    if filter_lang is None:
        match = re.search(r"filter-lang=([a-z0-9-]+)", query_params, re.IGNORECASE)
        if match:
            filter_lang = match.group(1)

    if datetime:
        search_request["datetime"] = format_datetime_range(datetime)

    if intersects:
        search_request["intersects"] = orjson.loads(unquote_plus(intersects))

    if sortby:
        search_request["sortby"] = [
            {
                "field": sort[1:] if sort[0] in ["-", "+"] else sort,
                "direction": "desc" if sort[0] == "-" else "asc",
            }
            for sort in sortby.split(",")
        ]

    if filter:
        search_request["filter-lang"] = "cql2-json"
        search_request["filter"] = orjson.loads(
            unquote_plus(filter)
            if filter_lang == "cql2-json"
            else to_cql2(parse_cql2_text(filter))
        )

    if fields:
        includes, excludes = set(), set()
        for field in fields.split(","):
            if field[0] == "-":
                excludes.add(field[1:])
            else:
                includes.add(field[1:] if field[0] in "+ " else field)
        search_request["fields"] = {"include": includes, "exclude": excludes}

    return await post_search(
        search_request=POST_REQUEST_MODEL(**search_request),
        request=request,
        credentials=credentials,
    )


@app.post("/search")
async def post_search(
    search_request: BaseSearchPostRequest,
    request: Request,
    credentials: Annotated[
        fastapi.security.HTTPBasicCredentials, fastapi.Depends(security)
    ],
) -> ItemCollection:
    """Search planet items.

    Args:
        search request (BaseSearchPostRequest): The search request.

    Returns:
        ItemCollection: The items.
    """

    client = get_authenticated_client(credentials)
    base_url = get_base_url(request)

    auth = get_auth(credentials)

    search_request.limit = MAX_ITEMS if search_request.limit > MAX_ITEMS else search_request.limit

    if search_request.ids:

        all_collections = search_request.collections if search_request.collections else await get_collections(client)

        all_items = []

        for item_id in search_request.ids:
            for collection_id in all_collections:
                try:
                    item = await get_item(collection_id=collection_id, item_id=item_id, request=request, credentials=credentials)
                    all_items.append(item)
                except httpx.HTTPStatusError:  # unable to find item in catalogue
                    pass

        return ItemCollection(**{
            "type": "FeatureCollection",
            "features": all_items,
            "links": [
                  {
                    "rel": "self",
                    "href": f"{base_url}search",
                    "type": "application/geo+json"
                  },
                  {
                    "rel": "root",
                    "href": base_url,
                    "type": "application/json"
                  }],
            })

    else:
        if getattr(search_request, "token", False):
            token_url = FERNET.decrypt(search_request.token).decode("utf-8")
            planet_response = await client.get(token_url)

        else:

            planet_parameters, planet_request = stac_to_planet_request(
                stac_request=search_request
            )

            planet_response = await client.post(
                "https://api.planet.com/data/v1/quick-search",
                params=planet_parameters,
                json=planet_request,
            )

        planet_response.raise_for_status()

        return planet_to_stac_response(
            planet_response=planet_response.json(), base_url=base_url, auth=auth
        )


@app.get("/collections/{collection_id}/items")
async def get_item_collection(
    collection_id: str,
    request: Request,
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
) -> ItemCollection:
    """GET Get planet items for collection.

    Args:
        collection_id (str): The identifier of the collection that contains the item.

    Returns:
        ItemCollection: The items.
    """
    client = get_authenticated_client(credentials)
    base_url = get_base_url(request)

    auth = get_auth(credentials)

    query_params = dict(request._query_params)

    limit = int(query_params.get("limit", MAX_ITEMS))
    query_params["limit"] = MAX_ITEMS if limit > MAX_ITEMS else limit

    planet_parameters, planet_request = stac_to_planet_request(
        stac_request=BaseSearchPostRequest(collections=[collection_id], **query_params)
    )

    planet_response = await client.post(
        "https://api.planet.com/data/v1/quick-search",
        params=planet_parameters,
        json=planet_request,
    )

    planet_response.raise_for_status()

    return planet_to_stac_response(
        planet_response=planet_response.json(), base_url=base_url, auth=auth
    )


@app.post("/collections/{collection_id}/items")
async def post_item_collection(
    collection_id: str,
    request: Request,
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
) -> ItemCollection:
    """POST Get planet items for collection.

    Args:
        collection_id (str): The identifier of the collection that contains the item.

    Returns:
        ItemCollection: The items.
    """
    client = get_authenticated_client(credentials)
    base_url = get_base_url(request)

    auth = get_auth(credentials)

    planet_parameters, planet_request = stac_to_planet_request(
        stac_request=BaseSearchPostRequest(collections=[collection_id])
    )

    planet_response = await client.post(
        "https://api.planet.com/data/v1/quick-search",
        params=planet_parameters,
        json=planet_request,
    )

    planet_response.raise_for_status()

    return planet_to_stac_response(
        planet_response=planet_response.json(), base_url=base_url, auth=auth
    )


@app.post("/collections/{collection_id}/items/{item_id}")
async def post_item(
    collection_id: str,
    item_id: str,
    request: Request,
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
) -> Item:
    """Get planet item.

    Args:
        collection_id (str): The identifier of the collection that contains the item.
        item_id (str): The identifier of the item.

    Returns:
        Item: The item.
    """
    client = get_authenticated_client(credentials)
    base_url = get_base_url(request)

    auth = get_auth(credentials)

    planet_response = await client.get(
        f"https://api.planet.com/data/v1/item-types/{collection_id}/items/{item_id}",
    )

    planet_response.raise_for_status()

    item_path = f"{base_url}collections/{collection_id}/items/{item_id}"

    return map_item(planet_item=planet_response.json(), base_url=base_url, auth=auth, path=item_path)


@app.get("/collections/{collection_id}/items/{item_id}")
async def get_item(
    collection_id: str,
    item_id: str,
    request: Request,
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
) -> Item:
    """Get planet item.

    Args:
        collection_id (str): The identifier of the collection that contains the item.
        item_id (str): The identifier of the item.

    Returns:
        Item: The item.
    """
    client = get_authenticated_client(credentials)
    base_url = get_base_url(request)

    auth = get_auth(credentials)

    planet_response = await client.get(
        f"https://api.planet.com/data/v1/item-types/{collection_id}/items/{item_id}",
    )

    planet_response.raise_for_status()

    item_path = f"{base_url}collections/{collection_id}/items/{item_id}"

    return map_item(planet_item=planet_response.json(), base_url=base_url, auth=auth, path=item_path)


@app.get("/collections/{collection_id}/items/{item_id}/thumbnail")
async def get_item_thumbnail(
    collection_id: str,
    item_id: str,
    request: Request,
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
) -> Response:
    """Get planet item.

    Args:
        collection_id (str): The identifier of the collection that contains the item.
        item_id (str): The identifier of the item.

    Returns:
        Response: Thumbnail image
    """
    client = get_authenticated_client(credentials)
    base_url = get_base_url(request)

    auth = get_auth(credentials)

    planet_response = await client.get(
        f"https://api.planet.com/data/v1/item-types/{collection_id}/items/{item_id}",
    )

    planet_response.raise_for_status()

    planet_data = map_item(planet_item=planet_response.json(), base_url=base_url, auth=auth, path=request.url)

    if planet_data['assets'].get('external_thumbnail'):
        thumbnail_url = planet_data['assets']['external_thumbnail']['href']

        thumbnail_response = await client.get(thumbnail_url)
        thumbnail_response.raise_for_status()

        return Response(content=thumbnail_response.content, media_type="image/png")

    raise HTTPException(status_code=404, detail="External thumbnail link not found in item")


@app.post("/collections/{collection_id}/items/{item_id}/thumbnail")
async def post_item_thumbnail(
    collection_id: str,
    item_id: str,
    request: Request,
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
) -> Response:
    """Get planet item.

    Args:
        collection_id (str): The identifier of the collection that contains the item.
        item_id (str): The identifier of the item.

    Returns:
        Response: Thumbnail image
    """
    client = get_authenticated_client(credentials)
    base_url = get_base_url(request)

    auth = get_auth(credentials)

    planet_response = await client.get(
        f"https://api.planet.com/data/v1/item-types/{collection_id}/items/{item_id}",
    )

    planet_response.raise_for_status()

    planet_data = map_item(planet_item=planet_response.json(), base_url=base_url, auth=auth, path=request.url)

    if planet_data['assets'].get('external_thumbnail'):
        thumbnail_url = planet_data['assets']['external_thumbnail']['href']

        thumbnail_response = await client.get(thumbnail_url)
        thumbnail_response.raise_for_status()

        return Response(content=thumbnail_response.content, media_type="image/png")

    raise HTTPException(status_code=404, detail="External thumbnail link not found in item")


async def get_collections(client) -> list:
    """Get collections from Planet"""

    planet_response = await client.get("https://api.planet.com/data/v1/item-types")

    return [collection["id"] for collection in planet_response.json()["item_types"]]
