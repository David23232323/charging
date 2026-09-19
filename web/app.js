"use strict";

const $ = (id) => document.getElementById(id);
const history = [];
const thermalHistory = [];
const chargeBins = new Map();
let lastInsightTimestamp = null;
let lastExternal = null;
let timer;
let fetching = false;
const valid = (value) => typeof value === "number" && Number.isFinite(value);
const format = (value, suffix = "", digits = 1) => valid(value) ? `${value.toFixed(digits)}${suffix}` : "\u2014";
const text = (id, value) => { $(id).textContent = value; };

function duration(minutes) {
  const rounded = Math.round(minutes);
  return rounded >= 60 ? `${Math.floor(rounded / 60)}h ${rounded % 60}m` : `${rounded} min`;
}

function drawHistory(now) {
  while (history.length && history[0].timestamp < now - 120) history.shift();
  const values = history.filter((point) => valid(point.battery_watts)).map((point) => point.battery_watts);
  const extent = Math.max(5, ...values.map(Math.abs)) * 1.15;
  let path = "";
  let previous = null;
  for (const point of history) {
    if (!valid(point.battery_watts)) { previous = null; continue; }
    const x = Math.max(0, (point.timestamp - (now - 120)) / 120 * 600);
    const y = 60 - point.battery_watts / extent * 55;
    const connected = previous && point.timestamp - previous.timestamp < 8;
    path += `${connected ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)} `;
    previous = point;
  }
  $("history-line").setAttribute("d", path);
  text("range", values.length > 1
    ? `${Math.min(...values).toFixed(1)} to ${Math.max(...values).toFixed(1)} W`
    : "Collecting readings");
}

function render(data) {
  text("state", data.state);
  text("percent", format(data.percent, "", 0));
  if (valid(data.percent)) $("battery-level").value = Math.max(0, Math.min(100, data.percent));
  else $("battery-level").removeAttribute("value");
  text("estimate", valid(data.minutes_to_full) ? `~${duration(data.minutes_to_full)} to full`
    : valid(data.minutes_to_empty) ? `~${duration(data.minutes_to_empty)} remaining`
    : data.state === "Fully charged" ? "Ready to unplug"
    : data.state === "Plugged in, not charging" ? "External power connected"
    : "Time estimate unavailable");
  const watts = data.battery_watts;
  text("battery-label", valid(watts) ? (watts < 0 ? "From battery" : "Into battery") : "Battery power");
  text("battery-watts", format(valid(watts) ? Math.abs(watts) : null, " W"));
  text("battery-hint", valid(watts) && watts < 0 ? "Power leaving the battery" : "Net flow at the battery");
  text("adapter-watts", format(data.adapter_watts, " W"));
  text("rated-watts", format(data.rated_watts, " W", 0));
  text("health", format(data.health, "%"));
  text("cycles", format(data.cycles, "", 0));
  text("temperature", format(data.temperature, " \u00b0C"));
  text("voltage", format(data.voltage, " V", 2));
  text("current", format(data.current, " A", 2));
  text("adapter-name", data.adapter_name || (data.external === false ? "Not connected" : "\u2014"));
  text("profile", valid(data.adapter_voltage) && valid(data.adapter_current)
    ? `${format(data.adapter_voltage, " V")} \u00d7 ${format(data.adapter_current, " A", 2)} (not live draw)` : "\u2014");
  if (!history.length || history[history.length - 1].timestamp !== data.timestamp) history.push(data);
  drawHistory(data.timestamp);
  renderInsights(data);
  document.title = `${format(data.percent, "%", 0)} \u00b7 ${data.state} \u2014 Charging`;
}

