/* Earnings Spread Desk: the page. State arrives over a WebSocket; the Is / Was model runs on the server
   (POST /api/analyze) so the page and the screens always use the same math. Nothing here places orders. */
'use strict';

/* ---------- formatting ---------- */
const tick = x => Math.round(x * 20) / 20;
const f2 = x => x == null ? '—' : (x < 0 ? '−' : '') + Math.abs(x).toFixed(2);
const usd = x => x == null ? '—' : (x < 0 ? '−$' : '$') + Math.abs(x).toFixed(2);
const usd0 = x => x == null ? '—' : (x < 0 ? '−$' : '$') + Math.abs(Math.round(x)).toLocaleString();
const pct = (x, d = 2) => x == null ? '—' : x.toFixed(d) + '%';
const ivp = x => x == null ? '—' : (x * 100).toFixed(1);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const D = s => new Date((s.length === 8 ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}` : s.slice(0, 10)) + 'T12:00:00Z');
const iso = d => d.toISOString().slice(0, 10);
const days = (a, b) => Math.round((D(b) - D(a)) / 864e5);
const fmtD = s => s ? D(s).toLocaleDateString('en-US', {weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC'}) : '—';
const fmtE = s => s ? D(s).toLocaleDateString('en-US', {month: 'short', day: 'numeric', timeZone: 'UTC'}) : '—';
const median = a => {const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length ? (s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2) : null};
const rp = (x, d = 0) => x == null ? '—' : (x * 100).toFixed(d) + '%';   // ask/bid ratio; null when a leg has no quote
const cap = x => x == null ? '' : x >= 1e9 ? '$' + (x / 1e9).toFixed(1) + 'B' : '$' + (x / 1e6).toFixed(0) + 'M';

/* ---------- state ---------- */
let ST = {cands: [], messages: [], trades: [], journal: [], connection: {}, settings: null, earnings: {}};
const S = {sel: null, crush: null, crushMode: null, backChg: null, basis: null, P: {}, budget: null, exp: 'new',
           exitMode: 'active', target: 50, timeStop: '13:30', checks: {}, week: null, ladder: null, exitSel: null,
           exitQuotes: {}, settingsErr: '', ovMsg: ''};
const AN = {};          // analyses for the current controls, by symbol
try {const s = JSON.parse(localStorage.getItem('esd-ui') || '{}'); if (s.sel) S.sel = s.sel; if (s.exp) S.exp = s.exp} catch (e) {}
const save = () => {try {localStorage.setItem('esd-ui', JSON.stringify({sel: S.sel, exp: S.exp}))} catch (e) {}};

const byTk = k => ST.cands.find(t => t.symbol === k);
const sel = () => byTk(S.sel) || ST.cands.find(t => t.state === 'ready') || ST.cands[0];
const strat = () => (ST.settings || {}).strategy || {};
const today = () => (ST.connection.clock_date || iso(new Date()));
const limit = () => S.exp === 'new' ? 1 : 5;
const lb = () => strat().lookback || 7;

function mainRight(t) {
  const p = S.P[t.symbol] || {}, K = p.K ?? t.K;
  return p.right ?? (K >= t.S ? 'C' : 'P');
}
function params(t, right) {
  const p = S.P[t.symbol] || {};
  return {symbol: t.symbol, K: p.K ?? null, right, front: p.front ?? null, back: p.back ?? null, crush_mode: S.crushMode,
          crush_percent: S.crush, back_iv_change_percent: S.backChg, gap_basis: S.basis};
}
function isDefault(t) {
  const p = S.P[t.symbol] || {}, s = strat();
  return p.K == null && p.front == null && p.back == null && (p.right == null || p.right === (t.a || {}).right) &&
         S.crushMode === s.crush_mode && S.crush === s.crush_percent && S.backChg === s.back_iv_change_percent && S.basis === s.gap_basis;
}
/* the analysis for the page's controls: the server's default one, or one asked for with these controls.
   side 'alt' is the same strike on the other side (put calendar for a call one, and the reverse) */
function A(t, side) {
  if (!t) return {};
  const r = side === 'alt' ? (mainRight(t) === 'C' ? 'P' : 'C') : mainRight(t);
  if (isDefault(t)) {
    const d = t.a || {}, alt = t.a_alt || {};
    if (!d.right || d.right === r) return d;
    if (alt.right === r) return alt;
  }
  const slot = t.symbol + '|' + r;
  const key = JSON.stringify(params(t, r)) + '|' + (t.screened || '');
  const hit = AN[slot];
  if (hit && hit.key === key) return hit.a;
  if (!hit || hit.pending !== key) {
    AN[slot] = {...(hit || {}), pending: key};
    fetch('/api/analyze', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(params(t, r))})
      .then(x => x.json()).then(a => {AN[slot] = {key, a}; render()}).catch(() => {});
  }
  return hit && hit.a ? hit.a : (side === 'alt' ? (t.a_alt || {}) : (t.a || {}));
}
function q(t, e, r, K) {return (t.chain || []).find(x => x.expiry === e && x.right === r && Math.abs(x.strike - K) < 1e-6)}
const stage = t => t.stage || {k: 'data', lbl: '—', cls: 'p-neutral'};
function ranked() {
  return ST.cands.filter(t => stage(t).k === 'ok')
    .sort((x, y) => (y.a.hits - x.a.hits) || ((y.a.meg - y.a.med) - (x.a.meg - x.a.med)));
}

/* ---------- routing ---------- */
const VIEWS = [
  ['grp', 'Today'], ['overview', '·', 'Pipeline'],
  ['grp', 'Setup steps'], ['calendar', '1', 'Earnings calendar'], ['screener', '2–3', 'Bid-ask & Yes/No'],
  ['analyze', '4', 'Is / Was analysis'], ['risk', '5', 'Risk checklist'], ['entry', '6', 'Entry ticket'],
  ['exit', '7', 'Exit next day'],
  ['grp', 'More'], ['playbooks', '+', 'Straddle · other plays'], ['results', '≡', 'Forward test'], ['settings', '⚙', 'Settings']];
function cur() {const h = location.hash.slice(1); return VIEWS.some(v => v[0] === h) ? h : 'overview'}
function nav() {
  document.getElementById('nav').innerHTML = VIEWS.map(v => v[0] === 'grp' ? `<div class="grp">${v[1]}</div>` :
    `<a href="#${v[0]}" class="${cur() === v[0] ? 'on' : ''}"><span class="n">${v[1]}</span><span>${v[2]}</span></a>`).join('');
}
function go(v, tk) {if (tk) {S.sel = tk; save()} if (location.hash === '#' + v) render(); else location.hash = v}
window.addEventListener('hashchange', () => {S.settingsErr = ''; S.settingsDirty = false; S.ovMsg = ''; render()});

/* ---------- shared bits ---------- */
const feed = s => `<div class="feed">${s}</div>`;
function banner() {
  const c = ST.connection;
  if (c.demo) return `<div class="banner"><b>Demo</b><span>Replay of the course sample week (Mar 26 2018) with a simulated TWS. RHT and LULU quotes and RHT's past gaps are from the slides; the rest are illustrative. Nothing here places orders.</span></div>`;
  if (c.state !== 'Connected') return `<div class="banner"><b>TWS not connected</b><span>${esc(c.error || 'Connecting…')} The earnings calendar still loads; option screens start once TWS answers.</span></div>`;
  return '';
}
const tkSelect = (list = ST.cands.filter(t => t.state === 'ready')) => list.length ?
  `<select id="tksel" aria-label="Ticker">${list.map(t => `<option ${t.symbol === (sel() || {}).symbol ? 'selected' : ''}>${t.symbol}</option>`).join('')}</select>` : '';
const noData = t => `<div class="panel empty">${t ? `${esc(t.symbol)}: ${t.state === 'loading' ? 'loading option quotes from TWS…' : t.error ? esc(t.error) : 'not screened yet (screens run for entries today and the next session).'}` : 'No candidates yet.'}
  ${t ? `<div style="margin-top:10px"><button class="sm" data-screen="${t.symbol}">Screen ${t.symbol} now</button></div>` : ''}</div>`;
const pill = (txt, cls) => `<span class="pill ${cls}">${txt}</span>`;
const baCls = x => x == null ? 'p-neutral' : x <= strat().ba_pass_percent / 100 + 1e-9 ? 'p-good' : x <= strat().ba_review_percent / 100 + 1e-9 ? 'p-warn' : 'p-bad';
const statusPill = t => ({verified: pill('Verified', 'p-good'), manual: pill('Confirmed by you', 'p-good'),
  single: pill('1 source', 'p-warn'), conflict: pill('Conflict', 'p-bad')}[t.status] || pill(t.status, 'p-neutral'));

/* ---------- views ---------- */
function vOverview() {
  const all = ST.cands, v = all.filter(t => t.verified), sc = v.filter(t => t.state === 'ready' && t.a && t.a.baRatio);
  const ba = sc.filter(t => t.a.baOverall !== 'fail'), yn = ba.filter(t => t.a.yes), rk = ranked();
  const f = [[all.length, 'Announcements loaded'], [v.length, 'Dates usable'], [sc.length, 'Screened in TWS'],
             [ba.length, 'Pass bid-ask'], [yn.length, 'Yes on term structure'], [rk.length, 'Pass Is / Was']];
  const max = Math.max(1, all.length);
  const sched = [];
  all.forEach(t => {if (!t.verified) return; const st = stage(t);
    sched.push({d: t.entry, time: '15:00–16:00', what: `Enter ${t.symbol} calendar`, t, st, kind: 'in'});
    if (st.k === 'ok') sched.push({d: t.model, time: '09:30–16:00', what: `Exit ${t.symbol} (close by end of day)`, t, st, kind: 'out'})});
  sched.sort((a, b) => a.d.localeCompare(b.d) || (a.kind === 'out' ? -1 : 1));
  const td = today(), c = ST.connection, srcs = (ST.earnings || {}).sources || {};
  return `${banner()}<div class="head"><div><h1>Pipeline</h1><p>Each announcement runs through the setup steps. Candidates that pass are ranked by Is / Was, and you trade only the top ones.</p></div>
  <div class="seg" role="group" aria-label="Experience"><button class="${S.exp === 'new' ? 'on' : ''}" data-exp="new">Newer · 1/day</button><button class="${S.exp === 'pro' ? 'on' : ''}" data-exp="pro">Experienced · 5/day</button></div></div>
  <div class="panel"><div class="funnel" style="grid-template-columns:repeat(6,minmax(0,1fr))">${f.map(x => `<div class="fstep"><div class="c">${x[0]}</div><div class="t">${x[1]}</div><div class="bar" style="width:${x[0] / max * 100}%"></div></div>`).join('')}</div></div>
  <div class="grid g2" style="margin-top:14px">
   <div class="panel"><div class="ph"><h2>Ranked candidates</h2><span class="muted" style="font-size:12px">Past gaps inside the endurable gap, then margin over the median</span></div>
    ${rk.length ? `<div class="tbl-wrap"><table><thead><tr><th>#</th><th>Ticker</th><th>Report</th><th class="r">Endurable</th><th class="r">Median past</th><th class="r">Inside</th><th>Verdict</th><th></th></tr></thead><tbody>
    ${rk.map((t, i) => `<tr class="${i >= limit() ? 'dim' : ''}"><td class="mono">${i + 1}</td><td class="tk">${t.symbol}</td><td>${fmtD(t.date)} ${pill(t.timing, 'p-neutral')}</td><td class="r num">${pct(t.a.meg)}</td><td class="r num">${pct(t.a.med)}</td><td class="r num">${t.a.hits}/${t.a.n}</td><td>${pill(stage(t).lbl, stage(t).cls)}</td><td><button class="sm" data-go="analyze" data-tk="${t.symbol}">Open</button></td></tr>`).join('')}
    </tbody></table></div>` : `<div class="empty">No candidate passes every screen yet.</div>`}
    <p class="muted" style="font-size:12px;margin:8px 0 0">${S.exp === 'new' ? 'Newer traders: up to 1 trade per day; rows after the first are greyed.' : 'Experienced traders: up to 5 per day.'}</p></div>
   <div class="panel"><div class="ph"><h2>Schedule</h2><span class="muted" style="font-size:12px">ET · enter in the last hour before the report, exit next session</span></div>
    <div class="tbl-wrap"><table><tbody>${sched.map(s => `<tr class="${s.d < td ? 'dim' : ''}"><td>${fmtD(s.d)}${s.d === td ? ' ' + pill('Today', 'p-acc') : ''}</td><td class="mono">${s.time}</td><td>${esc(s.what)}</td><td>${s.kind === 'out' ? pill('Exit', 'p-acc') : pill(s.st.lbl, s.st.cls)}</td></tr>`).join('') || '<tr><td class="empty">Nothing scheduled.</td></tr>'}</tbody></table></div></div>
  </div>
  <div class="grid g2" style="margin-top:14px">
   <div class="panel"><div class="ph"><h2>Connections</h2><button class="sm" data-screen="">Screen all now</button></div>
    <dl class="kv"><dt>TWS</dt><dd><span class="conn"><span class="dot ${c.state === 'Connected' ? 'on' : c.state === 'Connecting' ? 'mid' : ''}"></span>${esc(c.state || '—')} ${c.client_id ? '· client ' + c.client_id : ''}</span></dd>
    <dt>Address</dt><dd>${esc(c.host)}:${c.port} · ${esc(c.market_data_type)}</dd>
    <dt>Data lines</dt><dd>${c.lines_used ?? 0} / ${c.lines_max ?? '—'} (peak ${c.lines_peak ?? 0})</dd>
    <dt>Requests</dt><dd>${c.requests_per_second ?? 0}/s</dd>
    ${Object.entries(srcs).map(([k, s]) => `<dt>${esc(k)}</dt><dd style="color:var(${s.ok ? '--good' : '--warn'});white-space:normal">${esc(s.text)}</dd>`).join('')}
    <dt>Calendar updated</dt><dd>${esc((ST.earnings || {}).updated || '—')}</dd></dl></div>
   <div class="panel"><h2 style="margin-bottom:8px">Messages</h2><div class="msgs">${[...ST.messages].reverse().map(m => `<div class="${m.level}">${m.time} ${esc(m.text)}${m.count > 1 ? ` (×${m.count})` : ''}</div>`).join('') || '<div>None</div>'}</div></div>
  </div>`;
}

