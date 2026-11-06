"""Command line: research, paper books, intraday tools (setups, entry, plan, odds, ticket, alerts), live gates."""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from decimal import Decimal

from .brokers.paper import PaperBroker
from .config import Config, load_config
from .engine import Engine, run_backtest
from .exits import AtrTrailingStop
from .market_data import fetch_candles
from .risk import RiskManager
from .strategies import build_strategy


def _risk(cfg: Config) -> RiskManager:
    return RiskManager(cfg.risk, cfg.kill_switch_path, cfg.data_dir / f"risk_state_{cfg.mode}.json")


def _stop(cfg: Config) -> AtrTrailingStop | None:
    mult = float(cfg.exits.get("atr_mult", 0))
    return AtrTrailingStop(int(cfg.exits.get("atr_period", 14)), mult) if mult > 0 else None


def cmd_backtest(cfg: Config, args) -> int:
    settings = cfg.strategy
    if args.strategy and args.strategy != cfg.strategy["name"]:
        settings = {"name": args.strategy, "order_quote": cfg.strategy.get("order_quote", 50)}
    strategy = build_strategy(settings)
    stop = None if args.no_stop else _stop(cfg)
    products = args.product or cfg.products
    print(f"Fetching {args.bars} {cfg.granularity} candles for {', '.join(products)} ...")
    history = {p: fetch_candles(p, cfg.granularity, args.bars) for p in products}
    log = print if args.verbose else lambda _m: None
    engine = Engine(strategy, RiskManager(cfg.risk), PaperBroker(cfg.paper), log=log, stop=stop)
    result = run_backtest(engine, history)
    print(f"Strategy {strategy.name}, ATR stop {'off' if stop is None else f'{stop.mult}x'}")
    print(result.summary())
    return 0


def cmd_robustness(cfg: Config, args) -> int:
    from . import robustness

    products = args.product or cfg.products
    days = args.years * 365 + robustness.PREFIX_DAYS
    print(f"Fetching {days} daily candles for {', '.join(products)} ...")
    history = {p: fetch_candles(p, "ONE_DAY", days) for p in products}
    n = (args.years * 365 - args.window) // args.step + 1
    print(f"{n} windows of {args.window} days, every {args.step} days, ${cfg.paper.starting_cash} each, "
          f"ATR stop {'on' if not args.no_stop else 'off'}, drawdown limit {cfg.risk.max_drawdown_pct}%")
    print()
    print(robustness.hold_line(history, args.window, args.step))
    for row in robustness.run(cfg, history, args.window, args.step, use_stop=not args.no_stop):
        print(row.line())
    return 0


def cmd_scan(cfg: Config, args) -> int:
    from . import scanner

    print(f"Scanning Coinbase {args.quote} spot pairs with over {args.min_volume:,.0f} daily volume ...")
    rows = scanner.scan(args.quote, args.min_volume)
    if not rows:
        print("No coins matched.")
        return 1
    held = {h.upper() for h in (args.held or cfg.portfolio.get("holdings", []))}
    print(scanner.report(rows, held, args.top))
    print(f"\nFull table: {scanner.save_csv(rows, cfg.data_dir)}")
    return 0


def cmd_portfolio(cfg: Config, args) -> int:
    from . import portfolio

    quantities = {k.upper(): float(v) for k, v in cfg.portfolio.get("quantities", {}).items()}
    if not quantities:
        print("Add your coin amounts under [portfolio.quantities] in config/local.toml, e.g. HBAR = 7603.6")
        return 1
    print(portfolio.build(quantities, args.quote))
    return 0


def cmd_news(cfg: Config, args) -> int:
    from . import news

    coins = [c.upper() for c in (args.coin or [*cfg.portfolio.get("quantities", {}), "BTC"])]
    articles, failed = news.fetch_all(args.days)
    print(news.report(articles, failed, coins))
    return 0


