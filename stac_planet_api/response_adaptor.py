import concurrent.futures
import json
from json import JSONDecodeError
from urllib.parse import urljoin

import httpx
from cryptography.fernet import Fernet

from stac_planet_api.config import Settings

settings = Settings()

FERNET = Fernet(settings.fernet_key)

with open("stac_planet_api/queyables.json", mode="r", encoding="utf-8") as file:
    QUERYABLES: dict = json.load(file)

with open("stac_planet_api/asset_types.json", mode="r", encoding="utf-8") as file:
    ASSET_TYPES: dict = json.load(file)


def get_item_links(base_url: str, collection_id: str, item_id: str) -> list:
    """
    Get item links
    """
    return [
        {
            "rel": "self",
            "type": "application/geo+json",
            "href": urljoin(base_url, f"collections/{collection_id}/items/{item_id}"),
        },
        {
            "rel": "parent",
            "type": "application/json",
            "href": urljoin(base_url, f"collections/{collection_id}"),
        },
        {
            "rel": "collection",
            "type": "application/json",
            "href": urljoin(base_url, f"collections/{collection_id}"),
        },
        {"rel": "root", "type": "application/json", "href": base_url},
    ]


def get_search_links(base_url: str, next_token: str, prev_token: str) -> list:
    """
    Get search links
    """
    links = [
        {
            "rel": "self",
            "type": "application/geo+json",
            "href": urljoin(base_url, "/search"),
        },
        {"rel": "root", "type": "application/json", "href": base_url},
    ]

    if next_token:
        links.append(
            [
                {
                    "rel": "next",
                    "type": "application/geo+json",
                    "method": "GET",
                    "href": urljoin(
                        base_url,
                        f"search?token={FERNET.encrypt(next_token.encode('utf-8')).decode('utf-8')}",
                    ),
                },
                {
                    "rel": "next",
                    "type": "application/geo+json",
                    "method": "POST",
                    "href": urljoin(base_url, "search"),
                    "body": {
                        "token": FERNET.encrypt(next_token.encode("utf-8")).decode(
                            "utf-8"
                        )
                    },
                },
            ]
        )

    if prev_token:
        links.append(
            [
                {
                    "rel": "prev",
                    "type": "application/json",
                    "method": "GET",
                    "href": urljoin(
                        base_url,
                        f"search?token={FERNET.encrypt(prev_token.encode('utf-8')).decode('utf-8')}",
                    ),
                },
                {
                    "rel": "prev",
                    "type": "application/json",
                    "method": "POST",
                    "href": urljoin(base_url, "search"),
                    "body": {
                        "token": FERNET.encrypt(prev_token.encode("utf-8")).decode(
                            "utf-8"
                        )
                    },
                },
            ]
        )

    return links


def get_assets(
    collection_id: str, thumbnail_href: str, assets_href: str, auth, path: str = None
) -> dict:
    """
    Get item assets
    """
    output = {
        "external_thumbnail": {
            "href": thumbnail_href,
            "roles": ["external_thumbnail"],
            "type": "image/png",
        }
    }

    client = httpx.Client(
        auth=auth,
        verify=False,
        timeout=180,
    )

    count = 0
    while count < 10:
        count += 1
        try:
            assets = client.get(assets_href).json()
            break
        except JSONDecodeError:
            pass

    for key, value in assets.items():
        output[key] = {
            "href": value["_links"]["_self"],
            "roles": ["data"],
            "type": ASSET_TYPES.get(collection_id, {}).get(value["type"], "UNKNOWN"),
        }

    if path:
        output["thumbnail"] = {
            "href": f"{path}/thumbnail",
            "roles": ["thumbnail"],
            "type": "image/png",
        }

    return output


def point(coordinates: list) -> list:
    """
    Get point bbox
    """
    return [
        coordinates[0],
        coordinates[1],
        coordinates[0],
        coordinates[1],
    ]


def line(coordinates: list) -> list:
    """
    Get line bbox
    """
    bbox = point(coordinates[0])

    for coordinate in coordinates[1:]:

        if coordinate[0] < bbox[0]:
            bbox[0] = coordinate[0]

        elif coordinate[0] > bbox[2]:
            bbox[2] = coordinate[0]

        if coordinate[1] < bbox[1]:
            bbox[1] = coordinate[1]

        elif coordinate[1] > bbox[3]:
            bbox[3] = coordinate[1]

    return bbox


