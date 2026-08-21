# Declared done, not in effect — eight instances, one shape

*Written 2026-08-21 after a day that produced four fresh instances in about six hours. Every fact below is read from a journal, a `systemctl` output, an HTTP response, a commit
or an issue — not from memory. Governed by ADR-010; the live entry is **C-282**, which records
one instance; this report is the argument that the shape is worth more than the instance.*

---

## What the day was

Context, so this reads without the conversation that produced it.

`views-crafdapi` serves CRAF'd conflict forecasts from a Hetzner box it shares with
`views-faoapi`. That box has **22 GiB and no swap**, and it OOM-killed three times in August,
because crafdapi's first request after a restart peaked at **16.8 G** while loading a 28.4M-row
historical artifact. With faoapi resident at ~5 G there was no memory ceiling that both survived a
restart and left the neighbour room — issue **#98** (register **C-263**), which blocked **#99**
(**C-262**, the ceilings themselves).

On 2026-08-21 three things happened in sequence: **v0.5.1** shipped a streamed historical ingest
that took the cold start to **7.3 G**; the measurement proving it was taken on the box; and the
memory ceilings that measurement unblocked were sized. Each step surfaced at least one instance of
the shape below. None caused an outage. The last one found would have caused the exact outage the
work was preventing.

## Timeline

All 2026-08-21 UTC unless stated. Instance letters refer to the table below.

| when | what |
|---|---|
| 2026-08-09 | views-faoapi#368 closed with its operator install step undone — **B** begins, unnoticed |
| 2026-08-10 | repo-assimilation writes C-238's cause from reading code — **D** begins |
| 2026-08-18 | C-262 records the installed faoapi unit as having no `MemoryMax`. Nobody connects it to #368. A falsification audit finds **F** |
| ~10:02 | a restart is issued without writing the deploy-tag file. A 502 inside the ~3 s restart window is read as a live outage — wrongly |
| 10:06:23 | `deploy-gate: serving tag v0.4.0`. `GET /version` returns `0.4.0` — **A** |
| 10:06:24 | the same journal read shows three config-AST ERRORs — **D**; and the C-237 shutdown traceback, predicted 11 days earlier |
| midday | PR review of the streamed ingest constructs an adversarial input — **G**. A `[Service]`/`[Unit]` placement bug is caught pre-merge — **E** |
| ~15:00 | v0.5.1 released, tagged, deployed. `/version` = `0.5.1`, all checks green |
| ~19:00 | the ceiling work begins. `systemctl status views-faoapi` has no `max:` field — **B**, 12 days later |
| 19:55 | historical cache cleared, service restarted, one bulk request: **peak 7.3 G**, and the journal shows the streamed path executing for the first time — **C** |
| ~21:00 | `free -g` returns 22 GiB, not the 24 GB both ceilings were sized from — **H** |

## The shape

> **An artifact asserts a state. The system is in a different state. The assertion is the only
> thing anyone checks.**

Not a bug class — a *verification* class. In every case below the code was correct, the change
was correct, and the record of it was correct in isolation. What was missing was any comparison
between the record and the running system.

## The instances

