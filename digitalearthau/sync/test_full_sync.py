import logging
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Tuple

import pytest
import structlog
from dateutil import tz

from datacube.utils import uri_to_local_path
from digitalearthau import paths
from digitalearthau.archive import CleanConsoleRenderer
from digitalearthau.collections import Collection
from digitalearthau.index import DatasetLite, MemoryDatasetPathIndex
from digitalearthau.paths import write_files, register_base_directory
from digitalearthau.sync import differences as mm, fixes, scan, Mismatch


# These are ok in tests.
# pylint: disable=too-many-locals, protected-access, redefined-outer-name


@pytest.fixture(scope="session", autouse=True)
def configure_log_output(request):
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M.%S"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Coloured output if to terminal.
            CleanConsoleRenderer()
        ],
        context_class=dict,
        cache_logger_on_first_use=True,
    )


@pytest.fixture
def syncable_environment():
    on_disk = DatasetLite(uuid.UUID('1e47df58-de0f-11e6-93a4-185e0f80a5c0'))
    on_disk2 = DatasetLite(uuid.UUID('3604ee9c-e1e8-11e6-8148-185e0f80a5c0'))
    root = write_files(
        {
            'ls8_scenes': {
                'ls8_test_dataset': {
                    'ga-metadata.yaml':
                        ('id: %s\n' % on_disk.id),
                    'dummy-file.txt': ''
                }
            },
            'ls7_scenes': {
                'ls7_test_dataset': {
                    'ga-metadata.yaml':
                        ('id: %s\n' % on_disk2.id)
                }
            }
        }
    )
    cache_path = root.joinpath('cache')
    cache_path.mkdir()
    on_disk_uri = root.joinpath('ls8_scenes', 'ls8_test_dataset', 'ga-metadata.yaml').as_uri()
    on_disk_uri2 = root.joinpath('ls7_scenes', 'ls7_test_dataset', 'ga-metadata.yaml').as_uri()

    ls8_collection = Collection(
        name='ls8_scene_collection',
        query={},
        file_patterns=[str(root.joinpath('ls8_scenes', 'ls*/ga-metadata.yaml'))],
        unique=[],
        index=MemoryDatasetPathIndex()
    )

    # register this as a base directory so that datasets can be trashed within it.
    register_base_directory(root)

    return ls8_collection, on_disk, on_disk_uri, root


def test_index_disk_sync(syncable_environment):
    # type: (Tuple[Collection, DatasetLite, str, Path]) -> None
    ls8_collection, on_disk, on_disk_uri, root = syncable_environment

    # An indexed file not on disk, and disk file not in index.

    missing_uri = root.joinpath('indexed', 'already', 'ga-metadata.yaml').as_uri()
    old_indexed = DatasetLite(uuid.UUID('b9d77d10-e1c6-11e6-bf63-185e0f80a5c0'))
    ls8_collection._index.add_dataset(old_indexed, missing_uri)

    _check_sync(
        collection=ls8_collection,
        expected_paths=[
            missing_uri,
            on_disk_uri
        ],
        expected_mismatches=[
            mm.LocationMissingOnDisk(old_indexed, missing_uri),
            mm.DatasetNotIndexed(on_disk, on_disk_uri)
        ],
        expected_index_result={
            on_disk: (on_disk_uri,),
            old_indexed: ()
        },
        cache_path=root,
        fix_settings=dict(index_missing=True, update_locations=True)
    )

    # File on disk has a different id to the one in the index (ie. it was quietly reprocessed)
    ls8_collection._index.reset()
    ls8_collection._index.add_dataset(old_indexed, on_disk_uri)
    _check_sync(
        collection=ls8_collection,
        expected_paths=[
            on_disk_uri
        ],
        expected_mismatches=[
            mm.LocationMissingOnDisk(old_indexed, on_disk_uri),
            mm.DatasetNotIndexed(on_disk, on_disk_uri),
        ],
        expected_index_result={
            on_disk: (on_disk_uri,),
            old_indexed: ()
        },
        cache_path=root,
        fix_settings=dict(index_missing=True, update_locations=True)
    )

    # File on disk was moved without updating index, replacing existing indexed file location.
    ls8_collection._index.reset()
    ls8_collection._index.add_dataset(old_indexed, on_disk_uri)
    ls8_collection._index.add_dataset(on_disk, missing_uri)
    _check_sync(
        collection=ls8_collection,
        expected_paths=[
            on_disk_uri,
            missing_uri
        ],
        expected_mismatches=[
            mm.LocationMissingOnDisk(old_indexed, on_disk_uri),
            mm.LocationNotIndexed(on_disk, on_disk_uri),
            mm.LocationMissingOnDisk(on_disk, missing_uri),
        ],
        expected_index_result={
            on_disk: (on_disk_uri,),
            old_indexed: ()
        },
        cache_path=root,
        fix_settings=dict(index_missing=True, update_locations=True)
    )

    # A an already-archived file in on disk. Should report it, but not touch the file (trash_archived is false)
    ls8_collection._index.reset()
    archived_on_disk = DatasetLite(on_disk.id, archived_time=(datetime.utcnow() - timedelta(days=5)))
    ls8_collection._index.add_dataset(archived_on_disk, on_disk_uri)
    assert uri_to_local_path(on_disk_uri).exists(), "On-disk location should exist before test begins."
    _check_sync(
        collection=ls8_collection,
        expected_paths=[
            on_disk_uri
        ],
        expected_mismatches=[
            mm.ArchivedDatasetOnDisk(archived_on_disk, on_disk_uri),
        ],
        expected_index_result={
            on_disk: (on_disk_uri,),
        },
        cache_path=root,
        fix_settings=dict(index_missing=True, update_locations=True)
    )
    assert uri_to_local_path(on_disk_uri).exists(), "On-disk location shouldn't be touched"


