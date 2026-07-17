// SUAS 2026 — frontend client.
// This file only displays state and sends commands. All flight logic lives
// in the backend process — if this tab freezes or is closed, the mission
// keeps running untouched. Reopen the page and everything resyncs below.

const API = "";
let ws = null;
let wsReconnectTimer = null;
let state = {
  sim: true, mav_running: false, mission_running: false,
  awaiting_continue: false, awaiting_post_lap: false, search_available: false,
  click_to_fly_enabled: false, status_text: "Ready", status_level: "info", armed: false,
  conn_active: false,
};
let serverConfig = { mission_alt: 5, home_lat: null, home_lon: null, default_laps: 1,
                     webcam_index: 0, rtsp_url: "" };

async function loadServerConfig() {
  try {
    const r = await fetch("/api/config");
    serverConfig = await r.json();
    laps = serverConfig.default_laps || 1;
    lapsVal.textContent = laps;
    const home = serverConfig.home_lat
      ? `${serverConfig.home_lat.toFixed(5)}, ${serverConfig.home_lon.toFixed(5)}`
      : "auto GPS";
    $("homeInfo").textContent = `Alt ${serverConfig.mission_alt} m AGL  ·  Home: ${home}`;
    piSignalInfo.textContent = "Sent as text commands over MAVLink STATUSTEXT (no servo output involved).";
    setCamModeUi(camMode);
  } catch (e) { appendLog("[CONFIG] Could not load /api/config — using defaults.", "warn"); }
}
function defaultAlt() { return serverConfig.mission_alt ?? 5; }

// ── DOM refs ─────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const logBox = $("logBox");
const railBackend = $("railBackend"), railMav = $("railMav"), railVehicle = $("railVehicle"),
      railCamera = $("railCamera"), railPi = $("railPi");
const modeBadge = $("modeBadge");
const uriInput = $("uriInput");
const portSelect = $("portSelect");
const btnRefreshPorts = $("btnRefreshPorts");
const btnMav = $("btnMav");
const mavStatus = $("mavStatus");
const btnSim = $("btnSim"), btnReal = $("btnReal"), btnHybrid = $("btnHybrid");
const piLinkRow = $("piLinkRow"), piLinkHint = $("piLinkHint");
const btnAbort = $("btnAbort"), btnStart = $("btnStart"), btnContinue = $("btnContinue");
const statusLbl = $("statusLbl");
const camImg = $("camImg"), camPlaceholder = $("camPlaceholder"), camInfo = $("camInfo");
const btnCamWebcam = $("btnCamWebcam"), btnCamRtsp = $("btnCamRtsp"), camSourceInput = $("camSourceInput"),
      btnCamStart = $("btnCamStart"), btnCamStop = $("btnCamStop");
const btnConnect = $("btnConnect"), btnDisconnect = $("btnDisconnect"), connDot2 = $("connDot2");
const piLinkUriInput = $("piLinkUriInput"), btnSetPiLinkUri = $("btnSetPiLinkUri");
const btnClickFly = $("btnClickFly"), btnClickPin = $("btnClickPin");
const pinsList = $("pinsList"), pinsEmpty = $("pinsEmpty"), routeSuggestion = $("routeSuggestion");
const btnSuggestRoute = $("btnSuggestRoute"), btnClearPins = $("btnClearPins");
let pins = [];
let camMode = "webcam";
const btnPiRecordStart = $("btnPiRecordStart"), btnPiRecordStop = $("btnPiRecordStop"),
      btnPiProcessStart = $("btnPiProcessStart"), btnPiSendMap = $("btnPiSendMap");
const piLinkDot = $("piLinkDot"), piLastMsg = $("piLastMsg"), piSignalInfo = $("piSignalInfo");
const piLogBox = $("piLogBox");
const mapTransferCard = $("mapTransferCard"), mapProgressFill = $("mapProgressFill"),
      mapTransferInfo = $("mapTransferInfo"), mapReceivedImg = $("mapReceivedImg");

