#!/usr/bin/env python3
"""Reference parser for the CSV export of My Stocks Portfolio & Market (MSP).

This is a *reference implementation* of the format described in README.md. It is
deliberately small and dependency-free (Python 3.9+, standard library only) so it
can be read in one sitting and copied into any project.

What it does:
    - splits the export into (Portfolio, Symbol) blocks
    - derives the net position of each block from its transactions
    - handles every transaction type, including the three the official
      documentation never mentions: Interest, Sell All, Buy to Cover All

What it does NOT do (on purpose):
    - currency conversion, asset classification, P&L, cost basis
    - anything that requires knowing what your accounts mean

Usage:
    python3 msp_export_parser.py <export.csv>
    python3 msp_export_parser.py <export.csv> --portfolio Main
    python3 msp_export_parser.py <export.csv> --raw Margin EURUSD=X
    python3 msp_export_parser.py --self-test        # runs against example-export.csv

Not affiliated with, endorsed by, or derived from Peeksoft's code.

Copyright 2026 Izero

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import csv
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

# Column names seen in a 2026 export, for reference only.
# The column COUNT changes between app versions (a 2026-05 export had 19 columns;
# a 2026-07 export had 20, adding "Purchase Exchange Currencies"), so the parser
# always resolves columns BY HEADER NAME. Never hardcode indices.
COLUMNS_REFERENCE = [
    "Id", "Symbol", "Name", "Display Symbol", "Exchange", "Portfolio", "Currency",
    "Last Traded Price", "Shares Owned", "Cost Per Share", "Commission",
    "Transaction Date", "Transaction Time", "Purchase Exchange Rate",
    "Purchase Exchange Currencies", "Type", "Accounting",
    "Accounting Execution Ids", "Notes", "OutgoingCashLink",
]

# Types that move the share count, and their direction.
# "Dividend Reinvest" is +1 (same direction as Buy). That was established by
# three-way reconciliation against a second bookkeeping style for the same
# liability; see README "Dividend Reinvest direction".
QTY_SIGN = {
    "Buy": +1,
    "Buy to Cover": +1,
    "Dividend Reinvest": +1,
    "Sell": -1,
    "Sell Short": -1,
}

# Types that flatten the position to zero.
# *** The "Shares Owned" column is NOT usable for these. *** Across the rows of
# this type in one real-world export: the overwhelming majority held 0, a handful
# held the pre-close balance, one was off by a rounding tail. Treating the column
# as a delta produces phantom positions that never raise an error.
# See README "The Sell All trap".
FLATTEN_TYPES = {"Sell All", "Buy to Cover All"}

# Types where "Shares Owned" is a CASH AMOUNT, not a share count. They do not
# move the position at all. Positive = income, negative = expense.
CASH_ONLY_TYPES = {"Dividend", "Interest"}

# Ratio adjustment. shares:cost = new:old (shares=2, cost=1 means a 2-for-1 split).
SPLIT_TYPES = {"Split"}

# Every type this parser knows how to act on. A type outside this set is not
# "harmless extra data" — it means the file contains an instruction the parser
# cannot follow, and every position derived from that block is suspect.
KNOWN_TYPES = set(QTY_SIGN) | FLATTEN_TYPES | CASH_ONLY_TYPES | SPLIT_TYPES

# Columns without which the file cannot be an MSP export. Resolving a missing
# column to "" (which is what a name-based lookup naturally does) turns a wrong
# file into a plausible-looking empty portfolio, exit code 0 and all — the exact
# silent failure this specification is about.
REQUIRED_COLUMNS = ("Id", "Symbol", "Portfolio", "Type",
                    "Shares Owned", "Transaction Date")


class NotAnMspExport(ValueError):
    """The file is missing columns an MSP export always has."""


def _num(s, on_error=None):
    """Parse a numeric cell.

    An empty cell is a legitimate zero. A cell that is present but unparseable is
    *data loss*. Collapsing both to 0.0 with no signal is precisely the failure
    mode this specification spends its length warning about, so the two are kept
    apart: `on_error` is called with the raw value when a non-empty cell fails.

    ⚠ Number format is assumed to be "." decimal separator, "," thousands
    separator (discardable). This has not been checked on a device whose locale
    reverses the two. See README §8. [Unconfirmed]
    """
    s = (s or "").strip().replace(",", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        if on_error is not None:
            on_error(s)
        return 0.0


@dataclass
class Txn:
    """One transaction row."""
    id: str
    portfolio: str
    symbol: str
    ttype: str
    shares: float
    cost: float
    commission: float
    date: str
    time: str
    currency: str
    accounting: str
    notes: str
    cash_link: str
    raw: dict = field(default_factory=dict)


@dataclass
class Block:
    """One (Portfolio, Symbol) block: a snapshot row plus the transactions under it."""
    portfolio: str
    symbol: str
    name: str
    exchange: str
    currency: str
    last_price: float
    txns: list = field(default_factory=list)

    def net_shares(self):
        """Derive the net position by replaying transactions in file order.

        Positive = long, negative = short. There is no shortcut: the snapshot row
        does not carry a position, so this replay is the only way to get one.
        """
        qty = 0.0
        for t in self.txns:
            if t.ttype in QTY_SIGN:
                qty += QTY_SIGN[t.ttype] * t.shares
            elif t.ttype in FLATTEN_TYPES:
                qty = 0.0                       # unconditional; ignore t.shares
            elif t.ttype in SPLIT_TYPES:
                if t.cost:
                    qty = qty * t.shares / t.cost
            elif t.ttype in CASH_ONLY_TYPES:
                pass                            # amounts, not quantities
            # else: an unrecognised type. Deliberately does NOT fall through
            # silently — `unknown_types()` reports it and `parse()` collects it,
            # because a future app version adding a type would otherwise corrupt
            # positions with no signal at all.
        return qty

    def unknown_types(self):
        """Transaction types in this block that the parser cannot act on.

        Non-empty means `net_shares()` skipped rows, so the position it returns
        is missing whatever those rows were supposed to do.
        """
        return sorted({t.ttype for t in self.txns if t.ttype not in KNOWN_TYPES})

    def cash_flow_by_type(self):
        """Sum the cash-only types. Their "Shares Owned" column holds an amount."""
        out = defaultdict(float)
        for t in self.txns:
            if t.ttype in CASH_ONLY_TYPES:
                out[t.ttype] += t.shares
        return dict(out)

    def total_commission(self):
        return sum(t.commission for t in self.txns)

    def market_value(self):
        """net_shares x last_price, in this block's own currency (no FX applied).

        Correct for futures and index positions too: "Shares Owned" stores
        (value per point x contracts), so the product is already the notional.
        Do not multiply by a contract multiplier again.

        ⚠ **Only trust this for kinds you have verified yourself.** `kind()`
        classifies by symbol shape, and a futures contract whose ticker lacks the
        `=F` suffix is indistinguishable from an ordinary security — which is
        exactly the case where the product is not the notional (README §6 records
        one such counter-example). Symbol shape cannot detect this; it is a limit
        of the format, not a fixable bug. The symptom is a value off by an exact
        integer factor (the contract multiplier).
        """
        return self.net_shares() * self.last_price

    def kind(self):
        if self.symbol.endswith("=CASH"):
            return "cash"
        if self.symbol.endswith("=X"):
            return "fx"
        if self.symbol.startswith("^"):
            return "index"
        if self.symbol.endswith("=F"):
            return "futures"
        return "security"


@dataclass
class Problems:
    """Everything the parser noticed but could not fix.

    Empty is the healthy state. Anything here means some position may be wrong,
    and the caller has to decide what to do about it. The point of collecting
    these rather than swallowing them is that all three failure modes below are
    otherwise completely silent — the file parses, exit code is 0, and the
    numbers are simply incorrect.
    """
    unparseable: list = field(default_factory=list)   # (row_id, column, raw_value)
    unknown_types: dict = field(default_factory=dict)  # type name -> row count
    duplicate_pairs: list = field(default_factory=list)  # (portfolio, symbol)

    def __bool__(self):
        return bool(self.unparseable or self.unknown_types or self.duplicate_pairs)

    def summary(self):
        bits = []
        if self.unparseable:
            bits.append(f"{len(self.unparseable)} unparseable numeric cell(s)")
        if self.unknown_types:
            bits.append(f"{len(self.unknown_types)} unrecognised transaction type(s)")
        if self.duplicate_pairs:
            bits.append(f"{len(self.duplicate_pairs)} duplicated (Portfolio, Symbol) pair(s)")
        return "; ".join(bits) or "none"


def parse(path):
    """Parse an MSP export. Returns (blocks, header, problems). Blocks keep file order.

    Raises `NotAnMspExport` if the file is empty or lacks columns every export
    has. Without that check a three-column CSV parses "successfully" into a set
    of empty positions and exits 0.

    `problems` is a `Problems` instance — see that class. Check it. An empty
    `Problems` is the only state in which the returned positions can be trusted.

    **Rows within a block are replayed in file order.** In every export examined
    that order matched `Transaction Date` order exactly, but the app does not
    document any ordering guarantee. See README §1. [Unconfirmed]
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            header = [h.strip() for h in next(reader)]
        except StopIteration:
            raise NotAnMspExport(f"{path} is empty — no header row") from None
        rows = list(reader)

    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise NotAnMspExport(
            f"{path} does not look like an MSP export. Missing column(s): "
            f"{', '.join(missing)}. Found {len(header)} column(s): "
            f"{', '.join(header) if header else '(none)'}")

    idx = {name: i for i, name in enumerate(header)}
    problems = Problems()
    warnings = problems.unparseable

    def get(row, name):
        i = idx.get(name)
        if i is None or i >= len(row):
            return ""
        return row[i].strip()

    def num(row, name):
        return _num(get(row, name),
                    on_error=lambda v: warnings.append((get(row, "Id") or "?", name, v)))

    blocks = []
    current = None
    for row in rows:
        if not row or not any(cell.strip() for cell in row):
            continue                            # block separator; width varies
        if not get(row, "Transaction Date"):
            current = Block(
                portfolio=get(row, "Portfolio"),
                symbol=get(row, "Symbol"),
                name=get(row, "Name"),
                exchange=get(row, "Exchange"),
                currency=get(row, "Currency"),
                last_price=num(row, "Last Traded Price"),
            )
            blocks.append(current)
            continue

        txn = Txn(
            id=get(row, "Id"),
            portfolio=get(row, "Portfolio"),
            symbol=get(row, "Symbol"),
            ttype=get(row, "Type"),
            shares=num(row, "Shares Owned"),
            cost=num(row, "Cost Per Share"),
            commission=num(row, "Commission"),
            date=get(row, "Transaction Date")[:10],
            time=get(row, "Transaction Time"),
            currency=get(row, "Currency"),
            accounting=get(row, "Accounting"),
            notes=get(row, "Notes"),
            cash_link=get(row, "OutgoingCashLink"),
            raw={name: get(row, name) for name in header},
        )
        # A transaction with no preceding snapshot row should not happen, but
        # fail loudly rather than silently attaching it to the wrong block.
        if current is None or (current.portfolio, current.symbol) != (txn.portfolio, txn.symbol):
            current = Block(portfolio=txn.portfolio, symbol=txn.symbol,
                            name="<NO SNAPSHOT ROW>", exchange="",
                            currency=txn.currency, last_price=0.0)
            blocks.append(current)
        current.txns.append(txn)

    for b in blocks:
        for ttype in b.unknown_types():
            problems.unknown_types[ttype] = problems.unknown_types.get(ttype, 0) + sum(
                1 for t in b.txns if t.ttype == ttype)
    problems.duplicate_pairs = [k for k, n in Counter(
        (b.portfolio, b.symbol) for b in blocks).items() if n > 1]

    return blocks, header, problems


