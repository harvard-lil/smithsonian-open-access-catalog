"""Copy the EDAN metadata NDJSON files into local parquet shards."""

import logging

import duckdb

from smithsonian_open_access_catalog.config import Config
from smithsonian_open_access_catalog.connection import sql_literal

logger = logging.getLogger(__name__)

NDJSON_COLUMNS = """{
    id: 'VARCHAR',
    version: 'VARCHAR',
    unitCode: 'VARCHAR',
    linkedId: 'VARCHAR',
    type: 'VARCHAR',
    content: 'VARCHAR',
    url: 'VARCHAR',
    hash: 'VARCHAR',
    docSignature: 'VARCHAR',
    timestamp: 'BIGINT',
    lastTimeUpdated: 'BIGINT',
    title: 'VARCHAR'
}"""


class MetadataError(Exception):
    pass


def metadata_keys(conn: duckdb.DuckDBPyConnection, config: Config) -> list[str]:
    rows = conn.execute(
        'SELECT key FROM read_parquet($listing) WHERE regexp_matches(key, $pattern) ORDER BY key',
        {'listing': str(config.listing_path), 'pattern': config.metadata_file_regex},
    ).fetchall()
    return [key for (key,) in rows]


def build_metadata(conn: duckdb.DuckDBPyConnection, config: Config, urls: list[str]) -> int:
    config.metadata_dir.mkdir(parents=True, exist_ok=True)
    for shard in config.metadata_dir.glob('*.parquet'):
        shard.unlink()

    conn.execute(
        f"""
            COPY (FROM read_ndjson($urls, columns = {NDJSON_COLUMNS}, filename = true))
            TO {sql_literal(str(config.metadata_dir))} (FORMAT parquet, PER_THREAD_OUTPUT)
        """,
        {'urls': urls},
    )

    rows, files = conn.execute(
        'SELECT count(*), count(DISTINCT filename) FROM read_parquet($glob)',
        {'glob': str(config.metadata_dir / '*.parquet')},
    ).fetchone()
    if rows == 0:
        raise MetadataError('No metadata records were read')
    if files < len(urls):
        logger.warning('%d of %d metadata files contained no records', len(urls) - files, len(urls))
    logger.info('Wrote %d metadata records from %d files', rows, files)
    return rows


def run(config: Config, conn: duckdb.DuckDBPyConnection) -> None:
    urls = [config.s3_url(key) for key in metadata_keys(conn, config)]
    if not urls:
        raise MetadataError('The listing has no metadata files')
    logger.info('Reading %d metadata files', len(urls))
    build_metadata(conn, config, urls)