// ══════════════════════════════════════════════════════════════
//  WebSocket — log / status / state push channel
// ══════════════════════════════════════════════════════════════
function connectWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen = () => { railBackend.classList.add("on"); clearTimeout(wsReconnectTimer); };
  ws.onclose = () => { railBackend.classList.remove("on"); wsReconnectTimer = setTimeout(connectWs, 1500); };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
  ws.onmessage = (evt) => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch (e) { return; }
    if (msg.type === "log") appendLog(msg.text, msg.level);
    else if (msg.type === "status") setStatus(msg.text, msg.level);
    else if (msg.type === "state") applyState(msg.state);
    else if (msg.type === "camera_info") camInfo.textContent = msg.text;
    else if (msg.type === "pi_status") showPiMessage(msg.text, msg.level, msg.ts);
    else if (msg.type === "map_transfer") handleMapTransfer(msg);
    else if (msg.type === "pins_update") { pins = msg.pins; renderPins(); }
  };
}

function appendLog(text, level) {
  const line = document.createElement("div");
  line.className = "log-line log-" + (level || "plain");
  line.textContent = text;
  logBox.appendChild(line);
  logBox.scrollTop = logBox.scrollHeight;
  while (logBox.children.length > 800) logBox.removeChild(logBox.firstChild);
}

function setStatus(text, level) {
  statusLbl.textContent = text;
  statusLbl.style.color = { ok: "var(--acc-green)", error: "var(--acc-red)",
    warn: "var(--acc-amber)", info: "var(--muted)" }[level] || "var(--muted)";
}

function showPiMessage(text, level, ts) {
  piLastMsg.textContent = text;
  piLastMsg.className = "small log-" + (level || "plain");

  const line = document.createElement("div");
  line.className = "log-line log-" + (level || "plain");
  const time = new Date((ts || Date.now() / 1000) * 1000).toLocaleTimeString();
  line.textContent = `[${time}] ${text}`;
  piLogBox.appendChild(line);
  piLogBox.scrollTop = piLogBox.scrollHeight;
  while (piLogBox.children.length > 500) piLogBox.removeChild(piLogBox.firstChild);
}

function handleMapTransfer(msg) {
  mapTransferCard.style.display = "block";
  mapProgressFill.classList.remove("done", "failed");

  if (msg.phase === "start") {
    mapProgressFill.style.width = "0%";
    mapTransferInfo.textContent = `Receiving map: ${msg.packets} packets incoming...`;
    mapReceivedImg.style.display = "none";
  } else if (msg.phase === "progress") {
    mapProgressFill.style.width = `${msg.pct}%`;
    mapTransferInfo.textContent = `Receiving map: ${msg.pct}% (${msg.received}/${msg.packets} packets)`;
  } else if (msg.phase === "done") {
    mapProgressFill.style.width = "100%";
    mapProgressFill.classList.add("done");
    mapTransferInfo.textContent = `Map received (${msg.size} bytes, CRC32 verified) ✓`;
    mapReceivedImg.src = `/received_maps/${msg.filename}?t=${Date.now()}`;
    mapReceivedImg.style.display = "block";
  } else if (msg.phase === "failed") {
    mapProgressFill.classList.add("failed");
    mapTransferInfo.textContent = `Transfer failed: ${msg.reason || "unknown error"}`;
  }
}

