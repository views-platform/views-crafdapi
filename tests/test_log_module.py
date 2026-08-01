"""Characterization tests for ``views_crafdapi.managers.log.LoggingModule`` (register C-60).

Pins the observable behaviour of the logging setup: the log directory is created,
`get_logger` returns and caches a configured root logger, the YAML config loads,
and the `{LOG_PATH}` placeholder in file handlers is substituted with the
configured path. A restore fixture keeps the global root-logger state from
leaking into the rest of the suite (the module reconfigures the root logger).
"""
import logging

import pytest

from views_crafdapi.managers.log import LoggingModule

pytestmark = pytest.mark.layer4_infra


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """Snapshot and restore the root logger so `dictConfig` side effects in these
    tests (added file handlers under tmp_path, level changes) do not leak."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    for handler in root.handlers[:]:
        if handler not in saved_handlers:
            try:
                handler.close()
            except Exception:
                pass
            root.removeHandler(handler)
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


class TestInit:
    def test_creates_logging_directory(self, tmp_path):
        target = tmp_path / "logs" / "nested"
        LoggingModule(target)
        assert target.is_dir()

    def test_default_attributes(self, tmp_path):
        m = LoggingModule(tmp_path)
        assert m._default_level == logging.INFO
        assert m._logging_is_active is True
        assert m._logger is None


class TestGetLogger:
    def test_returns_a_logger(self, tmp_path):
        assert isinstance(LoggingModule(tmp_path).get_logger(), logging.Logger)

    def test_logger_is_cached(self, tmp_path):
        m = LoggingModule(tmp_path)
        assert m.get_logger() is m.get_logger()

    def test_inactive_setup_returns_none(self, tmp_path):
        m = LoggingModule(tmp_path)
        m._logging_is_active = False
        assert m._setup_logging() is None


class TestConfig:
    def test_load_logging_config_returns_dict(self, tmp_path):
        config = LoggingModule(tmp_path)._load_logging_config()
        assert isinstance(config, dict)
        assert config.get("version") == 1

    def test_log_path_is_substituted_into_file_handlers(self, tmp_path):
        """`get_logger` runs `dictConfig` with `{LOG_PATH}` -> the configured
        path, so emitting a record creates a `views_pipeline_*.log` under it."""
        LoggingModule(tmp_path).get_logger()
        logging.getLogger().info("characterization probe")
        assert any(tmp_path.glob("views_pipeline_*.log"))

    def test_ensure_log_directory_creates_nested_dir(self, tmp_path):
        m = LoggingModule(tmp_path)
        target = tmp_path / "a" / "b" / "out.log"
        m._ensure_log_directory(str(target))
        assert target.parent.is_dir()
