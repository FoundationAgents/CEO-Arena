"use strict";

/** @typedef {{run_ref: string, run_id: string, campaign_id?: string|null, seed?: number|null, model_ids?: string[], status?: string, phase?: string, day?: number, total_days?: number, updated_at?: string, revision?: number|null, control_action?: "pause"|"resume"|"none", control_label?: string}} RunSummary */
/** @typedef {{experiment_ref: string, campaign_id?: string|null, model_ids?: string[], status?: string, action?: string|null, can_start?: boolean, expected_runs?: number, completed_runs?: number, total_days?: number, seed_count?: number, message?: string}} ExperimentSummary */
/** @typedef {{category?: string, label?: string, value?: string}} Action */
/** @typedef {{window_index?: number, start_day?: number, end_day?: number|null, stage?: string, status?: string, summary?: string, actions?: Action[], provider_turns?: number, tool_calls?: number, output_tokens?: number}} TrajectoryEntry */
/** @typedef {{company_id: string, model_id?: string, status?: string, score_cents?: number, cash_cents?: number, revenue_cents?: number, cost_cents?: number, inventory_units?: number, backlog_units?: number, support_backlog?: number, satisfaction?: number, cash_series?: Array<number|object>, last_decision?: unknown, last_agent_status?: string, trajectory?: TrajectoryEntry[]}} Company */
/** @typedef {{run_id: string, status?: string, day?: number, total_days?: number, progress?: number, scenario_id?: string, updated_at?: string, companies?: Company[], decision_status_counts?: Record<string, number>, recent_events?: unknown[]}} RunDetail */

const FALLBACK_REFRESH_MS = 15000;
const UPDATE_DEBOUNCE_MS = 160;
const STORAGE_KEY = "ceo_arena.dashboard.selected-run";
const EXPERIMENT_STORAGE_KEY = "ceo_arena.dashboard.selected-experiment";
const SVG_NS = "http://www.w3.org/2000/svg";

const STATUS_LABELS = Object.freeze({
  active: "Active",
  bankrupt: "Bankrupt",
  budget_exhausted: "Incomplete (budget exhausted)",
  complete: "Completed",
  completed: "Completed",
  conflict: "Configuration conflict",
  error: "Error",
  failed: "Failed",
  finished: "Completed",
  interrupted: "Interrupted",
  invalid: "Invalid run",
  paused: "Paused",
  pending: "Pending",
  prepared: "Ready",
  protocol_error: "Protocol fallback",
  provider_error: "Provider fallback",
  queued: "Queued for a slot",
  ready: "Ready",
  running: "Running",
  sealed: "Submitted",
  starting: "Starting",
  stopped: "Stopped",
  timeout: "Decision timeout",
  waiting: "Awaiting decision",
  waiting_retry: "Awaiting retry",
  waiting_seed: "Waiting for the previous seed",
});

const PHASE_LABELS = Object.freeze({
  collecting_decisions: "Decision in progress",
  complete: "Completed",
  decisions_sealed: "Decisions sealed",
  incompatible: "Incompatible protocol",
  manifest_only: "Initializing",
  ready: "Preparing next window",
  retry_backoff: "Awaiting retry",
  waiting_capacity: "Queued for a slot",
  waiting_previous_seed: "Sequential seed scheduling",
});

const STAGE_LABELS = Object.freeze({
  collecting: "Collecting this window",
  completed: "Executed",
  pending: "Sealed; awaiting settlement",
});

const ACTION_LABELS = Object.freeze({
  capacity: "Capacity",
  enterprise: "Enterprise sales",
  marketing: "Marketing",
  market_research: "Market research",
  operations: "Operations",
  product: "Product",
  research: "Research",
});

const EXPENSE_LABELS = Object.freeze({
  procurement: "Procurement payments",
  capacity_operations: "Capacity operations",
  marketing: "Marketing",
  development: "Continuous development",
  reliability: "Reliability operations",
  support: "Support",
  innovation: "Innovation projects",
  capacity_investment: "Capacity investment",
  capacity_adjustment: "Capacity adjustment",
  market_research: "Market research",
  holding: "Inventory holding",
  remediation: "Remediation",
  contract_penalties: "Contract penalties",
});

const RECEIPT_LABELS = Object.freeze({
  applied: "Executed",
  no_op: "No change",
  completed: "Effective",
  cancelled: "Cancelled",
  rejected: "Rejected",
});

const CONTROL_ACTION_LABELS = Object.freeze({
  continue: "Resume",
  resume: "Resume experiment",
  retry: "Retry run",
  run: "Run experiment",
  start: "Run experiment",
});

const ICONS = Object.freeze({
  cash: '<path d="M2.5 5.2h11v7.3h-11z"/><path d="M5 5.2V3.5h6v1.7M5 8.8h.01M11 8.8h.01"/>',
  company:
    '<path d="M3 13.5V5.2L8 2.5l5 2.7v8.3M1.8 13.5h12.4M6 8h.01M10 8h.01M6 10.7h.01M10 10.7h.01"/>',
  decision:
    '<path d="M3 2.5h10v11H3z"/><path d="M5.5 5.5h5M5.5 8h5M5.5 10.5h3"/>',
  revenue:
    '<path d="m2.5 11 3.2-3.2 2.2 2.1 5.6-5.4"/><path d="M9.5 4.5h4v4"/>',
});

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = text;
  return node;
}

function svgElement(tag, attributes = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attributes).forEach(([name, value]) =>
    node.setAttribute(name, String(value)),
  );
  return node;
}

function finiteNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function formatMoney(cents, compact = false, exact = false) {
  const amount = finiteNumber(cents) / 100;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: compact && Math.abs(amount) >= 10000 ? "compact" : "standard",
    minimumFractionDigits: exact ? 2 : 0,
    maximumFractionDigits: exact ? 2 : compact ? 1 : 0,
  }).format(amount);
}

function formatInteger(value) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(
    finiteNumber(value),
  );
}

function formatPercent(value) {
  const number = finiteNumber(value);
  const ratio = Math.abs(number) <= 1 ? number : number / 100;
  return new Intl.NumberFormat("en-US", {
    style: "percent",
    maximumFractionDigits: 1,
  }).format(Math.max(-1, Math.min(1, ratio)));
}

function phaseLabel(value) {
  return PHASE_LABELS[value] || String(value || "");
}

function formatDate(value) {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Not recorded";
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).formatToParts(date);
  const fields = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  return `${fields.year}-${fields.month}-${fields.day} ${fields.hour}:${fields.minute}:${fields.second}`;
}

function formatDuration(milliseconds) {
  if (milliseconds == null || !Number.isFinite(milliseconds) || milliseconds < 0)
    return "Not recorded";
  if (milliseconds < 1000) return "<1 s";
  const seconds = Math.floor(milliseconds / 1000);
  return [
    seconds >= 3600 ? `${Math.floor(seconds / 3600)} h` : "",
    seconds >= 60 ? `${Math.floor((seconds % 3600) / 60)} m` : "",
    `${seconds % 60} s`,
  ].filter(Boolean).join(" ");
}

function decisionTiming(entry) {
  const completion = entry.status === "sealed" ? "Submitted" : "Decision ended";
  const settlement = entry.stage === "completed"
    ? `Settlement ${formatDate(entry.settled_at)}`
    : entry.stage === "pending" ? "Awaiting shared settlement" : "Waiting for other CEOs before settlement";
  return [
    `${completion} ${formatDate(entry.submitted_at)}`,
    `Decision time ${formatDuration(entry.decision_duration_ms)}`,
    settlement,
  ];
}

function statusKey(value) {
  return String(value || "pending")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]/g, "-");
}

function statusLabel(value) {
  const key = statusKey(value);
  return STATUS_LABELS[key] || String(value || "Unknown");
}

function scoreClass(value) {
  const number = finiteNumber(value);
  if (number > 0) return "is-positive";
  if (number < 0) return "is-negative";
  return "";
}

function companyLabel(company) {
  return String(company.model_id || company.company_id || "Unknown model");
}

function experimentDisplayName(campaignId) {
  return String(campaignId || "Standalone run")
    .replace(/(^|[-_\s])retail(?=$|[-_\s])/gi, "$1")
    .replace(/[-_\s]{2,}/g, "-")
    .replace(/^[-_\s]+|[-_\s]+$/g, "") || "Experiment";
}

/** Keep the real observation day, including gaps in sampled cash history. */
function normalizeSeries(rawSeries) {
  const points = new Map();
  for (const [index, point] of (Array.isArray(rawSeries) ? rawSeries : []).entries()) {
    const day = typeof point === "number" ? index : point?.day;
    const value = typeof point === "number" ? point : point?.cash_cents ?? point?.value ?? point?.cash;
    if (Number.isFinite(day) && day >= 0 && Number.isFinite(value)) {
      points.set(day, { day, value });
    }
  }
  return [...points.values()].sort((left, right) => left.day - right.day);
}

