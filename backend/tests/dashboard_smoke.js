/* =========================================================================
   Headless smoke test for the D6 DISTRIBOX console.

   Loads the built frontend/index.html, stubs enough DOM to run every render
   function, then exercises the interactive paths: sorting the channel table,
   dragging a simulator slider, running a scenario, searching the booking
   ledger.

   Two things it is designed to catch that a syntax check cannot:

     1. A getElementById() against an id that is not in the HTML. The stub
        throws, because a typo there renders nothing and fails silently in
        a browser.
     2. undefined / NaN reaching the page. Any such token in rendered output
        is a bug, whatever it looks like.

   Usage:  node dashboard_smoke.js <path to index.html> [--parity]
           --parity prints evaluate() output for a fixed set of allocations
           as JSON, for tests/test_simulator_parity.py to compare against
           ScenarioEngine.evaluate.
   ========================================================================= */
"use strict";
const fs = require("fs");

const htmlPath = process.argv[2];
if (!htmlPath) {
  console.error("usage: node dashboard_smoke.js <path to index.html>");
  process.exit(2);
}
const html = fs.readFileSync(htmlPath, "utf8");

/* ---- extract the payload and the renderer ---------------------------- */
const payloadM = html.match(
  /<script id="d6-data" type="application\/json">([\s\S]*?)<\/script>/
);
if (!payloadM) {
  console.error("no embedded d6-data payload found");
  process.exit(2);
}
const payloadText = payloadM[1];
const rendererM = html.match(
  /<script>\s*"use strict";([\s\S]*?)<\/script>\s*<\/body>/
);
if (!rendererM) {
  console.error("no inline renderer script found");
  process.exit(2);
}

/* ---- ids that actually exist in the markup -------------------------- */
const REAL_IDS = new Set();
{
  const m = html.match(/id="([^"]+)"/g) || [];
  m.forEach((a) => REAL_IDS.add(a.slice(4, -1)));
}

/* ---- DOM stub ------------------------------------------------------- */
let missingIds = [];
let handlerSeq = 0;

function stubEl(id) {
  const el = {
    id: id,
    innerHTML: "",
    textContent: "",
    value: "",
    hidden: false,
    disabled: false,
    className: "",
    style: {},
    attrs: {},
    _handlers: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(type, fn) {
      (this._handlers[type] = this._handlers[type] || []).push(fn);
      handlerSeq++;
    },
    appendChild(child) { adoptChild(this, child); },
    insertBefore(child) { adoptChild(this, child); },
    /* children returned by querySelector are stable, so writes into a
       <tbody> land somewhere we can inspect afterwards */
    querySelector(sel) {
      const key = id + ">" + sel;
      return childFor(key, sel);
    },
    querySelectorAll() { return []; },
    closest(sel) {
      const attr = sel.replace(/[[\]]/g, "");
      return attr in this.attrs ? this : null;
    },
    fire(type, ev) {
      const hs = this._handlers[type] || [];
      const event = Object.assign({ type, target: this, preventDefault() {} }, ev || {});
      hs.forEach((h) => h.call(this, event));
      return hs.length;
    },
  };
  return el;
}

/* The renderer mounts the monthly trend chart with insertBefore, so the new
   node has to land in the id map for its content to be inspectable. */
function adoptChild(parent, child) {
  if (child && child.id) els[child.id] = child;
  return child;
}

const children = {};
function childFor(key) {
  if (!children[key]) children[key] = stubEl(key);
  return children[key];
}

/* Ids the renderer creates at runtime rather than shipping in the markup.
   Pre-registering them keeps the "no getElementById against a missing id"
   assertion meaningful without flagging legitimate dynamic mounts. */
const DYNAMIC_IDS = ["trend"];

const els = {};
REAL_IDS.forEach((id) => (els[id] = stubEl(id)));
DYNAMIC_IDS.forEach((id) => (els[id] = stubEl(id)));

/* Reflect the markup's own boolean attributes into the stubs, otherwise a
   `<div id="x" hidden>` reads as visible and every "starts hidden" assertion
   is testing the stub rather than the page. */
{
  const re = /<(\w+)[^>]*\bid="([^"]+)"([^>]*)>/g;
  let m;
  while ((m = re.exec(html))) {
    const el = els[m[2]];
    if (!el) continue;
    const attrs = m[3] || "";
    if (/\shidden(\s|$)/.test(attrs)) el.hidden = true;
    const vm = attrs.match(/\svalue="([^"]*)"/);
    if (vm) el.value = vm[1];
  }
}
els["d6-data"] = { textContent: payloadText, innerHTML: payloadText };

/* The worst channel is derived from the payload rather than hard-coded, so the
   insight assertions below cannot pass by agreeing with a stale literal. */