def cmd_brief(cfg: Config, args) -> int:
    """Scanner, portfolio and news on one page, saved to data/briefs/YYYY-MM-DD.md. Read-only."""
    from datetime import datetime, timezone

    from . import news, portfolio, scanner

    quantities = {k.upper(): float(v) for k, v in cfg.portfolio.get("quantities", {}).items()}
    held = set(quantities) or {h.upper() for h in cfg.portfolio.get("holdings", [])}
    sections = []

    def section(title: str, build) -> None:
        try:
            body = build()
        except Exception as e:  # one failed source shouldn't sink the brief
            body = f"Unavailable: {e}"
        sections.append(f"## {title}\n\n```\n{body}\n```")

    def scan() -> str:
        rows = scanner.scan("USDC", 1_000_000)
        scanner.save_csv(rows, cfg.data_dir)
        return scanner.report(rows, held, 10)

    def headlines() -> str:
        articles, failed = news.fetch_all(1)
        return news.report(articles, failed, [*sorted(held), "BTC"], per_coin=3, market=8)

    def books() -> str:
        from .rotation_paper import BOOKS, PaperRotation
        out = []
        for b in BOOKS.values():
            r = PaperRotation(b, cfg.data_dir)
            if r.path.exists():
                out += [b.description, r.status(), ""]
        from .perps_paper import PaperPerps
        perps = PaperPerps(cfg.data_dir)
        if perps.path.exists():
            out += [perps.book.description, perps.status(), ""]
        return "\n".join(out).strip() or "No paper books running."

    section("Paper trading books (simulated money)", books)
    if quantities:
        section("Portfolio", lambda: portfolio.build(quantities))
    section("Market scan", scan)
    section("News, last 24 hours", headlines)

    now = datetime.now(timezone.utc)
    text = (f"# Daily brief {now:%Y-%m-%d}\n\nGenerated {now:%H:%M} UTC from public Coinbase data and news feeds. "
            "Descriptive only: no orders were placed and nothing here is advice.\n\n" + "\n\n".join(sections) + "\n")
    out = cfg.data_dir / "briefs" / f"brief_{now:%Y-%m-%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text if args.print else f"Brief saved to {out}")
    return 0


def cmd_lab(cfg: Config, args) -> int:
    from . import lab

    days = args.years * 365 + 210
    print(f"Loading {days} days of daily closes for liquid {args.quote} coins ...")
    closes = lab.load_universe(args.quote, days)
    configs = [lab.Rotation(top_n=n, lookback=lb, btc_filter=f) for n in (3, 5, 10) for lb in (30, 90) for f in (True, False)]
    print(lab.rolling(closes, configs, f"BTC-{args.quote}"))
    return 0


def cmd_rotate(cfg: Config, args) -> int:
    """Paper books: the spot momentum rotations and the perps trend book. Simulated money only."""
    from .perps_paper import PaperPerps
    from .rotation_paper import BOOKS, PaperRotation

    names = [*BOOKS, "perps"] if args.book == "all" else [args.book]
    for name in names:
        book = PaperPerps(cfg.data_dir) if name == "perps" else PaperRotation(BOOKS[name], cfg.data_dir)
        try:
            print(book.status() if args.status else book.run(force=args.force))
        except Exception as e:  # one book failing shouldn't stop the other
            print(f"[{name}] failed: {e}")
            if not args.status:
                book.log_error(e)
    return 0


def cmd_perps_backtest(cfg: Config, args) -> int:
    """Replay the perps trend book over years of daily spot closes. Read-only, public data."""
    from . import perps_paper

    days = args.years * 365 + perps_paper.BOOK.slow + 10
    print(f"Loading {days} days of daily closes for {', '.join(perps_paper.BOOK.assets)} ...")
    print(perps_paper.backtest_report(perps_paper.load_daily(days), funding_apr_pct=args.funding))
    return 0


def cmd_due(cfg: Config, args) -> int:
    """Print what this hour's workflow run should do, as key=value lines for $GITHUB_OUTPUT. Read-only."""
    from .rotation_paper import due

    d = due(cfg.data_dir, min_gap_minutes=args.min_gap)
    print(f"rotate={str(d['rotate'] or args.force).lower()}")
    print(f"brief={str(d['brief'] or args.brief).lower()}")
    print(f"minutes_since_last={d['minutes_since_last']}")
    return 0


