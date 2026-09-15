import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from smithsonian_open_access_catalog import catalog, validate
from smithsonian_open_access_catalog.catalog import CatalogError
from tests.conftest import BASE_URL, NAIVE_MODIFIED, PREFIX, write_listing

UUID = '0b6a4d1e-0d9a-4b6e-9c4e-1f2a3b4c5d6e'
GUID = 'http://n2t.net/ark:/65665/1'


def _record(record_id: str, title: str, image_ids: list[str]) -> str:
    resources = [{'url': f'https://ids.si.edu/ids/download?id={i}'} for i in image_ids]
    return json.dumps(
        {
            'record_id': record_id,
            'title': title,
            'guid': GUID,
            'descriptiveNonRepeating': {
                'unit_code': 'NPG',
                'online_media': {'media': [{'resources': resources}]},
            },
        }
    )


def _write_metadata(config) -> None:
    config.metadata_dir.mkdir(parents=True, exist_ok=True)
    filename = config.s3_url(config.metadata_key_prefix) + 'npg/00.txt'
    table = pa.table(
        {
            'unitCode': ['NPG', 'NPG'],
            'content': [
                _record('npg_1', 'Portrait', ['NPG-1_1.jpg', 'NPG-1_1.tif']),
                _record('npg_2', 'Unlinked', ['NPG-missing.jpg']),
            ],
            'filename': [filename, filename],
        }
    )
    pq.write_table(table, config.metadata_dir / 'data_0.parquet')


def _write_svx(tmp_path, metas: list[dict]) -> str:
    path = tmp_path / '3d' / UUID / 'scene.svx.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'metas': metas, 'models': []}))
    return str(path)


@pytest.fixture
def built(config, conn, tmp_path):
    write_listing(
        config,
        [
            PREFIX + 'media/npg/NPG-1_1.jpg',
            PREFIX + 'media/npg/NPG-1_1.tif',
            PREFIX + 'media/npg/',
            PREFIX + f'3d/{UUID}/scene.svx.json',
            PREFIX + f'3d/{UUID}/model.glb',
            PREFIX + 'metadata/edan/npg/00.txt',
            PREFIX + 'README.md',
        ],
    )
    _write_metadata(config)
    svx = _write_svx(
        tmp_path,
        [
            {'collection': {'edanRecordId': 'edanmdm:other_1', 'title': 'Superseded'}},
            {'collection': {'edanRecordId': 'edanmdm:nmnh_9', 'titles': {'EN': 'Skull'}}},
        ],
    )

    catalog.register_inputs(conn, config)
    catalog.build_files(conn, config)
    catalog.build_media_links(conn, config)
    assert catalog.build_svx_models(conn, config, [svx]) == []
    catalog.build_model_links(conn, config)
    catalog.build_linkages(conn)
    catalog.build_catalog_metadata(conn, config)
    return catalog.write_outputs(conn, config)


def _rows(conn, path):
    return conn.execute('SELECT * FROM read_parquet(?)', [str(path)]).fetchall()


def test_files_catalog(built, config, conn):
    assert built['files.parquet'] == 4
    rows = _rows(conn, config.search_dir / 'files.parquet')
    assert [url for url, _, _ in rows] == [
        BASE_URL + f'3d/{UUID}/model.glb',
        BASE_URL + f'3d/{UUID}/scene.svx.json',
        BASE_URL + 'media/npg/NPG-1_1.jpg',
        BASE_URL + 'media/npg/NPG-1_1.tif',
    ]
    assert rows[0][1] == len(PREFIX + f'3d/{UUID}/model.glb')
    assert rows[0][2] == NAIVE_MODIFIED


def test_linkages(built, config, conn):
    assert _rows(conn, config.search_dir / 'linkages.parquet') == [
        (BASE_URL + f'3d/{UUID}/model.glb', 'nmnh_9'),
        (BASE_URL + f'3d/{UUID}/scene.svx.json', 'nmnh_9'),
        (BASE_URL + 'media/npg/NPG-1_1.jpg', 'npg_1'),
        (BASE_URL + 'media/npg/NPG-1_1.tif', 'npg_1'),
    ]


def test_metadata_catalog_includes_synthetic_model_record(built, config, conn):
    assert _rows(conn, config.search_dir / 'metadata.parquet') == [
        ('NMNH', 'nmnh_9', 'Skull', None, None),
        ('NPG', 'npg_1', 'Portrait', GUID, BASE_URL + 'metadata/edan/npg/00.txt'),
    ]


def test_built_catalog_passes_validation(built, config, conn):
    assert all(gate.ok for gate in validate.run(conn, config))


def test_svx_parse_failures_are_isolated(config, conn, tmp_path):
    good = _write_svx(
        tmp_path, [{'collection': {'edanRecordId': 'edanmdm:nmnh_9', 'title': 'Skull'}}]
    )
    bad = tmp_path / 'bad.json'
    bad.write_text('{not json')

    assert catalog.build_svx_models(conn, config, [good, str(bad)]) == [str(bad)]
    assert conn.execute('SELECT record_id, title FROM svx_models').fetchall() == [
        ('nmnh_9', 'Skull')
    ]


def test_too_many_svx_failures_abort(config, conn, tmp_path):
    bad_files = []
    for i in range(6):
        path = tmp_path / f'bad{i}.json'
        path.write_text('{not json')
        bad_files.append(str(path))
    with pytest.raises(CatalogError, match='failed to parse'):
        catalog.build_svx_models(conn, config, bad_files)
