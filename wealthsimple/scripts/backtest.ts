// Honest walk-forward test of the alert rule: signal at bar i uses only bars <= i, then simulate its own stop/target.
import { computeIndicatorSeries, buildTechnicalSummary } from '../src/lib/indicators.ts';
import type { TickerMeta } from '../src/types/market.ts';

const SYMS = (process.env.SYMS ?? 'SOXL,NVDA,PLTR,IONQ,RGTI,TQQQ,BTC-USD,ETH-USD,SOL-USD').split(',');
const H = 35; // hold limit in 1h bars (~5 trading days)
const STEP = 5;

async function candles(symbol: string) {
  const r = await fetch(`https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?interval=60m&range=730d`,
    { headers: { 'User-Agent': 'Mozilla/5.0 (compatible; NorthPulse/1.0)' } });
  const res = (await r.json()).chart?.result?.[0];
  const q = res?.indicators?.quote?.[0];
  const out: any[] = [];
  for (let i = 0; i < (res?.timestamp?.length ?? 0); i++) {
    if ([q.open[i], q.high[i], q.low[i], q.close[i]].some((v) => typeof v !== 'number')) continue;
    out.push({ time: res.timestamp[i] * 1000, open: q.open[i], high: q.high[i], low: q.low[i], close: q.close[i], volume: q.volume[i] ?? 0 });
  }
  return out;
}
const sma = (a: number[], n: number) => a.slice(-n).reduce((x, y) => x + y, 0) / Math.min(n, a.length);

type Row = { sym: string; dir: 1 | -1; r: number; rsi: number; rr: number; trendOk: boolean; adx: number; conf: number; mktOk?: boolean };
const rows: Row[] = [];
const base: number[] = [];
const alt: Row[] = []; // rule B: pullback inside an established trend, ATR-based stop/target
const mon: { gap: number; day: string; sym: string; sameDay: number; nextDay: number; trendOk: boolean }[] = []; // rule M: Monday gap-down bounce
const spy = await candles('SPY');
const spyCloses = spy.map((c) => c.close);
// SPY regime at time t: +1 above its ~50-day (350 x 1h) average, -1 below; uses only bars <= t
function spyRegime(t: number) {
  let lo = 0, hi = spy.length - 1;
  while (lo < hi) { const m = (lo + hi + 1) >> 1; if (spy[m].time <= t) lo = m; else hi = m - 1; }
  return lo < 350 ? 0 : spy[lo].close > sma(spyCloses.slice(0, lo + 1), 350) ? 1 : -1;
}
const etDay = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' });