/** Responsive axes and inspection of recorded cash values, without interpolation. */
class CashTrend {
  constructor(points, id, label) {
    this.points = points;
    this.id = id;
    this.label = label;
    this.activeIndex = null;
    this.node = element("div", "cash-chart");
    this.node.tabIndex = 0;
    this.node.setAttribute("role", "group");
    this.node.setAttribute("aria-label", `Cash history for ${label}. Use Left and Right to inspect recorded days.`);
    this.svg = svgElement("svg", { class: "cash-plot", role: "img" });
    this.tooltip = element("div", "cash-tooltip");
    this.tooltip.hidden = true;
    this.tooltipDay = element("span", "cash-tooltip-day");
    this.tooltipValue = element("strong", "cash-tooltip-value");
    this.tooltip.append(this.tooltipDay, this.tooltipValue);
    this.node.append(this.svg, this.tooltip);
    this.node.addEventListener("pointermove", (event) => this.inspectPointer(event));
    this.node.addEventListener("pointerdown", (event) => this.inspectPointer(event));
    this.node.addEventListener("pointerleave", () => this.hideInspection());
    this.node.addEventListener("focus", () => this.inspect(this.points.length - 1));
    this.node.addEventListener("blur", () => this.hideInspection());
    this.node.addEventListener("keydown", (event) => this.inspectKey(event));
    this.draw(600, 280);
    if (typeof ResizeObserver === "function") {
      this.resizeObserver = new ResizeObserver(([entry]) => {
        const { width, height } = entry.contentRect;
        if (width > 0 && height > 0) this.draw(width, height);
      });
      this.resizeObserver.observe(this.node);
    }
  }

  dispose() {
    this.resizeObserver?.disconnect();
  }

  static tickStep(span, count) {
    const raw = Math.max(1, span / count);
    const magnitude = 10 ** Math.floor(Math.log10(raw));
    const multiple = [1, 2, 2.5, 5, 10].find((value) => value * magnitude >= raw);
    return Math.max(1, multiple * magnitude);
  }

  draw(width, height) {
    this.width = width;
    this.height = height;
    const left = width < 420 ? 66 : 78;
    this.plot = { left, right: Math.max(left + 1, width - 16), top: 26, bottom: height - 42 };
    this.maxDay = Math.max(1, this.points.at(-1).day);
    const values = this.points.map((point) => point.value);
    const low = Math.min(...values), high = Math.max(...values);
    const padding = Math.max((high - low) * 0.08, Math.abs(high) * 0.015, 100);
    const yStep = CashTrend.tickStep(high - low + 2 * padding, Math.max(4, Math.floor(height / 40)));
    this.minCash = Math.floor((low - padding) / yStep) * yStep;
    this.maxCash = Math.ceil((high + padding) / yStep) * yStep;
    this.svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    this.svg.setAttribute("aria-label", `${this.label}: Day ${this.points[0].day} to ${this.points.at(-1).day}, cash ${formatMoney(this.points[0].value, false, true)} to ${formatMoney(this.points.at(-1).value, false, true)}.`);
    this.svg.replaceChildren();
    for (let value = this.minCash; value <= this.maxCash + yStep / 2; value += yStep) {
      const y = this.y(value);
      this.svg.append(this.line(this.plot.left, y, this.plot.right, y, "chart-grid-line"));
      this.svg.append(this.text(this.plot.left - 10, y + 4, formatMoney(value, true), "end"));
    }
    const xStep = Math.ceil(CashTrend.tickStep(this.maxDay, Math.max(3, Math.min(12, Math.floor((width - left) / 64)))));
    for (let day = 0; day <= this.maxDay; day += xStep) {
      const x = this.x(day);
      this.svg.append(this.line(x, this.plot.top, x, this.plot.bottom, "chart-grid-line"));
      this.svg.append(this.text(x, this.plot.bottom + 20, formatInteger(day), "middle"));
    }
    this.svg.append(
      this.line(this.plot.left, this.plot.top, this.plot.left, this.plot.bottom, "chart-axis-line"),
      this.line(this.plot.left, this.plot.bottom, this.plot.right, this.plot.bottom, "chart-axis-line"),
      this.text(this.plot.left, 13, "Cash (USD)", "start", "chart-axis-title"),
      this.text((this.plot.left + this.plot.right) / 2, height - 3, "Day", "middle", "chart-axis-title"),
    );
    const path = this.points.map((point, index) => `${index ? "L" : "M"}${this.x(point.day)} ${this.y(point.value)}`).join(" ");
    const gradientId = `cash-fill-${this.id}`;
    const defs = svgElement("defs");
    const gradient = svgElement("linearGradient", { id: gradientId, x1: "0", y1: "0", x2: "0", y2: "1" });
    gradient.append(
      svgElement("stop", { offset: "0%", "stop-color": "#f4bd00", "stop-opacity": "0.28" }),
      svgElement("stop", { offset: "100%", "stop-color": "#f4bd00", "stop-opacity": "0.02" }),
    );
    defs.append(gradient);
    this.svg.append(
      defs,
      svgElement("path", { d: `${path} L${this.x(this.points.at(-1).day)} ${this.plot.bottom} L${this.x(this.points[0].day)} ${this.plot.bottom} Z`, fill: `url(#${gradientId})` }),
      svgElement("path", { class: "cash-line", d: path, fill: "none" }),
      svgElement("circle", { class: "cash-endpoint", cx: this.x(this.points.at(-1).day), cy: this.y(this.points.at(-1).value), r: "3.5" }),
    );
    this.guide = svgElement("g", { visibility: "hidden", "aria-hidden": "true" });
    this.verticalGuide = this.line(0, this.plot.top, 0, this.plot.bottom, "cash-crosshair");
    this.horizontalGuide = this.line(this.plot.left, 0, this.plot.right, 0, "cash-crosshair");
    this.marker = svgElement("circle", { class: "cash-marker", r: "5" });
    this.guide.append(this.verticalGuide, this.horizontalGuide, this.marker);
    this.svg.append(this.guide);
    if (this.activeIndex !== null) this.inspect(this.activeIndex);
  }

  x(day) {
    return this.plot.left + day / this.maxDay * (this.plot.right - this.plot.left);
  }

  y(value) {
    return this.plot.bottom - (value - this.minCash) / (this.maxCash - this.minCash) * (this.plot.bottom - this.plot.top);
  }

  line(x1, y1, x2, y2, className) {
    return svgElement("line", { x1, y1, x2, y2, class: className });
  }

  text(x, y, value, anchor, className = "chart-tick") {
    const node = svgElement("text", { x, y, "text-anchor": anchor, class: className });
    node.textContent = value;
    return node;
  }

  inspectPointer(event) {
    const rect = this.node.getBoundingClientRect();
    const x = (event.clientX - rect.left) * this.width / rect.width;
    const day = (x - this.plot.left) / (this.plot.right - this.plot.left) * this.maxDay;
    let nearest = 0;
    for (let index = 1; index < this.points.length; index++) {
      if (Math.abs(this.points[index].day - day) < Math.abs(this.points[nearest].day - day)) nearest = index;
    }
    this.inspect(nearest);
  }

  inspectKey(event) {
    const current = this.activeIndex ?? this.points.length - 1;
    const indices = { ArrowLeft: current - 1, ArrowRight: current + 1, Home: 0, End: this.points.length - 1 };
    if (event.key === "Escape") this.hideInspection();
    if (!(event.key in indices)) return;
    event.preventDefault();
    this.inspect(Math.max(0, Math.min(this.points.length - 1, indices[event.key])));
  }

  inspect(index) {
    this.activeIndex = index;
    const point = this.points[index], x = this.x(point.day), y = this.y(point.value);
    this.verticalGuide.setAttribute("x1", x);
    this.verticalGuide.setAttribute("x2", x);
    this.horizontalGuide.setAttribute("y1", y);
    this.horizontalGuide.setAttribute("y2", y);
    this.marker.setAttribute("cx", x);
    this.marker.setAttribute("cy", y);
    this.guide.setAttribute("visibility", "visible");
    this.tooltipDay.textContent = `Day ${formatInteger(point.day)}`;
    this.tooltipValue.textContent = formatMoney(point.value, false, true);
    this.tooltip.hidden = false;
    this.tooltip.style.left = `${Math.max(4, Math.min(this.width - 178, x + 12))}px`;
    this.tooltip.style.top = `${Math.max(4, Math.min(this.height - 70, y - 76))}px`;
  }

  hideInspection() {
    this.activeIndex = null;
    this.tooltip.hidden = true;
    this.guide.setAttribute("visibility", "hidden");
  }
}

function describeDecision(decision) {
  if (decision === null || decision === undefined || decision === "")
    return "No submitted decision";
  if (typeof decision === "string" || typeof decision === "number")
    return String(decision);
  if (typeof decision !== "object") return "Operating policy submitted";
  const record = decision;
  const summary =
    record.summary ?? record.status ?? record.action ?? record.decision_id;
  if (summary !== undefined) return String(summary);
  const keys = Object.keys(record).slice(0, 3);
  return keys.length ? `Updated ${keys.join(" · ")}` : "Operating policy submitted";
}

function eventView(event) {
  if (typeof event === "string" || typeof event === "number") {
    return { message: String(event), company: "WORLD", day: "", time: "Not recorded" };
  }
  if (!event || typeof event !== "object") {
    return { message: "Market state updated", company: "WORLD", day: "", time: "Not recorded" };
  }
  const label =
    event.label ?? event.message ?? event.summary ?? event.event ?? event.type;
  const detail =
    event.detail && event.detail !== label ? ` · ${event.detail}` : "";
  return {
    message: `${String(label ?? "Market state updated")}${detail}`,
    company: String(
      event.model_id ?? event.company_id ?? event.source ?? "WORLD",
    ),
    day:
      event.day !== null && event.day !== undefined
        ? `Day ${formatInteger(event.day)}`
        : "",
    time: formatDate(event.recorded_at ?? event.timestamp ?? event.created_at ?? event.time),
  };
}