def cmd_dashboard(cfg: Config, args) -> int:
    """Static HTML page of the paper books and workflow runs, from data/. Read-only."""
    from . import dashboard

    out = dashboard.write(cfg.data_dir, args.out, runs_path=args.runs, repo=args.repo)
    print(f"Dashboard saved to {out}")
    return 0


# ---- intraday tools (crypto adaptation of the day-trader skills). Read-only; nothing here places an order.

def cmd_setups(cfg: Config, args) -> int:
    from . import scanner
    from .daytrade import setups

    products = args.product or [p["product_id"] for p in scanner.list_products(args.quote, args.min_volume)]
    wanted = tuple(args.setup) if args.setup else setups.SETUPS
    if not args.quiet:
        print(f"Checking {len(products)} {args.quote} pairs for {', '.join(wanted)} on 5-minute candles ...")
    hits = setups.scan(products, wanted, fresh=args.fresh, range_minutes=args.range_minutes)
    if args.quiet:
        from .daytrade import quiet

        new, first = quiet.gate(cfg.data_dir, [setups.key(h) for h in hits])
        if not first:
            hits = [h for h in hits if setups.key(h) in new]
            if not hits:
                return 0  # nothing new since the last report today: stay silent
    print(setups.report(hits, len(products)))
    return 0


def cmd_entry(cfg: Config, args) -> int:
    from .daytrade import setups
    from .daytrade.common import bars

    while True:
        hit = setups.check(args.product, bars(args.product, 1.25), args.setup, args.fresh, args.range_minutes)
        print(time.strftime("%H:%M:%S UTC ", time.gmtime()) + setups.describe(hit))
        if not args.watch or hit.status in ("triggered", "expired"):
            return 0
        time.sleep(args.every)


def _plan_from_args(cfg: Config, args):
    from .daytrade import plan as tp
    from .daytrade.common import product_info
    from .indicators import atr

    info = product_info(args.product)
    entry = float(args.entry) if args.entry else float(info["price"])
    needs_atr = any(r.lower().startswith("atr:") for r in (args.stop, args.target))
    a = atr(fetch_candles(args.product, "ONE_HOUR", 60), 14) if needs_atr else None
    return tp.make_plan(args.product, args.side, entry, args.stop, args.target, args.budget, args.max_loss,
                        args.fee, args.hours or 24, a, info.get("price_increment") or info.get("quote_increment"),
                        info.get("quote_increment"))


def cmd_plan(cfg: Config, args) -> int:
    from .daytrade import plan as tp

    p = _plan_from_args(cfg, args)
    print(tp.describe(p))
    print(f"Saved to {tp.save(p, cfg.data_dir)}. Next: `cbagent odds`, then `cbagent ticket`.")
    return 0


def cmd_odds(cfg: Config, args) -> int:
    from .daytrade import plan as tp
    from .daytrade import timemachine as tm
    from .daytrade.common import bars, folder, write_json

    if args.product:
        p = _plan_from_args(cfg, args)
    else:
        p = tp.load(cfg.data_dir)
        if not p:
            print("No plan yet: run `cbagent plan PRODUCT ...` or pass a product here.")
            return 1
    plan = tm.Plan(p.product_id, p.side, p.entry, p.stop, p.target, start=p.created, hours=args.hours or p.hours)
    print(f"Loading {args.days} days of 5-minute candles for {p.product_id} ...")
    res = tm.run(plan, bars(p.product_id, args.days), runs=args.runs, seed=args.seed, fee_pct=p.fee_pct)
    print(tm.report(res))
    write_json(folder(cfg.data_dir) / "odds.json", res.to_dict())
    return 0


def cmd_ticket(cfg: Config, args) -> int:
    from .daytrade import plan as tp

    p = tp.load(cfg.data_dir)
    if not p:
        print("No plan yet: run `cbagent plan PRODUCT ...` first.")
        return 1
    if args.reply is not None:
        ok, why = tp.approve(p, args.reply, cfg.data_dir)
        print(("Recorded your yes for ticket " if ok else "Not recorded for ticket ") + f"{p.fingerprint()}: {why}.")
        print("Nothing was sent. Placing it is a separate step through the Coinbase connector." if ok else "Ask again.")
        return 0 if ok else 1
    print(tp.ticket(p, open_position=args.open_position))
    if tp.approved(p, cfg.data_dir):
        print("Status: your yes is recorded for this exact ticket.")
    return 0


