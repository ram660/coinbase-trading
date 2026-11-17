// Alert run for GitHub Actions: scan watchlist, send actionable signals to Telegram. Never trades.
// What's in here is what survived scripts/backtest.ts (2y hourly); see wealthsimple/README.md.
import fs from 'fs';
import { computeIndicatorSeries, buildTechnicalSummary } from '../src/lib/indicators.ts';
import type { TickerMeta } from '../src/types/market.ts';

const TOKEN = process.env.TELEGRAM_BOT_TOKEN?.trim();
const CHAT = process.env.TELEGRAM_CHAT_ID?.trim(); // channel: -100123... or @channelname
const MIN_CONF = Number(process.env.MIN_CONFIDENCE ?? 60);
const COOLDOWN_MS = Number(process.env.COOLDOWN_HOURS ?? 6) * 3600_000;
const MAX_PREMIUM_CAD = Number(process.env.MAX_PREMIUM_CAD ?? 350); // most you'd pay for one contract
const EARNINGS_DAYS = Number(process.env.EARNINGS_DAYS ?? 21); // no single-leg options if earnings fall inside the hold
const STATE = 'data/alerts-state.json';
const BOOK = 'data/paper-book.json';
const POSITION_CAD = Number(process.env.POSITION_CAD ?? 300); // paper size per trade
const START_CAD = Number(process.env.START_CAD ?? 2000);
const MAX_HOLD_DAYS = Number(process.env.MAX_HOLD_DAYS ?? 7); // signal trades: ~5 trading days, as in the backtest
// Wealthsimple: crypto spread ~1.5%/side; USD stocks 0 in a USD account (1.5% FX each way in a CAD account)
const FEE_PCT = { crypto: Number(process.env.CRYPTO_FEE_PCT ?? 1.5), stock: Number(process.env.STOCK_FEE_PCT ?? 0) };
const DRY = process.argv.includes('--dry'); // print instead of send

type Meta = TickerMeta & { etf?: boolean; group?: string }; // group: names that move together, one alert at a time
const eq = (symbol: string, name: string, sector: TickerMeta['sector'], etf = false): Meta => ({
  symbol, name, sector, etf, assetClass: 'US Equity', currency: 'USD', optionsEligible: true, wealthsimpleNote: '',
});
const cr = (symbol: string, name: string): Meta => ({
  symbol, name, sector: 'Crypto', assetClass: 'Crypto', currency: 'USD', optionsEligible: false, wealthsimpleNote: '',
});
const g = (group: string, ms: Meta[]) => ms.map((m) => ({ ...m, group }));
const WATCHLIST: Meta[] = [
  ...g('Nasdaq', [eq('SOXL', 'Semis 3X', 'Chips', true), eq('NVDA', 'NVIDIA', 'Chips'), eq('PLTR', 'Palantir', 'AI'), eq('TQQQ', 'Nasdaq 3X', 'Index', true)]),
  ...g('Quantum', [eq('IONQ', 'IonQ', 'Quantum'), eq('RGTI', 'Rigetti', 'Quantum')]),
  ...g('Crypto', [cr('BTC-USD', 'Bitcoin'), cr('ETH-USD', 'Ethereum'), cr('SOL-USD', 'Solana')]),
];

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const DAY = 86_400_000;

function et(t: number) {
  const p = Object.fromEntries(
    new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', weekday: 'short', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
      .formatToParts(t).map((x) => [x.type, x.value]),
  );
  return { date: `${p.year}-${p.month}-${p.day}`, dow: p.weekday, mins: (Number(p.hour) % 24) * 60 + Number(p.minute) };
}
const nowEt = et(Date.now());
// shortcut: ignores US market holidays; on a holiday stocks are just skipped for lack of fresh bars
const usOpen = !['Sat', 'Sun'].includes(nowEt.dow) && nowEt.mins >= 570 && nowEt.mins < 960;

