/* Spar Spar Schiene – Frontend */

const state = {
  from: null,   // {id, name, lat, lon}
  to: null,
  jobId: null,
  evtSource: null,
  results: [],
  lastSeq: -1,
  extraOrigins: [],
  extraDests: [],
  sollSelected: new Set(),  // Keys der angehakten Soll-Verbindungen
};

const sollKey = (trains) => trains.join("+");

// ---------- Karte ----------

const map = L.map("map").setView([47.8, 13.5], 7);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "&copy; OpenStreetMap",
}).addTo(map);

const mapLayers = {
  markers: L.layerGroup().addTo(map),
  circles: L.layerGroup().addTo(map),
  candidates: L.layerGroup().addTo(map),
  hits: L.layerGroup().addTo(map),
};

function drawHalfCircle(center, other, radiusKm, color) {
  // Halbkreis um center, weggedreht von other
  const toRad = (d) => (d * Math.PI) / 180;
  const cosLat = Math.cos(toRad(center.lat));
  // Winkel Richtung other
  const dx = (other.lon - center.lon) * cosLat;
  const dy = other.lat - center.lat;
  const away = Math.atan2(dy, dx) + Math.PI; // Gegenrichtung
  const pts = [[center.lat, center.lon]];
  const rLat = radiusKm / 111.32;
  for (let i = -90; i <= 90; i += 6) {
    const ang = away + toRad(i);
    const lat = center.lat + rLat * Math.sin(ang);
    const lon = center.lon + (rLat * Math.cos(ang)) / cosLat;
    pts.push([lat, lon]);
  }
  pts.push([center.lat, center.lon]);
  L.polygon(pts, { color, weight: 1, fillOpacity: 0.12 }).addTo(mapLayers.circles);
}

function updateMap() {
  mapLayers.markers.clearLayers();
  mapLayers.circles.clearLayers();
  if (state.from) {
    L.marker([state.from.lat, state.from.lon]).addTo(mapLayers.markers)
      .bindPopup("Einstieg: " + state.from.name);
  }
  if (state.to) {
    L.marker([state.to.lat, state.to.lon]).addTo(mapLayers.markers)
      .bindPopup("Ziel: " + state.to.name);
  }
  if (state.from && state.to) {
    drawHalfCircle(state.from, state.to, +radiusStart.value, "#e2002a");
    drawHalfCircle(state.to, state.from, +radiusDest.value, "#0057b8");
    map.fitBounds(L.latLngBounds(
      [state.from.lat, state.from.lon], [state.to.lat, state.to.lon]).pad(0.6));
  }
  scanBtn.disabled = !(state.from && state.to);
}

// ---------- Autocomplete ----------

function setupSuggest(input, sugg, onInput, onPick) {
  let timer = null;
  input.addEventListener("input", () => {
    onInput();
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { sugg.style.display = "none"; return; }
    timer = setTimeout(async () => {
      try {
        const res = await fetch("/api/stations?q=" + encodeURIComponent(q));
        const list = await res.json();
        sugg.innerHTML = "";
        list.slice(0, 10).forEach((s) => {
          const div = document.createElement("div");
          div.textContent = s.name;
          div.onclick = () => {
            sugg.style.display = "none";
            onPick(s);
          };
          sugg.appendChild(div);
        });
        sugg.style.display = list.length ? "block" : "none";
      } catch (e) { /* ignore */ }
    }, 250);
  });
  input.addEventListener("blur", () => setTimeout(() => (sugg.style.display = "none"), 250));
}

function setupAutocomplete(inputId, suggId, key) {
  const input = document.getElementById(inputId);
  setupSuggest(input, document.getElementById(suggId),
    () => { state[key] = null; scanBtn.disabled = true; },
    (s) => { state[key] = s; input.value = s.name; updateMap(); });
}

setupAutocomplete("from-input", "from-sugg", "from");
setupAutocomplete("to-input", "to-sugg", "to");

// ---------- Extra-Haltestellen (auch Bus) ----------

