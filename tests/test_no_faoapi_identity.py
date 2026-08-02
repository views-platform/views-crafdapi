"""Clone-identity guard: the shipped package carries no faoapi *identity*.

Epic #1 end-state #2: "no faoapi/un_fao/FAO literal leaks — a test proves it."
This is that test, scoped precisely.

views-crafdapi was cloned from views-faoapi. The **service/package/class
identity** must read crafd, never fao — a leaked identity token (`FaoApiClient`,
`views_faoapi`, a docstring calling this "the faoapi service") is simply wrong in
this repo and misleads every reader. This guard scans the shipped package (`src/`)
and fails loudly on any such token, so the clone can never silently drift back
toward its origin's name.

**Deliberately NOT banned here** (these are not identity — they are the *data* the
API currently serves, inherited verbatim and retargeted by later stories):

* Uppercase ``FAO`` naming the **partner / output schema / geography** (e.g. "the
  FAO output schema", "FAO Global Administrative Unit Layers / GAUL"). CRAF'd
  serves a *different* dataset; that schema is redesigned in **S8 (data contract)
  / S10 (data-shape)**, not renamed here — flipping "FAO"→"CRAF'd" now would
  assert a CRAF'd schema that does not yet exist.
* ``un_fao`` / ``unfao_*`` **bucket/document values** (e.g. ``unfao_bucket``,
  ``unfao_forecast_bulk.parquet``) — Appwrite coordinate *values*, retargeted in
  **S9 (console + registry)** once CRAF'd's bucket exists.

When S9/S10 land, tighten this guard to also forbid those.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.layer4_infra

SRC = Path(__file__).resolve().parent.parent / "src"

# Identity tokens that must never appear in the shipped package. Case-sensitive:
# lowercase ``faoapi`` also matches ``views_faoapi`` / ``views-faoapi``; the
# CamelCase/all-caps class + env stems (``FaoApi``, ``FAOApi``, ``FAOAPI``,
# ``FAODiskCache``) catch the renamed manager/client/cache classes and any
# reintroduced ``FAOAPI_*`` env var. Uppercase standalone ``FAO`` and
# ``unfao``/``un_fao`` are intentionally absent — see the module docstring.
_BANNED = re.compile(r"faoapi|FaoApi|FAOApi|FAOAPI|FAODiskCache")

# Scan every text file the package ships, not just .py — a src README carries
# identity too (an earlier .py-only guard let a `data/README.md` leak through).
_SCANNED_SUFFIXES = (".py", ".md")


def _src_modules() -> list[Path]:
    return sorted(p for p in SRC.rglob("*") if p.suffix in _SCANNED_SUFFIXES)


class TestNoFaoApiIdentity:

    def test_scan_is_not_vacuous(self):
        """Guard the guard: the scan must actually reach the shipped package."""
        modules = _src_modules()
        assert modules, f"no source modules found under {SRC} — the ban would pass vacuously"
        assert (SRC / "views_crafdapi" / "client.py") in modules

    def test_no_module_carries_faoapi_identity(self):
        """No shipped module names this service/package/class 'faoapi'."""
        violations: list[str] = []
        for path in _src_modules():
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if _BANNED.search(line):
                    violations.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")
        assert not violations, (
            "faoapi identity leaked into the shipped package — this is views-crafdapi:\n  "
            + "\n  ".join(violations)
        )
