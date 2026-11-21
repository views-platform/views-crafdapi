# CRAF'd forecast API: data dictionary

This page lists every column the API serves today, and what each one means. It goes with the
[partner guide](README.md).

This page is expected to change more often than the partner guide, which is why they are separate. The guide
explains how the API works. We aim to keep it stable and to change it as little as possible, so
that you do not have to adapt.

This page is different. Conflict deaths have an unusual distribution: most places and months have
none, and a few have very many. It is still an open question which summaries of the forecast are
most useful, and the answer will likely differ between partners. That is not only a question of which part of the distribution to show. It is also
whether to show totals over several months, numbers relative to population, or change over time,
such as whether violence is likely to escalate, rather than only its expected size. So we will try
out new summaries, guided by feedback from partners. (Take this as an invitation: if you have
thoughts, wishes or feedback, write to us at views-platform@prio.org.)

When the columns change, we update this page and record the change in the
[changelog](CHANGELOG.md).

## Columns that say which place and month a row is about

Every row, at every level, has:

| Column | Meaning |
|---|---|
| `month` | The month the row is about, written year-month: `2027-01` is January 2027. For a forecast, it is the month being forecast. For a past month, it is the month the deaths happened. |
| `month_id` | The same month as VIEWS numbers it internally: January 1980 is 1, and each month adds 1. Only needed to match VIEWS data. |

Then, depending on the level:

| Column | Grid | Admin 1 | Country | Meaning |
|---|---|---|---|---|
| `priogrid_id` | ✓ | | | The PRIO-GRID cell id (see below). |
| `latitude`, `longitude` | ✓ | | | The centre of the grid cell, in decimal degrees (WGS 84). |
| `admin1_code`, `admin1_name` | ✓ | ✓ | | The FAO GAUL 2024 admin-1 region: its code and name. |
| `country_code`, `country_name` | ✓ | ✓ | ✓ | The country: its FAO GAUL 2024 admin-0 code (a number between 100 and 372; not every number in that range is used) and its name. |
| `country_iso3` | ✓ | ✓ | ✓ | The country's three-letter ISO 3166 code, for joining with other data. |

Things to know about these codes:

- **PRIO-GRID** divides the world into square cells of 0.5 by 0.5 degrees, about 55 km across at
  the equator. If you publish work based on the grid, cite Tollefsen, Strand and Buhaug (2012),
  "PRIO-GRID: A unified spatial data structure", *Journal of Peace Research* 49(2).
- **FAO GAUL** (Global Administrative Unit Layers) is the UN Food and Agriculture Organization's map
  of countries and their regions. It is published under the CC-BY-4.0 licence, so credit FAO GAUL
  2024 when you use the codes or names.
- **GAUL 2024, not GAUL 2015.** FAO renumbered the countries in 2024. A GAUL 2015 table gives the
  wrong country for our codes.
- **A place belongs to the country its grid cell lies in.** Deaths are counted where they happened,
  by grid cell, and a cell belongs to one country. The source of the recorded deaths (the Uppsala
  Conflict Data Program, UCDP) sometimes names a different country, for example for fighting near a
  border. So country totals here can differ from UCDP's own country totals.
- **Some countries are not divided into regions.** FAO does not divide 62 countries, such as
  Greenland and Abyei. For those, the admin-1 name is the text `Administrative Unit Not Available`
  and the region covers the whole country.
- **Six grid cells in the Azores have negative admin-1 codes:** four codes, -3727 to -3730. FAO's map leaves
  out four of the islands, so they were added from another map (Natural Earth) with made-up codes
  that cannot be mistaken for FAO's.
- **ISO3 codes starting with `x`** (such as `xAB`, Abyei) are FAO's labels for disputed areas and
  areas without an official ISO code. Three ISO3 codes cover more than one GAUL country (`AUS`,
  `PYF`, `xFR`), so group by `country_code`, not by `country_iso3`.

## Summary columns, one set per outcome

Each outcome (`sb`, `ns`, `os`) has its own set of columns. The column name starts with the outcome
code: `sb_p_gt25` is the chance of more than 25 deaths from state-based violence. Below, `s` stands
for the outcome code.

All numbers of deaths are counts of deaths, not rates or logs. Every column is computed from the
draws (see the [partner guide](README.md)).

This set will change. We may add or remove columns, change how a column is defined, or add new
outcomes, each with its own set of columns. Every such change is recorded in the
[changelog](CHANGELOG.md), with the date it takes effect.