function setupExtraStations(inputId, suggId, chipsId, listKey) {
  const input = document.getElementById(inputId);
  const chips = document.getElementById(chipsId);

  const render = () => {
    chips.innerHTML = "";
    state[listKey].forEach((s, i) => {
      const span = document.createElement("span");
      span.className = "chip";
      span.textContent = s.name + " ✕";
      span.title = "Entfernen";
      span.onclick = () => { state[listKey].splice(i, 1); render(); };
      chips.appendChild(span);
    });
  };

  setupSuggest(input, document.getElementById(suggId),
    () => {},
    (s) => {
      if (!state[listKey].some((x) => x.id === s.id)) state[listKey].push(s);
      input.value = "";
      render();
    });
}

setupExtraStations("extra-origin-input", "extra-origin-sugg", "extra-origin-chips", "extraOrigins");
setupExtraStations("extra-dest-input", "extra-dest-sugg", "extra-dest-chips", "extraDests");

// ---------- Formular ----------

const scanBtn = document.getElementById("scan-btn");
const cancelBtn = document.getElementById("cancel-btn");
const radiusStart = document.getElementById("radius-start");
const radiusDest = document.getElementById("radius-dest");
const dateInput = document.getElementById("date-input");

const tomorrow = new Date(Date.now() + 86400000);
dateInput.value = tomorrow.toISOString().slice(0, 10);

radiusStart.oninput = () => {
  document.getElementById("radius-start-val").textContent = radiusStart.value;
  updateMap();
};
radiusDest.oninput = () => {
  document.getElementById("radius-dest-val").textContent = radiusDest.value;
  updateMap();
};

document.getElementById("sort-mode").onchange = () => renderResults();

// ---------- Scan ----------