type Bar = { time: number; open: number; high: number; low: number; close: number; volume: number };
async function candles(symbol: string): Promise<Bar[]> {
  const r = await fetch(
    `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?interval=60m&range=6mo`,
    { headers: { 'User-Agent': 'Mozilla/5.0 (compatible; NorthPulse/1.0)' }, signal: AbortSignal.timeout(15_000) },
  );
  if (!r.ok) throw new Error(`Yahoo ${r.status}`);
  const res = (await r.json()).chart?.result?.[0];
  const q = res?.indicators?.quote?.[0];
  const out: Bar[] = [];
  for (let i = 0; i < (res?.timestamp?.length ?? 0); i++) {
    if ([q.open[i], q.high[i], q.low[i], q.close[i]].some((v) => typeof v !== 'number')) continue;
    out.push({ time: res.timestamp[i] * 1000, open: q.open[i], high: q.high[i], low: q.low[i], close: q.close[i], volume: q.volume[i] ?? 0 });
  }
  return out;
}
const sma = (a: number[], n: number) => a.slice(-n).reduce((x, y) => x + y, 0) / Math.min(n, a.length);
function sessions(bars: Bar[]) {
  const out: Bar[][] = [];
  for (const b of bars) {
    if (!out.length || et(out[out.length - 1][0].time).date !== et(b.time).date) out.push([]);
    out[out.length - 1].push(b);
  }
  return out;
}

// Nasdaq's public JSON (Yahoo's quote/options endpoints now need a crumb). Failures return null, never throw.
async function nasdaq(path: string) {
  try {
    const r = await fetch(`https://api.nasdaq.com/api/${path}`, { headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', Accept: 'application/json' }, signal: AbortSignal.timeout(15_000) });
    return r.ok ? (await r.json()).data : null;
  } catch { return null; }
}
async function earningsDate(sym: string): Promise<Date | null> {
  const m = /(\d{2})\/(\d{2})\/(\d{4})/.exec((await nasdaq(`analyst/${sym}/earnings-date`))?.reportText ?? '');
  return m ? new Date(`${m[3]}-${m[1]}-${m[2]}T12:00:00Z`) : null;
}
type Spread = { expiry: string; dte: number; put: boolean; long: number; short: number | null; debit: number };
const iso = (d: number) => new Date(d).toISOString().slice(0, 10);
const mid = (bid: string, ask: string) => (+bid > 0 && +ask > 0 ? (+bid + +ask) / 2 : NaN);
async function chain(meta: Meta, from: number, to: number) {
  const d = await nasdaq(`quote/${meta.symbol}/option-chain?assetclass=${meta.etf ? 'etf' : 'stocks'}&limit=1000&fromdate=${iso(from)}&todate=${iso(to)}&excode=oprac&callput=callput&money=all&type=all`);
  const groups: { expiry: string; dte: number; rows: any[] }[] = [];
  for (const r of d?.table?.rows ?? []) {
    if (r.expirygroup) groups.push({ expiry: iso(new Date(r.expirygroup + ' 12:00 UTC').getTime()), dte: Math.round((new Date(r.expirygroup).getTime() - Date.now()) / DAY), rows: [] });
    else if (r.strike && groups.length) groups[groups.length - 1].rows.push(r);
  }
  return groups;
}
// Spread value per share at the mid: long leg minus short leg. NaN if a leg has no two-sided quote.
function spreadMid(rows: any[], sp: Pick<Spread, 'put' | 'long' | 'short'>) {
  const leg = (k: number) => { const r = rows.find((x) => +x.strike === k); return r ? (sp.put ? mid(r.p_Bid, r.p_Ask) : mid(r.c_Bid, r.c_Ask)) : NaN; };
  return leg(sp.long) - (sp.short === null ? 0 : leg(sp.short));
}
// 2-3 debit spreads to pick from: expiries just past the hold (theta is worst in the last days), buy the strike
// nearest the money, sell the strike nearest the target, or closer in until it fits the budget (maxDebit, USD/share).
// Max value = the width.
async function spreadChoices(meta: Meta, price: number, target: number, put: boolean, maxDebit: number): Promise<Spread[]> {
  const lo = MAX_HOLD_DAYS + 1;
  const groups = await chain(meta, Date.now() + lo * DAY, Date.now() + (lo + 10) * DAY);
  if (!groups.length) return [];
  const nearest = (t: number) => groups.reduce((a, x) => (Math.abs(x.dte - t) < Math.abs(a.dte - t) ? x : a));
  return [...new Set([lo, lo + 4, lo + 8].map(nearest))].flatMap((grp) => {
    const ks = grp.rows.map((r) => +r.strike).filter((k) => !Number.isNaN(k));
    if (!ks.length) return [];
    const by = (x: number) => (a: number, b: number) => Math.abs(a - x) - Math.abs(b - x);
    const long = [...ks].sort(by(price))[0];
    const shorts = ks.filter((k) => (put ? k < long : k > long)).sort(by(target)); // target first, then each strike after it
    const quoted = shorts.map((k) => ({ short: k as number | null, debit: spreadMid(grp.rows, { put, long, short: k }) })).filter((x) => x.debit > 0);
    if (!quoted.length) quoted.push({ short: null, debit: spreadMid(grp.rows, { put, long, short: null }) });
    const pick = quoted.find((x) => x.debit <= maxDebit && Math.abs(long - x.short!) >= price * 0.02) ?? quoted[0];
    return pick.debit > 0 ? [{ expiry: grp.expiry, dte: grp.dte, put, long, ...pick }] : [];
  });
}
async function quoteSpread(meta: Meta, sp: Spread) {
  const t = new Date(sp.expiry + 'T12:00:00Z').getTime();
  const grp = (await chain(meta, t, t))[0];
  return grp ? spreadMid(grp.rows, sp) : NaN;
}
const intrinsic = (sp: Spread, px: number) => {
  const v = (k: number) => Math.max(0, sp.put ? k - px : px - k);
  return v(sp.long) - (sp.short === null ? 0 : v(sp.short));
};
const short = (isoDate: string) => new Date(isoDate + 'T12:00:00Z').toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });

async function send(html: string) {
  if (DRY || !TOKEN || !CHAT) return console.log(html.replace(/<[^>]+>/g, ''), '\n---');
  const r = await fetch(`https://api.telegram.org/bot${TOKEN}/sendMessage`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: CHAT, text: html, parse_mode: 'HTML', disable_web_page_preview: true }),
  });
  if (!r.ok) throw new Error(`Telegram ${r.status}: ${await r.text()}`);
}

const state: Record<string, number> = fs.existsSync(STATE) ? JSON.parse(fs.readFileSync(STATE, 'utf8')) : {};

// --- Paper book: stock signals are tracked as the suggested debit spread (1 contract, re-quoted each run);
// crypto and Monday-gap trades on the underlying. Signals without a usable chain fall back to the underlying.
// shortcut: spreads are entered and marked at the mid; real fills are worse by part of the bid/ask.
type Pos = { id: string; sym: string; kind: 'mongap' | 'signal'; crypto: boolean; dir: 1 | -1; entry: number; stop: number; target: number | null;
  qty: number; fx: number; costCad: number; openedAt: number; openedDate: string; label: string;
  opt?: Spread; // set = entry/exit are the spread's price per share, qty 100 (one contract); stop/target stay on the stock
  filtered?: string }; // set = a raw signal the filters blocked; tracked silently to measure whether the filters help
type Closed = Pos & { exit: number; closedAt: number; pnlCad: number; reason: string };
// Every alert is recorded: no cash cap, so cash can go negative (it then shows capital over the START_CAD budget).
const book: { cashCad: number; open: Pos[]; closed: Closed[] } =
  fs.existsSync(BOOK) ? JSON.parse(fs.readFileSync(BOOK, 'utf8')) : { cashCad: START_CAD, open: [], closed: [] };
const pnlOf = (p: Pos, exit: number) =>
  p.dir * (exit - p.entry) * p.qty * p.fx - p.costCad * (FEE_PCT[p.crypto ? 'crypto' : 'stock'] / 100) * 2;
