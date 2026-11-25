"""A downloaded file is always cached inside the cache folder (#216, CodeQL py/path-injection).

The file name comes from the store, and the store accepts any name, so a crafted name must not
place the file elsewhere on the server.
"""
from datetime import timedelta
from pathlib import Path

import pytest

from unittest.mock import Mock, patch

from views_crafdapi.managers.appwrite.file_cache import (
    CacheManager,
    CacheMetadata,
    CacheValidationResult,
    _single_path_part,
)


@pytest.fixture
def cache(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    return CacheManager(cache_dir=root, cache_ttl=timedelta(hours=1))


@pytest.mark.parametrize("bucket_id, file_id", [
    ("..", "f1"), ("../..", "f1"), ("/etc", "f1"), ("b", "../../outside.bin"),
    ("b", "/etc/outside.bin"), ("b", "sub/../../outside.bin"),
])
def test_crafted_ids_stay_inside_the_cache_folder(cache, bucket_id, file_id):
    try:
        path = cache._get_cache_path(bucket_id, file_id)
    except ValueError:
        return  # refused outright is also safe
    assert path.is_relative_to(Path(cache.cache_dir).resolve())


@pytest.mark.parametrize("file_id", ["", ".", ".."])
def test_an_id_that_is_not_a_file_name_is_refused(cache, file_id):
    with pytest.raises(ValueError):
        cache._get_cache_path("bucket", file_id)


def test_the_cache_file_is_named_by_the_file_id(cache):
    path = cache._get_cache_path("bucket", "file1")
    assert path == Path(cache.cache_dir).resolve() / "bucket" / "file1"


def test_two_files_with_the_same_name_get_different_cache_files(cache):
    """The store's file name is not used, so runA/manifest.json and runB/manifest.json differ."""
    assert cache._get_cache_path("bucket", "idA") != cache._get_cache_path("bucket", "idB")


def test_a_cache_entry_pointing_outside_the_folder_is_never_read(cache, tmp_path):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x")
    cache.cache_metadata[cache._get_cache_key("bucket", "file1")] = CacheMetadata(
        bucket_id="bucket", file_id="file1", path=str(outside),
        cached_at="2026-11-19T00:00:00", size_bytes=1, filename="outside.bin",
    )
    # The stored path is never used: the cache file is derived from the ids, and none exists.
    assert not cache.get_cached_file_path("bucket", "file1").success
    assert cache.validate_cache("bucket", "file1").name == "NOT_FOUND"
    cache.remove_from_cache("bucket", "file1")
    cache.cache_metadata[cache._get_cache_key("bucket", "file2")] = CacheMetadata(
        bucket_id="bucket", file_id="file2", path=str(outside),
        cached_at="2000-01-01T00:00:00", size_bytes=1, filename="outside.bin",
    )
    cache.clear_cache()
    assert outside.exists(), "a delete must never reach a file outside the cache folder"


def test_no_log_line_writes_a_key_or_its_fingerprint():
    """#217: log lines name the category, never the caller's key or a value derived from it."""
    import re
    src = Path(__file__).resolve().parents[1] / "src" / "views_crafdapi"
    offenders = []
    for f in src.rglob("*.py"):
        text = f.read_text()
        # A tripwire, not a proof: it reads `logger.`/`logging.` calls whose text names the key.
        for m in re.finditer(r"(?:logger|logging)\.\w+\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)",
                             text, re.S):
            # also any name the fingerprint was copied into, e.g. `fp = api_key_hash`
            aliases = set(re.findall(r"(\w+)\s*=\s*api_key_hash\b", text))
            names = r"api_key|key_hash" + "".join(rf"|\b{a}\b" for a in aliases)
            if re.search(names, m.group(0)):
                offenders.append(f"{f.name}: {m.group(0)[:80]}")
    assert not offenders, offenders


@pytest.mark.parametrize("name", ["a/b/c.bin", "../../x", "/etc/x", "run1/manifest"])
def test_a_name_with_folders_is_refused_not_shortened(name):
    """The first layer on its own. Shortening would let run1/x and run2/x share one file."""
    with pytest.raises(ValueError):
        _single_path_part(name)


def test_a_bucket_folder_that_links_outside_is_refused(cache, tmp_path):
    """The second layer on its own: even a one-part name is refused if it leads outside."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (Path(cache.cache_dir) / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        cache._get_cache_path("linked", "file1")


def test_an_entry_with_an_unusable_id_is_a_miss_not_an_error(cache):
    cache.cache_metadata[cache._get_cache_key("..", "file1")] = CacheMetadata(
        bucket_id="..", file_id="file1", path="/etc/passwd",
        cached_at="2026-11-19T00:00:00", size_bytes=1, filename="passwd",
    )
    assert cache.validate_cache("..", "file1") == CacheValidationResult.NOT_FOUND
    assert not cache.get_cached_file_path("..", "file1").success


def _manager(tmp_path):
    from views_crafdapi.managers.appwrite.manager import AppWriteFileManager
    from views_crafdapi.managers.appwrite.config import AppwriteConfig, AuthMethod
    from views_crafdapi.managers.appwrite.results import OperationResult

    path_manager = Mock()
    path_manager.cache = tmp_path
    config = AppwriteConfig(
        endpoint="https://example.invalid/v1", project_id="p", credentials="k",
        auth_method=AuthMethod.API_KEY, bucket_id="b", cache_dir=str(tmp_path / "cache"),
        path_manager=path_manager,
    )
    with patch("views_crafdapi.managers.appwrite.manager.Client"), \
         patch("views_crafdapi.managers.appwrite.manager.Storage"), \
         patch("views_crafdapi.managers.appwrite.manager.Databases"), \
         patch("views_crafdapi.managers.appwrite.manager.Users"):
        mgr = AppWriteFileManager(config)
    mgr.storage.get_file_download.return_value = b"content"
    mgr.get_file = Mock(return_value=OperationResult(
        success=True, data={"name": "x", "$updatedAt": "2026-01-01T00:00:00Z",
                            "sizeOriginal": len(b"content")}))
    return mgr


def test_a_download_with_an_unusable_id_is_served_but_not_cached(tmp_path):
    mgr = _manager(tmp_path)
    result = mgr.download_file("..", "file1", use_cache=False)
    assert result.success
    assert result.data["file_bytes"] == b"content"
    assert result.data["from_cache"] is False
    assert not mgr.cache_manager.cache_metadata, "nothing may be cached for an unusable id"


def test_the_disk_cache_logs_no_key_fingerprint(tmp_path, caplog):
    """Runs the cache, so a renamed variable cannot hide the fingerprint from this check."""
    import logging
    from tests.conftest import make_fao_df
    from views_crafdapi.data.handlers import ForecastDataset
    from views_crafdapi.managers.disk_cache import CrafdDiskCacheManager

    fingerprint = "fp0123456789abcd"
    cache = CrafdDiskCacheManager(tmp_path)
    with caplog.at_level(logging.DEBUG):
        cache.write(fingerprint, "forecast",
                    ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=16)), "file_1")
        cache.read(fingerprint, "forecast")
    assert caplog.records, "the check must see some log lines"
    assert fingerprint not in caplog.text


def test_a_sibling_folder_sharing_the_prefix_is_outside(tmp_path):
    """/cache2/x starts with the text "/cache" but is not inside /cache."""
    root = tmp_path / "cache"
    root.mkdir()
    sibling = tmp_path / "cache2"
    sibling.mkdir()
    (root / "b").symlink_to(sibling, target_is_directory=True)
    cache = CacheManager(cache_dir=root, cache_ttl=timedelta(hours=1))
    with pytest.raises(ValueError):
        cache._get_cache_path("b", "f1")


def test_a_cache_folder_at_the_filesystem_root_still_works():
    """Runs __init__ for real: the prefix for "/" must be "/", not "//"."""
    cache = CacheManager(cache_dir=Path("/"), cache_ttl=timedelta(hours=1))
    assert cache._get_cache_path("b", "f1") == Path("/b/f1")


def test_a_cache_folder_reached_through_a_link_still_works(tmp_path):
    real = tmp_path / "real_cache"
    real.mkdir()
    link = tmp_path / "link_cache"
    link.symlink_to(real, target_is_directory=True)
    cache = CacheManager(cache_dir=link, cache_ttl=timedelta(hours=1))
    assert cache._get_cache_path("b", "f1") == real.resolve() / "b" / "f1"


def test_a_bucket_id_with_folders_is_refused(cache):
    with pytest.raises(ValueError):
        cache._get_cache_path("a/b", "f1")


def test_a_bucket_folder_that_is_the_cache_folder_itself_is_refused(cache):
    """Files would land beside cache_metadata.json and could overwrite it."""
    (Path(cache.cache_dir) / "b").symlink_to(Path(cache.cache_dir), target_is_directory=True)
    with pytest.raises(ValueError):
        cache._get_cache_path("b", "cache_metadata.json")


def test_two_bucket_and_file_pairs_never_share_a_cache_key(cache):
    assert cache._get_cache_key("a_b", "c") != cache._get_cache_key("a", "b_c")


def test_a_download_whose_cache_folder_cannot_be_made_is_served_uncached(tmp_path):
    mgr = _manager(tmp_path)
    blocker = Path(mgr.cache_manager.cache_dir) / "b"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("a file where the bucket folder should be")
    result = mgr.download_file("b", "file1", use_cache=False)
    assert result.success and result.data["from_cache"] is False
    assert not mgr.cache_manager.cache_metadata


def test_a_lookup_creates_no_folder(cache):
    cache._get_cache_path("never-downloaded", "f1")
    assert not (Path(cache.cache_dir) / "never-downloaded").exists()


def test_a_cached_file_that_is_a_link_is_returned_as_the_link(cache):
    bucket = Path(cache.cache_dir) / "b"
    bucket.mkdir()
    (bucket / "f2").write_bytes(b"x")
    (bucket / "f1").symlink_to(bucket / "f2")
    assert cache._get_cache_path("b", "f1") == bucket.resolve() / "f1"


def test_a_cached_file_that_is_a_link_is_never_read(cache, tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"x")
    bucket = Path(cache.cache_dir) / "b"
    bucket.mkdir()
    (bucket / "f1").symlink_to(secret)
    cache.add_to_cache("b", "f1", bucket / "f1")
    assert cache.validate_cache("b", "f1") == CacheValidationResult.NOT_FOUND
    assert not cache.get_cached_file_path("b", "f1").success


def test_a_bucket_folder_that_links_to_another_bucket_is_refused(cache):
    """Otherwise (b, f) and (c, f) would share one cache file."""
    (Path(cache.cache_dir) / "c").mkdir()
    (Path(cache.cache_dir) / "b").symlink_to(Path(cache.cache_dir) / "c", target_is_directory=True)
    with pytest.raises(ValueError, match="'b'"):
        cache._get_cache_path("b", "f1")


@pytest.mark.parametrize("name", [f"a{chr(c)}b" for c in range(32)])
def test_a_name_with_control_characters_is_refused(name):
    with pytest.raises(ValueError):
        _single_path_part(name)


def test_entries_from_before_the_change_are_dropped_and_their_files_deleted(tmp_path):
    import json
    root = tmp_path / "cache"
    (root / "b").mkdir(parents=True)
    old_file = root / "b" / "forecast.parquet"
    old_file.write_bytes(b"old")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"keep")
    current = {"bucket_id": "b", "file_id": "f2", "path": str(root / "b" / "f2"),
               "cached_at": "2026-11-19T00:00:00", "size_bytes": 1, "filename": "f2"}
    (root / "cache_metadata.json").write_text(json.dumps({
        "b_f1": {**current, "file_id": "f1", "path": str(old_file), "filename": "forecast.parquet"},
        "b_f3": {**current, "file_id": "f3", "path": str(outside), "filename": "outside.bin"},
        "b/f2": current,
    }))
    cache = CacheManager(cache_dir=root, cache_ttl=timedelta(hours=1))
    assert list(cache.cache_metadata) == ["b/f2"]
    assert not old_file.exists()
    assert outside.exists(), "a file outside the cache folder is never deleted"


def test_the_index_is_written_whole(cache):
    cache.add_to_cache("b", "f1", Path(cache.cache_dir) / "missing")
    cache._save_cache_metadata()
    assert [p.name for p in Path(cache.cache_dir).iterdir() if ".tmp-" in p.name] == []
    reloaded = CacheManager(cache_dir=Path(cache.cache_dir), cache_ttl=timedelta(hours=1))
    assert "b/f1" in reloaded.cache_metadata


def _old_index(root, entries):
    import json
    (root / "cache_metadata.json").write_text(json.dumps({
        key: {"bucket_id": "b", "file_id": fid, "path": str(path), "cached_at": "2026-11-19T00:00:00",
              "size_bytes": 1, "filename": "x"}
        for key, fid, path in entries
    }))


def test_an_old_entry_that_is_a_link_is_not_deleted(tmp_path):
    root = tmp_path / "cache"
    (root / "b").mkdir(parents=True)
    target = root / "b" / "real"
    target.write_bytes(b"x")
    link = root / "b" / "link"
    link.symlink_to(target)
    _old_index(root, [("b_f1", "f1", link)])
    CacheManager(cache_dir=root, cache_ttl=timedelta(hours=1))
    assert link.is_symlink() and target.exists()


def test_an_old_entry_behind_a_linked_bucket_folder_is_not_deleted(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "data.bin"
    victim.write_bytes(b"keep")
    (root / "b").symlink_to(outside, target_is_directory=True)
    _old_index(root, [("b_f1", "f1", root / "b" / "data.bin")])
    CacheManager(cache_dir=root, cache_ttl=timedelta(hours=1))
    assert victim.exists(), "a file reached through a linked folder is never deleted"


def test_a_save_killed_mid_write_leaves_the_previous_index_whole(cache, monkeypatch):
    import json
    cache.add_to_cache("b", "f1", Path(cache.cache_dir) / "missing")
    before = Path(cache.cache_metadata_file).read_text()

    def torn_dump(data, f, **kwargs):
        f.write("{")
        raise OSError("killed")

    monkeypatch.setattr(json, "dump", torn_dump)
    cache.add_to_cache("b", "f2", Path(cache.cache_dir) / "missing")
    assert Path(cache.cache_metadata_file).read_text() == before


def test_each_save_uses_its_own_temporary_file(cache, monkeypatch):
    import os
    real_replace, sources = os.replace, []
    monkeypatch.setattr(os, "replace", lambda src, dst: (sources.append(str(src)),
                                                         real_replace(src, dst)))
    cache._save_cache_metadata()
    cache._save_cache_metadata()
    assert len(sources) == 2 and len(set(sources)) == 2, sources