scanBtn.onclick = async () => {
  const body = {
    from: state.from,
    to: state.to,
    datetime: dateInput.value + "T" + document.getElementById("time-input").value,
    radiusStart: +radiusStart.value,
    radiusDest: +radiusDest.value,
    extraOrigins: state.extraOrigins,
    extraDests: state.extraDests,
    autoBus: document.getElementById("auto-bus").checked,
    comboMode: document.getElementById("combo-mode").value,
  };
  const res = await fetch("/api/scan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!data.jobId) { alert("Fehler: " + JSON.stringify(data)); return; }

  state.jobId = data.jobId;
  notifyApp("scanStarted", data.jobId);
  state.results = [];
  state.lastSeq = -1;
  document.querySelectorAll(".result-group").forEach((g) => {
    g.style.display = "none";
    g.querySelector("tbody").innerHTML = "";
  });
  document.getElementById("soll-table").querySelector("tbody").innerHTML = "";
  document.getElementById("log").textContent = "";
  document.getElementById("phase-label").textContent = "…";
  document.getElementById("progress-fill").style.width = "0%";
  document.getElementById("progress-text").textContent = "";
  state.sollSelected = new Set();
  document.getElementById("progress-card").style.display = "";
  document.getElementById("results-card").style.display = "none";
  document.getElementById("soll-card").style.display = "none";
  mapLayers.candidates.clearLayers();
  mapLayers.hits.clearLayers();
  scanBtn.disabled = true;
  cancelBtn.style.display = "";
  document.querySelectorAll(".board-name").forEach((e) => (e.textContent = state.from.name));
  document.querySelectorAll(".alight-name").forEach((e) => (e.textContent = state.to.name));

  connectEvents();
};

cancelBtn.onclick = async () => {
  if (state.jobId) await fetch(`/api/scan/${state.jobId}/cancel`, { method: "POST" });
};

function connectEvents() {
  if (state.evtSource) state.evtSource.close();
  // Ab dem letzten gesehenen Event weiterlesen, sonst gibt es beim Reconnect Duplikate
  const es = new EventSource(`/api/scan/${state.jobId}/events?seq=${state.lastSeq + 1}`);
  state.evtSource = es;
  es.onmessage = (m) => handleEvent(JSON.parse(m.data));
  es.onerror = () => {
    // Verbindung weg -> Snapshot holen und ggf. neu verbinden
    es.close();
    fetch(`/api/scan/${state.jobId}`).then((r) => {
      if (r.status === 404) {
        // Server wurde neu gestartet (z.B. Render-Hosting): Job existiert nur im
        // RAM und ist weg. Ohne diese Pruefung wuerde die App endlos reconnecten.
        logLine("FEHLER: Der Server wurde zwischenzeitlich neu gestartet – der laufende Scan ging verloren. Bitte Scan erneut starten.");
        document.getElementById("phase-label").textContent = "⚠️ Scan verloren (Server-Neustart)";
        finishScan();
        return null;
      }
      return r.json();
    }).then((snap) => {
      if (!snap) return;
      if (snap.finished) {
        // Scan wurde waehrend der Unterbrechung fertig: Endstand uebernehmen
        if (Array.isArray(snap.results)) {
          state.results = snap.results;
          renderResults();
        }
        if (snap.error) logLine("FEHLER: " + snap.error);
        document.getElementById("phase-label").textContent =
          snap.error ? "⚠️ Scan beendet mit Fehler" : "✅ Scan abgeschlossen";
        finishScan();
      } else {
        setTimeout(connectEvents, 1500);
      }
    }).catch(() => setTimeout(connectEvents, 3000));
  };
}

// ---------- Events ----------

function logLine(msg) {
  const log = document.getElementById("log");
  log.textContent += msg + "\n";
  log.scrollTop = log.scrollHeight;
}

function fmtTime(iso) {
  return iso ? iso.slice(11, 16) : "";
}
function fmtDate(iso) {
  return iso ? iso.slice(8, 10) + "." + iso.slice(5, 7) + "." : "";
}
function fmtPrice(p) {
  return "€ " + p.toFixed(2).replace(".", ",");
}
function fmtEta(sec, isMin) {
  if (sec == null) return "";
  const txt = sec < 90 ? `~${Math.max(10, Math.round(sec / 10) * 10)} s`
                       : `~${Math.round(sec / 60)} min`;
  return ` · noch ${isMin ? "mind. " : ""}${txt}`;
}

// ---------- Debug-Info kopieren ----------

function buildDebugInfo() {
  const g = (id) => (document.getElementById(id) || {}).textContent || "";
  const results = { A: 0, B: 0, C: 0 };
  state.results.forEach((r) => { if (results[r.phase] != null) results[r.phase]++; });
  return [
    "=== Spar Spar Schiene – Debug-Info ===",
    "Zeit: " + new Date().toISOString(),
    "Adresse: " + location.href,
    "App-Brücke (Android): " + (window.SparApp ? "ja" : "nein (Browser)"),
    "Gerät: " + navigator.userAgent,
    "Von: " + (state.from ? `${state.from.name} (${state.from.id})` : "–"),
    "Nach: " + (state.to ? `${state.to.name} (${state.to.id})` : "–"),
    "Datum/Abfahrt: " + dateInput.value + " " + document.getElementById("time-input").value,
    `Radien: ${radiusStart.value}/${radiusDest.value} km · Bus-Suche: ${document.getElementById("auto-bus").checked} · Kombinationen: ${document.getElementById("combo-mode").value}`,
    "Extra-Haltestellen: " + (state.extraOrigins.length + state.extraDests.length),
    "Scan-ID: " + (state.jobId || "–"),
    "Status: " + g("phase-label") + " | " + g("progress-text"),
    `Ergebnisse: ${state.results.length} gesamt (Abfahrt: ${results.A}, Ankunft: ${results.B}, Kreuz: ${results.C})`,
    "--- Protokoll ---",
    g("log") || "(leer)",
  ].join("\n");
}

async function copyDebugInfo() {
  const btn = document.getElementById("debug-copy");
  const text = buildDebugInfo();
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch (e) { /* WebView/http: Fallback unten */ }
  if (!ok) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    document.body.appendChild(ta);
    ta.select();
    try { ok = document.execCommand("copy"); } catch (e) { /* s.u. */ }
    ta.remove();
  }
  if (!ok) {
    // Letzte Rueckfallebene: Text anzeigen, damit er manuell kopiert werden kann
    showDebugOverlay(text);
  }
  btn.textContent = ok ? "✅ Kopiert – einfach einfügen & schicken" : "🐞 Debug-Info kopieren";
  if (ok) setTimeout(() => (btn.textContent = "🐞 Debug-Info kopieren"), 2500);
}

