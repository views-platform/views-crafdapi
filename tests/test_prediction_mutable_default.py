import inspect
import pytest
from views_faoapi.managers.prediction import PredictionStoreManager

pytestmark = pytest.mark.layer5_audit


class TestDownloadLatestFileMutableDefault:
    """Mutable default arguments share state between calls — filters must default to None."""

    def test_default_filters_is_none_not_mutable_dict(self):
        sig = inspect.signature(PredictionStoreManager.download_latest_file)
        default = sig.parameters["filters"].default
        assert default is None, (
            f"Default for 'filters' should be None, got {default!r}. "
            "Mutable default dict {{}} is a known Python anti-pattern."
        )
