"""#60: which source answers "what is the live forecast?", and when a 404 is honest.

Unit tests over `forecast.provenance` — no HTTP, no store. The endpoint-level regression that
reproduces the reported bug lives in `tests/test_api_endpoints.py`.

**Every store-side fixture is built through `PredictionProvenance.to_dict()`**, not hand-rolled.
The module's contract is that `manifest_record`/`stored_record` arrive in that shape; a
hand-written dict lets the real shape gain or lose a key without a single test noticing — which
is how the `methodology_version` drop below went unnoticed in the first version of this file.
"""
import pytest

from views_crafdapi.forecast import provenance
from views_crafdapi.managers.prediction import PredictionProvenance

pytestmark = pytest.mark.layer2_data


RUN_ID = "rusty_bucket_forecasting_20260727_095355"
MANIFEST_FILE_ID = "6a7f5fda000d0f4e1c22"
TARGETS = ["lr_ged_sb", "lr_ged_ns", "lr_ged_os"]


def _legacy() -> dict:
    return PredictionProvenance(
        file_id="legacy_001",
        source="orange_ensemble",
        created_at="2026-01-01T00:00:00.000Z",
        filename="forecast_dataset_20260101.parquet",
        name="orange_ensemble",
        category="forecast",
        targets=["pred_ln_ged_sb"],
        description="a superseded legacy artifact",
        file_hash="cafe",
    ).to_dict()


def _manifest() -> dict:
    """As `PredictionStoreManager._provenance_from` really builds it from a manifest document.

    Note `source="unknown"`: no producer stamps `source`/`pipeline` on a manifest, so this is
    the value the real path produces. Asserting anything else here would be fiction.
    """
    return PredictionProvenance(
        file_id=MANIFEST_FILE_ID,
        source="unknown",
        created_at="2026-08-14T18:35:54.962+00:00",
        filename=f"{RUN_ID}__manifest.json",
        name="un_crafd",
        category="forecast",
        targets=TARGETS,
        file_hash="beef",
    ).to_dict()


def _served_wire(**overrides) -> dict:
    served = {
        "file_id": MANIFEST_FILE_ID,
        "mode": "wire",
        "status": "graduate",
        "source": "rusty_bucket",
        "run_id": RUN_ID,
        "created_at": "2026-08-14T18:35:54.962+00:00",
        "targets": TARGETS,
    }
    served.update(overrides)
    return served


class TestWhenAForecastExists:
    """The reported bug: every one of these returned nothing before #60."""

    def test_a_manifested_run_is_reported_with_no_legacy_record(self):
        data = provenance.forecast_record(
            served=None, manifest_record=_manifest(), stored_record=None
        )
        assert data is not None
        assert data["file_id"] == MANIFEST_FILE_ID

    def test_the_manifest_only_answer_is_honest_about_what_it_cannot_know(self):
        """`source` is unattributable from a manifest — no producer stamps it."""
        data = provenance.forecast_record(
            served=None, manifest_record=_manifest(), stored_record=None
        )
        assert data["source"] == "unknown"
        assert data["mode"] is None

    def test_a_served_run_is_reported_on_a_worker_with_no_store_record(self):
        data = provenance.forecast_record(
            served=_served_wire(), manifest_record=None, stored_record=None
        )
        assert data["mode"] == "wire"
        assert data["file_id"] == MANIFEST_FILE_ID
        # Known gap, not #60: with no store record behind it this branch carries only what the
        # served run supplies, so contract keys `methodology_version` and `category` are absent.
        # Reachable only in the ADR-033 §6 grace fallback with the manifest gone. Tracked.


class TestPrecedence:
    def test_the_manifest_outranks_a_legacy_record(self):
        data = provenance.forecast_record(
            served=None, manifest_record=_manifest(), stored_record=_legacy()
        )
        assert data["file_id"] == MANIFEST_FILE_ID
        assert data["source"] != "orange_ensemble"

    def test_the_served_run_owns_a_record_describing_a_DIFFERENT_artifact(self):
        """#290, preserved: no field of a superseded artifact may bleed through."""
        data = provenance.forecast_record(
            served=_served_wire(), manifest_record=None, stored_record=_legacy()
        )
        assert data["file_id"] == MANIFEST_FILE_ID
        assert data["source"] == "rusty_bucket"
        assert data["targets"] == TARGETS
        assert data["file_hash"] is None, "a wire run is many hashed shards, not one file"
        assert "orange_ensemble" not in str(list(data.values()))

    def test_a_legacy_only_bucket_still_reports_legacy(self):
        data = provenance.forecast_record(
            served=None, manifest_record=None, stored_record=_legacy()
        )
        assert data["file_id"] == "legacy_001"
        assert data["mode"] is None
        assert data["artifact_id"] == "legacy_001"