function applyState(s) {
  state = { ...state, ...s };
  if (state.click_mode) setClickModeUi(state.click_mode);
  if (state.pi_link_uri !== undefined && document.activeElement !== piLinkUriInput) {
    piLinkUriInput.value = state.pi_link_uri || "";
  }
  btnAbort.disabled = !(state.armed || state.mission_running);
  btnStart.disabled = state.mission_running;
  btnContinue.style.display = state.awaiting_continue ? "inline-block" : "none";
  mavStatus.textContent = state.mav_running ? `Running on ${state.mav_port || ""}` : "Stopped";
  mavStatus.style.color = state.mav_running ? "var(--acc-green)" : "var(--muted)";
  btnMav.textContent = state.mav_running ? "Stop MAVProxy" : "Start MAVProxy";
  btnMav.classList.toggle("btn-danger", state.mav_running);
  btnMav.classList.toggle("btn-warn", !state.mav_running);
  setStatus(state.status_text, state.status_level);

  railMav.classList.toggle("on", !!state.mav_running);
  railVehicle.classList.toggle("on", !!state.armed);
  railCamera.classList.toggle("on", !!state.cam_active);
  railPi.classList.toggle("on", !!state.pi_link_active);

  const piConnected = state.conn_active;
  [btnPiRecordStart, btnPiRecordStop, btnPiProcessStart, btnPiSendMap].forEach((b) => (b.disabled = !piConnected));
  btnConnect.disabled = !!state.conn_active;
  btnDisconnect.disabled = !state.conn_active;
  connDot2.classList.toggle("on", !!state.conn_active);
  piLinkDot.classList.toggle("on", !!state.pi_link_active);
  if (state.pi_last_message && piLastMsg.textContent === "No Pi messages yet.") {
    piLastMsg.textContent = state.pi_last_message;
  }

  if (state.awaiting_post_lap) showPostLapModal(state.search_available);

  if (state.cam_active && camImg.getAttribute("src") !== "/video_feed") {
    camImg.src = "/video_feed";
    camPlaceholder.style.display = "none";
  }
  if (!state.cam_active) {
    camImg.removeAttribute("src");
    camPlaceholder.style.display = "block";
  }
  btnCamStart.disabled = !!state.cam_active;
  btnCamStop.disabled = !state.cam_active;
  [btnCamWebcam, btnCamRtsp].forEach((b) => (b.disabled = !!state.cam_active));
  camSourceInput.disabled = !!state.cam_active;
}

// Initial resync (covers the case where the page loads mid-mission)
async function initialSync() {
  try {
    const r = await fetch("/api/state");
    const data = await r.json();
    for (const entry of data.log) appendLog(entry.text, entry.level);
    for (const entry of data.pi_log) showPiMessage(entry.text, entry.level, entry.ts);
    if (data.pins) { pins = data.pins; renderPins(); }
    applyState(data.state);
  } catch (e) { /* backend not up yet — WS retry loop will catch it */ }
}

// ══════════════════════════════════════════════════════════════
//  Tabs
// ══════════════════════════════════════════════════════════════
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
  });
});

// ══════════════════════════════════════════════════════════════
//  Mode / connection
// ══════════════════════════════════════════════════════════════
function setModeUi(sim, hybrid) {
  hybrid = !!hybrid;
  btnSim.classList.toggle("active", sim && !hybrid);
  btnReal.classList.toggle("active", !sim);
  btnHybrid.classList.toggle("active", hybrid);
  modeBadge.textContent = hybrid ? "HYBRID (SITL + real Pi link)" : (sim ? "SIMULATION" : "REAL DRONE");
  modeBadge.classList.toggle("real", !sim && !hybrid);
  modeBadge.classList.toggle("hybrid", hybrid);
  uriInput.value = sim ? "tcp:127.0.0.1:5762" : "udp:0.0.0.0:14552";
  [portSelect, btnRefreshPorts, btnMav].forEach((el) => (el.disabled = sim));
  if (!sim) refreshPorts();

  piLinkRow.classList.toggle("hybrid-active", hybrid);
  piLinkHint.classList.toggle("hybrid-active", hybrid);
  if (hybrid) {
    piLinkHint.textContent = "Hybrid mode: set this to your real bench Pixhawk's connection " +
      "string (e.g. COM8) so camera/Pi commands go there while flight stays in SITL.";
    piLinkUriInput.focus();
  } else {
    piLinkHint.textContent = "Leave blank for normal operation (one Pixhawk handles both flight " +
      "and Pi commands). Set this to a separate connection string only when testing with SITL for " +
      "flight WHILE a real Pixhawk on the bench relays Pi commands — validates the real hardware " +
      "path without flying.";
  }
}

