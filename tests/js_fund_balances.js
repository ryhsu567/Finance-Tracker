// Harness for test_fund_parity.py: loads the real site/data.js + site/app.js
// under minimal DOM/localStorage shims and prints the sinking-fund balance
// app.js computes for every lumpy category in every month, as JSON.
//
// Run directly: node tests/js_fund_balances.js
const fs = require('fs');
const path = require('path');

const SITE = path.join(__dirname, '..', 'site');

// app.js's tooltip IIFE bails out when there is no #tooltip element, which is
// all the DOM this needs. No override is set, so localStorage reads empty and
// effectiveTarget() falls through to the configured target — the comparison is
// against build_dashboard.py, which only ever sees configured targets.
global.document = {
  getElementById: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {},
};
global.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};
global.window = {};

// eval rather than require: these are browser scripts, not modules. In
// CommonJS (non-strict) the function declarations land in this scope.
eval(fs.readFileSync(path.join(SITE, 'data.js'), 'utf8'));
eval(fs.readFileSync(path.join(SITE, 'app.js'), 'utf8'));

const DATA = window.DATA;
const out = {};
for (const [cat, meta] of Object.entries(DATA.categories)) {
  if (meta.cadence !== 'lumpy') continue;
  out[cat] = DATA.months.map((_, i) => categoryResult(DATA, cat, i).balance);
}
process.stdout.write(JSON.stringify(out));
