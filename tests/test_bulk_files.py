"""The kept bulk files (#203): one per level, outcome and delivery, shared by every key."""

import pytest

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.managers.bulk_files import BulkFiles
from views_crafdapi.managers.disk_cache import CrafdDiskCacheManager

_TARGETS = ("pred_lr_ged_sb", "pred_lr_ged_ns", "pred_lr_ged_os")


def _dataset(seed, file_id):
    ds = ForecastDataset(make_fao_df(seed=seed, targets=_TARGETS))
    if file_id is not None:
        ds.source_file_id = file_id
    return ds


class _Builds:
    """A `build` that writes a marker and counts its calls."""

    def __init__(self):
        self.count = 0

    def __call__(self, path):
        self.count += 1
        path.write_text(f"build {self.count}")


def test_the_same_delivery_reuses_the_file_whatever_object_holds_it(tmp_path):
    """Each key, and the fallback, loads its own copy of one delivery; they share one file."""
    store, build = BulkFiles(tmp_path), _Builds()
    first, kept = store.get(_dataset(1, "fc1"), _dataset(2, "h1"), "admin1", "sb", build)
    again, _ = store.get(_dataset(1, "fc1"), _dataset(2, "h1"), "admin1", "sb", build)
    assert kept and first == again and build.count == 1 and first.read_text() == "build 1"


@pytest.mark.parametrize("changed", ["forecast", "history"])
def test_a_new_delivery_rebuilds_and_removes_the_old_files(tmp_path, changed):
    store, build = BulkFiles(tmp_path), _Builds()
    old_a, _ = store.get(_dataset(1, "fc1"), _dataset(2, "h1"), "admin1", "sb", build)
    old_b, _ = store.get(_dataset(1, "fc1"), _dataset(2, "h1"), "grid", "os", build)
    ids = ("fc2", "h1") if changed == "forecast" else ("fc1", "h2")
    fc, h = _dataset(1, ids[0]), _dataset(2, ids[1])
    new, _ = store.get(fc, h, "admin1", "sb", build)
    assert build.count == 3 and new.read_text() == "build 3"
    assert old_a.exists() and old_b.exists()        # building never removes: a route may send them
    store.remove_other_deliveries([(fc, h)])             # the loader, after the hand-over
    assert not old_a.exists() and not old_b.exists() and new.exists()


def test_each_level_and_outcome_is_its_own_file(tmp_path):
    store, build = BulkFiles(tmp_path), _Builds()
    fc, h = _dataset(1, "fc1"), _dataset(2, "h1")
    paths = {store.get(fc, h, lv, oc, build)[0] for lv, oc in [("admin1", "sb"), ("grid", "sb"), ("admin1", "ns")]}
    assert len(paths) == 3 and build.count == 3


@pytest.mark.parametrize("ids", [(None, "h1"), ("fc1", None), ("../x", "h1")])
def test_without_a_usable_store_id_the_file_is_not_kept(tmp_path, ids):
    """Unknown or unsafe ids: built for this request only, and never handed to another."""
    store, build = BulkFiles(tmp_path), _Builds()
    a, kept = store.get(_dataset(1, ids[0]), _dataset(2, ids[1]), "admin1", "sb", build)
    b, _ = store.get(_dataset(1, ids[0]), _dataset(2, ids[1]), "admin1", "sb", build)
    assert not kept and a != b and build.count == 2
    assert a.parent == tmp_path


def test_a_failed_build_leaves_no_file_and_the_next_request_builds_again(tmp_path):
    store = BulkFiles(tmp_path)
    fc, h = _dataset(1, "fc1"), _dataset(2, "h1")

    def broken(path):
        path.write_text("half")
        raise RuntimeError("out of memory")

    with pytest.raises(RuntimeError):
        store.get(fc, h, "admin1", "sb", broken)
    assert not any(tmp_path.glob("*.parquet*"))
    assert store.get(fc, h, "admin1", "sb", _Builds())[0].read_text() == "build 1"


def test_files_left_by_an_earlier_process_are_removed_with_the_next_delivery(tmp_path):
    (tmp_path / "sb_grid__old_old.parquet").write_text("left behind")
    store = BulkFiles(tmp_path)
    fc, h = _dataset(1, "fc1"), _dataset(2, "h1")
    store.get(fc, h, "admin1", "sb", _Builds())
    store.remove_other_deliveries([(fc, h)])
    assert not (tmp_path / "sb_grid__old_old.parquet").exists()


def test_a_dataset_read_from_the_disk_cache_carries_its_store_id(tmp_path):
    """The id is stamped where datasets are loaded, and copies keep it."""
    cache = CrafdDiskCacheManager(tmp_path / "datasets")
    cache.write("k" * 16, "forecast", _dataset(1, None), "file_abc", source_kind="wire", provenance={})
    read = cache.read("k" * 16, "forecast")["dataset"]
    assert read.source_file_id == "file_abc" and read.copy().source_file_id == "file_abc"


def test_a_failed_one_request_build_leaves_no_file(tmp_path):
    store = BulkFiles(tmp_path)

    def broken(path):
        path.write_text("half")
        raise RuntimeError("out of memory")

    with pytest.raises(RuntimeError):
        store.get(_dataset(1, None), _dataset(2, "h1"), "admin1", "sb", broken)
    assert list(tmp_path.iterdir()) == []


def test_a_new_delivery_leaves_one_request_files_being_sent(tmp_path):
    """A file built for one request belongs to that request until it is sent."""
    store, build = BulkFiles(tmp_path), _Builds()
    once, _ = store.get(_dataset(1, None), _dataset(2, "h1"), "admin1", "sb", build)
    fc, h = _dataset(1, "fc1"), _dataset(2, "h1")
    store.get(fc, h, "admin1", "sb", build)
    store.remove_other_deliveries([(fc, h)])
    assert once.exists()


@pytest.mark.parametrize("level,outcome", [("../admin1", "sb"), ("admin1", "sb/../../x"), ("gaul2", "sb")])
def test_a_file_name_never_comes_from_an_unknown_level_or_outcome(tmp_path, level, outcome):
    store, build = BulkFiles(tmp_path), _Builds()
    with pytest.raises(ValueError, match="unknown level or outcome"):
        store.get(_dataset(1, "fc1"), _dataset(2, "h1"), level, outcome, build)
    assert build.count == 0