@pytest.mark.xfail(
    reason="Known defect, not #60: when the base record is this run's OWN manifest rather "
           "than a superseded artifact, #290's full reconcile overwrites the manifest's "
           "correct targets/file_hash/filename. #290 predates the manifest ever being a "
           "possible base. Tracked separately; deliberately out of scope here.",
    strict=True,
)
class TestTheBaseIsThisRunsOwnManifest:
    def test_the_manifests_targets_survive_a_served_record_that_lacks_them(self):
        served = _served_wire()
        del served["targets"]
        data = provenance.forecast_record(
            served=served, manifest_record=_manifest(), stored_record=None
        )
        assert data["targets"] == TARGETS


class TestWhenNothingExists:
    def test_all_three_absent_is_the_only_honest_404(self):
        assert (
            provenance.forecast_record(
                served=None, manifest_record=None, stored_record=None
            )
            is None
        )

    def test_an_empty_mapping_is_not_a_record(self):
        """A bare MagicMock's to_dict() collapses to {}; it must not suppress the 404."""
        assert (
            provenance.forecast_record(
                served=None, manifest_record={}, stored_record={}
            )
            is None
        )


class TestEmptyValuesDoNotClobber:
    def test_an_empty_served_timestamp_does_not_drive_the_freshness_verdict(self):
        """`freshness.forecast_freshness` must never be handed "" — its policy is an unknown
        verdict, not an asserted-stale one, on a signal it cannot compute."""
        served = _served_wire(created_at="")
        data = provenance.forecast_record(
            served=served, manifest_record=_manifest(), stored_record=None
        )
        from views_crafdapi.managers import freshness

        verdict = freshness.forecast_freshness(
            provenance.freshness_input(served, data), sla_days=45.0
        )
        assert verdict["is_stale"] is None, "unknown, never asserted-stale"

    @pytest.mark.xfail(
        reason="Pre-existing #290 behaviour, not introduced by #60: `served.get(\"source\", "
               "default)` uses a dict default, which does not fire when the key is present "
               "and None, so an explicitly-null served source wipes the store's. Tracked.",
        strict=True,
    )
    def test_an_explicitly_null_served_source_does_not_wipe_the_stores(self):
        data = provenance.forecast_record(
            served=_served_wire(source=None),
            manifest_record=_manifest(),
            stored_record=_legacy(),
        )
        assert data["source"] == "unknown", "the manifest's value, not None"


class TestTheQueryThatIsTheFixExecutesForReal:
    """#60's mechanism is `get_latest_manifest_provenance()` naming `type` EXPLICITLY.

    Every other test stubs that method, so reverting its body to the type-less query — the
    exact #60 root cause — would leave the suite green. These drive the real method against a
    fake store and assert on the filters it actually emits.
    """

    @staticmethod
    def _manager_with(captured, documents):
        from unittest.mock import MagicMock

        from views_crafdapi.managers.prediction.manager import PredictionStoreManager

        mgr = PredictionStoreManager.__new__(PredictionStoreManager)  # no Appwrite
        mgr.model_path = MagicMock(model_name="un_crafd")

        def fake_query(filters=None):
            captured.append(dict(filters or {}))
            return documents

        mgr.get_predictions_by_metadata = fake_query
        return mgr

    def test_it_asks_for_the_manifest_type_not_the_legacy_type(self):
        captured = []
        doc = {
            "fileId": MANIFEST_FILE_ID,
            "filename": f"{RUN_ID}__manifest.json",
            "$createdAt": "2026-08-14T18:35:54.962+00:00",
            "name": "un_crafd", "category": "forecast", "targets": TARGETS,
        }
        mgr = self._manager_with(captured, [doc])

        record = mgr.get_latest_manifest_provenance()

        assert captured, "the store was never queried"
        assert captured[-1]["type"] == "sampled_forecast_manifest", (
            "a type-less query is pinned to the legacy type by the ADR-013 §11.4 guard and "
            "matches nothing on a greenfield wire bucket — that IS #60"
        )
        assert captured[-1]["type"] != "model"
        assert record is not None
        assert record.file_id == MANIFEST_FILE_ID
        assert record.source == "unknown"  # no producer stamps source on a manifest

    def test_it_returns_none_when_no_manifested_run_exists(self):
        mgr = self._manager_with([], [])
        assert mgr.get_latest_manifest_provenance() is None