class DashboardApi {
  async getExperiments(signal) {
    try {
      const payload = await this.#getJson("/api/experiments", signal);
      return Array.isArray(payload.experiments) ? payload.experiments : [];
    } catch (error) {
      if ([404, 503].includes(error.status)) return [];
      throw error;
    }
  }

  async getRuns(signal) {
    const payload = await this.#getJson("/api/runs", signal);
    return {
      runs: Array.isArray(payload.runs) ? payload.runs : [],
      resources: Array.isArray(payload.resources) ? payload.resources : [],
    };
  }

  async getRun(runRef, signal) {
    return this.#getJson(`/api/runs/${encodeURIComponent(runRef)}`, signal);
  }

  async startExperiment(experimentRef) {
    return this.#postControl(
      `/api/experiments/${encodeURIComponent(experimentRef)}/start`,
      {},
    );
  }

  async controlRun(runRef, action, revision, idempotencyKey) {
    return this.#postControl(
      `/api/runs/${encodeURIComponent(runRef)}/control`,
      { action, revision },
      idempotencyKey,
    );
  }

  async #postControl(url, payload, idempotencyKey = createIdempotencyKey()) {
    const session = await this.#getJson("/api/control-session");
    const token = String(session.control_token || "");
    if (!token) throw new Error("Control session unavailable");
    const response = await fetch(url, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
        "X-CEOArena-Control": token,
      },
      cache: "no-store",
      body: JSON.stringify(payload),
    });
    const text = await response.text();
    if (!response.ok) {
      let message = `Server returned HTTP ${response.status}`;
      try {
        const payload = JSON.parse(text);
        message = String(payload.detail || payload.message || message);
      } catch (_error) {
        // Keep the safe HTTP fallback when no JSON error projection exists.
      }
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    if (!text) return {};
    try {
      return JSON.parse(text);
    } catch (_error) {
      return {};
    }
  }

  openEvents() {
    return new EventSource("/api/events");
  }

  async #getJson(url, signal) {
    const response = await fetch(url, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal,
    });
    if (!response.ok) {
      const error = new Error(`Server returned HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return response.json();
  }
}

function aggregateRunStatus(runs) {
  const statuses = new Set(runs.map((run) => statusKey(run.status)));
  for (const status of [
    "running",
    "starting",
    "failed",
    "interrupted",
    "waiting_retry",
    "waiting_seed",
    "queued",
    "complete",
    "completed",
  ]) {
    if (statuses.has(status)) return status;
  }
  return runs[0]?.status || "pending";
}

function createIdempotencyKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `start-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/** A live, keyed run list: focus never freezes the displayed progress. */
class RunPicker {
  constructor() {
    this.root = document.querySelector("#runSelect");
    this.options = document.querySelector("#runOptions");
    this.value = document.querySelector("#runPickerValue");
    this.overview = document.querySelector("#runOverview");
    this.rows = new Map();
    this.order = "";
  }

  bind(onChange) {
    document.querySelector("#runPickerClose").addEventListener("click", () => {
      this.root.open = false;
      document.querySelector("#runPickerSummary").focus();
    });
    this.options.addEventListener("click", (event) => {
      const button = event.target.closest("[data-run-ref]");
      if (!button) return;
      this.root.open = false;
      document.querySelector("#runPickerSummary").focus();
      onChange(button.dataset.runRef);
    });
    document.addEventListener("click", (event) => {
      if (!this.root.contains(event.target)) this.root.open = false;
    });
    this.root.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        this.root.open = false;
        document.querySelector("#runPickerSummary").focus();
      } else if (this.root.open && ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        const buttons = [...this.options.querySelectorAll("button")];
        const current = buttons.indexOf(document.activeElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
          : Math.max(0, Math.min(buttons.length - 1, current + (event.key === "ArrowDown" ? 1 : -1)));
        buttons[next]?.focus();
      }
    });
  }

  render(runs, selectedRef) {
    const sorted = [...runs].sort((a, b) => finiteNumber(a.seed) - finiteNumber(b.seed)
      || (a.model_ids || []).join().localeCompare((b.model_ids || []).join())
      || a.run_ref.localeCompare(b.run_ref));
    const refs = new Set(sorted.map((run) => String(run.run_ref)));
    for (const [ref, row] of this.rows) {
      if (!refs.has(ref)) { row.button.remove(); this.rows.delete(ref); }
    }
    for (const run of sorted) {
      const ref = String(run.run_ref);
      if (!this.rows.has(ref)) {
        const button = element("button", "run-option");
        button.type = "button";
        button.dataset.runRef = ref;
        const seed = element("span", "run-seed");
        const lineup = element("span", "run-lineup");
        const days = element("strong", "run-days");
        const status = element("span", "run-state");
        button.append(seed, lineup, days, status);
        this.rows.set(ref, { button, seed, lineup, days, status });
      }
      const row = this.rows.get(ref);
      row.seed.textContent = `Seed ${run.seed ?? "—"}`;
      row.lineup.textContent = run.model_ids?.join(" + ") || run.run_id;
      row.days.textContent = `${finiteNumber(run.day)} / ${finiteNumber(run.total_days)}`;
      row.status.textContent = statusLabel(run.status);
      row.status.className = `run-state is-${statusKey(run.status)}`;
      row.button.setAttribute("aria-pressed", String(ref === selectedRef));
      row.button.setAttribute("aria-label", `${row.seed.textContent}, Day ${row.days.textContent}, ${row.status.textContent}, ${row.lineup.textContent}`);
    }
    const order = sorted.map((run) => run.run_ref).join(",");
    if (order !== this.order) {
      this.options.replaceChildren(...sorted.map((run) => this.rows.get(String(run.run_ref)).button));
      this.order = order;
    }
    const selected = runs.find((run) => String(run.run_ref) === selectedRef);
    this.value.textContent = selected
      ? `Seed ${selected.seed ?? "—"} · Day ${selected.day} / ${selected.total_days} · ${statusLabel(selected.status)}`
      : "No runs yet";
    const counts = new Map();
    runs.forEach((run) => {
      const key = ["complete", "completed"].includes(statusKey(run.status)) ? "complete" : statusKey(run.status);
      counts.set(key, (counts.get(key) || 0) + 1);
    });
    this.overview.replaceChildren(element("strong", "run-total", `${runs.length} runs`),
      ...[...counts].sort(([a], [b]) => a.localeCompare(b)).map(([status, count]) =>
        element("span", `run-state is-${status}`, `${count} ${statusLabel(status)}`)));
  }
}

class DashboardView {
  constructor() {
    this.runPicker = new RunPicker();
    this.expandedTrajectories = new Set();
    this.pendingExperimentRender = null;
    this.trajectoryScroll = new Map();
    this.elements = {
      companyCount: document.querySelector("#companyCount"),
      companyGrid: document.querySelector("#companyGrid"),
      connection: document.querySelector("#connectionStatus"),
      controlMessage: document.querySelector("#controlMessage"),
      currentDay: document.querySelector("#currentDay"),
      dashboard: document.querySelector("#dashboard"),
      decisionSummary: document.querySelector("#decisionSummary"),
      empty: document.querySelector("#emptyState"),
      error: document.querySelector("#errorState"),
      errorMessage: document.querySelector("#errorMessage"),
      eventList: document.querySelector("#eventList"),
      experimentSelect: document.querySelector("#experimentSelect"),
      lastUpdated: document.querySelector("#lastUpdated"),
      loading: document.querySelector("#loadingState"),
      marketSignal: document.querySelector("#marketSignal"),
      runRetryStatus: document.querySelector("#runRetryStatus"),
      metricGrid: document.querySelector("#metricGrid"),
      notice: document.querySelector("#notice"),
      noticeRetry: document.querySelector("#noticeRetry"),
      noticeText: document.querySelector("#noticeText"),
      progressBar: document.querySelector("#progressBar"),
      progressPercent: document.querySelector("#progressPercent"),
      progressTrack: document.querySelector("#progressTrack"),
      rankingBody: document.querySelector("#rankingBody"),
      runButton: document.querySelector("#runButton"),
      runButtonLabel: document.querySelector("#runButtonLabel"),
      runControlButton: document.querySelector("#runControlButton"),
      runControlButtonLabel: document.querySelector("#runControlButtonLabel"),
      runCtas: [...document.querySelectorAll("[data-run-experiment]")],
      runSelect: document.querySelector("#runSelect"),
      runStatus: document.querySelector("#runStatus"),
      runTitle: document.querySelector("#runTitle"),
      runLineup: document.querySelector("#runLineup"),
      scenarioId: document.querySelector("#scenarioId"),
      schedulerStatus: document.querySelector("#schedulerStatus"),
      stateRetries: [...document.querySelectorAll("[data-retry]")],
      totalDays: document.querySelector("#totalDays"),
      transportStatus: document.querySelector("#transportStatus"),
    };
  }

