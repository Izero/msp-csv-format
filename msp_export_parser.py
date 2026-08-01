#!/usr/bin/env python3
"""Reference parser for the CSV export of My Stocks Portfolio & Market (MSP).

This is a *reference implementation* of the format described in README.md. It is
deliberately small and dependency-free (Python 3.8+, standard library only) so it
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
from collections import defaultdict
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
# *** The "Shares Owned" column is NOT usable for these. *** In one real-world
# export containing 105 such rows: 100 had shares=0, 4 held the pre-close balance,
# and 1 was off by a rounding tail. Treating the column as a delta produces
# phantom positions that never raise an error. See README "The Sell All trap".
FLATTEN_TYPES = {"Sell All", "Buy to Cover All"}

# Types where "Shares Owned" is a CASH AMOUNT, not a share count. They do not
# move the position at all. Positive = income, negative = expense.
CASH_ONLY_TYPES = {"Dividend", "Interest"}

# Ratio adjustment. shares:cost = new:old (shares=2, cost=1 means a 2-for-1 split).
SPLIT_TYPES = {"Split"}


def _num(s):
    """Parse a numeric cell. Blank, malformed, or missing all become 0.0."""
    s = (s or "").strip().replace(",", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
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
            # CASH_ONLY_TYPES deliberately do nothing here
        return qty

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


def parse(path):
    """Parse an MSP export. Returns (blocks, header). Blocks keep file order."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = [h.strip() for h in next(reader)]
        rows = list(reader)

    idx = {name: i for i, name in enumerate(header)}

    def get(row, name):
        i = idx.get(name)
        if i is None or i >= len(row):
            return ""
        return row[i].strip()

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
                last_price=_num(get(row, "Last Traded Price")),
            )
            blocks.append(current)
            continue

        txn = Txn(
            id=get(row, "Id"),
            portfolio=get(row, "Portfolio"),
            symbol=get(row, "Symbol"),
            ttype=get(row, "Type"),
            shares=_num(get(row, "Shares Owned")),
            cost=_num(get(row, "Cost Per Share")),
            commission=_num(get(row, "Commission")),
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

    return blocks, header


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
}


def self_test():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "example-export.csv")
    if not os.path.exists(path):
        print(f"example-export.csv not found next to this script ({path})")
        return 1
    blocks, header = parse(path)
    print(f"parsed {len(blocks)} blocks, {sum(len(b.txns) for b in blocks)} transactions, "
          f"{len(header)} columns\n")
    failures = 0
    for b in blocks:
        want = EXPECTED_SELF_TEST.get((b.portfolio, b.symbol))
        got = b.net_shares()
        ok = want is not None and abs(got - want) < 1e-9
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {b.portfolio:8s} {b.symbol:10s} "
              f"net={got:>12,.2f}" + ("" if ok else f"  expected {want:,.2f}"))
    print("\nall expectations met" if not failures else f"\n{failures} failure(s)")
    return 1 if failures else 0


def main():
    args = sys.argv[1:]
    if "--self-test" in args:
        sys.exit(self_test())
    if not args:
        print(__doc__)
        sys.exit(1)

    path = args[0]
    blocks, header = parse(path)

    if "--raw" in args:
        i = args.index("--raw")
        pf, sym = args[i + 1], args[i + 2]
        for b in blocks:
            if b.portfolio == pf and b.symbol == sym:
                print(f"[{b.portfolio}/{b.symbol}] {b.name} "
                      f"currency={b.currency} last={b.last_price}")
                for t in b.txns:
                    print(f"  {t.id:>5s} {t.date} {t.ttype:18s} "
                          f"shares={t.shares:>16,.4f} cost={t.cost:>12,.4f} "
                          f"comm={t.commission:>8,.2f} {t.notes[:48]!r}")
                print(f"  => net_shares={b.net_shares():,.4f}  "
                      f"market_value={b.market_value():,.2f} {b.currency}")
        return

    only = args[args.index("--portfolio") + 1] if "--portfolio" in args else None

    print(f"file:    {path}")
    print(f"columns: {len(header)}   blocks: {len(blocks)}   "
          f"transactions: {sum(len(b.txns) for b in blocks)}")
    links = cash_links(blocks)
    print(f"cash links: {len(links)} "
          f"({sum(1 for _, tgt in links if tgt is None)} unresolved)\n")

    for pf, bs in sorted(by_portfolio(blocks).items()):
        if only and pf != only:
            continue
        print(f"{'=' * 78}\n[{pf}]  {len(bs)} symbols")
        for b in bs:
            flows = " ".join(f"{k}={v:,.2f}" for k, v in b.cash_flow_by_type().items())
            print(f"  {b.symbol:14s} {b.kind():9s} net={b.net_shares():>16,.4f} "
                  f"@{b.last_price:<12,.4f} mv={b.market_value():>16,.2f} "
                  f"{b.currency:4s} n={len(b.txns):<4d} {flows}")


if __name__ == "__main__":
    main()