function vCalendar() {
  const t = byTk(S.sel) || ST.cands[0];
  const dates = [...new Set(ST.cands.map(x => x.date))].sort();
  const base = S.week || (dates.find(d => d >= today()) || today());
  const mon = D(base); mon.setUTCDate(mon.getUTCDate() - ((mon.getUTCDay() + 6) % 7));
  const wk = [0, 1, 2, 3, 4].map(i => {const d = new Date(mon); d.setUTCDate(d.getUTCDate() + i); return iso(d)});
  const card = x => `<div class="ecard ${x.status === 'conflict' ? 'conflict' : ''} ${t && x.symbol === t.symbol ? 'on' : ''}" data-pick="${x.symbol}" tabindex="0" role="button"><div class="r1"><span class="tk">${x.symbol}</span>${statusPill(x)}</div><div class="r2">${esc(x.name)} ${cap(x.market_cap)}</div><div class="r2 mono">Enter ${fmtD(x.entry)}${x.a && x.a.fIV ? ` · IV ${ivp(x.a.fIV)}/${ivp(x.a.bIV)}` : ''}</div></div>`;
  const shift = n => {const d = new Date(mon); d.setUTCDate(d.getUTCDate() + 7 * n); return iso(d)};
  let detail = '<div class="panel empty">Pick an announcement.</div>';
  if (t) {
    const a = t.a || {};
    const prem = a.ivDiff;
    detail = `<div class="panel"><div class="ph"><h2>${t.symbol} · ${esc(t.name)}</h2>${statusPill(t)}</div>
    <dl class="kv"><dt>Announcement</dt><dd>${fmtD(t.date)} · ${t.timing === 'AMC' ? 'After market close' : t.timing === 'BMO' ? 'Before market open' : 'Time not supplied (treated as after close)'}</dd>
    <dt>Entry window</dt><dd>${fmtD(t.entry)} · 15:00–16:00 ET</dd><dt>Exit session</dt><dd>${fmtD(t.model)} · close by 16:00</dd>
    <dt>Front expiry</dt><dd>${t.front ? fmtE(t.front) + ' (first after the report)' : '—'}</dd>
    <dt>Earnings premium in IV</dt><dd style="color:var(${prem == null ? '--muted' : prem >= strat().iv_edge_points ? '--good' : '--bad'})">${prem == null ? 'after the option screen' : f2(prem) + ' pts front over back'}</dd></dl>
    <div class="src" style="margin-top:10px">${(t.sources || []).map(s => `<span class="${s.date === t.date ? '' : 'x'}">${esc(s.source)} ${fmtE(s.date)} ${esc(s.timing)}</span>`).join('')}</div>
    ${t.note ? `<div class="note" style="margin-top:10px">${t.status === 'conflict' ? '<b>Red flag.</b> ' : ''}${esc(t.note)}</div>` : `<div class="note" style="margin-top:10px">Both sources agree.${prem != null ? ` The front expiry's IV sits ${prem.toFixed(1)} points above the long leg's.` : ''}</div>`}
    <h3 style="margin:14px 0 6px">Confirm or correct the date</h3>
    <form class="row" id="ovf"><input type="date" id="ov-date" value="${t.date}" aria-label="Report date"><select id="ov-time" aria-label="Before or after market"><option ${t.timing === 'BMO' ? 'selected' : ''}>BMO</option><option ${t.timing !== 'BMO' ? 'selected' : ''}>AMC</option></select>
     <input type="text" id="ov-note" placeholder="Where you checked (IR page…)" style="flex:1;min-width:140px"><button class="pri" type="submit">Save</button>${t.status === 'manual' ? '<button type="button" id="ov-clear">Remove</button>' : ''}</form>
    ${S.ovMsg ? `<div class="err" style="margin-top:6px">${esc(S.ovMsg)}</div>` : ''}
    ${feed('Sources: Nasdaq calendar · Finnhub calendar (free key) · your confirmations, kept in overrides.json in the data folder')}</div>`;
  }
  return `${banner()}<div class="head"><div><h1>1 · Earnings calendar</h1><p>Announcements from Nasdaq and Finnhub, cross-checked. Trust but verify: when the sources disagree, or only one lists a report, check the company's investor-relations page and confirm the date here. Note BMO or AMC.</p></div>
  <div style="display:flex;gap:8px"><button data-week="${shift(-1)}">← Week</button><button data-week="${shift(1)}">Week →</button><button class="pri" id="refresh-src">Refresh sources</button></div></div>
  <div class="week">${wk.map(d => {const items = ST.cands.filter(x => x.date === d);
    return `<div class="day"><div class="dh"><b>${fmtD(d)}</b><span class="muted mono" style="font-size:11px">${items.length || ''}</span></div>
    <div class="slot">Before open</div>${items.filter(x => x.timing === 'BMO').map(card).join('') || '<div class="muted" style="font-size:12px">—</div>'}<div class="slot">After close</div>${items.filter(x => x.timing !== 'BMO').map(card).join('') || '<div class="muted" style="font-size:12px">—</div>'}</div>`}).join('')}</div>
  <div class="grid g2" style="margin-top:14px">${detail}
   <div class="panel"><div class="ph"><h2>Past report dates${t ? ' · ' + t.symbol : ''}</h2></div>
    ${t && t.gaps && t.gaps.length ? `<div class="tbl-wrap"><table><thead><tr><th>Report</th><th>Time</th><th>Source</th><th class="r">Gap</th></tr></thead><tbody>${t.gaps.map(g => `<tr><td>${fmtD(g.date)}</td><td>${esc(g.timing)}</td><td>${g.source === 'inferred' ? pill('From price gap', 'p-warn') : esc(g.source)}</td><td class="r num">${pct(g.gap)}</td></tr>`).join('')}</tbody></table></div>` : '<p class="muted">Loaded with the option screen.</p>'}
    ${t && t.gap_note ? `<div class="note" style="margin-top:8px">${esc(t.gap_note)}</div>` : ''}
    ${t ? `<h3 style="margin:14px 0 6px">Set past dates yourself</h3><p class="muted" style="margin:0 0 6px;font-size:12px">One per line, e.g. <span class="mono">2025-10-28 AMC</span>. They replace what the sources say for ${t.symbol}.</p>
    <textarea id="past-rows" aria-label="Past report dates">${(t.gaps || []).filter(g => g.source === 'Manual').map(g => g.date + ' ' + g.timing).join('\n')}</textarea>
    <div style="margin-top:6px"><button id="past-save">Save past dates</button></div>` : ''}</div>
  </div>`;
}

