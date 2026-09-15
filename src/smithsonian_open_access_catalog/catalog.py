"""Build the files, metadata, and linkages catalogs."""

import logging

import duckdb

from smithsonian_open_access_catalog import validate
from smithsonian_open_access_catalog.config import EDAN_RECORD_ID_PREFIX, Config
from smithsonian_open_access_catalog.connection import sql_literal

logger = logging.getLogger(__name__)


class CatalogError(Exception):
    pass


FILES_SQL = """
    CREATE OR REPLACE TABLE files AS
    SELECT
        $base_url || substr(key, length($key_prefix) + 1) AS url,
        size,
        last_modified AT TIME ZONE 'UTC' AS last_modified
    FROM listing
    WHERE regexp_matches(substr(key, length($key_prefix) + 1), '^(media|3d)/')
    AND parse_filename(key) != ''
"""

MEDIA_LINKS_SQL = """
    CREATE OR REPLACE TABLE media_links AS
    WITH prep AS (
        SELECT
            coalesce(
                json_extract_string(content, '$.record_id'),
                json_extract_string(content, '$.descriptiveNonRepeating.record_ID')
            ) AS record_id,
            json_extract(content, '$.descriptiveNonRepeating') AS dnr
        FROM metadata
    ), exploded AS (
        SELECT
            prep.record_id,
            regexp_extract(resource.url, '[?&]id=([^&]+)', 1) AS filename
        FROM prep
        INNER JOIN LATERAL unnest(
            from_json(
                json_extract(dnr, '$.online_media.media'),
                '[{"resources":[{"url":"VARCHAR"}]}]'
            )
        ) AS m(media_item) ON true
        INNER JOIN LATERAL unnest(media_item.resources) AS r(resource) ON true
        WHERE prep.record_id IS NOT NULL
    ), media_files AS (
        SELECT
            parse_filename(key) AS filename,
            $base_url || substr(key, length($key_prefix) + 1) AS url
        FROM listing
        WHERE starts_with(key, $media_prefix)
        AND parse_filename(key) != ''
    )
    SELECT DISTINCT media_files.url, exploded.record_id
    FROM exploded
    JOIN media_files ON media_files.filename = exploded.filename
    WHERE regexp_matches(lower(exploded.filename), '(jpe?g|tiff?|pdf)$')
"""

SVX_RAW_SQL = 'CREATE OR REPLACE TABLE svx_raw (filename VARCHAR, metas JSON)'

SVX_INSERT_SQL = """
    INSERT INTO svx_raw
    SELECT filename, metas
    FROM read_json(
        $urls,
        columns = {metas: 'JSON'},
        filename = true,
        maximum_object_size = 67108864
    )
"""

SVX_MODELS_SQL = """
    CREATE OR REPLACE TABLE svx_models AS
    WITH arrays AS (
        SELECT filename, from_json(metas, '["JSON"]') AS arr
        FROM svx_raw
        WHERE metas IS NOT NULL
    ), metas AS (
        SELECT filename, unnest(arr) AS meta, unnest(range(len(arr))) AS position
        FROM arrays
    ), last_collection AS (
        SELECT filename, json_extract(meta, '$.collection') AS collection
        FROM metas
        WHERE json_extract(meta, '$.collection') IS NOT NULL
        QUALIFY row_number() OVER (PARTITION BY filename ORDER BY position DESC) = 1
    )
    SELECT
        regexp_extract(filename, $model_pattern, 1) AS model_id,
        substr(
            json_extract_string(collection, '$.edanRecordId'),
            length($edan_prefix) + 1
        ) AS record_id,
        coalesce(
            json_extract_string(collection, '$.title'),
            json_extract_string(collection, '$.titles.EN')
        ) AS title
    FROM last_collection
    WHERE record_id IS NOT NULL AND record_id != ''
"""

MODEL_LINKS_SQL = """
    CREATE OR REPLACE TABLE model_links AS
    WITH files_3d AS (
        SELECT
            regexp_extract(key, $model_pattern, 1) AS model_id,
            $base_url || substr(key, length($key_prefix) + 1) AS url
        FROM listing
        WHERE starts_with(key, $models_prefix)
        AND parse_filename(key) != ''
    )
    SELECT DISTINCT files_3d.url, svx_models.record_id
    FROM files_3d
    JOIN svx_models ON files_3d.model_id = svx_models.model_id
"""

LINKAGES_SQL = """
    CREATE OR REPLACE TABLE linkages AS
    SELECT url, record_id FROM media_links
    UNION ALL
    SELECT url, record_id FROM model_links
"""

CATALOG_METADATA_SQL = """
    CREATE OR REPLACE TABLE catalog_metadata AS
    WITH linked_ids AS (
        SELECT DISTINCT record_id FROM linkages
    ), extracted AS (
        SELECT
            coalesce(
                json_extract_string(content, '$.record_id'),
                json_extract_string(content, '$.descriptiveNonRepeating.record_ID')
            ) AS record_id,
            coalesce(
                unitCode,
                json_extract_string(content, '$.descriptiveNonRepeating.unit_code')
            ) AS unit_code,
            coalesce(
                json_extract_string(content, '$.title'),
                json_extract_string(content, '$.descriptiveNonRepeating.title.content')
            ) AS title,
            coalesce(
                json_extract_string(content, '$.guid'),
                json_extract_string(content, '$.descriptiveNonRepeating.guid')
            ) AS guid,
            $metadata_url_base || substr(filename, length($metadata_source_base) + 1) AS metadata_url
        FROM metadata
    ), dump AS (
        SELECT e.record_id, e.unit_code, e.title, e.guid, e.metadata_url
        FROM extracted e
        SEMI JOIN linked_ids l ON e.record_id = l.record_id
        QUALIFY row_number() OVER (PARTITION BY e.record_id) = 1
    ), synthetic AS (
        SELECT
            s.record_id,
            upper(split_part(s.record_id, '_', 1)) AS unit_code,
            any_value(s.title) AS title,
            NULL AS guid,
            NULL AS metadata_url
        FROM svx_models s
        ANTI JOIN dump d ON s.record_id = d.record_id
        GROUP BY s.record_id
    )
    SELECT unit_code, record_id, title, guid, metadata_url FROM dump
    UNION ALL BY NAME
    SELECT unit_code, record_id, title, guid, metadata_url FROM synthetic
"""