  bind({ onExperimentChange, onRefresh, onRunChange, onRunControl, onStart }) {
    this.elements.runButton.addEventListener("click", onStart);
    this.elements.runCtas.forEach((button) =>
      button.addEventListener("click", onStart),
    );
    this.elements.experimentSelect.addEventListener("change", (event) =>
      onExperimentChange(event.target.value),
    );
    this.runPicker.bind(onRunChange);
    this.elements.runControlButton.addEventListener("click", onRunControl);
    this.elements.experimentSelect.addEventListener("blur", () => {
      if (!this.pendingExperimentRender) return;
      const pending = this.pendingExperimentRender;
      this.pendingExperimentRender = null;
      this.renderExperiments(
        pending.experiments,
        pending.selectedExperimentRef,
      );
    });
    this.elements.stateRetries.forEach((button) =>
      button.addEventListener("click", () =>
        button.dataset.action === "start" ? onStart() : onRefresh(),
      ),
    );
    this.elements.noticeRetry.addEventListener("click", () =>
      this.elements.noticeRetry.dataset.action === "start"
        ? onStart()
        : onRefresh(),
    );
  }

  renderExperiments(experiments, selectedExperimentRef) {
    const select = this.elements.experimentSelect;
    if (document.activeElement === select) {
      this.pendingExperimentRender = { experiments, selectedExperimentRef };
      return;
    }
    this.pendingExperimentRender = null;
    select.replaceChildren();
    experiments.forEach((experiment) => {
      const option = element("option");
      option.value = String(experiment.experiment_ref);
      const models = Array.isArray(experiment.model_ids)
        ? experiment.model_ids.join(" + ")
        : "";
      const progress =
        `${formatInteger(experiment.completed_runs)}/` +
        formatInteger(experiment.expected_runs);
      const identity = experimentDisplayName(experiment.campaign_id);
      const duration = experiment.total_days
        ? ` · ${formatInteger(experiment.total_days)} days`
        : "";
      option.textContent =
        `${identity} · ${models || "Models not specified"} · ` +
        `${statusLabel(experiment.status)} ${progress}${duration}`;
      option.title = option.textContent;
      option.selected =
        String(experiment.experiment_ref) === selectedExperimentRef;
      select.append(option);
    });
    if (!experiments.length) {
      select.append(element("option", "", "No experiments available"));
    }
    select.disabled = experiments.length === 0;
    const selectedOption = [...select.options].find(
      (option) => option.selected,
    );
    select.title = selectedOption?.textContent || "";
  }

  renderResources(resources, campaignId) {
    const node = this.elements.schedulerStatus;
    const scheduler = resources.find((item) => item.campaign_id === campaignId)?.scheduler;
    node.hidden = !campaignId;
    node.classList.remove("is-pressure");
    if (!scheduler) {
      node.textContent = "Live resource data unavailable";
      node.title = "Resource sampling has not started, has stopped or is stale. Durable records determine run status.";
      return;
    }
    const pressure = ["pressure", "critical"].includes(scheduler.resource_status);
    const memory = scheduler.available_memory_mb == null
      ? "Available memory —"
      : `Available memory ${(scheduler.available_memory_mb / 1024).toFixed(1)} GB`;
    node.textContent = [
      `Server: ${scheduler.active_runs} / ${scheduler.run_limit} runs active`,
      `${scheduler.active_decisions} CEO decisions in progress`,
      memory,
    ].join(" · ");
    node.classList.toggle("is-pressure", pressure);
    node.title = `Sampled ${formatDate(scheduler.timestamp)}; monitoring only: CPU and memory do not reduce concurrency. Free run slots are filled in seed-priority order.`;
  }

  renderRunTelemetry(detail) {
    const node = this.elements.runRetryStatus;
    const retry = detail.retry_not_before;
    const performance = detail.performance;
    const companies = Object.entries(performance?.companies || {});
    const issues = companies.filter(([, value]) => ["retrying", "failed", "timed_out"].includes(value.state));
    const timings = performance?.stage_total_ms || {};
    node.hidden = !detail.last_error && !retry && issues.length === 0;
    node.textContent = [
      `Run attempt ${detail.attempt ?? 0} / ${detail.attempt_limit ?? "unlimited"}`,
      retry ? `Next retry ${formatDate(retry)} · Click Pause to stop waiting` : "",
      detail.last_error ? `Last error: ${detail.last_error}` : "",
      ...issues.map(([company, value]) => {
        const failed = value.state === "failed";
        const label = value.state === "timed_out"
          ? "Infrastructure timeout; prior policy retained"
          : value.last_stage === "decision_stage_failed"
            ? "Receipt save failed (not acknowledged)"
            : failed ? "Needs attention" : "Retrying within remaining allowance";
        const fallback = failed ? "Check the recorded failure" : "Waiting for provider recovery";
        const attempts = value.failure?.http_status === 400
          ? `HTTP 400 failures ${value.failure.attempt ?? value.attempt}/3`
          : `attempt ${value.attempt}`;
        return `${company} ${label} · ${attempts} · ${value.failure?.category || (failed ? "internal" : "provider")} · ${value.failure?.message || fallback}`;
      }),
      performance ? `Cumulative local time: SQL ${(finiteNumber(timings.sql_projection) / 1000).toFixed(2)}s / Settlement ${(finiteNumber(timings.settlement) / 1000).toFixed(2)}s(diagnostic, not Score)` : "",
    ].filter(Boolean).join(" | ");
  }

  renderRuns(runs, selectedRunRef) {
    this.runPicker.render(runs, selectedRunRef);
    this.elements.lastUpdated.textContent = formatDate(new Date());
  }

  renderRunPreview(run) {
    if (!run) return;
    this.elements.dashboard.dataset.loading = "true";
    this.elements.dashboard.setAttribute("aria-busy", "true");
    this.#renderHeading(run);
    this.elements.currentDay.textContent = formatInteger(run.day);
    this.elements.totalDays.textContent = formatInteger(run.total_days);
    this.elements.progressBar.style.width = `${finiteNumber(run.progress) * 100}%`;
    this.elements.progressPercent.textContent = "Loading run details...";
    this.elements.decisionSummary.textContent = "";
    this.elements.runRetryStatus.hidden = true;
    this.elements.marketSignal.hidden = true;
    this.renderRunControl(run);
    this.showDashboard();
  }

