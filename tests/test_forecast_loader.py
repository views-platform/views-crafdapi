"""The loader (#208, ADR-044): the service loads the forecast itself, with the server's key,
before any partner asks, and hands over complete data in one step."""

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.managers.bulk_files import BulkFiles
from views_crafdapi.managers.disk_cache import CrafdDiskCacheManager
from views_crafdapi.managers.forecast_loader import ForecastLoader, read_server_key, SERVER_KEY_ENV

_TARGETS = ("pred_lr_ged_sb", "pred_lr_ged_ns", "pred_lr_ged_os")


def _dataset(seed, file_id):
    ds = ForecastDataset(make_fao_df(seed=seed, targets=_TARGETS))
    ds.source_file_id = file_id
    return ds


class _Service:
    """A stand-in for DatasetService: hands out what the store holds now, counting loads."""

    def __init__(self, disk=None):
        self.forecast, self.history = _dataset(1, "fc1"), _dataset(2, "h1")
        self.disk = disk   # like the real service, every load is written to the disk cache
        self.loads = []
        self.fail_history = False
        self.degraded = False

    def get_latest_dataset(self, manager, key, category):
        self.loads.append((key, category))
        if category == "historical" and self.fail_history:
            raise RuntimeError("history download failed")
        ds = self.history if category == "historical" else self.forecast
        if self.disk is not None:
            self.disk.write("k" * 16, category, ds, ds.source_file_id, source_kind="wire", provenance={})
        return ds

    def forecast_serving_state(self):
        return {"degraded": self.degraded}

    def served_forecast_provenance(self):
        return {"created_at": "2026-07-27"}

    def served_forecast_entry(self):
        return {"written_by": "test"}

    def api_key_hash(self, key):
        return "k" * 16


def _store(forecast_id="fc1", history_id="h1"):
    store = MagicMock()
    store.get_latest_manifest.return_value = {"fileId": forecast_id}
    store.get_latest_file_id.return_value = history_id
    return store


def _loader(tmp_path, service=None, store=None, key="server-key"):
    disk = CrafdDiskCacheManager(tmp_path / "datasets")
    service = service or _Service(disk)
    store = store or _store()
    keys_used = []

    def make_manager(k):
        keys_used.append(k)
        return store

    loader = ForecastLoader(service, disk,
                            BulkFiles(tmp_path / "bulk"), key, make_manager)
    return loader, service, store, keys_used


def test_nothing_is_served_before_the_first_load(tmp_path):
    loader, _, _, _ = _loader(tmp_path)
    assert loader.served() is None


def test_the_first_load_reads_with_the_server_key_and_hands_over_forecast_and_history(tmp_path):
    loader, service, _, keys_used = _loader(tmp_path)
    assert loader.refresh()
    served = loader.served()
    assert served.forecast.source_file_id == "fc1" and served.history.source_file_id == "h1"
    assert set(keys_used) == {"server-key"}
    assert {key for key, _ in service.loads} == {"server-key"}
    assert served.provenance == {"created_at": "2026-07-27"}


def test_the_nine_bulk_files_are_built_before_the_hand_over(tmp_path):
    loader, _, _, _ = _loader(tmp_path)
    loader.refresh()
    built = sorted(p.name.split("__")[0] for p in (tmp_path / "bulk").glob("*.parquet"))
    assert built == sorted(f"{o}_{lv}" for o in ("sb", "ns", "os") for lv in ("grid", "admin1", "country"))


def test_an_unchanged_delivery_is_not_loaded_again(tmp_path):
    loader, service, _, _ = _loader(tmp_path)
    loader.refresh()
    loads = len(service.loads)
    assert not loader.refresh()
    assert len(service.loads) == loads


def _deliveries(tmp_path):
    return {p.name.split("__")[1] for p in (tmp_path / "bulk").glob("*.parquet")}


@pytest.mark.parametrize("changed", ["forecast", "history"])
def test_a_new_delivery_is_loaded_and_older_bulk_files_removed_one_delivery_later(tmp_path, changed):
    """The previous delivery's files are kept, because a download that began before the hand-over
    may still open one; they go at the next new delivery."""
    store = _store()
    loader, service, _, _ = _loader(tmp_path, store=store)
    loader.refresh()
    for n, new_id in enumerate(("2", "3"), start=3):
        if changed == "forecast":
            service.forecast = _dataset(n, f"fc{new_id}")
            store.get_latest_manifest.return_value = {"fileId": f"fc{new_id}"}
        else:
            service.history = _dataset(n, f"h{new_id}")
            store.get_latest_file_id.return_value = f"h{new_id}"
        assert loader.refresh()
    served = loader.served()

    def pair(f, h):
        return f"{f}+{h}.parquet"

    if changed == "forecast":
        assert served.forecast.source_file_id == "fc3"
        assert _deliveries(tmp_path) == {pair("fc3", "h1"), pair("fc2", "h1")}
    else:
        assert served.history.source_file_id == "h3"
        assert _deliveries(tmp_path) == {pair("fc1", "h3"), pair("fc1", "h2")}


