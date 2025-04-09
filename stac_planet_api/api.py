import itertools
import json
import logging
import os
import re
from typing import Annotated, Optional
from urllib.parse import unquote_plus

import fastapi
import fastapi.security
import httpx
import orjson
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
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

# Load all the planet api keys from the environment and setup a cycle so we can always use the next one.
try:
    PLANET_API_KEYS = itertools.cycle(
        os.environ.get(
            "PLANET_API_KEYS",
        ).split(":")
    )
except NameError:
    PLANET_API_KEYS = None

app = FastAPI(root_path=root_path)


class HeaderMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = (
            f"max-age={os.environ.get('CACHE_LENGTH', '3600')}"
        )
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

    if credentials is not None:
        api_key = credentials.username
        auth = httpx.BasicAuth(
            username=credentials.username, password=credentials.password
        )

    elif PLANET_API_KEYS is not None:
        api_key = next(PLANET_API_KEYS)
        auth = httpx.BasicAuth(username=api_key, password="")

    else:
        raise fastapi.HTTPException(
            status_code=401, detail="Credentials were not provided."
        )

    return auth, api_key


def get_authenticated_client(auth) -> httpx.Client:
    """Create a httpx client with correct auth for the planet apis."""

    return httpx.AsyncClient(
        auth=auth,
        verify=False,
        timeout=180,
    )


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
        collections str: comma separated list of collections.
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
        search_request["datetime"] = datetime

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
    search_request: POST_REQUEST_MODEL,
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
    base_url = get_base_url(request)

    if token := search_request.token:
        token_parts = FERNET.decrypt(token).decode("utf-8").split("\\")

        credentials = fastapi.security.HTTPBasicCredentials(
            username=token_parts[1], password=""
        )

        auth, api_key = get_auth(credentials)
        client = get_authenticated_client(auth=auth)

        planet_response = await client.get(token_parts[0])

    else:

        auth, api_key = get_auth(credentials)
        client = get_authenticated_client(auth)

        search_request.limit = (
            MAX_ITEMS if search_request.limit > MAX_ITEMS else search_request.limit
        )

        if search_request.ids:

            all_collections = (
                search_request.collections
                if search_request.collections
                else await get_collections(client)
            )

            all_items = []

            for item_id in search_request.ids:
                for collection_id in all_collections:
                    try:
                        item = await get_item(
                            collection_id=collection_id,
                            item_id=item_id,
                            request=request,
                            credentials=credentials,
                        )
                        all_items.append(item)
                    except httpx.HTTPStatusError:  # unable to find item in catalogue
                        pass

            return ItemCollection(
                **{
                    "type": "FeatureCollection",
                    "features": all_items,
                    "links": [
                        {
                            "rel": "self",
                            "href": f"{base_url}search",
                            "type": "application/geo+json",
                        },
                        {"rel": "root", "href": base_url, "type": "application/json"},
                    ],
                }
            )

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
        planet_response=planet_response.json(),
        base_url=base_url,
        auth=auth,
        api_key=api_key,
    )


@app.get("/collections/{collection_id}/items")
@app.post("/collections/{collection_id}/items")
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
    query_params = dict(request._query_params)

    search_request = {
        "collections": [collection_id],
        "limit": int(query_params.get("limit", MAX_ITEMS)),
        "token": query_params.get("token", None),
    }

    return await post_search(
        search_request=POST_REQUEST_MODEL(**search_request),
        request=request,
        credentials=credentials,
    )


@app.get("/collections/{collection_id}/items/{item_id}")
@app.post("/collections/{collection_id}/items/{item_id}")
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
    auth, _ = get_auth(credentials)
    client = get_authenticated_client(auth)
    base_url = get_base_url(request)

    planet_response = await client.get(
        f"https://api.planet.com/data/v1/item-types/{collection_id}/items/{item_id}",
    )

    planet_response.raise_for_status()

    item_path = f"{base_url}collections/{collection_id}/items/{item_id}"

    return map_item(
        planet_item=planet_response.json(), base_url=base_url, auth=auth, path=item_path
    )


@app.get("/collections/{collection_id}/items/{item_id}/thumbnail")
@app.post("/collections/{collection_id}/items/{item_id}/thumbnail")
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
    auth, _ = get_auth(credentials)
    client = get_authenticated_client(auth)
    base_url = get_base_url(request)

    planet_response = await client.get(
        f"https://api.planet.com/data/v1/item-types/{collection_id}/items/{item_id}",
    )

    planet_response.raise_for_status()

    planet_data = map_item(
        planet_item=planet_response.json(),
        base_url=base_url,
        auth=auth,
        path=request.url,
    )

    if planet_data["assets"].get("external_thumbnail"):
        thumbnail_url = planet_data["assets"]["external_thumbnail"]["href"]

        thumbnail_response = await client.get(thumbnail_url)
        thumbnail_response.raise_for_status()

        return Response(content=thumbnail_response.content, media_type="image/png")

    raise HTTPException(
        status_code=404, detail="External thumbnail link not found in item"
    )


async def get_collections(client) -> list:
    """Get collections from Planet"""

    planet_response = await client.get("https://api.planet.com/data/v1/item-types")

    return [collection["id"] for collection in planet_response.json()["item_types"]]



@app.get("/collections/{collection}/thumbnail")
async def get_collection_thumbnail(collection: str):
    """Endpoint to get the thumbnail of an Airbus collection"""
    # Thumbnail is a local file, return it directly
    thumbnail_path = f"stac_planet_api/thumbnails/{collection}.jpg"
    if not os.path.exists(thumbnail_path):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(thumbnail_path)
