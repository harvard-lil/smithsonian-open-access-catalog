# Smithsonian Open Access Archive Catalog

This repository includes code for rebuilding the [Smithsonian Open Access Archive](https://source.coop/harvard-lil/smithsonian-open-access)'s catalog files. It should be scheduled at some regular cadence to keep the catalogs in sync with the archive inventory as the latter is updated.

The Smithsonian Open Access Archive and this repository are maintained by the [Public Data Project](https://lil.law.harvard.edu/our-work/public-data-project), part of the [Library Innovation Lab](https://lil.law.harvard.edu) at Harvard University.

## Configuration

Default settings are specified in `src/config.py`. These can be overridden using environment variables of the pattern `CATALOG_*`: `CATALOG_BUCKET`, `CATALOG_LIST_WORKERS`, and so on.

## Development

This project uses [uv](https://docs.astral.sh/uv/), [Ruff](https://docs.astral.sh/ruff/), and [pytest](https://docs.pytest.org/en/stable/).

To lint:

```sh
uv run ruff check .
uv run ruff format --check .
```

To run tests:

```sh
uv run pytest
```

To build the Docker image:

```sh
docker build .
```
