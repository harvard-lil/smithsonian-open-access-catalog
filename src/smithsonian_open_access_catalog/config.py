"""Automation and environment settings."""

from collections.abc import Mapping
from dataclasses import dataclass, fields
import os
from pathlib import Path
import re
from typing import get_type_hints
from urllib.parse import urlparse

ENV_PREFIX = 'CATALOG_'
EDAN_RECORD_ID_PREFIX = 'edanmdm:'
CATALOG_FILENAMES = ('files.parquet', 'metadata.parquet', 'linkages.parquet')


@dataclass(frozen=True)
class Config:
    bucket: str = 'us-west-2.opendata.source.coop'
    region: str = 'us-west-2'
    endpoint_url: str = 'https://s3.us-west-2.amazonaws.com'
    base_prefix: str = 'harvard-lil/smithsonian-open-access'
    work_dir: Path = Path('work')
    list_segments: int = 64
    list_workers: int = 32
    duckdb_threads: int = 8
    duckdb_memory_limit: str = '22GB'
    duckdb_temp_dir_size: str = '120GB'
    duckdb_extension_dir: Path | None = None
    svx_batch_size: int = 200
    max_drop_percent: float = 5.0
    max_runtime_minutes: int = 360

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        """Inherit settings from CATALOG_* environment variables."""
        env = os.environ if env is None else env
        hints = get_type_hints(cls)
        values = {}
        for field in fields(cls):
            name = ENV_PREFIX + field.name.upper()
            raw = env.get(name)
            if raw is None:
                continue
            try:
                values[field.name] = _convert_to_type(hints[field.name], raw)
            except ValueError as error:
                raise ValueError(f'{name}: {error}') from error
        return cls(**values)

    @property
    def key_prefix(self) -> str:
        return self.base_prefix + '/'

    @property
    def source_coop_base_url(self) -> str:
        return f'https://source.coop/{self.base_prefix}/'

    @property
    def metadata_key_prefix(self) -> str:
        return self.key_prefix + 'metadata/edan/'

    @property
    def search_key_prefix(self) -> str:
        return self.key_prefix + 'search/'

    @property
    def endpoint_host(self) -> str:
        return urlparse(self.endpoint_url).netloc

    @property
    def metadata_file_regex(self) -> str:
        return '^' + re.escape(self.metadata_key_prefix) + r'[^/]+/[0-9a-f]{2}\.txt$'

    @property
    def model_key_regex(self) -> str:
        return '^' + re.escape(self.key_prefix) + '3d/([^/]{36})/'

    @property
    def svx_file_regex(self) -> str:
        return self.model_key_regex + r'scene\.svx\.json$'

    def s3_url(self, key: str) -> str:
        return f's3://{self.bucket}/{key}'

    @property
    def previous_dir(self) -> Path:
        return self.work_dir / 'previous'

    @property
    def segments_dir(self) -> Path:
        return self.work_dir / 'listing'

    @property
    def listing_path(self) -> Path:
        return self.work_dir / 'listing.parquet'

    @property
    def metadata_dir(self) -> Path:
        return self.work_dir / 'metadata'

    @property
    def search_dir(self) -> Path:
        return self.work_dir / 'search'

    @property
    def duckdb_temp_dir(self) -> Path:
        return self.work_dir / 'duckdb-tmp'


def _convert_to_type(hint: object, raw: str) -> object:
    if hint in (Path, Path | None):
        return Path(raw)
    if hint is int:
        return int(raw)
    if hint is float:
        return float(raw)
    return raw
