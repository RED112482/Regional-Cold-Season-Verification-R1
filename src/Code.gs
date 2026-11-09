/**
 * Regional Cold Season Verification & Freeze Frequency Viewer
 * Separate side project from the Heat/Cold daily verification viewer.
 * Build: REGIONAL-COLD-SEASON-R1
 */
const COLD_SEASON_R1 = (function() {
'use strict';

const APP_TITLE = 'Regional Cold Season Verification';
const BUILD_ID = 'REGIONAL-COLD-SEASON-R1';
const DATA_MODE = 'COLD_SEASON_COUNTY_VERIFICATION';
const LOCAL_TIME_ZONE = 'America/Chicago';
const IEM_BASE = 'https://mesonet.agron.iastate.edu';
const WFO_IDS = ['MOB', 'LIX', 'JAN', 'BMX', 'TAE'];
const MIN_SEASON_YEAR = 2005;

function doGet() {
  return HtmlService.createHtmlOutputFromFile('Index')
    .setTitle(APP_TITLE)
    .addMetaTag('viewport', 'width=device-width, initial-scale=1')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

function getInitialConfig() {
  const now = new Date();
  const year = Number(Utilities.formatDate(now, LOCAL_TIME_ZONE, 'yyyy'));
  const month = Number(Utilities.formatDate(now, LOCAL_TIME_ZONE, 'M'));
  // June-August intentionally point to the upcoming Sep-May season.
  const defaultSeasonStartYear = month >= 6 ? year : year - 1;
  const seasonOptions = [];
  for (let y = defaultSeasonStartYear; y >= MIN_SEASON_YEAR; y--) {
    seasonOptions.push({ value: y, label: y + '–' + (y + 1) });
  }
  return {
    appTitle: APP_TITLE,
    buildId: BUILD_ID,
    dataMode: DATA_MODE,
    wfos: WFO_IDS,
    center: [31.45, -87.7],
    zoom: 6,
    defaultSeasonStartYear: defaultSeasonStartYear,
    seasonOptions: seasonOptions,
    thresholdMin: -20,
    thresholdMax: 40,
    thresholdDefault: 32,
    defaultStationMode: 'all',
    sourceNote: 'IEM computed daily summaries + IEM VTEC archive + NOAA/NWS county/CWA reference boundaries. County fills are observation-based screening, not official gridded verification.'
  };
}

function getSeasonData(request) {
  request = request || {};
  const seasonStartYear = normalizeSeasonStartYear_(request.seasonStartYear);
  const stationMode = normalizeStationMode_(request.stationMode);
  const season = seasonInfo_(seasonStartYear);
  const warnings = [];

  const today = Utilities.formatDate(new Date(), LOCAL_TIME_ZONE, 'yyyy-MM-dd');
  const yesterday = addDaysToDateString_(today, -1);
  const queryEnd = season.endDate < yesterday ? season.endDate : yesterday;
  if (queryEnd < season.startDate) throw new Error('The selected season has not started yet.');

  const countiesGeoJson = fetchRegionalCounties_(warnings);
  const stationCatalog = fetchRegionalStationCatalog_(countiesGeoJson, warnings);
  const sites = stationMode === 'asosawos' ? stationCatalog.asosAwosSites : stationCatalog.allSites;

  assignCountyToSites_(sites, countiesGeoJson);
  const dailyByStation = fetchSeasonDailySummaries_(sites, season.startDate, queryEnd, warnings);
  const dates = dateRange_(season.startDate, queryEnd);
  const countyStats = buildCountyStats_(countiesGeoJson, sites, dailyByStation, dates);

  let hazardsKml = '';
  try {
    hazardsKml = fetchSeasonColdHazardsKml_(season.startDate, queryEnd);
  } catch (error) {
    warnings.push('Season cold-product archive could not be loaded: ' + error.message);
  }

  return {
    buildId: BUILD_ID,
    dataMode: DATA_MODE,
    season: season,
    queryEnd: queryEnd,
    stationMode: stationMode,
    stationModeLabel: stationMode === 'asosawos' ? 'ASOS / AWOS only' : 'All IEM METAR sites',
    dates: dates,
    countiesGeoJson: countiesGeoJson,
    countyStats: countyStats,
    stationCount: sites.length,
    catalogCounts: { all: stationCatalog.allSites.length, asosawos: stationCatalog.asosAwosSites.length },
    hazardsKml: hazardsKml,
    warnings: warnings,
    generatedUtc: formatUtcIso_(new Date())
  };
}

function normalizeSeasonStartYear_(value) {
  const n = Number(value);
  if (Number.isInteger(n) && n >= MIN_SEASON_YEAR && n <= 2100) return n;
  const now = new Date();
  const y = Number(Utilities.formatDate(now, LOCAL_TIME_ZONE, 'yyyy'));
  const m = Number(Utilities.formatDate(now, LOCAL_TIME_ZONE, 'M'));
  return m >= 6 ? y : y - 1;
}
function normalizeStationMode_(value) {
  return String(value || 'all').toLowerCase() === 'asosawos' ? 'asosawos' : 'all';
}
function seasonInfo_(startYear) {
  return {
    startYear: startYear,
    label: startYear + '–' + (startYear + 1),
    startDate: startYear + '-09-01',
    endDate: (startYear + 1) + '-05-31'
  };
}

function fetchRegionalCounties_(warnings) {
  const base = 'https://mapservices.weather.noaa.gov/static/rest/services/nws_reference_maps/nws_reference_map/MapServer/2/query';
  const where = "cwa IN ('" + WFO_IDS.join("','") + "')";
  const url = base + '?' + [
    'where=' + encodeURIComponent(where),
    'outFields=state,cwa,countyname,fips,lat,lon,time_zone',
    'returnGeometry=true',
    'outSR=4326',
    'f=geojson'
  ].join('&');
  try {
    const response = UrlFetchApp.fetch(url, { method: 'get', muteHttpExceptions: true, followRedirects: true, headers: { Accept: 'application/geo+json,application/json' } });
    if (response.getResponseCode() !== 200) throw new Error('HTTP ' + response.getResponseCode() + ': ' + responsePreview_(response.getContentText()));
    const payload = JSON.parse(response.getContentText());
    if (!payload || !Array.isArray(payload.features)) throw new Error('No county features returned.');
    payload.features.forEach(function(feature) {
      feature.properties = feature.properties || {};
      feature.properties.cwa = String(feature.properties.cwa || '').toUpperCase();
      feature.properties.state = String(feature.properties.state || '').toUpperCase();
      feature.properties.countyname = String(feature.properties.countyname || '').trim();
      feature.properties.fips = String(feature.properties.fips || '').padStart(5, '0');
    });
    return payload;
  } catch (error) {
    warnings.push('NOAA/NWS county boundaries could not be loaded: ' + error.message);
    return { type: 'FeatureCollection', features: [] };
  }
}

function fetchRegionalStationCatalog_(countyGeoJson, warnings) {
  const cache = CacheService.getScriptCache();
  const cacheKey = 'cold-season-r1-station-catalog';
  const cached = cache.get(cacheKey);
  if (cached) {
    try { return JSON.parse(cached); } catch (error) {}
  }
  const urls = [
    IEM_BASE + '/geojson/network.py?network=AZOS',
    IEM_BASE + '/geojson/network.py?network=ASOS1MIN',
    IEM_BASE + '/geojson/network.py?network=AZOS&has_attribute=IS_AWOS'
  ];
  let responses;
  try {
    responses = UrlFetchApp.fetchAll(urls.map(function(url) {
      return { url: url, method: 'get', muteHttpExceptions: true, followRedirects: true, headers: { Accept: 'application/geo+json,application/json' } };
    }));
  } catch (error) {
    warnings.push('IEM station catalog discovery failed: ' + error.message);
    return { allSites: [], asosAwosSites: [] };
  }
  const allGeo = parseGeoJsonResponse_(responses[0], 'IEM AZOS station catalog', warnings);
  const asosIds = featureIdSet_(parseGeoJsonResponse_(responses[1], 'IEM ASOS1MIN station catalog', warnings));
  const awosIds = featureIdSet_(parseGeoJsonResponse_(responses[2], 'IEM AWOS station catalog', warnings));
  const allSites = [];
  const seen = {};
  (allGeo.features || []).forEach(function(feature) {
    const site = stationFromFeature_(feature);
    if (!site || seen[site.obs]) return;
    const county = containingCounty_(site.lon, site.lat, countyGeoJson);
    if (!county) return;
    seen[site.obs] = true;
    site.wfo = county.cwa;
    site.countyFips = county.fips;
    site.countyName = county.countyname;
    if (!site.state) site.state = county.state;
    if (!site.network && site.state) site.network = site.state + '_ASOS';
    site.type = asosIds[site.obs] ? 'ASOS' : (awosIds[site.obs] ? 'AWOS' : 'METAR');
    allSites.push(site);
  });
  allSites.sort(function(a, b) {
    if (a.wfo !== b.wfo) return WFO_IDS.indexOf(a.wfo) - WFO_IDS.indexOf(b.wfo);
    return a.displayId.localeCompare(b.displayId);
  });
  const result = { allSites: allSites, asosAwosSites: allSites.filter(function(s) { return s.type === 'ASOS' || s.type === 'AWOS'; }) };
  try {
    const serialized = JSON.stringify(result);
    if (serialized.length < 95000) cache.put(cacheKey, serialized, 21600);
  } catch (error) {}
  return result;
}

function parseGeoJsonResponse_(response, label, warnings) {
  if (!response || response.getResponseCode() !== 200) {
    warnings.push(label + ' returned HTTP ' + (response ? response.getResponseCode() : 'unknown') + '.');
    return { type: 'FeatureCollection', features: [] };
  }
  try {
    const payload = JSON.parse(response.getContentText());
    return payload && Array.isArray(payload.features) ? payload : { type: 'FeatureCollection', features: [] };
  } catch (error) {
    warnings.push(label + ' could not be parsed: ' + error.message);
    return { type: 'FeatureCollection', features: [] };
  }
}
function featureIdSet_(geo) {
  const set = {};
  (geo.features || []).forEach(function(feature) { const s = stationFromFeature_(feature); if (s) set[s.obs] = true; });
  return set;
}
function stationFromFeature_(feature) {
  if (!feature || !feature.geometry || feature.geometry.type !== 'Point') return null;
  const coords = feature.geometry.coordinates || [];
  const lon = Number(coords[0]), lat = Number(coords[1]);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  const p = feature.properties || {};
  const obs = String(p.sid || p.station || p.id || feature.id || '').trim().toUpperCase();
  if (!obs) return null;
  const displayId = /^[A-Z]{3}$/.test(obs) ? 'K' + obs : obs;
  return {
    obs: obs, displayId: displayId,
    name: String(p.sname || p.name || p.station_name || p.site_name || displayId).trim(),
    lat: lat, lon: lon,
    network: String(p.network || p.iem_network || '').trim(),
    state: String(p.state || p.state_abbr || '').trim().toUpperCase(),
    type: 'METAR', wfo: '', countyFips: '', countyName: ''
  };
}

function assignCountyToSites_(sites, countyGeoJson) {
  (sites || []).forEach(function(site) {
    if (site.countyFips) return;
    const county = containingCounty_(site.lon, site.lat, countyGeoJson);
    if (!county) return;
    site.countyFips = county.fips;
    site.countyName = county.countyname;
    site.wfo = county.cwa;
    if (!site.state) site.state = county.state;
  });
}

function fetchSeasonDailySummaries_(sites, startDate, endDate, warnings) {
  const output = {};
  (sites || []).forEach(function(site) { output[site.obs] = {}; });
  const groups = {};
  (sites || []).forEach(function(site) {
    let network = String(site.network || '').trim();
    if (!network && site.state) network = site.state + '_ASOS';
    if (!network) return;
    if (!groups[network]) groups[network] = [];
    groups[network].push(site);
  });
  const requests = [];
  Object.keys(groups).forEach(function(network) {
    // Chunk station IDs to keep URLs reasonable.
    const chunkSize = 45;
    for (let i = 0; i < groups[network].length; i += chunkSize) {
      const subset = groups[network].slice(i, i + chunkSize);
      requests.push({
        network: network,
        sites: subset,
        request: {
          url: IEM_BASE + '/cgi-bin/request/daily.py?' + [
            'sts=' + encodeURIComponent(startDate),
            'ets=' + encodeURIComponent(endDate),
            'network=' + encodeURIComponent(network),
            'stations=' + encodeURIComponent(subset.map(function(s) { return s.obs; }).join(',')),
            'var=min_temp_f',
            'var=min_feel',
            'format=csv',
            'na=M'
          ].join('&'),
          method: 'get', muteHttpExceptions: true, followRedirects: true,
          headers: { Accept: 'text/csv,text/plain', 'User-Agent': 'Regional-Cold-Season-Viewer/' + BUILD_ID }
        }
      });
    }
  });
  let responses = [];
  try { responses = UrlFetchApp.fetchAll(requests.map(function(r) { return r.request; })); }
  catch (error) { warnings.push('IEM daily-summary requests failed: ' + error.message); return output; }

  responses.forEach(function(response, i) {
    const meta = requests[i];
    if (response.getResponseCode() !== 200) {
      warnings.push('IEM daily summaries for ' + meta.network + ' returned HTTP ' + response.getResponseCode() + '.');
      return;
    }
    parseDailySummaryCsv_(response.getContentText(), output);
  });
  return output;
}

function parseDailySummaryCsv_(text, output) {
  const lines = String(text || '').split(/\r?\n/).filter(function(line) { return line.trim() && line.charAt(0) !== '#'; });
  if (lines.length < 2) return;
  let rows;
  try { rows = Utilities.parseCsv(lines.join('\n')); } catch (error) { return; }
  const headers = rows[0].map(function(v) { return String(v || '').trim().toLowerCase(); });
  const idx = {}; headers.forEach(function(h, i) { idx[h] = i; });
  const stationIdx = idx.station !== undefined ? idx.station : idx.id;
  const dateIdx = idx.date;
  const minTempIdx = idx.min_temp_f !== undefined ? idx.min_temp_f : idx.min_tmpf;
  const minFeelIdx = idx.min_feel;
  if (stationIdx === undefined || dateIdx === undefined) return;
  rows.slice(1).forEach(function(row) {
    const station = cell_(row, stationIdx).toUpperCase();
    const date = cell_(row, dateIdx).substring(0, 10);
    if (!output[station] || !date) return;
    output[station][date] = {
      minTemp: numberOrNull_(cell_(row, minTempIdx)),
      minFeel: minFeelIdx === undefined ? null : numberOrNull_(cell_(row, minFeelIdx))
    };
  });
}

function buildCountyStats_(countyGeoJson, sites, dailyByStation, dates) {
  const byCounty = {};
  (sites || []).forEach(function(site) {
    if (!site.countyFips) return;
    if (!byCounty[site.countyFips]) byCounty[site.countyFips] = [];
    byCounty[site.countyFips].push(site);
  });
  const allSites = sites || [];
  const result = {};
  (countyGeoJson.features || []).forEach(function(feature) {
    const p = feature.properties || {};
    const fips = String(p.fips || '').padStart(5, '0');
    const countyLat = Number(p.lat), countyLon = Number(p.lon);
    let countySites = (byCounty[fips] || []).slice();
    let coverage = 'inside';
    let nearestMiles = null;
    if (!countySites.length && allSites.length && Number.isFinite(countyLat) && Number.isFinite(countyLon)) {
      let best = null;
      allSites.forEach(function(site) {
        const miles = haversineMiles_(countyLat, countyLon, site.lat, site.lon);
        if (!best || miles < best.miles) best = { site: site, miles: miles };
      });
      if (best) { countySites = [best.site]; coverage = 'nearest'; nearestMiles = Math.round(best.miles * 10) / 10; }
    }
    const dailyTemp = [];
    const dailyFeel = [];
    dates.forEach(function(date) {
      let minTemp = null, minFeel = null;
      countySites.forEach(function(site) {
        const d = dailyByStation[site.obs] && dailyByStation[site.obs][date];
        if (!d) return;
        if (isFiniteNumber_(d.minTemp) && (!isFiniteNumber_(minTemp) || d.minTemp < minTemp)) minTemp = d.minTemp;
        if (isFiniteNumber_(d.minFeel) && (!isFiniteNumber_(minFeel) || d.minFeel < minFeel)) minFeel = d.minFeel;
      });
      dailyTemp.push(nullableRound1_(minTemp));
      dailyFeel.push(nullableRound1_(minFeel));
    });
    const criteria = criteriaForCounty_({
      cwa: String(p.cwa || '').toUpperCase(), state: String(p.state || '').toUpperCase(),
      countyname: String(p.countyname || ''), lat: countyLat, lon: countyLon
    });
    result[fips] = {
      fips: fips,
      county: String(p.countyname || ''), state: String(p.state || ''), wfo: String(p.cwa || '').toUpperCase(),
      lat: countyLat, lon: countyLon,
      coverage: coverage, nearestMiles: nearestMiles,
      stations: countySites.map(function(s) { return { id: s.displayId, obs: s.obs, name: s.name, type: s.type, lat: s.lat, lon: s.lon }; }),
      criteria: criteria,
      dailyMinTemp: dailyTemp,
      dailyMinFeel: dailyFeel
    };
  });
  return result;
}

function criteriaForCounty_(county) {
  const wfo = String(county.cwa || '').toUpperCase();
  const state = String(county.state || '').toUpperCase();
  const name = String(county.countyname || '').toUpperCase();
  const lat = Number(county.lat);
  let advisory = null, warning = null, band = '';

  if (wfo === 'MOB') {
    const northAl = ['CHOCTAW','WASHINGTON','CLARKE','WILCOX','BUTLER','CRENSHAW','MONROE','CONECUH','COVINGTON','ESCAMBIA'];
    const north = state === 'MS' || (state === 'AL' && northAl.indexOf(name) >= 0);
    advisory = north ? 20 : 25;
    warning = north ? 10 : 15;
    band = north ? 'MOB northern tier' : 'MOB southern/coastal tier';
  } else if (wfo === 'LIX') {
    const north = Number.isFinite(lat) ? lat >= 30.45 : false;
    advisory = north ? 20 : 25;
    warning = north ? 10 : 15;
    band = north ? 'LIX north of I-10/12 proxy' : 'LIX south/coastal proxy';
  } else if (wfo === 'JAN') {
    let zone = 'south';
    if (state === 'AR') zone = 'central';
    else if (state === 'LA') zone = lat >= 31.6 ? 'central' : 'south';
    else if (lat >= 33.0) zone = 'north';
    else if (lat >= 31.8) zone = 'central';
    if (zone === 'north') { advisory = 10; warning = 0; }
    else if (zone === 'central') { advisory = 15; warning = 5; }
    else { advisory = 20; warning = 10; }
    band = 'JAN ' + zone + ' tier';
  } else if (wfo === 'BMX') {
    advisory = 0;
    warning = -10;
    band = 'BMX Central Alabama';
  } else if (wfo === 'TAE') {
    const florida = state === 'FL';
    advisory = florida ? 24 : 19;
    warning = florida ? 14 : 9;
    band = florida ? 'TAE Florida tier' : 'TAE Alabama/Georgia tier';
  }

  return {
    frostProxy: 36,
    freeze: 32,
    hardFreeze: 25,
    coldAdvisory: advisory,
    extremeCold: warning,
    bandLabel: band
  };
}

function fetchSeasonColdHazardsKml_(startDate, endDate) {
  const start = new Date(startDate + 'T00:00:00Z');
  const lookback = new Date(start.getTime() - 7 * 86400000);
  const endExclusive = new Date(addDaysToDateString_(endDate, 1) + 'T00:00:00Z');
  // Modern products + legacy Wind Chill / Hard Freeze for older seasons.
  const phenomena = 'FR,FZ,FZ,CW,EC,EC,WC,WC,WC,HZ,HZ';
  const significance = 'Y,A,W,Y,A,W,Y,A,W,A,W';
  const url = IEM_BASE + '/cgi-bin/request/gis/watchwarn.py?' + [
    'accept=kml',
    'sts=' + encodeURIComponent(formatUtcIso_(lookback)),
    'ets=' + encodeURIComponent(formatUtcIso_(endExclusive)),
    'wfo=' + encodeURIComponent(WFO_IDS.join(',')),
    'limitps=1',
    'phenomena=' + encodeURIComponent(phenomena),
    'significance=' + encodeURIComponent(significance),
    'simple=yes',
    'addsvs=yes'
  ].join('&');
  const response = UrlFetchApp.fetch(url, { method: 'get', muteHttpExceptions: true, followRedirects: true, headers: { Accept: 'application/vnd.google-earth.kml+xml,application/xml,text/xml', 'User-Agent': 'Regional-Cold-Season-Viewer/' + BUILD_ID } });
  if (response.getResponseCode() !== 200) throw new Error('IEM VTEC archive HTTP ' + response.getResponseCode() + ': ' + responsePreview_(response.getContentText()));
  return response.getContentText();
}

function containingCounty_(lon, lat, geo) {
  const features = geo && geo.features ? geo.features : [];
  for (let i = 0; i < features.length; i++) {
    if (featureContainsPoint_(features[i], lon, lat)) {
      const p = features[i].properties || {};
      return { fips: String(p.fips || '').padStart(5, '0'), countyname: String(p.countyname || ''), state: String(p.state || '').toUpperCase(), cwa: String(p.cwa || '').toUpperCase() };
    }
  }
  return null;
}
function featureContainsPoint_(feature, lon, lat) {
  if (!feature || !feature.geometry) return false;
  if (feature.geometry.type === 'Polygon') return polygonContainsPoint_(feature.geometry.coordinates, lon, lat);
  if (feature.geometry.type === 'MultiPolygon') return (feature.geometry.coordinates || []).some(function(p) { return polygonContainsPoint_(p, lon, lat); });
  return false;
}
function polygonContainsPoint_(rings, lon, lat) {
  if (!rings || !rings.length || !pointInRing_(lon, lat, rings[0])) return false;
  for (let i = 1; i < rings.length; i++) if (pointInRing_(lon, lat, rings[i])) return false;
  return true;
}
function pointInRing_(x, y, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
    const hit = ((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / ((yj - yi) || 1e-12) + xi);
    if (hit) inside = !inside;
  }
  return inside;
}
function haversineMiles_(lat1, lon1, lat2, lon2) {
  const R = 3958.7613, rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad, dLon = (lon2 - lon1) * rad;
  const a = Math.sin(dLat/2) ** 2 + Math.cos(lat1*rad) * Math.cos(lat2*rad) * Math.sin(dLon/2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}
function dateRange_(start, end) {
  const out = [];
  let d = start;
  while (d <= end) { out.push(d); d = addDaysToDateString_(d, 1); }
  return out;
}
function addDaysToDateString_(dateStr, days) {
  const p = dateStr.split('-').map(Number);
  const d = new Date(Date.UTC(p[0], p[1]-1, p[2]+days));
  return Utilities.formatDate(d, 'UTC', 'yyyy-MM-dd');
}
function formatUtcIso_(date) { return Utilities.formatDate(date, 'UTC', "yyyy-MM-dd'T'HH:mm:ss'Z'"); }
function cell_(row, index) { return index === undefined || index === null ? '' : String(row[index] === undefined || row[index] === null ? '' : row[index]).trim(); }
function numberOrNull_(value) { if (value === '' || value === null || value === undefined || value === 'M' || value === 'null') return null; const n = Number(value); return Number.isFinite(n) ? n : null; }
function isFiniteNumber_(value) { return typeof value === 'number' && Number.isFinite(value); }
function nullableRound1_(value) { return isFiniteNumber_(value) ? Math.round(value * 10) / 10 : null; }
function responsePreview_(text) { return String(text || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().substring(0, 260) || 'No response body'; }

function getBuildInfo() { return { buildId: BUILD_ID, dataMode: DATA_MODE, wfos: WFO_IDS }; }
function authorizeAndTest() {
  const config = getInitialConfig();
  const data = getSeasonData({ seasonStartYear: config.defaultSeasonStartYear, stationMode: 'asosawos' });
  const summary = { buildId: data.buildId, season: data.season.label, counties: Object.keys(data.countyStats || {}).length, stations: data.stationCount, warnings: data.warnings };
  console.log(JSON.stringify(summary, null, 2));
  return summary;
}

return { doGet: doGet, getInitialConfig: getInitialConfig, getSeasonData: getSeasonData, getBuildInfo: getBuildInfo, authorizeAndTest: authorizeAndTest };
})();

function doGet() { return COLD_SEASON_R1.doGet(); }
function coldSeasonR1_getInitialConfig() { return COLD_SEASON_R1.getInitialConfig(); }
function coldSeasonR1_getSeasonData(request) { return COLD_SEASON_R1.getSeasonData(request); }
function coldSeasonR1_getBuildInfo() { return COLD_SEASON_R1.getBuildInfo(); }
function coldSeasonR1_authorizeAndTest() { return COLD_SEASON_R1.authorizeAndTest(); }