OUTPUTS = (
    ('files', 'files.parquet', 'url, size, last_modified', 'url'),
    (
        'catalog_metadata',
        'metadata.parquet',
        'unit_code, record_id, title, guid, metadata_url',
        'unit_code, record_id, title',
    ),
    ('linkages', 'linkages.parquet', 'url, record_id', 'url, record_id'),
)


def register_inputs(conn: duckdb.DuckDBPyConnection, config: Config) -> None:
    conn.execute(
        'CREATE OR REPLACE TABLE listing AS FROM read_parquet($listing)',
        {'listing': str(config.listing_path)},
    )
    conn.execute(
        f"""
            CREATE OR REPLACE VIEW metadata AS
            SELECT unitCode, content, filename
            FROM read_parquet({sql_literal(str(config.metadata_dir / '*.parquet'))})
        """
    )


def build_files(conn: duckdb.DuckDBPyConnection, config: Config) -> None:
    conn.execute(
        FILES_SQL, {'base_url': config.source_coop_base_url, 'key_prefix': config.key_prefix}
    )


def build_media_links(conn: duckdb.DuckDBPyConnection, config: Config) -> None:
    conn.execute(
        MEDIA_LINKS_SQL,
        {
            'base_url': config.source_coop_base_url,
            'key_prefix': config.key_prefix,
            'media_prefix': f'{config.key_prefix}media/',
        },
    )


def svx_keys(conn: duckdb.DuckDBPyConnection, config: Config) -> list[str]:
    rows = conn.execute(
        'SELECT key FROM listing WHERE regexp_matches(key, $pattern) ORDER BY key',
        {'pattern': config.svx_file_regex},
    ).fetchall()
    return [key for (key,) in rows]


def build_svx_models(conn: duckdb.DuckDBPyConnection, config: Config, urls: list[str]) -> list[str]:
    conn.execute(SVX_RAW_SQL)
    failures = []
    for start in range(0, len(urls), config.svx_batch_size):
        batch = urls[start : start + config.svx_batch_size]
        try:
            conn.execute(SVX_INSERT_SQL, {'urls': batch})
        except duckdb.Error:
            for url in batch:
                try:
                    conn.execute(SVX_INSERT_SQL, {'urls': [url]})
                except duckdb.Error as error:
                    logger.warning('Skipping %s: %s', url, error)
                    failures.append(url)

    allowed = max(5, len(urls) // 100)
    if len(failures) > allowed:
        raise CatalogError(f'{len(failures)} of {len(urls)} SVX files failed to parse')

    conn.execute(
        SVX_MODELS_SQL,
        {
            'model_pattern': r'/3d/([^/]{36})/scene\.svx\.json$',
            'edan_prefix': EDAN_RECORD_ID_PREFIX,
        },
    )
    (models,) = conn.execute('SELECT count(*) FROM svx_models').fetchone()
    logger.info(
        '%d SVX files yielded %d models with a record_id', len(urls) - len(failures), models
    )
    return failures


def build_model_links(conn: duckdb.DuckDBPyConnection, config: Config) -> None:
    conn.execute(
        MODEL_LINKS_SQL,
        {
            'model_pattern': config.model_key_regex,
            'base_url': config.source_coop_base_url,
            'key_prefix': config.key_prefix,
            'models_prefix': f'{config.key_prefix}3d/',
        },
    )


def build_linkages(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(LINKAGES_SQL)


def build_catalog_metadata(conn: duckdb.DuckDBPyConnection, config: Config) -> None:
    conn.execute(
        CATALOG_METADATA_SQL,
        {
            'metadata_url_base': f'{config.source_coop_base_url}metadata/edan/',
            'metadata_source_base': config.s3_url(config.metadata_key_prefix),
        },
    )


def write_outputs(conn: duckdb.DuckDBPyConnection, config: Config) -> dict[str, int]:
    config.search_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for table, filename, columns, order in OUTPUTS:
        path = config.search_dir / filename
        conn.execute(
            f"""
                COPY (SELECT {columns} FROM {table} ORDER BY {order})
                TO {sql_literal(str(path))} (FORMAT parquet, COMPRESSION zstd, COMPRESSION_LEVEL 22)
            """
        )
        (counts[filename],) = conn.execute(f'SELECT count(*) FROM {table}').fetchone()
        logger.info('%s: %d rows', path, counts[filename])
    return counts


def run(config: Config, conn: duckdb.DuckDBPyConnection) -> None:
    register_inputs(conn, config)
    build_files(conn, config)
    build_media_links(conn, config)
    build_svx_models(conn, config, [config.s3_url(key) for key in svx_keys(conn, config)])
    build_model_links(conn, config)
    build_linkages(conn)
    build_catalog_metadata(conn, config)
    write_outputs(conn, config)
    validate.run(conn, config)
