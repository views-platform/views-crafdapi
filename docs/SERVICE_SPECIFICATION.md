---
title: "crafdapi: service specification"
subtitle: "Ratified by the maintainer, 2026-11-13. Details may change."
---

# What this service is

**Purpose.** Give partner organisations the current forecast, and the matching recent history, over a web API.

**Users.** Partner organisations. CRAF'd funds the work and does not use it.

**Intended use.** Partners use the forecasts as one input to their own planning and early warning. The forecast describes a range of possible outcomes, not a prediction of exact numbers.

**Not designed for.** Reading a single number as a certainty, or judging any one place and month on its own without its uncertainty.

**Same data for everyone, always.** Separate keys exist only so that one partner can be switched off.

# What the data is

- **Source:** one named ensemble from the VIEWS system.
  - An ensemble is a combination of several individual forecasting models. Those models are named in the ensemble's own configuration, not here.
  - Which ensemble the API serves is a setting.
- **Two forecast levels, made to agree:** the ensemble forecasts at **grid-cell** level and at **country** level.
  - The two are reconciled: adjusted so that a country's grid cells add up to that country's forecast.
  - **Admin 1** (the first level below country, such as provinces) is not forecast directly. It is the sum of its grid cells. Admin-level models may be added later.
- **Monthly:** the models run every month, and each run replaces the previous forecast.
- **Samples:** each forecast is a set of sampled possible outcomes per place and month.
  - Partners do **not** get the samples. They get summary columns derived from them, each describing one aspect of the outcomes: for example, the expected outcome, the most likely outcome, a plausible range, the chance of exceeding a level, or a severe-case outcome.
  - The exact columns may change and are documented elsewhere (see below).
- **Outcomes:** what is forecast. Today, three kinds of violence: state-based, non-state and one-sided. The list is open: new outcomes, such as the impact modelling of child mortality in conflict zones, must fit in without reworking the API.
- **History:** the recorded values that go with the forecast.

# Levels

Grid cell, admin 1 and country. Admin 0 (a duplicate of country) and admin 2 are dropped.

A grid cell is one square of a fixed worldwide grid, about 55 km across at the equator.

# Bulk download: the main way in

One ready-made file per outcome and level, in Parquet (a standard compact format for tables). Each file is built once per monthly forecast and downloaded as is.

Today that is nine files: three outcomes times three levels. A new outcome adds three files, one per level.

# Slicing: secondary

At one level, a request takes four options: **which places, which months, which outcome**, and optionally **which columns**. Without a column list, all summary columns come back; with one, only the columns named. Place names are always included.

Nothing else is offered: no sample selection, no forced rebuilds, no on-request range widths. A new option needs a partner who needs it.

# Keys

- Add a partner: make a key and send it.
- Remove a partner: delete the key, and it stops working within minutes.

# Updates

A new forecast arrives monthly. The API serves exactly **one** forecast: the latest good one uploaded, with the month it is from shown in every answer.

- If a new run is late or broken, the last good one stays.
- The API never serves older forecasts. A partner who wants one contacts VIEWS.

# Running it

One server, with its own storage project holding only this forecast. An alert goes out if it is down.

Storage is currently Appwrite, a hosted storage service. That is today's choice, not part of what this service is: the API could move to another store without changing anything partners see.

Design constraint: the maintainer has 2–3 hours a week for all the APIs together. Anything that does not fit in that does not get built.

# Where things are defined (not here)

This document says what the service is. Everything below lives somewhere else; this list only says where to look. "This repo" means views-crafdapi.

- **Which ensemble is served:** the CRAF'd delivery setting in **views-models**. It names the grid-level ensemble and the country-level ensemble it is reconciled with.
- **Which models make up the ensemble:** that ensemble's own configuration in **views-models**.
- **Whether the levels are reconciled, and with which method:** decided only in the ensemble's configuration in **views-models**. The reconciliation methods themselves are code in the shared **views-frames** library.
- **The history data:** **views-datafactory**.
- **Turning a forecast into the files this API reads:** the CRAF'd postprocessor in **views-postprocessing**. It also attaches the admin-1 and country codes to each grid cell, and owns the format of those files.
- **Which columns partners get, and what each means:** the data schema and data dictionary in **this repo**. The schema in code is the authority; the dictionary explains it in words.
- **Where the data is stored, and the keys themselves:** CRAF'd's own project in the current storage service (Appwrite).
  - Its address and names are recorded in the shared registry in **views-appwrite**.
  - Keys are made and deleted in that project's Appwrite console.
- **Which partner holds which key:** a private key register kept by the maintainer, outside every repo. It never holds the keys themselves.
- **Adding or removing a partner:** the partner-key runbook in **this repo**.
- **Server setup, updates and the monthly routine:** the deployment folder in **this repo**.
- **Monitoring:** an external uptime monitor, plus a daily check of the data's age in **this repo**.
- **Forecast accuracy and known limitations:** not yet decided where (see Open).
- **Shared terms (forecast, run, outcome, level, ...):** a shared glossary. It does not exist yet.

# Out of scope

Raw samples, older forecasts, per-partner data, accounts, a website, and options nobody has asked for.

# Open

1. **Limitations.** Where do partners read how accurate the forecasts are and what they cannot tell you? This document will point there; it will not hold it.
2. **Telling partners about changes.** How do partners learn that the columns or outcomes changed: an email from VIEWS, a change log, or a version shown in every answer?
3. **Intended use.** Check the wording above against what was agreed with the partners.