btnSim.addEventListener("click", () => { setModeUi(true, false); postJson("/api/mode", { sim: true }); });
btnReal.addEventListener("click", () => { setModeUi(false, false); postJson("/api/mode", { sim: false }); });
btnHybrid.addEventListener("click", () => { setModeUi(true, true); postJson("/api/mode", { sim: true }); });

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => (uriInput.value = chip.dataset.uri));
});

async function refreshPorts() {
  try {
    const r = await fetch("/api/ports");
    const { ports } = await r.json();
    portSelect.innerHTML = "";
    const list = ports.length ? ports : ["(no ports found)"];
    for (const p of list) {
      const opt = document.createElement("option");
      opt.value = p; opt.textContent = p;
      portSelect.appendChild(opt);
    }
  } catch (e) { appendLog("[PORTS] Could not reach backend.", "error"); }
}
btnRefreshPorts.addEventListener("click", refreshPorts);

btnMav.addEventListener("click", () => {
  if (state.mav_running) postJson("/api/mavproxy/stop", {});
  else postJson("/api/mavproxy/start", { port: portSelect.value });
});

$("btnClearLog").addEventListener("click", () => (logBox.innerHTML = ""));
$("btnClearPiLog").addEventListener("click", () => (piLogBox.innerHTML = ""));

// ══════════════════════════════════════════════════════════════
//  Waypoints table
// ══════════════════════════════════════════════════════════════
let rowSeq = 0;
const wpRows = $("wpRows");

function addRow(lat = "", lon = "", alt = "", name = "") {
  rowSeq += 1;
  const div = document.createElement("div");
  div.className = "wp-row";
  div.dataset.id = rowSeq;
  div.innerHTML = `
    <div class="idx">${wpRows.children.length + 1}</div>
    <input type="text" class="f-lat" value="${lat}">
    <input type="text" class="f-lon" value="${lon}">
    <input type="text" class="f-alt" value="${alt}">
    <input type="text" class="f-name" value="${name}">
    <button class="rm-row" title="Remove">✕</button>`;
  div.querySelector(".rm-row").addEventListener("click", () => { div.remove(); renumberRows(); });
  wpRows.appendChild(div);
}
function renumberRows() {
  [...wpRows.children].forEach((row, i) => (row.querySelector(".idx").textContent = i + 1));
}
for (let i = 0; i < 4; i++) addRow();

$("btnAddRow").addEventListener("click", () => addRow());
$("btnRemoveLast").addEventListener("click", () => { if (wpRows.lastChild) wpRows.lastChild.remove(); });
$("btnClearRows").addEventListener("click", () => (wpRows.innerHTML = ""));

const lapsVal = $("lapsVal");
let laps = 1;
$("lapsMinus").addEventListener("click", () => { laps = Math.max(1, laps - 1); lapsVal.textContent = laps; });
$("lapsPlus").addEventListener("click", () => { laps = Math.min(20, laps + 1); lapsVal.textContent = laps; });

// ── JSON load ──
$("btnLoadJson").addEventListener("click", () => $("jsonFile").click());
$("jsonFile").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const data = JSON.parse(reader.result);
      const wps = data.waypoints || [];
      if (!wps.length) { alert("No 'waypoints' key found."); return; }
      if (data.default_laps) { laps = parseInt(data.default_laps, 10); lapsVal.textContent = laps; }
      wpRows.innerHTML = "";
      for (const wp of wps) addRow(wp.lat ?? "", wp.lon ?? "", wp.alt ?? "", wp.name ?? "");
      const corners = data.search_corners;
      if (corners && corners.length === 4) {
        ["A", "B", "C", "D"].forEach((tag, i) => {
          const d = corners[i];
          searchInputs[tag].lat.value = d.lat ?? "";
          searchInputs[tag].lon.value = d.lon ?? "";
          searchInputs[tag].alt.value = d.alt ?? "";
        });
      }
      if (data.search_swath) appendLog("[JSON] Note: 'search_swath' is no longer used.", "info");
      appendLog(`[JSON] Loaded ${wps.length} waypoints from ${file.name}`, "ok");
    } catch (err) { appendLog(`[JSON] Error: ${err}`, "error"); alert("Load error: " + err); }
  };
  reader.readAsText(file);
  e.target.value = "";
});