function showDebugOverlay(text) {
  const wrap = document.createElement("div");
  wrap.className = "debug-overlay";
  const box = document.createElement("div");
  box.className = "debug-box";
  box.innerHTML = "<p>Automatisches Kopieren war nicht erlaubt – bitte Text markieren und kopieren:</p>";
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.readOnly = true;
  const close = document.createElement("button");
  close.textContent = "Schließen";
  close.onclick = () => wrap.remove();
  box.appendChild(ta);
  box.appendChild(close);
  wrap.appendChild(box);
  document.body.appendChild(wrap);
  ta.focus();
  ta.select();
}

document.getElementById("debug-copy").onclick = copyDebugInfo;

// Brücke zur Android-App (window.SparApp existiert nur im WebView der App)
function notifyApp(fn, arg) {
  try {
    if (window.SparApp && window.SparApp[fn]) window.SparApp[fn](arg == null ? "" : String(arg));
  } catch (e) { /* App-Brücke optional */ }
}

function handleEvent(ev) {
  const d = ev.data;
  const stamp = ev.ts ? `[${ev.ts}] ` : "";
  if (typeof ev.seq === "number") state.lastSeq = ev.seq;
  switch (ev.type) {
    case "phase": {
      const labels = { soll: "Soll-Verbindungen laden", A: "Phase A: Einstiegs-Bahnhöfe testen", B: "Phase B: Ziel-Bahnhöfe testen", C: "Phase C: Kombinationen testen", bus: d.label };
      document.getElementById("phase-label").textContent = labels[d.name] || d.label;
      document.getElementById("progress-fill").style.width = "0%";
      document.getElementById("progress-text").textContent = d.total ? `0 / ${d.total}` : "";
      break;
    }
    case "progress": {
      const pct = d.total ? Math.round((100 * d.done) / d.total) : 0;
      document.getElementById("progress-fill").style.width = pct + "%";
      document.getElementById("progress-text").textContent =
        `${d.done} / ${d.total} Abfragen · ${d.found} Bahnhöfe mit Treffern` +
        fmtEta(d.eta, d.etaMin);
      break;
    }
    case "soll": {
      const tbody = document.getElementById("soll-table").querySelector("tbody");
      tbody.innerHTML = "";
      state.sollSelected = new Set();
      d.connections.forEach((c) => {
        const key = sollKey(c.trains);
        state.sollSelected.add(key);
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.className = "s-check";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = true;
        cb.onchange = () => {
          if (cb.checked) state.sollSelected.add(key);
          else state.sollSelected.delete(key);
          renderResults();
        };
        td.appendChild(cb);
        tr.appendChild(td);
        tr.insertAdjacentHTML("beforeend",
          `<td class="s-dep">${fmtDate(c.dep)} ${fmtTime(c.dep)}</td><td class="s-arr">${fmtTime(c.arr)}</td>` +
          `<td class="s-trains trains">${c.trains.join(" → ")}</td>` +
          `<td class="s-price price">${c.price != null ? fmtPrice(c.price) : "–"}${c.sparschiene ? '<span class="tag">Sparschiene</span>' : ""}</td>`);
        tbody.appendChild(tr);
      });
      document.getElementById("soll-card").style.display = "";
      break;
    }
    case "candidates": {
      const addDot = (s, railColor) =>
        L.circleMarker([s.lat, s.lon],
          { radius: s.bus ? 4 : 3, color: s.bus ? "#f59e0b" : railColor, fillOpacity: 0.6 })
          .bindTooltip(s.name + (s.bus ? " (Bus)" : "")).addTo(mapLayers.candidates);
      d.origins.forEach((s) => addDot(s, "#e2002a"));
      d.dests.forEach((s) => addDot(s, "#0057b8"));
      const buses = d.origins.filter((s) => s.bus).length + d.dests.filter((s) => s.bus).length;
      logLine(`${stamp}Kandidaten: ${d.origins.length} Einstiege, ${d.dests.length} Ziele (davon ${buses} Bushaltestellen)`);
      break;
    }
    case "result": {
      // Dedupe: gleiche Ticket-Relation + Zuege -> Eintrag ersetzen (billigster gewinnt serverseitig)
      const key = `${d.ticketFromId}|${d.ticketToId}|${d.trains.join("+")}`;
      const idx = state.results.findIndex(
        (r) => `${r.ticketFromId}|${r.ticketToId}|${r.trains.join("+")}` === key);
      if (idx >= 0) state.results[idx] = d;
      else state.results.push(d);
      renderResults();
      break;
    }
    case "log":
      logLine(stamp + d.msg);
      break;
    case "error":
      logLine(stamp + "FEHLER: " + d.message);
      finishScan();
      break;
    case "done":
      logLine(`${stamp}Fertig: ${d.count} Ticket-Varianten gefunden.`);
      document.getElementById("phase-label").textContent = "✅ Scan abgeschlossen";
      finishScan();
      break;
  }
}