def polygon(coordinates: list) -> list:
    """
    Get polygon bbox
    """
    return line(coordinates[0][1:])


def multi(coordinate_type: str, coordinates: list) -> list:
    """
    Get polygon bbox
    """

    bboxes = [
        get_bbox(coordinate_type.lstrip("Multi"), coordinate)
        for coordinate in coordinates
    ]
    return [
        min(bbox[0] for bbox in bboxes),
        max(bbox[2] for bbox in bboxes),
        min(bbox[1] for bbox in bboxes),
        max(bbox[3] for bbox in bboxes),
    ]


def get_bbox(coordinate_type: str, coordinates: list) -> list:
    """
    Get bbox from geometry
    """
    if coordinate_type == "Point":
        return point(coordinates)

    if coordinate_type == "Line":
        return line(coordinates)

    if coordinate_type == "Polygon":
        return polygon(coordinates)

    if coordinate_type.startswith("Multi"):
        return multi(coordinate_type, coordinates)

    return None


def map_item(planet_item, base_url, auth, path=None):

    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "stac_extensions": [],
        "id": planet_item["id"],
        "collection": planet_item["properties"]["item_type"],
        "geometry": planet_item["geometry"],
        "bbox": get_bbox(
            coordinate_type=planet_item["geometry"]["type"],
            coordinates=planet_item["geometry"]["coordinates"],
        ),
        "properties": planet_item["properties"]
        | {"datetime": planet_item["properties"]["acquired"]},
        "links": get_item_links(
            base_url=base_url,
            collection_id=planet_item["properties"]["item_type"],
            item_id=planet_item["id"],
        ),
        "assets": get_assets(
            collection_id=planet_item["properties"]["item_type"],
            thumbnail_href=planet_item["_links"]["thumbnail"],
            assets_href=planet_item["_links"]["assets"],
            auth=auth,
            path=path,
        ),
    }


def get_quertables(collection_id: str = ""):

    queryables = {
        "$schema": "https://json-schema.org/draft/2019-09/schema",
        "$id": "https://stac-api.example.com/queryables",
        "type": "object",
        "title": "Queryables for Example STAC API",
        "description": "Queryable names for the example STAC API Item Search filter.",
        "properties": {
            "id": {
                "description": "ID",
                "$ref": "https://schemas.stacspec.org/v1.0.0/item-spec/json-schema/item.json#/id",
            },
            "collection": {
                "description": "Collection",
                "$ref": "https://schemas.stacspec.org/v1.0.0/item-spec/json-schema/item.json#/collection",
            },
            "geometry": {
                "description": "Geometry",
                "$ref": "https://schemas.stacspec.org/v1.0.0/item-spec/json-schema/item.json#/geometry",
            },
            "datetime": {
                "description": "Datetime",
                "$ref": "https://schemas.stacspec.org/v1.0.0/item-spec/json-schema/datetime.json#/properties/datetime",
            },
            "acquired": {
                "description": "Timestamp when the item was captured.",
                "type": "string",
                "format": "date-time",
                "pattern": "(\\+00:00|Z)$",
            },
        },
        "additionalProperties": True,
    }

    if collection_id:
        queryables["properties"] |= QUERYABLES[collection_id.lower()]

    else:
        for collection_queryables in QUERYABLES.values():
            queryables["properties"] |= collection_queryables

    return queryables


def planet_to_stac_response(planet_response: dict, base_url: str, auth, method: str):
    stac_items = []

    # keeping this for non-concurrency testing purposes
    # for planet_item in planet_response["features"]:
    #     stac_items.append(map_item(planet_item, base_url, auth))

    with concurrent.futures.ThreadPoolExecutor() as e:
        fut = []
        for planet_item in planet_response["features"]:
            collection_id = planet_item["properties"]["item_type"]
            item_id = planet_item["id"]
            item_path = f"{base_url}collections/{collection_id}/items/{item_id}"
            fut.append(e.submit(map_item, planet_item, base_url, auth, item_path))

        for r in concurrent.futures.as_completed(fut):
            try:
                data = r.result()
                stac_items.append(data)
            except json.decoder.JSONDecodeError as e:
                pass

    return {
        "type": "FeatureCollection",
        "features": stac_items,
        "links": get_search_links(
            base_url=base_url,
            next_token=planet_response["_links"].get("_next"),
            prev_token=planet_response["_links"].get("_prev"),
        ),
    }
