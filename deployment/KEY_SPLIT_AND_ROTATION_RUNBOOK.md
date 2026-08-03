> **⚠ INHERITED FROM views-faoapi — DOES NOT APPLY TO CRAF'd.**
> This runbook closes a key that sits exposed in **faoapi's** git history (issue #338,
> C-178). CRAF'd was seeded from a scrubbed v1.4.0 snapshot with fresh history and gets
> its own read-scoped key at **S9** — there is no leaked crafd key to close. This file is
> scheduled for deletion/relocation at **S9/S12**; do not act on it for CRAF'd.

# Key split & rotation runbook — closing the leaked-key problem, safely

*Written to be read by a human. Operator-only work (Appwrite console + one message
to FAO); no agent can do it. Every step says what you do, what you should see, and
how to undo it. **The old key keeps working until the very last step**, so stopping
at any point before then is always safe. Related: issue #338, ADR-031, register
C-178 / C-77 / #123.*

---

## The problem, in one paragraph

Today **one** Appwrite key does four jobs at once: it is FAO's key, your dev key,
the ops/server key, and — because it was committed long ago — the key that sits
**exposed in this repo's git history**. You can't just delete the exposed key,
because deleting it cuts off FAO, dev, and the server all at the same moment. So it
never gets deleted, and the leak stays open. This runbook fixes that by **giving
each party its own key first, confirming everyone works, and only then revoking the
old shared key** — at which point the copy in git history becomes a dead, useless
string.

## Two facts that make this safe and simple

1. **faoapi is a pure proxy.** A caller sends their Appwrite key in the `X-API-Key`
   header; faoapi hands it straight to Appwrite. So "what FAO can see" is decided
   entirely by **how their Appwrite key is scoped in the console** — not by any code
   here. Nothing in this repo changes. (ADR-027.)
2. **Revoking a key is the real fix, not scrubbing history.** Rewriting 180-odd
   commits of history is risky and never fully certain. A **revoked** key is inert
   no matter how many copies exist. So the goal is *revoke the old key*, and history
   can be left alone.

## Before you start (gather, don't act yet)

- [ ] Admin access to the Appwrite **console** for the project.
- [ ] Your **password manager** open (this is where new keys are stored — never a
      `.env`, never a commit).
- [ ] A **secure channel to FAO** (whatever you already use — not plain email if you
      can help it) and the name of the person who owns their integration.
- [ ] 30 quiet minutes. Nothing here is reversible-unfriendly until Step 5.

---

## Step 1 — create the new keys in the Appwrite console  *(~10 min, console only)*

In the Appwrite console, **create four new API keys** (leave the old one alone):

| New key | Scopes (least privilege) | Who holds it |
|---|---|---|
| `faoapi-caller-fao` | `files.read`, `documents.read`, `databases.read` | FAO (their `X-API-Key`) |
| `faoapi-datastore-serve` | `files.read`, `documents.read`, `databases.read` | the **server** (operator secret at bootstrap/warmup) |
| `faoapi-dev` | read scopes; add write only if you actually test writes locally | you, on your laptop |
| `faoapi-ops` | read scopes (admin tasks use your console login, not a key) | ops/maintenance |

Notes:
- **Read-only for FAO and the server** is the whole point — the old key was
  full-scope ("fanned out"); these are narrowed to exactly read the bucket + the
  metadata collection/database.
- Copy each new key straight into the **password manager** as you create it. Do not
  paste any of them into a file in this repo.
- You should see four new keys listed alongside the old shared one. **Traffic still
  runs on the old key — nothing has moved.**

## Step 2 — move the server onto its own key  *(~5 min, terminal)*

On the server, replace the operator secret with `faoapi-datastore-serve` and
restart, exactly as in `RELEASE_RUNBOOK.md`:

```bash
export APPWRITE_DATASTORE_API_KEY='<faoapi-datastore-serve, from the password manager>'
# update the operator secret slot the service reads, then:
sudo systemctl restart views-faoapi
sleep 5
sudo systemctl status views-faoapi --no-pager | head -12          # -> active (running)
```

Verify serving works on the new key (this also warms its cache):

```bash
APPWRITE_DATASTORE_API_KEY='<faoapi-datastore-serve>' .venv/bin/python scripts/smoke.py
```

Expect `ALL PASS`. **The old key is still valid** — if anything looks wrong, put the
old secret back and restart; you're exactly where you started.

## Step 3 — move yourself (dev/ops) onto the new keys  *(~2 min)*

Swap your local `faoapi-dev` key in the password manager into wherever you export it
for local work. Do one request to confirm it works. Ops the same with `faoapi-ops`.

## Step 4 — hand FAO their new key  *(coordination — their clock, not yours)*

Over the secure channel, send FAO their `faoapi-caller-fao` key with a short note:
"please switch your `X-API-Key` to this new key at your convenience; the old one
keeps working until you confirm the new one, then we retire the old one." Ask them
to confirm one successful call on the new key.

**Wait for that confirmation before Step 5.** This is the only step you don't
control the timing of — that's fine, the old key stays valid meanwhile.

## Step 5 — revoke the old shared key  *(~1 min, console — this is the irreversible one)*

Only once the server (Step 2), you (Step 3), and FAO (Step 4) are all confirmed on
their new keys: in the Appwrite console, **delete/revoke the old shared key**.

The moment you do, the copy sitting in git history is a dead string — the leak
(C-77 / #123) is closed. Nothing else needs to change; you do **not** have to rewrite
history.

Confirm nothing broke:

```bash
curl -s https://faoapi.viewsforecasting.org/ping        # -> {"status":"ok"}
.venv/bin/python scripts/smoke.py                        # -> ALL PASS  (server on its own key)
```

## If something breaks after Step 5 — the undo

You revoked the shared key, so "undo" is **forward**: whoever broke was still using
the old key — issue them a fresh scoped key from Step 1's recipe and hand it over.
You never need the old key back; that's the point of retiring it.

## After — close the paperwork

- Mark **#338** done and note the date the old key was revoked.
- Update register **C-178 / C-77** to resolved (leaked key revoked; least-privilege
  caller keys in place).
- This also unblocks the public flip (#123 / #315): the exposed-secret precondition
  is now cleared.

## Why this is worth doing before cloning faoapi

When faoapi is cloned into the two new APIs, whatever key model it carries gets
copied too. Splitting to per-party, least-privilege, read-scoped keys **now** means
the clones start clean instead of inheriting one over-scoped, leaked, shared key.
