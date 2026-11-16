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

type Meta = TickerMeta & { etf?: boolean };
const eq = (symbol: string, name: string, sector: TickerMeta['sector'], etf = false): Meta => ({
  symbol, name, sector, etf, assetClass: 'US Equity', currency: 'USD', optionsEligible: true, wealthsimpleNote: '',
});
const cr = (symbol: string, name: string): Meta => ({
  symbol, name, sector: 'Crypto', assetClass: 'Crypto', currency: 'USD', optionsEligible: false, wealthsimpleNote: '',
});
const WATCHLIST: Meta[] = [
  eq('SOXL', 'Semis 3X', 'Chips', true), eq('NVDA', 'NVIDIA', 'Chips'), eq('PLTR', 'Palantir', 'AI'),
  eq('IONQ', 'IonQ', 'Quantum'), eq('RGTI', 'Rigetti', 'Quantum'), eq('TQQQ', 'Nasdaq 3X', 'Index', true),
  cr('BTC-USD', 'Bitcoin'), cr('ETH-USD', 'Ethereum'), cr('SOL-USD', 'Solana'),
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
// Near-the-money contract ~30-50 days out: real bid/ask plus the straddle price (for implied vol).
async function optionQuote(meta: Meta, price: number, bear: boolean) {
  const iso = (d: number) => new Date(d).toISOString().slice(0, 10);
  const d = await nasdaq(`quote/${meta.symbol}/option-chain?assetclass=${meta.etf ? 'etf' : 'stocks'}&limit=60&fromdate=${iso(Date.now() + 30 * DAY)}&todate=${iso(Date.now() + 50 * DAY)}&excode=oprac&callput=callput&money=at&type=all`);
  const rows: any[] = d?.table?.rows ?? [];
  const group = rows.find((r) => r.expirygroup)?.expirygroup;
  const firstGroupRows = rows.slice(rows.findIndex((r) => r.expirygroup) + 1);
  const end = firstGroupRows.findIndex((r) => r.expirygroup);
  const strikes = (end < 0 ? firstGroupRows : firstGroupRows.slice(0, end)).filter((r) => r.strike);
  if (!group || !strikes.length) return null;
  const row = strikes.reduce((a, b) => (Math.abs(+b.strike - price) < Math.abs(+a.strike - price) ? b : a));
  const num = (v: string | null) => (v && !Number.isNaN(+v) ? +v : NaN);
  const cb = num(row.c_Bid), ca = num(row.c_Ask), pb = num(row.p_Bid), pa = num(row.p_Ask);
  const bid = bear ? pb : cb, ask = bear ? pa : ca;
  if (!(ask > 0)) return null;
  const dte = Math.max(1, Math.round((new Date(group).getTime() - Date.now()) / DAY));
  // ATM straddle ~= 0.8 * S * sigma * sqrt(T)  ->  implied vol estimate
  const straddle = (cb + ca + pb + pa) / 2;
  const iv = straddle > 0 ? straddle / (0.8 * price * Math.sqrt(dte / 365)) : NaN;
  return { expiry: group as string, dte, strike: +row.strike, bid, ask, spreadPct: bid > 0 ? ((ask - bid) / ((ask + bid) / 2)) * 100 : NaN, iv };
}
function realizedVol(bars: Bar[]) {
  const closes = sessions(bars).map((s) => s[s.length - 1].close).slice(-21);
  const rets = closes.slice(1).map((c, i) => Math.log(c / closes[i]));
  const m = rets.reduce((a, b) => a + b, 0) / rets.length;
  return Math.sqrt(rets.reduce((a, b) => a + (b - m) ** 2, 0) / (rets.length - 1)) * Math.sqrt(252);
}

async function send(html: string) {
  if (DRY || !TOKEN || !CHAT) return console.log(html.replace(/<[^>]+>/g, ''), '\n---');
  const r = await fetch(`https://api.telegram.org/bot${TOKEN}/sendMessage`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: CHAT, text: html, parse_mode: 'HTML', disable_web_page_preview: true }),
  });
  if (!r.ok) throw new Error(`Telegram ${r.status}: ${await r.text()}`);
}

