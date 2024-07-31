from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """
    Container for configuration of the planet-stac-converter.
    """

    # Don't use default key in prod
    fernet_key: str = "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="
    item_types: list[str] = [
        "PSScene",
        "REOrthoTile",
        "REScene",
        "SkySatScene",
        "SkySatCollect",
        "SkySatVideo",
        "Landsat8L1G",
        "Sentinel2L1C",
    ]