| # | What was declared | What was true | For how long | What caught it |
|---|---|---|---|---|
| **A** | `v0.5.0` tagged, pushed, in the release ledger | The deploy-tag file still named `v0.4.0`; a restart re-deployed v0.4.0 | ~2 h | `GET /version` |
| **B** | `views-faoapi` caps `MemoryHigh=9G`/`MemoryMax=11G` (unit file, issue #368 **closed** 2026-08-09) | No cgroup ceiling in effect on the box | **12 days** | absence of a `max:` field in `systemctl status`, noticed while looking for something else |
| **C** | v0.5.1 deployed, `/version` = `0.5.1`, all checks green | `historical_stream.stream_to_value` had **never executed**; the disk cache served the v0.4.0-era value-dir | ~40 min (would have been up to 3.5 weeks) | clearing the cache and grepping the journal for the ingest line |
| **D** | C-238: the config scripts "fail to load because `apis/un_crafd/` is absent" | They are **found and rejected** by `_validate_config_ast` | **11 days** | three ERROR lines in the journal, read while investigating something else |
| **E** | `StartLimitIntervalSec`/`StartLimitBurst` present in the unit file | systemd ≥229 **silently ignores** them under `[Service]` | caught pre-merge | re-reading my own diff, then a test that parses sections rather than text |
| **F** | C-266: "One variable, changed once, is why it is written this way" | The trap was reduced, not removed — `TAG=v0.4.0` still pasted cleanly | 2 releases | a falsification audit |
| **G** | Byte-identity on the real 28.4M-row artifact, served-output equality on 1.55M rows, **8 mutation tests all green** | The row-ordering guard had a hole; the real artifact's global sortedness made it unreachable | through review | `/code-review medium`, constructing an input that violated the precondition across a row-group boundary |
| **H** | views-faoapi#368: a "**24 GB** Hetzner box", and both services' ceilings sized from it (`11 GB each`) | `free -g`: **22 GiB, zero swap**. `11 + 11` is the entire machine | **12 days**, and it set both ceilings | `free -g`, one line |

## Four ways it happens

The eight are not one mechanism. Sorting them is what makes the remedies different.

**1. An action recorded as done was not done.** (A, B) A tag exists and the deploy-tag file was
never written; a unit file carries a cap and the box was never reloaded. In both cases the *code*
half completed and the *activation* half did not, and the record covers only the first.

**2. A fact recorded as established was never checked against the system.** (D, H) C-238 asserted
a cause from reading code; the journal said otherwise. #368 asserted a box size; `free` said
otherwise. **H is the costliest**, because the wrong fact was load-bearing: both services' ceilings
were derived from it, and the pair it produced would have caused precisely the OOM the ceilings
exist to prevent.

H also has a name worth using: **GB versus GiB**. The plan is sold as 24 GB, the hostname says
`ubuntu-24gb-…`, and 24 × 10⁹ bytes is 22.35 GiB, of which ~22 is usable. systemd's `11G` means
11 **GiB**. Nobody was careless; two units were silently mixed, in a place where the arithmetic
had to be exact.

**3. A property assumed to follow from another.** (C, E) *Deployed* was taken to imply *running* —
it did not, because the disk cache was still valid. *Present in the unit file* was taken to imply
*in effect* — it would not have been, under `[Service]`. Nobody declared these; they were
inferred, which is why nothing was written down to be wrong.

**4. A verification believed to cover what it did not.** (F, G) A runbook fix that reduced a trap
and claimed to have removed it. A test suite green on inputs that could not reach the defect.

## What actually caught them

Two families, and neither is "read the artifact".

**Read runtime state** — A, B, C, D, H. `/version`, `systemctl status`, the journal, `free -g`. In
every case the system was willing to say what was true; nobody had asked. Four of the five took a
single command.

**Attack the claim** — E, F, G. A test that parses a file the way its consumer does; a
falsification audit; an adversarial input. Not "does this look right" but "construct the case
where it isn't".

**Reading the artifact caught nothing.** Not once in eight.

## The part that should be uncomfortable

Four of the eight — B, C, D, H — were found **while investigating something else.** That is not a
detection method; it is luck, and B and H each ran 12 days on it. H is the one that should worry
us most: it was found because a merged ceiling looked suspicious, not because anything checks the
number a ceiling is derived from.

Worse for B: this repository had **already written the fact down.** C-262, dated 2026-08-18,
records the installed faoapi unit as having no `MemoryMax=` — nine days after the repo acquired
one. Both halves were in writing, three days apart, and nobody put them together, because nothing
compares the two and no one thought to.

And G is the sharpest, because it is the *verification* failing rather than the code. A
byte-identity proof over the production artifact, four served-output comparisons, and eight
mutation tests each confirmed to fail when its guard was removed — all green, with a real hole in
the guard. They were green because the real artifact satisfied the precondition, so the defect was
unreachable through any of them. **A guard tested only on inputs that satisfy its precondition
tests the happy path of the guard, not the guard.**

## Why the existing defences did not fire

They are good defences aimed one step short.

- **The deploy gate** refuses to serve a tag that does not resolve. It cannot know the tag file was
  never updated — serving v0.4.0 correctly *is* its job (A).
- **CI** proves the committed artifact is correct. It has no view of what is installed (B).
- **`/version`** reports the deployed tag faithfully, and did. There is no equivalent surface for
  *which ingest path last ran* (C).
- **The register** is written from code reading. C-238's conclusion was right and its stated cause
  was wrong, and nothing re-checks a written cause against a running system (D).
- **The test suite** asserts what the author thought to assert (E, G).

None of them is wrong. The gap is between them: **no defence compares the declared state to the
running state**, and that is exactly where all seven live.

## What to do

Concrete, cheap, and in rough order of value.

1. **Verify activation from runtime state, never from the file.** After installing a unit,
   `systemctl show <unit> -p <Property>`. After a deploy, `GET /version`. After an ingest change,
   the journal line naming the path. The runbook now says this for the deploy; it should say it
   for every step that has an activation.
2. **Do not close an issue whose acceptance includes an operator action until there is evidence of
   the action.** #368 was closed with its code half done and said so in its own text. A closed
   issue is itself a declaration, and it was wrong for 12 days.
3. **Give the ingest path a runtime surface.** C-282's residual is that nothing reports which path
   last ran; the journal line exists but only if you know to grep for it. This is the one item here
   that is a code change rather than a habit.
4. **Treat a green suite as evidence about its inputs.** Before trusting "byte-identical on the
   real artifact", ask which properties of *that* artifact the proof relied on, and construct an
   input that violates them. That is C-279.
5. **When a fix reduces a trap without removing it, say so.** C-266's runbook text claimed the
   problem solved. The honest form is "reduced, and here is what remains".
6. **Check the number a derived value is derived from.** H is the whole argument for this: two
   ceilings, on two production services, computed from a box size nobody had run `free` against.
   When a value is arithmetic on a constant, the constant is the thing to verify — and where the
   constant carries a unit, verify the unit too.

## A neighbouring family, not counted here

C-281 tracks a different shape that showed up four times in the same twelve days: **a document
that reads correctly and misleads in use** — a runbook step stating a pre-delivery failure state
as current, a copy-paste block naming a real-but-stale tag, a block naming which user and not
which host, and a pasted block corrupting its own input. A fifth arrived while this report was
being written: a `free -g` in a code block, sent to an operator, that failed as
`Command ' free' not found` because the block carried a non-breaking space.

Related but distinct. C-281's instances are wrong *at the point of use*; these eight are wrong
about *the state of the system*. Both are the record disagreeing with reality; only these eight
are invisible until someone looks at the running system. Kept separate so neither remedy is
diluted into the other.

## What this report does not claim

- It does not claim any of the seven caused an outage. None did. B is the only one with real
  exposure — an unbounded service on a box that has OOM-killed three times — and even there the
  harm is hypothetical.
- It does not claim the defences should be replaced. They should be *joined*.
- Instance E was caught before merge and is included because it is the same shape, not because it
  escaped.
- Instance H spans two repositories, and this report is written from one of them. views-faoapi may
  have context that changes it.
- Eight instances from one repository over twelve days is not a base rate. It is enough to justify
  the habit changes above and not enough to justify machinery.

## Where it stands, 2026-08-22

| | state |
|---|---|
| **views-crafdapi** | v0.5.1 serving. Cold start **7.3 G**, down from 16.8 G, output byte-identical. |
| crafd ceiling | `MemoryHigh=8G` / `MemoryMax=9G` **committed, not installed**. A temporary `MemoryMax=14G` set via `set-property --runtime` during the measurement still overrides it. |
| **views-faoapi** | **No ceiling in effect.** Its unit has declared `MemoryMax=11G` since 2026-08-09; the box has never been reloaded. Filed as views-faoapi#432. |
| faoapi cold start | **Never measured.** Its 4.8 G is a resident figure — the same kind of number crafd's 3.7 G steady state is, and crafd's cold start was 2× that. |
| #98 / C-263 | closed / resolved |
| #99 / C-262 | open, half-satisfied — crafd's half sized from a measurement, faoapi's neither measured nor active |

The honest summary of the position: **the service that can still exhaust the box is the one with
no ceiling, and its ceiling is the one nobody has measured.** That is instance B and instance H
sitting next to each other, and it is the single most useful thing this report has to say.

## Evidence

The preamble claims every fact here is read rather than remembered. These are the reads, so the
instances can be checked rather than taken on trust.

**A** — after a restart that did not write the deploy-tag file:
```
$ curl -s https://crafdapi.viewsforecasting.org/version
{"version":"0.4.0","deployed_tag":"v0.4.0","served_contract_version":"1.5"}
$ sudo journalctl -u views-crafdapi
10:06:23  deploy-gate: serving tag v0.4.0 (v0.4.0, 1fb30b6)
```

**B** — the two services side by side. faoapi has no `max:` field; crafd has one only because a
temporary runtime cap was set minutes earlier:
```
views-faoapi:   Memory: 4.8G (peak: 4.8G)
views-crafdapi: Memory: 3.7G (max: 14.0G available: 10.2G peak: 7.3G)
```

**C** — the streamed ingest, executing for the first time after the cache was cleared:
```
historical_stream.py:339 - Streamed historical value-dir: 28421738 rows,
                           439 months x 64742 cells, 3 target(s)
dataset_service.py:625  - ... (C-263 low-memory path)
```
439 × 64,742 = 28,421,738, the exact grid the loader's precondition asserts.

**D** — every boot, at ERROR, for eleven days:
```
ERROR - Config config_deployment.py failed AST safety check — refusing to execute
ERROR - Config config_hyperparameters.py failed AST safety check — refusing to execute
ERROR - Config config_meta.py failed AST safety check — refusing to execute
```

**H** — the number both ceilings were derived from:
```
$ free -g
               total        used        free      shared  buff/cache   available
Mem:              22           7           8           0           7          15
Swap:              0           0           0
```

**The measurement the day existed for**, for completeness — cold start, cache cleared, one request:
```
200  461991 bytes  62.09 s
Memory: 3.7G (max: 14.0G available: 10.2G peak: 7.3G)
```
461,991 bytes is the same byte count ADR-030 records for the v0.4.0 production run: the served
artifact is unchanged across the change.

## Cross-references

**C-282** (a released ingest change can lie dormant behind a valid disk-cache entry — instance C,
and the entry this report generalises) · **C-262** (the faoapi ceiling; recorded the symptom of B
without recognising it) · **C-279** (the verification gap, G) · **C-281** (runbook text that reads
correctly and misleads in use — four instances of a neighbouring shape) · **C-238** (D) ·
**C-266** (F) · **C-265** · views-faoapi **#368** (closed with the operator step undone) and
**#432** (filed 2026-08-21) · ADR-003 (declarations over inference — the principle this shape
violates from the other direction: the declaration was honest and the system disagreed).