const state: Record<string, number> = fs.existsSync(STATE) ? JSON.parse(fs.readFileSync(STATE, 'utf8')) : {};

// --- Paper book: every alert opens a simulated position on the underlying (puts = short underlying, as a proxy) ---
// shortcut: option alerts are tracked on the underlying, not the contract premium; add chain re-quotes if options P&L matters.
type Pos = { id: string; sym: string; kind: 'mongap' | 'signal'; crypto: boolean; dir: 1 | -1; entry: number; stop: number; target: number | null;
  qty: number; fx: number; costCad: number; openedAt: number; openedDate: string; label: string;
  filtered?: string }; // set = a raw signal the filters blocked; tracked silently to measure whether the filters help
type Closed = Pos & { exit: number; closedAt: number; pnlCad: number; reason: string };
// Every alert is recorded: no cash cap, so cash can go negative (it then shows capital over the START_CAD budget).
const book: { cashCad: number; open: Pos[]; closed: Closed[] } =
  fs.existsSync(BOOK) ? JSON.parse(fs.readFileSync(BOOK, 'utf8')) : { cashCad: START_CAD, open: [], closed: [] };
const pnlOf = (p: Pos, exit: number) =>
  p.dir * (exit - p.entry) * p.qty * p.fx - p.costCad * (FEE_PCT[p.crypto ? 'crypto' : 'stock'] / 100) * 2;
function openPaper(p: Omit<Pos, 'id' | 'qty' | 'fx' | 'costCad' | 'openedAt' | 'openedDate'>, fx: number): string {
  // one open trade per symbol per track (alerted / filtered): a repeat of the same signal is the same trade
  if (book.open.some((o) => o.sym === p.sym && !o.filtered === !p.filtered)) return p.filtered ? '' : `\n📝 Paper: already holding ${esc(p.sym)}`;
  book.open.push({ ...p, id: `${p.sym}-${Date.now()}`, qty: POSITION_CAD / fx / p.entry, fx, costCad: POSITION_CAD, openedAt: Date.now(), openedDate: nowEt.date });
  if (p.filtered) return '';
  book.cashCad -= POSITION_CAD;
  return `\n📝 Paper ${p.dir === 1 ? 'LONG' : 'SHORT'} $${POSITION_CAD} CAD @ <code>${p.entry.toFixed(2)}</code>`;
}
const money = (v: number) => `${v >= 0 ? '+' : '-'}$${Math.abs(v).toFixed(2)}`;
function bookLine() {
  const line = (xs: Closed[]) => `${money(xs.reduce((a, c) => a + c.pnlCad, 0))} · ${xs.length} closed (${xs.filter((c) => c.pnlCad > 0).length} won)`;
  const alerted = book.closed.filter((c) => !c.filtered), filtered = book.closed.filter((c) => c.filtered);
  return `Book: realized <b>${line(alerted)}</b> · ${book.open.filter((o) => !o.filtered).length} open · cash $${book.cashCad.toFixed(0)}` +
    `\nFiltered-out signals: ${line(filtered)} · ${book.open.filter((o) => o.filtered).length} open`;
}
// Check one open position against the bars since entry. Returns a Telegram line if it closed.
function manage(p: Pos, all: Bar[]): string | null {
  const after = all.filter((b) => b.time > p.openedAt), last = all[all.length - 1].close;
  let exit: number | null = null, reason = '';
  for (const b of after) { // stop checked before target in the same bar: conservative
    if (p.dir === 1 ? b.low <= p.stop : b.high >= p.stop) { exit = p.stop; reason = 'stop'; break; }
    if (p.target !== null && (p.dir === 1 ? b.high >= p.target : b.low <= p.target)) { exit = p.target; reason = 'target'; break; }
  }
  if (exit === null && p.kind === 'mongap') {
    const day = sessions(all).find((d) => et(d[0].time).date === p.openedDate);
    if (nowEt.date !== p.openedDate && day) { exit = day[day.length - 1].close; reason = 'Monday close'; }
    else if (nowEt.date === p.openedDate && nowEt.mins >= 945) { exit = last; reason = 'Monday close'; }
  }
  if (exit === null && p.kind === 'signal' && Date.now() - p.openedAt > MAX_HOLD_DAYS * DAY) { exit = last; reason = `${MAX_HOLD_DAYS}d time limit`; }
  if (exit === null) return null;
  const pnlCad = pnlOf(p, exit);
  book.open = book.open.filter((o) => o.id !== p.id);
  book.closed.push({ ...p, exit, closedAt: Date.now(), pnlCad, reason });
  if (p.filtered) return null; // silent: shows up in the daily recap
  book.cashCad += p.costCad + pnlCad;
  return `${pnlCad >= 0 ? '✅' : '🛑'} <b>Paper CLOSE ${esc(p.sym)}</b> (${esc(p.label)}) · ${reason}\n` +
    `${p.entry.toFixed(2)} → ${exit.toFixed(2)} · <b>${money(pnlCad)} CAD</b> (${((pnlCad / p.costCad) * 100).toFixed(1)}%)`;
}

