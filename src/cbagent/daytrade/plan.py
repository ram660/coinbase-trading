"""Trade plan, position size and the order ticket.

`make_plan` turns an entry and two rules into prices and a size:

  stop rules    loss:5      lose about $5 on the position at the stop (fees included)
                pct:3       3% from entry
                atr:2       2 x ATR(14) of 1-hour candles
                price:178.6 a fixed price
  target rules  gain:5      make about $5 at the target (fees included)
                R:1.5       1.5 times the stop distance
                pct:4 / atr:3 / price:204.2

Size: spend `budget`, unless `max_loss` is set, in which case the spend is cut so the loss at the stop (fees
included) stays under it. With a loss: stop rule the size is the budget and the stop is moved to fit.

The ticket shows the plan in plain words plus the exact arguments for the Coinbase connector's orders_create
(market BUY with attached take-profit and stop). Nothing here sends it. `approve` records Ram's reply word for word
and only counts an explicit yes for that one ticket; placing the order stays a separate, confirmed step.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from .common import folder, read_json, round_to, write_json

STANDING_MAX_QUOTE = 75  # Ram's standing approval: one spot market BUY, USDC, at most $75 (CLAUDE.md rule 1)


@dataclass
class TradePlan:
    product_id: str
    side: str
    entry: float
    stop: float
    target: float
    quote_size: float        # what the BUY spends, in quote currency
    base_size: float         # what it buys, before fees
    max_loss: float          # at the stop, fees in and out included
    max_gain: float          # at the target, fees included
    fee_pct: float
    hours: float
    created: int
    stop_rule: str
    target_rule: str
    note: str = ""
    price_increment: str | None = None
    quote_increment: str | None = None

    @property
    def reward_r(self) -> float:
        return abs(self.target - self.entry) / abs(self.entry - self.stop)

    def fingerprint(self) -> str:
        key = f"{self.product_id}|{self.side}|{self.quote_size:.2f}|{self.stop:.10g}|{self.target:.10g}|{self.created}"
        return hashlib.sha256(key.encode()).hexdigest()[:10]


def _rule(rule: str) -> tuple[str, float]:
    kind, _, value = rule.partition(":")
    kind = kind.strip().lower()
    if kind not in ("loss", "gain", "pct", "atr", "price", "r") or not value:
        raise ValueError(f"bad rule {rule!r}: use loss:5, gain:5, pct:3, atr:2, R:1.5 or price:178.6")
    return kind, float(value)


def _pnl(side: str, entry: float, exit_: float, quote: float, fee_pct: float) -> float:
    base = quote / entry
    gross = (exit_ - entry) * base if side == "long" else (entry - exit_) * base
    return gross - fee_pct / 100 * (quote + base * exit_)


def make_plan(product_id: str, side: str, entry: float, stop_rule: str, target_rule: str, budget: float,
              max_loss: float | None = None, fee_pct: float = 0.5, hours: float = 24, atr: float | None = None,
              price_increment: str | None = None, quote_increment: str | None = None, now: int | None = None) -> TradePlan:
    if side not in ("long", "short"):
        raise ValueError("side must be long or short")
    sign = 1 if side == "long" else -1
    fee = fee_pct / 100
    quote = budget

    sk, sv = _rule(stop_rule)
    if sk == "loss":
        # loss = quote*(entry-stop)/entry + fee*(quote + quote*stop/entry), solved for stop (long; mirrored for short)
        stop = entry * (1 + fee - sv / quote) / (1 - fee) if side == "long" else entry * (1 - fee + sv / quote) / (1 + fee)
    elif sk == "pct":
        stop = entry * (1 - sign * sv / 100)
    elif sk == "atr":
        if not atr:
            raise ValueError("atr: rules need the ATR")
        stop = entry - sign * sv * atr
    elif sk == "price":
        stop = sv
    else:
        raise ValueError("stop rule must be loss, pct, atr or price")
    if stop <= 0 or ((stop >= entry) if side == "long" else (stop <= entry)):
        raise ValueError(f"that stop ({stop:g}) is on the wrong side of the entry ({entry:g}); is the budget too small for the loss?")
    stop = float(round_to(stop, price_increment))

    if max_loss is not None and sk != "loss":
        per_dollar = -_pnl(side, entry, stop, 1.0, fee_pct)
        quote = min(budget, max_loss / per_dollar)
    if quote_increment:
        quote = float(round_to(quote, quote_increment, down=True))

    tk, tv = _rule(target_rule)
    risk = abs(entry - stop)
    if tk == "gain":
        target = entry * (1 + fee + tv / quote) / (1 - fee) if side == "long" else entry * (1 - fee - tv / quote) / (1 + fee)
    elif tk == "r":
        target = entry + sign * tv * risk
    elif tk == "pct":
        target = entry * (1 + sign * tv / 100)
    elif tk == "atr":
        if not atr:
            raise ValueError("atr: rules need the ATR")
        target = entry + sign * tv * atr
    elif tk == "price":
        target = tv
    else:
        raise ValueError("target rule must be gain, R, pct, atr or price")
    if (target <= entry) if side == "long" else (target >= entry):
        raise ValueError(f"that target ({target:g}) is on the wrong side of the entry ({entry:g})")
    target = float(round_to(target, price_increment))

    return TradePlan(product_id, side, entry, stop, target, round(quote, 2), quote / entry,
                     round(-_pnl(side, entry, stop, quote, fee_pct), 2), round(_pnl(side, entry, target, quote, fee_pct), 2),
                     fee_pct, hours, now or int(time.time()), stop_rule, target_rule,
                     price_increment=price_increment, quote_increment=quote_increment)


def save(plan: TradePlan, data_dir: Path) -> Path:
    path = folder(data_dir) / "plan.json"
    write_json(path, asdict(plan))
    return path


def load(data_dir: Path) -> TradePlan | None:
    raw = read_json(folder(data_dir) / "plan.json", None)
    return TradePlan(**raw) if raw else None


def describe(p: TradePlan) -> str:
    pct = lambda x: (x / p.entry - 1) * 100
    return "\n".join([
        f"Plan: {p.side.upper()} {p.product_id} at {p.entry:g}, spend {p.quote_size:.2f} (about {p.base_size:.6g} units)",
        f"  stop   {p.stop:g} ({pct(p.stop):+.2f}%), loss about {p.max_loss:.2f} with fees  [{p.stop_rule}]",
        f"  target {p.target:g} ({pct(p.target):+.2f}%), gain about {p.max_gain:.2f} with fees  [{p.target_rule}]",
        f"  reward {p.reward_r:.2f}R before fees, fees {p.fee_pct:g}% each way, out after {p.hours:g}h",
    ])


def order_args(p: TradePlan) -> dict | None:
    """Arguments for the Coinbase connector's orders_create, or None when spot can't express it (a short)."""
    if p.side != "long":
        return None
    return {
        "product_id": p.product_id,
        "side": "BUY",
        "type": "market",
        "quote_size": f"{p.quote_size:.2f}",
        "attached_take_profit_price": str(round_to(p.target, p.price_increment)),
        "attached_stop_loss_price": str(round_to(p.stop, p.price_increment)),
    }


def checks(p: TradePlan, open_position: bool = False) -> list[str]:
    """Plain-word notes about the ticket. They inform; they don't block."""
    notes = []
    quote = p.product_id.split("-")[-1]
    within = (p.side == "long" and quote == "USDC" and p.quote_size <= STANDING_MAX_QUOTE and not open_position)
    if within:
        notes.append(f"Fits the standing approval (one spot BUY, USDC, at most ${STANDING_MAX_QUOTE}, with TP and stop) "
                     "if it is the coin the stated rule picked and no live position is open.")
    else:
        why = []
        if p.side != "long":
            why.append("it is a short")
        if quote != "USDC":
            why.append(f"it is paid in {quote}, not USDC")
        if p.quote_size > STANDING_MAX_QUOTE:
            why.append(f"it spends more than ${STANDING_MAX_QUOTE}")
        if open_position:
            why.append("a live position is already open")
        notes.append("Needs Ram's explicit yes for this exact order: " + ", ".join(why) + ".")
    if p.side == "short":
        notes.append("Spot can't sell what isn't held: a short needs a futures contract, and the account isn't approved for US futures yet.")
    if p.reward_r < 1:
        notes.append(f"Reward is {p.reward_r:.2f}R: the target is closer than the stop.")
    if p.max_gain <= 0:
        notes.append("Fees eat the whole target: this trade can't make money even if the target fills.")
    return notes


