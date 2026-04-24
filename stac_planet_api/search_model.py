from typing import cast

from stac_fastapi.api.models import create_post_request_model
from stac_fastapi.extensions.core import (
    FieldsExtension,
    FilterExtension,
    QueryExtension,
    SortExtension,
    TokenPaginationExtension,
)
from stac_fastapi.types.search import BaseSearchPostRequest

extensions = [
    FieldsExtension(),
    QueryExtension(),
    SortExtension(),
    TokenPaginationExtension(),
    FilterExtension(),
]

POST_REQUEST_MODEL: type[BaseSearchPostRequest] = cast(
    type[BaseSearchPostRequest], create_post_request_model(extensions)
)
