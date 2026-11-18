const test = require('node:test');
const assert = require('node:assert/strict');
const Flow = require('../dist/capital-flow.js');
const data = require('../data/capital-flow-2026-11-12.json');
test('Daily closing flow cannot leak into intraday replay', () => {
  for (const minute of [0, 1, 120, 239]) {
    const row = Flow.at(data, '2026-11-12', '300394', minute);
    assert.equal(row.status, 'locked');
    assert.equal(row.main, undefined);
  }
});
test('Close and deliberate reveal show observed signed net; never infer gross', () => {
  for (const [minute, reveal] of [[240, false], [5, true]]) {
    const row = Flow.at(data, '2026-11-12', '300394', minute, reveal);
    assert.equal(row.status, 'ready');
    assert.equal(row.main.net, -357909856);
    assert.equal(row.super.net, -76378384);
    assert.equal(row.main.inflow, null);
    assert.equal(row.main.outflow, null);
  }
});
test('Wrong dates and unavailable securities are missing, never zero', () => {
  for (const [date, code] of [['2026-11-13', '300394'], ['2026-11-12', '515050'], ['2026-11-12', '000000']]) {
    const row = Flow.at(data, date, code, 240, true);
    assert.equal(row.status, 'unavailable');
    assert.equal(row.main, undefined);
  }
});
test('Raw net identity and normalized source record agree', () => {
  const raw = require('../data/capital-flow-raw-2026-11-12.json');
  const row = raw['300394'].data.klines.find(line => line.startsWith('2026-11-12,')).split(',');
  assert.equal(Number(row[1]), Number(row[4]) + Number(row[5]));
  assert.equal(data.assets['300394'].main.net, Number(row[1]));
});
