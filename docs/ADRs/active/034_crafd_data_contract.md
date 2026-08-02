# ADR-034 — The CRAF'd data contract (targets, geography, summary statistics, bucket)

**Status:** Proposed (2026-08-02) — awaiting CRAF'd/product sign-off on the target list.
**Deciders:** operator (Simon) + CRAF'd/product. Drafted with the design authority.
**Story:** epic #1, S8 (#9). **Gates:** S10 (data-shape build) and the views-postprocessing CRAF'd producer.

---

## Context

`views-crafdapi` is a read-only clone of `views-faoapi` (ADR-031, epic #1). Band A (S3–S7)
retargeted identity/coordinates/branding/deploy but **not** the served *data* — that is this
contract's job. CRAF'd (the Complex Risk Analytics Fund) is a second external partner. Its
dataset is **an extension of FAO's, not a different quantity**:

- **Same** geographic scope, country set, and cadence as FAO.
- **Same** base series — `sb`/`ns`/`os` GED conflict-fatality forecasts.
- **Same** posterior-sample delivery over the Sampled-Forecast Wire Contract (ADR-013).
- **Plus** additional targets (TBD — see §2), and
- **Plus** a new summary statistic: **threshold exceedance probabilities** `P(Y > c)`,
  alongside the FAO HDI/MAP surface.

The FAO product declaration this mirrors is `views-postprocessing/unfao/product.py`
(`TARGETS=("lr_ged_sb","lr_ged_ns","lr_ged_os")`, `loa="pgm"`, `CONSUMER_DOCUMENT_NAME="un_fao"`).
**No CRAF'd producer exists yet** (`views-postprocessing/crafd/` is absent), so this contract is
the specification the future producer's `product.py` must implement — a co-design, not a read.

---

## Decision

### 1. Base targets — CONFIRMED

`sb`, `ns`, `os` (state-based / non-state / one-sided GED conflict fatalities), wire vocabulary
`lr_ged_sb` / `lr_ged_ns` / `lr_ged_os`, served as **raw counts** (ADR-024), consumer names
prefix-free (ADR-025 §3). Inherited from FAO unchanged.

### 2. Additional targets — PLACEHOLDER, pending CRAF'd

CRAF'd wants more targets than sb/ns/os; the exact list is **not yet available from the
partner**. The contract is structured to receive them without a redesign:

```
ADDITIONAL_TARGETS: tuple[str, ...] = ()   # TODO(CRAFD): awaiting the partner target list
```

The serving path already flows any declared target to its columns generically (ADR-025 schema
module). Adding a target when CRAF'd delivers the list is: name it here (wire vocabulary), add a
producer mapping entry, regenerate goldens. **No placeholder is invented as a real name** — an
empty tuple that fails no test is the honest representation until the spec lands.

### 3. Summary statistics

| Statistic | Masses / thresholds | Source | Status |
|---|---|---|---|
| **HDI** (highest-density intervals) | 50 / 90 / 95 | `views_frames_summarize` (collapse), as FAO | inherited (FAO signed-LoA levels) |
| **MAP** (point estimate) | — | as FAO | inherited |
| **Exceedance** `P(Y > c)` | **c ∈ {25, 100, 1000}** | `views_frames_summarize.exceedance_reducer` | **new** |

**Exceedance is wiring, not building.** `views_frames_summarize.exceedance` / `exceedance_reducer`
are public, `collapse`-compatible (ADR-021 in views-frames), so they slot into the same reduction
faoapi already runs for HDI/MAP. `P(Y > c)` is the empirical survival fraction over the posterior
samples, strict `>`, in fatality units, fail-loud on non-finite draws.

The three thresholds `25 / 100 / 1000` are **reasonable placeholders** chosen by the operator
(2026-08-02); real CRAF'd numbers replace them when available.

> **Note — onset.** views-frames' flagship exceedance is `P(Y > 0)` (probability of any
> activity). It is deliberately **not** in the initial set (operator chose the three severity
> thresholds), but it is a one-entry addition under §5 if CRAF'd wants it.

### 4. Geography / entity model — CONFIRMED same as FAO

