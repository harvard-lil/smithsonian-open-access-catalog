from datetime import UTC, datetime
from itertools import pairwise

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from smithsonian_open_access_catalog import listing
from smithsonian_open_access_catalog.listing import ListingError, Segment, segments_from_boundaries
from tests.conftest import BASE_URL, MODIFIED, PREFIX

UUID = '0b6a4d1e-0d9a-4b6e-9c4e-1f2a3b4c5d6e'


class FakePaginator:
    def __init__(self, keys: list[str], page_size: int):
        self.keys = sorted(keys)
        self.page_size = page_size
        self.calls: list[dict] = []
        self.pages = 0

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        start_after = kwargs.get('StartAfter')
        selected = [
            k
            for k in self.keys
            if k.startswith(kwargs['Prefix']) and (start_after is None or k > start_after)
        ]
        for start in range(0, len(selected), self.page_size):
            self.pages += 1
            yield {'Contents': [_obj(k) for k in selected[start : start + self.page_size]]}


def _obj(key: str) -> dict:
    return {
        'Key': key,
        'Size': len(key),
        'LastModified': datetime(2026, 1, 1, tzinfo=UTC),
        'ETag': '"abc"',
    }


class FakeClient:
    def __init__(self, keys: list[str], page_size: int = 2):
        self.paginator = FakePaginator(keys, page_size)

    def get_paginator(self, name: str) -> FakePaginator:
        assert name == 'list_objects_v2'
        return self.paginator


def test_no_boundaries_gives_one_open_segment():
    assert segments_from_boundaries([]) == [Segment(0, None, None)]


def test_segments_are_contiguous():
    segments = segments_from_boundaries(['b', 'd'])
    assert segments == [Segment(0, None, 'b'), Segment(1, 'b', 'd'), Segment(2, 'd', None)]
    for left, right in pairwise(segments):
        assert left.end_inclusive == right.start_after


@pytest.mark.parametrize('boundaries', [['b', 'a'], ['a', 'a']])
def test_boundaries_must_be_strictly_increasing(boundaries):
    with pytest.raises(ListingError):
        segments_from_boundaries(boundaries)


def test_boundary_key_is_listed_exactly_once():
    keys = [PREFIX + letter for letter in 'abcd']
    client = FakeClient(keys)
    segments = segments_from_boundaries([PREFIX + 'b'])

    tables = [listing.list_segment(client, 'bucket', PREFIX, s) for s in segments]

    assert tables[0]['key'].to_pylist() == [PREFIX + 'a', PREFIX + 'b']
    assert tables[1]['key'].to_pylist() == [PREFIX + 'c', PREFIX + 'd']
    assert 'StartAfter' not in client.paginator.calls[0]
    assert client.paginator.calls[1]['StartAfter'] == PREFIX + 'b'


def test_listing_stops_after_passing_the_end():
    keys = [PREFIX + f'k{i:02d}' for i in range(20)]
    client = FakeClient(keys, page_size=5)

    table = listing.list_segment(client, 'bucket', PREFIX, Segment(0, None, PREFIX + 'k07'))

    assert table.num_rows == 8
    assert client.paginator.pages == 2
    assert table.schema == listing.SCHEMA


def test_list_bucket_and_merge(config, conn):
    keys = [PREFIX + f'media/x/{i:02d}.jpg' for i in range(10)]
    keys += [PREFIX + 'metadata/edan/acm/00.txt', PREFIX + f'3d/{UUID}/scene.svx.json']
    client = FakeClient(keys, page_size=3)
    segments = segments_from_boundaries([PREFIX + 'media/x/03.jpg', PREFIX + 'media/x/07.jpg'])

    assert listing.list_bucket(client, config, segments) == len(keys)
    assert listing.merge_listing(conn, config, segments) == len(keys)

    table = pq.read_table(config.listing_path)
    assert table.column_names == ['key', 'size', 'last_modified', 'etag']
    assert table['key'].to_pylist() == sorted(keys)


def _write_segments(config, rows_by_segment: dict[int, list[str]]) -> None:
    config.segments_dir.mkdir(parents=True, exist_ok=True)
    for index, keys in rows_by_segment.items():
        table = pa.table(
            {
                'key': pa.array(keys, pa.string()),
                'size': pa.array([1] * len(keys), pa.uint64()),
                'last_modified': pa.array([MODIFIED] * len(keys), pa.timestamp('us', tz='UTC')),
                'etag': pa.array(['e'] * len(keys), pa.string()),
                'segment': pa.array([index] * len(keys), pa.int32()),
            },
            schema=listing.SCHEMA,
        )
        pq.write_table(table, config.segments_dir / f'segment-{index:03d}.parquet')


VALID_EXTRA = [PREFIX + 'metadata/edan/acm/00.txt', PREFIX + f'3d/{UUID}/scene.svx.json']


def test_coverage_rejects_duplicates(config, conn):
    segments = segments_from_boundaries([PREFIX + 'b'])
    _write_segments(config, {0: [PREFIX + 'a', PREFIX + 'b'], 1: [PREFIX + 'b', *VALID_EXTRA]})
    with pytest.raises(ListingError, match='duplicate'):
        listing.merge_listing(conn, config, segments)


def test_coverage_rejects_keys_outside_their_segment(config, conn):
    segments = segments_from_boundaries([PREFIX + 'b'])
    _write_segments(config, {0: [PREFIX + 'a', PREFIX + 'c'], 1: VALID_EXTRA})
    with pytest.raises(ListingError, match='outside their range'):
        listing.merge_listing(conn, config, segments)


def test_coverage_rejects_foreign_prefix(config, conn):
    segments = segments_from_boundaries([])
    _write_segments(config, {0: ['other/prefix/a', *VALID_EXTRA]})
    with pytest.raises(ListingError, match='outside'):
        listing.merge_listing(conn, config, segments)


def test_coverage_requires_metadata_and_svx_files(config, conn):
    segments = segments_from_boundaries([])
    _write_segments(config, {0: [PREFIX + 'media/x/a.jpg']})
    with pytest.raises(ListingError, match='metadata file'):
        listing.merge_listing(conn, config, segments)


def test_merge_rejects_a_shrunken_listing(config, conn, tmp_path):
    previous = tmp_path / 'files.parquet'
    pq.write_table(pa.table({'url': [BASE_URL + f'media/x/{i}.jpg' for i in range(100)]}), previous)
    segments = segments_from_boundaries([])
    _write_segments(config, {0: [PREFIX + 'media/x/a.jpg', *VALID_EXTRA]})
    with pytest.raises(ListingError, match='previous catalog'):
        listing.merge_listing(conn, config, segments, previous)


def test_compute_boundaries_from_previous_catalog(config, conn, tmp_path):
    previous = tmp_path / 'files.parquet'
    urls = [BASE_URL + f'media/x/{i:03d}.jpg' for i in range(100)]
    pq.write_table(pa.table({'url': urls}), previous)

    boundaries = listing.compute_boundaries(conn, config, previous)

    assert len(boundaries) == config.list_segments - 1
    assert boundaries == sorted(boundaries)
    assert all(b.startswith(PREFIX + 'media/x/') for b in boundaries)


def test_compute_boundaries_rejects_foreign_urls(config, conn, tmp_path):
    previous = tmp_path / 'files.parquet'
    pq.write_table(
        pa.table({'url': [f'https://elsewhere.example/{i}' for i in range(20)]}), previous
    )
    with pytest.raises(ListingError, match='outside the prefix'):
        listing.compute_boundaries(conn, config, previous)