function finishScan() {
  notifyApp("scanFinished");
  scanBtn.disabled = false;
  cancelBtn.style.display = "none";
  if (state.evtSource) state.evtSource.close();
}

function renderResults() {
  // Filter: nur Ergebnisse, deren Soll-Verbindung angehakt ist
  const filtered = state.results.filter(
    (r) => !r.sollTrains || state.sollSelected.has(sollKey(r.sollTrains)));
  const mode = document.getElementById("sort-mode").value;
  const sorted = [...filtered].sort(mode === "saving"
    ? (a, b) => ((b.saving ?? -1e9) - (a.saving ?? -1e9)) || (a.price - b.price)
    : (a, b) => (a.price - b.price) || ((b.saving ?? 0) - (a.saving ?? 0)));

  mapLayers.hits.clearLayers();
  sorted.forEach((r) => {
    if (r.lat && r.lon) {
      L.circleMarker([r.lat, r.lon], { radius: 6, color: "#1a7f37", fillOpacity: 0.9 })
        .bindTooltip(`${r.ticketFrom} → ${r.ticketTo}: ${fmtPrice(r.price)}`)
        .addTo(mapLayers.hits);
    }
  });

  // Bestpreis global: erstes Ticket der Sortierung, dessen Preis die GANZE Strecke abdeckt
  const best = sorted.find((r) => !r.reduced);

  const rowHtml = (r) => {
    const saving = r.saving != null ? r.saving : 0;
    const savingHtml = saving > 0
      ? `<span class="saving-pos">-${fmtPrice(saving).slice(2)} €</span>`
      : `<span class="saving-neg">${saving < 0 ? "+" + fmtPrice(-saving).slice(2) + " €" : "±0"}</span>`;
    const reducedHtml = r.reduced
      ? `<span class="tag tag-warn" title="${r.reducedScope || ""}">⚠ Teilstrecke</span>` +
        (r.reducedScope ? `<br><small class="warn-text">Preis gilt nur: ${r.reducedScope}</small>` : "")
      : "";
    return `<td class="c-price price">${fmtPrice(r.price)}${r.sparschiene ? '<span class="tag">Sparschiene</span>' : ""}${reducedHtml}</td>` +
      `<td class="c-saving">${savingHtml}</td>` +
      `<td class="c-from">${r.ticketFrom}<br><small>ab ${fmtTime(r.ticketDep)}</small></td>` +
      `<td class="c-to">${r.ticketTo}<br><small>an ${fmtTime(r.ticketArr)}</small></td>` +
      `<td class="c-trains trains">${r.trains.join(" → ")}</td>` +
      `<td class="c-board">${fmtTime(r.boardTime)} <small>(dein Bahnhof)</small></td>` +
      `<td class="c-book"><a href="https://shop.oebbtickets.at/de/ticket" target="_blank">buchen ↗</a></td>`;
  };

  ["A", "B", "C"].forEach((phase) => {
    const group = document.getElementById("group-" + phase);
    const rows = sorted.filter((r) => r.phase === phase);
    group.style.display = rows.length ? "" : "none";
    group.querySelector(".group-count").textContent = `(${rows.length})`;
    const tbody = group.querySelector("tbody");
    tbody.innerHTML = "";
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      if (r === best) tr.classList.add("best");
      tr.innerHTML = rowHtml(r);
      tbody.appendChild(tr);
    });
  });
  document.getElementById("result-count").textContent =
    sorted.length === state.results.length
      ? `(${sorted.length})`
      : `(${sorted.length} von ${state.results.length} – gefiltert)`;
  document.getElementById("results-card").style.display = "";
}
