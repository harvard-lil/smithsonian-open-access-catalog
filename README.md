# Smithsonian Open Access Archive Catalog

This repository includes code for rebuilding the [Smithsonian Open Access Archive](https://source.coop/harvard-lil/smithsonian-open-access)'s catalog files. It should be scheduled at some regular cadence to keep the catalogs in sync with the archive inventory as the latter is updated.

The Smithsonian Open Access Archive and this repository are maintained by the [Public Data Project](https://lil.law.harvard.edu/our-work/public-data-project), part of the [Library Innovation Lab](https://lil.law.harvard.edu) at Harvard University.

## Process

The [Smithsonian Open Access Archive](https://source.coop/harvard-lil/smithsonian-open-access) is a very large collection of images, 3D models, and metadata. Since it mirrors the Smithsonian's own (largely flat) S3 bucket directory structure, the collection is not easy to browse or navigate, and it would be next to impossible to associate an image or model with its associated metadata. To support users in querying the repository, we've developed a group of [Parquet search catalogs](https://source.coop/harvard-lil/smithsonian-open-access/README.md#search).

This package is used to rebuild those catalog files on a recurring, automated basis. It is run as a command line script executable (`smithsonian-open-access-catalog`). Along the way, intermediate copies of the data are stored in a local directory; if a run is interrupted, it can be resumed with `--from-step` or `--only-step`. We've also included a watchdog thread to kill the process if it exceeds its configured runtime, keeping a stuck run from occupying a scheduled slot indefinitely.

This process includes the following steps:

### 1. List files

The large number of files in the repository, more than 9 million, makes them very time-consuming to list. To speed the process up, we take the previously published files catalog (`files.parquet`) and obtain evenly spaced quantiles of its URLs, dividing the inventory into 64 segments of roughly equal size. We then use a thread pool to issue list requests to S3 in parallel and assemble the results for the refreshed files catalog.

### 2. Read metadata

The collection includes more than 17 million metadata records in newline-delimited JSON format, constituting over 47 GB uncompressed. Loading this metadata is therefore time- and memory-intensive. It's worth noting that not all of these records are associated with images or models in the Open Access collection, but we cannot know that until we first read in all the metadata and attempt a join. The results of this step form the basis for our metadata catalog.

### 3. Build catalogs

Having generated listings of files and metadata records, we must now identify the linkages between the two. This is complicated for a variety of reasons, including the following:

- Files and metadata records have a many-to-many relationship
- Many metadata records aren't associated with a file, and many files aren't associated with a metadata record
- An image file can only be matched to a metadata record using its filename
- 3D model metadata can't be derived the same way as for image files
- Some 3D models don't match records from the newline-delimited JSON files at all, so we must synthesize minimal metadata from their SVX files

To account for all this, we produce the output catalogs (files, metadata, and linkages) through a series of DuckDB SQL queries and joins. See [`catalog.py`](src/smithsonian_open_access_catalog/catalog.py) for details.

After the final Parquet catalogs are produced, we subject them to a series of data validation checks. These address column names and types, row counts against the previous catalogs, unique identifier constraints, referential integrity across the three files, URL prefixes, and sort order.

### 4. Upload catalogs

If all validation checks succeed, we upload the catalog files to the Source repository. This process writes the files in a deliberate order, starting with the metadata catalog and ending with the files catalog, so that a user who happens to join the catalogs mid-upload sees a consistent set rather than linkages pointing at files that don't yet exist.

## Configuration

Default settings are specified in [`config.py`](src/smithsonian_open_access_catalog/config.py). These can be overridden using environment variables of the pattern `CATALOG_*`: `CATALOG_BUCKET`, `CATALOG_LIST_WORKERS`, and so on.

## Development

This project uses [uv](https://docs.astral.sh/uv/), [Ruff](https://docs.astral.sh/ruff/), and [pytest](https://docs.pytest.org/en/stable/).

To lint:

```sh
uv run ruff check .
uv run ruff format --check .
```

To run the tests:

```sh
uv run pytest
```

To build the Docker image:

```sh
docker build .
```
