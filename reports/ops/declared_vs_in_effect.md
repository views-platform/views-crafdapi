# Declared done, not in effect — seven instances, one shape

*First draft, written 2026-08-21 after a day that produced three fresh instances in about six
hours. Every fact below is read from a journal, a `systemctl` output, an HTTP response, a commit
or an issue — not from memory. Governed by ADR-010; the live entry is **C-282**, which records
one instance; this report is the argument that the shape is worth more than the instance.*

---

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

## What actually caught them

Two families, and neither is "read the artifact".

**Read runtime state** — A, B, C, D. `/version`, `systemctl status`, the journal. In every case
the system was willing to say what was true; nobody had asked.

**Attack the claim** — E, F, G. A test that parses a file the way its consumer does; a
falsification audit; an adversarial input. Not "does this look right" but "construct the case
where it isn't".

**Reading the artifact caught nothing.** Not once in seven.

## The part that should be uncomfortable

Three of the seven — B, C, D — were found **while investigating something else.** That is not a
detection method; it is luck, and B ran 12 days on it.

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

## What this report does not claim

- It does not claim any of the seven caused an outage. None did. B is the only one with real
  exposure — an unbounded service on a box that has OOM-killed three times — and even there the
  harm is hypothetical.
- It does not claim the defences should be replaced. They should be *joined*.
- Instance E was caught before merge and is included because it is the same shape, not because it
  escaped.
- Seven instances from one repository over twelve days is not a base rate. It is enough to justify
  the habit changes above and not enough to justify machinery.

## Cross-references

**C-282** (a released ingest change can lie dormant behind a valid disk-cache entry — instance C,
and the entry this report generalises) · **C-262** (the faoapi ceiling; recorded the symptom of B
without recognising it) · **C-279** (the verification gap, G) · **C-281** (runbook text that reads
correctly and misleads in use — four instances of a neighbouring shape) · **C-238** (D) ·
**C-266** (F) · **C-265** · views-faoapi **#368** (closed with the operator step undone) and
**#432** (filed 2026-08-21) · ADR-003 (declarations over inference — the principle this shape
violates from the other direction: the declaration was honest and the system disagreed).