def test_a_failed_load_keeps_what_is_served(tmp_path):
    store = _store()
    loader, service, _, _ = _loader(tmp_path, store=store)
    loader.refresh()
    before = loader.served()
    store.get_latest_manifest.return_value = {"fileId": "fc2"}
    service.get_latest_dataset = MagicMock(side_effect=RuntimeError("store down"))
    assert not loader.refresh()
    assert loader.served() is before


def test_a_failed_check_with_the_store_keeps_what_is_served(tmp_path):
    store = _store()
    loader, _, _, _ = _loader(tmp_path, store=store)
    loader.refresh()
    before = loader.served()
    store.get_latest_manifest.side_effect = RuntimeError("timeout")
    assert not loader.refresh()
    assert loader.served() is before


def test_without_the_history_the_forecast_is_still_served_and_tried_again(tmp_path):
    loader, service, _, _ = _loader(tmp_path)
    service.fail_history = True
    assert loader.refresh()
    assert loader.served().history is None and loader.served().forecast is not None
    service.fail_history = False
    assert not loader.refresh()                   # not at every check: at most once an hour
    loader._retry_at = 0                          # the hour is over
    assert loader.refresh()
    assert loader.served().history is not None


def test_a_degraded_serve_is_tried_again_within_the_hour(tmp_path):
    """The newest run refused and the last good one served: tried again within the hour."""
    loader, service, _, _ = _loader(tmp_path)
    service.degraded = True
    loader.refresh()
    loads = len(service.loads)
    loader.refresh()
    assert len(service.loads) == loads            # a refused run is several GB: not every check
    loader._retry_at = 0                          # an hour later
    loader.refresh()
    assert len(service.loads) > loads


def test_without_a_server_key_nothing_is_ever_loaded(tmp_path):
    loader, service, _, _ = _loader(tmp_path, key=None)
    assert not loader.refresh()
    assert loader.served() is None and service.loads == []


def test_the_handed_over_data_is_read_freshly_from_disk(tmp_path):
    """The thread that loads must not share pandas objects with the routes: a fresh read of the
    same delivery from disk is handed over, not the loaded object."""
    loader, service, _, _ = _loader(tmp_path)
    loader.refresh()
    served = loader.served().forecast
    assert served is not service.forecast and served.source_file_id == "fc1"


def test_without_a_matching_disk_slot_a_later_hand_over_is_refused(tmp_path):
    """After the first load the loaded object may share its index with what the routes read, so
    it is never handed over; the data already served stays."""
    store = _store()
    loader, service, _, _ = _loader(tmp_path, store=store)
    loader.refresh()
    before = loader.served()
    service.disk = None                                   # the new load reaches no disk slot
    service.forecast = _dataset(5, "fc2")
    store.get_latest_manifest.return_value = {"fileId": "fc2"}
    assert not loader.refresh()
    assert loader.served() is before and loader.failing_since() is not None


def test_at_the_first_load_the_loaded_copy_may_be_handed_over(tmp_path):
    """Nothing is being read yet, so nothing can be shared."""
    loader, service, _, _ = _loader(tmp_path)
    service.disk = None
    assert loader.refresh()
    assert loader.served().forecast is service.forecast


def test_a_successful_load_clears_the_failure(tmp_path):
    store = _store()
    loader, service, _, _ = _loader(tmp_path, store=store)
    loader.refresh()
    store.get_latest_manifest.side_effect = RuntimeError("timeout")
    loader.refresh()
    assert loader.failing_since() is not None
    store.get_latest_manifest.side_effect = None
    store.get_latest_manifest.return_value = {"fileId": "fc2"}
    service.forecast = _dataset(6, "fc2")
    assert loader.refresh() and loader.failing_since() is None


def test_a_stopped_loader_loads_nothing(tmp_path):
    loader, service, _, _ = _loader(tmp_path)
    loader.stop()
    assert not loader.refresh() and service.loads == []


@pytest.mark.parametrize("raw,expected", [(None, 5.0), ("10", 10.0), ("1", 1.0), ("60", 60.0), ("0.9", 5.0),
                                          ("60.1", 5.0), ("inf", 5.0), ("0", 5.0), ("1e9", 5.0), ("x", 5.0)])
def test_the_check_interval_is_kept_between_one_and_sixty_minutes(monkeypatch, caplog, raw, expected):
    from views_crafdapi.managers.forecast_loader import check_minutes

    if raw is None:
        monkeypatch.delenv("CRAFDAPI_CHECK_MINUTES", raising=False)
    else:
        monkeypatch.setenv("CRAFDAPI_CHECK_MINUTES", raw)
    with caplog.at_level(logging.WARNING, logger="views_crafdapi.managers.forecast_loader"):
        assert check_minutes() == expected
    ignored = raw not in (None, "10", "1", "60")
    assert ("CRAFDAPI_CHECK_MINUTES" in caplog.text) == ignored   # a setting ignored is said so