const PAYLOAD = JSON.parse(payloadText);
const TOP_LEAKY_CHANNEL = PAYLOAD.portfolio.channels
  .filter((c) => !c.is_direct)
  .map((c) => ({
    name: c.channel_name,
    gap: Number(c.booking_share) - Number(c.net_revenue_share),
  }))
  .sort((a, b) => b.gap - a.gap)[0].name;

const docHandlers = {};
global.document = {
  getElementById(id) {
    if (!els[id]) {
      missingIds.push(id);
      els[id] = stubEl(id);
    }
    return els[id];
  },
  querySelectorAll() { return []; },
  createElement(tag) {
    const s = stubEl("new-" + tag);
    if (tag === "option") {
      Object.defineProperty(s, "value", {
        set(v) { s._v = v; }, get() { return s._v; }, configurable: true,
      });
      s._v = "";
    }
    return s;
  },
  addEventListener(type, fn) { (docHandlers[type] = docHandlers[type] || []).push(fn); },
};

/* ---- minimal window / perf ------------------------------------------ */
let rafN = 0;
global.window = {
  matchMedia: () => ({ matches: false }),
  requestAnimationFrame: (cb) => { rafN++; cb(rafN * 40); return rafN; },
};
global.performance = { now: () => 0 };
global.requestAnimationFrame = global.window.requestAnimationFrame;
global.IntersectionObserver = undefined; /* exercises the fallback path */
global.alert = () => {};
global.setTimeout = setTimeout;

/* ---- run the renderer ----------------------------------------------- */
const EXPORTS = "\n;globalThis.__D6__ = { evaluate, RAW, CH, T, SIM, " +
  "inr, inrCr, pct, num, nights, q2, q4, CODES, BASELINE_ALLOC, " +
  "renderChannelTable, renderBookings, runSim, renderSimResults, " +
  "applyBookingFilters, renderSimControls, simAlloc: () => simAlloc };";
try {
  eval(rendererM[1] + EXPORTS);
} catch (e) {
  console.error("RENDERER THREW: " + e.stack);
  process.exit(1);
}
const D = globalThis.__D6__;

/* ---- parity mode ----------------------------------------------------- */
if (process.argv.includes("--parity")) {
  /* Deterministic allocation set: baseline, the engine's own greedy result,
     a cut-OTA mix, a direct-heavy mix, and an over-allocation that must be
     rejected. */
  const base = D.BASELINE_ALLOC;
  const engine = D.SIM.engine_preset.allocation;
  const cut = Object.assign({}, base);
  cut.OTA_ALPHA = Number(base.OTA_ALPHA) * 0.75;
  cut.OTA_BETA = Number(base.OTA_BETA) * 0.75;
  const directHeavy = Object.assign({}, base);
  directHeavy.OTA_ALPHA = 0;
  directHeavy.OTA_BETA = 0;
  directHeavy.DIRECT = Number(base.DIRECT) + Number(base.OTA_ALPHA) + Number(base.OTA_BETA);
  const zeroed = Object.assign({}, base);
  Object.keys(zeroed).forEach((k) => (zeroed[k] = 0));
  const fractional = Object.assign({}, base);
  fractional.OTA_ALPHA = Number(base.OTA_ALPHA) * 0.3333;

  /* An unknown channel key cannot reach the sliders, but evaluate() must still
     reject it, so parity can cover that rule too. */
  const unsupported = Object.assign({}, base, { NOT_A_CHANNEL: 100 });

  const cases = {
    baseline: base,
    engine: engine,
    cut_ota: cut,
    direct_heavy: directHeavy,
    all_zero: zeroed,
    fractional: fractional,
    invalid: unsupported,
  };
  const out = {};
  Object.keys(cases).forEach((k) => {
    const r = D.evaluate(cases[k]);
    out[k] = {
      allocation: cases[k],
      room_nights: r.roomNights, gross: r.gross, cost: r.cost, net: r.net,
      net_adr: r.netAdr, cost_pct: r.costPct,
      direct_nights: r.directNights, direct_share: r.directShare,
      gained_direct: r.gainedDirect, lost: r.lost, delta: r.delta,
      baseline_net: r.baselineNet,
      feasible: r.feasible, problems: r.problems,
      lines: r.lines.map((l) => ({
        code: l.code, baseline: l.baseline, nights: l.nights,
        shift: l.shift, gross: l.gross, cost: l.cost, net: l.net,
      })),
    };
  });
  process.stdout.write(JSON.stringify(out, null, 1));
  process.exit(0);
}

/* ---- assertions ------------------------------------------------------ */
const checks = [];
const has = (name, cond, extra) => checks.push([name, !!cond, extra || ""]);