def by_portfolio(blocks):
    out = defaultdict(list)
    for b in blocks:
        out[b.portfolio].append(b)
    return dict(out)


def cash_links(blocks):
    """Resolve OutgoingCashLink -> the transaction it points at.

    Returns [(source_txn, target_txn_or_None)]. The link is an Id, and Ids are
    only valid WITHIN ONE FILE — they get renumbered between exports.
    """
    by_id = {t.id: t for b in blocks for t in b.txns}
    return [(t, by_id.get(t.cash_link))
            for b in blocks for t in b.txns if t.cash_link]


# --------------------------------------------------------------------------- CLI

EXPECTED_SELF_TEST = {
    ("Main", "ACME"): 150.0,        # 100 buy, 2-for-1 split -> 200, sell 50
    ("Main", "GLOBEX.L"): 0.0,      # Sell All with shares=0 must still flatten
    ("Main", "USD=CASH"): -4956.0,  # -5001 + 45; Interest does not move shares
    ("Margin", "EURUSD=X"): -70250.0,  # short 100000, +(-250) capitalised, cover 30000
    ("Main", "^GSPC"): 250.0,       # Dividend (roll cost) does not move shares
    ("Margin", "CHFUSD=X"): 0.0,    # Buy to Cover All with shares=0 must still flatten
}