function otherSide(t) {
  const b = t.a_alt || {};
  if (!b.right) return '';
  const name = b.right === 'C' ? 'Call' : 'Put';
  if (b.baOverall === 'fail') return pill(name + ': fails bid-ask', 'p-neutral');
  if (!b.pts) return '';
  return pill(`${name}: ${b.verdict} ${b.hits}/${b.n}`, b.verdict === 'Strong' ? 'p-good' : b.verdict === 'Acceptable' ? 'p-warn' : 'p-neutral');
}
function vScreener() {
  const list = ST.cands.filter(t => t.verified);
  const rows = list.map(t => {const a = t.a || {}, st = stage(t), ok = a.baRatio && a.fq;
    return `<tr class="${t.symbol === (sel() || {}).symbol ? 'sel' : ''}"><td class="tk">${t.symbol}</td><td>${fmtE(t.date)} ${pill(t.timing, 'p-neutral')}</td><td class="r num">${t.S ? t.S.toFixed(2) : '—'}</td><td class="num">${a.K ?? '—'} ${a.right || ''}</td>
    <td>${t.front ? fmtE(t.front) + ' / ' + fmtE(a.back) : '—'}</td>
    <td class="r num">${ok ? f2(a.fq[0]) + ' / ' + f2(a.fq[1]) : '—'}</td><td>${ok ? pill(rp(a.baRatio[0], 0), baCls(a.baRatio[0])) : ''}</td>
    <td class="r num">${ok ? f2(a.bq[0]) + ' / ' + f2(a.bq[1]) : '—'}</td><td>${ok ? pill(rp(a.baRatio[1], 0), baCls(a.baRatio[1])) : ''}</td>
    <td class="r num">${ivp(a.fIV)}</td><td class="r num">${ivp(a.bIV)}</td><td class="r num" style="color:var(${a.yes ? '--good' : '--bad'})">${a.ivDiff == null ? '—' : (a.ivDiff >= 0 ? '+' : '') + a.ivDiff.toFixed(1)}</td>
    <td>${a.ivDiff == null ? '' : a.yes ? pill('YES', 'p-good') : pill('NO', 'p-bad')}</td><td>${pill(st.lbl, st.cls)}</td><td>${otherSide(t)}</td><td class="mono muted" style="font-size:11px">${esc(t.screened || '')}</td>
    <td>${t.state === 'ready' ? `<button class="sm" data-go="analyze" data-tk="${t.symbol}">Is / Was →</button>` : `<button class="sm" data-screen="${t.symbol}">Screen</button>`}</td></tr>`}).join('');
  const t = sel(), a = A(t);
  return `${banner()}<div class="head"><div><h1>2–3 · Bid-ask and Yes / No screens</h1><p>Both legs must quote with the ask no more than ${strat().ba_pass_percent}% of the bid. Then the front (earnings) expiry must carry IV at least ${strat().iv_edge_points} points above the long expiry. A YES moves on to Is / Was.</p></div>
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">${pill('≤' + strat().ba_pass_percent + '% pass', 'p-good')}${pill(strat().ba_pass_percent + '–' + strat().ba_review_percent + '% review', 'p-warn')}${pill('>' + strat().ba_review_percent + '% fail', 'p-bad')}<button class="sm" data-screen="">Screen all now</button></div></div>
  <div class="panel"><div class="tbl-wrap"><table><thead><tr><th>Ticker</th><th>Report</th><th class="r">Last</th><th>Strike</th><th>Expiries</th><th class="r">Front</th><th>Ask/bid</th><th class="r">Back</th><th>Ask/bid</th><th class="r">Front IV</th><th class="r">Back IV</th><th class="r">Δ pts</th><th>Yes/No</th><th>Status</th><th>Other side</th><th>At</th><th></th></tr></thead><tbody>${rows || '<tr><td colspan="17" class="empty">No announcements loaded yet.</td></tr>'}</tbody></table></div>
  ${feed('IBKR snapshots (reqMktData until bid, ask and model IV arrive) · calls and puts at the 4 strikes nearest the price · default strike per Settings (course trade: nearest call at or above the price), other side at the same strike · long leg = next expiry; if its IV is still high or its market fails, the next 3 are tried')}</div>
  ${list.some(x => x.quotes_live === false) ? `<div class="note" style="margin-top:14px"><b>Screened after the close.</b> TWS sends no live option bid / ask outside 9:30–16:00 ET, so the bid-ask screen can't be judged. Screen again during the last hour, or set Market data to Frozen in Settings to use the closing quotes.</div>` : ''}
  ${t && t.state === 'ready' && a.fq ? `<div class="grid g2" style="margin-top:14px">
   <div class="panel"><div class="ph"><h2>${t.symbol} chain</h2>${tkSelect()}</div>${chainTable(t, a)}
   <div class="legend" style="margin-top:8px"><span><i style="background:var(--warn)"></i>Legs being screened</span><span>E = earnings inside this expiry</span></div></div>
   <div class="panel"><h2 style="margin-bottom:10px">Why ${t.symbol} ${['ok', 'iw'].includes(stage(t).k) ? 'passes' : 'stops here'}</h2>
    <div class="rule"><span class="pill ${baCls(a.baRatio[0])}">Front</span><span class="mono">${f2(a.fq[1])} ÷ ${f2(a.fq[0])} = ${rp(a.baRatio[0], 1)}</span></div>
    <div class="rule" style="margin-top:6px"><span class="pill ${baCls(a.baRatio[1])}">Back</span><span class="mono">${f2(a.bq[1])} ÷ ${f2(a.bq[0])} = ${rp(a.baRatio[1], 1)}</span></div>
    ${a.ivDiff != null ? `<div class="rule" style="margin-top:6px">${pill('IV', a.yes ? 'p-good' : 'p-bad')}<span class="mono">${ivp(a.fIV)} − ${ivp(a.bIV)} = ${a.ivDiff.toFixed(1)} pts ${a.yes ? '≥' : '<'} ${strat().iv_edge_points}</span></div>` : ''}
    ${backWhy(t, a) ? `<div class="note" style="margin-top:12px">${esc(backWhy(t, a))}</div>` : ''}
    ${a.error ? `<div class="note" style="margin-top:12px">${esc(a.error)}</div>` : ''}
    <div class="note" style="margin-top:12px"><b>Expiration rule.</b> Short the first expiry after earnings. Prefer the next consecutive expiry for the long leg; skip one or two if its IV is still very high or its market is much wider than later ones.</div></div>
  </div>` : (t && t.state !== 'ready' ? `<div style="margin-top:14px">${noData(t)}</div>` : '')}`;
}
const backWhy = (t, a) => a.front === t.front ? ((t.back_reason_by_right || {})[a.right] ?? t.back_reason ?? '') : '';
function chainTable(t, a) {
  const ks = t.strikes || [];
  const exps = [a.front, a.back].filter(Boolean);
  return `<div class="tbl-wrap"><table class="chain"><thead><tr><th class="r">IV</th><th class="r">Bid</th><th class="r">Ask</th><th class="r">Ask/Bid</th><th>Contract</th><th>Sim</th></tr></thead><tbody>
  ${exps.map((e, ei) => `<tr class="exp"><td colspan="6">${days(t.entry, e)} days to expiration · ${fmtE(e)}${e === t.front ? '<span class="e">E</span>' : ''}</td></tr>` + ks.map(k => {
    const r = a.right || (k >= t.S ? 'C' : 'P'), x = q(t, e, r, k) || {}, on = Math.abs(k - a.K) < 1e-6;
    return `<tr><td class="r num">${ivp(x.iv)}</td><td class="r num"><span class="${on ? 'hl' : ''}" style="padding:1px 4px">${f2(x.bid)}</span></td><td class="r num"><span class="${on ? 'hl' : ''}" style="padding:1px 4px">${f2(x.ask)}</span></td><td class="r num">${x.bid && x.ask ? (x.ask / x.bid * 100).toFixed(0) + '%' : '—'}</td><td class="mono">${t.symbol} ${fmtE(e)} ${k} ${r}</td><td class="mono" style="color:var(${ei === 0 ? '--bad' : '--good'})">${on ? (ei === 0 ? '−1' : '+1') : ''}</td></tr>`}).join('')).join('')}
  </tbody></table></div>`;
}

function pnlChart(t, a) {
  const W = 640, H = 300, L = 46, Rr = 12, Tp = 12, B = 30;
  const xs = a.pts.map(p => p[0]), ys = a.pts.flatMap(p => [p[1], p[2]]);
  const x0 = xs[0], x1 = xs[xs.length - 1]; let y0 = Math.min(...ys), y1 = Math.max(...ys); const pad = (y1 - y0) * .08; y0 -= pad; y1 += pad;
  const X = x => L + (x - x0) / (x1 - x0) * (W - L - Rr), Y = y => Tp + (y1 - y) / (y1 - y0) * (H - Tp - B);
  const path = i => a.pts.map((p, j) => (j ? 'L' : 'M') + X(p[0]).toFixed(1) + ' ' + Y(p[i]).toFixed(1)).join(' ');
  const step = (() => {const r = y1 - y0; const c = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000]; return c.find(s => r / s <= 7) || 2000})();
  const yt = []; for (let v = Math.ceil(y0 / step) * step; v <= y1; v += step) yt.push(v);
  const xr = x1 - x0, xstep = [0.5, 1, 2, 5, 10, 20, 50, 100].find(s => xr / s <= 10) || 200;
  const xt = []; for (let v = Math.ceil(x0 / xstep) * xstep; v <= x1; v += xstep) xt.push(+v.toFixed(2));
  const zone = (a.beLo && a.beHi) ? `<rect x="${X(a.beLo)}" y="${Tp}" width="${X(a.beHi) - X(a.beLo)}" height="${H - Tp - B}" fill="var(--accent)" opacity=".08"/>` : '';
  const beM = [a.beLo, a.beHi].filter(Boolean).map(b => `<line x1="${X(b)}" x2="${X(b)}" y1="${Tp}" y2="${H - B}" stroke="var(--fg)" stroke-dasharray="3 3" opacity=".6"/><text x="${X(b) + (b < t.S ? -4 : 4)}" y="${Tp + 12}" text-anchor="${b < t.S ? 'end' : 'start'}" style="fill:var(--fg)">BE ${b.toFixed(2)}</text>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Calendar spread P&L today and the day after earnings">
  ${yt.map(v => `<line x1="${L}" x2="${W - Rr}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--c-grid)"/><text x="${L - 6}" y="${Y(v) + 3}" text-anchor="end">${v}</text>`).join('')}
  ${xt.map(v => `<text x="${X(v)}" y="${H - B + 16}" text-anchor="middle">${v}</text>`).join('')}
  ${zone}<line x1="${L}" x2="${W - Rr}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--muted)"/>
  <line x1="${X(t.S)}" x2="${X(t.S)}" y1="${Tp}" y2="${H - B}" stroke="var(--warn)" stroke-width="1.5"/><text x="${X(t.S)}" y="${H - 4}" text-anchor="middle" style="fill:var(--warn)">${t.S.toFixed(2)}</text>
  <path d="${path(1)}" fill="none" stroke="var(--c-front)" stroke-width="2"/>
  <path d="${path(2)}" fill="none" stroke="var(--c-fwd)" stroke-width="2.5"/>${beM}</svg>`;
}
function gapChart(t, a) {
  const g = (t.gaps || []).slice(0, lb()); if (!g.length) return '<div class="empty">No past gaps.</div>';
  const W = 640, H = 200, L = 40, B = 34, Tp = 10; const mx = Math.max(...g.map(x => x.gap), a.meg || 0) * 1.15;
  const Y = v => Tp + (1 - v / mx) * (H - Tp - B); const bw = (W - L - 10) / g.length;
  const st = [1, 2, 5, 10].find(s => mx / s <= 6) || 20; const ticks = []; for (let v = 0; v <= mx; v += st) ticks.push(v);
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Past earnings gaps against the endurable gap">
  ${ticks.map(v => `<line x1="${L}" x2="${W - 10}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--c-grid)"/><text x="${L - 6}" y="${Y(v) + 3}" text-anchor="end">${v}%</text>`).join('')}
  ${g.map((x, i) => {const cx = L + i * bw + bw * .2, ok = a.meg != null && x.gap < a.meg; return `<rect x="${cx}" y="${Y(x.gap)}" width="${bw * .6}" height="${Y(0) - Y(x.gap)}" rx="2" fill="var(${ok ? '--good' : '--bad'})" opacity="${x.source === 'inferred' ? .45 : ok ? .75 : .85}"/><text x="${cx + bw * .3}" y="${Y(x.gap) - 4}" text-anchor="middle" style="fill:var(--fg)">${x.gap.toFixed(2)}</text><text x="${cx + bw * .3}" y="${H - B + 14}" text-anchor="middle">${i === 0 ? 'Last' : i + 1 + ' ago'}</text><text x="${cx + bw * .3}" y="${H - B + 27}" text-anchor="middle" style="font-size:9.5px">${x.date.slice(0, 7)}${x.source === 'inferred' ? '*' : ''}</text>`}).join('')}
  ${a.meg != null ? `<line x1="${L}" x2="${W - 10}" y1="${Y(a.meg)}" y2="${Y(a.meg)}" stroke="var(--accent)" stroke-width="2" stroke-dasharray="6 4"/><text x="${W - 12}" y="${Y(a.meg) - 5}" text-anchor="end" style="fill:var(--accent);font-weight:600">Endurable ${a.meg.toFixed(2)}%</text>` : ''}</svg>`;
}

