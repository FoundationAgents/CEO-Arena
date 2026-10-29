const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

class Node {
  constructor() { this.children = []; this.dataset = {}; this.attrs = {}; this.style = {}; this.handlers = {}; this.textContent = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(key, value) { this.attrs[key] = value; }
  remove() {}
  addEventListener(name, handler) { this.handlers[name] = handler; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 600, height: 280 }; }
}

function browser() {
  const nodes = new Map();
  const document = {
    activeElement: null,
    createElement: () => new Node(),
    createElementNS: () => new Node(),
    querySelector: key => { if (!nodes.has(key)) nodes.set(key, new Node()); return nodes.get(key); },
  };
  const context = { document, URL, URLSearchParams, AbortController, console,
    window: { location: { href: 'http://localhost/', search: '' } },
    history: { replaceState() {} }, localStorage: { getItem() {}, setItem() {} },
  };
  const source = fs.readFileSync(path.join(__dirname, '../../src/ceo_arena/dashboard/static/app.js'), 'utf8');
  vm.runInNewContext(source.replace('new DashboardController(new DashboardApi(), new DashboardView()).start();',
    'globalThis.exports = {RunPicker, DashboardController, CashTrend, normalizeSeries, experimentDisplayName};'), context);
  return { ...context.exports, nodes, document };
}

const run = (ref, seed, day, status = 'running') => ({
  run_ref: ref, run_id: `run-${ref}`, campaign_id: 'campaign', seed, day, total_days: 500,
  status, model_ids: ['model <literal>', 'second-model'],
});

test('progress updates while picker is focused; fixed columns preserve all lineup text', () => {
  const { RunPicker, nodes, document } = browser();
  const picker = new RunPicker();
  const runs = [run('b', 47, 392, 'failed'), run('a', 11, 126), run('c', 29, 500, 'complete')];
  picker.render(runs, 'a');
  const button = picker.rows.get('a').button;
  document.activeElement = button;
  runs[1].day = 133;
  picker.render(runs.reverse(), 'a');
  assert.equal(picker.rows.get('a').button, button);
  assert.equal(picker.rows.get('a').days.textContent, '133 / 500');
  assert.equal(picker.rows.get('a').lineup.textContent, 'model <literal> + second-model');
  assert.equal(picker.rows.get('b').status.textContent, 'Failed');
  assert.deepEqual(nodes.get('#runOptions').children.map(node => node.dataset.runRef), ['a', 'c', 'b']);
  assert.match(nodes.get('#runPickerValue').textContent, /133/);
  assert.equal(nodes.get('#runOverview').children.length, 4);
});

test('switch fetches only the selected run, updates immediately, and rejects late responses', async () => {
  const { DashboardController } = browser();
  const requests = [];
  const api = { getRun(ref, signal) { return new Promise(resolve => requests.push({ ref, signal, resolve })); } };
  const previews = [], rendered = [];
  const view = new Proxy({ renderRunPreview: run => previews.push(run.run_ref), render: detail => rendered.push(detail.run_id) },
    { get: (obj, key) => obj[key] || (() => {}) });
  const controller = new DashboardController(api, view);
  controller.runs = [run('a', 11, 100), run('b', 29, 200)];
  controller.experiments = [{ experiment_ref: 'experiment', campaign_id: 'campaign' }];
  controller.selectedExperimentRef = 'experiment';
  controller.selectRun('a');
  controller.selectRun('b');
  assert.deepEqual(previews, ['a', 'b']);
  assert.equal(requests[0].signal.aborted, true);
  requests[1].resolve({ run_id: 'run-b', day: 200 });
  await new Promise(resolve => setImmediate(resolve));
  requests[0].resolve({ run_id: 'run-a', day: 100 });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(rendered, ['run-b']);
  controller.selectRun('b');
  assert.equal(requests.length, 2);
});

test('experiment labels hide the scenario tag without changing campaign identity', () => {
  const { experimentDisplayName } = browser();
  const campaign = 'retail-eight-models-20260911';
  assert.equal(experimentDisplayName(campaign), 'eight-models-20260911');
  assert.equal(experimentDisplayName('Retail'), 'Experiment');
  assert.equal(experimentDisplayName(null), 'Standalone run');
  assert.equal(campaign, 'retail-eight-models-20260911');
});

test('cash inspection preserves sparse days and reports recorded cents exactly', () => {
  const { CashTrend, normalizeSeries } = browser();
  const points = normalizeSeries([
    { day: 101, value: -475 }, { day: 0, value: 10000 },
    { day: 7, value: 12345 }, { day: 10, value: null }, { day: 12, value: NaN },
  ]);
  assert.deepEqual(Array.from(points, point => point.day), [0, 7, 101]);
  const chart = new CashTrend(points, 'company-1', 'Model A');
  assert.ok(Math.abs((chart.x(7) - chart.x(0)) / (chart.x(101) - chart.x(0)) - 7 / 101) < 1e-10);
  chart.node.handlers.pointermove({ clientX: chart.x(7) });
  assert.equal(chart.tooltipDay.textContent, 'Day 7');
  assert.equal(chart.tooltipValue.textContent, '$123.45');
  chart.node.handlers.keydown({ key: 'End', preventDefault() {} });
  assert.equal(chart.tooltipDay.textContent, 'Day 101');
  assert.equal(chart.tooltipValue.textContent, '-$4.75');
  chart.node.handlers.keydown({ key: 'ArrowLeft', preventDefault() {} });
  assert.equal(chart.tooltipDay.textContent, 'Day 7');
  chart.node.handlers.pointerleave();
  assert.equal(chart.tooltip.hidden, true);
  assert.equal(chart.guide.attrs.visibility, 'hidden');
});

test('cash axes remain finite and labelled for a flat or single-point history', () => {
  const { CashTrend } = browser();
  for (const value of [0, -100, 50000000]) {
    const chart = new CashTrend([{ day: 0, value }], `flat-${value}`, 'Flat cash');
    chart.draw(300, 268);
    assert.ok(Number.isFinite(chart.x(0)) && Number.isFinite(chart.y(value)));
    assert.ok(chart.maxCash > chart.minCash);
    const labels = chart.svg.children.map(node => node.textContent);
    assert.ok(labels.includes('Cash (USD)') && labels.includes('Day'));
    assert.ok(chart.svg.children.filter(node => node.attrs.class === 'chart-tick').length >= 6);
    chart.inspect(0);
    assert.equal(chart.tooltipDay.textContent, 'Day 0');
  }
});
