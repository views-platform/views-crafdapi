# ADR-033: Fail-Visible Forecast Selection and Serving

**Status:** Active (ratified 2026-07-28, S7/#252 — against the shipped S1–S6 behaviour)
**Deciders:** Simon (maintainer)
**Records:** the decisions of Epic #244, from the 2026-07-27 expert-code-review of the run-0 non-serving incident (#243).

---

## Context

On 2026-07-27 the first global-land forecast ("run-0", `rusty_bucket`) was delivered to `unfao_bucket`. The **live API did not serve it** — it served a **139-day-old March test artifact** (`orange_ensemble`, *"This is a test DataFrame"*, 0 rows for current months) behind **HTTP 200**, undiagnosable from outside. An expert-code-review found the cause is architectural: the forecast selection/serving path **prefers a silent, healthy-looking wrong answer over a loud, correct failure.** Grounded findings:

- **C-170 (Tier 1)** — `dataset_service._load_wire_run` returns `None` on *any* ingest failure (`:540`), and the caller silently serves the legacy artifact (`:230`); "run present but unservable" is indistinguishable from "no run exists."
- **C-50 (reopened, Tier 1)** — the staleness check is log-only (`:166–174`); a 139-day artifact serves with green health.
- **C-71 (reopened, Tier 1)** — the promotion allowlist is optional/unset in prod; a *test* artifact is live.
- **C-86 (residual realized)** — live provenance is `source="unknown"`.
- **C-171 (Tier 2)** — deployed build (`v1.1.1`) lags the delivered dialect/schema; no capability gate.
- **C-169 (realized)** — contract mode ships forecast-only; global historical validated-but-not-uploaded.

This is a **UN-facing** endpoint, where silently serving stale/wrong/test data is the worst failure class. This ADR records the redesign. It is faoapi's consumer-side companion to **views-models ADR-017** (the maturity/delivery model) and **views-postprocessing ADR-013** (the wire contract), and an application of **ADR-028** (terminal-consumer boundary, trust-but-verify).

## Decision

**Principle: for the FAO forecast endpoints, serving stale/wrong/test data must be a *louder* failure than serving nothing.** The selection/serving path is redesigned so every request resolves to an explicit, typed decision whose reason is visible in `/health`, `/provenance`, logs, and an external alarm — never a silent fall-through.

### 1. Typed selection decision (foundation)

Selection returns a typed result — `Served(artifact, mode, freshness, provenance)` | `Refused(reason)` | `NoRun` — not `dataset | None`. The reason is a first-class value. Gates are a **composable sequence** (so a future value-**drift** gate — deliberately out of scope, see Consequences — slots in without rework).

### 2. Selection rule — serve the newest complete manifested run; make no eligibility judgment (D1, revised 2026-07-28)

faoapi serves the **newest _complete_ manifested run for its configured line**, and reads **no deployment/production label** — neither today's decorative `deployment_status` nor a re-derived ADR-017 `maturity`. Whether a run is *eligible* for production is decided **upstream, at the delivery boundary** (the producer/postprocessor decides what to deliver to the bucket); ADR-017 governs that when it lands, and "delivered to faoapi's bucket" is precisely ADR-017's *delivered* half. faoapi therefore never re-judges eligibility, and never needs rework when ADR-017 arrives.

This is deliberately **not** "serve anything." faoapi still **never silently falls back to the legacy artifact** — it fails visible instead (the C-170 fix) — and it verifies the *mechanical* facts it can (complete manifest, integrity hashes, renderable schema §7, identifiable source §3, freshness §4). What it drops is the *governance* judgment.

> **Unservable-run behaviour — decision (i), 2026-07-28 (S2).** When a manifested run is present but *unservable* (`Refused` — a failed integrity/capacity/parse check), faoapi fails visible (interim: a 5xx carrying the reason) and does **not** serve the stale legacy artifact. A persistently-unservable run is **re-evaluated and re-refused on each request** — the prior *legacy-fallback caching* is dropped (the file-byte cache avoids re-downloading its shards; only the assembly is re-attempted — a minor, self-limiting cost on a bad run only). Caching the refusal to skip re-ingest of a known-bad run was **considered and deferred to S4**, which rebuilds the fallback/last-good path. The FAO-facing consequence — repeated unavailability while a delivered run is unservable — is surfaced as one trigger of the degraded state in **Pre-Release Note 07** (Topic A); the eventual behaviour (503 vs last-good-flagged) is settled by D3/§5 on FAO's reply.

> **No-manifest behaviour — epic #263 S1 (#264), 2026-07-28.** The legacy forecast fallback is **retired**: a forecast is served **only** from a manifested run. When *no* manifest exists (`NoRun` / a manifest quarantined-to-nothing), faoapi serves the **last-good manifested run within the freshness SLA** (the §6 bounded, alarmed grace window, now extended from `Refused` to `NoRun`), else **fails visible (503)** — it never falls back to a loose legacy artifact. This closes the last seam where a stale/placeholder artifact could be served (the durable "no source-switch is ever a big deal" property, register **C-71**). The guard holds on the cache tiers too: a warm/disk *forecast* entry is served only if it is a WIRE entry (`_forecast_entry_servable`). `/historical` is unchanged — it still serves loose legacy files until the producer co-delivers historical on the wire path (epic #263 S5 / C-169).

faoapi **surfaces** the run's declared status/source/freshness in `/provenance` and a response flag (informational, not a gate), so an interim/shadow run — such as run-0 (`rusty_bucket`, a pre-production shakedown that retires at views-models #146) — is served *and clearly marked*, never masquerading as graduated production.

> **Supersedes the original D1** (a faoapi-side `maturity == graduate` gate). Withdrawn 2026-07-28: it re-introduced the eligibility judgment ADR-017 places *upstream*, and a faithful version would have **refused the run-0 shakedown** outright rather than unblocking it. faoapi builds on the delivery boundary, not on a label — which is what ADR-017 would tell it to do. (views-models seat note, 2026-07-28.)

### 3. Served runs must be identifiable (D2)

faoapi surfaces the served run's `source`/provenance, and refuses to serve a run it **cannot identify** (`source` absent or `"unknown"`) as `Refused(unidentifiable)` — an audit requirement (*which run, from which producer*), **not** an eligibility judgment. Manifested wire runs already carry their producing-ensemble provenance (run-0 carries `source = rusty_bucket`), so this does not block a real delivered run; it excludes only unattributable artifacts. The legacy `source="unknown"` artifact is already excluded by §2 (it is unmanifested).

### 4. Freshness SLA — a hard gate, not a log line

A configurable freshness threshold on the served artifact's age. Past it, `/health` goes **red** and the external monitor (ADR-032) **alarms** — not a `logger.warning`. Freshness is surfaced in `/provenance`.

### 5. Degraded-state behaviour (D3 — provisional, FAO to confirm)

When no current forecast is servable (nothing promoted per §2/§3, or the last-good is past the freshness SLA), the data endpoints return **HTTP 200 with an explicit `stale`/`unavailable` flag + machine-readable freshness metadata**, with `/health` red and an alarm; a **hard error (503)** is reserved for "no prior forecast at all." **This depends on FAO's consumption pattern and is surfaced to FAO as a decision point in Pre-Release Note 07;** it is locked on their reply. Touches the API surface — see ADR-026 amendment.

**Observability surface — delivered (S3/S4/S7).** The single decision (Rationale §"one decision, many surfaces") is exposed on two endpoints, independent of the still-pending §5 *data-endpoint* HTTP-status choice:
- **`GET /provenance/forecast`** reports the full **served** decision: `{artifact_id, mode ("wire"/"legacy"), status (producer-declared maturity), freshness (verdict vs the SLA), serving_state, refusal_reason?}`. It is sourced from the run actually being served (`DatasetService.served_forecast_provenance()` / `forecast_serving_state()`), which is authoritative — it may differ from the store's newest record; `refusal_reason` appears only while a bounded grace fallback (§6) is active.
- **`/health`** is `status:"degraded"` (HTTP 200 — *service* up, *data* not) when either the served forecast is past the freshness SLA (§4) **or** a grace fallback is active (§6, the newest run was refused); a WARNING is logged. `status:"degraded"` is the external monitor's page condition (ADR-032 §2a). HTTP 503 on `/health` remains reserved for Appwrite being unreachable (a genuine service outage). `GET /version` carries `served_contract_version` (§7).

### 6. Bounded, alarmed fallback (settles D-24)

Fallback to the last-good forecast is permitted **only within the freshness SLA and loudly** (health degraded + alarm); past the SLA it fails visible. This keeps the anti-500 goal (no fleet-wide 500 during a producer upload window) without unbounded silent fallback.

**Delivered (S4/#249).** When the newest run is `Refused` (§2), `DatasetService._serve_last_good_within_sla` reads the last-good **manifested** run persisted on disk (a failed ingest discards only its staging dir, §4.6, so the prior value-dir is intact) and serves it **only if** its `created_at` (from the manifest `$createdAt`, uploaded last per vpp ADR-013 §11.4) is positively within the freshness SLA — stale *or* unknown-age ⇒ fail visible (503). It never falls back to the legacy artifact (that was C-170). The fallback does **not** repopulate the warm cache, so every request re-evaluates the newest run (decision (i)) and recovery is immediate. A degraded serving-state (`{degraded, reason, fallback_available, file_id, age_days, sla_days}`) is surfaced on `/health` (flips `status:"degraded"`) and `/provenance` (`serving_state`), and a WARNING is logged — the alarm. The flag clears on the next normal serve (warm hit, disk hit, wire `Served`, or transition-mode legacy).

### 7. Deploy/serve capability gate (D4)

The run manifest **declares the contract/schema version it requires** (extending the existing `contract_version`); the consumer `Refused(schema_capability_mismatch)`s a run it can only render in a degraded/old schema rather than serving it degraded, and a startup/deploy assertion surfaces the built served-schema capability at `/version`. *(Recorded as the maintainer's recommendation for momentum; mechanical; cross-repo via vpp ADR-013.)*

**Delivered (S5/#250).** `forecast/contract.py` is the single source of the build's capability (`SERVED_CONTRACT_VERSION = "1.5"`) and the compatibility rule (`can_render_contract`): a stamped `contract_version` renders iff it shares the served MAJOR and its MINOR ≤ served MINOR; a newer minor / different major / unparseable version is refused. `_load_wire_run` applies the gate **right after parsing the manifest — before any shard fetch** — so an unrenderable run is `Refused("schema_capability_mismatch")` cheaply (→ 503 / S4 grace, never a degraded serve). `GET /version` exposes `served_contract_version` so deploy↔delivered skew is remotely diagnosable. **Transition-safe:** an *unstamped* (empty) version is surfaced-not-gated (the producer does not yet stamp it everywhere — cross-repo vpp ADR-013 / views-postprocessing#133); tighten to fail-closed once stamping is guaranteed.

### 8. The manifest-declared-metadata contract (cross-repo)

The run manifest (vpp ADR-013) should **declare `{status/maturity, source, required-schema-version}`**. faoapi's use of each differs by kind: it **surfaces** `status/maturity` (informational — §2, no gate), **verifies** that `source` is present (§3) and that the required schema is renderable (§7), and fails visible on the latter two. `status/maturity` is carried for provenance/audit and for ADR-017's **upstream** governance, **not** for a faoapi eligibility gate. A coordinated **vpp ADR-013 amendment** + producer stamping (views-postprocessing / views-models); faoapi consumes the declarations, it does not originate the eligibility decision.

## Rationale

- **Invert the default.** The current path optimises for uptime-looking-green; for a UN-facing forecast the correct optimisation is *never present wrong/stale/test data as current*. A visible refusal is actionable; a silent stale 200 deceives (it hid run-0 for 139 days).
- **Eligibility is upstream, not faoapi's.** ADR-017 makes "in production" a *derived* fact (graduate AND delivered); faoapi is the *delivered* half, so it serves what reaches its bucket and *surfaces* the run's declared status rather than re-judging it. Verifying mechanics while trusting the delivery boundary for eligibility is **more** ADR-028-faithful than a parallel faoapi gate — and it needs no rework when ADR-017 lands.
- **Trust, but verify, at the boundary that matters.** ADR-028 makes faoapi a checked consumer; §2–§7 are that principle applied to selection — the last wall before the UN.
- **One decision, many surfaces.** Computing the decision once and exposing its reason to `/health`, `/provenance`, logs, and alarms is what makes "why isn't run-0 served?" answerable in one read, not by log-archaeology.

## Consequences

**Positive:**
- No serving path returns stale/legacy/test data without a `/health`-red + logged + alarmed signal.
- The newest delivered run **is served** (run-0 included — the shakedown is unblocked), with its declared status/source/freshness **surfaced** so an interim/shadow run is never mistaken for graduated production.
- The endpoint's state is diagnosable from `/health` + `/provenance` alone.
- faoapi builds on ADR-017's *delivery* boundary, not a parallel label gate — it needs no rework when ADR-017 lands.

**Costs / accepted:**
- faoapi serves **whatever is delivered to its bucket, including an interim/shadow run** (run-0). This is deliberate (the shakedown), mitigated by the surfaced status flag, the interim caveat on the run-0 record, and ADR-017's *upstream* governance of what may reach the bucket. faoapi does **not** guard against an *ineligible run being delivered* — that is the delivery boundary's job (ADR-017); the honest risk is that a shadow forecast must not harden into "the UN's production forecast" without the graduate ensemble (#146).
- A cross-repo **vpp ADR-013 amendment** (manifest *declares* `{status/maturity, source, required-schema-version}`) + producer stamping (views-postprocessing / views-models).
- An **ADR-026 amendment** for the degraded-state HTTP semantics (§5).

**Explicitly out of scope:** value-**drift** detection (served numbers diverge unexpectedly vs baseline) — a *separate* fail-loud gate, a known future sibling; the §1 gate framework must leave room for it. Nearest existing pieces: C-72 (value plausibility), ADR-023 (re-baseline diff).

**Register (reconciled at ratification, S7/#252):** **C-170** RESOLVED (silent fail-open replaced by fail-visible S2 + freshness S3 + bounded-alarmed fallback S4; monitor rule ADR-032 §2a + deploy are the maintainer-owned externals), **C-50** RESOLVED (freshness is a hard `/health`-degrading gate, not a log line), **C-171** RESOLVED (capability gate + `/version`, S5), **D-24** SETTLED (bounded-alarmed fallback, S4). **C-71** updated — the fix is manifest-first serving + no-legacy-fallback + surfaced status, *not* a faoapi maturity/promotion gate (eligibility is upstream, ADR-017). **C-86** updated — identifiable-source is now *required* to serve (§3) and the served lineage is surfaced at `/provenance`. **C-169** — co-delivery of global historical is cross-repo (views-postprocessing #133 / the `land_gaul` scope flip), tracked there.

## References

Epic #244, tracking #253; incident #243; expert-code-review 2026-07-27; **views-models ADR-017** (maturity/delivery); **views-postprocessing ADR-013** (wire contract); ADR-028 (boundary), ADR-026 (API surface), ADR-032 (monitoring), ADR-023 (re-baseline); C-50/71/86/169/170/171, D-24; Pre-Release Note 07 (D3, FAO-facing).