def ticket(p: TradePlan, open_position: bool = False) -> str:
    args = order_args(p)
    lines = [f"TICKET {p.fingerprint()}  (REAL money if sent; nothing is sent from here)", describe(p)]
    if args:
        lines += ["Exact orders_create arguments:", "  " + json.dumps(args)]
    lines += ["Notes:"] + [f"  - {n}" for n in checks(p, open_position)]
    lines.append(f"To approve: reply \"yes {p.product_id.split('-')[0]}\" (one ticket per yes).")
    return "\n".join(lines)


YES = re.compile(r"^\s*(yes|y|yep|yeah|confirm(ed)?|approved?|go( ahead)?|place it|send it|do it)\b", re.I)
HEDGE = re.compile(r"\b(not|no|don'?t|wait|hold|maybe|later|if|unless|all|every|both)\b", re.I)


def is_yes(reply: str, p: TradePlan) -> tuple[bool, str]:
    """Only an unhedged yes that doesn't name another coin or another amount counts."""
    if not YES.match(reply):
        return False, "that isn't an explicit yes"
    if HEDGE.search(reply):
        return False, "the reply is hedged or covers more than this one order"
    base = p.product_id.split("-")[0].upper()
    coins = set(re.findall(r"\b[A-Z][A-Z0-9]{1,9}\b", reply)) - {"YES", "Y", "USDC", "USD", "OK", "TP", "SL", base}
    if coins:
        return False, f"the reply names {', '.join(sorted(coins))}, not {base}"
    amounts = [float(a.replace(",", "")) for a in re.findall(r"\$\s*([\d,]+(?:\.\d+)?)", reply)]
    if any(abs(a - p.quote_size) > 0.5 for a in amounts):
        return False, f"the reply names a different amount than {p.quote_size:.2f}"
    return True, "explicit yes for this ticket"


def approve(p: TradePlan, reply: str, data_dir: Path, now: int | None = None) -> tuple[bool, str]:
    ok, why = is_yes(reply, p)
    entry = {"ts": now or int(time.time()), "ticket": p.fingerprint(), "product_id": p.product_id,
             "quote_size": p.quote_size, "reply": reply, "recorded_as_yes": ok, "why": why}
    with (folder(data_dir) / "approvals.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return ok, why


def approved(p: TradePlan, data_dir: Path) -> bool:
    path = folder(data_dir) / "approvals.jsonl"
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e.get("ticket") == p.fingerprint() and e.get("recorded_as_yes"):
            return True
    return False