def _report_problems(problems, prefix="⚠"):
    """Print whatever the parser could not resolve. Returns the number of issues."""
    n = 0
    if problems.unparseable:
        n += len(problems.unparseable)
        print(f"\n{prefix} {len(problems.unparseable)} numeric cell(s) present but "
              f"unparseable — each became 0.0, so some position below may be wrong:")
        for row_id, col, raw in problems.unparseable[:10]:
            print(f"    Id {row_id} column {col!r}: {raw!r}")
        if len(problems.unparseable) > 10:
            print(f"    ... and {len(problems.unparseable) - 10} more")
    if problems.unknown_types:
        n += len(problems.unknown_types)
        print(f"\n{prefix} {len(problems.unknown_types)} unrecognised transaction "
              f"type(s). Rows of these types were SKIPPED, so the positions below "
              f"are missing whatever they were meant to do:")
        for ttype, count in sorted(problems.unknown_types.items()):
            print(f"    {ttype!r}: {count} row(s)")
        print("    If the app added a type, this parser is out of date.")
    if problems.duplicate_pairs:
        n += len(problems.duplicate_pairs)
        print(f"\n{prefix} {len(problems.duplicate_pairs)} (Portfolio, Symbol) pair(s) "
              f"occupy more than one block. Anything that keys on the pair rather "
              f"than iterating blocks will lose a position (README §8):")
        for pf, sym in problems.duplicate_pairs[:5]:
            print(f"    {pf} / {sym}")
    return n


