# ADR-042: The documents a partner-facing API repository keeps, and no more

**Status:** Accepted
**Date:** 2026-11-19
**Deciders:** Simon (PRIO), Claude Code
**Informed:** views-faoapi, and any future VIEWS API built for partners

---

## Context

Before November 2026 this repository had many documents, written for a server shared with
views-faoapi, and none matched what the service had become. Rewriting them (epic #198) showed which
ones a maintainer and a partner actually need. One person maintains this API, with 1 to 2 hours a
week for all of its upkeep. Every document is a promise to keep it true, so the set must be small.

## Decision

A partner-facing API repository keeps these documents. Each has one reader and one job.

| Document | Reader | Job | Here |
|---|---|---|---|
| Service specification | maintainer, reviewers | what the service is for and what it promises; changes rarely | `docs/SERVICE_SPECIFICATION.md` |
| Partner guide | partners | how to use the API; we aim to keep it stable | `docs/api/README.md` |
| Data dictionary | partners | every column and what it means; changes more often | `docs/api/data_dictionary.md` |
| Changelog | partners | every change that affects them, newest first | `docs/api/CHANGELOG.md` |
| Partner keys | maintainer | how a key is made, recorded, sent, renewed and revoked | `deployment/PARTNER_KEYS.md` (the kinds of key: ADR-040) |
| Release runbook | maintainer | release a version; the API is down; an old forecast; a new server | `deployment/RELEASE_RUNBOOK.md` |
| Service settings and setup script | the server | the files the runbook installs, with plain comments | `deployment/views-crafdapi.service`, `deployment/bootstrap.sh` |
| Monitoring setup | maintainer | how to re-create the monitors | `deployment/MONITORING.md` (#213); the decisions: ADR-032, ADR-041 |
| Repository README | anyone opening the repository | what it is, where the documents above are, how to run the tests | `README.md` |
| ADRs | maintainer, reviewers | why things are as they are | `docs/ADRs/active/` |
| Code rules | people and AI agents changing the code | class contracts, standards, contributor protocols | `docs/CICs/`, `docs/standards/`, `docs/contributor_protocols/` |

Rules:

1. **Nothing else is required.** A new document needs a reader and a job that none of these
   covers. If one of these can hold it, it goes there.
2. **Things that need no human get no routine.** This repository has no monthly routine document:
   nothing monthly is done here, and the forecast arrives from upstream.
3. **Partner documents say only what the code does today.** Plans are named as plans.
4. **Plain words.** Internal codes, issue numbers and decision ids belong in ADRs and commit
   messages, not in partner documents or runbook steps.

## Consequences

- A document outside this list is a candidate for deletion when it goes stale.
- views-faoapi can use the table as a checklist. Its own contents differ; the list of jobs is the
  part to copy.
