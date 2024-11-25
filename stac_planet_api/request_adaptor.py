from stac_planet_api.config import Settings

settings = Settings()

COMPARISONS = {
    ">": "gt",
    "<": "lt",
    ">=": "gte",
    "<=": "lte",
}


def get_datetime(datetime_str: str) -> str:
    if datetime_str == "..":
        return None

    return datetime_str


def datetime_filter(date_filter: str):
    start_date, end_date = date_filter.split("/")
    start_date = get_datetime(start_date)
    end_date = get_datetime(end_date)

    return {
        "type": "DateRangeFilter",
        "field_name": "acquired",
        "config": {
            "gte": start_date,
            "lte": end_date,
        },
    }


def comparison_filter(comp_filter):
    return {
        "type": "RangeFilter",
        "field_name": comp_filter["args"][0]["property"],
        "config": {
            COMPARISONS[comp_filter["op"]]: comp_filter["args"][1],
        },
    }


def convert_filter(stac_filter: dict):
    if stac_filter["op"] in ["and", "or"]:
        config = []
        for sub_filter in stac_filter["args"]:
            config.append(convert_filter(sub_filter))

        return {"type": f"{stac_filter['op'].title()}Filter", "config": config}

    elif stac_filter["op"] in ["<", ">", "<=", ">="]:
        return comparison_filter(stac_filter)


def build_search_filter(stac_request):
    config = []
    if datetime_str := getattr(stac_request, "datetime", None):
        config.append(datetime_filter(datetime_str))

    # Multiple field filters, e.g.: "range", "string", "numberin
    if stac_filter := getattr(stac_request, "filter", None):
        config.append(convert_filter(stac_filter))

    if intersects := getattr(stac_request, "intersects", None):
        config.append(
            {
                "type": "GeometryFilter",
                "field_name": "geometry",
                "config": intersects.dict(),
            }
        )

    # Multiple logical filters, e.g.: "and", "or", "not"
    return {"type": "AndFilter", "config": config}


def stac_to_planet_request(stac_request: dict) -> tuple[dict, dict]:
    planet_parameters = {}
    planet_request = {"filter": build_search_filter(stac_request)}

    planet_request["item_types"] = (
        collections
        if (collections := getattr(stac_request, "collections"))
        else settings.item_types
    )

    if not planet_request["item_types"]:
        planet_request["item_types"] = settings.item_types

    if limit := getattr(stac_request, "limit", None):
        planet_parameters["_page_size"] = limit

    if limit := getattr(stac_request, "sortby", None):
        field = limit.get("field")
        direction = limit.get("direction", "asc")
        planet_parameters["_sort"] = (field, direction)

    return planet_parameters, planet_request
