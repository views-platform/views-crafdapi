# CRAF'd forecast API: partner guide

This API gives partner organisations a forecast made by an ensemble of models from the VIEWS
conflict forecasting system. For each outcome it forecasts, you also get the historical
observations of that same outcome, the records the models were trained on: for example, recorded
state-based violence deaths alongside the state-based violence forecast. You do not get the other
data the models use as inputs.

**An experimental API.** This is not the VIEWS public forecast; that one is published on the VIEWS
website and public API. This API is more experimental by design. The benefit is more features
sooner: today, global coverage and measures of uncertainty; soon, impact models; and in general
newer models. The cost is more frequent change: which ensemble is used, and which models it
combines, will change more often than in the public forecast. We will be as open and as early as we
can about each change.

**We aim to keep this guide stable.** It explains how the API works. It is our sincere intent to
change it as seldom as we can, so that you do not have to adapt. The list of columns lives in a separate page, the
[data dictionary](data_dictionary.md), because it will change more often as we try out new
summaries. Every change to either is recorded in the [changelog](CHANGELOG.md).

- **Address:** `https://crafdapi.viewsforecasting.org`
- **Your key:** send it with every request, in a request header named `X-API-Key`.
- **Questions, or an older forecast:** contact VIEWS at views-platform@prio.org.

## Getting started

**We recommend the bulk download.** It is usually faster, and less prone to error: everything in
it comes from the same, most recent forecast.

The bulk file also lists every place id and name, every month and every column name the API uses.
Use those if you do need a smaller slice.

In the examples, `$CRAFD_KEY` stands for your key.

## What you get

**One forecast: the latest.** A new forecast arrives once a month and replaces the previous one.
Every answer says which month the forecast was made in. If a new forecast is late, you keep getting
the last good one.

**Outcomes.** Each forecast covers several outcomes. Today these are three types of conflict
fatalities, as defined by the Uppsala Conflict Data Program
([UCDP definitions](https://www.uu.se/en/department/peace-and-conflict-research/research/ucdp/ucdp-definitions)):

| Outcome | Code |
|---|---|
| State-based violence | `sb` |
| Non-state violence | `ns` |
| One-sided violence (against civilians) | `os` |

More outcomes may be added; each gets its own code.

**Levels.** Every outcome is available at three levels:

| Level | Code | A place is identified by |
|---|---|---|
| Grid cell (a square about 55 km across at the equator) | `grid` | its PRIO-GRID cell id, a number such as `148759` |
| Admin 1 (the first level below country, such as a province) | `admin1` | its FAO GAUL 2024 admin-1 code, a number |
| Country (GAUL calls this admin 0) | `country` | its FAO GAUL 2024 country code, a number such as `161` (Sudan) |

Every row at the admin-1 and country levels also carries the country's name and its ISO 3166
three-letter code (such as `SDN`), for joining with other data. GAUL 2024 numbers countries differently from the older GAUL 2015, so
an older GAUL table gives the wrong country. The bulk file for a level lists
every place id at that level, with its name.

**Summary columns, not raw numbers.** In most places, in most months, there is little or no
conflict, especially below country level. So an average or a single "expected" number of deaths is almost
always close to zero. That is accurate, but it tells you little. It hides the chance of a bad
month, which is often what matters most.

So the models do not stop at one number. They produce a large set of possible futures for each
place and month: quiet ones, typical ones, and rare, very bad ones. From that set we can say how
likely the bad futures are, and how bad they could plausibly get.

Technically, each possible future is a draw (a sample) from a probability distribution that the
models estimate but that cannot be observed directly. So for each place and month there are many
draws, not one number.

You don't get the draws themselves: there are too many of them to send and to compute with
quickly. You get columns that summarise them, for each place, month and outcome. For example:

- **The chance of any deaths at all,** and **how many deaths there would be if there are any.**
  Read these two together: two places can have the same chance of deaths, but very different
  numbers if it happens.
- **The chance of exceeding a level.** For example: "a 12% chance of more than 100 deaths".
- **The expected number of deaths:** the average over all the draws. It is usually small, because
  most draws have no deaths.
- **A severe case.** The average of the worst 5% of the draws: what a bad month could realistically
  look like.

Which summaries are offered is not fixed. We will try out different summaries, so the columns
above are examples, and any one of them may not be available at a given time. Every change is
recorded in the [changelog](CHANGELOG.md), and the [data dictionary](data_dictionary.md) always
lists the columns available now.

For past months, the recorded number of deaths is in its own column, so you can compare the forecast
with what actually happened. The recorded numbers are added once they have been published: the
first forecast month never shows recorded deaths, because only a sliver of that month has been
reported when the forecast is made.

## Bulk download (the fast way)

One ready-made file per outcome and level, in Parquet format (a compact table format read by
pandas, R's `arrow`, and most data tools).

```
GET /bulk/{level}/{outcome}
```

Example: state-based violence for every admin-1 region.

```bash
curl -H "X-API-Key: $CRAFD_KEY" -o sb_admin1.parquet \
  https://crafdapi.viewsforecasting.org/bulk/admin1/sb
```

A file holds the same table as a slice with no options: every place and every month. Months before
the forecast are recorded months: they have the recorded deaths, and their summary columns are empty.
Forecast months have the summary columns, and their recorded-deaths column is empty. To keep only
one kind, filter on whichever column is filled, or on `month_id`.

The first download after a new forecast arrives builds the file and can take about a minute for the
grid level; later downloads are sent straight away.

## Slices (a smaller piece)

For a few places or months, ask for a slice. You get JSON back.

```
GET /slice/{level}/{outcome}?places=...&months=...&columns=...
```

| Option | Meaning | If left out |
|---|---|---|
| `places` | Comma-separated place ids at that level | All places |
| `months` | Comma-separated months, written year-month: `2027-01` is January 2027 | All months |
| `columns` | Comma-separated column names | All columns |

Place names and the month are always included. Every row has a `month` column written the same
way, `2027-01`, and VIEWS' own month number, `month_id`. Grid rows also give the cell's centre,
`latitude` and `longitude`.

Example: state-based violence in Sudan and South Sudan, for two months, only the chance of more
than 25 deaths.

```bash
curl -H "X-API-Key: $CRAFD_KEY" \
  "https://crafdapi.viewsforecasting.org/slice/country/sb?places=161,160&months=2027-01,2027-02&columns=sb_p_gt25"
```

## If something goes wrong

| Answer | Meaning |
|---|---|
| `401` | The key is missing, wrong, or no longer valid. Contact VIEWS. |
| `404` | That level or outcome does not exist. Check the codes above. |
| `413` | The slice asks for too much at once. Ask for fewer places or months, or use the bulk download for the same level and outcome. |
| `422` | An option is written wrong, for example a month not written `2027-01`. |
| `503` | The forecast, or the recorded deaths, are not available right now. Try again later. |
| `500`, `502`, `504` or no answer | A problem on our side. Try again later; contact VIEWS if it lasts. |
