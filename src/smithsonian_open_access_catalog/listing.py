"""Threaded listing of the archive prefix, split into contiguous key ranges."""

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import pairwise
import logging
from pathlib import Path
import shutil
import time

from botocore.client import BaseClient
import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from smithsonian_open_access_catalog import s3
from smithsonian_open_access_catalog.config import CATALOG_FILENAMES, Config
from smithsonian_open_access_catalog.connection import sql_literal

logger = logging.getLogger(__name__)

SCHEMA = pa.schema(
    [
        ('key', pa.string()),
        ('size', pa.uint64()),
        ('last_modified', pa.timestamp('us', tz='UTC')),
        ('etag', pa.string()),
        ('segment', pa.int32()),
    ]
)
SEGMENT_ATTEMPTS = 3
RETRY_WAIT_SECONDS = (5, 15)
MIN_FRACTION_OF_PREVIOUS = 0.5


class ListingError(Exception):
    pass


@dataclass(frozen=True)
class Segment:
    index: int
    start_after: str | None
    end_inclusive: str | None


def segments_from_boundaries(boundaries: Sequence[str]) -> list[Segment]:
    boundaries = list(boundaries)
    if boundaries != sorted(set(boundaries)):
        raise ListingError('Boundaries must be strictly increasing')
    edges = [None, *boundaries, None]
    return [Segment(i, start, end) for i, (start, end) in enumerate(pairwise(edges))]


def compute_boundaries(
    conn: duckdb.DuckDBPyConnection, config: Config, previous_files: Path
) -> list[str]:
    if config.list_segments < 2:
        return []
    (foreign,) = conn.execute(
        'SELECT count(*) FILTER (WHERE NOT starts_with(url, $base_url)) FROM read_parquet($path)',
        {'base_url': config.source_coop_base_url, 'path': str(previous_files)},
    ).fetchone()
    if foreign:
        raise ListingError(f'{foreign} urls in the previous catalog are outside the prefix')
    fractions = ', '.join(repr(i / config.list_segments) for i in range(1, config.list_segments))
    (quantiles,) = conn.execute(
        f"""
        SELECT quantile_disc(key, [{fractions}])
        FROM (
            SELECT $key_prefix || substr(url, length($base_url) + 1) AS key
            FROM read_parquet($path)
        )
        """,
        {
            'key_prefix': config.key_prefix,
            'base_url': config.source_coop_base_url,
            'path': str(previous_files),
        },
    ).fetchone()
    return sorted(set(quantiles or []))


def list_segment(client: BaseClient, bucket: str, prefix: str, segment: Segment) -> pa.Table:
    kwargs = {'Bucket': bucket, 'Prefix': prefix}
    if segment.start_after is not None:
        kwargs['StartAfter'] = segment.start_after

    keys, sizes, modified, etags = [], [], [], []
    for page in client.get_paginator('list_objects_v2').paginate(**kwargs):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if segment.end_inclusive is not None and key > segment.end_inclusive:
                return _table(segment, keys, sizes, modified, etags)
            keys.append(key)
            sizes.append(obj['Size'])
            modified.append(obj['LastModified'])
            etags.append(obj['ETag'].strip('"'))
    return _table(segment, keys, sizes, modified, etags)


def _table(segment: Segment, keys, sizes, modified, etags) -> pa.Table:
    return pa.table(
        {
            'key': pa.array(keys, pa.string()),
            'size': pa.array(sizes, pa.uint64()),
            'last_modified': pa.array(modified, pa.timestamp('us', tz='UTC')),
            'etag': pa.array(etags, pa.string()),
            'segment': pa.array([segment.index] * len(keys), pa.int32()),
        },
        schema=SCHEMA,
    )


def _list_with_retry(client: BaseClient, config: Config, segment: Segment) -> pa.Table:
    for attempt in range(1, SEGMENT_ATTEMPTS + 1):
        try:
            return list_segment(client, config.bucket, config.key_prefix, segment)
        except Exception as error:
            if attempt == SEGMENT_ATTEMPTS:
                raise ListingError(f'Segment {segment.index} failed: {error}') from error
            wait = RETRY_WAIT_SECONDS[min(attempt, len(RETRY_WAIT_SECONDS)) - 1]
            logger.warning(
                'Segment %d attempt %d failed (%s); retrying in %ds',
                segment.index,
                attempt,
                error,
                wait,
            )
            time.sleep(wait)
    raise AssertionError('Unreachable')


