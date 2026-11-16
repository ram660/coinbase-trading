// Calendar + pattern scan on 2y hourly data. Reports mean % return, t-stat (|t|>2 = maybe real; expect some false hits when testing many).
const STOCKS = ['SPY', 'QQQ', 'NVDA', 'PLTR', 'SOXL', 'TQQQ', 'IONQ', 'RGTI'];
const CRYPTO = ['BTC-USD', 'ETH-USD', 'SOL-USD'];
type C = { t: number; o: number; h: number; l: number; c: number };
type D = { date: string; dow: number; o: number; h: number; l: number; c: number; firstC: number; bars: C[] };

async function get(sym: string): Promise<C[]> {
  const r = await fetch(`https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(sym)}?interval=60m&range=730d`, { headers: { 'User-Agent': 'Mozilla/5.0' } });
  const res = (await r.json()).chart?.result?.[0], q = res?.indicators?.quote?.[0], out: C[] = [];
  for (let i = 0; i < (res?.timestamp?.length ?? 0); i++)
    if ([q.open[i], q.high[i], q.low[i], q.close[i]].every((v) => typeof v === 'number')) out.push({ t: res.timestamp[i] * 1000, o: q.open[i], h: q.high[i], l: q.low[i], c: q.close[i] });
  return out;
}
const tz = (z: string) => new Intl.DateTimeFormat('en-CA', { timeZone: z, year: 'numeric', month: '2-digit', day: '2-digit' });
function days(bars: C[], zone: string): D[] {
  const f = tz(zone), m = new Map<string, C[]>();
  for (const b of bars) { const k = f.format(b.t); (m.get(k) ?? m.set(k, []).get(k)!).push(b); }
  return [...m].map(([date, bs]) => ({ date, dow: new Date(date + 'T12:00:00Z').getUTCDay(), o: bs[0].o, h: Math.max(...bs.map((b) => b.h)), l: Math.min(...bs.map((b) => b.l)), c: bs[bs.length - 1].c, firstC: bs[0].c, bars: bs }))
    .filter((d) => d.bars.length >= 5 || zone === 'UTC');
}
const rows: Record<string, number[]> = {};
const add = (k: string, v: number) => (rows[k] ??= []).push(v * 100);
const show = (title: string, keys: string[]) => {
  console.log('\n' + title);
  for (const k of keys) {
    const x = rows[k] ?? []; if (x.length < 20) continue;
    const m = x.reduce((a, b) => a + b, 0) / x.length, sd = Math.sqrt(x.reduce((a, b) => a + (b - m) ** 2, 0) / (x.length - 1));
    console.log(' ', k.padEnd(34), `n=${String(x.length).padStart(5)} mean=${m.toFixed(3).padStart(7)}%  win=${((x.filter((v) => v > 0).length / x.length) * 100).toFixed(0)}%  t=${(m / (sd / Math.sqrt(x.length))).toFixed(1)}`);
  }
};
const DN = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

for (const grp of [{ g: 'stk', syms: STOCKS, zone: 'America/New_York' }, { g: 'cry', syms: CRYPTO, zone: 'UTC' }]) {
  for (const sym of grp.syms) {
    const d = days(await get(sym), grp.zone);
    for (let i = 1; i < d.length; i++) {
      const p = d[i - 1], x = d[i], g = grp.g;
      add(`${g} dow ${DN[x.dow]} close-close`, x.c / p.c - 1);
      if (g === 'stk') {
        add(`${g} overnight (close->open)`, x.o / p.c - 1);
        add(`${g} intraday (open->close)`, x.c / x.o - 1);
        const gap = x.o / p.c - 1;
        if (Math.abs(gap) > 0.005) {
          const lab = x.dow === 1 ? 'Mon' : 'Tue-Fri', dir = gap > 0 ? 1 : -1;
          add(`${g} ${lab} gap-${gap > 0 ? 'up' : 'down'} >0.5%: day return in gap dir`, dir * (x.c / x.o - 1));
        }
        add(`${g} first-hour dir -> rest of day (in dir)`, Math.sign(x.firstC - x.o) * (x.c / x.firstC - 1));
        // RSI2-style dip buy: 2 down closes, above 100d avg of daily closes
        if (i >= 100 && d[i - 1].c < d[i - 2].c && d[i - 2].c < d[i - 3 < 0 ? 0 : i - 3].c) {
          const ma = d.slice(i - 100, i).reduce((a, b) => a + b.c, 0) / 100;
          if (p.c > ma) add(`${g} 2 down days in uptrend -> next day`, x.c / p.c - 1);
        }
        if (i >= 21 && p.c >= Math.max(...d.slice(i - 21, i - 1).map((v) => v.c))) add(`${g} 20d-high close -> next day`, x.c / p.c - 1);
      } else {
        for (const b of x.bars) add(`${g} hourly UTC ${String(new Date(b.t).getUTCHours()).padStart(2, '0')}h`, b.c / b.o - 1);
        add(`${g} ${[0, 6].includes(x.dow) ? 'weekend' : 'weekday'} day`, x.c / p.c - 1);
      }
    }
    // stock hourly ET by clock hour
    if (grp.g === 'stk') for (const x of d) for (const b of x.bars) add(`stk hourly ET ${new Date(b.t).toLocaleString('en-US', { timeZone: 'America/New_York', hour: '2-digit', hour12: false })}h`, b.c / b.o - 1);
  }
}
const ks = Object.keys(rows);
show('STOCK day of week (close-to-close)', ks.filter((k) => k.startsWith('stk dow')).sort());
show('STOCK overnight vs intraday', ks.filter((k) => /overnight|intraday/.test(k)));
show('STOCK hour of day (ET, hourly bar return)', ks.filter((k) => k.startsWith('stk hourly')).sort());
show('STOCK gaps + patterns', ks.filter((k) => /gap|first-hour|2 down|20d/.test(k)).sort());
show('CRYPTO day of week (UTC)', ks.filter((k) => k.startsWith('cry dow') || /weekend|weekday/.test(k)));
show('CRYPTO hour of day (UTC)', ks.filter((k) => k.startsWith('cry hourly')).sort());