def cmd_alerts(cfg: Config, args) -> int:
    from .daytrade import alerts
    from .daytrade import plan as tp

    if args.action == "add":
        if args.from_plan:
            p = tp.load(cfg.data_dir)
            if not p:
                print("No plan to take levels from.")
                return 1
            stop_dir, target_dir = ("below", "above") if p.side == "long" else ("above", "below")
            for d, price, note in ((stop_dir, p.stop, "stop"), (target_dir, p.target, "target")):
                a = alerts.add(cfg.data_dir, p.product_id, d, price, note)
                print(f"Added [{a['id']}] {a['product_id']} {d} {price:g} ({note})")
            return 0
        if not (args.product and args.direction and args.price):
            print("Usage: cbagent alerts add PRODUCT above|below PRICE [--note TEXT], or --from-plan")
            return 1
        a = alerts.add(cfg.data_dir, args.product, args.direction, args.price, args.note or "")
        print(f"Added [{a['id']}] {a['product_id']} {a['direction']} {a['price']:g}")
    elif args.action == "check":
        rang = alerts.check(cfg.data_dir)
        if args.quiet:
            from .daytrade import quiet

            new, _ = quiet.gate(cfg.data_dir, ["alert|" + line for line in rang])
            rang = [k.removeprefix("alert|") for k in new]  # an unavailable price is reported once a day, not every run
        if rang or not args.quiet:
            print("\n".join(rang) if rang else "Nothing rang.")
    elif args.action == "clear":
        print(f"Removed {alerts.remove(cfg.data_dir, args.id or None)} alerts.")
    else:
        print(alerts.listing(cfg.data_dir))
    return 0


def _live_loop(cfg: Config, engine: Engine, once: bool) -> int:
    strategy = engine.strategy
    while True:
        now = int(time.time())
        try:
            candles = {p: fetch_candles(p, cfg.granularity, strategy.warmup + 5) for p in cfg.products}
            engine.tick(candles, now)  # handles the kill switch, including selling to cash after a drawdown trip
        except Exception as e:  # a network blip should not kill the loop
            print(f"tick failed: {e}", file=sys.stderr)
            engine.journal("error", message=str(e))
        state = engine.risk.state
        waiting_for_cooloff = state.auto_tripped and cfg.risk.cooloff_days > 0
        if engine.risk.kill_switch_active() and not state.flatten_pending and not waiting_for_cooloff:
            print(f"Kill switch is on ({cfg.kill_switch_path}). Stopping. Run `cbagent unkill` after reviewing.")
            return 2
        if once:
            return 0
        time.sleep(cfg.poll_seconds)


def cmd_paper(cfg: Config, args) -> int:
    paper_cfg = replace(cfg, mode="paper")
    broker = PaperBroker(cfg.paper, cfg.data_dir / "paper_state.json")
    engine = Engine(build_strategy(cfg.strategy), _risk(paper_cfg), broker, cfg.data_dir / "journal_paper.jsonl", stop=_stop(cfg))
    print(f"PAPER trading {', '.join(cfg.products)} every {cfg.poll_seconds}s. No real orders. Ctrl+C to stop.")
    return _live_loop(paper_cfg, engine, args.once)


def cmd_live(cfg: Config, args) -> int:
    from .brokers.coinbase_live import CoinbaseLiveBroker, LiveTradingNotAllowed

    try:
        broker = CoinbaseLiveBroker(cfg, live_flag=args.live)
    except LiveTradingNotAllowed as e:
        print(f"Live trading refused: {e}")
        return 1
    engine = Engine(build_strategy(cfg.strategy), _risk(cfg), broker, cfg.data_dir / "journal_live.jsonl", stop=_stop(cfg))
    print(f"LIVE trading {', '.join(cfg.products)} with REAL money. Max order {cfg.risk.max_order_quote}. Ctrl+C to stop.")
    return _live_loop(cfg, engine, args.once)


