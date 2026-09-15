"""Validation gates that a catalog must pass before upload."""

from dataclasses import dataclass
import logging
from pathlib import Path

import duckdb

from smithsonian_open_access_catalog.config import Config

logger = logging.getLogger(__name__)

EXPECTED_SCHEMAS = {
    'files.parquet': [('url', 'VARCHAR'), ('size', 'UBIGINT'), ('last_modified', 'TIMESTAMP')],
    'metadata.parquet': [
        (column, 'VARCHAR')
        for column in ('unit_code', 'record_id', 'title', 'guid', 'metadata_url')
    ],
    'linkages.parquet': [('url', 'VARCHAR'), ('record_id', 'VARCHAR')],
}
SORT_KEYS = {
    'files.parquet': 'url',
    'metadata.parquet': 'unit_code, record_id, title',
    'linkages.parquet': 'url, record_id',
}


class CatalogValidationError(Exception):
    pass


@dataclass(frozen=True)
class Gate:
    name: str
    ok: bool
    detail: str


def run(conn: duckdb.DuckDBPyConnection, config: Config) -> list[Gate]:
    gates = [
        *schema_gates(conn, config),
        *row_count_gates(conn, config),
        *integrity_gates(conn, config),
        *url_gates(conn, config),
        *sort_gates(conn, config),
    ]
    for gate in gates:
        logger.info('%s %s: %s', 'ok  ' if gate.ok else 'FAIL', gate.name, gate.detail)
    failed = [gate for gate in gates if not gate.ok]
    if failed:
        raise CatalogValidationError('; '.join(f'{g.name}: {g.detail}' for g in failed))
    return gates


def schema_gates(conn: duckdb.DuckDBPyConnection, config: Config) -> list[Gate]:
    gates = []
    for filename, expected in EXPECTED_SCHEMAS.items():
        rows = conn.execute(
            'DESCRIBE SELECT * FROM read_parquet($path)', {'path': _path(config, filename)}
        ).fetchall()
        actual = [(name, kind) for name, kind, *_ in rows]
        gates.append(Gate(f'schema {filename}', actual == expected, str(actual)))
    return gates


def row_count_gates(conn: duckdb.DuckDBPyConnection, config: Config) -> list[Gate]:
    gates = []
    for filename in EXPECTED_SCHEMAS:
        (rows,) = conn.execute(
            'SELECT count(*) FROM read_parquet($path)', {'path': _path(config, filename)}
        ).fetchone()
        previous_path = config.previous_dir / filename
        if not previous_path.exists():
            gates.append(
                Gate(f'rows {filename}', rows > 0, f'{rows} rows; no previous catalog to compare')
            )
            continue
        (previous,) = conn.execute(
            'SELECT count(*) FROM read_parquet($path)', {'path': str(previous_path)}
        ).fetchone()
        floor = (1 - config.max_drop_percent / 100) * previous
        gates.append(
            Gate(
                f'rows {filename}',
                rows >= floor,
                f'{rows} rows, previously {previous} (floor {floor:.0f})',
            )
        )
    return gates


def integrity_gates(conn: duckdb.DuckDBPyConnection, config: Config) -> list[Gate]:
    paths = {name: _path(config, name) for name in EXPECTED_SCHEMAS}

    duplicate_urls, null_urls = conn.execute(
        """
            SELECT
                count(*) - count(DISTINCT url),
                count(*) FILTER (WHERE url IS NULL)
            FROM read_parquet($files)
        """,
        {'files': paths['files.parquet']},
    ).fetchone()
    duplicate_ids, null_ids, empty_ids = conn.execute(
        """
            SELECT
                count(*) - count(DISTINCT record_id),
                count(*) FILTER (WHERE record_id IS NULL),
                count(*) FILTER (WHERE record_id = '')
            FROM read_parquet($metadata)
        """,
        {'metadata': paths['metadata.parquet']},
    ).fetchone()
    linkage_rows, orphan_records, orphan_urls = conn.execute(
        """
            SELECT
                (SELECT count(*) FROM read_parquet($linkages)),
                (SELECT count(*) FROM read_parquet($linkages) l
                ANTI JOIN read_parquet($metadata) m ON l.record_id = m.record_id),
                (SELECT count(*) FROM read_parquet($linkages) l
                ANTI JOIN read_parquet($files) f ON l.url = f.url)
        """,
        paths_as_params(paths),
    ).fetchone()

    return [
        Gate(
            'files urls unique',
            duplicate_urls == 0 and null_urls == 0,
            f'{duplicate_urls} duplicates, {null_urls} null',
        ),
        Gate(
            'metadata record_ids unique',
            duplicate_ids == 0 and null_ids == 0 and empty_ids == 0,
            f'{duplicate_ids} duplicates, {null_ids} null, {empty_ids} empty',
        ),
        Gate('linkages present', linkage_rows > 0, f'{linkage_rows} rows'),
        Gate(
            'linkages reference metadata',
            orphan_records == 0,
            f'{orphan_records} orphan record_ids',
        ),
        Gate('linkages reference files', orphan_urls == 0, f'{orphan_urls} orphan urls'),
    ]


def url_gates(conn: duckdb.DuckDBPyConnection, config: Config) -> list[Gate]:
    base = config.source_coop_base_url
    gates = []
    for filename in ('files.parquet', 'linkages.parquet'):
        (bad,) = conn.execute(
            'SELECT count(*) FILTER (WHERE NOT starts_with(url, $base)) FROM read_parquet($path)',
            {'base': base, 'path': _path(config, filename)},
        ).fetchone()
        gates.append(Gate(f'urls {filename}', bad == 0, f'{bad} urls outside {base}'))
    (bad,) = conn.execute(
        """
            SELECT count(*) FILTER (
                WHERE metadata_url IS NOT NULL AND NOT starts_with(metadata_url, $base)
            )
            FROM read_parquet($path)
        """,
        {'base': base + 'metadata/edan/', 'path': _path(config, 'metadata.parquet')},
    ).fetchone()
    gates.append(
        Gate('metadata urls', bad == 0, f'{bad} metadata urls outside {base}metadata/edan/')
    )
    return gates


def sort_gates(conn: duckdb.DuckDBPyConnection, config: Config) -> list[Gate]:
    gates = []
    for filename, order in SORT_KEYS.items():
        (misplaced,) = conn.execute(
            f"""
                SELECT count(*) FROM (
                    SELECT file_row_number AS actual, row_number() OVER (ORDER BY {order}) - 1 AS expected
                    FROM read_parquet($path, file_row_number = true)
                ) WHERE actual != expected
            """,
            {'path': _path(config, filename)},
        ).fetchone()
        gates.append(
            Gate(f'sorted {filename}', misplaced == 0, f'{misplaced} rows out of order by {order}')
        )
    return gates


def _path(config: Config, filename: str) -> str:
    return str(config.search_dir / filename)


def paths_as_params(paths: dict[str, Path | str]) -> dict[str, str]:
    return {name.removesuffix('.parquet'): str(path) for name, path in paths.items()}