function renderInsights(data) {
  text("mac-watts", format(data.mac_watts, " W"));
  const splitAvailable = data.external === true && valid(data.adapter_watts) && data.adapter_watts > 0
    && valid(data.battery_watts) && data.battery_watts >= 0 && data.battery_watts <= data.adapter_watts;
  $("power-split").hidden = !splitAvailable;
  const share = splitAvailable ? data.battery_watts / data.adapter_watts * 100 : null;
  text("battery-share", format(share, "%", 0));
  text("mac-share", format(splitAvailable ? 100 - share : null, "%", 0));
  if (splitAvailable) $("power-split").value = share;
  text("flow-note", splitAvailable
    ? `${format(data.battery_watts, " W")} into battery / ${format(data.mac_watts, " W")} to Mac + losses. Shares of actual input, not charger rating.`
    : data.external === false ? "On battery. Mac power is estimated from battery discharge."
    : valid(data.battery_watts) && data.battery_watts < 0 ? "Battery is supplementing adapter power. Mac estimate includes both sources and losses."
    : "Power split unavailable: waiting for consistent adapter and battery readings.");

  if (lastInsightTimestamp !== data.timestamp) {
    if (lastExternal === false && data.external === true) chargeBins.clear();
    lastExternal = data.external;
    lastInsightTimestamp = data.timestamp;
    thermalHistory.push(data);
    if (data.state === "Charging" && valid(data.percent) && data.percent >= 0 && data.percent <= 100
        && valid(data.battery_watts) && data.battery_watts > 0) {
      const level = Math.round(data.percent);
      const bin = chargeBins.get(level) || { total: 0, count: 0 };
      bin.total += data.battery_watts;
      bin.count += 1;
      chargeBins.set(level, bin);
    }
  }
  while (thermalHistory.length && thermalHistory[0].timestamp < data.timestamp - 600) thermalHistory.shift();
  const bins = [...chargeBins.entries()].sort((a, b) => a[0] - b[0]);
  const peak = Math.max(5, ...bins.map(([, bin]) => bin.total / bin.count)) * 1.1;
  let curve = "";
  let previousLevel = null;
  for (const [level, bin] of bins) {
    const x = level * 6;
    const y = 115 - bin.total / bin.count / peak * 110;
    curve += `${previousLevel !== null && level - previousLevel <= 1 ? "L" : "M"}${x},${y} L${x + 0.01},${y} `;
    previousLevel = level;
  }
  $("curve-line").setAttribute("d", curve);
  text("curve-range", bins.length ? `0\u2013${peak.toFixed(0)} W` : "Collecting readings");

  const temperatures = thermalHistory.filter((point) => valid(point.temperature));
  const low = Math.min(...temperatures.map((point) => point.temperature));
  const high = Math.max(...temperatures.map((point) => point.temperature));
  const extent = Math.max(1, high - low);
  let path = "";
  let previous = null;
  for (const point of thermalHistory) {
    if (!valid(point.temperature)) { previous = null; continue; }
    const x = (point.timestamp - data.timestamp + 600) / 600 * 600;
    const y = 115 - (point.temperature - (low - 0.5)) / (extent + 1) * 110;
    path += `${previous && point.timestamp - previous.timestamp < 8 ? "L" : "M"}${x},${y} L${x + 0.01},${y} `;
    previous = point;
  }
  $("temperature-line").setAttribute("d", path);
  text("trend-temperature", format(data.temperature, " \u00b0C"));
  text("temperature-range", temperatures.length ? `${format(low)}\u2013${format(high, " \u00b0C")}` : "\u2014");
  const first = temperatures[0];
  const elapsed = first ? data.timestamp - first.timestamp : 0;
  const delta = first && valid(data.temperature) ? data.temperature - first.temperature : null;
  text("temperature-change", valid(delta) && elapsed >= 30
    ? `${delta > 0 ? "+" : ""}${format(delta, " \u00b0C")} over ${duration(elapsed / 60)}`
    : "Collecting readings");
  text("cell-spread", valid(data.cell_spread_mv) ? `${format(data.cell_spread_mv, " mV", 0)} spread` : "\u2014");
  const cells = Array.isArray(data.cell_voltages) ? data.cell_voltages : [];
  $("cells").replaceChildren();
  if (!cells.length) {
    $("cells").textContent = "Cell telemetry unavailable";
  } else {
    cells.forEach((voltage, index) => {
      const cell = document.createElement("div");
      cell.className = "cell";
      const label = document.createElement("span");
      label.className = "label";
      label.textContent = `Cell ${index + 1}`;
      const value = document.createElement("strong");
      value.textContent = format(voltage, " V", 3);
      cell.append(label, value);
      $("cells").append(cell);
    });
  }
}

async function refresh() {
  clearTimeout(timer);
  if (document.hidden || fetching) return;
  fetching = true;
  try {
    const response = await fetch("/api/battery", { signal: AbortSignal.timeout(8000), cache: "no-store" });
    if (!response.ok) {
      const body = await response.json();
      throw new Error(body.error || `Battery service returned ${response.status}.`);
    }
    render(await response.json());
    $("error").hidden = true;
    document.body.classList.remove("stale");
    $("live-dot").classList.add("active");
    text("connection", "Live \u00b7 local");
  } catch (error) {
    $("error").hidden = false;
    text("error", `${error.message} Readings are not live. If the server stopped, run charging again and use the new tab.`);
    text("connection", "Disconnected");
    $("live-dot").classList.remove("active");
    document.body.classList.add("stale");
    document.title = "Disconnected \u2014 Charging";
  } finally {
    fetching = false;
    if (!document.hidden) timer = setTimeout(refresh, 3000);
  }
}

document.addEventListener("visibilitychange", () => {
  clearTimeout(timer);
  if (!document.hidden) refresh();
  else {
    text("connection", "Paused");
    $("live-dot").classList.remove("active");
  }
});
refresh();