def cmd_live_check(cfg: Config, args) -> int:
    """Read-only: checks the key, balances and products, and says what the strategy would do now. Places nothing."""
    from .brokers.coinbase_live import LiveTradingNotAllowed, key_permissions, make_client

    try:
        client = make_client(cfg)
        perms = key_permissions(client)
    except LiveTradingNotAllowed as e:
        print(f"Not ready: {e}")
        return 1
    ok = True
    print(f"Key: view={perms.get('can_view')} trade={perms.get('can_trade')} transfer={perms.get('can_transfer')} "
          f"portfolio={perms.get('portfolio_type')}")
    if perms.get("can_transfer"):
        print("  NOT OK: this key can move funds out. Delete it and create one with View and Trade only.")
        ok = False
    if not perms.get("can_trade"):
        print("  NOT OK: this key cannot trade.")
        ok = False

    accounts = client.get_accounts(limit=250).to_dict().get("accounts", [])
    balances = {a["currency"]: Decimal(a["available_balance"]["value"]) for a in accounts}
    quote = cfg.products[0].split("-")[1]
    allocation = Decimal(str(cfg.live.get("allocation_quote", 0)))
    print(f"{quote} available: {balances.get(quote, Decimal('0')):.2f}   bot allocation: {allocation:.2f}")
    if balances.get(quote, Decimal("0")) < allocation:
        print(f"  Note: less {quote} than the allocation; the bot will only use what is there.")

    strategy = build_strategy(cfg.strategy)
    for p in cfg.products:
        info = client.get_product(p).to_dict()
        print(f"{p}: price {info.get('price')}  min order {info.get('quote_min_size')} {quote}  "
              f"trading {'disabled' if info.get('trading_disabled') else 'enabled'}")
        candles = fetch_candles(p, cfg.granularity, strategy.warmup + 5)
        signal = strategy.on_candles(p, candles, Decimal("0"))
        print(f"  {strategy.name} would {signal.action} now if flat: {signal.reason}")
    print("Ready for `cbagent live --live`." if ok and cfg.mode == "live" else "Not ready yet (see above, and mode must be live).")
    return 0 if ok else 1


def cmd_status(cfg: Config, args) -> int:
    print(f"Mode in config: {cfg.mode}")
    print(f"Kill switch:    {'ON' if cfg.kill_switch_path.exists() else 'off'}")
    state = cfg.data_dir / "paper_state.json"
    if state.exists():
        data = json.loads(state.read_text(encoding="utf-8"))
        print(f"Paper cash:     {Decimal(data['cash']):.2f}")
        for p, v in data["positions"].items():
            if Decimal(v["base"]):
                print(f"  {p}: {v['base']} (cost {Decimal(v['cost']):.2f})")
    return 0


def cmd_kill(cfg: Config, args) -> int:
    _risk(cfg).trip_kill_switch(args.reason or "manual kill switch")
    print(f"Kill switch ON: {cfg.kill_switch_path}")
    return 0