if (process.argv.includes('--report')) {
  console.log(bookLine().replace(/<[^>]+>/g, ''));
  const tag = (p: Pos) => (p.filtered ? ` [filtered: ${p.filtered}]` : '');
  for (const c of book.closed) console.log(`closed ${c.sym.padEnd(8)} ${c.label.padEnd(16)} ${c.entry.toFixed(2)} -> ${c.exit.toFixed(2)}  ${c.pnlCad.toFixed(2)} CAD (${c.reason})${tag(c)}`);
  for (const o of book.open) console.log(`open   ${o.sym.padEnd(8)} ${o.label.padEnd(16)} ${o.dir === 1 ? 'long' : 'short'} @ ${o.entry.toFixed(2)} stop ${o.stop.toFixed(2)} target ${o.target?.toFixed(2) ?? '-'}${tag(o)}`);
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
    for (const h of held) { const line = manage(h, all); if (line) await send(line + '\n' + bookLine()); }
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
          `Idea: buy <b>shares</b> near <code>${px.toFixed(2)}</code> · stop below today's low <code>${low.toFixed(2)}</code> · exit at today's close (or Tue close)\n` +
          `Past 2y: +1.0% avg to close, 66% of Mondays up. A ~1% move doesn't cover an option's spread + theta, so shares, not options.` +
          openPaper({ sym: meta.symbol, kind: 'mongap', crypto: false, dir: 1, entry: px, stop: low, target: null, label: 'Monday gap' }, usdcad) +
          `\n<i>Alert only. Place manually in Wealthsimple.</i>`,
        );
        state[key] = now;
      }
    }

    // 2) Trend-aligned indicator signal.
    const c = all.slice(-180);
    const s = buildTechnicalSummary(meta, c, computeIndicatorSeries(c), stock ? 'TFSA' : 'Crypto');
    const st = s.strategy;
    if (!['Buy Call', 'Buy Put', 'Spot Accumulate'].includes(st.action)) continue;
    const bear = st.action === 'Buy Put';
    const record = (filtered: string) =>
      openPaper({ sym: meta.symbol, kind: 'signal', crypto: !stock, dir: bear ? -1 : 1, entry: s.price, stop: st.stopLossPrice, target: st.targetPrice, label: st.action, filtered }, usdcad);
    // Backtest: trend-aligned ~+0.08R vs ~+0.03R unfiltered; RSI guard avoids chasing exhaustion. SPY filter tested, didn't help.
    const closes = all.map((x) => x.close), slow = sma(closes, 350), fast = sma(closes, 100);
    if (st.confidence < MIN_CONF) { record(`confidence ${st.confidence}`); continue; }
    if (bear ? !(s.price < slow && fast < slow) : !(s.price > slow && fast > slow)) { record('against trend'); continue; }
    if (bear ? s.rsi < 30 : s.rsi > 70) { record(`RSI ${s.rsi.toFixed(0)}`); continue; }
    const key = `${meta.symbol}:${st.action}`;
    if (now - (state[key] ?? 0) < COOLDOWN_MS) continue;

    let optionLines = '';
    if (meta.optionsEligible) {
      if (!meta.etf) {
        const ed = await earningsDate(meta.symbol);
        const days = ed ? Math.ceil((ed.getTime() - now) / DAY) : null;
        if (days !== null && days >= 0 && days <= EARNINGS_DAYS) {
          console.log(`${meta.symbol}: skip ${st.action}, earnings in ${days}d (${ed!.toISOString().slice(0, 10)})`);
          record(`earnings in ${days}d`);
          continue; // IV crush + gap risk: a single-leg option through earnings is a coin flip you pay for
        }
        optionLines += `\nEarnings: ${ed ? `${ed.toISOString().slice(0, 10)} (${days}d)` : 'unknown, check before buying'}`;
      }
      const q = await optionQuote(meta, s.price, bear);
      if (q) {
        const costCad = q.ask * 100 * usdcad, rv = realizedVol(all), ratio = q.iv / rv;
        optionLines +=
          `\nContract: ${esc(q.expiry)} (${q.dte}d) ${q.strike} ${bear ? 'Put' : 'Call'} · bid/ask <code>${q.bid.toFixed(2)}/${q.ask.toFixed(2)}</code> (spread ${q.spreadPct.toFixed(0)}%)` +
          `\n1 contract ≈ <b>$${costCad.toFixed(0)} CAD</b> · IV ~${(q.iv * 100).toFixed(0)}% vs 20d realized ${(rv * 100).toFixed(0)}% → ` +
          (ratio > 1.3 ? 'options <b>expensive</b>: prefer a debit spread or shares' : ratio < 0.9 ? 'options <b>cheap</b>: single leg OK' : 'options fairly priced') +
          (costCad > MAX_PREMIUM_CAD ? `\n⚠️ Over your $${MAX_PREMIUM_CAD} CAD per-trade cap: use a debit spread (buy this strike, sell one ~10% further ${bear ? 'down' : 'up'}) or skip` : '') +
          (q.spreadPct > 10 ? '\n⚠️ Wide spread: use a limit order at mid, never market' : '');
      } else optionLines += '\nContract: no chain data, check strikes in the app';
    }

    await send(
      `🔔 <b>${esc(meta.symbol)}</b> · <code>${esc(st.action)}</code> · conf ${st.confidence}%\n` +
      `Price <code>${s.price.toFixed(2)}</code> · RSI <code>${s.rsi.toFixed(0)}</code> · trend ${bear ? 'down' : 'up'} (aligned)\n` +
      `${esc(st.strikeGuidance)}\nDelta ${esc(st.targetDelta)} · DTE ${esc(st.suggestedDTE)}\n` +
      `Stop <code>${st.stopLossPrice.toFixed(2)}</code> · Target <code>${st.targetPrice.toFixed(2)}</code> · R:R ${esc(st.riskRewardRatio)}` +
      optionLines + '\n' +
      st.rationale.slice(0, 3).map((x) => `• ${esc(x)}`).join('\n') +
      (meta.optionsEligible
        ? `\n<b>Exit plan</b>: sell at -40% premium or if price goes ${bear ? 'above' : 'below'} stop · take half off at +50% · close by ~21 DTE`
        : `\n<b>Risk plan</b>: spot only · size so a stop-out costs ≤1-2% of account · limit orders (spread ~1.5%)`) +
      record('') +
      `\n<i>Alert only. Place manually in Wealthsimple.</i>`,
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