function sideBySide(t, a) {
  const b = A(t, 'alt');
  const C = a.right === 'C' ? a : b, P = a.right === 'C' ? b : a;
  const v = (x, f) => x && x.pts ? f(x) : (x && x.error ? '—' : '…');
  const rows = [
    ['Legs', x => `${fmtE(x.front)} / ${fmtE(x.back)}`],
    ['Bid-ask front / back', x => `${rp(x.baRatio[0])} / ${rp(x.baRatio[1])}`],
    ['IV front − back', x => (x.ivDiff >= 0 ? '+' : '') + x.ivDiff.toFixed(1) + ' pts'],
    ['Debit (25-20)', x => usd(x.debit)],
    ['Endurable gap', x => pct(x.meg)],
    ['Past gaps inside', x => `${x.hits}/${x.n}`],
    ['Max gain, modeled', x => usd0(x.maxGain)],
    ['Verdict', x => pill(x.verdict, x.verdict === 'Strong' ? 'p-good' : x.verdict === 'Acceptable' ? 'p-warn' : 'p-bad')]];
  const cell = (x, f) => {try {return v(x, f)} catch (e) {return '—'}};
  return `<div class="panel"><div class="ph"><h2>Call vs put calendar · ${a.K}</h2><span class="muted" style="font-size:12px">same strike, same crush</span></div>
   <div class="tbl-wrap"><table><thead><tr><th></th><th class="r">${a.right === 'C' ? '<b>Call</b>' : 'Call'}</th><th class="r">${a.right === 'P' ? '<b>Put</b>' : 'Put'}</th></tr></thead><tbody>
   ${rows.map(([l, f]) => `<tr><td>${l}</td><td class="r num">${cell(C, f)}</td><td class="r num">${cell(P, f)}</td></tr>`).join('')}</tbody></table></div>
   <p class="muted" style="font-size:12px;margin:8px 0 0">${strat().strike_rule === 'nearest_otm' ? 'The default is the nearest strike on its out-of-the-money side (the course spreadsheet).' : 'The default is the course trade: the nearest call at or above the price (RHT at 153.09 → 155C).'} The other side is at the same strike. Change it in Settings.</p></div>`;
}
function ivByStrike(t, a) {
  // the Is / Was screen at every nearby strike, both sides (computed by the app at your settings' crush)
  const rows = (t.by_strike || []).filter(x => x.fIV != null || x.error);
  if (!rows.length) return '';
  const top = f => {const v = rows.map(x => x[f]).filter(x => x != null); return v.length && Math.max(...v) - Math.min(...v) > 0.0005 ? Math.max(...v) : null};
  const hi = {fIV: top('fIV'), ivDiff: top('ivDiff'), meg: top('meg')};
  const g = (x, f, txt) => x[f] == null ? '—' : hi[f] != null && Math.abs(x[f] - hi[f]) < 1e-9 ? `<b style="color:var(--good)">${txt}</b>` : txt;
  const vp = (txt, v) => pill(txt, v === 'Strong' ? 'p-good' : v === 'Acceptable' ? 'p-warn' : 'p-bad');
  const shown = x => Math.abs(x.K - a.K) < 1e-6 && x.right === a.right;
  return `<div class="ph"><h2>Strikes compared · ${fmtE(a.front)} short</h2><span class="muted" style="font-size:12px">highest IV, IV edge and endurable gap in green</span></div>
   <div class="tbl-wrap"><table><thead><tr><th>Strike</th><th>Side</th><th class="r">Front IV</th><th class="r">Long IV</th><th class="r">Edge</th><th class="r">Bid-ask</th><th class="r">Debit</th><th class="r">Endurable</th><th class="r">Inside</th></tr></thead><tbody>
   ${rows.map(x => `<tr class="${shown(x) ? '' : 'clickable'}" data-strike="${x.K}" data-sright="${x.right}"${shown(x) ? ' style="background:var(--c-grid)"' : ''}>
     <td class="num">${x.K}${Math.abs(x.K - t.K) < 1e-6 ? ' <span class="muted">default</span>' : ''}</td><td>${x.right === 'C' ? 'Call' : 'Put'} <span class="muted">${fmtE(x.back)}</span></td>
     <td class="r num">${g(x, 'fIV', ivp(x.fIV))}</td><td class="r num">${ivp(x.bIV)}</td>
     <td class="r num">${g(x, 'ivDiff', x.ivDiff == null ? '' : (x.ivDiff >= 0 ? '+' : '') + x.ivDiff.toFixed(1))}</td>
     <td class="r num">${x.baRatio ? rp(Math.max(...x.baRatio.filter(v => v != null))) : '—'}</td>
     <td class="r num">${x.entry != null ? usd(x.entry) : '—'}</td><td class="r num">${g(x, 'meg', pct(x.meg))}</td>
     <td class="r num" title="${esc(x.verdict || '')}">${x.n ? vp(x.hits + '/' + x.n, x.verdict) : '—'}</td></tr>`).join('')}</tbody></table></div>
   <p class="muted" style="font-size:12px;margin:8px 0 0">Click a row to model that strike above. Inside = past gaps inside the endurable gap, coloured by verdict. Edge = front IV − long-leg IV in points. Bid-ask = the wider leg. Shaded = the spread shown above.</p>`;
}
function vAnalyze() {
  const t = sel();
  if (!t || t.state !== 'ready') return `${banner()}<div class="head"><div><h1>4 · Is / Was analysis</h1></div>${tkSelect()}</div>${noData(t)}`;
  const a = A(t);
  const head = `${banner()}<div class="head"><div><h1>4 · Is / Was analysis</h1><p>Model the time spread, move the date to the day after earnings, crush the front IV, and read the breakevens. That distance as a share of price is the <b>Is</b>, the endurable gap. Compare it with the last ${lb()} earnings gaps, the <b>Was</b>.</p></div>${tkSelect()}</div>`;
  const fronts = t.expiries || [], backs = fronts.filter(e => e > a.front);
  const ivAt = e => {const r = a.right; const x = q(t, e, r, a.K); return x && x.iv ? ' · IV ' + ivp(x.iv) : ''};
  const ctrls = `<div class="panel"><div class="ctrls">
    <label class="f" for="k-sel">Strike<select id="k-sel">${(t.strikes || []).map(k => `<option value="${k}" ${Math.abs(k - a.K) < 1e-6 ? 'selected' : ''}>${k}${Math.abs(k - t.S) === Math.min(...(t.strikes || []).map(z => Math.abs(z - t.S))) ? ' · nearest' : ''}${Math.abs(k - t.K) < 1e-6 ? ' · default' : ''}</option>`).join('')}</select></label>
    <label class="f">Calendar<span class="seg"><button data-right="C" class="${a.right === 'C' ? 'on' : ''}">Call</button><button data-right="P" class="${a.right === 'P' ? 'on' : ''}">Put</button></span></label>
    <label class="f" for="f-sel">Short expiry<select id="f-sel">${fronts.slice(0, -1).map(f => `<option value="${f}" ${f === a.front ? 'selected' : ''}>${fmtE(f)}${ivAt(f)}${f === t.front ? ' · E' : ''}</option>`).join('')}</select></label>
    <label class="f" for="b-sel">Long expiry<select id="b-sel">${backs.map(b => `<option value="${b}" ${b === a.back ? 'selected' : ''}>${fmtE(b)}${ivAt(b)}</option>`).join('')}</select></label>
    <label class="f" for="crush"><span class="seg" style="align-self:flex-start"><button data-cm="mult" class="${S.crushMode === 'mult' ? 'on' : ''}">Front IV × %</button><button data-cm="retrace" class="${S.crushMode === 'retrace' ? 'on' : ''}">Retrace premium</button></span><span class="v">${a.fIV ? (S.crushMode === 'retrace' ? `${ivp(a.fIV)} − ${S.crush}% × (${ivp(a.fIV)} − ${ivp(a.bIV)})` : `${ivp(a.fIV)} × ${S.crush}%`) + ` = <b>${ivp(a.fIV1)} IV</b>` : ''}</span><input id="crush" type="range" min="${S.crushMode === 'mult' ? 30 : 50}" max="100" step="5" value="${S.crush}"></label>
    <label class="f" for="bchg">Back IV change <span class="v">${S.backChg > 0 ? '+' : ''}${S.backChg}% → ${ivp(a.bIV1)} IV</span><input id="bchg" type="range" min="-30" max="10" step="5" value="${S.backChg}"></label>
    <label class="f">Endurable gap basis<span class="seg"><button data-basis="avg" class="${S.basis === 'avg' ? 'on' : ''}">Average of BEs</button><button data-basis="near" class="${S.basis === 'near' ? 'on' : ''}">Nearer BE</button></span></label>
    <label class="f">&nbsp;<button class="sm" id="reset-an">Reset to settings</button></label>
  </div></div>`;
  if (!a.pts) return head + ctrls + `<div class="panel empty" style="margin-top:14px">${esc(a.error || 'Calculating…')}</div>`;
  const vb = a.verdict === 'Strong' ? 'vb-good' : a.verdict === 'Acceptable' ? 'vb-warn' : 'vb-bad';
  const dist = a.dist;
  return head + ctrls + `
  <div class="grid g-an" style="margin-top:14px">
   <div class="panel"><div class="ph"><h2>${t.symbol} ${a.K} ${a.call ? 'call' : 'put'} calendar · ${fmtE(a.front)} / ${fmtE(a.back)} · debit ${usd(a.debit)}</h2>
     <div class="legend"><span><i style="background:var(--c-front)"></i>Today (${fmtD(t.entry)})</span><span><i style="background:var(--c-fwd)"></i>Day after (${fmtD(t.model)}), crushed</span><span><i style="background:var(--accent);opacity:.25;height:8px"></i>Endurable zone</span></div></div>
     ${pnlChart(t, a)}
     ${feed(`Black-Scholes, r=${(strat().risk_free_rate * 100).toFixed(1)}% · front ${ivp(a.fIV)}→${ivp(a.fIV1)} IV, ${a.daysFront}d left · back ${ivp(a.bIV)}→${ivp(a.bIV1)} IV, ${a.daysBack}d left · per spread ×100 · IV = IBKR model IV per leg`)}
     <div style="margin-top:14px">${ivByStrike(t, a)}</div></div>
   <div class="grid" style="align-content:start">
    <div class="verdict ${vb}"><div><div class="big">${a.verdict}</div><div style="font-size:12.5px">${a.hits} of ${a.n} past gaps were smaller than ${pct(a.meg)}.${a.straddle ? ' The endurable gap is below the median gap, so check the straddle playbook.' : ''}</div></div></div>
    ${a.front !== t.front ? `<div class="note"><b>Short leg is not the earnings expiry.</b> The course shorts the first expiry after earnings (${fmtE(t.front)}), so the short option carries the IV that gets crushed.</div>` : ''}
    ${t.gap_note ? `<div class="note">${esc(t.gap_note)} They are marked * and drawn lighter.</div>` : ''}
    <div class="panel"><div class="grid g2">
     <div class="stat"><span class="l">Is · endurable gap</span><span class="v">${pct(a.meg)}</span><span class="s">${usd(dist)} / ${usd(t.S)}</span></div>
     <div class="stat"><span class="l">Was · median gap</span><span class="v">${pct(a.med)}</span><span class="s">last ${a.n} announcements</span></div>
     <div class="stat"><span class="l">Lower BE</span><span class="v">${a.beLo ? a.beLo.toFixed(2) : '—'}</span><span class="s">${a.beLo ? '−' + usd(a.dLo).slice(1) + ' · −' + (a.dLo / t.S * 100).toFixed(1) + '%' : ''}</span></div>
     <div class="stat"><span class="l">Upper BE</span><span class="v">${a.beHi ? a.beHi.toFixed(2) : '—'}</span><span class="s">${a.beHi ? '+' + usd(a.dHi) + ' · +' + (a.dHi / t.S * 100).toFixed(1) + '%' : ''}</span></div>
     <div class="stat"><span class="l">Max gain at strike</span><span class="v">${usd0(a.maxGain)}</span><span class="s">per spread, modeled</span></div>
     <div class="stat"><span class="l">Max loss</span><span class="v">${usd0(a.maxLoss)}</span><span class="s">debit paid</span></div>
    </div></div>
    ${sideBySide(t, a)}
   </div>
  </div>
  <div class="grid g-an" style="margin-top:14px">
   <div class="panel"><div class="ph"><h2>Was · last ${a.n} earnings gaps</h2><span class="muted" style="font-size:12px">|close before − open after| ÷ close before</span></div>${gapChart(t, a)}
    ${feed('IBKR daily bars (3 years, cached a day) · report dates from Finnhub / Nasdaq / your entries')}</div>
   <div class="panel"><h2 style="margin-bottom:8px">Is / Was worksheet</h2><div class="tbl-wrap"><table class="ws"><tbody>
    <tr><td>Modeled endurable gap</td><td class="r num">${usd(dist)} / ${usd(t.S)}</td><td class="r num"><b>${pct(a.meg)}</b></td></tr>
    ${(t.gaps || []).slice(0, lb()).map((g, i) => `<tr><td>${i === 0 ? 'Last' : (i + 1) + ' ago'} · ${fmtE(g.date)} ${esc(g.timing)}${g.source === 'inferred' ? '*' : ''}</td><td class="r num">${usd(g.close_before)} − ${usd(g.open_after)} = ${usd(Math.abs(g.close_before - g.open_after))}</td><td class="r num" style="color:var(${g.gap < a.meg ? '--good' : '--bad'})">${pct(g.gap)}</td></tr>`).join('')}
   </tbody></table></div>
   <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap"><button class="pri" data-go="risk" data-tk="${t.symbol}">Continue to risk checklist →</button>${a.straddle ? `<button data-go="playbooks">Open straddle playbook</button>` : ''}</div></div>
  </div>`;
}