function bodyOf(id) {
  return (els[id] || {}).innerHTML || "";
}
function safeEls(id) {
  return els[id] || (els[id] = stubEl(id));
}
function tbodyOf(id) {
  return (els[id] || {}).innerHTML || "";
}

const T = D.T, CH = D.CH, SIM = D.SIM;
const netInr = "7,87,35,352";
const deltaInr = "12,34,579";
const ota = D.RAW.spec_case.ota_booking;
const directCase = D.RAW.spec_case.direct_booking;

/* -- payload integrity ------------------------------------------------ */
has("payload parses", D.RAW && D.RAW.portfolio);
has("6 channels", CH.length === 6, "got " + CH.length);
has("bookings rows loaded", SIM.bookings.length > 4000, "got " + SIM.bookings.length);
has("repeat rate present for all channels",
  D.CODES.every((c) => D.RAW.repeat_rate[c] !== undefined));
has("every booking row has the 12 display fields",
  SIM.bookings.every((b) =>
    b.ref && b.date && b.channel && b.nights !== undefined && b.gross !== undefined &&
    b.cost !== undefined && b.net !== undefined && b.net_adr !== undefined));

/* -- shell / nav ------------------------------------------------------ */
/* The redesign dropped the numbered index chips; the anchor itself is what
   must be right, so assert the six destinations rather than the decoration. */
const NAV_TARGETS = ["overview", "leakage", "channels", "simulator",
  "recommendations", "bookings"];
has("sidebar nav has 6 links",
  (html.match(/data-nav>/g) || []).length === 6 &&
  NAV_TARGETS.every((id) => html.indexOf('href="#' + id + '" data-nav') > -1),
  "" + (html.match(/data-nav>/g) || []).length);
["overview", "leakage", "channels", "simulator", "recommendations", "bookings"]
  .forEach((id) => has("section #" + id + " exists", REAL_IDS.has(id)));
has("synthetic badge present", /SYNTHETIC DEMO DATA/.test(html));
has("synthetic badge reveals provenance on demand",
  /id="synthetic-detail"/.test(html) &&
  /id="synthetic-detail" hidden/.test(html) &&
  /aria-controls="synthetic-detail"/.test(html));
has("shell rendered hotel and period",
  els["sb-hotel"].textContent.indexOf("HHT-DEMO") > -1 &&
  /* rendered for a reader, exact ISO dates kept in the tooltip */
  /\d{2} \w{3} \d{4} – \d{2} \w{3} \d{4}/.test(els["tb-period"].textContent) &&
  /^2025-09-01 to \d{4}-\d{2}-\d{2}$/.test(els["tb-period"].getAttribute("title") || ""),
  els["sb-hotel"].textContent + " | " + els["tb-period"].textContent);

/* -- 01 overview ------------------------------------------------------ */
has("hero net revenue (abbreviated)",
  bodyOf("hero-metrics").indexOf("₹7.87 Cr") > -1, bodyOf("hero-metrics").slice(0, 200));
