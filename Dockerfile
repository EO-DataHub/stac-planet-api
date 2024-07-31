FROM python:3.12-slim

RUN mkdir -p /opt/stac-planet-api
WORKDIR /opt/stac-planet-api

COPY . /opt/stac-planet-api

RUN pip install poetry

RUN poetry install --only main

ENTRYPOINT ["poetry", "run", "uvicorn", "stac_planet_api.api:app", "--host", "0.0.0.0"]
