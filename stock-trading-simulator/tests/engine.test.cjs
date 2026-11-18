const test = require('node:test');
const assert = require('node:assert/strict');
const T = require('../dist/engine.js');
const code = '300394';
const make = () => T.create(1000000, 42);
const buy = (s, qty = 100, c = code) => T.place(s, { code: c, side: 'buy', type: 'market', qty });
const sell = (s, qty = 100, c = code) => T.place(s, { code: c, side: 'sell', type: 'market', qty });

test('11 requested securities have unique, correct ticker mappings', () => {
  assert.equal(T.ASSETS.length, 11);
  assert.equal(new Set(T.ASSETS.map(a => a.code)).size, 11);
  assert.equal(T.asset('515050').name, '通信ETF华夏');
  assert.equal(T.asset('560780').name, '半导体设备ETF广发');
  assert.equal(T.asset('588200').name, '科创芯片ETF嘉实');
  assert.equal(T.asset('159530').name, '机器人ETF易方达');
});
test('a new account has 1m cash, no invented trades or positions', () => {
  const s = make();
  assert.equal(s.cash, 1000000); assert.equal(T.totals(s).pnl, 0);
  assert.deepEqual(s.positions, {}); assert.equal(s.trades.length, 0);
  assert.equal(s.minute, 45); assert.equal(T.clock(s.minute), '10:15');
});
test('market buy debits cash, includes fees in cost, and locks shares for T+1', () => {
  const s = make(), o = buy(s), t = s.trades[0];
  assert.equal(o.status, 'filled');
  assert.equal(s.cash, T.round(s.initial - t.amount - t.fees.total));
  assert.equal(s.positions[code].cost, T.round(t.amount + t.fees.total));
  assert.equal(T.availableQty(s, code), 0);
  assert.throws(() => sell(s), /可卖数量不足/);
});
test('next day unlocks shares and a sale realizes weighted-average-cost P&L', () => {
  const s = make(); buy(s, 200); const cost = s.positions[code].cost;
  T.nextDay(s); assert.equal(T.availableQty(s, code), 200);
  sell(s); const t = s.trades[0];
  assert.equal(t.realized, T.round(t.amount - t.fees.total - T.round(cost / 2)));
  assert.equal(s.positions[code].qty, 100); assert.equal(s.positions[code].available, 100);
  const totals = T.totals(s);
  assert.ok(Math.abs(totals.pnl - totals.realized - totals.unrealized) < .02);
});
test('limit buy reserves cash, cannot be double spent, and cancellation releases it', () => {
  const s = make(), limit = T.bounds(s, code).lower;
  const o = T.place(s, { code, side: 'buy', type: 'limit', limit, qty: 5000 });
  assert.equal(o.status, 'pending'); assert.ok(T.reserved(s) > 0);
  assert.throws(() => buy(s, 5000), /可用资金不足/);
  T.cancel(s, o.id); assert.equal(T.reserved(s), 0); assert.equal(T.availableCash(s), s.initial);
  assert.throws(() => T.cancel(s, o.id), /已成交或已撤销/);
});
test('limit sell freezes available shares and rejects over-selling', () => {
  const s = make(); buy(s, 200); T.nextDay(s);
  const o = T.place(s, { code, side: 'sell', type: 'limit', limit: T.bounds(s, code).upper, qty: 200 });
  assert.equal(o.status, 'pending'); assert.equal(T.availableQty(s, code), 0);
  assert.throws(() => sell(s, 100), /可卖数量不足/);
  T.cancel(s, o.id); assert.equal(T.availableQty(s, code), 200);
});
test('marketable limit order receives price improvement instead of filling at limit', () => {
  const s = make(), ask = T.execution(s, code, 'buy');
  const o = T.place(s, { code, side: 'buy', type: 'limit', limit: T.bounds(s, code).upper, qty: 100 });
  assert.equal(o.status, 'filled'); assert.equal(o.filledPrice, ask); assert.equal(T.reserved(s), 0);
});
test('pending order fills on subsequent matching tick and only once', () => {
  const s = make(), limit = T.price(code, s.quotes[code].last * .98);
  const o = T.place(s, { code, side: 'buy', type: 'limit', limit, qty: 100 });
  assert.equal(o.status, 'pending');
  s.quotes[code].last = limit * .98;
  T.advance(s); assert.equal(o.status, 'filled');
  T.advance(s, 10); assert.equal(s.trades.length, 1); assert.equal(T.reserved(s), 0);
});
test('unfilled day orders expire at close and release reservations', () => {
  const s = make(), o = T.place(s, { code, side: 'buy', type: 'limit', limit: T.bounds(s, code).lower, qty: 100 });
  T.advance(s, 240); assert.equal(s.minute, 240); assert.equal(o.status, 'expired'); assert.equal(T.reserved(s), 0);
  assert.throws(() => buy(s), /已收盘/);
});
test('invalid quantities, price ticks and insufficient funds do not mutate account', () => {
  const s = make(), before = JSON.stringify(s);
  for (const qty of [0, -100, 1, 99, 100.5, NaN, Infinity, 1e10]) assert.throws(() => buy(s, qty));
  assert.throws(() => T.place(s, { code, side: 'buy', type: 'limit', qty: 100, limit: 140.001 }));
  assert.throws(() => T.place(s, { code, side: 'buy', type: 'limit', qty: 100, limit: Infinity }));
  assert.equal(JSON.stringify(s), before);
});
test('ETF fees omit stock taxes, ETF quotes keep three decimals, and T+1 applies', () => {
  const s = make(); buy(s, 1000, '588200'); const t = s.trades[0];
  assert.equal(t.fees.stamp, 0); assert.equal(t.fees.transfer, 0);
  assert.equal(T.tick('588200'), .001); assert.equal(T.availableQty(s, '588200'), 0);
  T.nextDay(s); sell(s, 1000, '588200');
  assert.equal(s.trades[0].fees.stamp, 0); assert.equal(s.trades[0].fees.transfer, 0);
  assert.equal(s.positions['588200'], undefined);
});
test('max position respects commission and available funds', () => {
  for (const a of T.ASSETS) {
    const s = make(), px = T.execution(s, a.code, 'buy'), qty = T.maxQty(s, a.code, 'buy', px);
    buy(s, qty, a.code); assert.ok(s.cash >= 0);
    assert.throws(() => buy(s, 100, a.code), /可用资金不足/);
  }
});
test('limit-up buy and limit-down sell are blocked', () => {
  const s = make(); s.quotes[code].last = T.bounds(s, code).upper;
  assert.throws(() => buy(s), /涨停/);
  s.quotes[code].last = s.quotes[code].prev; buy(s); T.nextDay(s);
  s.quotes[code].last = T.bounds(s, code).lower;
  assert.throws(() => sell(s), /跌停/);
});
test('seeded simulations resume identically after JSON round-trip', () => {
  const a = make(); buy(a); T.advance(a, 20);
  const b = JSON.parse(JSON.stringify(a)); T.advance(a, 10); T.advance(b, 10);
  assert.deepEqual(a, b);
});
test('day rollover processes outstanding orders before unlocking shares', () => {
  const s = make(); const limit = T.price(code, s.quotes[code].last * .98);
  const o = T.place(s, { code, side: 'buy', type: 'limit', limit, qty: 100 });
  s.quotes[code].last = limit * .97; T.nextDay(s);
  assert.equal(o.status, 'filled'); assert.equal(T.availableQty(s, code), 100);
  assert.equal(s.day, 2); assert.equal(s.minute, 0);
});
test('multi-day simulation preserves price bounds and account reconciliation', () => {
  const s = make();
  for (const a of T.ASSETS) buy(s, 100, a.code);
  for (let d = 0; d < 10; d++) {
    T.nextDay(s);
    for (let m = 0; m < 24; m++) {
      T.advance(s, 10);
      for (const a of T.ASSETS) {
        const q = s.quotes[a.code], b = T.bounds(s, a.code);
        assert.ok(q.last >= b.lower && q.last <= b.upper);
        assert.equal(q.points.length, s.minute + 1);
      }
      const t = T.totals(s); assert.ok(Math.abs(t.pnl - t.realized - t.unrealized) < .02);
    }
  }
  assert.ok(s.drawdown >= 0 && s.drawdown <= 100); assert.ok(s.cash >= 0);
});
test('simulated clock skips lunch and terminates at 15:00', () => {
  assert.equal(T.clock(0), '09:30'); assert.equal(T.clock(120), '11:30');
  assert.equal(T.clock(121), '13:01'); assert.equal(T.clock(240), '15:00');
});