def test_detect_corrupt(syncable_environment):
    # type: (Tuple[Collection, str, str, Path]) -> None
    """If a dataset exists but cannot be read, report as corrupt"""
    ls8_collection, on_disk, on_disk_uri, root = syncable_environment
    path = uri_to_local_path(on_disk_uri)
    os.unlink(str(path))
    with path.open('w') as f:
        f.write('corruption!')
    assert path.exists()

    # Another dataset exists in the same location
    ls8_collection._index.add_dataset(DatasetLite(uuid.UUID("a2a51f76-3b67-11e7-9fa9-185e0f80a5c0")), on_disk_uri)
    _check_sync(
        collection=ls8_collection,
        expected_paths=[on_disk_uri],
        expected_mismatches=[
            mm.UnreadableDataset(None, on_disk_uri)
        ],
        # Unmodified index
        expected_index_result=ls8_collection._index.as_map(),
        cache_path=root,
        fix_settings=dict(trash_missing=True, trash_archived=True, update_locations=True)
    )
    # If a dataset is in the index pointing to the corrupt location, it shouldn't be trashed with trash_archived=True
    assert path.exists(), "Corrupt dataset with sibling in index should not be trashed"

    # No dataset in index at the corrupt location, so it should be trashed.
    ls8_collection._index.reset()
    _check_sync(
        collection=ls8_collection,
        expected_paths=[on_disk_uri],
        expected_mismatches=[
            mm.UnreadableDataset(None, on_disk_uri)
        ],
        expected_index_result={},
        cache_path=root,
        fix_settings=dict(trash_missing=True, trash_archived=True, update_locations=True)
    )
    assert not path.exists(), "Corrupt dataset without sibling should be trashed with trash_archived=True"


_TRASH_PREFIX = ('.trash', (datetime.utcnow().strftime('%Y-%m-%d')))


# noinspection PyShadowingNames
def test_remove_missing(syncable_environment):
    """An on-disk dataset that's not indexed should be trashed when trash_missing=True"""
    ls8_collection, on_disk, on_disk_uri, root = syncable_environment

    register_base_directory(root)
    on_disk_path = root.joinpath('ls8_scenes', 'ls8_test_dataset', 'ga-metadata.yaml')
    trashed_path = root.joinpath(*_TRASH_PREFIX, 'ls8_scenes', 'ls8_test_dataset', 'ga-metadata.yaml')

    # Add a second dataset outside of the collection folder. Should not be touched!
    paths.write_files(
        {
            'ls8_test_dataset2': {
                'ga-metadata.yaml': 'id: 5294efa6-348d-11e7-a079-185e0f80a5c0\n',
                'dummy-file.txt': ''
            }
        },
        containing_dir=root
    )

    outside_path = root.joinpath('ls8_test_dataset2')
    assert outside_path.exists()

    assert on_disk_path.exists(), "On-disk location should exist before test begins."
    assert not trashed_path.exists(), "Trashed file shouldn't exit."
    _check_sync(
        collection=ls8_collection,
        expected_paths=[
            on_disk_uri
        ],
        expected_mismatches=[
            mm.DatasetNotIndexed(on_disk, on_disk_uri)
        ],
        expected_index_result={},
        cache_path=root,
        fix_settings=dict(trash_missing=True, update_locations=True)
    )
    assert not on_disk_path.exists(), "On-disk location should exist before test begins."
    assert trashed_path.exists(), "Trashed file shouldn't exit."
    assert outside_path.exists(), "Dataset outside of collection folder shouldn't be touched"


