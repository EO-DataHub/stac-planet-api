import logging

import fastapi

from stac_planet_api.config import Settings

settings = Settings()

COMPARISONS = {
    ">": "gt",
    "<": "lt",
    ">=": "gte",
    "<=": "lte",
}


def bbox_to_intersects(bbox):
    latitudes = bbox[0::2]
    longitudes = bbox[1::2]

    intersects = []
    for lat in latitudes:
        for long in longitudes:
            intersects.append([lat, long])

    intersects.append(intersects[0])

    return [intersects]


def get_datetime(datetime_str: str) -> str:
    if datetime_str == "..":
        return None

    return datetime_str


def datetime_filter(date_filter: str):
    start_date, end_date = date_filter.split("/")
    start_date = get_datetime(start_date)
    end_date = get_datetime(end_date)

    dt_filter = {
        "type": "DateRangeFilter",
        "field_name": "acquired",
        "config": {},
    }

    if start_date:
        dt_filter["config"]["gte"] = start_date
    if end_date:
        dt_filter["config"]["lte"] = end_date

    return dt_filter


def comparison_filter(comp_filter):
    field_name = comp_filter["args"][0]["property"].lstrip("properties.")

    if field_name == "datetime":
        dt_filter = {
            "type": "DateRangeFilter",
            "field_name": "acquired",
            "config": {},
        }

        if comp_filter["op"].lower() == "between":
            dt_filter = datetime_filter(f"{comp_filter['args'][1]}/{comp_filter['args'][2]}")

        if comp_filter["op"] == ">":
            dt_filter["config"]["gt"] = comp_filter["args"][1]

        if comp_filter["op"] == ">=":
            dt_filter["config"]["gte"] = comp_filter["args"][1]

        if comp_filter["op"] == "<":
            dt_filter["config"]["lt"] = comp_filter["args"][1]

        if comp_filter["op"] == "<=":
            dt_filter["config"]["lte"] = comp_filter["args"][1]

        return dt_filter

    if comp_filter["op"].lower() == "between":
        return {
            "type": "RangeFilter",
            "field_name": field_name,
            "config": {
                COMPARISONS[">="]: comp_filter["args"][1],
                COMPARISONS["<="]: comp_filter["args"][2],
            },
        }

    return {
        "type": "RangeFilter",
        "field_name": field_name,
        "config": {
            COMPARISONS[comp_filter["op"]]: comp_filter["args"][1],
        },
    }


def equals_filter(eq_filter):
    field_name = eq_filter["args"][0]["property"].lstrip("properties.")
    value = eq_filter["args"][1]

    if eq_filter["op"] == "=":
        value = [value]

    if field_name == "collection":
        return {"type": "Collection", "collections": value}

    if isinstance(value[0], str):
        return {"type": "StringInFilter", "field_name": field_name, "config": value}

    return {"type": "NumberInFilter", "field_name": field_name, "config": value}


def geometry_filter(geometry_filter):
    return {
        "type": "GeometryFilter",
        "field_name": geometry_filter["args"][0]["property"].lstrip("properties."),
        "config": {
            "type": "Polygon",
            "coordinates": geometry_filter["args"][1]["coordinates"],
        },
    }


def convert_filter(stac_filter: dict):
    collections = []
    if stac_filter["op"] in ["and", "or"]:
        config = []
        for sub_filter in stac_filter["args"]:
            result = convert_filter(sub_filter)

            if result["type"] == "Collection":
                collections.extend(result["collections"])
            else:
                config.append(convert_filter(sub_filter))

        config = [
            c for c in config if c is not None
        ]  # if there are any unrecognised filters then ignore them instead of erroring

        return {"type": f"{stac_filter['op'].title()}Filter", "collections": collections, "config": config}

    elif stac_filter["op"].lower() in ["between", "<", ">", "<=", ">="]:
        return comparison_filter(stac_filter)

    elif stac_filter["op"].lower() in ["in", "="]:
        return equals_filter(stac_filter)

    elif stac_filter["op"] in ["s_intersects"]:
        return geometry_filter(stac_filter)

    else:
        logging.info(f"Filter {stac_filter['op']} not recognised")


def build_search_filter(stac_request):
    config = []
    collections = []
    if datetime_str := getattr(stac_request, "datetime", None):
        config.append(datetime_filter(datetime_str))
    # Multiple field filters, e.g.: "range", "string", "numberin

    if stac_filter := getattr(stac_request, "filter", None):
        planet_filter = convert_filter(stac_filter)
        collections.extend(planet_filter.pop("collections", []))
        config.append(planet_filter)

    if intersects := getattr(stac_request, "intersects", None):
        config.append(
            {
                "type": "GeometryFilter",
                "field_name": "geometry",
                "config": intersects.dict(),
            }
        )

    if bbox := getattr(stac_request, "bbox", None):
        config.append(
            {
                "type": "GeometryFilter",
                "field_name": "geometry",
                "config": {"type": "Polygon", "coordinates": bbox_to_intersects(bbox)},
            }
        )

    # Multiple logical filters, e.g.: "and", "or", "not"
    return {"type": "AndFilter", "collections": collections, "config": config}


def stac_to_planet_request(stac_request: dict) -> tuple[dict, dict]:
    planet_parameters = {}
    search_filter = build_search_filter(stac_request)
    planet_request = {"filter": search_filter}

    collections = stac_request.collections if getattr(stac_request, "collections") else []
    collections.extend(search_filter.pop("collections", []))

    planet_request["item_types"] = collections if collections else settings.item_types

    if limit := getattr(stac_request, "limit", None):
        planet_parameters["_page_size"] = limit

    if sortby := getattr(stac_request, "sortby", None):
        sort_param = sortby[0]

        if sort_param.field in [
            "published",
            "acquired",
            "datetime",
        ]:
            field = "acquired" if sort_param.field else sort_param.field

            planet_parameters["_sort"] = f"{field} {sort_param.direction.value}"

        else:
            raise fastapi.HTTPException(
                status_code=400,
                detail="Planet only supports `sortby` for `datetime`, `published` or `acquired`.",
            )

    return planet_parameters, planet_request