  #renderHeading(run) {
    this.elements.runTitle.textContent = `Market run · Seed ${run.seed ?? "—"}`;
    this.elements.runTitle.title = run.run_id ? experimentDisplayName(run.run_id) : "";
    this.elements.runLineup.textContent = run.model_ids?.join(" + ") || run.run_id || "";
    this.#renderStatus(this.elements.runStatus, run.status);
  }

  render(detail, busyAction = null) {
    const companies = Array.isArray(detail.companies) ? detail.companies : [];
    const day = Math.max(0, finiteNumber(detail.day));
    const totalDays = Math.max(0, finiteNumber(detail.total_days));
    const rawProgress = Number.isFinite(Number(detail.progress))
      ? finiteNumber(detail.progress)
      : totalDays > 0
        ? day / totalDays
        : 0;
    const progress = Math.max(
      0,
      Math.min(1, rawProgress > 1 ? rawProgress / 100 : rawProgress),
    );

    this.elements.dashboard.dataset.loading = "false";
    this.elements.dashboard.setAttribute("aria-busy", "false");
    this.#renderHeading(detail);
    this.#setText(
      this.elements.scenarioId,
      `SCENARIO ${experimentDisplayName(detail.scenario_id || "—")}`,
    );
    this.#setText(this.elements.currentDay, formatInteger(day));
    this.#setText(this.elements.totalDays, formatInteger(totalDays));
    this.#setText(
      this.elements.progressPercent,
      `${(progress * 100).toFixed(1)}% complete`,
    );
    this.#setText(this.elements.companyCount, `${companies.length} companies`);
    this.#setText(
      this.elements.lastUpdated,
      formatDate(new Date()),
    );
    this.#renderStatus(this.elements.runStatus, detail.status);
    const signal = detail.market_signal;
    this.elements.marketSignal.hidden = !signal;
    this.elements.marketSignal.textContent = signal
      ? `Published market observation · D${formatInteger(signal.day)} · Demand index ${finiteNumber(signal.demand_index).toFixed(2)} · Quality requirement index ${finiteNumber(signal.quality_expectation_index).toFixed(2)} · Delayed information; hidden phases and future states are not shown`
      : "";
    this.renderRunControl(detail, busyAction);

    this.elements.progressBar.style.width = `${progress * 100}%`;
    this.elements.progressTrack.setAttribute(
      "aria-valuenow",
      String(Math.round(progress * 100)),
    );
    this.#renderDecisionSummary(detail.decision_status_counts);
    this.#renderMetrics(companies);
    this.#renderRanking(companies);
    this.#renderCompanies(companies);
    this.#renderEvents(detail.recent_events);
    this.showDashboard();
  }

  renderRunControl(run, busyAction = null) {
    const action = String(run?.control_action || "none");
    const busy = busyAction === "pause" || busyAction === "resume";
    const label = busy
      ? busyAction === "pause"
        ? "Pausing..."
        : "Resuming..."
      : String(run?.control_label || "Read-only");
    const enabled =
      !busy &&
      ["pause", "resume"].includes(action) &&
      Number.isInteger(run?.revision);
    this.elements.runControlButtonLabel.textContent = label;
    this.elements.runControlButton.disabled = !enabled;
    this.elements.runControlButton.dataset.action = action;
    this.elements.runControlButton.setAttribute("aria-busy", String(busy));
    this.elements.runControlButton.classList.toggle("is-starting", busy);
    this.elements.runControlButton.title = label;
  }

  showLoading() {
    this.#showOnly(this.elements.loading);
  }

  showEmpty() {
    this.#showOnly(this.elements.empty);
  }

  showError(message, hasData) {
    this.#setRetryAction("refresh");
    if (hasData) {
      this.elements.noticeText.textContent = `Refresh failed: ${message}.Showing the last available data.`;
      this.elements.notice.classList.add("is-visible");
    } else {
      this.elements.errorMessage.textContent = message;
      this.#showOnly(this.elements.error);
    }
  }

  showStartError(message, hasData) {
    this.#setRetryAction("start");
    if (hasData) {
      this.elements.noticeText.textContent = `Launch failed: ${message}`;
      this.elements.notice.classList.add("is-visible");
    } else {
      this.elements.errorMessage.textContent = `Launch failed: ${message}`;
      this.#showOnly(this.elements.error);
    }
  }

  showRunControlError(message) {
    this.#setRetryAction("refresh");
    this.elements.noticeText.textContent = `Run control failed: ${message}`;
    this.elements.notice.classList.add("is-visible");
  }

  showInvalidRun(run) {
    this.elements.errorMessage.textContent = `Run ${run.run_id} has invalid metadata. Select another experiment.`;
    this.#showOnly(this.elements.error);
  }

  showDashboard() {
    [this.elements.loading, this.elements.empty, this.elements.error].forEach(
      (node) => node.classList.remove("is-visible"),
    );
    this.elements.dashboard.classList.add("is-visible");
    this.elements.notice.classList.remove("is-visible");
  }

  renderControl(experiment, starting = false) {
    const action = String(experiment?.action || "").toLowerCase();
    const actionLabel =
      CONTROL_ACTION_LABELS[action] ||
      (action && action !== "none" ? String(experiment.action) : "");
    const label = !experiment
      ? "No experiment configured"
      : starting
        ? "Starting..."
        : actionLabel ||
          (experiment.can_start ? "Run experiment" : statusLabel(experiment.status));
    const disabled = starting || !experiment?.can_start;
    this.elements.runButtonLabel.textContent = label;
    this.elements.runButton.disabled = disabled;
    this.elements.runButton.classList.toggle("is-starting", starting);
    this.elements.runButton.setAttribute("aria-busy", String(starting));
    this.elements.runButton.title = String(experiment?.message || label);
    const status = statusKey(experiment?.status);
    const showMessage =
      Boolean(experiment) &&
      (starting ||
        !experiment.can_start ||
        ["interrupted", "failed", "conflict", "invalid"].includes(status));
    this.elements.controlMessage.textContent = showMessage
      ? String(experiment.message || "")
      : "";
    this.elements.runCtas.forEach((button) => {
      button.textContent = label;
      button.disabled = disabled;
      button.title = String(experiment?.message || label);
    });
  }

  resetTrajectoryState() {
    this.expandedTrajectories.clear();
    this.trajectoryScroll.clear();
  }

  setConnection(state, label) {
    this.elements.connection.className = `connection is-${state}`;
    this.elements.connection.querySelector(".connection-label").textContent =
      label;
  }

  setTransport(state, label, detail) {
    this.setConnection(state, label);
    this.elements.transportStatus.textContent = detail;
  }

  #setRetryAction(action) {
    const starting = action === "start";
    this.elements.noticeRetry.dataset.action = action;
    this.elements.noticeRetry.textContent = starting ? "Run again" : "Retry";
    this.elements.stateRetries.forEach((button) => {
      button.dataset.action = action;
      button.textContent = starting ? "Run again" : "Reconnect";
    });
  }

  #showOnly(target) {
    [this.elements.loading, this.elements.empty, this.elements.error].forEach(
      (node) => node.classList.toggle("is-visible", node === target),
    );
    this.elements.dashboard.classList.remove("is-visible");
  }

  #renderStatus(container, status) {
    const key = statusKey(status);
    container.className = `status-pill status-${key}`;
    container.replaceChildren(
      element("span"),
      document.createTextNode(statusLabel(status)),
    );
  }

  #renderDecisionSummary(counts) {
    if (!counts || typeof counts !== "object") {
      this.elements.decisionSummary.textContent = "Awaiting the first decision";
      return;
    }
    const entries = Object.entries(counts).filter(
      ([, count]) => finiteNumber(count) > 0,
    );
    this.elements.decisionSummary.textContent = entries.length
      ? entries
          .map(
            ([status, count]) =>
              `${statusLabel(status)} ${formatInteger(count)}`,
          )
          .join(" · ")
      : "Awaiting the first decision";
  }

  #renderMetrics(companies) {
    const totalScore = companies.reduce(
      (sum, company) => sum + finiteNumber(company.score_cents),
      0,
    );
    const activeCount = companies.filter(
      (company) => !["bankrupt", "failed"].includes(statusKey(company.status)),
    ).length;
    const averageSatisfaction = companies.length
      ? companies.reduce(
          (sum, company) => sum + finiteNumber(company.satisfaction),
          0,
        ) / companies.length
      : 0;
    const metrics = [
      {
        label: "System score",
        value: formatMoney(totalScore, true),
        hint: "Total enterprise-value gain, including inventory liquidation adjustment",
        icon: "cash",
        tone: scoreClass(totalScore),
      },
      {
        label: "Active companies",
        value: `${activeCount} / ${companies.length}`,
        hint:
          activeCount === companies.length
            ? "All companies are operating"
            : `${companies.length - activeCount} bankrupt`,
        icon: "company",
      },
      {
        label: "Mean satisfaction",
        value: formatPercent(averageSatisfaction),
        hint: "Service experience across companies",
        icon: "decision",
      },
    ];

    const fragment = document.createDocumentFragment();
    metrics.forEach((metric) => {
      const card = element("article", "metric-card");
      const label = element("div", "metric-label");
      const icon = svgElement("svg", {
        viewBox: "0 0 16 16",
        "aria-hidden": "true",
      });
      icon.innerHTML = ICONS[metric.icon];
      label.append(icon, document.createTextNode(metric.label));
      const value = element(
        "strong",
        `metric-value ${metric.tone || ""}`,
        metric.value,
      );
      card.append(label, value, element("span", "metric-hint", metric.hint));
      fragment.append(card);
    });
    this.elements.metricGrid.replaceChildren(fragment);
  }

  #renderRanking(companies) {
    const ranked = [...companies].sort(
      (left, right) =>
        finiteNumber(right.score_cents) - finiteNumber(left.score_cents),
    );
    const fragment = document.createDocumentFragment();
    ranked.forEach((company, index) => {
      const row = element("tr");
      row.append(
        element(
          "td",
          `rank-cell ${index === 0 ? "is-first" : ""}`,
          String(index + 1).padStart(2, "0"),
        ),
      );

      const identityCell = element("td");
      identityCell.append(this.#companyIdentity(company));
      row.append(identityCell);

      const statusCell = element("td");
      const status = element("span", "table-status");
      status.append(
        element("span", `status-dot status-${statusKey(company.status)}`),
        document.createTextNode(statusLabel(company.status)),
      );
      statusCell.append(status);
      row.append(statusCell);

      row.append(
        element("td", "numeric", formatMoney(company.cash_cents, true)),
        element(
          "td",
          `numeric ${scoreClass(company.score_cents)}`,
          formatMoney(company.score_cents, true),
        ),
      );
      fragment.append(row);
    });
    if (!ranked.length) {
      const row = element("tr");
      const cell = element(
        "td",
        "empty-inline",
        "Company data will appear after the first simulated day",
      );
      cell.colSpan = 5;
      row.append(cell);
      fragment.append(row);
    }
    this.elements.rankingBody.replaceChildren(fragment);
  }

  #renderCompanies(companies) {
    this.cashCharts?.forEach((chart) => chart.dispose());
    this.cashCharts = [];
    const focusedTrajectory = document.activeElement?.closest?.(".trajectory, .operating-audit");
    const focusedCompanyId = focusedTrajectory?.dataset.companyId || null;
    const fragment = document.createDocumentFragment();
    companies.forEach((company, index) => {
      const card = element("article", "company-card");
      const header = element("header", "company-card-header");
      header.append(this.#companyIdentity(company));

      const score = element("div", "score-block");
      const scoreValue = element(
        "strong",
        scoreClass(company.score_cents),
        formatMoney(company.score_cents, true),
      );
      score.append(element("span", "", "Company score"), scoreValue);
      header.append(score);

      const series = normalizeSeries(company.cash_series);
      const chart = element("div", "chart-region");
      chart.append(element("span", "chart-label", "CASH TREND"));
      const change = series.length > 1 ? series.at(-1).value - series[0].value : 0;
      chart.append(
        element(
          "span",
          `chart-value ${scoreClass(change)}`,
          `${change >= 0 ? "+" : ""}${formatMoney(change, true)}`,
        ),
      );
      if (series.length) {
        const trend = new CashTrend(series, `cash-${index}`, companyLabel(company));
        this.cashCharts.push(trend);
        chart.append(trend.node);
      } else {
        chart.append(element("div", "chart-empty", "Waiting for cash history"));
      }

      const metricGrid = element("div", "company-metrics");
      [
        ["Cash", formatMoney(company.cash_cents, true)],
        ["Cumulative revenue", formatMoney(company.revenue_cents, true)],
        ["Cumulative cash payments", formatMoney(company.cost_cents, true)],
        ["Refunds received (non-sales)", formatMoney(company.refunds_cents, true)],
        ["Gross profit (not net income)", formatMoney(company.gross_profit_cents, true)],
        ["Customer satisfaction", formatPercent(company.satisfaction)],
        ["Inventory", `${formatInteger(company.inventory_units)} units`],
        ["Order backlog", formatInteger(company.backlog_units)],
        ["Support backlog", formatInteger(company.support_backlog)],
      ].forEach(([label, value]) => {
        const metric = element("div", "company-metric");
        metric.append(element("span", "", label), element("strong", "", value));
        metricGrid.append(metric);
      });

      card.append(header, chart, metricGrid, this.#operatingAudit(company), this.#trajectory(company));
      fragment.append(card);
    });
    if (!companies.length)
      fragment.append(element("div", "empty-inline", "Waiting for company initialization"));
    this.elements.companyGrid.replaceChildren(fragment);
    if (focusedCompanyId) {
      window.requestAnimationFrame(() => {
        const target = [
          ...this.elements.companyGrid.querySelectorAll(".trajectory, .operating-audit"),
        ].find((item) => item.dataset.companyId === focusedCompanyId);
        target?.querySelector("summary")?.focus({ preventScroll: true });
      });
    }
  }

  #renderEvents(events) {
    const normalized = Array.isArray(events)
      ? events.map(eventView).reverse()
      : [];
    const fragment = document.createDocumentFragment();
    normalized.forEach((event) => {
      const item = element("li", "event-item");
      const metadata = element("div", "event-metadata");
      const timestamp = element("time", "event-time", event.time);
      timestamp.title = "Event recorded at · UTC+8";
      metadata.append(element("span", "event-company", event.company), timestamp);
      item.append(
        element("span", "event-marker"),
        element("span", "event-message", event.message),
        element("span", "event-day", event.day),
        metadata,
      );
      fragment.append(item);
    });
    if (!normalized.length)
      fragment.append(element("li", "empty-inline", "No recent events"));
    this.elements.eventList.replaceChildren(fragment);
  }

  #operatingAudit(company) {
    const auditKey = `${company.company_id}:audit`;
    const details = element("details", "operating-audit");
    details.dataset.companyId = auditKey;
    details.open = this.expandedTrajectories.has(auditKey);
    details.append(element("summary", "", "Operating ledger and execution receipts"));
    const content = element("div", "audit-content");
    content.append(element("p", "audit-note", "Closing cash = opening cash + sales + refunds - cash payments. Gross profit deducts cost of goods sold, not all expenses. Showing up to 30 recent days."));
    const ledger = Array.isArray(company.daily_cashflow) ? company.daily_cashflow : [];
    if (ledger.length) {
      const region = element("div", "audit-table-region");
      region.tabIndex = 0;
      region.setAttribute("role", "region");
      region.setAttribute("aria-label", "Daily cash and gross-profit ledger; scroll horizontally");
      const table = element("table", "audit-table");
      const header = element("tr");
      ["Day", "Opening cash", "Sales revenue", "Payments / breakdown", "Refunds", "Gross profit", "Net cash flow", "Closing cash"].forEach((label) => {
        const cell = element("th", "", label);
        cell.scope = "col";
        header.append(cell);
      });
      const thead = element("thead");
      thead.append(header);
      const tbody = element("tbody");
      [...ledger].reverse().forEach((entry) => {
        const row = element("tr");
        row.append(element("td", "", `D${entry.day}`));
        [entry.opening_cash_cents, entry.revenue_cents].forEach((value) => {
          row.append(element("td", "numeric", formatMoney(value, false, true)));
        });
        const payment = element("td", "numeric");
        const breakdown = element("details", "expense-breakdown");
        const expenseKey = `${auditKey}:expense:${entry.day}`;
        breakdown.open = this.expandedTrajectories.has(expenseKey);
        breakdown.append(element("summary", "", formatMoney(entry.payments_cents, false, true)));
        Object.entries(EXPENSE_LABELS).forEach(([key, label]) => {
          const cents = finiteNumber(entry.expenses?.[key]);
          if (cents) breakdown.append(element("div", "", `${label}: ${formatMoney(cents, false, true)}`));
        });
        payment.append(breakdown);
        this.#bindDisclosure(breakdown, breakdown, expenseKey);
        row.append(payment);
        [entry.refunds_cents, entry.gross_profit_cents, entry.net_cashflow_cents, entry.ending_cash_cents].forEach((value) => {
          row.append(element("td", "numeric", formatMoney(value, false, true)));
        });
        tbody.append(row);
      });
      table.append(thead, tbody);
      region.append(table);
      content.append(region);
    } else {
      content.append(element("p", "audit-note", "Ledger available after the first settlement day."));
    }
    const receipts = Array.isArray(company.action_receipts) ? company.action_receipts : [];
    content.append(element("h4", "", "Execution receipts - latest 60"));
    const list = element("ol", "receipt-list");
    [...receipts].reverse().forEach((receipt) => {
      const item = element("li", "receipt-item");
      item.append(element("strong", "", `Day ${receipt.day} · ${RECEIPT_LABELS[receipt.status] || receipt.status} · ${receipt.action}`));
      item.append(element("p", "", receipt.message));
      const amounts = [`Paid ${formatMoney(receipt.charged_cents, false, true)}`, `Refunds ${formatMoney(receipt.refunded_cents, false, true)}`];
      if (receipt.effective_day != null) amounts.push(`Effective day D${receipt.effective_day}`);
      if (receipt.target_tier != null) amounts.push(`Target tier ${receipt.target_tier}`);
      if (receipt.project_id) amounts.push(`Project ${receipt.project_id.slice(0, 8)}`);
      item.append(element("span", "audit-note", amounts.join(" · ")));
      list.append(item);
    });
    if (!receipts.length) list.append(element("li", "audit-note", "No execution receipts yet. A submitted decision is not an executed action."));
    content.append(list);
    details.append(content);
    this.#bindDisclosure(details, content, auditKey);
    return details;
  }

  #trajectory(company) {
    const entries = Array.isArray(company.trajectory) ? company.trajectory : [];
    const companyId = String(company.company_id || companyLabel(company));
    const details = element("details", "trajectory");
    details.dataset.companyId = companyId;
    details.open = this.expandedTrajectories.has(companyId);

    const latest = entries.at(-1);
    const summary = element("summary");
    const icon = element("span", "trajectory-icon", "LOG");
    icon.setAttribute("aria-hidden", "true");
    const copy = element("span", "trajectory-summary-copy");
    copy.append(
      element(
        "strong",
        "",
        `Decision history · ${formatInteger(entries.length)} decision windows`,
      ),
      element(
        "span",
        "",
        latest
          ? `Latest: ${this.#decisionText(latest.summary)} · ${statusLabel(latest.status)}`
          : "Awaiting the CEO's first decision",
      ),
    );
    const chevron = svgElement("svg", {
      class: "trajectory-chevron",
      viewBox: "0 0 20 20",
      "aria-hidden": "true",
    });
    chevron.append(svgElement("path", { d: "m5 7.5 5 5 5-5" }));
    summary.append(icon, copy, chevron);

    const content = element("div", "trajectory-content");
    if (!entries.length) {
      content.append(
        element("div", "trajectory-empty", "No decision history yet"),
      );
    } else {
      const list = element("ol", "trajectory-list");
      [...entries].reverse().forEach((entry) => {
        const item = element("li", "trajectory-item");
        const head = element("div", "trajectory-head");
        const endDay = entry.end_day ?? entry.start_day;
        const dayRange =
          endDay > finiteNumber(entry.start_day)
            ? `Day ${formatInteger(entry.start_day)}–${formatInteger(endDay)}`
            : `Day ${formatInteger(entry.start_day)}`;
        head.append(
          element(
            "span",
            "trajectory-window",
            `Window ${formatInteger(finiteNumber(entry.window_index) + 1)} · ${dayRange}`,
          ),
          element(
            "span",
            "trajectory-status",
            `${STAGE_LABELS[entry.stage] || entry.stage || "In progress"} · ${statusLabel(entry.status)}`,
          ),
        );
        item.append(
          head,
          element(
            "p",
            "trajectory-decision",
            this.#decisionText(entry.summary),
          ),
        );
        const timing = element("div", "trajectory-timing");
        timing.title = "Times use UTC+8. Decision duration is cumulative active time, excluding queues, pauses and waiting for other CEOs; not individual tool latency.";
        decisionTiming(entry).forEach((text) => timing.append(element("span", "", text)));
        item.append(timing);

        const actions = Array.isArray(entry.actions) ? entry.actions : [];
        if (actions.length) {
          const actionList = element("div", "action-list");
          actions.forEach((action) => {
            const chip = element("span", "action-chip");
            chip.append(
              element(
                "span",
                "",
                ACTION_LABELS[action.category] || action.category || "Action",
              ),
              document.createTextNode(
                `${action.label || "Policy"} · ${action.value || "Updated"}`,
              ),
            );
            actionList.append(chip);
          });
          item.append(actionList);
        }

        const providerTurns = finiteNumber(entry.provider_turns);
        const toolCalls = finiteNumber(entry.tool_calls);
        const outputTokens = finiteNumber(entry.output_tokens);
        const usage =
          providerTurns + toolCalls + outputTokens > 0
            ? `${formatInteger(providerTurns)} model replies · ${formatInteger(toolCalls)} tools · ${formatInteger(outputTokens)} output tokens`
            : "Rule policy - no model calls";
        item.append(element("div", "trajectory-usage", usage));
        list.append(item);
      });
      content.append(list);
    }

    details.append(summary, content);
    this.#bindDisclosure(details, content, companyId);
    return details;
  }

  #bindDisclosure(details, content, key) {
    const scrollTop = finiteNumber(this.trajectoryScroll.get(key));
    window.requestAnimationFrame(() => {
      if (content.isConnected) content.scrollTop = scrollTop;
    });
    content.addEventListener("scroll", () => {
      this.trajectoryScroll.set(key, content.scrollTop);
    });
    details.addEventListener("toggle", () => {
      if (!details.isConnected) return;
      if (details.open) this.expandedTrajectories.add(key);
      else this.expandedTrajectories.delete(key);
    });
  }

  #decisionText(value) {
    const text = describeDecision(value);
    if (text === "Kept prior operating policy") return "Keep prior operating policy";
    if (text === "Awaiting CEO") return "Awaiting CEO decision";
    return text;
  }

  #companyIdentity(company) {
    const wrapper = element("div", "company-identity");
    const copy = element("div");
    copy.append(
      element("strong", "", companyLabel(company)),
      element("span", "", company.company_id || "—"),
    );
    wrapper.append(copy);
    return wrapper;
  }

  #setText(node, text) {
    node.textContent = String(text);
  }
}

