const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const T = require('../dist/engine.js');
const sandbox={window:{}};
vm.runInNewContext(fs.readFileSync(require.resolve('../dist/replay-data.js'),'utf8'),sandbox);
const data=JSON.parse(JSON.stringify(sandbox.window.REPLAY_DATA));
T.setReplayData(data);
const fresh=()=>T.createReplay();
const place=(s,code,side,qty,type='market',limit)=>T.place(s,{code,side,qty,type,limit});

test('real dataset contains the requested day and exactly 240 continuous trading minutes',()=>{
  assert.equal(data.date,'2026-11-12');
  for(const a of T.ASSETS){
    const d=data.assets[a.code];assert.equal(d.bars.length,240);
    assert.equal(d.bars[0].label,'09:31');assert.equal(d.bars[119].label,'11:30');
    assert.equal(d.bars[120].label,'13:01');assert.equal(d.bars[239].label,'15:00');
    assert.equal(d.bars.at(-1).close,d.close);
  }
});
test('preset is seven 100-share holdings, four 10000-unit ETFs and cash 20000',()=>{
  const s=fresh();assert.equal(s.cash,20000);assert.equal(s.minute,0);
  assert.equal(Object.keys(s.positions).length,11);assert.equal(s.orders.length,0);assert.equal(s.trades.length,0);
  for(const a of T.ASSETS){const qty=a.etf?10000:100;assert.equal(s.positions[a.code].qty,qty);assert.equal(T.availableQty(s,a.code),qty);assert.equal(s.positions[a.code].cost,T.round(qty*data.assets[a.code].prev));}
  assert.equal(s.initial,20000+T.ASSETS.reduce((sum,a)=>sum+T.round(data.assets[a.code].prev*(a.etf?10000:100)),0));
});
test('preset overnight holdings are immediately sellable; sale reconciles realized cost',()=>{
  const s=fresh();place(s,'300394','sell',100);assert.equal(s.positions['300394'],undefined);
  const tr=s.trades[0];assert.equal(tr.realized,T.round(tr.amount-tr.fees.total-14406));
  assert.equal(s.cash,T.round(20000+tr.amount-tr.fees.total));
});
test('new buys are locked while remaining overnight quantity stays available',()=>{
  const s=fresh();place(s,'300394','buy',100);assert.equal(s.positions['300394'].qty,200);assert.equal(T.availableQty(s,'300394'),100);
  assert.throws(()=>place(s,'300394','sell',200),/可卖数量不足/);
  place(s,'300394','sell',100);assert.equal(s.positions['300394'].qty,100);assert.equal(T.availableQty(s,'300394'),0);
});
test('replay follows raw minute close, high, low, amount and volume without synthesis',()=>{
  const s=fresh();T.advance(s,1);const b=data.assets['300394'].bars[0],q=s.quotes['300394'];
  assert.equal(q.last,145.67);assert.equal(q.volume,3168500);assert.equal(q.high,b.high);assert.equal(q.low,b.low);
  assert.equal(q.amount,b.amount);assert.equal(q.avg,b.amount/b.volume);assert.equal(q.points.length,2);
  T.advance(s,239);for(const a of T.ASSETS){const d=data.assets[a.code],q=s.quotes[a.code];assert.equal(q.last,d.close);assert.equal(q.high,d.high);assert.equal(q.low,d.low);assert.equal(q.volume,d.bars.reduce((n,b)=>n+b.volume,0));}
});
test('market breadth is timestamp-aligned and excludes double-counted ChiNext',()=>{
  const s=fresh();assert.equal(T.marketContext(s).temperature,null);T.advance(s,30);let m=T.marketContext(s);
  const sh=data.indices['999999'].bars[29],sz=data.indices['399001'].bars[29];
  assert.equal(m.up,sh.up+sz.up);assert.equal(m.down,sh.down+sz.down);
  assert.equal(m.temperature,m.up/(m.up+m.down)*100);
  assert.equal(m.indices[0].last,sh.close);assert.equal(m.indices[0].points.length,31);
  T.advance(s,210);m=T.marketContext(s);assert.equal(m.up,846);assert.equal(m.down,4379);
});
test('no future data contributes to cumulative metrics, plots or volume comparisons',()=>{
  const s=fresh();T.advance(s,9);assert.equal(T.metrics(s,'300394').recentRatio,null);
  assert.equal(s.quotes['300394'].points.length,10);T.advance(s,1);
  const bars=data.assets['300394'].bars.slice(0,10),m=T.metrics(s,'300394');
  assert.equal(m.recentRatio,bars.slice(5).reduce((n,b)=>n+b.volume,0)/bars.slice(0,5).reduce((n,b)=>n+b.volume,0));
  assert.equal(m.volumeRatio,(bars.reduce((n,b)=>n+b.volume,0)/10)/(data.assets['300394'].average5DayVolume/240));
});
test('hold benchmark is zero excess without trading and charges reduce excess',()=>{
  const s=fresh();assert.equal(T.marketContext(s).excess,0);T.advance(s,60);assert.equal(T.marketContext(s).excess,0);
  place(s,'515050','sell',10000);assert.ok(T.marketContext(s).excess<0);
  const t=T.totals(s);assert.ok(Math.abs(t.pnl-t.realized-t.unrealized)<.02);
});
test('limits do not use future bars and trigger only after actual price reaches order',()=>{
  const s=fresh();const o=place(s,'300394','sell',100,'limit',150);
  assert.equal(o.status,'pending');assert.equal(T.availableQty(s,'300394'),0);
  T.advance(s,1);assert.equal(o.status,'pending');
  T.advance(s,60);assert.equal(o.status,'filled');assert.ok(o.filledPrice>=150);assert.ok(o.filledMinute>1);
});
test('replay ends at real close, expires pending orders and cannot invent next day',()=>{
  const s=fresh();const o=place(s,'300394','buy',100,'limit',120);T.advance(s,240);
  assert.equal(o.status,'expired');assert.equal(T.reserved(s),0);assert.throws(()=>place(s,'300394','buy',100),/回放已结束/);
  const before=JSON.stringify(s);T.advance(s,240);assert.equal(JSON.stringify(s),before);assert.throws(()=>T.nextDay(s),/不生成/);
});
test('JSON restored replay advances identically',()=>{
  const s=fresh();T.advance(s,15);place(s,'159530','sell',1000);const restored=JSON.parse(JSON.stringify(s));
  T.advance(s,30);T.advance(restored,30);assert.deepEqual(s,restored);
});
test('complete account reconciles through realistic mixed overnight trades',()=>{
  const s=fresh();place(s,'300308','sell',100);place(s,'588200','buy',10000);T.advance(s,50);
  place(s,'515050','sell',10000);T.advance(s,80);place(s,'000636','buy',100);T.advance(s,110);
  const t=T.totals(s);assert.ok(s.cash>=0);assert.ok(Math.abs(t.pnl-t.realized-t.unrealized)<.02);
  assert.equal(T.availableQty(s,'588200'),10000);assert.equal(T.availableQty(s,'000636'),100);
});