// one open trade per symbol per track (alerted / filtered): a repeat of the same signal is the same trade
const holding = (sym: string, filtered: string) => book.open.some((o) => o.sym === sym && !o.filtered === !filtered);
function openPaper(p: Omit<Pos, 'id' | 'qty' | 'fx' | 'costCad' | 'openedAt' | 'openedDate'>, fx: number): string {
  if (holding(p.sym, p.filtered ?? '')) return p.filtered ? '' : `\n📝 Paper: already holding ${esc(p.sym)}`;
  const size = p.opt ? { entry: p.opt.debit, qty: 100, costCad: p.opt.debit * 100 * fx } : { entry: p.entry, qty: POSITION_CAD / fx / p.entry, costCad: POSITION_CAD };
  book.open.push({ ...p, ...size, id: `${p.sym}-${Date.now()}`, fx, openedAt: Date.now(), openedDate: nowEt.date });
  if (p.filtered) return '';
  book.cashCad -= size.costCad;
  return `\n📝 Paper ${p.dir === 1 ? 'LONG' : 'SHORT'} $${POSITION_CAD} CAD @ <code>${p.entry.toFixed(2)}</code>`;
}
const spreadName = (sp: Spread) => `${short(sp.expiry)} $${sp.long}${sp.short === null ? '' : `/$${sp.short}`} ${sp.put ? 'Put' : 'Call'}${sp.short === null ? '' : ' spread'}`;
const money = (v: number) => `${v >= 0 ? '+' : '-'}$${Math.abs(v).toFixed(2)}`;
function bookLine() {
  const line = (xs: Closed[]) => `${money(xs.reduce((a, c) => a + c.pnlCad, 0))} · ${xs.length} closed (${xs.filter((c) => c.pnlCad > 0).length} won)`;
  const alerted = book.closed.filter((c) => !c.filtered), filtered = book.closed.filter((c) => c.filtered);
  return `Book: realized <b>${line(alerted)}</b> · ${book.open.filter((o) => !o.filtered).length} open · cash $${book.cashCad.toFixed(0)}` +
    `\nFiltered-out signals: ${line(filtered)} · ${book.open.filter((o) => o.filtered).length} open`;
}
// Spread exits: +50% / -50% on the spread, stock past its stop, sell-by date, or expiry (intrinsic).
async function exitSpread(p: Pos, meta: Meta, after: Bar[], last: number): Promise<[number, string] | null> {
  const sp = p.opt!;
  if (Date.now() > new Date(sp.expiry + 'T21:00:00Z').getTime()) return [intrinsic(sp, last), 'expiry'];
  const v = await quoteSpread(meta, sp);
  if (Number.isNaN(v)) return null; // no quote this run: try again next run
  if (v >= p.entry * 1.5) return [v, '+50% take profit'];
  if (v <= p.entry * 0.5) return [v, '-50% stop'];
  if (after.some((b) => (p.dir === 1 ? b.low <= p.stop : b.high >= p.stop))) return [v, 'stock hit stop'];
  if (Date.now() - p.openedAt > MAX_HOLD_DAYS * DAY) return [v, `${MAX_HOLD_DAYS}d sell-by`];
  return null;
}
// Check one open position against the bars since entry. Returns a Telegram line if it closed.
async function manage(p: Pos, meta: Meta, all: Bar[]): Promise<string | null> {
  const after = all.filter((b) => b.time > p.openedAt), last = all[all.length - 1].close;
  let exit: number | null = null, reason = '';
  if (p.opt) { const r = await exitSpread(p, meta, after, last); if (r) [exit, reason] = r; }
  else for (const b of after) { // stop checked before target in the same bar: conservative
    if (p.dir === 1 ? b.low <= p.stop : b.high >= p.stop) { exit = p.stop; reason = 'stop'; break; }
    if (p.target !== null && (p.dir === 1 ? b.high >= p.target : b.low <= p.target)) { exit = p.target; reason = 'target'; break; }
  }
  if (exit === null && p.kind === 'mongap') {
    const day = sessions(all).find((d) => et(d[0].time).date === p.openedDate);
    if (nowEt.date !== p.openedDate && day) { exit = day[day.length - 1].close; reason = 'Monday close'; }
    else if (nowEt.date === p.openedDate && nowEt.mins >= 945) { exit = last; reason = 'Monday close'; }
  }
  if (exit === null && p.kind === 'signal' && !p.opt && Date.now() - p.openedAt > MAX_HOLD_DAYS * DAY) { exit = last; reason = `${MAX_HOLD_DAYS}d time limit`; }
  if (exit === null) return null;
  const pnlCad = pnlOf(p, exit);
  book.open = book.open.filter((o) => o.id !== p.id);
  book.closed.push({ ...p, exit, closedAt: Date.now(), pnlCad, reason });
  if (p.filtered) return null; // silent: shows up in the daily recap
  book.cashCad += p.costCad + pnlCad;
  return `${pnlCad >= 0 ? '✅' : '🛑'} <b>Paper CLOSE ${esc(p.sym)}</b> (${p.opt ? spreadName(p.opt) : esc(p.label)}) · ${reason}\n` +
    `${p.entry.toFixed(2)} → ${exit.toFixed(2)} · <b>${money(pnlCad)} CAD</b> (${((pnlCad / p.costCad) * 100).toFixed(1)}%)`;
}