def cmd_unkill(cfg: Config, args) -> int:
    for mode in ("paper", "live"):
        RiskManager(cfg.risk, cfg.kill_switch_path, cfg.data_dir / f"risk_state_{mode}.json").reset_kill_switch()
    print("Kill switch off.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cbagent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    bt = sub.add_parser("backtest", help="replay recent public candles through the strategy")
    bt.add_argument("--product", action="append", help="override products (repeatable)")
    bt.add_argument("--bars", type=int, default=1000)
    bt.add_argument("--strategy", help="try another registered strategy with its default parameters")
    bt.add_argument("--no-stop", action="store_true", help="turn the ATR trailing stop off")
    bt.add_argument("-v", "--verbose", action="store_true")
    bt.set_defaults(fn=cmd_backtest)

    rb = sub.add_parser("robustness", help="test every candidate strategy over rolling windows of daily data")
    rb.add_argument("--product", action="append")
    rb.add_argument("--years", type=int, default=6)
    rb.add_argument("--window", type=int, default=365, help="window length in days")
    rb.add_argument("--step", type=int, default=90, help="days between window starts")
    rb.add_argument("--no-stop", action="store_true")
    rb.set_defaults(fn=cmd_robustness)

    sc = sub.add_parser("scan", help="measure every liquid Coinbase spot pair (read-only)")
    sc.add_argument("--quote", default="USDC")
    sc.add_argument("--min-volume", type=float, default=1_000_000, help="minimum 24h volume in quote currency")
    sc.add_argument("--top", type=int, default=15)
    sc.add_argument("--held", action="append", help="coins you hold, e.g. --held HBAR (default: [portfolio] holdings)")
    sc.set_defaults(fn=cmd_scan)

    pf = sub.add_parser("portfolio", help="risk and concentration report for [portfolio.quantities] (read-only)")
    pf.add_argument("--quote", default="USDC")
    pf.set_defaults(fn=cmd_portfolio)

    nw = sub.add_parser("news", help="recent crypto headlines matched to your coins (read-only)")
    nw.add_argument("--coin", action="append", help="coins to match (default: your holdings plus BTC)")
    nw.add_argument("--days", type=int, default=3)
    nw.set_defaults(fn=cmd_news)

    br = sub.add_parser("brief", help="portfolio, market scan and news on one page in data/briefs (read-only)")
    br.add_argument("--print", action="store_true", help="also print the brief")
    br.set_defaults(fn=cmd_brief)

    lb = sub.add_parser("lab", help="test cross-coin rotation ideas over rolling windows (read-only)")
    lb.add_argument("--quote", default="USDC")
    lb.add_argument("--years", type=int, default=4)
    lb.set_defaults(fn=cmd_lab)

    ro = sub.add_parser("rotate", help="run the paper books: spot rotations and perps trend (simulated money)")
    ro.add_argument("--book", default="all", choices=["all", "daily", "fast", "perps"])
    ro.add_argument("--status", action="store_true", help="show equity and holdings without trading")
    ro.add_argument("--force", action="store_true", help="rebalance now even if not due")
    ro.set_defaults(fn=cmd_rotate)

    pb = sub.add_parser("perps-backtest", help="replay the perps trend book over daily history (read-only)")
    pb.add_argument("--years", type=int, default=4)
    pb.add_argument("--funding", type=float, default=10.0, help="funding longs pay a year, in percent")
    pb.set_defaults(fn=cmd_perps_backtest)

    du = sub.add_parser("due", help="whether the books and the brief are due this hour (for the workflow)")
    du.add_argument("--min-gap", type=int, default=12, help="minutes since the last run before the books run again")
    du.add_argument("--force", action="store_true", help="say the books are due regardless")
    du.add_argument("--brief", action="store_true", help="say the brief is due regardless")
    du.set_defaults(fn=cmd_due)

    db = sub.add_parser("dashboard", help="HTML page of the paper books, trades and workflow runs (read-only)")
    db.add_argument("--out", default=None, help="output file (default data/dashboard/index.html)")
    db.add_argument("--runs", default=None, help="GitHub Actions runs JSON (default data/workflow_runs.json)")
    db.add_argument("--repo", default="ram660/coinbase-trading", help="owner/name, for links")
    db.set_defaults(fn=cmd_dashboard)

    def plan_args(sp, product_required: bool) -> None:
        sp.add_argument("product", nargs=None if product_required else "?", help="e.g. QNT-USDC")
        sp.add_argument("--side", default="long", choices=["long", "short"])
        sp.add_argument("--entry", help="entry price (default: the live price)")
        sp.add_argument("--stop", default="loss:5", help="loss:5, pct:3, atr:2 or price:X (default loss:5)")
        sp.add_argument("--target", default="gain:5", help="gain:5, R:1.5, pct:4, atr:3 or price:X (default gain:5)")
        sp.add_argument("--budget", type=float, default=75, help="quote to spend (default 75)")
        sp.add_argument("--max-loss", type=float, help="cut the spend so the loss at the stop stays under this")
        sp.add_argument("--fee", type=float, default=0.5, help="fee %% each way (default 0.5)")

    st = sub.add_parser("setups", help="intraday setups on 5-minute candles across liquid coins (read-only)")
    st.add_argument("--product", action="append", help="only these products (default: every liquid pair)")
    st.add_argument("--quote", default="USDC")
    st.add_argument("--min-volume", type=float, default=5_000_000)
    st.add_argument("--setup", action="append", choices=["range_break", "break_24h", "volume_spike", "rsi_bounce", "rsi_fade"])
    st.add_argument("--fresh", type=int, default=2, help="how many latest candles count as now")
    st.add_argument("--range-minutes", type=int, default=60, help="UTC-day opening range length")
    st.add_argument("--quiet", action="store_true",
                    help="for schedules: full report on the first run of the UTC day, then only new hits, else silent")
    st.set_defaults(fn=cmd_setups)

    en = sub.add_parser("entry", help="is a setup waiting, triggered or expired on one coin (read-only)")
    en.add_argument("product")
    en.add_argument("--setup", default="range_break", choices=["range_break", "break_24h", "volume_spike", "rsi_bounce", "rsi_fade"])
    en.add_argument("--watch", action="store_true", help="keep checking until it triggers or expires")
    en.add_argument("--every", type=int, default=60, help="seconds between checks with --watch")
    en.add_argument("--fresh", type=int, default=2)
    en.add_argument("--range-minutes", type=int, default=60)
    en.set_defaults(fn=cmd_entry)

    pl = sub.add_parser("plan", help="stop, target and size for one trade, saved to data/daytrade/plan.json")
    plan_args(pl, True)
    pl.add_argument("--hours", type=float, default=24, help="how long the trade may stay open")
    pl.set_defaults(fn=cmd_plan)

    od = sub.add_parser("odds", help="Time Machine: replay the plan over the coin's own 5-minute candles (read-only)")
    plan_args(od, False)
    od.add_argument("--hours", type=float, default=None, help="how long the trade may stay open (default: the plan's, 24)")
    od.add_argument("--days", type=float, default=30, help="lookback of 5-minute candles")
    od.add_argument("--runs", type=int, default=10_000)
    od.add_argument("--seed", type=int, default=7)
    od.set_defaults(fn=cmd_odds)

    tk = sub.add_parser("ticket", help="the plan as an order ticket; --reply records Ram's yes (never sends)")
    tk.add_argument("--reply", help="Ram's reply, word for word")
    tk.add_argument("--open-position", action="store_true", help="a live position is already open")
    tk.set_defaults(fn=cmd_ticket)

    al = sub.add_parser("alerts", help="price alerts: list, add, check, clear")
    al.add_argument("action", nargs="?", default="list", choices=["list", "add", "check", "clear"])
    al.add_argument("product", nargs="?")
    al.add_argument("direction", nargs="?", choices=["above", "below"])
    al.add_argument("price", nargs="?", type=float)
    al.add_argument("--note")
    al.add_argument("--from-plan", action="store_true", help="add the plan's stop and target")
    al.add_argument("--quiet", action="store_true", help="with check: print only what rang, each line once a UTC day")
    al.add_argument("--id", type=int, action="append", help="with clear: remove these (default: the ones that rang)")
    al.set_defaults(fn=cmd_alerts)

    pp = sub.add_parser("paper", help="paper trade against live prices (default, safe)")
    pp.add_argument("--once", action="store_true", help="run one tick and exit")
    pp.set_defaults(fn=cmd_paper)

    lv = sub.add_parser("live", help="REAL orders; needs mode=live, --live and CBAGENT_LIVE_CONFIRM")
    lv.add_argument("--live", action="store_true", help="required to confirm real orders")
    lv.add_argument("--once", action="store_true")
    lv.set_defaults(fn=cmd_live)

    sub.add_parser("live-check", help="read-only check of the API key, balances and products").set_defaults(fn=cmd_live_check)
    sub.add_parser("status", help="show mode, kill switch and paper holdings").set_defaults(fn=cmd_status)
    k = sub.add_parser("kill", help="turn the kill switch on")
    k.add_argument("--reason")
    k.set_defaults(fn=cmd_kill)
    sub.add_parser("unkill", help="turn the kill switch off").set_defaults(fn=cmd_unkill)

    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # headlines carry curly quotes
    return args.fn(load_config(), args)


if __name__ == "__main__":
    raise SystemExit(main())
