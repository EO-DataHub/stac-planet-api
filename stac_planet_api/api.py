import json
import logging
import os
import re
from datetime import datetime
from typing import Annotated, Optional
from urllib.parse import unquote_plus

import httpx
import orjson
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.security import HTTPBasic
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
from stac_pydantic import ItemCollection

from stac_planet_api.config import Settings
from stac_planet_api.request_adaptor import stac_to_planet_request
from stac_planet_api.response_adaptor import planet_to_stac_response

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

root_path = os.environ.get('ROOT_PATH', '/')

app = FastAPI(root_path=root_path)

security = HTTPBasic()


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


@app.get("/search")
async def get_search(
    request: Request,
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
            {"field": sort[1:], "direction": "desc" if sort[0] == "-" else "asc"}
            for sort in sortby
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
    )


@app.post("/search")
async def post_search(
    search_request: BaseSearchPostRequest,
    request: Request,
) -> ItemCollection:
    """Search planet items.

    Args:
        search request (str): The identifier of the collection that contains the item.
        item (stac_types.Item): The new item data.

    Returns:
        ItemCollection: The item, or `None` if the item was successfully deleted.
    """

    api_key = os.environ.get("PLANET_API_KEY")
    auth = httpx.BasicAuth(username=api_key, password="")

    client = httpx.AsyncClient(
        auth=auth,
        verify=False,
        timeout=180,
    )
    base_url = str(request.base_url)

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