if (process.argv.includes('--report')) {
  console.log(bookLine().replace(/<[^>]+>/g, ''));
  const tag = (p: Pos) => (p.filtered ? ` [filtered: ${p.filtered}]` : '');
  for (const c of book.closed) console.log(`closed ${c.sym.padEnd(8)} ${c.label.padEnd(16)} ${c.entry.toFixed(2)} -> ${c.exit.toFixed(2)}  ${c.pnlCad.toFixed(2)} CAD (${c.reason})${tag(c)}`);
  for (const o of book.open) console.log(`open   ${o.sym.padEnd(8)} ${(o.opt ? spreadName(o.opt) : o.label).padEnd(16)} ${o.opt ? 'paid' : o.dir === 1 ? 'long' : 'short'} @ ${o.entry.toFixed(2)} stock stop ${o.stop.toFixed(2)} target ${o.target?.toFixed(2) ?? '-'}${tag(o)}`);
  process.exit(0);
}
const now = Date.now();
let usdcad = 1.4;
try { const fx = await candles('CAD=X'); usdcad = fx[fx.length - 1].close; } catch { /* keep fallback */ }

for (const meta of WATCHLIST) {
  const stock = meta.assetClass !== 'Crypto';
  const held = book.open.filter((p) => p.sym === meta.symbol);
  // stocks: only during the session, except a Monday trade from an earlier day gets settled at that day's close
  if (stock && !usOpen && !held.some((h) => h.kind === 'mongap' && h.openedDate !== nowEt.date)) continue;
  try {
    const all = await candles(meta.symbol);
    if (all.length < 350) continue;
    for (const h of held) { const line = await manage(h, meta, all); if (line) await send(line + '\n' + bookLine()); }
    if (stock && !usOpen) continue;

    // 1) Monday gap-down bounce (stocks). Backtest: gap down >0.5% at Monday open, buy after the first hour,
    //    +1.0% avg to Monday close, 66% win, 80 Mondays, positive in both years and on all 6 names.
    if (stock && nowEt.dow === 'Mon' && nowEt.mins >= 630 && nowEt.mins < 780) {
      const days = sessions(all), today = days[days.length - 1], prev = days[days.length - 2];
      const gap = today[0].open / prev[prev.length - 1].close - 1;
      const key = `${meta.symbol}:mongap:${nowEt.date}`;
      if (et(today[0].time).date === nowEt.date && today.length >= 2 && gap <= -0.005 && !state[key]) {
        const px = all[all.length - 1].close, low = Math.min(...today.map((b) => b.low));
        await send(
          `🌅 <b>${esc(meta.symbol)}</b> · Monday gap-down bounce\n` +
          `Gapped <code>${(gap * 100).toFixed(1)}%</code> vs Friday close · now <code>${px.toFixed(2)}</code>\n` +
          `Buy <b>shares</b> (not options) · stop <code>${low.toFixed(2)}</code> · sell at today's close\n` +
          `Past 2y: +1.0% avg, 66% of Mondays up`,
        );
        openPaper({ sym: meta.symbol, kind: 'mongap', crypto: false, dir: 1, entry: px, stop: low, target: null, label: 'Monday gap' }, usdcad);
        state[key] = now;
      }
    }

    // 2) Trend-aligned indicator signal.
    const c = all.slice(-180);
    const s = buildTechnicalSummary(meta, c, computeIndicatorSeries(c), stock ? 'TFSA' : 'Crypto');
    const st = s.strategy;
    if (!['Buy Call', 'Buy Put', 'Spot Accumulate'].includes(st.action)) continue;
    const bear = st.action === 'Buy Put';
    let picks: Spread[] | null = null; // fetched once, only when something will be recorded or sent
    const getPicks = async () => (picks ??= meta.optionsEligible ? await spreadChoices(meta, s.price, st.targetPrice, bear, MAX_PREMIUM_CAD / usdcad / 100) : []);
    const record = async (filtered: string) => {
      if (holding(meta.symbol, filtered)) return;
      const ps = await getPicks();
      openPaper({ sym: meta.symbol, kind: 'signal', crypto: !stock, dir: bear ? -1 : 1, entry: s.price, stop: st.stopLossPrice, target: st.targetPrice,
        label: st.action, opt: ps[1] ?? ps[0], filtered }, usdcad); // paper takes the middle expiry
    };
    // Backtest: trend-aligned ~+0.08R vs ~+0.03R unfiltered; RSI guard avoids chasing exhaustion. SPY filter tested, didn't help.
    const closes = all.map((x) => x.close), slow = sma(closes, 350), fast = sma(closes, 100);
    if (st.confidence < MIN_CONF) { await record(`confidence ${st.confidence}`); continue; }
    if (bear ? !(s.price < slow && fast < slow) : !(s.price > slow && fast > slow)) { await record('against trend'); continue; }
    if (bear ? s.rsi < 30 : s.rsi > 70) { await record(`RSI ${s.rsi.toFixed(0)}`); continue; }
    if (holding(meta.symbol, '')) continue; // same signal, same open trade: nothing new to alert or record
    // one alert per group: SOXL/TQQQ/NVDA/PLTR are one Nasdaq bet. shortcut: first in watchlist order wins, not the strongest.
    const twin = book.open.find((o) => !o.filtered && o.sym !== meta.symbol && WATCHLIST.find((m) => m.symbol === o.sym)?.group === meta.group);
    if (twin) { await record(`already in ${meta.group} via ${twin.sym}`); continue; }
    const key = `${meta.symbol}:${st.action}`;
    if (now - (state[key] ?? 0) < COOLDOWN_MS) continue;

    const sellBy = new Date(now + MAX_HOLD_DAYS * DAY).toLocaleDateString('en-CA', { month: 'short', day: 'numeric', timeZone: 'America/New_York' });
    let lines = '';
    if (meta.optionsEligible) {
      if (!meta.etf) {
        const ed = await earningsDate(meta.symbol);
        const days = ed ? Math.ceil((ed.getTime() - now) / DAY) : null;
        if (days !== null && days >= 0 && days <= EARNINGS_DAYS) {
          console.log(`${meta.symbol}: skip ${st.action}, earnings in ${days}d (${ed!.toISOString().slice(0, 10)})`);
          await record(`earnings in ${days}d`);
          continue; // IV crush + gap risk: a single-leg option through earnings is a coin flip you pay for
        }
      }
      const ps = await getPicks(), side = bear ? 'Put' : 'Call', cad = (v: number) => `$${(v * 100 * usdcad).toFixed(0)} CAD`;
      lines = ps.length
        ? ps.map((o, i) => `${i + 1}) ${short(o.expiry)} (${o.dte}d) · ` + (o.short === null
            ? `buy $${o.long} ${side} · limit $${o.debit.toFixed(2)} → <b>${cad(o.debit)}</b>`
            : `buy $${o.long} / sell $${o.short} ${side} · limit $${o.debit.toFixed(2)} → <b>${cad(o.debit)}</b>, max ${cad(Math.abs(o.long - o.short))}`) +
            (o.debit * 100 * usdcad > MAX_PREMIUM_CAD ? ' ⚠️ over budget' : '')).join('\n')
        : 'No chain data: pick a strike near the price, ~10-15 days out';
    }
    await record(''); // paper book tracks the middle choice; not shown in the alert
    await send(
      `🔔 <b>${esc(meta.symbol)}</b> · ${meta.optionsEligible ? `BUY ${bear ? 'PUT' : 'CALL'}` : 'BUY spot'} · now <code>${s.price.toFixed(2)}</code>\n` +
      (lines ? lines + '\n' : '') +
      (lines ? 'Sell at +50% / -50%, or if ' : 'Exit: ') + `stock ${bear ? 'above' : 'below'} <code>${st.stopLossPrice.toFixed(2)}</code> or ${bear ? 'below' : 'above'} <code>${st.targetPrice.toFixed(2)}</code>, else by ${sellBy}`,
    );
    state[key] = now;
  } catch (e) {
    console.error(meta.symbol, e instanceof Error ? e.message : e);
  }
}

// Daily recap once after the US close (16:05 ET)
if (nowEt.mins >= 965 && !state[`summary:${nowEt.date}`]) {
  const today = book.closed.filter((c) => et(c.closedAt).date === nowEt.date);
  await send(`📊 <b>Paper recap ${nowEt.date}</b>\n` +
    (today.length ? today.map((c) => `${c.pnlCad >= 0 ? '✅' : '🛑'} ${esc(c.sym)} ${esc(c.label)}${c.filtered ? ` (filtered: ${esc(c.filtered)})` : ''}: ${money(c.pnlCad)}`).join('\n') : 'No trades closed today') +
    `\nOpen: ${book.open.map((o) => esc(o.sym) + (o.filtered ? '*' : '')).join(', ') || 'none'} (* = filtered-out signal)\n` + bookLine());
  state[`summary:${nowEt.date}`] = now;
}

for (const [k, t] of Object.entries(state)) if (now - t > 24 * 3600_000) delete state[k];
if (DRY) process.exit(0);
fs.mkdirSync('data', { recursive: true });
fs.writeFileSync(STATE, JSON.stringify(state, null, 1));
fs.writeFileSync(BOOK, JSON.stringify(book, null, 1));
