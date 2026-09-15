"""Upload the validated catalogs to the repository."""

import logging

import duckdb

from smithsonian_open_access_catalog import s3, validate
from smithsonian_open_access_catalog.config import Config

logger = logging.getLogger(__name__)

# Readers joining through linkages see consistent data if files catalog lands last
UPLOAD_ORDER = ('metadata.parquet', 'linkages.parquet', 'files.parquet')


def run(config: Config, conn: duckdb.DuckDBPyConnection) -> None:
    validate.run(conn, config)
    client = s3.upload_client(config)
    for filename in UPLOAD_ORDER:
        key = config.search_key_prefix + filename
        s3.upload_file(client, config.bucket, key, config.search_dir / filename)
        logger.info('Uploaded s3://%s/%s', config.bucket, key)