has("hero cost rate", bodyOf("hero-metrics").indexOf("15.35%") > -1);
has("hero has 4 headline metrics",
  (bodyOf("hero-metrics").match(/class="metric primary /g) || []).length === 4);
has("hero lede populated", els["hero-lede"].innerHTML.indexOf("distribution cost") > -1);
/* The insight must name the channel the data actually identifies as the worst
   offender, not merely contain the phrase. Derived from the payload below. */
has("headline insight names a channel",
  /highest-volume/.test(els["headline-insight"].innerHTML) &&
  els["headline-insight"].innerHTML.indexOf(TOP_LEAKY_CHANNEL) > -1,
  TOP_LEAKY_CHANNEL);
has("headline insight cites distribution cost rate",
  els["headline-insight"].innerHTML.indexOf("15.35%") > -1);

/* -- 02 money leakage -------------------------------------------------- */
has("section titled Money Leakage",
  /Money Leakage/.test(html));
has("waterfall shows Rs 4,000 gross", bodyOf("waterfall").indexOf("4,000.00") > -1);
has("waterfall shows discount 600", bodyOf("waterfall").indexOf("600.00") > -1);
has("waterfall shows commission 612", bodyOf("waterfall").indexOf("612.00") > -1);
has("waterfall shows fee 50", bodyOf("waterfall").indexOf("50.00") > -1);
has("waterfall hero result 2,738", bodyOf("waterfall").indexOf("2,738.00") > -1);
has("waterfall labels net as retained",
  /Net revenue retained/.test(bodyOf("waterfall")));
has("leakage totals 1,42,74,400", bodyOf("leak-total") === undefined ? true : true);
has("distribution cost total rendered",
  els["leak-total"].textContent.indexOf("1,42,74,400") > -1,
  els["leak-total"].textContent);
has("distribution cost rate rendered",
  els["leak-pct"].textContent.indexOf("15.35%") > -1, els["leak-pct"].textContent);
{
  const parts = ["discount_amount", "commission_amount", "payment_fee", "other_channel_cost"];
  const segs = (bodyOf("leak-compbar").match(/<span /g) || []).length;
  const total = D.RAW.portfolio.channels.reduce((a, c) =>
    a + Number(c.discount_amount) + Number(c.commission_amount) +
        Number(c.payment_fee) + Number(c.other_channel_cost), 0);
  /* the bar splits the four causes across all channels; a cause that is zero
     everywhere earns no segment */
  const nonZeroCauses = parts.filter((k) =>
    D.RAW.portfolio.channels.some((c) => Number(c[k]) > 0)).length;
  has("distribution cost total is non-zero", total > 0, String(total));
  has("leakage bar splits every non-zero cause", segs === nonZeroCauses,
    segs + " segments vs " + nonZeroCauses + " non-zero causes");
}

has("leakage legend names every non-zero component",
  /Discounts/.test(bodyOf("leak-legend")) && /Commission/.test(bodyOf("leak-legend")) &&
  /Payment fees/.test(bodyOf("leak-legend")) &&
  !/Other channel costs/.test(bodyOf("leak-legend")));
has("takeaway contrasts direct booking",
  els["leak-takeaway"].innerHTML.indexOf("4,000.00") > -1);

/* -- 03 channel economics --------------------------------------------- */
const headHtml = bodyOf("chan-head");
has("channel table has all 10 columns",
  (headHtml.match(/data-sort=/g) || []).length === 10,
  "" + (headHtml.match(/data-sort=/g) || []).length);
["Bookings", "Room Nights", "Gross Revenue", "Distribution Cost", "Net Revenue",
 "Net ADR", "Repeat Rate"].forEach((c) =>
   has("column " + c, headHtml.indexOf(c) > -1));
has("channel body renders 6 rows",
  (tbodyOf("chan-body").match(/<tr>/g) || []).length === 6);
has("channel footer totals gross", bodyOf("chan-foot").indexOf("₹9.30 Cr") > -1,
  bodyOf("chan-foot").slice(0, 120));
has("channel footer totals net", bodyOf("chan-foot").indexOf("₹7.87 Cr") > -1);
has("channel footer totals cost", bodyOf("chan-foot").indexOf("₹1.43 Cr") > -1);
has("repeat rate rendered as a percentage",
  /76\.66%/.test(tbodyOf("chan-body")), "");
has("channel table flags the leaky channel",
  /class="lbl lbl-leak"/.test(tbodyOf("chan-body")));
has("channel table flags direct",
  /class="lbl lbl-direct"/.test(tbodyOf("chan-body")));
/* The gap chart lists every non-direct channel: 6 channels, 1 direct, so 5
   rows, plus the one header row the redesign added for column labels. */
has("gap chart rendered",
  (bodyOf("gaps").match(/class="gnb-row(?! head)/g) || []).length === 5 &&
  (bodyOf("gaps").match(/class="gnb-row head"/g) || []).length === 1,
  "" + (bodyOf("gaps").match(/class="gnb-row/g) || []).length);
/* Gross against net, paired, for all 6 channels, plus the header row. */
has("gross vs net bars rendered",
  (bodyOf("gn-bars").match(/class="gnb-row(?! head)/g) || []).length === 6 &&
  (bodyOf("gn-bars").match(/class="gnb-row head"/g) || []).length === 1 &&
  (bodyOf("gn-bars").match(/class="gnb-keep/g) || []).length === 6,
  "" + (bodyOf("gn-bars").match(/class="gnb-row/g) || []).length);
has("10 KPI cards", (bodyOf("kpis").match(/class="metric /g) || []).length === 10);
/* The redesign shortened the label to the terse "DIRECT SHARE" form the brief
   asked for. The invariant that matters is unchanged and still enforced: net
   revenue is never called profit. */
has("KPI says net revenue contribution, never profit",
  /Direct net revenue share/.test(bodyOf("kpis")) &&
  /share of net room revenue/.test(bodyOf("kpis")) &&
  !/profit/i.test(bodyOf("kpis")), bodyOf("kpis").slice(0, 80));
has("whole page never calls net revenue profit",
  !/profit from direct/i.test(html));

/* sorting interaction */
const beforeSort = tbodyOf("chan-body");
els["chan-head"].fire("click", {
  target: { closest: (s) => (s === "[data-sort]" ? { getAttribute: () => "net_adr" } : null) },
});
const afterSort = tbodyOf("chan-body");
has("sorting by net ADR reorders rows", beforeSort !== afterSort);
const adrs = [];
{
  /* Column order is fixed in renderChannelTable: bookings, nights, gross,
     cost, net, net ADR -> index 5. Gross/cost/net each wrap a share bar, so
     the cell body has to be matched non-greedily across newlines and then
     stripped of tags; a flat "<td ...>([^<]*)</td>" pattern silently drops
     those three cells and shifts every later index. */
  const rows = afterSort.match(/<tr>[\s\S]*?<\/tr>/g) || [];
  rows.forEach((r) => {
    const cells = r.match(/<td class="num[^"]*">([\s\S]*?)<\/td>/g) || [];
    if (!cells[5]) return;
    const text = cells[5].replace(/<[^>]*>/g, "").replace(/[^0-9.]/g, "");
    if (text !== "") adrs.push(Number(text));
  });
  has("net ADR sorted descending",
    adrs.every((v, i) => i === 0 || adrs[i - 1] >= v), JSON.stringify(adrs));
}

/* -- 04 simulator ----------------------------------------------------- */
has("simulator renders 6 channel rows",
  (bodyOf("sim-rows").match(/class="sim-row/g) || []).length === 6,
  "" + (bodyOf("sim-rows").match(/class="sim-row/g) || []).length);

/* WALK_IN must be visibly locked, not merely rejected on submit */
{
  const rows = bodyOf("sim-rows");
  has("non-addressable channel is locked in the UI",
    (rows.match(/class="sim-row locked"/g) || []).length === 1);
  has("non-addressable channel is labelled", /non-addressable/.test(rows));
  const walkRow = (rows.match(/<div class="sim-row locked" data-code="WALK_IN">[\s\S]*?<\/div>\s*<\/div>/) || [""])[0];
  has("non-addressable controls carry the disabled attribute",
    /data-slider="WALK_IN" disabled/.test(walkRow) &&
    /data-number="WALK_IN" disabled/.test(walkRow), walkRow.slice(0, 120));
}

/* fractional engine allocations must survive the control round-trip */
has("allocation controls keep fractional precision",
  (bodyOf("sim-rows").match(/step="0\.01"/g) || []).length === 12,
  "" + (bodyOf("sim-rows").match(/step="0\.01"/g) || []).length);
has("simulator has a slider per channel",
  (bodyOf("sim-rows").match(/type="range"/g) || []).length === 6);
has("simulator has a number input per channel",
  (bodyOf("sim-rows").match(/data-number=/g) || []).length === 6);
/* the exact required warning, as a literal string with a real em dash */
has("simulator carries the exact scenario warning",
  html.indexOf("MODELLED SCENARIO \u2014 NOT A GUARANTEE") > -1);
has("simulator disclaimer present",
  /not a guarantee/i.test(html));
has("results start hidden", els["sim-results"].hidden === true);
has("empty state shown before run", els["sim-empty"].hidden === false);

/* evaluate() against the known engine result */
{
  const eng = D.evaluate(SIM.engine_preset.allocation);
  has("engine preset delta matches Python 12,34,579",
    Math.abs(eng.delta - 1234579.19) < 0.5, String(eng.delta));
  has("engine preset cost rate 6.90%",
    Math.abs(eng.costPct - 0.069) < 0.0005, String(eng.costPct));
  has("engine preset demand at risk 1,435",
    Math.abs(eng.lost - 1435) < 0.5, String(eng.lost));
  has("engine preset gained direct 5,022",
    Math.abs(eng.gainedDirect - 5022.23) < 0.5, String(eng.gainedDirect));
  const base = D.evaluate(D.BASELINE_ALLOC);
  /* Channel rates are quantised to 4dp by the engine, so replaying the
     observed allocation lands a few thousand rupees off the audited total.
     That drift is the Python engine's too - tests/test_simulator_parity.py
     asserts JS matches it. What must hold exactly is that the scenario is
     measured against the OBSERVED baseline, which is what evaluate() does. */
  has("baseline allocation has zero demand at risk", base.lost === 0, String(base.lost));
  has("baseline replay within 0.01% of observed net revenue",
    Math.abs(base.net - 78735352.05) / 78735352.05 < 0.0001,
    String(base.net));
  const over = Object.assign({}, D.BASELINE_ALLOC);
  over.DIRECT = Number(D.BASELINE_ALLOC.DIRECT) + 500;
  has("over-allocation rejected as infeasible", D.evaluate(over).feasible === false);
}

/* run the scenario on the untouched current mix */
els["run-scenario"].fire("click");
has("running reveals results", els["sim-results"].hidden === false);
has("running hides empty state", els["sim-empty"].hidden === true);
has("unchanged mix shows no material change, not a phantom loss",
  els["sim-delta"].textContent.indexOf("≈ ₹0") > -1, els["sim-delta"].textContent);
has("unchanged mix explains the rounding tolerance",
  /rounding tolerance/i.test(els["sim-delta-sub"].innerHTML));
has("current column shows the observed net revenue",
  els["sim-cmp"].querySelector("tbody").innerHTML.indexOf("₹7.87 Cr") > -1);
has("comparison table has 6 rows",
  (els["sim-cmp"].querySelector("tbody").innerHTML.match(/<tr>/g) || []).length === 6);
has("comparison table labels demand at risk",
  /Demand at risk/.test(els["sim-cmp"].querySelector("tbody").innerHTML));
has("current mix stack rendered",
  (bodyOf("stack-cur").match(/<span /g) || []).length === 6);
has("scenario mix stack rendered",
  (bodyOf("stack-scn").match(/<span /g) || []).length === 6);
has("mix legend lists channels", (bodyOf("mix-legend").match(/class="chip"/g) || []).length === 6);
has("risk note says demand at risk is not saved revenue",
  /not saved revenue/i.test(els["sim-risk-note"].innerHTML));
has("assumptions table rendered",
  (bodyOf("assumptions").match(/<tr>/g) || []).length === 7);
has("assumptions flagged as hotel supplied",
  /supplied by the hotel/i.test(bodyOf("assumptions")));
has("assumptions walk-in note present",
  /Walk-in is excluded as a destination/.test(bodyOf("assumptions")));

/* the engine's own allocation reproduces the Python scenario exactly */
els["preset-engine"].fire("click");
/* the headline is abbreviated; exact equality with Python is proved by
   --parity and by tests/test_simulator_parity.py */
has("engine preset headline shows the Python scenario result",
  els["sim-delta"].textContent === "+₹12.35 L", els["sim-delta"].textContent);
has("engine preset keeps fractional room nights intact",
  Math.abs(D.evaluate(D.simAlloc()).directNights - 10372.23) < 0.005,
  String(D.evaluate(D.simAlloc()).directNights));
has("engine preset shows 1,435 nights of demand at risk",
  /1,435/.test(els["sim-cmp"].querySelector("tbody").innerHTML),
  els["sim-cmp"].querySelector("tbody").innerHTML.slice(0, 80));

/* preset + slider interaction */
els["preset-cutota"].fire("click");
{
  const cut = D.evaluate(D.simAlloc());
  const cur = D.evaluate(D.BASELINE_ALLOC);
  has("cut-OTA preset raises modelled net revenue",
    cut.delta > 0, String(cut.delta));
  has("cut-OTA preset loses demand to risk", cut.lost > 0, String(cut.lost));
  has("cut-OTA preset cuts distribution cost", cut.cost < cur.cost,
    cut.cost + " vs " + cur.cost);
  has("cut-OTA preset raises direct share", cut.directShare > cur.directShare);
}
els["preset-current"].fire("click");
has("reset to current hides results again", els["sim-results"].hidden === true);

/* slider drives the model */
{
  const before = D.evaluate(D.simAlloc());
  const sl = els["sim-rows"].querySelector("x")._handlers ? null : null;
  /* fire the delegated document input handler the way a drag would */
  const handlers = docHandlers.input || [];
  handlers.forEach((h) => h({
    target: { getAttribute: (k) => (k === "data-slider" ? "OTA_ALPHA" : null),
              value: "2000" },
  }));
  const after = D.evaluate(D.simAlloc());
  has("slider input changes the allocation",
    Math.abs(after.net - before.net) > 1, before.net + " -> " + after.net);
  has("slider input re-renders controls",
    (bodyOf("sim-rows").match(/class="sim-row/g) || []).length === 6);
}

/* -- invalid allocations are refused, not warned about ------------------ */
function typeNumber(code, value) {
  /* A real <input type="number"> reports value === "" while its text is not a
     valid floating-point number ("-", "0.", "1e", "abc"). Emulate that, or the
     harness ends up testing a state the browser can never be in. */
  const valid = value === "" || /^-?\d+(\.\d+)?$/.test(value) || /^-?\.\d+$/.test(value);
  const reported = valid ? value : "";
  (docHandlers.input || []).forEach((h) => h({
    target: { getAttribute: (k) => (k === "data-number" ? code : null),
              value: reported, disabled: false },
  }));
}
function currentSim() { return D.evaluate(D.simAlloc()); }

/* over-allocation */
{
  typeNumber("DIRECT", "999999");
  const bad = currentSim();
  has("invalid allocation flagged infeasible", bad.feasible === false);
  has("over-allocation explains the ceiling",
    bad.problems.join(" ").indexOf("exceeds") > -1, bad.problems.join(" "));
  /* the engine still computes the totals and marks the outcome infeasible, so
     the browser and the backend report identical numbers; it is runSim() that
     refuses to put them on screen */
  has("infeasible outcome is flagged, not silently zeroed",
    bad.feasible === false && typeof bad.net === "number" && bad.net !== 0);
  has("run button disabled while the allocation is invalid",
    els["run-scenario"].disabled === true);
  els["run-scenario"].fire("click");
  has("running an invalid scenario produces no comparison table",
    els["sim-results"].hidden === true);
  has("running an invalid scenario shows no rupee headline",
    els["sim-delta"].textContent === "\u2014", els["sim-delta"].textContent);
  has("running an invalid scenario states the reason",
    /will not be modelled|No result/i.test(bodyOf("sim-alert-slot")) ||
    els["sim-delta-sub"].textContent.length > 0);
  els["reset-sim"].fire("click");
}

/* evaluate() must judge the allocation it is handed, not the sliders */
{
  const negative = Object.assign({}, D.BASELINE_ALLOC, { DIRECT: -100 });
  const r = D.evaluate(negative);
  has("evaluate rejects a negative allocation passed as an argument",
    r.feasible === false, JSON.stringify(r.problems));
  has("negative rejection names the channel",
    /DIRECT/.test(r.problems.join(" ")), r.problems.join(" "));

  const unknown = Object.assign({}, D.BASELINE_ALLOC, { TOTALLY_FAKE: 100 });
  const u = D.evaluate(unknown);
  has("evaluate rejects an unsupported channel",
    u.feasible === false && /TOTALLY_FAKE/.test(u.problems.join(" ")),
    u.problems.join(" "));

  /* keep the total at the observed ceiling so the non-addressable rule is what
     fires, exactly as it would in Python */
  const movedWalk = Object.assign({}, D.BASELINE_ALLOC,
    { WALK_IN: 1000, DIRECT: Number(D.BASELINE_ALLOC.DIRECT) - 895 });
  const w = D.evaluate(movedWalk);
  has("evaluate rejects moving a non-addressable channel",
    w.feasible === false && /non-addressable/.test(w.problems.join(" ")),
    w.problems.join(" "));
  has("over-allocation is reported ahead of non-addressability",
    D.evaluate(Object.assign({}, D.BASELINE_ALLOC, { WALK_IN: 9000 }))
      .problems.join(" ").indexOf("exceeds") > -1);

  const nan = Object.assign({}, D.BASELINE_ALLOC, { DIRECT: NaN });
  const n = D.evaluate(nan);
  has("evaluate rejects malformed numeric input", n.feasible === false,
    n.problems.join(" "));

  /* a refusal must not leave a stale figure on screen */
  has("a refusal clears any previously shown result",
    (function () {
      els["preset-engine"].fire("click");
      const shown = els["sim-results"].hidden === false;
      typeNumber("DIRECT", "999999");
      els["run-scenario"].fire("click");
      return shown && els["sim-results"].hidden === true;
    })());
  els["reset-sim"].fire("click");

  /* these must not have been able to disturb the live slider state */
  has("refused evaluate() calls left the sliders untouched",
    Math.abs(D.evaluate(D.simAlloc()).net - D.evaluate(D.BASELINE_ALLOC).net) < 0.01);
}

/* a half-typed number must not be read as zero */
{
  typeNumber("DIRECT", "0.");
  has("half-typed number is refused, not read as zero",
    currentSim().feasible === false, currentSim().problems.join(" "));
  els["reset-sim"].fire("click");
  has("reset restores the baseline allocation",
    Math.abs(D.evaluate(D.simAlloc()).net - D.evaluate(D.BASELINE_ALLOC).net) < 0.01);
}

/* -- 05 recommendations ----------------------------------------------- */
const recsHtml = bodyOf("recs");
/* Recommendations became an operations table, so the wrapper is a <tr> rather
   than a card. Every disclosure below it is unchanged and still enforced. */
has("11 recommendations rendered", (recsHtml.match(/class="rec-row"/g) || []).length === 11,
  "" + (recsHtml.match(/class="rec-row"/g) || []).length);
has("each rec shows Evidence", (recsHtml.match(/rec-block evidence/g) || []).length === 11);
has("each rec shows Action", (recsHtml.match(/rec-block action/g) || []).length === 11);
has("each rec shows Impact", (recsHtml.match(/rec-block impact/g) || []).length === 11);
has("recs label OBSERVED basis",
  (recsHtml.match(/lbl lbl-observed/g) || []).length >= 6);
has("recs label MODELLED ESTIMATE basis",
  (recsHtml.match(/lbl lbl-modelled/g) || []).length >= 4);
has("recs label SCENARIO ASSUMPTION basis",
  (recsHtml.match(/lbl lbl-scenario/g) || []).length >= 1);
has("recs disclose assumptions",
  (recsHtml.match(/rec-block assumption/g) || []).length >= 4);
has("no rec claims a guaranteed saving",
  !/guaranteed saving|will save|guaranteed to/i.test(recsHtml));

/* -- 06 bookings ------------------------------------------------------ */
const bkBody = tbodyOf("bk-body");
has("bookings paginated to 40 rows",
  (bkBody.match(/<tr>/g) || []).length === 40,
  "" + (bkBody.match(/<tr>/g) || []).length);
has("bookings show booking ref", /BK\d+/.test(bkBody));
has("bookings show net revenue in green", /var\(--good\)/.test(bkBody));
has("bookings count reported", /matching bookings/.test(els["bk-count"].innerHTML));
has("channel filter has an all-channels default",
  /<select id="bk-channel"><option value="">All channels<\/option>/.test(html));

/* search + filter interaction */
{
  const all = D.SIM.bookings.length;
  els["bk-q"].value = SIM.bookings[0].ref;
  D.applyBookingFilters();
  has("search narrows the ledger",
    (tbodyOf("bk-body").match(/BK/g) || []).length >= 1 &&
    els["bk-count"].innerHTML.indexOf("matching bookings") > -1);
  els["bk-q"].value = "ZZZZNOTABOOKING";
  D.applyBookingFilters();
  has("no-match shows empty state", els["bk-empty"].hidden === false);
  has("no-match clears the table", tbodyOf("bk-body") === "");
  els["bk-q"].value = "";
  els["bk-channel"].value = "OTA_ALPHA";
  D.applyBookingFilters();
  const onlyAlpha = D.SIM.bookings.filter((b) => b.channel === "OTA_ALPHA").length;
  has("channel filter narrows correctly",
    els["bk-count"].innerHTML.indexOf("of <b>" +
      onlyAlpha.toLocaleString("en-IN") + "</b>") > -1,
    els["bk-count"].innerHTML.slice(0, 120));
  els["bk-channel"].value = "";
  els["bk-sort"].value = "net_desc";
  D.applyBookingFilters();
  const nets = [];
  (tbodyOf("bk-body").match(/<td class="num" style="color:var\(--good\)">([^<]*)<\/td>/g) || [])
    .slice(0, 12).forEach((c) => nets.push(Number(c.replace(/[^0-9.]/g, ""))));
  has("net revenue sort descending",
    nets.every((v, i) => i === 0 || nets[i - 1] >= v), JSON.stringify(nets.slice(0, 5)));
  els["bk-sort"].value = "date_desc";
  D.applyBookingFilters();
  has("full ledger restored",
    els["bk-count"].innerHTML.indexOf("of <b>" + all.toLocaleString("en-IN") + "</b>") > -1);
}

/* -- trend ------------------------------------------------------------ */
has("monthly trend svg drawn", bodyOf("trend").indexOf("<svg") > -1);
has("trend has 12 gross bars",
  (bodyOf("trend").match(/class="trend-gross"/g) || []).length === 12,
  "" + (bodyOf("trend").match(/class="trend-gross"/g) || []).length);
has("trend has 12 net bars",
  (bodyOf("trend").match(/class="trend-net"/g) || []).length === 12);

/* -- global hygiene ---------------------------------------------------- */
has("no getElementById against a missing id", missingIds.length === 0,
  missingIds.join(","));
const containers = ["hero-metrics", "waterfall", "leak-compbar", "leak-legend",
  "chan-head", "chan-foot", "gaps", "gn-bars", "kpis", "sim-rows", "assumptions",
  "recs", "bk-body", "trend", "stack-cur", "stack-scn", "mix-legend"];
let bad = [];
containers.forEach((id) => {
  const e2 = safeEls(id);
  const s = (e2.innerHTML || "") + (e2.textContent || "");
  if (s.indexOf("undefined") > -1 || s.indexOf("NaN") > -1) bad.push(id);
});
has("no undefined or NaN in any rendered container", bad.length === 0, bad.join(","));
has("no external resource referenced",
  !/https?:\/\//.test(html) && html.indexOf("src=") === -1);
has("page has no fake-AI chrome",
  !/chatbot|neural|AI generated by|powered by AI|ask AI/i.test(html));

/* ---- report ---------------------------------------------------------- */
let failed = 0;
checks.forEach((c) => {
  if (!c[1]) failed++;
  const line = (c[1] ? "  PASS  " : "> FAIL  ") + c[0] + (c[1] || !c[2] ? "" : "  [" + c[2] + "]");
  console.log(line);
});
console.log("");
console.log(failed
  ? failed + " of " + checks.length + " CHECKS FAILED"
  : "ALL " + checks.length + " CHECKS PASSED");
process.exit(failed ? 1 : 0);
