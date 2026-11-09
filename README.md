# Regional Cold Season Verification

A dark-mode, Poppins-based Google Apps Script web viewer for cold-season climatology and NWS frost/freeze/cold-product verification across **MOB, LIX, JAN, BMX, and TAE**.

## What the viewer does

### Freeze Frequency
- Uses a September–May cold season.
- Color-fills counties by the number of days a selected cold threshold was reached.
- Custom temperature threshold slider from **-20°F to 40°F**.
- Presets for frost screening, freeze, hard freeze, local Cold Weather Advisory criteria, and local Extreme Cold criteria.
- Supports **All IEM METAR sites** or **ASOS/AWOS only**.
- Counties without an observation site use the nearest regional station and are explicitly treated as fallback/estimated coverage.

### Season Products
- Loads archived cold-season NWS products from the IEM VTEC archive.
- Includes Frost Advisory, Freeze Watch/Warning, Cold Weather Advisory, Extreme Cold Watch/Warning, plus legacy Wind Chill/Hard Freeze products for older seasons.
- Lets you select a season event and screen affected counties against observed criteria.
- Green/red county verification is an observational screening analysis, not official NWS verification.

### Criteria / Methods
- Documents the county/station methodology and the local threshold logic used by the viewer.

## Repository structure

```text
Regional-Cold-Season-Verification-R1/
├── src/
│   ├── Code.gs
│   ├── Index.html
│   └── appsscript.json
├── .clasp.json.example
├── .gitignore
└── README.md
```

## Recommended workflow

GitHub is the source-control home for the project, while **Google Apps Script remains the runtime/backend**. This preserves UrlFetchApp, server-side caching, IEM/NOAA requests, and the deployed web-app URL.

### First-time setup

1. Install Google's Apps Script CLI:
   ```bash
   npm install -g @google/clasp
   ```
2. Log in:
   ```bash
   clasp login
   ```
3. Create a standalone Apps Script project in your Google account.
4. Copy its **Script ID** from Apps Script → Project Settings.
5. Copy the example config:
   ```bash
   cp .clasp.json.example .clasp.json
   ```
6. Replace `PASTE_YOUR_APPS_SCRIPT_ID_HERE` with the Script ID.
7. Push the GitHub source into Apps Script:
   ```bash
   clasp push
   ```
8. In Apps Script, run:
   - `coldSeasonR1_getBuildInfo`
   - `coldSeasonR1_authorizeAndTest`
9. Deploy as **Web app**, executing as you, with the appropriate access setting for your office/site.
10. Use the deployed URL ending in `/exec`.

## Day-to-day development

```bash
git pull
# edit files under src/
git add .
git commit -m "Describe the change"
git push
clasp push
```

After code changes, update the Apps Script web-app deployment to a **new version** before testing the production `/exec` URL.

## Data sources

- Iowa Environmental Mesonet station metadata and computed daily summaries
- Iowa Environmental Mesonet VTEC archive
- NOAA/NWS county and CWA reference boundaries

## Build

Current build: **REGIONAL-COLD-SEASON-R1**
