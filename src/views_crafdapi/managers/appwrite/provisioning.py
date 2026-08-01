"""The provisioning gate — auto-provisioning is opt-in, default OFF (þing-01 #276 / PLATFORM-001 D5).

faoapi authored the create_* provisioning helpers the platform inherited. A missing or wrong
coordinate on a serving/read path must RAISE naming the resource, never silently create phantom
storage (the run-0-era failure class, S8). Provisioning runs only from a deliberate setup entrypoint
that sets CRAFDAPI_ALLOW_PROVISIONING=1. Extracted from the appwrite god-module (epic #325 S9).
"""

import os

_ALLOW_PROVISIONING_ENV = "CRAFDAPI_ALLOW_PROVISIONING"


class ProvisioningError(RuntimeError):
    """Base for provisioning-gate failures (þing-01 #276)."""


class ProvisioningDisabledError(ProvisioningError):
    """A create_* helper was reached while provisioning is opt-in-OFF (the default)."""


def _require_provisioning(what: str) -> None:
    """Fail loud unless provisioning is explicitly enabled. Guards every create_* leaf so a missing
    coordinate cannot auto-create storage on a serving path (þing-01 #276 / PLATFORM-001 D5)."""
    if os.getenv(_ALLOW_PROVISIONING_ENV, "").strip().lower() not in ("1", "true", "yes"):
        raise ProvisioningDisabledError(
            f"refusing to auto-provision {what}: provisioning is opt-in and OFF. A missing or wrong "
            f"coordinate on a serving/read path is a misconfiguration, not a signal to create "
            f"storage. Set {_ALLOW_PROVISIONING_ENV}=1 on a deliberate setup entrypoint to provision "
            f"(þing-01 #276 / PLATFORM-001 D5)."
        )