def test_read_server_key(monkeypatch, caplog):
    monkeypatch.delenv(SERVER_KEY_ENV, raising=False)
    with caplog.at_level(logging.CRITICAL):
        assert read_server_key() is None
    assert SERVER_KEY_ENV in caplog.text
    monkeypatch.setenv(SERVER_KEY_ENV, "  secret-value-123 \n")
    caplog.clear()
    assert read_server_key() == "secret-value-123"
    assert "secret-value-123" not in caplog.text


def test_run_checks_at_start_and_again_after_the_interval(tmp_path):
    loader, _, _, _ = _loader(tmp_path)
    calls = []
    loader.refresh = lambda: calls.append(1) or False

    async def two_rounds():
        task = asyncio.create_task(loader.run(minutes=0.0005))   # 0.03 seconds
        await asyncio.sleep(0.1)
        loader.stop()
        await asyncio.wait_for(task, timeout=2)                  # a stop ends the loop

    asyncio.run(two_rounds())
    assert len(calls) >= 2


def test_each_check_logs_what_the_store_listed(tmp_path, caplog):
    """The listing request is what proves the key may read the delivery table (rows.read)."""
    loader, _, _, _ = _loader(tmp_path)
    with caplog.at_level(logging.INFO, logger="views_crafdapi.managers.forecast_loader"):
        loader.refresh()
    assert "store listing: newest forecast manifest fc1, newest history file h1" in caplog.text


def test_an_empty_listing_is_logged_as_an_error_not_a_quiet_month(tmp_path, caplog):
    loader, _, _, _ = _loader(tmp_path, store=_store(forecast_id=None))
    with caplog.at_level(logging.INFO, logger="views_crafdapi.managers.forecast_loader"):
        loader.refresh()
    assert "newest forecast manifest NONE" in caplog.text
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("rows.read" in r.getMessage() for r in errors)


def test_an_incomplete_hand_over_is_tried_again_after_an_hour_without_help(tmp_path, monkeypatch):
    import views_crafdapi.managers.forecast_loader as fl

    loader, service, _, _ = _loader(tmp_path)
    service.degraded = True
    loader.refresh()
    loads = len(service.loads)
    now = fl.time.time()
    monkeypatch.setattr(fl.time, "time", lambda: now + fl.RETRY_SECONDS + 1)
    loader.refresh()
    assert len(service.loads) > loads


def test_the_reported_serving_state_is_the_one_at_the_hand_over(tmp_path):
    """/health reports what partners are served; a later failed attempt must not rewrite it."""
    loader, service, _, _ = _loader(tmp_path)
    state = {"degraded": False}
    service.forecast_serving_state = lambda: state
    loader.refresh()
    state["degraded"] = True
    assert loader.served().serving_state == {"degraded": False}


def test_a_stop_during_the_bulk_files_ends_the_load_without_a_hand_over(tmp_path, monkeypatch):
    import views_crafdapi.managers.forecast_loader as fl

    loader, _, _, _ = _loader(tmp_path)
    built = []

    def build_then_stop(path, *args):
        built.append(path)
        loader.stop()
        path.write_bytes(b"x")

    monkeypatch.setattr(fl, "write_bulk_file", build_then_stop)
    assert not loader.refresh()
    assert loader.served() is None and len(built) == 1


def test_run_ends_promptly_after_a_stop(tmp_path):
    loader, _, _, _ = _loader(tmp_path)
    calls = []
    loader.refresh = lambda: calls.append(1) or False

    async def stop_after_first():
        task = asyncio.create_task(loader.run(minutes=0.0005))
        await asyncio.sleep(0.05)
        loader.stop()
        seen = len(calls)
        await asyncio.wait_for(task, timeout=2)
        return seen

    seen = asyncio.run(stop_after_first())
    assert len(calls) == seen   # no check after the stop


def test_a_clean_up_failure_after_the_hand_over_is_not_a_failed_load(tmp_path):
    loader, _, _, _ = _loader(tmp_path)

    def broken(keep):
        raise OSError("disk hiccup")

    loader._bulk_files.remove_other_deliveries = broken
    assert loader.refresh()
    assert loader.failing_since() is None and loader.served() is not None


def test_the_loader_reads_the_disk_slot_the_real_service_writes(tmp_path):
    """The loader finds the server key's own loads under `DatasetService.api_key_hash`; the real
    method must give the label the service writes its slot under."""
    from views_crafdapi.managers.dataset_service import DatasetService

    hash_fn = lambda key: "h-" + key   # noqa: E731
    service = DatasetService(dataframe_cache={}, file_cache={}, disk_cache=None, prediction_bucket_id="b",
                             configs_getter=lambda: {}, api_key_hash_fn=hash_fn,
                             check_staleness_fn=lambda *a, **k: None)
    assert service.api_key_hash("server-key") == "h-server-key"
