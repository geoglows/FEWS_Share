// Entry point. Vite bundles these imports — no CDN <script>/<link> tags needed.
import "./style.css" // Tailwind + custom theme + Leaflet overrides
import "leaflet/dist/leaflet.css" // Leaflet's own stylesheet (from npm)
import "iconify-icon" // registers the <iconify-icon> web component
import L from "leaflet";

(function () {
  "use strict";

  const SEVERITY = {
    warning: {rank: 1, color: "#ffd21f", label: "Warning"},
    danger: {rank: 2, color: "#ff8c00", label: "Danger"},
    extreme: {rank: 3, color: "#e0201b", label: "Extreme"},
  };
  const DEFAULT_COLOR = "#4da3ff";

  const RES_START_ZOOM = 3;
  const RES_ZOOM_STEP = 2;

  function sevColor(s) {
    const k = (s || "").toLowerCase();
    return SEVERITY[k] ? SEVERITY[k].color : DEFAULT_COLOR;
  }

  const FIELD_LABELS = [
    ["severity", "Severity"],
    ["riverId", "Gauge / River ID"],
    ["country", "Country"],
    ["returnPeriodYr", "Return period"],
    ["peakDischargeCms", "Mean discharge"], //this will need to be changed into peak, the info from geoglows is in mean discharge.
    ["issuedTime", "Issued"],
    ["startTime", "Start"],
    ["peakTime", "Peak"],
    ["endTime", "End"],
    ["historicalComparison", "Historical"],
  ];

  // Every pipeline stamps the FeatureCollection with a `kind`, so all the
  // user-facing wording (readout, attribution, panel copy) comes from one place
  // instead of being hardcoded to H3.
  const DATASETS = {
    "basins-telescoping": {
      unit: "Basin",
      resLabel: "Basin level",
      attribution: "HydroBASINS",
      emptyTitle: "No basin selected",
      emptyBody: "Click a highlighted basin on the map to see every forecast inside it.",
      pipeline: ["csv_to_json_basins.py", "build_basins.py"],
    },
    "h3-telescoping": {
      unit: "Cell",
      resLabel: "H3 res",
      attribution: "Grid: H3 (Uber H3)",
      emptyTitle: "No cell selected",
      emptyBody: "Click a highlighted grid cell on the map to see every forecast inside it.",
      pipeline: ["csv_to_json_vgrid.py", "build_cells_h3.py"],
    },
    "s2-telescoping": {
      unit: "Cell",
      resLabel: "S2 level",
      attribution: "Grid: S2 (Google S2)",
      emptyTitle: "No cell selected",
      emptyBody: "Click a highlighted grid cell on the map to see every forecast inside it.",
      pipeline: ["csv_to_json_vgrid.py", "build_cells_s2.py"],
    },
  };

  // Until the data lands we don't know which build it is; stay neutral.
  let dataset = {
    unit: "Area", resLabel: "Level", attribution: "",
    emptyTitle: "No area selected",
    emptyBody: "Click a highlighted area on the map to see every forecast inside it.",
    pipeline: ["csv_to_json_vgrid.py", "build_cells_h3.py"],
  };

  function unitLabel(props) {
    // Fall back to the per-feature tag if a file predates the `kind` member.
    if (props && props.basin_id && dataset.unit === "Area") return "Basin";
    return dataset.unit;
  }

  // Compact counts for the impact tiles: 8_181_280 -> "8.2M".
  function fmtCount(n) {
    if (n === null || n === undefined || !isFinite(n)) return "—";
    if (n >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
    return Math.round(n).toLocaleString();
  }

  function fmtValue(key, val) {
    if (val === undefined || val === null || val === "") return "—";
    if (key === "returnPeriodYr") return val + "-year";
    if (key === "peakDischargeCms") return val + " m³/s";
    if (key.endsWith("Time")) {
      const d = new Date(val);
      if (!isNaN(d)) {
        return d.toLocaleString(undefined, {
          month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
          hour12: false, timeZoneName: "short",
        });
      }
    }
    return String(val);
  }

  // A single copy of the world. The data only exists once, so we clamp panning to
  // one world width; without this Leaflet lets you scroll into empty repeated
  // tiles where no cells/basins are drawn. `worldCopyJump:false` keeps us from
  // teleporting across copies; the tile layers get `noWrap:true` below so the
  // basemap doesn't repeat either.
  const WORLD_BOUNDS = L.latLngBounds([-85, -180], [85, 180]);
  const map = L.map("map", {
    zoomControl: true,
    minZoom: 2,
    maxZoom: 20,
    worldCopyJump: false,
    maxBounds: WORLD_BOUNDS,
    maxBoundsViscosity: 1.0,
  });
  map.setView([20, 0], 2);

  // Tile layers carry only their own imagery credit. The grid/basin credit is a
  // separate attribution entry so it survives base-layer switches and can be
  // swapped once we know what the data actually is.
  const googleAttr = "Imagery &copy; Google";
  const googleSub = ["mt0", "mt1", "mt2", "mt3"];

  const baseLayers = {
    "Google Hybrid": L.tileLayer("https://{s}.google.com/vt/lyrs=y&x={x}&y={y}&z={z}", {
      subdomains: googleSub, maxZoom: 20, attribution: googleAttr, noWrap: true, bounds: WORLD_BOUNDS,
    }),
    "Google Satellite": L.tileLayer("https://{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}", {
      subdomains: googleSub, maxZoom: 20, attribution: googleAttr, noWrap: true, bounds: WORLD_BOUNDS,
    }),
    "OpenStreetMap": L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors · Grid: H3",
    }),
  };

  let gridAttribution = null;

  function setGridAttribution(text) {
    if (!map.attributionControl) return;
    if (gridAttribution) map.attributionControl.removeAttribution(gridAttribution);
    gridAttribution = text || null;
    if (gridAttribution) map.attributionControl.addAttribution(gridAttribution);
  }

  baseLayers["Google Hybrid"].addTo(map);
  L.control.layers(baseLayers, null, {position: "topright", collapsed: false}).addTo(map);

  const resControl = L.control({position: "bottomleft"});
  resControl.onAdd = function () {
    const div = L.DomUtil.create("div", "");
    div.style.cssText =
      "background:rgba(255,255,255,.92);padding:3px 9px;border-radius:6px;" +
      "font:600 12px system-ui,sans-serif;color:#0f172a;box-shadow:0 1px 4px rgba(0,0,0,.35)";
    div.id = "res-readout";
    div.textContent = dataset.resLabel + " —";
    return div;
  };
  resControl.addTo(map);

  function updateResReadout(res) {
    const el = document.getElementById("res-readout");
    if (el) el.textContent = dataset.resLabel + " " + res;
  }

  const legendEl = document.getElementById("legend");
  legendEl.innerHTML = Object.keys(SEVERITY)
      .filter((k) => k !== "none")
      .map((k) => `<span class="flex items-center gap-1.5 text-xs font-semibold text-slate-800">
        <span class="w-3.5 h-3.5 rounded-sm border border-black/30" style="background:${SEVERITY[k].color}"></span>${SEVERITY[k].label}
      </span>`)
      .join("") +
    `<span class="flex items-center gap-1.5 text-xs font-semibold text-slate-800">
        <span class="w-3.5 h-3.5 rounded-sm bg-white border-2 border-slate-900"></span>Multi-model
      </span>`;

  const panelEmpty = document.getElementById("panel-empty");
  const panelContent = document.getElementById("panel-content");

  function showCell(props) {
    panelEmpty.hidden = true;
    panelContent.hidden = false;

    const worst = (props.severity || "").toLowerCase();
    const worstColor = sevColor(worst);

    const badge = (sev, color) =>
      `<span class="inline-block px-2.5 py-0.5 rounded-full text-[11px] font-semibold capitalize text-[#10161d]" style="background:${color}">${sev || "—"}</span>`;

    const cards = props.forecasts.map((fc) => {
      const rows = FIELD_LABELS
        .filter(([k]) => k !== "historicalComparison")
        .map(([k, label]) => {
          const dt = `<dt class="text-slate-400">${label}</dt>`;
          if (k === "severity") {
            return `${dt}<dd class="m-0">${badge(fc.severity, sevColor(fc.severity))}</dd>`;
          }
          return `${dt}<dd class="m-0 text-slate-100 break-words">${fmtValue(k, fc[k])}</dd>`;
        })
        .join("");
      const note = fc.historicalComparison
        ? `<div class="flex items-start gap-1.5 text-xs text-slate-400 italic mt-2">
             <iconify-icon icon="heroicons:clock" class="text-sm mt-0.5 not-italic shrink-0"></iconify-icon>
             <span>“${fc.historicalComparison}”</span>
           </div>` : "";
      return `
        <div class="bg-[#1b2a3a] border border-slate-700 border-l-4 rounded-[10px] px-3.5 py-3 mb-3" style="border-left-color:${sevColor(fc.severity)}">
          <div class="flex items-center justify-between mb-2">
            <span class="flex items-center gap-1.5 font-semibold text-[13px] capitalize text-slate-100">
              <iconify-icon icon="heroicons:chart-bar" class="text-sky-300"></iconify-icon>
              ${modelLabel(fc.model || "model")}
            </span>
          </div>
          <dl class="grid grid-cols-[128px_1fr] gap-x-2.5 gap-y-1 text-[12.5px]">${rows}</dl>
          ${note}
        </div>`;
    }).join("");

    const imp = props.impact;
    const tile = (icon, label, value, span) => `
      <div class="${span ? "col-span-2 " : ""}rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-2">
        <div class="flex items-center gap-1 text-slate-400 text-[10px] font-semibold uppercase tracking-wide mb-1">
          <iconify-icon icon="${icon}" class="text-[12px]"></iconify-icon>${label}
        </div>
        <div class="text-slate-800 font-bold text-[15px] leading-none">${value}</div>
      </div>`;
    const impactHtml = imp ? `
      <div class="mt-4 pt-3 border-t border-slate-200">
        <h3 class="flex items-center gap-1.5 text-slate-800 font-semibold text-[11px] uppercase tracking-wider mb-2">
          <iconify-icon icon="heroicons:exclamation-triangle" class="text-amber-500 text-sm"></iconify-icon>Impact
        </h3>
        <div class="grid grid-cols-2 gap-2">
          ${tile("heroicons:building-office-2", "Buildings", fmtCount(imp.buildings))}
          ${tile("lucide:wheat", "Farmland", fmtCount(imp.farmland_m2 / 1e6) + " km²")}
          ${tile("lucide:route", "Roads", fmtCount(imp.highway_km) + " km")}
          ${tile("lucide:train-front", "Railways", fmtCount(imp.railway_km) + " km")}
          ${tile("heroicons:users", "Population", fmtCount(imp.population), true)}
        </div>
        <p class="text-slate-400 text-[10px] mt-1.5">Totals across the whole basin.</p>
      </div>` : "";

    const nModels = (props.models || []).length;
    const confidence = nModels >= 2
      ? `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-semibold bg-emerald-100 text-emerald-800">
           <iconify-icon icon="heroicons:check-badge"></iconify-icon>${nModels} models agree</span>`
      : `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-semibold bg-slate-100 text-slate-500">Single model</span>`;

    panelContent.innerHTML = `
      <h2 class="flex items-center gap-2 text-slate-800 font-semibold text-[15px] mb-0.5">
        <iconify-icon icon="heroicons:squares-2x2" style="color:${worstColor}"></iconify-icon>
        ${unitLabel(props)} ${props.cell_id}
        ${badge(worst, worstColor)}
      </h2>
      <div class="flex items-center gap-2 mb-3.5">
        ${confidence}
        <span class="text-slate-500 text-xs">${props.model_count} forecast${props.model_count === 1 ? "" : "s"} · worst-case above</span>
      </div>
      ${cards}
      ${impactHtml}`;
  }

  let resolutions = [];
  const byRes = {};

  // ---- Model filter (toggle at the top of the panel) ------------------------
  const visibleModels = new Set();
  const MODEL_LABELS = {flood_hub: "Flood Hub", geoglows: "GEOGLOWS"};

  function modelLabel(m) {
    return MODEL_LABELS[m] || m.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function worstSeverity(forecasts) {
    let best = "", bestRank = -1;
    for (const fc of forecasts) {
      const info = SEVERITY[(fc.severity || "").toLowerCase()];
      const r = info ? info.rank : -1;
      if (r > bestRank) {
        bestRank = r;
        best = (fc.severity || "").toLowerCase();
      }
    }
    return best;
  }

  // Keep only forecasts from visible models; drop empty cells; recolour the rest.
  function visibleFeatures(features) {
    const out = [];
    for (const f of features) {
      const fcs = f.properties.forecasts.filter((x) => visibleModels.has(x.model));
      if (!fcs.length) continue;
      const modelNames = [...new Set(fcs.map((x) => x.model))];
      out.push({
        type: "Feature",
        geometry: f.geometry,
        properties: Object.assign({}, f.properties, {
          forecasts: fcs, model_count: fcs.length, severity: worstSeverity(fcs),
          models: modelNames, agree: modelNames.length >= 2,
        }),
      });
    }
    return out;
  }

  function baseStyle(feature) {
    const p = feature.properties;
    const c = sevColor(p.severity);
    // Colour = severity; outline = model agreement. Multi-model cells get a bold
    // dark ring + more opaque fill so higher-confidence areas stand out.
    if (p.agree) {
      return {color: "#0f172a", weight: 2.5, opacity: 1, fillColor: c, fillOpacity: 0.3};
    }
    return {color: c, weight: 1, opacity: 0.85, fillColor: c, fillOpacity: 0.3};
  }

  const HOVER_STYLE = {weight: 3, fillOpacity: 0.6};
  // Selection reads as "solid": the white ring plus a near-opaque fill lifts the
  // clicked basin off the basemap. This matters most for multi-model basins,
  // whose resting fill is already 0.6 — without a bump, clicking barely changed them.
  const SELECT_STYLE = {weight: 4, fillOpacity: 0.2, color: "#ffffff"};

  let selected = null;
  let selectedGroup = null;

  function clearSelection() {
    if (selected && selectedGroup) selectedGroup.resetStyle(selected);
    selected = null;
    selectedGroup = null;
    panelContent.hidden = true;
    panelEmpty.hidden = false;
  }

  function bindFeature(feature, lyr, getGroup) {
    const p = feature.properties;
    lyr.bindTooltip(
      `${unitLabel(p)} ${p.cell_id} · <b>${p.severity || "?"}</b> (${p.model_count} forecast${p.model_count === 1 ? "" : "s"})` +
      (p.agree ? ` · ✓ ${p.models.length} models` : ""),
      {sticky: true}
    );
    lyr.on({
      mouseover: () => {
        if (lyr !== selected) lyr.setStyle(HOVER_STYLE);
      },
      mouseout: () => {
        if (lyr !== selected) getGroup().resetStyle(lyr);
      },
      click: (e) => {
        L.DomEvent.stopPropagation(e);
        if (selected && selectedGroup) selectedGroup.resetStyle(selected);
        selected = lyr;
        selectedGroup = getGroup();
        lyr.setStyle(SELECT_STYLE);
        showCell(p);
        map.fitBounds(lyr.getBounds(), {maxZoom: 12, padding: [40, 40]});
      },
    });
  }

  const layers = {};

  function layerFor(res) {
    if (layers[res]) return layers[res];
    let group;
    group = L.geoJSON(
      {type: "FeatureCollection", features: visibleFeatures(byRes[String(res)].features)},
      {
        style: baseStyle,
        onEachFeature: (feature, lyr) => bindFeature(feature, lyr, () => group),
      }
    );
    layers[res] = group;
    return group;
  }

  function zoomToRes(zoom) {
    let chosen = resolutions[0];
    resolutions.forEach((r, i) => {
      if (zoom >= RES_START_ZOOM + i * RES_ZOOM_STEP) chosen = r;
    });
    return chosen;
  }

  let currentRes = null;
  let activeLayer = null;

  function showRes(res) {
    if (res === currentRes) return;
    if (activeLayer) map.removeLayer(activeLayer);
    clearSelection();
    activeLayer = layerFor(res).addTo(map);
    currentRes = res;
    updateResReadout(res);
  }

  // Rebuild the current layer after a model toggle (cached layers are now stale).
  function refreshLayers() {
    for (const k in layers) delete layers[k];
    const res = currentRes;
    if (activeLayer) {
      map.removeLayer(activeLayer);
      activeLayer = null;
    }
    currentRes = null;
    if (res != null) showRes(res);
  }

  function renderModelToggle(models) {
    const el = document.getElementById("model-toggle");
    if (!el || models.length < 2) {
      if (el) el.innerHTML = "";
      return;
    }
    el.innerHTML =
      '<div class="relative mb-3 pb-3 border-b border-slate-200">' +
      '  <button id="model-dd-btn" type="button" class="w-full flex items-center justify-between gap-2 px-3 py-2 rounded-lg border border-slate-300 bg-white text-[13px] font-medium text-slate-700 hover:bg-slate-50">' +
      '    <span class="flex items-center gap-1.5"><iconify-icon icon="heroicons:funnel" class="text-sky-500"></iconify-icon><span id="model-dd-label">All models</span></span>' +
      '    <iconify-icon id="model-dd-caret" icon="heroicons:chevron-down" class="text-slate-400 transition-transform"></iconify-icon>' +
      '  </button>' +
      '  <div id="model-dd-menu" class="hidden absolute z-[1000] left-0 right-0 mt-1 rounded-lg border border-slate-200 bg-white shadow-lg p-1.5">' +
      models.map((m) =>
        `<label class="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-slate-50 text-[13px] text-slate-700 cursor-pointer select-none">` +
        `<input type="checkbox" data-model="${m}" checked class="accent-sky-500 w-3.5 h-3.5">${modelLabel(m)}</label>`
      ).join("") +
      "  </div></div>";

    const root = el.firstElementChild;
    const btn = document.getElementById("model-dd-btn");
    const menu = document.getElementById("model-dd-menu");
    const caret = document.getElementById("model-dd-caret");
    const label = document.getElementById("model-dd-label");

    function updateLabel() {
      const n = models.filter((m) => visibleModels.has(m)).length;
      label.textContent = n === models.length ? "All models"
        : n === 0 ? "No models" : n + " of " + models.length + " models";
    }

    function closeMenu() {
      menu.classList.add("hidden");
      caret.style.transform = "";
    }

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = menu.classList.toggle("hidden");
      caret.style.transform = open ? "" : "rotate(180deg)";
    });
    document.addEventListener("click", (e) => {
      if (!root.contains(e.target)) closeMenu();
    });

    el.querySelectorAll("input[type=checkbox]").forEach((cb) => {
      cb.addEventListener("change", () => {
        if (cb.checked) visibleModels.add(cb.dataset.model);
        else visibleModels.delete(cb.dataset.model);
        updateLabel();
        refreshLayers();
      });
    });
    updateLabel();
  }

  function buildFromGeojson(geo) {
    // Resolve the wording before anything renders.
    if (geo && DATASETS[geo.kind]) dataset = DATASETS[geo.kind];
    setGridAttribution(dataset.attribution);

    const feats = (geo && geo.features) || [];
    const grouped = {};
    for (const f of feats) {
      const r = (f.properties && f.properties.res != null) ? f.properties.res : 0;
      (grouped[r] = grouped[r] || []).push(f);
    }
    resolutions = (Array.isArray(geo.resolutions) && geo.resolutions.length
      ? geo.resolutions.map(Number)
      : Object.keys(grouped).map(Number)).sort((a, b) => a - b);
    for (const r of resolutions) {
      byRes[String(r)] = {type: "FeatureCollection", features: grouped[r] || []};
    }

    if (!resolutions.some((r) => byRes[String(r)].features.length)) {
      document.getElementById("panel-empty").innerHTML =
        `<h2>No ${dataset.unit.toLowerCase()} data</h2><p>Run ` +
        `<code>python ${dataset.pipeline[0]}</code> then ` +
        `<code>python ${dataset.pipeline[1]}</code> to generate <code>data.geojson</code>.</p>`;
      return;
    }

    const emptyH = document.querySelector("#panel-empty h2");
    const emptyP = document.querySelector("#panel-empty p");
    if (emptyH) emptyH.textContent = dataset.emptyTitle;
    if (emptyP) emptyP.textContent = dataset.emptyBody;

    const models = new Set();
    for (const f of byRes[String(resolutions[0])].features) {
      for (const fc of f.properties.forecasts) if (fc.model) models.add(fc.model);
    }
    models.forEach((m) => visibleModels.add(m));
    renderModelToggle([...models].sort());

    map.on("zoomend", () => showRes(zoomToRes(map.getZoom())));
    map.on("click", clearSelection);

    showRes(resolutions[0]);
    map.fitBounds(activeLayer.getBounds(), {padding: [40, 40], maxZoom: 6});
    showRes(zoomToRes(map.getZoom()));
  }

  // data_basins.geojson or data_hexagons.geojson
  fetch("https://d3hbj0z0f67zhd.cloudfront.net/fews4all/data_basins.geojson")
    .then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(buildFromGeojson)
    .catch((err) => {
      document.getElementById("panel-empty").innerHTML =
        "<h2 class=\"text-slate-800 font-semibold text-[15px] mb-1\">Couldn't load forecast data</h2>" +
        "<p class=\"text-sm leading-relaxed max-w-[240px]\">" +
        String(err && err.message ? err.message : err) +
        "</p>";
    });
})();