for (const sym of SYMS) {
  const all = await candles(sym).catch(() => []);
  if (all.length < 600) { console.error('skip', sym, all.length); continue; }
  const crypto = sym.endsWith('-USD');
  const meta: TickerMeta = { symbol: sym, name: sym, assetClass: crypto ? 'Crypto' : 'US Equity', currency: 'USD', optionsEligible: !crypto, wealthsimpleNote: '' };
  for (let i = 400; i < all.length - H; i += STEP) {
    const w = all.slice(i - 179, i + 1);
    const s = buildTechnicalSummary(meta, w, computeIndicatorSeries(w), crypto ? 'Crypto' : 'TFSA');
    const a = s.strategy.action;
    const closesB = all.slice(0, i + 1).map((c) => c.close);
    const slowB = sma(closesB, 350), fastB = sma(closesB, 100), pxB = all[i].close;
    const upB = pxB > slowB && fastB > slowB, dnB = pxB < slowB && fastB < slowB;
    const dirB = upB && s.rsi < 45 ? 1 : dnB && s.rsi > 55 ? -1 : 0;
    if (dirB) {
      const risk = 1.5 * s.atr, stop = pxB - dirB * risk, tgt = pxB + dirB * 3 * s.atr;
      let exit = all[i + H].close;
      for (let j = i + 1; j <= i + H; j++) {
        const c = all[j];
        if (dirB === 1 ? c.low <= stop : c.high >= stop) { exit = stop; break; }
        if (dirB === 1 ? c.high >= tgt : c.low <= tgt) { exit = tgt; break; }
      }
      alt.push({ sym, dir: dirB as 1 | -1, r: (dirB * (exit - pxB)) / risk, rsi: s.rsi, rr: 2, trendOk: true, adx: s.adx, conf: 0 });
    }
    const dir = a === 'Buy Put' ? -1 : a === 'Buy Call' || a === 'Spot Accumulate' ? 1 : 0;
    base.push(all[i + H].close / all[i].close - 1);
    if (!dir) continue;
    const entry = all[i].close, stop = s.strategy.stopLossPrice, tgt = s.strategy.targetPrice;
    const risk = Math.abs(entry - stop);
    if (risk <= 0) continue;
    let exit = all[i + H].close;
    for (let j = i + 1; j <= i + H; j++) {
      const c = all[j];
      if (dir === 1 ? c.low <= stop : c.high >= stop) { exit = stop; break; } // stop checked first = conservative
      if (dir === 1 ? c.high >= tgt : c.low <= tgt) { exit = tgt; break; }
    }
    const closes = all.slice(0, i + 1).map((c) => c.close);
    const slow = sma(closes, 350), fast = sma(closes, 100); // ~50 / ~15 trading days on 1h bars; shortcut for daily 50/200
    const trendOk = dir === 1 ? entry > slow && fast > slow : entry < slow && fast < slow;
    const reg = crypto ? 0 : spyRegime(all[i].time);
    rows.push({ sym, dir: dir as 1 | -1, r: (dir * (exit - entry)) / risk, rsi: s.rsi, rr: Math.abs(tgt - entry) / risk, trendOk, adx: s.adx, conf: s.strategy.confidence, mktOk: crypto ? undefined : reg === dir });
  }
  if (crypto) continue;
  // Rule M: Monday open gaps down >0.5% vs Friday close; enter at close of first hour (10:30 ET bar close), exit Monday close / Tuesday close.
  const byDay: (typeof all)[] = [];
  for (const c of all) { const k = etDay.format(c.time); if (!byDay.length || etDay.format(byDay[byDay.length - 1][0].time) !== k) byDay.push([]); byDay[byDay.length - 1].push(c); }
  for (let d = 1; d < byDay.length - 1; d++) {
    const day = byDay[d], prev = byDay[d - 1];
    if (day.length < 5 || new Date(day[0].time).getUTCDay() !== 1) continue;
    const prevClose = prev[prev.length - 1].close;
    if (day[0].open / prevClose - 1 > -0.005) continue;
    const entry = day[0].close; // close of the first hourly bar
    const idx = all.indexOf(day[0]);
    const closes = all.slice(0, idx + 1).map((c) => c.close);
    mon.push({ gap: day[0].open / prevClose - 1, day: etDay.format(day[0].time), sym, sameDay: day[day.length - 1].close / entry - 1, nextDay: byDay[d + 1][byDay[d + 1].length - 1].close / entry - 1, trendOk: prevClose > sma(closes, 350) });
  }
}

