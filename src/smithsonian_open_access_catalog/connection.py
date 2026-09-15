"""Scaffolding for DuckDB connection setup."""

import duckdb

from smithsonian_open_access_catalog.config import Config

EXTENSIONS = ('httpfs',)


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def connect(config: Config) -> duckdb.DuckDBPyConnection:
    config.duckdb_temp_dir.mkdir(parents=True, exist_ok=True)
    settings = {
        'threads': config.duckdb_threads,
        'memory_limit': config.duckdb_memory_limit,
        'max_temp_directory_size': config.duckdb_temp_dir_size,
        'temp_directory': str(config.duckdb_temp_dir),
        'preserve_insertion_order': False,
        'autoinstall_known_extensions': False,
        'autoload_known_extensions': False,
    }
    if config.duckdb_extension_dir is not None:
        settings['extension_directory'] = str(config.duckdb_extension_dir)

    conn = duckdb.connect(config=settings)
    for extension in EXTENSIONS:
        # The image bakes extensions in; only a local run may fetch them
        if config.duckdb_extension_dir is None:
            conn.install_extension(extension)
        conn.load_extension(extension)

    for setting, value in (
        ('http_timeout', 600),
        ('http_retries', 6),
        ('http_retry_wait_ms', 1000),
        ('http_retry_backoff', 2),
        ('http_keep_alive', True),
    ):
        conn.execute(f'SET {setting} = {value!r}')

    conn.execute(
        f"""
            CREATE SECRET source_coop (
                TYPE s3,
                REGION {sql_literal(config.region)},
                ENDPOINT {sql_literal(config.endpoint_host)},
                URL_STYLE 'path'
            )
        """
    )
    return conn
