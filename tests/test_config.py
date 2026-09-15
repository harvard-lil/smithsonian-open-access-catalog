from pathlib import Path
import re

import pytest

from smithsonian_open_access_catalog.config import Config


def test_defaults_and_derived_values():
    config = Config.from_env({})
    assert config.bucket == 'us-west-2.opendata.source.coop'
    assert config.key_prefix == 'harvard-lil/smithsonian-open-access/'
    assert config.source_coop_base_url == 'https://source.coop/harvard-lil/smithsonian-open-access/'
    assert config.metadata_key_prefix == 'harvard-lil/smithsonian-open-access/metadata/edan/'
    assert config.search_key_prefix == 'harvard-lil/smithsonian-open-access/search/'
    assert config.endpoint_host == 's3.us-west-2.amazonaws.com'
    assert config.s3_url('a/b.txt') == 's3://us-west-2.opendata.source.coop/a/b.txt'
    assert config.duckdb_extension_dir is None


def test_env_overrides_are_typed():
    config = Config.from_env(
        {
            'CATALOG_WORK_DIR': '/data/work',
            'CATALOG_LIST_SEGMENTS': '8',
            'CATALOG_MAX_DROP_PERCENT': '2.5',
            'CATALOG_DUCKDB_EXTENSION_DIR': '/opt/ext',
            'CATALOG_BASE_PREFIX': 'org/repo',
            'UNRELATED': 'ignored',
        }
    )
    assert config.work_dir == Path('/data/work')
    assert config.list_segments == 8
    assert config.max_drop_percent == 2.5
    assert config.duckdb_extension_dir == Path('/opt/ext')
    assert config.key_prefix == 'org/repo/'
    assert config.listing_path == Path('/data/work/listing.parquet')


def test_unparseable_value_names_the_variable():
    with pytest.raises(ValueError, match='CATALOG_LIST_SEGMENTS'):
        Config.from_env({'CATALOG_LIST_SEGMENTS': 'many'})


def test_file_patterns():
    config = Config()
    prefix = config.key_prefix
    uuid = '0b6a4d1e-0d9a-4b6e-9c4e-1f2a3b4c5d6e'
    assert re.match(config.metadata_file_regex, prefix + 'metadata/edan/acm/0f.txt')
    assert not re.match(config.metadata_file_regex, prefix + 'metadata/edan/acm/index.txt')
    assert re.match(config.svx_file_regex, prefix + f'3d/{uuid}/scene.svx.json')
    assert not re.match(config.svx_file_regex, prefix + f'3d/{uuid}/articles/scene.svx.json')
    assert re.match(config.model_key_regex, prefix + f'3d/{uuid}/model.glb').group(1) == uuid