| Column | What it is | Unit |
|---|---|---|
| `s_p_gt0` | The chance of any deaths at all. | probability, 0 to 1 |
| `s_p_gt25`, `s_p_gt100`, `s_p_gt500`, `s_p_gt1000` | The chance of more than 25, 100, 500 or 1,000 deaths. | probability, 0 to 1 |
| `s_mean` | The expected number of deaths: the average over all draws, including those with no deaths. | deaths |
| `s_mean_if_any`, `s_median_if_any` | How many deaths there would be if there are any: the average and the middle value over the draws with at least one death. | deaths |
| `s_severe_scenario` | The average of the worst 5% of the draws: what a bad month could realistically look like. Empty when there are fewer than 20 usable draws, because the worst 5% would then be a single draw. | deaths |
| `s_actual` | The recorded number of deaths, for months before the forecast. Empty for the forecast's months. | deaths |

And one column for the whole row:

| Column | What it is |
|---|---|
| `n_draws` | How many usable draws this row's summaries were computed from. The more draws, the more precise the chances and the severe case. |

**Why these thresholds.** 25, 100, 500 and 1,000 deaths are edges of the bins VIEWS uses to score
monthly forecasts for each place, in its Prediction Challenge (Hegre, Vesco, Colaresi et al. 2025,
*Journal of Peace Research*). 25 is also the number of deaths per month at which VIEWS counts a
country as in conflict, a rule inspired by the Uppsala Conflict Data Program's threshold of 25
battle-related deaths, which is per year, not per month.

## Read these with care

- **Chance of any deaths and how many if there are any belong together.** In one VIEWS grid
  forecast, two cells both had about a 37% chance of deaths; if deaths happened, one would see about
  17 and the other about 41. Read `s_p_gt0` with `s_mean_if_any`.
- **The chances always go down as the number goes up:** `s_p_gt0` is at least `s_p_gt25`, which is
  at least `s_p_gt100`, and so on. You can use this as a check.
- **A chance of 0 means that no draw went above that number.** It does not mean it is impossible.
  The smallest chance above 0 is one divided by `n_draws`.
- **Empty means no answer, not zero.** If a place and month has no usable draws, its summaries are
  empty, never 0.
- **The thresholds may change.** If they do, the column names change with them, and the change is
  recorded in the [changelog](CHANGELOG.md). So read the names from the data, rather than typing
  them into your code.
- **The first forecast month shows no recorded deaths.** Recorded deaths arrive about a month
  late, so when a forecast is made, its first month is only partly recorded. Rather than show a
  misleading `0`, `s_actual` is empty for every forecast month, that one included.

## Adding up places or months yourself

**Ask the API for the level you need; don't build it from smaller places.** The API keeps every
draw for every place, lined up, so it knows whether two places have their bad months in the same
draw. It adds up the draws first and summarises after. A bulk file only has the summaries, one
number per place, and from those you cannot tell whether bad months happen together.

What you can and cannot combine yourself:

| Column | Add up across places or months? | Why |
|---|---|---|
| `s_mean` | Yes | An average of a sum is the sum of the averages. |
| `s_actual` | Yes | It is a recorded count. |
| `s_p_gt0` to `s_p_gt1000` | No, and do not average them either | The chance for a region depends on whether its places go bad together. Ask the API for the region. |
| `s_mean_if_any`, `s_median_if_any` | No, and do not average them | Each place's value assumes deaths happen in that place; an average of them assumes nothing in particular. |
| `s_severe_scenario` | No | Adding up the worst cases assumes every place has its worst month in the same draw, which gives far too high a number. Ask the API for the region. |
| `n_draws` | No | Check that it is the same for all the rows you combine. If it is not, their summaries are not comparable. |

Three things to watch:

- **Count the empty rows before you add up.** Most tools skip empty values when they sum, so a total
  over places with some empty rows comes out too low and looks fine. In pandas, for example,
  `sum(min_count=1)` and a count of `isna()` per group show you what was left out.
- **A region's chance of deaths is at least as high as that of its riskiest place.** Often it is
  exactly equal, because one place carries almost all the risk. If you find that the region's value
  equals its highest place's value, that is correct, not a mistake.
- **There are no totals over several months yet, except by adding up `s_mean` or `s_actual`.** The
  API serves one month at a time, and a chance or a severe case for several months cannot be built
  from the monthly rows. We plan to add periods of 3, 6 and 12 months, computed from the draws, to
  both the bulk files and the slices. Until then, ask us if you need them.
