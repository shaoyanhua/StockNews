(function () {
  'use strict';
  const T = window.Trading, $ = id => document.getElementById(id);
  const DATA = window.REPLAY_DATA;
  if (!DATA) { document.body.innerHTML = '<p style="padding:40px">行情文件未载入，请确保 replay-data.js 与网页在同一文件夹，然后刷新。</p>'; return; }
  T.setReplayData(DATA);
  const STORAGE = `lianpan.replay.v2.${DATA.date}`;
  const fmt = (v, digits = 2) => Number(v).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const signed = (v, digits = 2) => { const rounded = T.round(v, digits); return rounded === 0 ? fmt(0, digits) : `${rounded > 0 ? '+' : ''}${fmt(rounded, digits)}`; };
  const tone = v => v > 0.000001 ? 'up' : v < -0.000001 ? 'down' : 'neutral';
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const quoteFmt = (code, v) => fmt(v, T.asset(code).etf ? 3 : 2);
  const units = n => n >= 1e8 ? `${fmt(n/1e8,2)} 亿` : n >= 1e4 ? `${fmt(n/1e4,2)} 万` : fmt(n,0);
  let state, selected = '300394', side = 'buy', filter = 'all', sort = 0, chartType = 'line', activity = 'positions', view = 'trade';
  let timer = null, toastTimer, storageWorks = true, startupMessage = '';
  function validState(s) {
    const finite = (v, min = 0) => Number.isFinite(v) && v >= min;
    const integer = v => Number.isSafeInteger(v) && v >= 0;
    if (!s || s.version !== 2 || s.mode !== 'replay' || s.datasetId !== DATA.datasetId || s.date !== DATA.date || s.initial !== T.createReplay().initial || !finite(s.cash) || s.day !== 1 || !integer(s.minute) || s.minute > 240 || !integer(s.nextId)) return false;
    if (!s.settings || !finite(s.peak, s.initial) || !finite(s.drawdown) || s.drawdown > 100) return false;
    if (['commission', 'minimum', 'stamp', 'transfer'].some(k => !finite(s.settings[k]) || s.settings[k] > (k === 'minimum' ? 1000 : .1))) return false;
    if (!s.quotes || !s.positions || typeof s.positions !== 'object') return false;
    for (const a of T.ASSETS) {
      const q = s.quotes[a.code];
      if (!q || ['prev', 'open', 'last', 'high', 'low','avg'].some(k => !finite(q[k], T.tick(a.code))) || !finite(q.volume) || !finite(q.amount) || !Array.isArray(q.points) || q.points.length !== s.minute + 1) return false;
      const d = DATA.assets[a.code];
      if (q.prev !== d.prev || q.open !== d.open || q.last !== (s.minute ? d.bars[s.minute-1].close : d.open)) return false;
      if (q.points.some((p, i) => p.time !== i || !finite(p.price, T.tick(a.code)) || !finite(p.volume) || !finite(p.amount) || !finite(p.avg,T.tick(a.code)) || (i>0 && (p.price !== d.bars[i-1].close || p.volume !== d.bars[i-1].volume)))) return false;
    }
    for (const [code, p] of Object.entries(s.positions)) {
      if (!T.asset(code) || !p || !integer(p.qty) || !p.qty || p.qty % 100 || !integer(p.available) || p.available % 100 || p.available > p.qty || !finite(p.cost)) return false;
    }
    if (!Array.isArray(s.orders) || !Array.isArray(s.trades) || !Array.isArray(s.curve) || s.orders.length > 100000 || s.trades.length > 100000 || s.curve.length > 12000) return false;
    const ids = new Set();
    for (const o of s.orders) {
      if (!o || !T.asset(o.code) || !integer(o.id) || ids.has(o.id) || o.id >= s.nextId || !['buy', 'sell'].includes(o.side) || !['market', 'limit'].includes(o.type) || !integer(o.qty) || !o.qty || o.qty % 100 || !finite(o.limit, T.tick(o.code)) || !['pending', 'filled', 'cancelled', 'expired'].includes(o.status) || !integer(o.day) || !integer(o.minute) || o.minute > 240 || typeof o.note !== 'string' || o.note.length > 240) return false;
      ids.add(o.id);
    }
    if (s.trades.some(t => !t || !T.asset(t.code) || !ids.has(t.id) || !['buy', 'sell'].includes(t.side) || !integer(t.qty) || !finite(t.price, T.tick(t.code)) || !finite(t.amount) || !t.fees || ['commission', 'stamp', 'transfer', 'total'].some(k => !finite(t.fees[k])) || !(t.realized === null || Number.isFinite(t.realized)) || !integer(t.day) || !integer(t.minute) || typeof t.note !== 'string' || t.note.length > 240)) return false;
    if (s.curve.some(p => !p || !finite(p.value) || !integer(p.day) || !integer(p.minute))) return false;
    if (T.availableCash(s) < -.01 || T.ASSETS.some(a => T.availableQty(s, a.code) < 0)) return false;
    return true;
  }
  try {
    const raw = localStorage.getItem(STORAGE);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (!validState(parsed)) throw Error('存档格式异常');
      state = parsed;
    }
  } catch {
    startupMessage = '原存档无法读取，已尝试保留副本；已创建新的练习账户。';
    try { const raw = localStorage.getItem(STORAGE); if (raw) localStorage.setItem(STORAGE + '.recovery', raw); } catch { storageWorks = false; }
  }
  if (!state) state = T.createReplay();
  function toast(message) {
    $('toast').textContent = message; $('toast').classList.add('visible');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.remove('visible'), 4500);
  }
  function save() {
    try { localStorage.setItem(STORAGE, JSON.stringify(state)); storageWorks = true; }
    catch { if (storageWorks) toast('浏览器存储不可用或空间已满，请导出 JSON 备份。'); storageWorks = false; }
    $('save-state').innerHTML = storageWorks ? '<i></i>本地自动保存' : '<i></i>未保存 · 请备份';
  }
  function setTone(id, value, text) { const el = $(id); el.textContent = text; el.classList.remove('up', 'down', 'neutral'); el.classList.add(tone(value)); }
  function renderAccount() {
    const t = T.totals(state);
    $('equity').textContent = fmt(t.equity); $('initial').textContent = `¥${fmt(state.initial)}`;
    setTone('pnl', t.pnl, signed(t.pnl)); setTone('return-pct', t.returnPct, `${signed(t.returnPct)}%`);
    $('cash').textContent = fmt(T.availableCash(state)); $('reserved').textContent = `¥${fmt(T.reserved(state))}`;
    $('market-value').textContent = fmt(t.market); $('exposure-label').textContent = `仓位 ${fmt(t.exposure, 1)}%`;
    $('exposure-bar').style.width = `${Math.min(100, t.exposure)}%`;
    $('session-label').textContent = '隔夜底仓 · 昨收成本 · 非实时推送';
    $('market-clock').textContent = T.clock(state.minute); $('market-day').textContent = '11 月 12 日';
    $('market-status').textContent = state.minute >= 240 ? '回放结束' : timer ? '回放中' : '已暂停';
    $('play').textContent = timer ? 'Ⅱ 暂停回放' : '▶ 开始回放';
    for (const id of ['play','step','step-five','finish-day']) $(id).disabled = state.minute >= 240;
    $('progress-label').textContent = `${state.minute} / 240 分钟`;
    $('progress-fill').style.width = `${state.minute/240*100}%`;
  }
  function sparkline(values, color, width=80, height=30) {
    const min=Math.min(...values), max=Math.max(...values), spread=max-min||1;
    const points=values.map((v,i)=>`${i/Math.max(1,values.length-1)*width},${height-3-(v-min)/spread*(height-6)}`).join(' ');
    return `<svg viewBox="0 0 ${width} ${height}" aria-hidden="true"><polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.5"/></svg>`;
  }
  function renderMarket() {
    const m=T.marketContext(state);
    $('index-cards').innerHTML=m.indices.map(i=>`<article class="index-card"><div class="ribbon-label">${i.name}<span>${i.code==='999999'?'000001':i.code}</span></div><div class="index-content"><div><strong class="${tone(i.change)}">${fmt(i.last)}</strong><span class="${tone(i.change)}">${signed(i.change)}%</span></div>${sparkline(i.points,i.change>=0?'#df6973':'#57ac93')}</div></article>`).join('');
    const v=m.temperature;
    $('temperature').textContent=v===null?'—':`${fmt(v,1)}%`;
    $('emotion-state').textContent=v===null?'待开盘':v<30?'偏冷':v<45?'偏弱':v<55?'分歧':v<70?'偏强':'偏热';
    $('temperature').className=v===null?'neutral':tone(v-50);
    $('breadth-counts').innerHTML=v===null?'等待第一分钟':`<span class="up">涨 ${fmt(m.up,0)}</span> / <span class="down">跌 ${fmt(m.down,0)}</span>`;
    $('breadth-up').style.width=`${v??0}%`; $('market-amount').textContent=state.minute?`${units(m.amount)}元`:'—';
    $('breadth-time').textContent=`回放 ${T.clock(state.minute)}`;
  }
  function renderWatchlist() {
    const keyword = $('search').value.trim().toLowerCase();
    let list = T.ASSETS.filter(a => (filter === 'all' || (filter === 'etf' ? a.etf : !a.etf)) && (a.name.includes(keyword) || a.code.includes(keyword)));
    if (sort) list = list.slice().sort((a, b) => sort * (state.quotes[b.code].last / state.quotes[b.code].prev - state.quotes[a.code].last / state.quotes[a.code].prev));
    $('watchlist').innerHTML = list.length ? list.map(a => {
      const q = state.quotes[a.code], change = (q.last / q.prev - 1) * 100;
      const qty=state.positions[a.code]?.qty||0;
      return `<button class="stock-row ${a.code === selected ? 'selected' : ''}" data-code="${a.code}" aria-pressed="${a.code === selected}"><div class="stock-label"><strong>${a.name}</strong><small>${a.code}<span class="held-badge">持 ${fmt(qty,0)}</span></small></div><div class="stock-numbers ${tone(change)}"><b>${quoteFmt(a.code, q.last)}</b><span>${signed(change)}%</span></div></button>`;
    }).join('') : '<div class="empty-state"><strong>没有匹配的证券</strong><p>试试名称或 6 位代码</p></div>';
  }
  function renderQuote() {
    const a = T.asset(selected), q = state.quotes[selected], diff = q.last - q.prev;
    $('stock-name').textContent = a.name;
    $('stock-market').textContent = a.etf ? 'ETF' : a.code.startsWith('300') ? '创业板' : '主板';
    $('stock-code').textContent = `${a.code} · ${a.market} / ${a.sector}`;
    setTone('stock-price', diff, quoteFmt(selected, q.last));
    setTone('stock-change', diff, `${signed(diff / q.prev * 100)}%`);
    setTone('stock-delta', diff, `${signed(diff, a.etf ? 3 : 2)} 元`);
    for (const [id, key] of [['stock-open', 'open'], ['stock-high', 'high'], ['stock-prev', 'prev'], ['stock-low', 'low']]) $(id).textContent = quoteFmt(selected, q[key]);
    $('volume-label').textContent = `${fmt(q.volume / 10000, 1)} 万${a.etf ? '份' : '股'}`;
    renderChart();
    renderMetrics();
    renderCapital();
  }
  function capitalTable(row) {
    const cell = (group, field) => {
      const value = row[group]?.[field];
      if (row.status === 'locked' && field === 'net') return '<span class="flow-missing">收盘后可见</span>';
      if (row.status !== 'ready' || !Number.isFinite(value)) return '<span class="flow-missing">暂无数据</span>';
      const amount = Math.abs(value) >= 1e7 ? `${fmt(Math.abs(value)/1e8)} 亿` : units(Math.abs(value));
      const text = `${field === 'net' ? (value > 0 ? '+' : value < 0 ? '−' : '') : ''}${amount}元`;
      return `<strong class="${field === 'net' ? tone(value) : field === 'inflow' ? 'up' : 'down'}">${text}</strong>`;
    };
    return `<div class="flow-table"><div class="flow-labels"><span>资金类型</span><span>流入</span><span>流出</span><span>净流入 / 出</span></div>${[['main','主力'],['super','特大单']].map(([key,name])=>`<div class="flow-row"><b>${name}</b>${['inflow','outflow','net'].map(field=>`<div>${cell(key,field)}</div>`).join('')}</div>`).join('')}</div>`;
  }
  function renderCapital() {
    const row = window.CapitalFlow.at(window.CAPITAL_FLOW_DATA, DATA.date, selected, state.minute);
    $('capital-time').textContent = `${DATA.date} · 日统计`;
    $('capital-content').innerHTML = capitalTable(row);
    $('capital-note').textContent = row.status === 'locked' ? '仅取得收盘净额，当前回放分钟无可靠资金流数据。主力包含特大单。' : row.status === 'ready' ? '收盘净额 · 正值流入，负值流出。流入 / 流出总额未提供；主力包含特大单。' : '该日期资金数据暂缺，未按零处理。价格、成交量和情绪仍按真实分钟回放。';
    $('capital-reveal').hidden = row.status !== 'locked';
    $('capital-close').innerHTML = $('capital-reveal').open && row.status === 'locked' ? capitalTable(window.CapitalFlow.at(window.CAPITAL_FLOW_DATA, DATA.date, selected, state.minute, true)) : '';
    $('capital-source').href = `https://data.eastmoney.com/zjlx/${selected}.html`;
  }
  $('capital-reveal').addEventListener('toggle', renderCapital);
  function renderMetrics() {
    const q=state.quotes[selected],m=T.metrics(state,selected),context=T.marketContext(state);
    const stats=[['成交量',`${units(q.volume)}${T.asset(selected).etf?'份':'股'}`,'','实际分钟成交量累计'],
      ['成交额',`${units(q.amount)}元`,'','实际分钟成交额累计'],
      ['量比（估）',m.volumeRatio===null?'—':`${fmt(m.volumeRatio)}×`,m.volumeRatio===null?'':m.volumeRatio>=1.5?'up':'','当前均量 / 前5日每分钟平均成交量'],
      ['换手（估）',state.minute?`${fmt(m.turnover)}%`:'—','','当前累计量 / 当日快照推算流通规模'],
      ['分时均价',q.volume?quoteFmt(selected,q.avg):'—','','累计成交额 / 累计成交量'],
      ['日内振幅',`${fmt(m.amplitude)}%`,'','已回放最高价与最低价之差 / 昨收'],
      ['5 分钟量能比',m.recentRatio===null?'—':`${fmt(m.recentRatio)}×`,m.recentRatio>1.5?'up':'','最近5分钟成交量 / 前5分钟成交量'],
      ['5 分钟涨速',m.momentum5===null?'—':`${signed(m.momentum5)}%`,tone(m.momentum5||0),'当前价相对5分钟前价格的变化']];
    $('quote-metrics').innerHTML=stats.map(([label,value,c,title])=>`<div class="quote-metric" title="${title}"><span>${label}</span><strong class="${c}">${value}</strong></div>`).join('');
    const tags=[];
    if(q.volume) tags.push([q.last>=q.avg?'站上均价':'低于均价',q.last>=q.avg?'up':'down'],[`距均价 ${signed(m.vwapBias)}%`,tone(m.vwapBias)]);
    if(m.recentRatio!==null) tags.push([m.recentRatio>=1.5?'近5分钟放量':m.recentRatio<.7?'近5分钟缩量':'近5分钟量能平稳',m.recentRatio>=1.5?'up':'']);
    if(m.momentum5!==null)tags.push([m.momentum5>.15?'短线走强':m.momentum5<-.15?'短线走弱':'短线横盘',tone(m.momentum5)]);
    $('signal-strip').innerHTML=tags.length?tags.map(([text,c])=>`<span class="signal-tag ${c}">${text}</span>`).join(''):'<span class="signal-tag">点击开始回放，观察真实量价变化</span>';
    $('basket-strip').innerHTML=`<span>自选情绪</span><span class="up">${context.rising} 涨</span><span class="down">${context.falling} 跌</span><span>均幅 <b class="${tone(context.average)}">${signed(context.average)}%</b></span><span>站上均价 <b>${state.minute?context.above:'—'} / 11</b></span>`;
  }
  function renderChart() {
    const q = state.quotes[selected], pts = q.points, W = window.innerWidth<=760?360:650, left = 5, right = W-63, top = 14, bottom = 222, volBottom = 284;
    const xmax=$('chart-range').value==='day'?240:Math.max(30,Math.ceil(state.minute/30)*30);
    const spread = Math.max(q.high - q.low, q.prev * .006), low = Math.min(q.low, q.prev) - spread * .15, high = Math.max(q.high, q.prev) + spread * .15;
    const x = t => left + t / xmax * (right - left), y = v => top + (high - v) / (high - low) * (bottom - top);
    let grid = '', labels = '';
    for (let i = 0; i <= 4; i++) {
      const val = high - (high - low) * i / 4, yy = y(val);
      grid += `<line x1="${left}" y1="${yy}" x2="${right}" y2="${yy}" stroke="#edf1f6" stroke-dasharray="3 4"/>`;
      labels += `<text x="${right + 9}" y="${yy + 3}">${quoteFmt(selected, val)}</text>`;
    }
    for (const t of [0,Math.round(xmax/4),Math.round(xmax/2),Math.round(xmax*3/4),xmax]) {
      grid += `<line x1="${x(t)}" y1="${top}" x2="${x(t)}" y2="${volBottom}" stroke="#f1f3f7" stroke-dasharray="3 4"/>`;
      labels += `<text x="${x(t)}" y="309" text-anchor="${t === 0 ? 'start' : t === xmax ? 'end' : 'middle'}">${t === 120 && W>400 ? '11:30 / 13:00' : T.clock(t)}</text>`;
    }
    const line = pts.map((p, i) => `${i ? 'L' : 'M'}${x(p.time).toFixed(2)},${y(p.price).toFixed(2)}`).join(' ');
    const avg = pts.map((p, i) => `${i ? 'L' : 'M'}${x(p.time).toFixed(2)},${y(p.avg).toFixed(2)}`).join(' ');
    let marks = '';
    if (chartType === 'line') {
      marks = `<path d="${line} L${x(state.minute)},${bottom} L${left},${bottom} Z" fill="url(#chart-fill)"/><path d="${line}" stroke="#6385e9" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><circle cx="${x(state.minute)}" cy="${y(q.last)}" r="3" fill="#6385e9" stroke="#fff" stroke-width="1.5"/>`;
    } else {
      for (let n = 1; n < pts.length; n += 5) {
        const segment = pts.slice(n, n + 5), open = segment[0].open, close = segment[segment.length - 1].price;
        const hi = Math.max(...segment.map(p => p.high)), lo = Math.min(...segment.map(p => p.low)), xx = x(Math.min(n + 1.5,state.minute)), color = close >= open ? '#df7379' : '#64af9c';
        marks += `<g><title>${T.clock(n)} 开 ${quoteFmt(selected, open)} 高 ${quoteFmt(selected, hi)} 低 ${quoteFmt(selected, lo)} 收 ${quoteFmt(selected, close)}</title><line x1="${xx}" x2="${xx}" y1="${y(hi)}" y2="${y(lo)}" stroke="${color}"/><rect x="${xx - 3.8}" y="${Math.min(y(open), y(close))}" width="7.6" height="${Math.max(1, Math.abs(y(open) - y(close)))}" fill="${color}"/></g>`;
      }
    }
    const maxVol = Math.max(...pts.map(p => p.volume), 1);
    const bars = pts.slice(1).map((p, i) => `<rect x="${x(p.time)-.5}" y="${volBottom - p.volume / maxVol * 36}" width="${Math.max(.6,(right-left)/xmax*.7)}" height="${p.volume / maxVol * 36}" fill="${p.price >= pts[i].price ? '#edb1b6' : '#abd5c9'}"/>`).join('');
    $('chart-legend-name').textContent = chartType === 'line' ? '成交价' : '5 分钟';
    const trades=state.trades.filter(t=>t.code===selected).map(t=>`<g><title>${T.clock(t.minute)} ${t.side==='buy'?'买入':'卖出'} ${t.qty}</title><circle cx="${x(t.minute)}" cy="${y(t.price)}" r="7" fill="${t.side==='buy'?'#df5059':'#168c71'}"/><text x="${x(t.minute)}" y="${y(t.price)+3}" text-anchor="middle" style="fill:white;font-size:9px">${t.side==='buy'?'B':'S'}</text></g>`).join('');
    $('price-chart').innerHTML = `<svg viewBox="0 0 ${W} 320" role="img" aria-label="${T.asset(selected).name} ${DATA.date}真实${chartType === 'line' ? '分时' : 'K线'}回放图"><defs><linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#7296f2" stop-opacity=".14"/><stop offset="100%" stop-color="#7296f2" stop-opacity=".01"/></linearGradient></defs>${grid}<line x1="${left}" y1="${y(q.prev)}" x2="${right}" y2="${y(q.prev)}" stroke="#b7c1d2" stroke-dasharray="4 4"/>${marks}<path d="${avg}" stroke="#d9b36b" stroke-width="1.2" fill="none"/>${bars}${labels}${trades}<line id="chart-crosshair" x1="0" x2="0" y1="14" y2="284" stroke="#8ca0c3" stroke-dasharray="3 3" style="display:none"/></svg>`;
    $('chart-readout').textContent=`${T.clock(state.minute)}  最新 ${quoteFmt(selected,q.last)}  ${state.minute?'悬停图表查看分钟量价':'等待 09:31 首根分钟线'}`;
  }
  function orderPrice() { return $('order-type').value === 'market' ? T.execution(state, selected, side) ?? state.quotes[selected].last : Number($('order-price').value); }
  function renderEstimate(resetPrice = false) {
    const a = T.asset(selected), market = $('order-type').value === 'market';
    $('order-name').textContent = a.name; $('order-code').textContent = a.code;
    document.querySelectorAll('[data-qty]').forEach(b=>{b.textContent=`${fmt(Number(b.dataset.qty),0)} ${a.etf?'份':'股'}`;});
    const p=state.positions[selected];
    $('order-holding').innerHTML=`<span>持仓<b>${fmt(p?.qty||0,0)}</b></span><span>可卖<b>${fmt(T.availableQty(state,selected),0)}</b></span><span class="holding-wide">可用现金 ¥${fmt(T.availableCash(state))} · 今日锁定 ${fmt((p?.qty||0)-(p?.available||0),0)}</span>`;
    $('order-price').step = T.tick(selected); $('order-price').min = T.tick(selected); $('order-price').disabled = market;
    if (market || resetPrice) $('order-price').value = (market ? T.execution(state, selected, side) ?? state.quotes[selected].last : state.quotes[selected].last).toFixed(a.etf ? 3 : 2);
    $('price-mode').textContent = market ? '市价' : '限价';
    const b = T.bounds(state, selected);
    $('price-help').textContent = market ? '对手价模拟成交' : `${quoteFmt(selected, b.lower)}—${quoteFmt(selected, b.upper)}`;
    $('quantity-unit').textContent = a.etf ? '份' : '股';
    const max = T.maxQty(state, selected, side, orderPrice());
    $('max-quantity').textContent = `${side === 'buy' ? '最多' : '可卖'} ${fmt(max, 0)} ${a.etf ? '份' : '股'}`;
    const qty = Math.max(0, Number($('quantity').value)), amount = Math.max(0, orderPrice() * qty) || 0;
    const charge = amount ? T.fees(state, selected, side, amount) : { total: 0 };
    $('estimate-amount').textContent = `¥${fmt(amount)}`; $('estimate-fee').textContent = `¥${fmt(charge.total)}`;
    $('estimate-fee').title = amount ? `佣金 ${fmt(charge.commission)} / 印花税 ${fmt(charge.stamp)} / 过户费 ${fmt(charge.transfer)}` : '';
    $('estimate-total-label').textContent = side === 'buy' ? '预计支出' : '预计收入';
    $('estimate-total').textContent = `¥${fmt(side === 'buy' ? amount + charge.total : amount - charge.total)}`;
    $('submit-order').textContent = `${side === 'buy' ? '买入' : '卖出'} · 模拟委托`;
    $('submit-order').classList.toggle('sell', side === 'sell');
    $('submit-order').disabled = state.minute >= 240;
  }
  const empty = (heading, detail) => `<div class="empty-state"><span class="empty-icon">▤</span><strong>${heading}</strong><p>${detail}</p></div>`;
  const table = (heads, rows) => `<div class="table-wrap"><table><thead><tr>${heads.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
  function renderActivity() {
    const positions = T.ASSETS.filter(a=>state.positions[a.code]).map(a=>[a.code,state.positions[a.code]]);
    $('position-count').textContent = positions.length;
    $('order-count').textContent = state.orders.filter(o => o.status === 'pending').length;
    $('trade-count').textContent = state.trades.length;
    if (activity === 'positions') {
      $('activity-content').innerHTML = positions.length ? table(['证券', '持仓 / 可卖', '成本价', '最新价', '持仓市值', '浮动盈亏', '快捷操作'], positions.map(([code, p]) => {
        const a = T.asset(code), q = state.quotes[code], pnl = q.last * p.qty - p.cost;
        return `<tr><td><strong>${a.name}</strong><small>${a.code}</small></td><td>${fmt(p.qty, 0)} / ${fmt(T.availableQty(state, code), 0)}<small>今日锁定 ${fmt(p.qty - p.available, 0)}</small></td><td>${quoteFmt(code, p.cost / p.qty)}</td><td class="${tone(q.last - q.prev)}">${quoteFmt(code, q.last)}</td><td>${fmt(q.last * p.qty)}</td><td class="${tone(pnl)}">${signed(pnl)}<small class="${tone(pnl)}">${signed(pnl / p.cost * 100)}%</small></td><td><div class="position-action"><button class="table-button" data-buy="${code}">买入</button><button class="table-button sell-button" data-sell="${code}">卖出</button></div></td></tr>`;
      })) : empty('当前已空仓', '可用资金与历史成交仍在账户中保留。');
    } else if (activity === 'orders') {
      const status = { pending: '待成交', filled: '已成交', cancelled: '已撤单', expired: '已失效' };
      $('activity-content').innerHTML = state.orders.length ? table(['委托时间', '证券', '方向 / 类型', '委托价', '数量', '状态', '操作'], state.orders.slice().reverse().map(o => `<tr><td>第 ${o.day} 日 ${T.clock(o.minute)}<small>#${o.id}</small></td><td><strong>${T.asset(o.code).name}</strong><small>${o.code}</small></td><td class="${o.side === 'buy' ? 'up' : 'down'}">${o.side === 'buy' ? '买入' : '卖出'} / ${o.type === 'market' ? '市价' : '限价'}</td><td>${quoteFmt(o.code, o.limit)}</td><td>${fmt(o.qty, 0)}</td><td><span class="tag ${o.status}">${status[o.status]}</span></td><td>${o.status === 'pending' ? `<button class="table-button" data-cancel="${o.id}">撤单</button>` : '—'}</td></tr>`)) : empty('暂时没有委托', '限价委托在价格达到条件时成交，也可以在这里撤单。');
    } else {
      $('activity-content').innerHTML = state.trades.length ? table(['成交时间', '证券', '方向', '成交价 / 数量', '成交金额', '手续费', '已实现盈亏'], state.trades.map(t => `<tr><td>第 ${t.day} 日 ${T.clock(t.minute)}</td><td><strong>${T.asset(t.code).name}</strong><small>${t.code}</small></td><td class="${t.side === 'buy' ? 'up' : 'down'}">${t.side === 'buy' ? '买入' : '卖出'}</td><td>${quoteFmt(t.code, t.price)} / ${fmt(t.qty, 0)}</td><td>${fmt(t.amount)}</td><td>${fmt(t.fees.total)}</td><td class="${tone(t.realized || 0)}">${t.realized === null ? '—' : signed(t.realized)}</td></tr>`)) : empty('成交记录将在这里出现', '每一次买卖都会记录成交价格、交易费用和你的决策。');
    }
  }
  function renderReview() {
    const t = T.totals(state), sells = state.trades.filter(t => t.side === 'sell'), wins = sells.filter(t => t.realized > 0).length;
    const m=T.marketContext(state);
    const metrics = [['相对持有收益',signed(m.excess),tone(m.excess)],['已实现盈亏', signed(t.realized), tone(t.realized)], ['浮动盈亏', signed(t.unrealized), tone(t.unrealized)], ['卖出胜率', sells.length ? `${fmt(wins / sells.length * 100, 1)}%` : '—', ''], ['最大回撤', `${fmt(state.drawdown)}%`, ''], ['累计手续费', `¥${fmt(t.fees)}`, ''], ['成交笔数', `${state.trades.length} 笔`, '']];
    $('review-metrics').innerHTML = metrics.map(([a, b, c]) => `<div class="review-metric"><span>${a}</span><strong class="${c}">${b}</strong></div>`).join('');
    const points = state.curve, values = points.map(p => p.value), min = Math.min(state.initial, ...values) * .999, max = Math.max(state.initial, ...values) * 1.001;
    const x = i => 8 + i / Math.max(1, points.length - 1) * 610, y = v => 20 + (max - v) / (max - min) * 190;
    let lines = '';
    for (let i = 0; i <= 4; i++) { const v = max - (max - min) * i / 4; lines += `<line x1="8" x2="618" y1="${y(v)}" y2="${y(v)}" stroke="#edf1f7"/><text x="629" y="${y(v) + 4}">${fmt(v / state.initial, 4)}</text>`; }
    const path = points.map((p, i) => `${i ? 'L' : 'M'}${x(i)},${y(p.value)}`).join(' ');
    $('equity-chart').innerHTML = `<svg viewBox="0 0 700 250" role="img" aria-label="账户净值曲线，初始净值为1">${lines}<path d="${path}" stroke="#6889ec" fill="none" stroke-width="2"/><line x1="8" x2="618" y1="${y(state.initial)}" y2="${y(state.initial)}" stroke="#bcc8de" stroke-dasharray="4 4"/><text x="8" y="239">练习开始</text><text x="618" y="239" text-anchor="end">第 ${state.day} 日 ${T.clock(state.minute)}</text></svg>`;
    $('journal-list').innerHTML = state.trades.length ? state.trades.map(t => `<div class="journal-item"><div class="journal-time">第 ${t.day} 日 · ${T.clock(t.minute)}<small>成交 #${t.id}</small></div><div><strong><span class="${t.side === 'buy' ? 'up' : 'down'}">${t.side === 'buy' ? '买入' : '卖出'}</span> ${T.asset(t.code).name}</strong><small>${fmt(t.qty, 0)} ${T.asset(t.code).etf ? '份' : '股'} × ${quoteFmt(t.code, t.price)}</small></div><p>${t.note ? esc(t.note) : '这笔交易没有记录计划。下次交易前，试着写下你的理由。'}</p></div>`).join('') : empty('让每一笔交易都有理由', '下单时填写的交易计划会自动归档到这里。');
  }
  function render() {
    renderAccount(); renderMarket();
    if (view === 'trade') { renderWatchlist(); renderQuote(); renderEstimate(); renderActivity(); }
    else renderReview();
  }
  function stop() { clearInterval(timer); timer = null; renderAccount(); }
  function advance(steps) {
    const before = state.trades.length;
    T.advance(state, steps);
    if (state.minute >= 240) { stop(); toast('今日回放结束，挂单已失效。点击左侧复盘查看相对持有收益。'); }
    else if (state.trades.length > before) toast(`${state.trades.length - before} 笔限价委托已成交`);
    save(); render();
  }
  function setSide(next) { side = next; document.querySelectorAll('[data-side]').forEach(b => b.classList.toggle('selected', b.dataset.side === side)); $('order-error').textContent = ''; renderEstimate(); }
  function selectStock(code) { selected = code; $('capital-reveal').open = false; $('quantity').value=T.asset(code).etf?10000:100; $('order-error').textContent = ''; renderWatchlist(); renderQuote(); renderEstimate(true); }
  function download(name, contents, type) {
    const url = URL.createObjectURL(new Blob([contents], { type })), link = document.createElement('a');
    link.href = url; link.download = name; document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function openDialog(id) { stop(); $(id).showModal(); }
  $('watchlist').addEventListener('click', e => { const b = e.target.closest('[data-code]'); if (b) selectStock(b.dataset.code); });
  $('search').addEventListener('input', renderWatchlist);
  $('filters').addEventListener('click', e => { const b = e.target.closest('[data-filter]'); if (!b) return; filter = b.dataset.filter; document.querySelectorAll('[data-filter]').forEach(b => b.classList.toggle('selected', b.dataset.filter === filter)); renderWatchlist(); });
  $('sort').addEventListener('click', () => { sort = sort === 0 ? 1 : sort === 1 ? -1 : 0; $('sort').textContent = `最新价 / 涨跌幅 ${sort === 1 ? '↓' : sort === -1 ? '↑' : '⇅'}`; renderWatchlist(); });
  $('chart-tabs').addEventListener('click', e => { const b = e.target.closest('[data-chart]'); if (!b) return; chartType = b.dataset.chart; document.querySelectorAll('[data-chart]').forEach(b => b.classList.toggle('selected', b.dataset.chart === chartType)); renderChart(); });
  $('chart-range').addEventListener('change',renderChart);
  $('side-switch').addEventListener('click', e => { const b = e.target.closest('[data-side]'); if (b) setSide(b.dataset.side); });
  $('order-type').addEventListener('change', () => renderEstimate(true));
  $('order-price').addEventListener('input', () => renderEstimate()); $('quantity').addEventListener('input', () => renderEstimate());
  $('qty-minus').addEventListener('click', () => { $('quantity').value = Math.max(100, (Number($('quantity').value) || 0) - 100); renderEstimate(); });
  $('qty-plus').addEventListener('click', () => { $('quantity').value = Math.max(100, (Number($('quantity').value) || 0) + 100); renderEstimate(); });
  document.querySelectorAll('[data-fraction]').forEach(b => b.addEventListener('click', () => { const max = T.maxQty(state, selected, side, orderPrice()); $('quantity').value = Math.floor(max * Number(b.dataset.fraction) / 100) * 100; renderEstimate(); }));
  $('order-form').addEventListener('submit', e => {
    e.preventDefault(); $('order-error').textContent = '';
    try {
      const o = T.place(state, { code: selected, side, type: $('order-type').value, qty: Number($('quantity').value), limit: Number($('order-price').value), note: $('trade-note').value.trim() });
      $('trade-note').value = ''; save(); render();
      toast(o.status === 'filled' ? `${side === 'buy' ? '买入' : '卖出'}已成交：${T.asset(selected).name} ${o.qty} ${T.asset(selected).etf ? '份' : '股'}，成交价 ${quoteFmt(selected, o.filledPrice)}` : '限价委托已提交，资金或可卖数量已冻结');
    } catch (error) { $('order-error').textContent = error.message; }
  });
  $('activity-tabs').addEventListener('click', e => { const b = e.target.closest('[data-tab]'); if (!b) return; activity = b.dataset.tab; document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('selected', b.dataset.tab === activity)); renderActivity(); });
  $('activity-content').addEventListener('click', e => {
    const cancel = e.target.closest('[data-cancel]'), sell = e.target.closest('[data-sell]'), buy=e.target.closest('[data-buy]');
    if (cancel) { try { T.cancel(state, Number(cancel.dataset.cancel)); save(); render(); toast('委托已撤销，冻结资金或份额已释放'); } catch (error) { toast(error.message); } }
    if (sell) { selectStock(sell.dataset.sell); setSide('sell'); $('quantity').value = T.availableQty(state, selected); renderEstimate(); $('order-form').scrollIntoView({ behavior: 'smooth', block: 'center' }); }
    if (buy) { selectStock(buy.dataset.buy); setSide('buy'); renderEstimate(); $('order-form').scrollIntoView({behavior:'smooth',block:'center'}); }
  });
  document.querySelectorAll('[data-qty]').forEach(b=>b.addEventListener('click',()=>{ $('quantity').value=b.dataset.qty;renderEstimate(); }));
  $('price-chart').addEventListener('pointermove',e=>{
    const svg=$('price-chart').querySelector('svg'); if(!svg) return;
    const rect=svg.getBoundingClientRect(), width=svg.viewBox.baseVal.width, span=width-68, scale=Math.min(rect.width/width,rect.height/320), offset=(rect.width-width*scale)/2;
    const rawX=(e.clientX-rect.left-offset)/scale;
    const xmax=$('chart-range').value==='day'?240:Math.max(30,Math.ceil(state.minute/30)*30);
    const minute=Math.max(0,Math.min(state.minute,Math.round((rawX-5)/span*xmax))), p=state.quotes[selected].points[minute];
    const cross=$('chart-crosshair');cross.style.display='';cross.setAttribute('x1',5+minute/xmax*span);cross.setAttribute('x2',5+minute/xmax*span);
    $('chart-readout').innerHTML=`<b>${T.clock(minute)}</b><span>价 ${quoteFmt(selected,p.price)}</span><span>均 ${quoteFmt(selected,p.avg)}</span><span>量 ${units(p.volume)}${T.asset(selected).etf?'份':'股'}</span><span>额 ${units(p.amount)}元</span>`;
  });
  $('price-chart').addEventListener('pointerleave',()=>{const cross=$('chart-crosshair');if(cross)cross.style.display='none';});
  $('play').addEventListener('click', () => { if (timer) stop(); else { timer = setInterval(() => advance(Number($('speed').value)), 1000); renderAccount(); } });
  $('step').addEventListener('click', () => { stop(); advance(1); });
  $('step-five').addEventListener('click', () => { stop(); advance(5); });
  $('finish-day').addEventListener('click', () => openDialog('next-dialog'));
  $('confirm-next').addEventListener('click', () => { $('next-dialog').close(); advance(240-state.minute); });
  $('rules-button').addEventListener('click', () => openDialog('rules-dialog'));
  $('emotion-info').addEventListener('click',()=>openDialog('rules-dialog'));
  const settings = () => { $('confirm-reset').checked = false; openDialog('settings-dialog'); };
  $('settings-button').addEventListener('click', settings); $('new-session').addEventListener('click', settings);
  document.querySelectorAll('.close-dialog').forEach(b => b.addEventListener('click', () => b.closest('dialog').close()));
  document.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => {
    view = b.dataset.view;
    document.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === view));
    $('trade-view').hidden = view !== 'trade'; $('review-view').hidden = view !== 'review';
    $('page-title').textContent = view === 'trade' ? '真实行情，你来交易。' : '你的操作，跑赢持有了吗？'; render();
  }));
  $('reset-form').addEventListener('submit', e => { e.preventDefault(); if (!$('confirm-reset').checked) return; try { state = T.createReplay(); $('settings-dialog').close(); $('trade-note').value = ''; $('order-error').textContent = ''; $('quantity').value = T.asset(selected).etf?10000:100; save(); render();renderEstimate(true); toast('已恢复初始底仓和 20,000 元现金，回到 09:30'); } catch (error) { toast(error.message); } });
  $('backup').addEventListener('click', () => { download(`练盘-${DATA.date}-备份.json`, JSON.stringify(state, null, 2), 'application/json'); toast('练习备份已导出'); });
  $('import-backup').addEventListener('change', async e => {
    const file = e.target.files[0]; if (!file) return;
    try {
      if (!$('confirm-reset').checked) throw Error('恢复备份会替换当前账户，请先勾选确认结束当前练习');
      if (file.size > 20 * 1024 * 1024) throw Error('备份文件过大（最大 20 MB）');
      const parsed = JSON.parse(await file.text()); if (!validState(parsed)) throw Error('不是有效的练盘备份文件');
      state = parsed; $('settings-dialog').close(); save(); render(); toast('练习备份已恢复');
    } catch (error) { toast(`导入失败：${error.message}`); } finally { e.target.value = ''; }
  });
  $('export-csv').addEventListener('click', () => {
    if (!state.trades.length) { toast('还没有成交记录，完成第一笔交易后再导出'); return; }
    const cell = v => { let str = String(v ?? ''); if (/^[=+@\-\t\r]/.test(str)) str = "'" + str; return `"${str.replace(/"/g, '""')}"`; };
    const rows = [['成交编号', '行情日期', '回放时间', '证券代码', '证券名称', '方向', '数量', '价格', '金额', '佣金', '印花税', '过户费', '已实现盈亏', '交易计划'], ...state.trades.slice().reverse().map(t => [t.id, DATA.date, T.clock(t.minute), t.code, T.asset(t.code).name, t.side === 'buy' ? '买入' : '卖出', t.qty, t.price, t.amount, t.fees.commission, t.fees.stamp, t.fees.transfer, t.realized, t.note])];
    download(`练盘-${DATA.date}-成交记录.csv`, '\uFEFF' + rows.map(r => r.map(cell).join(',')).join('\r\n'), 'text/csv;charset=utf-8'); toast('成交记录已导出');
  });
  document.addEventListener('keydown', e => { if (e.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName) && !document.querySelector('dialog[open]') && view === 'trade') { e.preventDefault(); $('search').focus(); } });
  document.addEventListener('visibilitychange', () => { if (document.hidden) { stop(); save(); } });
  window.addEventListener('storage', e => {
    if (e.key !== STORAGE || !e.newValue) return;
    try { const next = JSON.parse(e.newValue); if (validState(next)) { stop(); state = next; render(); toast('检测到另一标签页更新账户，已同步并暂停行情'); } } catch { /* Invalid external data does not replace this session. */ }
  });
  window.addEventListener('beforeunload', save);
  let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(view==='trade')renderChart();},150);});
  save(); render(); if (startupMessage) toast(startupMessage);
})();