def self_test():
    """Smoke test against the bundled synthetic export. See test_parser.py for the
    full unittest suite; this exists so `--self-test` works with no test runner."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "example-export.csv")
    if not os.path.exists(path):
        print(f"example-export.csv not found next to this script ({path})")
        return 1
    try:
        blocks, header, problems = parse(path)
    except NotAnMspExport as exc:
        print(f"FAIL {exc}")
        return 1

    print(f"parsed {len(blocks)} blocks, {sum(len(b.txns) for b in blocks)} transactions, "
          f"{len(header)} columns\n")
    failures = 0
    seen = set()
    for b in blocks:
        want = EXPECTED_SELF_TEST.get((b.portfolio, b.symbol))
        got = b.net_shares()
        ok = want is not None and abs(got - want) < 1e-9
        failures += 0 if ok else 1
        seen.add((b.portfolio, b.symbol))
        print(f"  {'ok  ' if ok else 'FAIL'} {b.portfolio:8s} {b.symbol:10s} "
              f"net={got:>12,.2f}" + ("" if ok else f"  expected {want}"))

    # Iterating only the blocks that showed up means a truncated file passes:
    # four of five blocks could vanish and every remaining one still checks out.
    for key in sorted(set(EXPECTED_SELF_TEST) - seen):
        failures += 1
        print(f"  FAIL {key[0]:8s} {key[1]:10s} block missing entirely")

    # The synthetic file must also exercise every documented transaction type,
    # or the README's claim that it does becomes false without anyone noticing.
    present = {t.ttype for b in blocks for t in b.txns}
    for missing in sorted(KNOWN_TYPES - present):
        failures += 1
        print(f"  FAIL type {missing!r} never appears in the example")

    failures += _report_problems(problems, prefix="  FAIL")
    print("\nall expectations met" if not failures else f"\n{failures} failure(s)")
    return 1 if failures else 0


def main():
    args = sys.argv[1:]
    if "--self-test" in args:
        sys.exit(self_test())
    if not args or args[0].startswith("-"):
        print(__doc__)
        sys.exit(1)

    path = args[0]
    try:
        blocks, header, problems = parse(path)
    except FileNotFoundError:
        print(f"error: no such file: {path}", file=sys.stderr)
        sys.exit(2)
    except NotAnMspExport as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)

    def opt(flag, count=1):
        if flag not in args:
            return None
        i = args.index(flag)
        if i + count >= len(args):
            print(f"error: {flag} needs {count} argument(s)", file=sys.stderr)
            sys.exit(2)
        return args[i + 1:i + 1 + count]

    raw = opt("--raw", 2)
    if raw:
        pf, sym = raw
        hits = [b for b in blocks if (b.portfolio, b.symbol) == (pf, sym)]
        if not hits:
            print(f"error: no block for {pf} / {sym}", file=sys.stderr)
            sys.exit(2)
        for b in hits:
            print(f"[{b.portfolio}/{b.symbol}] {b.name} "
                  f"currency={b.currency} last={b.last_price}")
            for t in b.txns:
                print(f"  {t.id:>5s} {t.date} {t.ttype:18s} "
                      f"shares={t.shares:>16,.4f} cost={t.cost:>12,.4f} "
                      f"comm={t.commission:>8,.2f} {t.notes[:48]!r}")
            print(f"  => net_shares={b.net_shares():,.4f}  "
                  f"market_value={b.market_value():,.2f} {b.currency}")
        return

    pf_filter = opt("--portfolio")
    only = pf_filter[0] if pf_filter else None

    print(f"file:    {path}")
    print(f"columns: {len(header)}   blocks: {len(blocks)}   "
          f"transactions: {sum(len(b.txns) for b in blocks)}")
    links = cash_links(blocks)
    print(f"cash links: {len(links)} "
          f"({sum(1 for _, tgt in links if tgt is None)} unresolved)")
    _report_problems(problems)
    print()

    for pf, bs in sorted(by_portfolio(blocks).items()):
        if only and pf != only:
            continue
        print(f"{'=' * 78}\n[{pf}]  {len(bs)} symbols")
        for b in bs:
            flows = " ".join(f"{k}={v:,.2f}" for k, v in b.cash_flow_by_type().items())
            print(f"  {b.symbol:14s} {b.kind():9s} net={b.net_shares():>16,.4f} "
                  f"@{b.last_price:<12,.4f} mv={b.market_value():>16,.2f} "
                  f"{b.currency:4s} n={len(b.txns):<4d} {flows}")

    # A file with problems still prints its positions — they are useful even when
    # incomplete — but the exit code has to say so, or a script calling this will
    # treat corrupted output as success.
    if problems:
        print(f"\nexiting non-zero: {problems.summary()}")
        sys.exit(1)


if __name__ == "__main__":
    main()
