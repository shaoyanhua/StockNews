(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.Trading = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const ASSETS = [
    { code: '300394', name: '天孚通信', sector: '光通信', market: 'SZ', base: 140.34, limit: .20 },
    { code: '600487', name: '亨通光电', sector: '光通信', market: 'SH', base: 43.89, limit: .10 },
    { code: '000636', name: '风华高科', sector: '电子元件', market: 'SZ', base: 32.59, limit: .10 },
    { code: '300308', name: '中际旭创', sector: '光通信', market: 'SZ', base: 508.90, limit: .20 },
    { code: '600183', name: '生益科技', sector: '电子材料', market: 'SH', base: 75.71, limit: .10 },
    { code: '300475', name: '香农芯创', sector: '存储芯片', market: 'SZ', base: 95.77, limit: .20 },
    { code: '600206', name: '有研新材', sector: '半导体材料', market: 'SH', base: 38.60, limit: .10 },
    { code: '515050', name: '通信ETF华夏', sector: '通信主题', market: 'SH', base: 1.62, limit: .10, etf: true },
    { code: '560780', name: '半导体设备ETF广发', sector: '半导体设备', market: 'SH', base: .785, limit: .10, etf: true },
    { code: '588200', name: '科创芯片ETF嘉实', sector: '科创芯片', market: 'SH', base: .894, limit: .20, etf: true },
    { code: '159530', name: '机器人ETF易方达', sector: '机器人', market: 'SZ', base: 1.320, limit: .10, etf: true }
  ];
  const round = (v, digits = 2) => Math.round((v + Number.EPSILON) * 10 ** digits) / 10 ** digits;
  const asset = code => ASSETS.find(a => a.code === code);
  const price = (code, value) => round(value, asset(code).etf ? 3 : 2);
  const tick = code => asset(code).etf ? .001 : .01;
  let replayData = null;
  function setReplayData(data) {
    if (!data || !data.datasetId || !ASSETS.every(a => data.assets[a.code]?.bars.length === 240)) throw Error('真实行情数据不完整');
    replayData = data;
  }
  function createReplay() {
    if (!replayData) throw Error('尚未载入真实行情');
    const s = { version: 2, mode: 'replay', datasetId: replayData.datasetId, date: replayData.date,
      initial: 20000, cash: 20000, day: 1, minute: 0, quotes: {}, positions: {}, orders: [], trades: [], curve: [], nextId: 1,
      settings: { commission: .00025, minimum: 5, stamp: .0005, transfer: .00001 }, peak: 0, drawdown: 0 };
    for (const a of ASSETS) {
      const d = replayData.assets[a.code], qty = a.etf ? 10000 : 100;
      s.positions[a.code] = { qty, available: qty, cost: round(qty * d.prev) };
      s.initial = round(s.initial + qty * d.prev);
      s.quotes[a.code] = { prev: d.prev, open: d.open, last: d.open, high: d.open, low: d.open,
        volume: 0, amount: 0, avg: d.open,
        points: [{ time: 0, price: d.open, open: d.open, high: d.open, low: d.open, volume: 0, amount: 0, avg: d.open }] };
    }
    s.peak = s.initial; recordEquity(s); return s;
  }
  function requireReplay(s) {
    if (!replayData || s.datasetId !== replayData.datasetId) throw Error('当前存档与行情数据日期不匹配');
  }
  function advanceReplay(s, steps) {
    requireReplay(s);
    for (let n = 0; n < Math.min(240, steps) && s.minute < 240; n++) {
      s.minute++;
      for (const a of ASSETS) {
        const q = s.quotes[a.code], b = replayData.assets[a.code].bars[s.minute - 1];
        q.last = b.close; q.high = Math.max(q.high, b.high); q.low = Math.min(q.low, b.low);
        q.volume += b.volume; q.amount += b.amount; q.avg = q.volume ? q.amount / q.volume : b.close;
        q.points.push({ time: s.minute, price: b.close, open: b.open, high: b.high, low: b.low,
          volume: b.volume, amount: b.amount, avg: q.avg });
      }
      match(s);
      if (s.minute === 240) for (const o of s.orders) if (o.status === 'pending') o.status = 'expired';
      recordEquity(s);
    }
  }
  function metrics(s, code) {
    requireReplay(s);
    const q = s.quotes[code], d = replayData.assets[code], elapsed = s.minute;
    const points = q.points.slice(1), recent = points.slice(-5).reduce((n,p) => n+p.volume,0), prior = points.slice(-10,-5).reduce((n,p) => n+p.volume,0);
    const earlier = q.points[Math.max(0,q.points.length-6)].price;
    return { change: (q.last/q.prev-1)*100, amplitude: (q.high-q.low)/q.prev*100,
      volumeRatio: elapsed ? (q.volume/elapsed)/(d.average5DayVolume/240) : null,
      turnover: q.volume/d.floatUnits*100, recentVolume: elapsed >= 5 ? recent : null,
      recentRatio: elapsed >= 10 && prior ? recent/prior : null,
      momentum5: elapsed >= 5 ? (q.last/earlier-1)*100 : null,
      vwapBias: q.volume ? (q.last/q.avg-1)*100 : null };
  }
  function marketContext(s) {
    requireReplay(s);
    const indices = ['999999','399001','399006'].map(code => {
      const d = replayData.indices[code];
      const b = s.minute ? d.bars[s.minute-1] : null;
      return { code, name:d.name, last:b ? b.close : d.open, change:((b ? b.close : d.open)/d.prev-1)*100,
        up:b?.up ?? null, down:b?.down ?? null,
        amount: d.bars.slice(0,s.minute).reduce((n,p)=>n+p.amount,0),
        points:[d.open,...d.bars.slice(0,s.minute).map(p=>p.close)] };
    });
    const up = s.minute ? indices[0].up+indices[1].up : null;
    const down = s.minute ? indices[0].down+indices[1].down : null;
    const temperature = up+down > 0 ? up/(up+down)*100 : null;
    const changes = ASSETS.map(a=>(s.quotes[a.code].last/s.quotes[a.code].prev-1)*100);
    const above = ASSETS.filter(a=>s.quotes[a.code].volume && s.quotes[a.code].last >= s.quotes[a.code].avg).length;
    const average = changes.reduce((v,n)=>v+n,0)/changes.length;
    const benchmark = 20000+ASSETS.reduce((v,a)=>v+s.quotes[a.code].last*(a.etf?10000:100),0);
    return {indices,up,down,temperature,amount:indices[0].amount+indices[1].amount,
      rising:changes.filter(v=>v>0).length,falling:changes.filter(v=>v<0).length,above,average,benchmark:round(benchmark),
      excess:round(totals(s).equity-benchmark)};
  }
  function random(s) {
    s.seed = (Math.imul(s.seed, 1664525) + 1013904223) >>> 0;
    return s.seed / 4294967296;
  }
  function create(initial = 1000000, seed = Date.now() >>> 0, scenario = 'mixed') {
    if (!Number.isFinite(initial) || initial < 10000 || initial > 100000000) throw Error('初始资金须在 1 万至 1 亿元之间');
    if (!['mixed', 'bull', 'bear', 'volatile'].includes(scenario)) throw Error('未知训练场景');
    const s = { version: 1, initial: round(initial), cash: round(initial), seed, day: 1, minute: 0, scenario,
      quotes: {}, positions: {}, orders: [], trades: [], curve: [], nextId: 1,
      settings: { commission: .00025, minimum: 5, stamp: .0005, transfer: .00001 }, peak: initial, drawdown: 0 };
    for (const a of ASSETS) {
      s.quotes[a.code] = { prev: a.base, open: a.base, last: a.base, high: a.base, low: a.base, volume: 0,
        points: [{ time: 0, price: a.base, volume: 0 }] };
    }
    for (let i = 0; i < 45; i++) advance(s);
    s.curve = [{ day: 1, minute: 45, value: initial }];
    return s;
  }
  function fees(s, code, side, amount) {
    const a = asset(code), cfg = s.settings;
    const commission = round(Math.max(cfg.minimum, amount * cfg.commission));
    const stamp = !a.etf && side === 'sell' ? round(amount * cfg.stamp) : 0;
    const transfer = !a.etf ? round(amount * cfg.transfer) : 0;
    return { commission, stamp, transfer, total: round(commission + stamp + transfer) };
  }
  function bounds(s, code) {
    const a = asset(code), q = s.quotes[code];
    return { upper: price(code, q.prev * (1 + a.limit)), lower: Math.max(tick(code), price(code, q.prev * (1 - a.limit))) };
  }
  function execution(s, code, side) {
    const q = s.quotes[code], b = bounds(s, code);
    if ((side === 'buy' && q.last >= b.upper) || (side === 'sell' && q.last <= b.lower)) return null;
    return price(code, Math.max(b.lower, Math.min(b.upper, q.last + tick(code) * (side === 'buy' ? 1 : -1))));
  }
  function reserved(s, ignoreId) {
    return round(s.orders.filter(o => o.status === 'pending' && o.side === 'buy' && o.id !== ignoreId)
      .reduce((sum, o) => sum + o.limit * o.qty + fees(s, o.code, 'buy', o.limit * o.qty).total, 0));
  }
  const availableCash = (s, ignoreId) => round(s.cash - reserved(s, ignoreId));
  function availableQty(s, code, ignoreId) {
    const p = s.positions[code];
    if (!p) return 0;
    const held = s.orders.filter(o => o.status === 'pending' && o.side === 'sell' && o.code === code && o.id !== ignoreId).reduce((sum, o) => sum + o.qty, 0);
    return p.available - held;
  }
  function totals(s) {
    const market = Object.entries(s.positions).reduce((sum, [c, p]) => sum + p.qty * s.quotes[c].last, 0);
    const cost = Object.values(s.positions).reduce((sum, p) => sum + p.cost, 0);
    const equity = round(s.cash + market);
    const realized = round(s.trades.reduce((sum, t) => sum + (t.realized || 0), 0));
    return { market: round(market), equity, pnl: round(equity - s.initial), unrealized: round(market - cost), realized,
      returnPct: (equity / s.initial - 1) * 100, exposure: market / equity * 100, fees: round(s.trades.reduce((v, t) => v + t.fees.total, 0)) };
  }
  function recordEquity(s) {
    const value = totals(s).equity;
    s.peak = Math.max(s.peak, value);
    s.drawdown = Math.max(s.drawdown, (s.peak - value) / s.peak * 100);
    const prev = s.curve[s.curve.length - 1];
    if (prev && prev.day === s.day && prev.minute === s.minute) prev.value = value;
    else s.curve.push({ day: s.day, minute: s.minute, value });
    if (s.curve.length > 12000) s.curve.splice(1, 1);
  }
  function fill(s, o, value) {
    const amount = round(value * o.qty), charge = fees(s, o.code, o.side, amount);
    if (o.side === 'buy' && amount + charge.total > availableCash(s, o.id) + .001) return false;
    if (o.side === 'sell' && o.qty > availableQty(s, o.code, o.id)) return false;
    let realized = null;
    if (o.side === 'buy') {
      s.cash = round(s.cash - amount - charge.total);
      const p = s.positions[o.code] || { qty: 0, available: 0, cost: 0 };
      p.qty += o.qty;
      p.cost = round(p.cost + amount + charge.total);
      s.positions[o.code] = p;
    } else {
      const p = s.positions[o.code], cost = round(p.cost * o.qty / p.qty);
      realized = round(amount - charge.total - cost);
      s.cash = round(s.cash + amount - charge.total);
      p.cost = round(p.cost - cost); p.qty -= o.qty; p.available -= o.qty;
      if (!p.qty) delete s.positions[o.code];
    }
    o.status = 'filled'; o.filledPrice = value; o.filledDay = s.day; o.filledMinute = s.minute;
    s.trades.unshift({ id: o.id, code: o.code, side: o.side, qty: o.qty, price: value, amount, fees: charge,
      day: s.day, minute: s.minute, realized, note: o.note });
    recordEquity(s);
    return true;
  }
  function match(s) {
    for (const o of s.orders) {
      if (o.status !== 'pending') continue;
      const value = execution(s, o.code, o.side);
      if (value !== null && (o.side === 'buy' ? value <= o.limit : value >= o.limit)) fill(s, o, value);
    }
  }
  function place(s, { code, side, type, qty, limit, note = '' }) {
    if (!asset(code)) throw Error('请选择有效的证券');
    if (!['buy', 'sell'].includes(side) || !['market', 'limit'].includes(type)) throw Error('无效的委托类型');
    if (s.minute >= 240) throw Error(s.mode === 'replay' ? '当日回放已结束，可复盘或重新开盘练习' : '模拟市场已收盘，请进入下一交易日');
    if (!Number.isSafeInteger(qty) || qty <= 0 || qty % 100 !== 0) throw Error('委托数量必须是 100 股 / 份的正整数倍');
    const b = bounds(s, code), value = type === 'market' ? execution(s, code, side) : limit;
    if (value === null) throw Error(side === 'buy' ? '模拟涨停封板，暂不接受市价买入' : '模拟跌停封板，暂不接受市价卖出');
    if (!Number.isFinite(value) || value <= 0 || Math.abs(price(code, value) - value) > .0000001) throw Error('请输入符合最小价格单位的有效委托价格');
    if (value < b.lower || value > b.upper) throw Error(`委托价格须在 ${b.lower} 至 ${b.upper} 之间`);
    if (side === 'buy' && value * qty + fees(s, code, side, value * qty).total > availableCash(s) + .001) throw Error('可用资金不足（需预留手续费及挂单冻结资金）');
    if (side === 'sell' && qty > availableQty(s, code)) throw Error('可卖数量不足：当日买入须下一交易日卖出，挂单数量也会冻结');
    const o = { id: s.nextId++, code, side, type, qty, limit: value, note: String(note).slice(0, 240), status: 'pending', day: s.day, minute: s.minute };
    s.orders.push(o);
    if (type === 'market') fill(s, o, value); else match(s);
    return o;
  }
  function cancel(s, id) {
    const o = s.orders.find(o => o.id === id && o.status === 'pending');
    if (!o) throw Error('这笔委托已成交或已撤销');
    o.status = 'cancelled';
  }
  function maxQty(s, code, side, value) {
    if (side === 'sell') return availableQty(s, code);
    if (!Number.isFinite(value) || value <= 0) return 0;
    const cash = availableCash(s);
    let qty = Math.max(0, Math.floor(cash / value / 100) * 100);
    while (qty && qty * value + fees(s, code, 'buy', qty * value).total > cash + .001) qty -= 100;
    return qty;
  }
  function advance(s, steps = 1) {
    if (s.mode === 'replay') return advanceReplay(s, steps);
    for (let n = 0; n < Math.min(240, steps) && s.minute < 240; n++) {
      s.minute++;
      const bias = { mixed: 0, bull: .00022, bear: -.00022, volatile: 0 }[s.scenario];
      const common = (random(s) - .5) * .002;
      for (const a of ASSETS) {
        const q = s.quotes[a.code], b = bounds(s, a.code);
        const volatility = (a.etf ? .0025 : .0045) * (s.scenario === 'volatile' ? 2 : 1);
        const move = (random(s) - .5) * volatility + common + bias;
        q.last = price(a.code, Math.max(b.lower, Math.min(b.upper, q.last * (1 + move))));
        q.high = Math.max(q.high, q.last); q.low = Math.min(q.low, q.last);
        const volume = Math.floor((random(s) * 800 + 100) * (a.etf ? 20 : 1)) * 100;
        q.volume += volume;
        q.points.push({ time: s.minute, price: q.last, volume });
      }
      match(s);
      if (s.minute === 240) for (const o of s.orders) if (o.status === 'pending') o.status = 'expired';
      recordEquity(s);
    }
  }
  function nextDay(s) {
    if (s.mode === 'replay') throw Error('只载入当日真实行情，不生成虚构的下一交易日；请重新开盘练习');
    advance(s, 240 - s.minute);
    s.day++; s.minute = 0;
    for (const p of Object.values(s.positions)) p.available = p.qty;
    for (const a of ASSETS) {
      const prev = s.quotes[a.code].last;
      const open = price(a.code, Math.max(tick(a.code), prev * (1 + (random(s) - .5) * .016)));
      s.quotes[a.code] = { prev, open, last: open, high: open, low: open, volume: 0, points: [{ time: 0, price: open, volume: 0 }] };
    }
    recordEquity(s);
  }
  function clock(minute) {
    const minutes = minute <= 120 ? 570 + minute : 780 + minute - 120;
    return `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`;
  }
  return { ASSETS, create, createReplay, setReplayData, metrics, marketContext, asset, round, price, tick, fees, bounds, execution, reserved, availableCash, availableQty, totals, place, cancel, maxQty, advance, nextDay, clock };
});