const stat = (name: string, f: (r: Row) => boolean) => {
  const x = rows.filter(f);
  if (!x.length) return console.log(name.padEnd(34), 'n=0');
  const wins = x.filter((r) => r.r > 0).length, avg = x.reduce((a, r) => a + r.r, 0) / x.length;
  console.log(name.padEnd(34), `n=${String(x.length).padStart(4)}  win=${((wins / x.length) * 100).toFixed(0)}%  avgR=${avg.toFixed(2)}`);
};
console.log(`unconditional ${H}-bar return: avg ${(100 * base.reduce((a, b) => a + b, 0) / base.length).toFixed(2)}%  (n=${base.length})`);
stat('ALL signals (current rule)', () => true);
stat('  calls/accumulate only', (r) => r.dir === 1);
stat('  puts only', (r) => r.dir === -1);
stat('+ trend aligned', (r) => r.trendOk);
stat('+ not RSI-extreme vs direction', (r) => (r.dir === -1 ? r.rsi >= 30 : r.rsi <= 70));
stat('+ ADX >= 25', (r) => r.adx >= 25);
stat('+ R:R >= 1.5', (r) => r.rr >= 1.5);
stat('+ conf >= 70', (r) => r.conf >= 70);
stat('trend + RSI guard', (r) => r.trendOk && (r.dir === -1 ? r.rsi >= 30 : r.rsi <= 70));
stat('trend + RSI guard + ADX>=25', (r) => r.trendOk && r.adx >= 25 && (r.dir === -1 ? r.rsi >= 30 : r.rsi <= 70));

for (const [name, f] of [['B: pullback in trend (all)', () => true], ['B: longs', (r: Row) => r.dir === 1], ['B: shorts', (r: Row) => r.dir === -1], ['B: stocks only', (r: Row) => !r.sym.endsWith('-USD')], ['B: crypto only', (r: Row) => r.sym.endsWith('-USD')]] as const) {
  const x = alt.filter(f as any); if (!x.length) continue;
  console.log(name.padEnd(34), `n=${String(x.length).padStart(4)}  win=${((x.filter((r) => r.r > 0).length / x.length) * 100).toFixed(0)}%  avgR=${(x.reduce((a, r) => a + r.r, 0) / x.length).toFixed(2)}`);
}

stat('+ SPY regime agrees (stocks)', (r) => r.mktOk === true);
stat('+ SPY regime disagrees (stocks)', (r) => r.mktOk === false);
stat('trend + RSI + SPY agrees', (r) => r.mktOk === true && r.trendOk && (r.dir === -1 ? r.rsi >= 30 : r.rsi <= 70));
const ms = (name: string, x: number[]) => {
  const m = x.reduce((a, b) => a + b, 0) / x.length, sd = Math.sqrt(x.reduce((a, b) => a + (b - m) ** 2, 0) / (x.length - 1));
  console.log(name.padEnd(34), `n=${String(x.length).padStart(4)}  mean=${(m * 100).toFixed(2)}%  win=${((x.filter((v) => v > 0).length / x.length) * 100).toFixed(0)}%  t=${(m / (sd / Math.sqrt(x.length))).toFixed(1)}`);
};
ms('M: Mon gap-down, buy 10:30 -> Mon close', mon.map((m) => m.sameDay));
ms('M: ... -> Tue close', mon.map((m) => m.nextDay));
ms('M: in uptrend -> Mon close', mon.filter((m) => m.trendOk).map((m) => m.sameDay));
ms('M: in uptrend -> Tue close', mon.filter((m) => m.trendOk).map((m) => m.nextDay));
ms('M: downtrend -> Mon close', mon.filter((m) => !m.trendOk).map((m) => m.sameDay));
console.log('M: distinct Mondays (independent samples ~ this, not n):', new Set(mon.map((m) => m.day)).size);
const days = [...new Set(mon.map((m) => m.day))].sort(), mid = days[days.length >> 1];
ms('M: first half -> Mon close', mon.filter((m) => m.day < mid).map((m) => m.sameDay));
ms('M: second half -> Mon close', mon.filter((m) => m.day >= mid).map((m) => m.sameDay));
for (const sym of [...new Set(mon.map((m) => m.sym))]) ms(`M: ${sym} -> Mon close`, mon.filter((m) => m.sym === sym).map((m) => m.sameDay));
ms('M: gap 0.5-2%', mon.filter((m) => m.gap > -0.02).map((m) => m.sameDay));
ms('M: gap 2-4%', mon.filter((m) => m.gap <= -0.02 && m.gap > -0.04).map((m) => m.sameDay));
ms('M: gap >4%', mon.filter((m) => m.gap <= -0.04).map((m) => m.sameDay));