class DashboardController {
  constructor(api, view) {
    this.api = api;
    this.view = view;
    this.abortController = null;
    this.detailAbortController = null;
    this.eventSource = null;
    this.fallbackTimer = null;
    this.hasData = false;
    this.experiments = [];
    this.runs = [];
    this.lastRenderRevision = null;
    this.refreshPending = false;
    this.refreshTimer = null;
    this.runControlOperation = null;
    this.runControlRetry = null;
    this.selectedExperimentRef = null;
    this.selectedRunRef = null;
    this.startingExperimentRef = null;
    this.streamOpen = false;
    this.view.bind({
      onExperimentChange: (experimentRef) =>
        this.selectExperiment(experimentRef),
      onRefresh: () => this.refresh(true),
      onRunChange: (runId) => this.selectRun(runId),
      onRunControl: () => this.controlSelectedRun(),
      onStart: () => this.startSelectedExperiment(),
    });
  }

  start() {
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        this.#closeEventStream();
        this.#stopFallback();
      } else {
        this.refresh(true);
        this.#connectEventStream();
      }
    });
    window.addEventListener("beforeunload", () => this.#closeEventStream());
    this.refresh(true);
    this.#connectEventStream();
  }

  async refresh(force = false) {
    if (this.abortController && !force) {
      this.refreshPending = true;
      return;
    }
    if (force) this.abortController?.abort();
    const controller = new AbortController();
    this.abortController = controller;
    try {
      const [configuredExperiments, listing] = await Promise.all([
        this.api.getExperiments(controller.signal),
        this.api.getRuns(controller.signal),
      ]);
      if (controller.signal.aborted) return;
      const { runs, resources } = listing;
      this.runs = runs;
      this.experiments = this.#mergeExperiments(configuredExperiments, runs);
      this.selectedExperimentRef = this.#resolveExperimentSelection(
        this.experiments,
        runs,
      );
      this.view.renderExperiments(this.experiments, this.selectedExperimentRef);
      const selectedExperiment = this.experiments.find(
        (experiment) =>
          String(experiment.experiment_ref) === this.selectedExperimentRef,
      );
      this.view.renderResources(resources, selectedExperiment?.campaign_id);
      this.view.renderControl(
        selectedExperiment,
        this.startingExperimentRef === this.selectedExperimentRef,
      );
      const visibleRuns = this.#runsForExperiment(selectedExperiment, runs);
      if (!visibleRuns.length) {
        this.selectedRunRef = null;
        this.hasData = false;
        this.view.renderRuns([], null);
        this.view.showEmpty();
        return;
      }
      this.selectedRunRef = this.#resolveRunSelection(visibleRuns);
      this.view.renderRuns(visibleRuns, this.selectedRunRef);
      const selectedRun = visibleRuns.find(
        (run) => String(run.run_ref) === this.selectedRunRef,
      );
      if (statusKey(selectedRun?.status) === "invalid") {
        this.hasData = false;
        this.view.showInvalidRun(selectedRun);
        return;
      }
      await this.loadRun(force);
    } catch (error) {
      if (error.name !== "AbortError") {
        this.view.showError(error.message || "Unknown connection error", this.hasData);
        this.view.setTransport("offline", "Data unavailable", "Displayed progress may be stale; reconnecting does not restart experiments");
      }
    } finally {
      if (this.abortController === controller) {
        this.abortController = null;
        if (this.refreshPending) {
          this.refreshPending = false;
          this.#queueRefresh(0);
        }
      }
    }
  }

  async loadRun(force = false) {
    if (this.detailAbortController && !force) return;
    this.detailAbortController?.abort();
    const controller = new AbortController();
    this.detailAbortController = controller;
    const detailRunRef = this.selectedRunRef;
    if (!detailRunRef) { this.detailAbortController = null; return; }
    try {
      const detail = await this.api.getRun(detailRunRef, controller.signal);
      if (controller.signal.aborted || detailRunRef !== this.selectedRunRef) return;
      const renderRevision = JSON.stringify([
        detailRunRef,
        detail.economic_revision || detail.updated_at || "",
        detail.status || "",
        detail.phase || "",
        finiteNumber(detail.day),
        finiteNumber(detail.current_window),
        detail.control_action || "none",
        detail.control_label || "",
        detail.companies?.map((company) => company.trajectory?.map((entry) => [
          entry.submitted_at, entry.settled_at, entry.decision_duration_ms,
        ])),
      ]);
      this.view.renderRunTelemetry(detail);
      const pendingControl = this.runControlOperation?.runRef === detailRunRef
        ? this.runControlOperation : null;
      this.view.renderRunControl(detail, pendingControl?.action || null);
      if (force || renderRevision !== this.lastRenderRevision) {
        const operation =
          this.runControlOperation?.runRef === this.selectedRunRef
            ? this.runControlOperation
            : null;
        this.view.render(detail, operation?.action || null);
        if (
          !operation &&
          this.runControlRetry?.runRef === this.selectedRunRef
        ) {
          this.view.renderRunControl(
            {
              ...detail,
              control_action: this.runControlRetry.action,
              control_label: "Resume",
              revision: this.runControlRetry.revision,
            },
          );
        }
        this.lastRenderRevision = renderRevision;
      }
      this.hasData = true;
      if (this.streamOpen) {
        this.view.setTransport(
          "online",
          "Live",
          "Live updates within about one second of state changes",
        );
      }
    } catch (error) {
      if (controller.signal.aborted || detailRunRef !== this.selectedRunRef) return;
      if (error.name !== "AbortError") {
        this.view.showError(error.message || "Unknown connection error", this.hasData);
        if (this.streamOpen) {
          this.view.setTransport(
            "syncing",
            "Retrying data request",
            "Event stream connected; fetching the latest state",
          );
        }
      }
    } finally {
      if (this.detailAbortController === controller) this.detailAbortController = null;
    }
  }

  selectExperiment(experimentRef) {
    this.detailAbortController?.abort();
    this.selectedExperimentRef = String(experimentRef);
    this.selectedRunRef = null;
    this.lastRenderRevision = null;
    this.view.resetTrajectoryState();
    try {
      localStorage.setItem(EXPERIMENT_STORAGE_KEY, this.selectedExperimentRef);
    } catch (_error) {
      // A blocked localStorage must not prevent experiment selection.
    }
    const url = new URL(window.location.href);
    url.searchParams.set("experiment", this.selectedExperimentRef);
    url.searchParams.delete("run");
    history.replaceState(null, "", url);
    this.refresh(true);
  }

  selectRun(runRef) {
    if (runRef === this.selectedRunRef) return;
    this.selectedRunRef = runRef;
    this.lastRenderRevision = null;
    this.view.resetTrajectoryState();
    const selected = this.runs.find(
      (run) => String(run.run_ref) === this.selectedRunRef,
    );
    const experiment = this.experiments.find((item) => item.experiment_ref === this.selectedExperimentRef);
    this.view.renderRuns(this.#runsForExperiment(experiment, this.runs), runRef);
    this.view.renderRunPreview(selected);
    try {
      localStorage.setItem(STORAGE_KEY, runRef);
    } catch (_error) {
      // A blocked localStorage must not prevent run selection.
    }
    const url = new URL(window.location.href);
    url.searchParams.set("run", runRef);
    history.replaceState(null, "", url);
    this.loadRun(true);
  }

  async controlSelectedRun() {
    if (this.runControlOperation) return;
    const selected = this.runs.find(
      (run) => String(run.run_ref) === this.selectedRunRef,
    );
    const retry =
      this.runControlRetry?.runRef === this.selectedRunRef
        ? this.runControlRetry
        : null;
    const action = retry?.action || String(selected?.control_action || "none");
    const revision = retry?.revision ?? selected?.revision;
    if (
      !["pause", "resume"].includes(action) ||
      !Number.isInteger(revision)
    ) {
      return;
    }
    const operation = {
      action,
      idempotencyKey: retry?.idempotencyKey || createIdempotencyKey(),
      revision,
      runRef: this.selectedRunRef,
    };
    this.runControlOperation = operation;
    this.view.renderRunControl(selected, action);
    try {
      const receipt = await this.api.controlRun(
        operation.runRef,
        operation.action,
        operation.revision,
        operation.idempotencyKey,
      );
      this.runControlRetry = null;
      Object.assign(selected, {
        control_action: receipt.control_action,
        control_label: receipt.control_label,
        job_status: receipt.status,
        revision: receipt.revision,
      });
      this.view.renderRunControl(selected);
      this.#queueRefresh(0);
    } catch (error) {
      this.runControlRetry =
        operation.action === "resume" && error.status === 503
          ? operation
          : null;
      this.view.showRunControlError(error.message || "Operation did not complete");
      if (error.status === 409) this.#queueRefresh(0);
    } finally {
      this.runControlOperation = null;
      const current = this.runs.find(
        (run) => String(run.run_ref) === this.selectedRunRef,
      );
      if (this.runControlRetry?.runRef === this.selectedRunRef) {
        this.view.renderRunControl({
          ...current,
          control_action: "resume",
          control_label: "Resume",
          revision: this.runControlRetry.revision,
        });
      } else {
        this.view.renderRunControl(current);
      }
    }
  }

  async startSelectedExperiment() {
    const experiment = this.experiments.find(
      (candidate) =>
        String(candidate.experiment_ref) === this.selectedExperimentRef,
    );
    if (!experiment?.can_start || this.startingExperimentRef) return;
    const previousControl = {
      action: experiment.action,
      can_start: experiment.can_start,
      status: experiment.status,
    };
    let accepted = false;
    this.startingExperimentRef = String(experiment.experiment_ref);
    this.view.renderControl(experiment, true);
    try {
      await this.api.startExperiment(this.startingExperimentRef);
      accepted = true;
      experiment.action = null;
      experiment.can_start = false;
      experiment.status = "starting";
      this.#queueRefresh(0);
    } catch (error) {
      this.view.showStartError(error.message || "Experiment failed to start", this.hasData);
    } finally {
      if (!accepted) Object.assign(experiment, previousControl);
      this.startingExperimentRef = null;
      const current = this.experiments.find(
        (candidate) =>
          String(candidate.experiment_ref) === this.selectedExperimentRef,
      );
      this.view.renderControl(current, false);
    }
  }

  #resolveExperimentSelection(experiments, runs) {
    if (!experiments.length) return null;
    const refs = new Set(
      experiments.map((experiment) => String(experiment.experiment_ref)),
    );
    const runRef = new URLSearchParams(window.location.search).get("run");
    const linkedRun = runs.find((run) => String(run.run_ref) === runRef);
    const linkedExperiment = linkedRun
      ? experiments.find((experiment) =>
          linkedRun.campaign_id
            ? experiment.campaign_id === linkedRun.campaign_id
            : experiment.experiment_ref === `legacy:${linkedRun.run_ref}`,
        )
      : null;
    const urlRef = new URLSearchParams(window.location.search).get(
      "experiment",
    );
    let storedRef = null;
    try {
      storedRef = localStorage.getItem(EXPERIMENT_STORAGE_KEY);
    } catch (_error) {
      // Ignore privacy-mode storage restrictions.
    }
    const preferred = [
      this.selectedExperimentRef,
      linkedExperiment?.experiment_ref,
      urlRef,
      storedRef,
    ].find((ref) => ref && refs.has(String(ref)));
    return String(preferred || experiments[0].experiment_ref);
  }

  #resolveRunSelection(runs) {
    const refs = new Set(runs.map((run) => String(run.run_ref)));
    const urlRef = new URLSearchParams(window.location.search).get("run");
    let storedRef = null;
    try {
      storedRef = localStorage.getItem(STORAGE_KEY);
    } catch (_error) {
      // Ignore privacy-mode storage restrictions.
    }
    const preferred = [this.selectedRunRef, urlRef, storedRef].find(
      (ref) => ref && refs.has(String(ref)),
    );
    const fallback =
      runs.find((run) => statusKey(run.status) === "running") ||
      runs.find((run) => statusKey(run.status) !== "invalid") ||
      runs[0];
    return String(preferred || fallback.run_ref);
  }

  #runsForExperiment(experiment, runs) {
    if (!experiment) return [];
    if (experiment.campaign_id) {
      return runs.filter(
        (run) => String(run.campaign_id || "") === experiment.campaign_id,
      );
    }
    if (String(experiment.experiment_ref).startsWith("legacy:")) {
      const runRef = String(experiment.experiment_ref).slice("legacy:".length);
      return runs.filter((run) => String(run.run_ref) === runRef);
    }
    return [];
  }

  #mergeExperiments(configured, runs) {
    const experiments = configured.map((experiment) => ({ ...experiment }));
    const knownCampaigns = new Set(
      experiments
        .map((experiment) => experiment.campaign_id)
        .filter((campaignId) => campaignId),
    );
    const historicalCampaigns = new Map();
    runs.forEach((run) => {
      if (!run.campaign_id || knownCampaigns.has(run.campaign_id)) return;
      const group = historicalCampaigns.get(run.campaign_id) || [];
      group.push(run);
      historicalCampaigns.set(run.campaign_id, group);
    });
    historicalCampaigns.forEach((campaignRuns, campaignId) => {
      const models = [
        ...new Set(
          campaignRuns.flatMap((run) =>
            Array.isArray(run.model_ids) ? run.model_ids : [],
          ),
        ),
      ];
      experiments.push({
        experiment_ref: `history:${campaignId}`,
        campaign_id: campaignId,
        model_ids: models,
        status: aggregateRunStatus(campaignRuns),
        action: null,
        can_start: false,
        expected_runs: campaignRuns.length,
        completed_runs: campaignRuns.filter((run) =>
          ["complete", "completed"].includes(statusKey(run.status)),
        ).length,
        total_days: Math.max(
          ...campaignRuns.map((run) => finiteNumber(run.total_days)),
        ),
        seed_count: new Set(campaignRuns.map((run) => run.seed)).size,
        message: "Historical campaign (read-only)",
      });
    });
    runs
      .filter((run) => !run.campaign_id)
      .forEach((run) => {
        experiments.push({
          experiment_ref: `legacy:${run.run_ref}`,
          campaign_id: null,
          model_ids: Array.isArray(run.model_ids) ? run.model_ids : [],
          status: run.status,
          action: null,
          can_start: false,
          expected_runs: 1,
          completed_runs: ["complete", "completed"].includes(
            statusKey(run.status),
          )
            ? 1
            : 0,
          total_days: finiteNumber(run.total_days),
          seed_count: 1,
          message: "Standalone run (read-only)",
        });
      });
    return experiments;
  }

  #connectEventStream() {
    if (document.hidden || this.eventSource) return;
    if (!("EventSource" in window)) {
      this.view.setTransport(
        "online",
        "Polling",
        `Fallback mode; polling every ${FALLBACK_REFRESH_MS / 1000} seconds`,
      );
      this.#startFallback();
      return;
    }

    this.view.setTransport("syncing", "Connecting", "Connecting to the read-only event stream");
    const source = this.api.openEvents();
    this.eventSource = source;
    source.onopen = () => {
      if (this.eventSource !== source) return;
      this.streamOpen = true;
      this.#stopFallback();
      this.view.setTransport(
        "online",
        "Live",
        "Live updates within about one second of state changes",
      );
    };
    source.addEventListener("update", () => this.#queueRefresh());
    source.onerror = () => {
      if (this.eventSource !== source) return;
      this.streamOpen = false;
      this.view.setTransport(
        "syncing",
        "Reconnecting",
        `Reconnecting event stream; polling every ${FALLBACK_REFRESH_MS / 1000} seconds`,
      );
      this.#startFallback();
    };
  }

  #closeEventStream() {
    this.eventSource?.close();
    this.eventSource = null;
    this.streamOpen = false;
  }

  #queueRefresh(delay = UPDATE_DEBOUNCE_MS) {
    if (this.refreshTimer !== null) window.clearTimeout(this.refreshTimer);
    this.refreshTimer = window.setTimeout(() => {
      this.refreshTimer = null;
      this.refresh();
    }, delay);
  }

  #startFallback() {
    if (this.fallbackTimer !== null) return;
    this.fallbackTimer = window.setInterval(
      () => this.refresh(),
      FALLBACK_REFRESH_MS,
    );
  }

  #stopFallback() {
    if (this.fallbackTimer !== null) window.clearInterval(this.fallbackTimer);
    this.fallbackTimer = null;
  }
}

new DashboardController(new DashboardApi(), new DashboardView()).start();
