import pytest

from smithsonian_open_access_catalog import validate
from smithsonian_open_access_catalog.validate import CatalogValidationError
from tests.conftest import BASE_URL, NAIVE_MODIFIED

FILES = [(BASE_URL + f'media/x/{i}.jpg', 10, NAIVE_MODIFIED) for i in range(10)]
METADATA = [('X', f'x_{i}', 'Title', None, BASE_URL + 'metadata/edan/x/00.txt') for i in range(10)]
LINKAGES = [(BASE_URL + f'media/x/{i}.jpg', f'x_{i}') for i in range(10)]


def write_catalog(
    conn, config, files=FILES, metadata=METADATA, linkages=LINKAGES, sort=True, size_type='UBIGINT'
):
    config.search_dir.mkdir(parents=True, exist_ok=True)
    tables = {
        'files': (f'url VARCHAR, size {size_type}, last_modified TIMESTAMP', files, 'url'),
        'metadata': (
            'unit_code VARCHAR, record_id VARCHAR, title VARCHAR, guid VARCHAR, metadata_url VARCHAR',
            metadata,
            'unit_code, record_id, title',
        ),
        'linkages': ('url VARCHAR, record_id VARCHAR', linkages, 'url, record_id'),
    }
    for name, (columns, rows, order) in tables.items():
        conn.execute(f'CREATE OR REPLACE TABLE {name} ({columns})')
        placeholders = ', '.join('?' * len(columns.split(',')))
        conn.executemany(f'INSERT INTO {name} VALUES ({placeholders})', rows)
        ordering = f'ORDER BY {order}' if sort else ''
        conn.execute(
            f"COPY (SELECT * FROM {name} {ordering}) TO '{config.search_dir / name}.parquet' (FORMAT parquet)"
        )


def test_clean_catalog_passes(config, conn):
    write_catalog(conn, config)
    gates = validate.run(conn, config)
    assert all(gate.ok for gate in gates)


def test_orphan_linkage_fails(config, conn):
    write_catalog(conn, config, linkages=[*LINKAGES, (BASE_URL + 'media/x/0.jpg', 'nobody')])
    with pytest.raises(CatalogValidationError, match='linkages reference metadata'):
        validate.run(conn, config)


def test_linkage_to_unknown_file_fails(config, conn):
    write_catalog(conn, config, linkages=[*LINKAGES, (BASE_URL + 'media/x/missing.jpg', 'x_0')])
    with pytest.raises(CatalogValidationError, match='linkages reference files'):
        validate.run(conn, config)


def test_unsorted_output_fails(config, conn):
    write_catalog(conn, config, files=list(reversed(FILES)), sort=False)
    with pytest.raises(CatalogValidationError, match='sorted files.parquet'):
        validate.run(conn, config)


def test_wrong_column_type_fails(config, conn):
    write_catalog(conn, config, size_type='BIGINT')
    with pytest.raises(CatalogValidationError, match='schema files.parquet'):
        validate.run(conn, config)


def test_empty_record_id_fails(config, conn):
    write_catalog(conn, config, metadata=[*METADATA, ('X', '', 'Blank', None, None)])
    with pytest.raises(CatalogValidationError, match='record_ids unique'):
        validate.run(conn, config)


def test_foreign_url_fails(config, conn):
    write_catalog(
        conn, config, files=[*FILES, ('https://elsewhere.example/a.jpg', 1, NAIVE_MODIFIED)]
    )
    with pytest.raises(CatalogValidationError, match='urls files.parquet'):
        validate.run(conn, config)


def test_row_drop_beyond_threshold_fails(config, conn):
    write_catalog(conn, config)
    config.previous_dir.mkdir(parents=True)
    conn.execute(
        f"COPY (SELECT * FROM files, range(20)) TO '{config.previous_dir / 'files.parquet'}' (FORMAT parquet)"
    )
    with pytest.raises(CatalogValidationError, match='rows files.parquet'):
        validate.run(conn, config)


def test_row_drop_within_threshold_passes(config, conn):
    write_catalog(conn, config)
    config.previous_dir.mkdir(parents=True)
    conn.execute(
        f"COPY (SELECT * FROM files) TO '{config.previous_dir / 'files.parquet'}' (FORMAT parquet)"
    )
    assert all(gate.ok for gate in validate.run(conn, config))
