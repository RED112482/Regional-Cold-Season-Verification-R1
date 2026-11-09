# Regional Cold Season Verification

A **GitHub-only** cold-season climatology and verification viewer for **MOB, LIX, JAN, BMX, and TAE**.

The live app uses:

- **GitHub Pages** for the HTML/CSS/JavaScript viewer.
- **GitHub Actions** for all NOAA/IEM data fetching and preprocessing.
- Static JSON files under `site/data/` for fast browser loading.

There is **no Google Apps Script backend, no npm requirement, and no local command-line setup required**.

## Viewer

The viewer is designed around three tabs:

### Freeze Frequency
- September–May cold seasons.
- County-filled counts for days meeting a selected threshold.
- Custom slider from **-20°F to 40°F**.
- Frost, Freeze, Hard Freeze, local Cold Weather Advisory, and local Extreme Cold presets.
- All IEM METAR or ASOS/AWOS-only observation sets.
- Counties without a site use the nearest regional station and are marked as estimated/fallback coverage.

### Season Products
- Archived Frost Advisory, Freeze Watch/Warning, Cold Weather Advisory, Extreme Cold Watch/Warning.
- Legacy Wind Chill and Hard Freeze products are retained for older seasons.
- Click an event to compare affected counties against observed criteria.
- Verification colors are an observational screening tool, not official NWS verification.

### Criteria / Methods
- Documents the local thresholds and the county/station methodology.

## Repository layout

```text
Regional-Cold-Season-Verification-R1/
├── .github/
│   └── workflows/
│       └── pages.yml
├── scripts/
│   └── build_season.py
├── site/
│   ├── index.html
│   ├── .nojekyll
│   └── data/
│       ├── config.json
│       └── seasons/
└── README.md
```

## One-time GitHub Pages setup

In the repository:

1. Open **Settings**.
2. Select **Pages**.
3. Under **Build and deployment → Source**, choose **GitHub Actions**.
4. Open the **Actions** tab.
5. Choose **Build data + deploy Pages**.
6. Click **Run workflow**.
7. Leave `season = current` and `mode = both`, then run it.

After the workflow finishes, the project page will be:

`https://red112482.github.io/Regional-Cold-Season-Verification-R1/`

## Historical seasons

You do not need a command prompt.

From **Actions → Build data + deploy Pages → Run workflow**:

- Enter `2025` to build the 2025–26 season.
- Enter `2024` to build the 2024–25 season.
- Enter `all` to build every available season from 2005 through the current season.
- Keep mode at `both` to build both station datasets.

The generated JSON is committed back into `site/data/seasons/`, so once a historical season is built it remains available to the viewer.

## Automatic updates

The workflow automatically refreshes the **current season every day at 4:17 AM America/Chicago**. It also rebuilds/deploys on relevant pushes to `main`.

The data builder deliberately makes IEM daily-summary requests sequentially and pauses between requests to respect IEM's documented per-IP throttle.

## Data sources

- Iowa Environmental Mesonet computed daily summaries
- Iowa Environmental Mesonet VTEC archive
- NOAA/NWS county reference boundaries
- CARTO / OpenStreetMap basemap tiles

## Build

Current viewer build: **REGIONAL-COLD-SEASON-GITHUB-R2**