def list_bucket(client: BaseClient, config: Config, segments: Sequence[Segment]) -> int:
    config.segments_dir.mkdir(parents=True, exist_ok=True)

    def work(segment: Segment) -> int:
        table = _list_with_retry(client, config, segment)
        pq.write_table(
            table, config.segments_dir / f'segment-{segment.index:03d}.parquet', compression='zstd'
        )
        logger.info('Segment %d: %d keys', segment.index, table.num_rows)
        return table.num_rows

    with ThreadPoolExecutor(max_workers=config.list_workers) as pool:
        return sum(pool.map(work, segments))


def merge_listing(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    segments: Sequence[Segment],
    previous_files: Path | None = None,
) -> int:
    conn.execute(
        'CREATE OR REPLACE TABLE segments (index INTEGER, start_after VARCHAR, end_inclusive VARCHAR)'
    )
    conn.executemany(
        'INSERT INTO segments VALUES (?, ?, ?)',
        [(s.index, s.start_after, s.end_inclusive) for s in segments],
    )
    glob = sql_literal(str(config.segments_dir / '*.parquet'))
    conn.execute(f'CREATE OR REPLACE VIEW segment_keys AS SELECT * FROM read_parquet({glob})')

    total = assert_coverage(conn, config)
    if previous_files is not None:
        (previous,) = conn.execute(
            'SELECT count(*) FROM read_parquet($path)', {'path': str(previous_files)}
        ).fetchone()
        if total < MIN_FRACTION_OF_PREVIOUS * previous:
            raise ListingError(f'Listed {total} keys but the previous catalog had {previous}')

    conn.execute(
        f"""
            COPY (SELECT key, size, last_modified, etag FROM segment_keys ORDER BY key)
            TO {sql_literal(str(config.listing_path))} (FORMAT parquet, COMPRESSION zstd)
        """
    )
    conn.execute('DROP VIEW segment_keys')
    conn.execute('DROP TABLE segments')
    return total


def assert_coverage(conn: duckdb.DuckDBPyConnection, config: Config) -> int:
    total, distinct, prefixed = conn.execute(
        """
            SELECT
                count(*),
                count(DISTINCT key),
                coalesce(bool_and(starts_with(key, $prefix)), true)
            FROM segment_keys
        """,
        {'prefix': config.key_prefix},
    ).fetchone()
    if total == 0:
        raise ListingError('Listing is empty')
    if distinct != total:
        raise ListingError(f'{total - distinct} duplicate keys across segments')
    if not prefixed:
        raise ListingError(f'Keys outside {config.key_prefix!r} were listed')

    out_of_range = conn.execute(
        """
            SELECT s.index, r.lowest, r.highest
            FROM segments s
            JOIN (
                SELECT segment, min(key) AS lowest, max(key) AS highest
                FROM segment_keys GROUP BY segment
            ) r
            ON r.segment = s.index
            WHERE (s.start_after IS NOT NULL AND r.lowest <= s.start_after)
            OR (s.end_inclusive IS NOT NULL AND r.highest > s.end_inclusive)
        """
    ).fetchall()
    if out_of_range:
        raise ListingError(f'Segments hold keys outside their range: {out_of_range[:3]}')

    empty = conn.execute(
        'SELECT index FROM segments WHERE index NOT IN (SELECT DISTINCT segment FROM segment_keys)'
    ).fetchall()
    if empty:
        logger.warning('%d segments listed no keys', len(empty))

    metadata_files, svx_files = conn.execute(
        """
            SELECT
                count(*) FILTER (WHERE regexp_matches(key, $metadata)),
                count(*) FILTER (WHERE regexp_matches(key, $svx))
            FROM segment_keys
        """,
        {'metadata': config.metadata_file_regex, 'svx': config.svx_file_regex},
    ).fetchone()
    if metadata_files == 0 or svx_files == 0:
        raise ListingError(f'Listing has {metadata_files} metadata files and {svx_files} SVX files')
    logger.info(
        'Listed %d keys (%d metadata files, %d SVX files)', total, metadata_files, svx_files
    )
    return total


def run(config: Config, conn: duckdb.DuckDBPyConnection) -> None:
    client = s3.anonymous_client(config, max_pool=config.list_workers + 4)

    for name in CATALOG_FILENAMES:
        key = config.search_key_prefix + name
        if s3.download_if_exists(client, config.bucket, key, config.previous_dir / name):
            logger.info('Downloaded previous %s', name)
        else:
            logger.warning('No previous %s published', name)

    previous_files = config.previous_dir / 'files.parquet'
    boundaries = compute_boundaries(conn, config, previous_files) if previous_files.exists() else []
    if not boundaries:
        logger.warning('Listing sequentially in a single segment')
    segments = segments_from_boundaries(boundaries)

    shutil.rmtree(config.segments_dir, ignore_errors=True)
    list_bucket(client, config, segments)
    merge_listing(conn, config, segments, previous_files if previous_files.exists() else None)
    shutil.rmtree(config.segments_dir)
