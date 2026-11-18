(function(root) {
  'use strict';
  function at(data, date, code, minute, reveal = false) {
    if (!data || data.date !== date) return { status: 'unavailable', reason: '资金日期与回放不一致' };
    const row = data.assets?.[code];
    if (!row || row.status !== 'daily-net-only' || row.date !== date || !Number.isFinite(row.main?.net) || !Number.isFinite(row.super?.net)) return { status: 'unavailable', reason: '未取得该日可靠资金数据' };
    if (minute < 240 && !reveal) return { status: 'locked', asOf: '15:00' };
    return { ...row, status: 'ready' };
  }
  const api = { at };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.CapitalFlow = api;
})(typeof window !== 'undefined' ? window : globalThis);