// ══════════════════════════════════════════════════════════════
//  Search Area tab
// ══════════════════════════════════════════════════════════════
const CORNER_DEFS = [
  ["A", "Entry left", "corner-A"], ["B", "Entry right", "corner-B"],
  ["C", "Exit right", "corner-C"], ["D", "Exit left", "corner-D"],
];
const searchInputs = {};
const searchTbody = $("searchRows");
for (const [tag, desc, cls] of CORNER_DEFS) {
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td class="corner-label ${cls}">● ${tag} — ${desc}</td>
    <td><input type="text" class="s-lat"></td>
    <td><input type="text" class="s-lon"></td>
    <td><input type="text" class="s-alt"></td>`;
  searchTbody.appendChild(tr);
  searchInputs[tag] = {
    lat: tr.querySelector(".s-lat"), lon: tr.querySelector(".s-lon"), alt: tr.querySelector(".s-alt"),
  };
}

function parseSearchCorners() {
  const vals = {};
  for (const tag of ["A", "B", "C", "D"]) {
    vals[tag] = [searchInputs[tag].lat.value.trim(), searchInputs[tag].lon.value.trim(), searchInputs[tag].alt.value.trim()];
  }
  const flat = Object.values(vals).flatMap((v) => [v[0], v[1]]);
  if (flat.every((v) => v === "")) return null;          // all blank → skip
  if (!flat.every((v) => v !== "")) return "ERR";         // partially filled → error
  const corners = [];
  for (const tag of ["A", "B", "C", "D"]) {
    const [la, lo, al] = vals[tag];
    const lat = parseFloat(la), lon = parseFloat(lo);
    if (Number.isNaN(lat) || Number.isNaN(lon)) return "ERR";
    corners.push({ lat, lon, alt: al === "" ? null : parseFloat(al) });
  }
  return corners;
}

// ══════════════════════════════════════════════════════════════
//  Mission start / continue / abort / post-lap
// ══════════════════════════════════════════════════════════════
function collectWaypoints() {
  const pts = [];
  for (const row of wpRows.children) {
    const la = row.querySelector(".f-lat").value.trim();
    const lo = row.querySelector(".f-lon").value.trim();
    if (!la && !lo) continue;
    const lat = parseFloat(la), lon = parseFloat(lo);
    if (Number.isNaN(lat) || Number.isNaN(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) {
      return { error: `WP ${pts.length + 1}: bad or out-of-range lat/lon.` };
    }
    const as = row.querySelector(".f-alt").value.trim();
    const alt = as === "" ? null : parseFloat(as);
    if (as !== "" && Number.isNaN(alt)) return { error: `WP ${pts.length + 1}: bad altitude.` };
    pts.push({ lat, lon, alt });
  }
  return { pts };
}

btnStart.addEventListener("click", () => {
  const { pts, error } = collectWaypoints();
  if (error) { alert(error); return; }
  if (!pts || !pts.length) { alert("Enter at least one waypoint."); return; }
  if (!uriInput.value.trim()) { alert("URI cannot be empty."); return; }
  const corners = parseSearchCorners();
  if (corners === "ERR") { alert("Fill all 4 search corners (lat/lon) or leave all blank."); return; }

  const modeTxt = state.sim ? "SIMULATION" : "REAL DRONE";
  $("confirmText").textContent =
    `Mode      : ${modeTxt}\nWaypoints : ${pts.length}\nLaps      : ${laps}\n` +
    `Search    : ${corners ? "4-corner area" : "SKIPPED"}\nURI       : ${uriInput.value.trim()}`;
  $("confirmModal").style.display = "flex";
  $("confirmModal")._payload = { pts, corners, uri: uriInput.value.trim() };
});

$("btnConfirmCancel").addEventListener("click", () => ($("confirmModal").style.display = "none"));
$("btnConfirmGo").addEventListener("click", () => {
  const { pts, corners, uri } = $("confirmModal")._payload;
  $("confirmModal").style.display = "none";
  postJson("/api/mission/start", {
    waypoints: pts.map((p) => ({ lat: p.lat, lon: p.lon, alt: p.alt ?? defaultAlt() })),
    laps,
    uri,
    search_corners: corners ? corners.map((c) => ({ lat: c.lat, lon: c.lon, alt: c.alt ?? defaultAlt() })) : null,
  });
});

btnContinue.addEventListener("click", () => postJson("/api/mission/continue", {}));

btnAbort.addEventListener("click", () => {
  if (!confirm("Command RTL and abort?")) return;
  postJson("/api/mission/abort", {});
});

function showPostLapModal(searchAvailable) {
  const modal = $("postLapModal");
  if (modal.style.display === "flex") return;   // already shown
  $("postLapNote").style.display = searchAvailable ? "none" : "block";
  $("btnGoSearch").disabled = !searchAvailable;
  modal.style.display = "flex";
}
$("btnGoHome").addEventListener("click", () => {
  $("postLapModal").style.display = "none";
  postJson("/api/mission/post_lap_choice", { choice: "home" });
});
$("btnGoSearch").addEventListener("click", () => {
  $("postLapModal").style.display = "none";
  postJson("/api/mission/post_lap_choice", { choice: "search" });
});

// ══════════════════════════════════════════════════════════════
//  Camera feed — click-to-fly + altitude nudge
// ══════════════════════════════════════════════════════════════
camImg.addEventListener("click", (evt) => {
  camImg.focus();
  if (!state.click_to_fly_enabled) return;
  const rect = camImg.getBoundingClientRect();
  const nw = camImg.naturalWidth || rect.width;
  const nh = camImg.naturalHeight || rect.height;
  const px = ((evt.clientX - rect.left) / rect.width) * nw;
  const py = ((evt.clientY - rect.top) / rect.height) * nh;
  appendLog(`[CLICK] pixel=(${px.toFixed(0)},${py.toFixed(0)}) of ${nw}x${nh}`, "info");
  postJson("/api/camera/click", { px, py, w: nw, h: nh });
});
camImg.addEventListener("keydown", (evt) => {
  if (!state.click_to_fly_enabled) return;
  const key = evt.key.toLowerCase();
  if (key !== "u" && key !== "d") return;
  postJson("/api/camera/alt", { direction: key });
});

btnPiRecordStart.addEventListener("click", () => postJson("/api/pi/recording/start", {}));
btnPiRecordStop.addEventListener("click", () => postJson("/api/pi/recording/stop", {}));
btnPiProcessStart.addEventListener("click", () => postJson("/api/pi/processing/start", {}));
btnPiSendMap.addEventListener("click", () => postJson("/api/pi/map/send", {}));

function setCamModeUi(mode) {
  camMode = mode;
  btnCamWebcam.classList.toggle("active", mode === "webcam");
  btnCamRtsp.classList.toggle("active", mode === "rtsp");
  camSourceInput.placeholder = mode === "webcam"
    ? String(serverConfig.webcam_index ?? 0)
    : (serverConfig.rtsp_url || "rtsp://192.168.144.25:8554/main.264");
}
btnCamWebcam.addEventListener("click", () => setCamModeUi("webcam"));
btnCamRtsp.addEventListener("click", () => setCamModeUi("rtsp"));

btnCamStart.addEventListener("click", () => {
  const source = camSourceInput.value.trim();   // blank -> backend falls back to config default
  postJson("/api/camera/start", { mode: camMode, source: source || null });
});
btnCamStop.addEventListener("click", () => postJson("/api/camera/stop", {}));

btnConnect.addEventListener("click", () => postJson("/api/connect", { uri: uriInput.value.trim() }));
btnDisconnect.addEventListener("click", () => postJson("/api/disconnect", {}));

btnSetPiLinkUri.addEventListener("click", () => {
  const uri = piLinkUriInput.value.trim();
  if (uri && !confirm(`Route Pi commands through a SEPARATE connection (${uri}) instead of the vehicle link?`)) return;
  postJson("/api/pi_link/set_uri", { uri });
});

btnClickFly.addEventListener("click", () => { setClickModeUi("fly"); postJson("/api/camera/click_mode", { mode: "fly" }); });
btnClickPin.addEventListener("click", () => { setClickModeUi("pin"); postJson("/api/camera/click_mode", { mode: "pin" }); });
function setClickModeUi(mode) {
  btnClickFly.classList.toggle("active", mode === "fly");
  btnClickPin.classList.toggle("active", mode === "pin");
}

function renderPins() {
  pinsEmpty.style.display = pins.length ? "none" : "block";
  pinsList.innerHTML = "";
  for (const p of pins) {
    const row = document.createElement("div");
    row.className = "pin-row";
    const dist = p.distance_m != null ? `${p.distance_m.toFixed(0)} m` : "—";
    row.innerHTML = `
      <span class="pin-name">${p.name}</span>
      <span class="pin-coords">${p.lat.toFixed(7)}, ${p.lon.toFixed(7)}</span>
      <span class="pin-dist">${dist}</span>
      <button class="btn btn-mini fly-pin-btn">Fly here</button>`;
    row.querySelector(".fly-pin-btn").addEventListener("click", () => {
      if (!confirm(`Fly to ${p.name}?`)) return;
      postJson("/api/pins/fly", { pin_id: p.id });
    });
    pinsList.appendChild(row);
  }
}

btnClearPins.addEventListener("click", () => {
  if (!confirm("Clear all pinned objects?")) return;
  postJson("/api/pins/clear", {});
});

btnSuggestRoute.addEventListener("click", async () => {
  try {
    const r = await fetch("/api/pins/suggest_route");
    const data = await r.json();
    if (!data.names || !data.names.length) {
      routeSuggestion.style.display = "block";
      routeSuggestion.textContent = "No pins to route yet.";
      return;
    }
    routeSuggestion.style.display = "block";
    routeSuggestion.innerHTML =
      `Suggested order: <b>${data.names.join(" → ")}</b> — total ${data.total_distance_m.toFixed(0)} m. ` +
      `<button class="btn btn-mini btn-success" id="btnFlySuggested">Fly this route</button>`;
    document.getElementById("btnFlySuggested").addEventListener("click", () => {
      if (!confirm(`Fly route: ${data.names.join(" → ")}?`)) return;
      postJson("/api/pins/fly_route", { order: data.order });
    });
  } catch (e) { appendLog("[ROUTE] Could not fetch suggestion.", "error"); }
});

// ══════════════════════════════════════════════════════════════
//  Helpers
// ══════════════════════════════════════════════════════════════
async function postJson(path, body) {
  try {
    const r = await fetch(API + path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!r.ok) appendLog(`[HTTP] ${path} → ${r.status}`, "error");
  } catch (e) { appendLog(`[HTTP] ${path} failed — backend unreachable.`, "error"); }
}

// ── Boot ──
setModeUi(true);
connectWs();
loadServerConfig();
initialSync();
camInfo.textContent = "Camera feed inactive. Pick a source and click Start Camera.";
appendLog("Web UI ready — select mode, load JSON, click Start Mission.", "info");
