"""Characterization tests for ``views_faoapi.wandb.utils.wandb_alert`` (register C-60).

Primary focus is the **security-relevant** behaviour: a server filesystem path
(`models_path`) embedded in the alert text is redacted before the alert leaves
for Weights & Biases. The gating (notifications flag / active run) and the
swallow-and-log error handling are pinned too, so a refactor that drops the
redaction, fires alerts when it should not, or lets a WandB error propagate is
caught by a failing test.
"""
import logging
from unittest import mock

import pytest
import wandb

from views_faoapi.wandb import utils as wandb_utils

pytestmark = pytest.mark.layer4_infra


def _active_run():
    """Patch the module's `wandb` so a run is 'active' and `alert` is a spy.

    Only `run` and `alert` are patched — `wandb.errors.*` and `wandb.AlertLevel`
    stay real so the production `except` clauses bind to genuine exception types.
    """
    return mock.patch.multiple(wandb_utils.wandb, run=mock.Mock(), alert=mock.Mock())


class TestRedaction:
    def test_models_path_is_redacted_from_alert_text(self):
        """A server path in the text must not reach `wandb.alert`."""
        path = "/srv/views/models/secret_model"
        with _active_run():
            wandb_utils.wandb_alert(
                title="run failed",
                text=f"crash in {path}/train.py at step 3",
                models_path=path,
            )
            sent_text = wandb_utils.wandb.alert.call_args.kwargs["text"]
        assert path not in sent_text
        assert "[REDACTED]" in sent_text

    def test_text_without_the_path_is_passed_through(self):
        with _active_run():
            wandb_utils.wandb_alert(title="ok", text="all nominal", models_path="/srv/x")
            sent_text = wandb_utils.wandb.alert.call_args.kwargs["text"]
        assert sent_text == "all nominal"

    def test_title_and_level_are_forwarded(self):
        with _active_run():
            wandb_utils.wandb_alert(
                title="hi", text="x", level=wandb.AlertLevel.WARN, models_path="/p"
            )
            kwargs = wandb_utils.wandb.alert.call_args.kwargs
        assert kwargs["title"] == "hi"
        assert kwargs["level"] == wandb.AlertLevel.WARN

    def test_models_path_none_redacts_literal_none_substring(self):
        """Characterization of a footgun: redaction is a plain ``str.replace`` of
        ``str(models_path)``. With ``models_path=None`` it replaces the literal
        substring ``"None"`` wherever it appears. Documents current behaviour
        (register C-60) — not an endorsement; a caller should pass a real path."""
        with _active_run():
            wandb_utils.wandb_alert(title="t", text="None shall pass", models_path=None)
            sent_text = wandb_utils.wandb.alert.call_args.kwargs["text"]
        assert sent_text == "[REDACTED] shall pass"


class TestGating:
    def test_no_alert_when_notifications_disabled(self):
        with _active_run():
            wandb_utils.wandb_alert(
                title="t", text="x", wandb_notifications=False, models_path="/p"
            )
            wandb_utils.wandb.alert.assert_not_called()

    def test_no_alert_when_no_active_run(self):
        with mock.patch.object(wandb_utils.wandb, "run", None), mock.patch.object(
            wandb_utils.wandb, "alert", mock.Mock()
        ):
            wandb_utils.wandb_alert(title="t", text="x", models_path="/p")
            wandb_utils.wandb.alert.assert_not_called()


class TestErrorsAreSwallowed:
    def test_comm_error_is_swallowed_and_logged(self, caplog):
        with mock.patch.object(wandb_utils.wandb, "run", mock.Mock()), mock.patch.object(
            wandb_utils.wandb, "alert", side_effect=wandb.errors.CommError("down")
        ):
            with caplog.at_level(logging.ERROR, logger="views_faoapi.wandb.utils"):
                wandb_utils.wandb_alert(title="t", text="x", models_path="/p")  # no raise
        assert any("Communication error" in r.message for r in caplog.records)

    def test_usage_error_is_swallowed_and_logged(self, caplog):
        with mock.patch.object(wandb_utils.wandb, "run", mock.Mock()), mock.patch.object(
            wandb_utils.wandb, "alert", side_effect=wandb.errors.UsageError("bad")
        ):
            with caplog.at_level(logging.ERROR, logger="views_faoapi.wandb.utils"):
                wandb_utils.wandb_alert(title="t", text="x", models_path="/p")
        assert any("Usage error" in r.message for r in caplog.records)

    def test_unexpected_error_is_swallowed(self):
        with mock.patch.object(wandb_utils.wandb, "run", mock.Mock()), mock.patch.object(
            wandb_utils.wandb, "alert", side_effect=RuntimeError("boom")
        ):
            # Must not raise.
            wandb_utils.wandb_alert(title="t", text="x", models_path="/p")
