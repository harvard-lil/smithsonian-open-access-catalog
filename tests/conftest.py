from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from smithsonian_open_access_catalog.config import Config

PREFIX = Config().key_prefix
BASE_URL = Config().source_coop_base_url
MODIFIED = datetime(2026, 1, 1, tzinfo=UTC)
NAIVE_MODIFIED = MODIFIED.replace(tzinfo=None)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        work_dir=tmp_path / 'work',
        list_segments=4,
        list_workers=2,
        duckdb_threads=2,
        duckdb_memory_limit='512MB',
        duckdb_temp_dir_size='1GB',
        svx_batch_size=2,
    )


@pytest.fixture
def conn() -> Iterator[duckdb.DuckDBPyConnection]:
    with duckdb.connect() as connection:
        yield connection


def write_listing(config: Config, keys: list[str]) -> None:
    config.listing_path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            'key': pa.array(sorted(keys), pa.string()),
            'size': pa.array([len(k) for k in sorted(keys)], pa.uint64()),
            'last_modified': pa.array([MODIFIED] * len(keys), pa.timestamp('us', tz='UTC')),
            'etag': pa.array(['abc'] * len(keys), pa.string()),
        }
    )
    pq.write_table(table, config.listing_path)