@pytest.mark.parametrize("archived_dt,expect_to_be_trashed", [
    # Default settings: trash files archived more than three days ago.
    # Four days ago, should be trashed.
    (datetime.utcnow() - timedelta(days=4), True),
    # Only one day ago, not trashed
    (datetime.utcnow() - timedelta(days=1), False),
    # One day in the future, not trashed.
    (datetime.utcnow() + timedelta(days=1), False),

    # ------ With embedded timezone info ------
    # Four days ago, should be trashed.
    (datetime.utcnow().replace(tzinfo=tz.tzutc()) - timedelta(days=4), True),
    # Only one day ago, not trashed
    (datetime.utcnow().replace(tzinfo=tz.tzutc()) - timedelta(days=1), False),
])
def test_is_trashed(syncable_environment, archived_dt, expect_to_be_trashed):
    ls8_collection, on_disk, on_disk_uri, root = syncable_environment

    # Same test, but trash_archived=True, so it should be renamed to the.
    register_base_directory(root)
    archived_on_disk = DatasetLite(on_disk.id, archived_time=archived_dt)
    ls8_collection._index.add_dataset(archived_on_disk, on_disk_uri)
    on_disk_path = root.joinpath('ls8_scenes', 'ls8_test_dataset', 'ga-metadata.yaml')

    trashed_path = root.joinpath(*_TRASH_PREFIX, 'ls8_scenes', 'ls8_test_dataset', 'ga-metadata.yaml')
    # Before the test, file is in place and nothing trashed.
    assert on_disk_path.exists(), "On-disk location should exist before test begins."
    assert not trashed_path.exists(), "Trashed file shouldn't exit."
    _check_sync(
        collection=ls8_collection,
        expected_paths=[
            on_disk_uri
        ],
        expected_mismatches=[
            mm.ArchivedDatasetOnDisk(archived_on_disk, on_disk_uri),
        ],
        expected_index_result={
            on_disk: (on_disk_uri,),
        },
        cache_path=root,
        fix_settings=dict(index_missing=True, update_locations=True, trash_archived=True)
    )

    # Show output structure for debugging
    print("Output structure")
    for p in paths.list_file_paths(root):
        print("\t{}".format(p))

    if expect_to_be_trashed:
        assert trashed_path.exists(), "File isn't in trash."
        assert not on_disk_path.exists(), "On-disk location still exists (should have been moved to trash)."
    else:
        assert not trashed_path.exists(), "File shouldn't have been trashed."
        assert on_disk_path.exists(), "On-disk location should still be in place."


def _check_sync(expected_paths: Iterable[str],
                collection: Collection,
                expected_mismatches: Iterable[Mismatch],
                expected_index_result: Mapping[DatasetLite, Iterable[str]],
                cache_path: Path,
                fix_settings: dict):
    """Check the correct outputs come from the given sync inputs"""
    log = structlog.getLogger()

    cache_path = cache_path.joinpath(str(uuid.uuid4()))
    cache_path.mkdir()

    _check_pathset_loading(cache_path, expected_paths, log, collection)

    mismatches = _check_mismatch_find(cache_path, expected_mismatches, collection._index, log, collection)

    _check_mismatch_fix(collection._index, mismatches, expected_index_result, fix_settings=fix_settings)


# noinspection PyProtectedMember
def _check_pathset_loading(cache_path: Path,
                           expected_paths: Iterable[str],
                           log: logging.Logger,
                           collection: Collection):
    """Check that the right mix of paths (index and filesystem) are loaded"""
    path_set = scan.build_pathset(collection, cache_path, log=log)

    loaded_paths = set(path_set.iterkeys('file://'))
    assert loaded_paths == set(expected_paths)

    # Sanity check that a random path doesn't match...
    dummy_dataset = cache_path.joinpath('dummy_dataset', 'ga-metadata.yaml')
    assert dummy_dataset.absolute().as_uri() not in path_set


def _check_mismatch_find(cache_path, expected_mismatches, index, log, collection: Collection):
    """Check that the correct mismatches were found"""

    mismatches = []

    for mismatch in scan.mismatches_for_collection(collection, cache_path, index):
        print(repr(mismatch))
        mismatches.append(mismatch)

    def mismatch_sort_key(m):
        dataset_id = None
        if m.dataset:
            dataset_id = m.dataset.id
        return m.__class__.__name__, dataset_id, m.uri

    sorted_mismatches = sorted(mismatches, key=mismatch_sort_key)
    sorted_expected_mismatches = sorted(expected_mismatches, key=mismatch_sort_key)

    assert sorted_mismatches == sorted_expected_mismatches

    # DatasetLite.__eq__ only tests for identical ids, so we'll check the properties here too.
    # This is to catch when we're passing the indexed instance of DatasetLite vs the one Loaded from the file.
    # (eg. only the indexed one will have archived information.)
    for i, mismatch in enumerate(sorted_mismatches):
        expected_mismatch = sorted_expected_mismatches[i]

        if not expected_mismatch.dataset:
            assert not mismatch.dataset
        else:
            assert expected_mismatch.dataset.__dict__ == mismatch.dataset.__dict__

    return mismatches


def _check_mismatch_fix(index: MemoryDatasetPathIndex,
                        mismatches: Iterable[Mismatch],
                        expected_index_result: Mapping[DatasetLite, Iterable[str]],
                        fix_settings: dict):
    """Check that the index is correctly updated when fixing mismatches"""

    # First check that no change is made to the index if we have all fixes set to False.
    starting_index = index.as_map()
    # Default settings are all false.
    fixes.fix_mismatches(mismatches, index)
    assert starting_index == index.as_map(), "Changes made to index despite all fix settings being " \
                                             "false (index_missing=False etc)"

    # Now perform fixes, check that they match expected.
    fixes.fix_mismatches(mismatches, index, **fix_settings)
    assert expected_index_result == index.as_map()