function vRisk() {
  const t = sel();
  if (!t || t.state !== 'ready') return `${banner()}<div class="head"><div><h1>5 · Risk checklist</h1></div>${tkSelect()}</div>${noData(t)}`;
  const a = A(t); if (!a.pts) return `${banner()}<div class="head"><div><h1>5 · Risk checklist</h1></div>${tkSelect()}</div><div class="panel empty">${esc(a.error || 'Calculating…')}</div>`;
  const qty = Math.max(1, Math.floor(S.budget / a.maxLoss)); const st = stage(t);
  const tradesToday = (ST.trades || []).filter(x => x.entry === t.entry).length, lim = limit();
  const auto = [
    ['date', 'Earnings date and time checked', `${fmtD(t.date)} ${t.timing} · ${t.status === 'verified' ? 'two sources agree' : t.status === 'manual' ? 'confirmed by you' : t.status === 'single' ? 'one source only: check the IR page' : 'sources disagree'}`, t.status !== 'conflict'],
    ['ba', 'Bid-ask screen passed on both legs', `${rp(a.baRatio[0], 0)} / ${rp(a.baRatio[1], 0)}`, a.baOverall !== 'fail'],
    ['yn', 'Yes / No term structure', `${a.ivDiff.toFixed(1)} pts front over back`, a.yes],
    ['iw', 'Is / Was supports the trade', `${a.hits}/${a.n} past gaps inside ${pct(a.meg)}`, a.verdict === 'Strong' || a.verdict === 'Acceptable'],
    ['lim', 'Within daily trade limit', `${tradesToday} of ${lim} recorded for ${fmtD(t.entry)}`, tradesToday < lim]];
  const man = [
    ['ml', 'Maximum potential loss documented', `${usd0(a.maxLoss * qty)} for ${qty} spread${qty > 1 ? 's' : ''} (debit × 100 × qty)`],
    ['sz', 'Trade size planned from max loss', `Budget ${usd0(S.budget)} ÷ ${usd0(a.maxLoss)} = ${qty}. Start small while learning.`],
    ['be', 'Projected breakevens documented', `${a.beLo ? a.beLo.toFixed(2) : '—'} and ${a.beHi ? a.beHi.toFixed(2) : '—'} on ${fmtD(t.model)}`],
    ['ex', 'Exit plan chosen before entry', S.exitMode === 'active' ? 'Active: work limit orders, close by end of day' : 'Passive: profit target before the open, time stop after lunch'],
    ['one', 'Enter as one combo order, not on legs', 'Single combo order, 25-20 middling'],
    ['hr', 'Enter in the last hour before the announcement', `${fmtD(t.entry)} 15:00–16:00 ET`]];
  const done = man.filter(m => S.checks[t.symbol + m[0]]).length;
  return `${banner()}<div class="head"><div><h1>5 · Earnings time spread risk checklist</h1><p>Document the worst case, size from it, write down the breakevens and pick the exit plan before you enter.</p></div>${tkSelect()}</div>
  <div class="grid g2">
   <div class="panel"><div class="ph"><h2>${t.symbol} ${a.K}${a.right} ${fmtE(a.front)} / ${fmtE(a.back)}</h2>${pill(st.lbl, st.cls)}</div>
    <h3 style="margin:4px 0 2px">Checked by the app</h3>
    ${auto.map(c => `<div class="check"><span aria-hidden="true" style="color:var(${c[3] ? '--good' : '--bad'});font-weight:700">${c[3] ? '✓' : '✕'}</span><div>${c[1]}<div class="d">${esc(c[2])}</div></div>${pill(c[3] ? 'OK' : 'Stop', c[3] ? 'p-good' : 'p-bad')}</div>`).join('')}
    <h3 style="margin:14px 0 2px">You confirm · ${done}/${man.length}</h3>
    ${man.map(c => `<label class="check" for="ck-${c[0]}"><input type="checkbox" id="ck-${c[0]}" data-ck="${t.symbol + c[0]}" ${S.checks[t.symbol + c[0]] ? 'checked' : ''}><div>${c[1]}<div class="d">${esc(c[2])}</div></div><span></span></label>`).join('')}
   </div>
   <div class="grid" style="align-content:start">
    <div class="panel"><h2 style="margin-bottom:10px">Size</h2><div class="ctrls">
      <label class="f" for="budget">Max loss budget for this trade<input id="budget" type="number" min="50" step="50" value="${S.budget}"></label>
      <label class="f">Spreads<span class="v" style="font-size:20px">${qty}</span></label></div>
      <dl class="kv" style="margin-top:12px"><dt>Debit (25-20 entry)</dt><dd>${usd(a.debit)}</dd><dt>Max loss</dt><dd>${usd0(a.maxLoss * qty)}</dd><dt>Modeled max gain</dt><dd>${usd0(a.maxGain * qty)}</dd><dt>Breakevens</dt><dd>${a.beLo ? a.beLo.toFixed(2) : '—'} – ${a.beHi ? a.beHi.toFixed(2) : '—'}</dd></dl></div>
    <div class="panel"><div class="ph"><h2>Exit plan</h2><span class="seg"><button data-exit="active" class="${S.exitMode === 'active' ? 'on' : ''}">Active</button><button data-exit="passive" class="${S.exitMode === 'passive' ? 'on' : ''}">Passive</button></span></div>
     ${S.exitMode === 'active' ? `<p style="margin:0">Preferred. Watch the spread after the open, let limit orders rest, and wait for IV to bleed out. Bid-asks are wide early and P&L can flip both ways. <b>Close by end of day.</b></p>` :
     `<div class="ctrls"><label class="f" for="tgt">Profit target <span class="v">${S.target}% → ${usd(a.debit * (1 + S.target / 100))}</span><input id="tgt" type="range" min="25" max="100" step="5" value="${S.target}"></label><label class="f" for="tstop">Time stop (ET)<input id="tstop" type="text" value="${esc(S.timeStop)}"></label></div>
     <p style="margin:10px 0 0" class="muted">Before the open, rest a sell order at the target. If it hasn't filled by the time stop, close with the middling ladder. Works for busy days; usually leaves money on the table.</p>`}</div>
    <button class="pri" data-go="entry" data-tk="${t.symbol}" ${done === man.length && auto.every(c => c[3]) ? '' : 'disabled'}>Build entry ticket →</button>
    ${done === man.length && auto.every(c => c[3]) ? '' : `<p class="muted" style="margin:0;font-size:12px">${auto.every(c => c[3]) ? 'Tick every confirmation to continue.' : 'A check failed. This ticker should not be traded today.'}</p>`}
   </div>
  </div>`;
}

function vEntry() {
  const t = sel();
  if (!t || t.state !== 'ready') return `${banner()}<div class="head"><div><h1>6 · Entry ticket</h1></div>${tkSelect()}</div>${noData(t)}`;
  const a = A(t); if (!a.pts) return `${banner()}<div class="head"><div><h1>6 · Entry ticket</h1></div>${tkSelect()}</div><div class="panel empty">${esc(a.error || 'Calculating…')}</div>`;
  const qty = Math.max(1, Math.floor(S.budget / a.maxLoss));
  const steps = (a.ladder || []).map(s => ({t: s.seconds, p: s.price}));
  const L = S.ladder && S.ladder.tk === t.symbol ? S.ladder : null;
  const R = a.call ? 'CALL' : 'PUT ';
  return `${banner()}<div class="head"><div><h1>6 · Entry ticket</h1><p>One combo order in the last hour before the announcement, priced with the 25-20 middling rule: bid 25% of the spread's width below the offer, wait 20 seconds, step up if unfilled. Enter it in TWS yourself: this app is read-only.</p></div>${tkSelect()}</div>
  <div class="grid g2">
   <div class="panel"><h2 style="margin-bottom:10px">Combo order</h2>
<div class="ticket">BUY ${qty} ${t.symbol} CALENDAR  (combo, SMART)
  leg 1  SELL ${qty}  ${t.symbol} ${a.front} ${a.K} ${R}   ${f2(a.fq[0])} × ${f2(a.fq[1])}
  leg 2  BUY  ${qty}  ${t.symbol} ${a.back} ${a.K} ${R}   ${f2(a.bq[0])} × ${f2(a.bq[1])}
  type   LMT DEBIT ${a.entry.toFixed(2)}   TIF DAY
  window ${fmtD(t.entry)} 15:00–16:00 ET</div>
    <dl class="kv" style="margin-top:12px"><dt>Spread offer (natural)</dt><dd>${f2(a.bq[1])} − ${f2(a.fq[0])} = ${f2(a.natural)}</dd><dt>Spread bid</dt><dd>${f2(a.bq[0])} − ${f2(a.fq[1])} = ${f2(a.inside)}</dd><dt>Width</dt><dd>${f2(a.width)}</dd><dt>25-20 start</dt><dd>${f2(a.natural)} − 0.25 × ${f2(a.width)} → <b>${a.entry.toFixed(2)}</b></dd><dt>Quotes from</dt><dd>${esc(t.screened || '—')} <button class="sm" data-screen="${t.symbol}">Refresh</button></dd></dl>
    <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;align-items:center"><button id="simulate">Show ladder steps</button><button class="pri" disabled title="This app does not place orders">Send to IBKR</button><span class="muted" style="font-size:12px">Order placement is off: the app connects read-only.</span></div>
    <h3 style="margin:16px 0 6px">Record your fill</h3>
    <form class="row" id="fillf"><label class="f">Spreads<input type="number" id="fill-qty" min="1" value="${qty}" style="width:80px"></label><label class="f">Fill (debit)<input type="number" id="fill-px" step="0.01" min="0.01" value="${a.entry.toFixed(2)}" style="width:100px"></label><button class="pri" type="submit">Add to my trades</button></form>
    <p class="muted" style="font-size:12px;margin:6px 0 0">The exit page uses it to price the close the next session.</p></div>
   <div class="panel"><div class="ph"><h2>25-20 ladder</h2><span class="muted" style="font-size:12px">+$0.05 every 20s, capped at natural</span></div>
    <div class="ladder">${steps.map((s, i) => {const shown = !L || i <= L.step; return shown ? `<div><span>+${s.t}s</span><span>Bid ${s.p.toFixed(2)}</span><span style="text-align:right">${usd0(s.p * 100 * qty)}</span></div>` : ''}).join('')}</div>
    <div class="note" style="margin-top:10px">Course example: the RHT 155 calendar was offered at 0.90 with a 0.20 bid. The 25-20 rule started at 0.725, rounded down to 0.70, and it filled there.</div>
    ${feed('Enter as one combo (BAG) order in TWS · modify the price every 20 s until filled or the window closes')}</div>
  </div>`;
}

function vExit() {
  const tr = (ST.trades || []).filter(x => !x.closed);
  const head = `${banner()}<div class="head"><div><h1>7 · Exit the day after</h1><p>Take it off the next session regardless of what happens. IV crush is slow but fairly reliable; middle the order because markets are wide: offer at the spread bid plus 25% of the width.</p></div></div>`;
  if (!tr.length) return head + `<div class="panel empty">No open trades recorded. Record a fill on the entry ticket and it shows here.</div>` + closedTable();
  const x = tr.find(z => z.id === S.exitSel) || tr[0]; S.exitSel = x.id;
  const qd = S.exitQuotes[x.id];
  let calc = null;
  if (qd && !qd.error) {
    const f = qd.quotes[1], b = qd.quotes[2];
    if (f && b && f.bid != null && f.ask != null && b.bid != null && b.ask != null) {
      const bid = b.bid - f.ask, ask = b.ask - f.bid, w = ask - bid, off = tick(bid + .25 * w);
      calc = {bid, ask, w, off, pnl: (off - x.debit) * 100 * x.qty, f, b, S: qd.quotes[0].underlying};
    }
  }
  const tgt = x.debit * (1 + S.target / 100);
  return head + `<div class="grid g3">${tr.map(z => `<div class="snap ${z.id === x.id ? 'on' : ''}" data-trade="${z.id}" tabindex="0" role="button"><div class="t">Entered ${fmtD(z.entry)} · exit ${fmtD(z.exit)}</div><div class="mono" style="margin-top:4px;font-weight:600">+${z.qty} ${z.symbol} ${z.K}${z.right} ${fmtE(z.front)}/${fmtE(z.back)} @ ${z.debit.toFixed(2)}</div></div>`).join('')}</div>
  <div class="grid g2" style="margin-top:14px">
   <div class="panel"><div class="ph"><h2>Close ticket</h2><button class="pri sm" id="getq">Get quotes from TWS</button></div>
    ${qd && qd.error ? `<div class="err">${esc(qd.error)}</div>` : ''}
    ${calc ? `<div class="tbl-wrap"><table class="chain"><thead><tr><th>Leg</th><th class="r">IV</th><th class="r">Bid</th><th class="r">Ask</th></tr></thead><tbody>
     <tr><td class="mono">−${x.qty} ${x.symbol} ${fmtE(x.front)} ${x.K}${x.right}</td><td class="r num">${ivp(calc.f.iv)}</td><td class="r num">${f2(calc.f.bid)}</td><td class="r num"><span class="hl" style="padding:1px 4px">${f2(calc.f.ask)}</span></td></tr>
     <tr><td class="mono">+${x.qty} ${x.symbol} ${fmtE(x.back)} ${x.K}${x.right}</td><td class="r num">${ivp(calc.b.iv)}</td><td class="r num"><span class="hl" style="padding:1px 4px">${f2(calc.b.bid)}</span></td><td class="r num">${f2(calc.b.ask)}</td></tr></tbody></table></div>
     <dl class="kv" style="margin-top:10px"><dt>${x.symbol}</dt><dd>${f2(calc.S)}</dd><dt>Spread bid (sell now)</dt><dd>${f2(calc.bid)}</dd><dt>Spread ask</dt><dd>${f2(calc.ask)}</dd><dt>Width</dt><dd>${f2(calc.w)}</dd><dt>Middled offer (bid + 25%)</dt><dd><b>${calc.off.toFixed(2)}</b></dd><dt>P&L if filled</dt><dd style="color:var(${calc.pnl >= 0 ? '--good' : '--bad'})">${calc.pnl >= 0 ? '+' : ''}${usd0(calc.pnl)} (${((calc.off - x.debit) / x.debit * 100).toFixed(0)}%)</dd><dt>Quoted</dt><dd>${esc(qd.time)}</dd></dl>` : `<p class="muted">Press “Get quotes” for live bid / ask on both legs.</p>`}
    <form class="row" id="closef" style="margin-top:12px"><label class="f">Closed at (credit)<input type="number" id="close-px" step="0.01" min="0" value="${calc ? calc.off.toFixed(2) : ''}" style="width:100px"></label><button type="submit">Mark closed</button><button type="button" id="del-trade">Delete</button></form></div>
   <div class="panel"><div class="ph"><h2>Passive plan</h2>${pill('Set before 09:30', 'p-neutral')}</div>
     <div class="ctrls"><label class="f" for="tgt">Profit target <span class="v">${S.target}% → ${usd(tgt)}</span><input id="tgt" type="range" min="25" max="100" step="5" value="${S.target}"></label><label class="f" for="tstop">Time stop (ET)<input id="tstop" type="text" value="${esc(S.timeStop)}"></label></div>
     <dl class="kv" style="margin-top:10px"><dt>Profit target</dt><dd>sell at ${usd(tgt)}</dd><dt>Time stop</dt><dd>${esc(S.timeStop)} ET</dd><dt>Hard stop</dt><dd>15:55 ET, any price</dd></dl>
     ${calc ? `<div class="note" style="margin-top:10px">${calc.off >= tgt - 1e-9 ? `The middled offer ${calc.off.toFixed(2)} reaches the target, so a resting order would likely fill.` : `The middled offer ${calc.off.toFixed(2)} is below the ${usd(tgt)} target; the order keeps resting.`}</div>` : ''}
     <div class="note" style="margin-top:10px">Course example (RHT, the day after): at 09:35 the stock had gapped up near the upper breakeven and the spread showed a loss; by 13:00 it had drifted back toward the strike and closed for about 50%.</div></div>
  </div>` + closedTable();
}
function closedTable() {
  const c = (ST.trades || []).filter(x => x.closed);
  if (!c.length) return '';
  const tot = c.reduce((s, x) => s + (x.close - x.debit) * 100 * x.qty, 0);
  return `<div class="panel" style="margin-top:14px"><div class="ph"><h2>Closed trades</h2><span class="mono" style="color:var(${tot >= 0 ? '--good' : '--bad'})">${tot >= 0 ? '+' : ''}${usd0(tot)}</span></div><div class="tbl-wrap"><table><thead><tr><th>Trade</th><th>Entered</th><th class="r">Debit</th><th class="r">Close</th><th class="r">P&L</th></tr></thead><tbody>
  ${c.map(x => {const p = (x.close - x.debit) * 100 * x.qty; return `<tr><td class="mono">+${x.qty} ${x.symbol} ${x.K}${x.right} ${fmtE(x.front)}/${fmtE(x.back)}</td><td>${fmtD(x.entry)}</td><td class="r num">${x.debit.toFixed(2)}</td><td class="r num">${x.close.toFixed(2)}</td><td class="r num" style="color:var(${p >= 0 ? '--good' : '--bad'})">${p >= 0 ? '+' : ''}${usd0(p)}</td></tr>`}).join('')}</tbody></table></div></div>`;
}

function vPlaybooks() {
  const st = ST.cands.filter(t => t.state === 'ready' && t.a && t.a.straddleQuote);
  return `${banner()}<div class="head"><div><h1>Other earnings plays</h1><p>Variations that reuse the same Is / Was machinery. The calendar stays the primary trade.</p></div></div>
  <div class="panel"><div class="ph"><h2>Straddle check</h2>${pill('Rare', 'p-acc')}</div>
   <p style="margin:0 0 10px">When the term structure is not very disparate and the calendar's endurable gap is <i>smaller</i> than typical gaps, model a long straddle instead: it needs the stock to move more than its cost. Flagged rows have the calendar's endurable gap below the median past gap.</p>
   ${st.length ? `<div class="tbl-wrap"><table><thead><tr><th>Ticker</th><th>Report</th><th class="r">Straddle</th><th class="r">Cost (asks)</th><th class="r">Move needed</th><th class="r">Past gaps above it</th><th class="r">Calendar endurable</th><th class="r">Median gap</th><th></th></tr></thead><tbody>
   ${st.map(t => {const s = t.a.straddleQuote; return `<tr class="${t.a.straddle ? '' : 'dim'}"><td class="tk">${t.symbol}</td><td>${fmtE(t.date)} ${pill(t.timing, 'p-neutral')}</td><td class="r num">${fmtE(t.front)} ${s.K}</td><td class="r num">${f2(s.call_ask)} + ${f2(s.put_ask)} = ${f2(s.cost)}</td><td class="r num">${pct(s.need, 1)}</td><td class="r num">${s.above}/${s.n}</td><td class="r num">${pct(t.a.meg)}</td><td class="r num">${pct(t.a.med)}</td><td>${t.a.straddle ? pill('Flag', 'p-acc') : ''}</td></tr>`}).join('')}</tbody></table></div>` : '<div class="empty">Shows once candidates are screened.</div>'}
   <p class="muted" style="font-size:12px;margin:8px 0 0">Course example: LOGI (Oct 2017), 37 straddle for 3.30 against a modeled 8.1% move; 3 of 7 past gaps were bigger. You can leg out: sell the winning side on a big gap and keep the other for a retracement.</p></div>
  <div class="grid g2" style="margin-top:14px">
   <div class="panel pb"><h2>Directional time spread</h2>${pill('Lean, don\'t bet', 'p-warn')}
    <p>Direction on earnings is hard to call, so stay neutral by default. Early in the season you may lean slightly bullish: set the strike just short of the price target, because the calendar pays most at the strike. Pick the strike on the Is / Was page.</p></div>
   <div class="panel pb"><h2>Put sale for a long-term buy</h2>${pill('<2.5% of trades', 'p-neutral')}
    <p>Only if you'd buy the stock before earnings anyway. Sell a just-in-the-money front-expiry put when the downside is acceptable, and compare the premium as a % of price with prior gaps. Course example: LULU 79.5 put for 3.45 = 4.4% of price.</p></div>
  </div>`;
}

function vResults() {
  const checks = [...(strat().exit_checks || [])].sort();
  const chk = checks.includes(S.resCheck) ? S.resCheck : checks[checks.length - 1];
  const other = S.resSide === 'other';
  const recs = [...(ST.journal || [])].sort((x, y) => y.entry_date.localeCompare(x.entry_date) || x.symbol.localeCompare(y.symbol));
  const sideOf = r => {const d = r.default_right; const k = other ? Object.keys(r.sides).find(x => x !== d) : d; return k ? {k, s: r.sides[k]} : null};
  const group = (r, sd) => {
    if (!other) return r.stage_k === 'ok' ? sd.s.verdict : r.stage_k === 'iw' ? 'Weak' : 'Did not pass screens';
    if (sd.s.baOverall === 'fail') return 'Fails bid-ask';
    return sd.s.verdict && sd.s.verdict !== 'n/a' ? sd.s.verdict : 'No Is / Was';
  };
  const order = ['Strong', 'Acceptable', 'Weak', 'Did not pass screens', 'Fails bid-ask', 'No Is / Was'];
  const G = {};
  recs.forEach(r => {const sd = sideOf(r); if (!sd) return; const q = (((r.exits || {})[chk] || {}).sides || {})[sd.k];
    const g = G[group(r, sd)] = G[group(r, sd)] || {n: 0, done: 0, wins: 0, sum: 0};
    g.n++; if (q && q.pnl != null) {g.done++; g.sum += q.pnl; if (q.pnl > 0) g.wins++}});
  const money = x => `<span style="color:var(${x >= 0 ? '--good' : '--bad'})">${x >= 0 ? '+' : ''}${usd0(x)}</span>`;
  return `${banner()}<div class="head"><div><h1>Forward test</h1><p>Every candidate's last-hour screen on its entry day is saved, passed or not, with both the call and the put calendar. The next session the same spreads are quoted at ${checks.join(', ')} ET and priced to sell at the spread bid + 25% of the width. P&L is per spread against the 25-20 entry price. Nothing is traded.</p></div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><span class="seg">${checks.map(c => `<button data-rcheck="${c}" class="${c === chk ? 'on' : ''}">${c}</button>`).join('')}</span>
  <span class="seg"><button data-rside="default" class="${other ? '' : 'on'}">Default side</button><button data-rside="other" class="${other ? 'on' : ''}">Other side</button></span>
  <a class="btn" href="/api/journal.csv">Download CSV</a></div></div>
  ${recs.length ? `<div class="panel"><div class="ph"><h2>By verdict at ${chk} ET</h2><span class="muted" style="font-size:12px">${other ? 'the other side of each strike' : 'the side the screens chose'}</span></div><div class="tbl-wrap"><table><thead><tr><th>Group</th><th class="r">Candidates</th><th class="r">Priced at ${chk}</th><th class="r">Winners</th><th class="r">Win rate</th><th class="r">Avg P&L / spread</th><th class="r">Total</th></tr></thead><tbody>
   ${order.filter(k => G[k]).map(k => {const g = G[k]; return `<tr><td>${k}</td><td class="r num">${g.n}</td><td class="r num">${g.done}</td><td class="r num">${g.wins}</td><td class="r num">${g.done ? pct(g.wins / g.done * 100, 0) : '—'}</td><td class="r num">${g.done ? money(g.sum / g.done) : '—'}</td><td class="r num">${g.done ? money(g.sum) : '—'}</td></tr>`}).join('')}
   </tbody></table></div></div>
  <div class="panel" style="margin-top:14px"><h2 style="margin-bottom:8px">Candidates</h2><div class="tbl-wrap"><table><thead><tr><th>Entered</th><th>Ticker</th><th>Report</th><th>Screens</th><th>Calendar</th><th class="r">Entry</th>${checks.map(c => `<th class="r">${c}</th>`).join('')}<th class="r">Stock move</th></tr></thead><tbody>
   ${recs.map(r => {const sd = sideOf(r); if (!sd) return ''; const s = sd.s; const last = checks.map(c => r.exits[c]).filter(Boolean).pop();
     return `<tr><td>${fmtD(r.entry_date)}</td><td class="tk">${r.symbol}</td><td>${fmtE(r.report)} ${pill(r.timing, 'p-neutral')}</td><td>${other ? pill(group(r, sd), 'p-neutral') : pill(r.stage || '—', r.stage_k === 'ok' ? 'p-good' : r.stage_k === 'iw' ? 'p-warn' : 'p-neutral')}</td>
     <td class="mono">${s.K}${s.right} ${fmtE(s.front)}/${fmtE(s.back)}</td><td class="r num">${f2(s.entry)}</td>
     ${checks.map(c => {const q = ((r.exits[c] || {}).sides || {})[sd.k]; return `<td class="r num">${q && q.pnl != null ? money(q.pnl) + `<div class="muted" style="font-size:11px">@ ${f2(q.offer)}</div>` : q && q.error ? '<span class="muted">no market</span>' : r.exit_date > today() || (r.exit_date === today() && !r.exits[c]) ? '<span class="muted">' + fmtE(r.exit_date) + '</span>' : '<span class="muted">missed</span>'}</td>`}).join('')}
     <td class="r num">${last && last.move != null ? (last.move >= 0 ? '+' : '') + last.move.toFixed(2) + '%' : '—'}</td></tr>`}).join('')}
   </tbody></table></div>
   <p class="muted" style="font-size:12px;margin:8px 0 0">"missed" means the app wasn't running (or TWS wasn't connected) within an hour of that check. Entries record only while the app runs in the last hour on the entry day.</p></div>` :
  `<div class="panel empty">No records yet. They start with the next last-hour screen (15:00–16:00 ET on an entry day, with TWS connected and Live or Frozen data), and the next session's checks fill in on their own while the app runs.</div>`}`;
}

function vSettings() {
  const s = ST.settings; if (!s) return '<div class="empty">Loading…</div>';
  const c = s.connection, e = s.earnings, g = s.strategy, l = s.load;
  const num = (id, v, step = 1, min = '', max = '') => `<input type="number" id="${id}" value="${v}" step="${step}" ${min !== '' ? `min="${min}"` : ''} ${max !== '' ? `max="${max}"` : ''}>`;
  return `${banner()}<div class="head"><div><h1>Settings</h1><p>Saved to settings.json in the data folder (outside the code folder, so updates keep them).</p></div></div>
  <form id="setf" class="settings"><div class="grid g2">
   <div class="panel"><h2>TWS connection</h2><div class="ctrls" style="margin-top:10px">
    <label class="f">Host<input type="text" id="s-host" value="${esc(c.host)}"></label><label class="f">Port (TWS 7497 paper / 7496 live; Gateway 4002 / 4001)${num('s-port', c.port, 1, 1, 65535)}</label>
    <label class="f">Client ID (tries 4 more if taken)${num('s-cid', c.client_id, 1, 0)}</label>
    <label class="f">Market data<select id="s-mdt">${['Live', 'Frozen', 'Delayed', 'DelayedFrozen'].map(x => `<option ${x === c.market_data_type ? 'selected' : ''}>${x}</option>`).join('')}</select></label>
    <label class="f">Data lines (GEX Live uses 60 of 100)${num('s-lines', l.max_lines, 1, 5, 100)}</label></div>
    <p class="muted" style="font-size:12px">The app connects read-only and has no order code.</p></div>
   <div class="panel"><h2>Earnings sources</h2><div class="ctrls" style="margin-top:10px">
    <label class="f">Finnhub API key (free at finnhub.io)<input type="password" id="s-fh" value="${esc(e.finnhub_api_key)}" autocomplete="off" placeholder="not set"></label>
    <label class="f">Days ahead${num('s-days', e.days_ahead, 1, 1, 21)}</label>
    <label class="f">Min market cap ($bn)${num('s-cap', e.min_market_cap_billions, 0.5, 0)}</label>
    <label class="f">Max per day${num('s-maxd', e.max_symbols_per_day, 1, 1)}</label></div>
    <label class="f" style="margin-top:10px">Earnings I'm following (only these when set; comma separated)<input type="text" id="s-watch" value="${esc((e.watchlist || []).join(', '))}"></label></div>
   <div class="panel"><h2>Screens</h2><div class="ctrls" style="margin-top:10px">
    <label class="f">Bid-ask pass ≤ %${num('s-bap', g.ba_pass_percent, 1, 100)}</label><label class="f">Review ≤ %${num('s-bar', g.ba_review_percent, 1, 100)}</label>
    <label class="f">Yes/No edge (IV pts)${num('s-edge', g.iv_edge_points, 0.5, 0)}</label>
    <label class="f">Skip long expiry if IV ≥ pts above next${num('s-skip', g.skip_back_iv_points, 0.5, 0)}</label>
    <label class="f">Screen entries for the next N sessions${num('s-ahead', g.screen_days_ahead, 1, 0, 5)}</label>
    <label class="f">Re-screen every (min, last hour)${num('s-every', g.screen_minutes, 1, 1)}</label>
    <label class="f">Forward test<select id="s-fwd"><option value="1" ${g.record_forward_test ? 'selected' : ''}>Record</option><option value="0" ${g.record_forward_test ? '' : 'selected'}>Off</option></select></label>
    <label class="f">Next-day checks (ET, comma separated)<input type="text" id="s-checks" value="${esc((g.exit_checks || []).join(', '))}"></label></div></div>
   <div class="panel"><h2>Is / Was</h2><div class="ctrls" style="margin-top:10px">
    <label class="f">Crush<select id="s-cm"><option value="mult" ${g.crush_mode === 'mult' ? 'selected' : ''}>Front IV × %</option><option value="retrace" ${g.crush_mode === 'retrace' ? 'selected' : ''}>Retrace premium</option></select></label>
    <label class="f">Crush %${num('s-crush', g.crush_percent, 5, 10, 100)}</label>
    <label class="f">Default strike<select id="s-strike"><option value="call_above" ${g.strike_rule !== 'nearest_otm' ? 'selected' : ''}>Nearest call at or above price (course trade)</option><option value="nearest_otm" ${g.strike_rule === 'nearest_otm' ? 'selected' : ''}>Nearest strike, OTM side (course spreadsheet)</option></select></label>
    <label class="f">Gap basis<select id="s-basis"><option value="avg" ${g.gap_basis === 'avg' ? 'selected' : ''}>Average of BEs</option><option value="near" ${g.gap_basis === 'near' ? 'selected' : ''}>Nearer BE</option></select></label>
    <label class="f">Past announcements${num('s-lb', g.lookback, 1, 3, 12)}</label>
    <label class="f">Strong ≥ hits${num('s-strong', g.strong_hits, 1, 1)}</label><label class="f">Acceptable ≥ hits${num('s-acc', g.acceptable_hits, 1, 1)}</label>
    <label class="f">Risk-free rate %${num('s-rate', (g.risk_free_rate * 100).toFixed(2), 0.25, 0)}</label>
    <label class="f">Budget per trade ($)${num('s-budget', g.budget, 50, 50)}</label></div></div>
  </div>
  <div style="display:flex;gap:10px;align-items:center;margin-top:14px"><button class="pri" type="submit">Save settings</button><span id="set-msg" class="${S.settingsOk ? 'muted' : 'err'}">${esc(S.settingsErr)}</span></div></form>`;
}

/* ---------- render ---------- */
let renderQueued = false;
function render() {
  nav();
  if (cur() === 'settings' && S.settingsDirty) return;   // live updates must not wipe unsaved edits
  const c = ST.connection;
  document.getElementById('clock').innerHTML = `${c.demo ? 'Replay clock' : 'Market clock'}<br><b>${esc(c.clock || '—')}</b><br><span class="conn"><span class="dot ${c.state === 'Connected' ? 'on' : c.state === 'Connecting' ? 'mid' : ''}"></span>TWS ${esc(c.state || '—')}</span>`;
  const M = document.getElementById('main');
  const active = document.activeElement && M.contains(document.activeElement) && ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName) ? document.activeElement.id : null;
  if (active && ['setf', 'ovf', 'fillf', 'closef', 'past-rows'].some(f => document.activeElement.closest('#' + f) || active === f)) return;   // don't wipe a form being typed in
  M.innerHTML = ({overview: vOverview, calendar: vCalendar, screener: vScreener, analyze: vAnalyze, risk: vRisk, entry: vEntry,
                  exit: vExit, playbooks: vPlaybooks, results: vResults, settings: vSettings}[cur()])();
  bind();
  if (active) {const n = document.getElementById(active); if (n) n.focus()}
}
function soon() {if (!renderQueued) {renderQueued = true; requestAnimationFrame(() => {renderQueued = false; render()})}}
const post = (url, body) => fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})}).then(r => r.json());
function saveTrades(rows) {ST.trades = rows; post('/api/trades', {trades: rows}).then(render)}

function bind() {
  const M = document.getElementById('main');
  M.querySelectorAll('[data-go]').forEach(b => b.onclick = () => go(b.dataset.go, b.dataset.tk));
  M.querySelectorAll('[data-pick]').forEach(b => {const f = () => {S.sel = b.dataset.pick; S.ovMsg = ''; save(); render()}; b.onclick = f; b.onkeydown = e => {if (e.key === 'Enter' || e.key === ' ') {e.preventDefault(); f()}}});
  M.querySelectorAll('[data-screen]').forEach(b => b.onclick = () => post('/api/screen' + (b.dataset.screen ? '?symbol=' + b.dataset.screen : '')).then(r => {if (!r.connected) alertMsg('TWS is not connected, so screens wait until it is.')}));
  M.querySelectorAll('[data-week]').forEach(b => b.onclick = () => {S.week = b.dataset.week; render()});
  const ts = M.querySelector('#tksel'); if (ts) ts.onchange = () => {S.sel = ts.value; save(); render()};
  const P = () => (S.P[sel().symbol] = S.P[sel().symbol] || {});
  M.querySelectorAll('[data-right]').forEach(x => x.onclick = e => {e.preventDefault(); P().right = x.dataset.right; render()});
  const k = M.querySelector('#k-sel'); if (k) k.onchange = () => {P().K = +k.value; render()};
  M.querySelectorAll('tr[data-strike]').forEach(x => x.onclick = () => {P().K = +x.dataset.strike; P().right = x.dataset.sright; render(); window.scrollTo({top: 0, behavior: 'smooth'})});
  const fs = M.querySelector('#f-sel'); if (fs) fs.onchange = () => {P().front = fs.value; if (P().back && P().back <= fs.value) delete P().back; render()};
  const b = M.querySelector('#b-sel'); if (b) b.onchange = () => {P().back = b.value; render()};
  const keep = (id, fn) => {const el = M.querySelector('#' + id); if (el) el.oninput = () => {fn(el.value); render(); const n = document.getElementById(id); if (n) n.focus()}};
  keep('crush', v => S.crush = +v); keep('bchg', v => S.backChg = +v); keep('tgt', v => S.target = +v);
  const ra = M.querySelector('#reset-an'); if (ra) ra.onclick = () => {resetControls(); delete S.P[sel().symbol]; render()};
  const bu = M.querySelector('#budget'); if (bu) bu.onchange = () => {S.budget = Math.max(50, +bu.value || 500); render()};
  const tsop = M.querySelector('#tstop'); if (tsop) tsop.onchange = () => {S.timeStop = tsop.value; render()};
  M.querySelectorAll('[data-cm]').forEach(x => x.onclick = e => {e.preventDefault(); S.crushMode = x.dataset.cm; if (S.crushMode === 'retrace' && S.crush < 50) S.crush = 50; render()});
  M.querySelectorAll('[data-basis]').forEach(x => x.onclick = () => {S.basis = x.dataset.basis; render()});
  M.querySelectorAll('[data-exit]').forEach(x => x.onclick = () => {S.exitMode = x.dataset.exit; render()});
  M.querySelectorAll('[data-exp]').forEach(x => x.onclick = () => {S.exp = x.dataset.exp; save(); render()});
  M.querySelectorAll('[data-ck]').forEach(x => x.onchange = () => {S.checks[x.dataset.ck] = x.checked; render()});
  M.querySelectorAll('[data-trade]').forEach(x => {const f = () => {S.exitSel = x.dataset.trade; render()}; x.onclick = f; x.onkeydown = e => {if (e.key === 'Enter' || e.key === ' ') {e.preventDefault(); f()}}});
  M.querySelectorAll('[data-rcheck]').forEach(x => x.onclick = () => {S.resCheck = x.dataset.rcheck; render()});
  M.querySelectorAll('[data-rside]').forEach(x => x.onclick = () => {S.resSide = x.dataset.rside; render()});
  const sim = M.querySelector('#simulate'); if (sim) sim.onclick = () => {
    S.ladder = {tk: sel().symbol, step: 0}; render(); let i = 0;
    const iv = setInterval(() => {i++; if (!S.ladder || i > 6) {clearInterval(iv); return} S.ladder.step = i; render()}, 600)};
  const rs = M.querySelector('#refresh-src'); if (rs) rs.onclick = () => post('/api/earnings/refresh');
  const ovf = M.querySelector('#ovf'); if (ovf) ovf.onsubmit = e => {e.preventDefault(); const t = byTk(S.sel) || ST.cands[0];
    post('/api/earnings/override', {symbol: t.symbol, date: M.querySelector('#ov-date').value, timing: M.querySelector('#ov-time').value, note: M.querySelector('#ov-note').value})
      .then(r => {S.ovMsg = r.error || 'Saved. The calendar reloads in a moment.'; document.activeElement.blur(); render()})};
  const ovc = M.querySelector('#ov-clear'); if (ovc) ovc.onclick = () => {const t = byTk(S.sel); post('/api/earnings/override', {symbol: t.symbol, date: ''}).then(() => {S.ovMsg = 'Removed.'; render()})};
  const ps = M.querySelector('#past-save'); if (ps) ps.onclick = () => {const t = byTk(S.sel) || ST.cands[0];
    const rows = M.querySelector('#past-rows').value.split(/\n+/).map(l => l.trim().split(/[\s,]+/)).filter(x => x[0]).map(x => ({date: x[0], timing: (x[1] || '').toUpperCase()}));
    post('/api/earnings/past', {symbol: t.symbol, rows}).then(() => {document.activeElement.blur(); S.ovMsg = 'Past dates saved; re-screening.'; render()})};
  const ff = M.querySelector('#fillf'); if (ff) ff.onsubmit = e => {e.preventDefault(); const t = sel(), a = A(t);
    const row = {id: Date.now().toString(36), symbol: t.symbol, K: a.K, right: a.right, front: a.front, back: a.back,
                 qty: Math.max(1, +M.querySelector('#fill-qty').value || 1), debit: +M.querySelector('#fill-px').value, entry: t.entry, exit: t.model};
    if (!(row.debit > 0)) return; document.activeElement.blur(); saveTrades([...(ST.trades || []), row]); go('exit')};
  const gq = M.querySelector('#getq'); if (gq) gq.onclick = () => {const x = ST.trades.find(z => z.id === S.exitSel); gq.disabled = true; gq.textContent = 'Asking TWS…';
    post('/api/quote', {legs: [{symbol: x.symbol, expiry: x.front, strike: x.K, right: x.right}, {symbol: x.symbol, expiry: x.back, strike: x.K, right: x.right}]})
      .then(r => {S.exitQuotes[x.id] = r.error ? {error: r.error} : {quotes: r.quotes, time: new Date().toLocaleTimeString()}; render()})};
  const cf = M.querySelector('#closef'); if (cf) cf.onsubmit = e => {e.preventDefault(); const v = +M.querySelector('#close-px').value; if (!(v >= 0) || M.querySelector('#close-px').value === '') return;
    document.activeElement.blur(); saveTrades(ST.trades.map(z => z.id === S.exitSel ? {...z, closed: true, close: v} : z))};
  const dt = M.querySelector('#del-trade'); if (dt) dt.onclick = () => saveTrades(ST.trades.filter(z => z.id !== S.exitSel));
  const sf = M.querySelector('#setf');
  if (sf) sf.oninput = sf.onchange = () => {const m = M.querySelector('#set-msg'); S.settingsErr = 'Unsaved changes'; S.settingsOk = false; S.settingsDirty = true; if (m) {m.textContent = S.settingsErr; m.className = 'err'}};
  if (sf) sf.onsubmit = e => {e.preventDefault(); const s = JSON.parse(JSON.stringify(ST.settings)); const v = id => M.querySelector('#' + id).value, n = id => +v(id);
    Object.assign(s.connection, {host: v('s-host'), port: n('s-port'), client_id: n('s-cid'), market_data_type: v('s-mdt')});
    s.load.max_lines = n('s-lines');
    Object.assign(s.earnings, {finnhub_api_key: v('s-fh'), days_ahead: n('s-days'), min_market_cap_billions: n('s-cap'), max_symbols_per_day: n('s-maxd'),
      watchlist: v('s-watch').split(/[\s,]+/).filter(Boolean)});
    Object.assign(s.strategy, {ba_pass_percent: n('s-bap'), ba_review_percent: n('s-bar'), iv_edge_points: n('s-edge'), skip_back_iv_points: n('s-skip'),
      screen_days_ahead: n('s-ahead'), screen_minutes: n('s-every'), record_forward_test: v('s-fwd') === '1',
      exit_checks: v('s-checks').split(/[\s,]+/).filter(Boolean).map(x => x.length === 4 ? '0' + x : x), crush_mode: v('s-cm'), crush_percent: n('s-crush'), gap_basis: v('s-basis'), strike_rule: v('s-strike'),
      lookback: n('s-lb'), strong_hits: n('s-strong'), acceptable_hits: n('s-acc'), risk_free_rate: n('s-rate') / 100, budget: n('s-budget')});
    post('/api/settings', s).then(r => {S.settingsErr = r.errors ? r.errors.join(' ') : 'Saved.'; S.settingsOk = !r.errors; if (!r.errors) S.settingsDirty = false; document.activeElement.blur(); resetControls(true); if (r.errors) {const m = M.querySelector('#set-msg'); if (m) {m.textContent = S.settingsErr; m.className = 'err'}} else render()})};
}
function alertMsg(t) {ST.messages.push({time: new Date().toLocaleTimeString(), level: 'warning', text: t}); render()}
function resetControls(force) {
  const s = strat();
  if (force || S.crush == null) {S.crush = s.crush_percent; S.crushMode = s.crush_mode; S.backChg = s.back_iv_change_percent; S.basis = s.gap_basis; S.budget = s.budget}
}

/* ---------- live connection ---------- */
function connect() {
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.type === 'state') {ST = m; resetControls(false)}
    else if (m.type === 'connection') ST.connection = m.connection;
    else if (m.type === 'cand') {const i = ST.cands.findIndex(c => c.symbol === m.cand.symbol); if (i >= 0) ST.cands[i] = m.cand; else ST.cands.push(m.cand)}
    else if (m.type === 'message') {if (m.update) {const i = ST.messages.findIndex(x => x.id === m.message.id); if (i >= 0) ST.messages[i] = m.message} else ST.messages.push(m.message)}
    else if (m.type === 'trades') ST.trades = m.trades;
    else if (m.type === 'journal') ST.journal = m.journal;
    if (m.type === 'connection' && cur() !== 'overview') {
      const c = ST.connection; document.getElementById('clock').innerHTML = `${c.demo ? 'Replay clock' : 'Market clock'}<br><b>${esc(c.clock || '—')}</b><br><span class="conn"><span class="dot ${c.state === 'Connected' ? 'on' : c.state === 'Connecting' ? 'mid' : ''}"></span>TWS ${esc(c.state || '—')}</span>`;
      return;
    }
    soon();
  };
  ws.onclose = () => {ST.connection = {...ST.connection, state: 'Disconnected', error: 'Lost the connection to the app; retrying.'}; soon(); setTimeout(connect, 2000)};
}
connect();