PRIO-GRID cell × month (`loa=pgm`) base grain, aggregated (conservation-correct joint sum,
ADR-014) to GAUL admin-1 / admin-0 + ISO3. Endpoint level vocabulary `pg` / `country` /
`gaul0` / `gaul1` / `gaul2`, same reference shapefiles. Unchanged from FAO.

### 5. Configurability discipline — the served contract is one declared constant

Both the credible **masses** (§3) and the exceedance **thresholds** (§3) are single declared
tuples (`MASSES`, `EXCEEDANCE_THRESHOLDS`) from which **every** column name, golden fixture, and
the producer's `product.py` derive. Changing a threshold is therefore *easy* — one reviewed,
git-historied line that auto-propagates — **and** *safe*: it is a deliberate contract amendment,
not a silent runtime tweak.

**Explicitly rejected:** a live environment override (`CRAFDAPI_EXCEEDANCE_THRESHOLDS`) for the
served set. A served contract that changes with the environment is the silent-corruption class
ADR-025 exists to prevent — two deploys could serve differently-named columns under the same tag.
"Easily configurable" is satisfied by the one-line-amendment property, not by runtime mutability.

### 6. `CONSUMER_DOCUMENT_NAME` — `un_crafd`

The store-document `name` the API filters on unconditionally (ADR-013 §4.1a; a document under
any other name is invisible to the consumer — a silent-mismatch hazard). Set to **`un_crafd`**,
matching the model name (S4). The producer's `product.py` MUST stamp the same value.

### 7. Bucket — its own new bucket

CRAF'd reads a **dedicated new Appwrite bucket** (per-consumer isolation, seam contract §5.8),
**not** a shared container. S9 (operator) creates the bucket + collection and fills the reserved
registry slots `APPWRITE_CRAFD_BUCKET_ID/NAME`, `APPWRITE_CRAFD_COLLECTION_ID/NAME` (currently
`status="planned"` in `views-appwrite` `coordinate_registry.toml`). The reserved slots are
therefore **kept**, not deleted.

---

## Served column plan (derived; S10 builds it)

Per served row, in ADR-025 §4 order:

1. **Identity** (6): `month_id`, `admin1_code`, `admin1_name`, `country_code`, `country_name`,
   `country_iso3` — unchanged from FAO.
2. **Per series** `∈ {sb, ns, os} ∪ ADDITIONAL_TARGETS`:
   - MAP: `{series}_map`
   - HDI: `{series}_hdi{50,90,95}_{lower,upper}`
   - **Exceedance (new):** `{series}_p_gt{25,100,1000}` = `P(series > c)`
3. Column names derive entirely from `MASSES`, `EXCEEDANCE_THRESHOLDS`, and the target set — no
   name is spelled twice (Common Closure, as ADR-025).

Golden outputs are regenerated in S10 against CRAF'd fixtures once the producer emits them.

---

## Consequences

- **S10 (data-shape)** is unblocked on everything except the target list (§2): geography, summary
  statistics, and the column-naming scheme are all specified now. The exceedance columns can be
  built and golden-tested against sb/ns/os immediately; additional-target columns append when §2
  resolves.
- **S9 (console + registry)** is unblocked: bucket decision made (§7), doc name fixed (§6).
- **The views-postprocessing CRAF'd producer** (separate track) must implement this contract:
  `TARGETS` ⊇ {sb,ns,os} + additional, `EXCEEDANCE_THRESHOLDS`, `MASSES`,
  `CONSUMER_DOCUMENT_NAME="un_crafd"`, uploading to the new bucket. It does not exist yet.
- **Ratification:** this ADR flips Proposed → Active when CRAF'd confirms (a) the additional
  target list and (b) the exceedance thresholds. Until then S10 builds against the confirmed
  parts and the placeholders.

## Open items (owned by operator + CRAF'd/product)

- [ ] CRAF'd's additional target list (§2) — the one true blocker on full S10.
- [ ] Confirm exceedance thresholds `25/100/1000` (or replace) (§3).
- [ ] Confirm HDI masses `50/90/95` carry over, or CRAF'd wants different (§3).
- [ ] Confirm `P(Y>0)` onset inclusion (§3 note).
