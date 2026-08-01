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
import argparse
import csv
import math
import re
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
# these types in one real-world export (a hundred-odd of them): ~95% held 0, ~4%
# held the pre-close balance, a single row was off by a rounding tail. Treating
# the column as a delta produces phantom positions that never raise an error.
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

# Every column this parser reads. A name-based lookup resolves a missing column
# to "", so an absent column does not raise — it changes the answer. Measured:
# drop "Cost Per Share" and a 2-for-1 Split is skipped, turning a 150-share
# position into 50; drop "Last Traded Price" and every market value becomes 0.
# Both exited 0. So the check covers everything read, not just what identifies
# the file.
REQUIRED_COLUMNS = (
    "Id", "Symbol", "Name", "Exchange", "Portfolio", "Currency",
    "Last Traded Price", "Shares Owned", "Cost Per Share", "Commission",
    "Transaction Date", "Transaction Time", "Type", "Accounting", "Notes",
    "OutgoingCashLink",
)
# Deliberately NOT required — present in exports but never read here:
#   "Display Symbol", "Purchase Exchange Rate", "Accounting Execution Ids",
#   "Purchase Exchange Currencies" (absent from the 19-column version entirely).


class NotAnMspExport(ValueError):
    """The file is missing columns an MSP export always has."""


# A comma is only a thousands separator when it is followed by exactly three
# digits. "1,5" is one-and-a-half in a comma-decimal locale and would otherwise
# be read here as fifteen — a tenfold error, silently.
_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(,\d{3})*(\.\d+)?$")


def _num(s, on_error=None):
    """Parse a numeric cell.

    An empty cell is a legitimate zero. A cell that is present but unparseable is
    *data loss*. Collapsing both to 0.0 with no signal is precisely the failure
    mode this specification spends its length warning about, so the two are kept
    apart: `on_error` is called with the raw value when a non-empty cell fails.

    Assumes "." decimal separator and "," thousands separator, and catches only
    the unambiguous half of that assumption: a comma somewhere a thousands
    separator cannot go ("1,5") is rejected rather than stripped. A comma where
    one *can* go is indistinguishable — "1,234" is 1234 here and 1.234 under a
    comma-decimal locale, and no single value tells you which. See README §8.
    """
    s = (s or "").strip()
    if not s:
        return 0.0
    if "," in s:
        if not _THOUSANDS.match(s):
            if on_error is not None:
                on_error(s)
            return 0.0
        s = s.replace(",", "")
    try:
        value = float(s)
    except ValueError:
        if on_error is not None:
            on_error(s)
        return 0.0
    if not math.isfinite(value):
        # float() accepts "NaN" and "Infinity" without complaint. A NaN in a
        # share count propagates through every later addition, turns the whole
        # position into nan, and compares false against everything — so it slips
        # past most sanity checks instead of tripping them.
        if on_error is not None:
            on_error(s)
        return 0.0
    return value


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
                if t.shares > 0 and t.cost > 0:
                    qty = qty * t.shares / t.cost
                # else: undefined ratio. Left alone here and reported by
                # `unapplicable_splits()` — see that method for why skipping
                # quietly is the worst of the available options.
            elif t.ttype in CASH_ONLY_TYPES:
                pass                            # amounts, not quantities
            # else: an unrecognised type. Deliberately does NOT fall through
            # silently — `unknown_types()` reports it and `parse()` collects it,
            # because a future app version adding a type would otherwise corrupt
            # positions with no signal at all.
        return qty

    def unapplicable_splits(self):
        """`Split` rows whose ratio cannot be applied — blank or zero denominator.

`shares:cost = new:old`. **Both sides have to be positive.** Zero or blank
        makes the ratio undefined; a negative on either side makes it *inverted*,
        which is worse — a truthiness guard lets `-1` through and turns a 100
        share long into a 200 share short, exit 0, no output. That is the same
        phantom short position §4 warns about, arriving through a different door,
        and negative values are not far-fetched in this format (see the sign
        convention).

        Skipping quietly is the wrong response either way: it leaves the position
        at its pre-split value, a plausible number with nothing attached. `Split`
        is one of only two order-sensitive types, which makes silence expensive.
        """
        return [t.id for t in self.txns
                if t.ttype in SPLIT_TYPES and not (t.shares > 0 and t.cost > 0)]

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
    unapplicable_splits: list = field(default_factory=list)  # (row_id, portfolio, symbol)
    duplicate_ids: list = field(default_factory=list)   # (id, occurrences)
    unresolved_links: list = field(default_factory=list)  # (source_id, target_id)
    malformed_rows: list = field(default_factory=list)  # (line_number, cell_count)
    duplicate_pairs: list = field(default_factory=list)  # (portfolio, symbol)

    def __bool__(self):
        """True when something is actually *wrong*.

        `duplicate_pairs` is deliberately excluded. A (Portfolio, Symbol) pair
        occupying two blocks is documented format behaviour (README §8), not a
        defect, and this parser handles it correctly by keeping both. It is
        reported so that callers who key on the pair are not caught out, but a
        file containing one is not a bad file.
        """
        return bool(self.unparseable or self.unknown_types
                    or self.unapplicable_splits or self.duplicate_ids
                    or self.malformed_rows or self.unresolved_links)

    def summary(self):
        bits = []
        if self.unparseable:
            bits.append(f"{len(self.unparseable)} unparseable numeric cell(s)")
        if self.unknown_types:
            bits.append(f"{len(self.unknown_types)} unrecognised transaction type(s)")
        if self.unapplicable_splits:
            bits.append(f"{len(self.unapplicable_splits)} split(s) with an undefined ratio")
        if self.duplicate_ids:
            bits.append(f"{len(self.duplicate_ids)} duplicated Id(s)")
        if self.unresolved_links:
            bits.append(f"{len(self.unresolved_links)} unresolved cash link(s)")
        if self.malformed_rows:
            bits.append(f"{len(self.malformed_rows)} row(s) of the wrong width")
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
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            try:
                header = [h.strip() for h in next(reader)]
            except StopIteration:
                raise NotAnMspExport(f"{path} is empty — no header row") from None
            rows = list(reader)
    except UnicodeDecodeError as exc:
        # The exit-code contract promises 2 for unreadable input; without this
        # a UTF-16 file gave a traceback instead.
        raise NotAnMspExport(
            f"{path} is not UTF-8 text ({exc.reason} at byte {exc.start}). "
            f"MSP writes UTF-8; another encoding means this is either not an "
            f"export or was re-saved by something else.") from None

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
    all_ids = []
    for line_no, row in enumerate(rows, start=2):   # header is line 1
        if not row or not any(cell.strip() for cell in row):
            continue                            # block separator; width varies
        if len(row) != len(header):
            # A short row is not harmless: `get()` resolves the missing columns
            # to "", so an absent "Transaction Date" makes the row look like a
            # snapshot and it becomes a ghost block with an empty portfolio name.
            problems.malformed_rows.append((line_no, len(row)))
            continue
        all_ids.append(get(row, "Id"))
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

    # §1 states Id uniqueness within a file as [Verified]. cash_links() trusts it
    # — a dict keyed on Id silently keeps the last of any duplicates — so a file
    # that breaks the claim is not the thing the specification describes.
    problems.duplicate_ids = [(i, n) for i, n in Counter(all_ids).items()
                              if n > 1 and i]

    for b in blocks:
        for row_id in b.unapplicable_splits():
            problems.unapplicable_splits.append((row_id, b.portfolio, b.symbol))
        for ttype in b.unknown_types():
            problems.unknown_types[ttype] = problems.unknown_types.get(ttype, 0) + sum(
                1 for t in b.txns if t.ttype == ttype)
    problems.duplicate_pairs = [k for k, n in Counter(
        (b.portfolio, b.symbol) for b in blocks).items() if n > 1]
    # §5 and §7 record every link in the sample resolving inside its own file.
    # One that does not means the pairing data is incomplete — it does not move
    # a position, but it is not the format behaving as documented either.
    problems.unresolved_links = [(src.id, src.cash_link)
                                 for src, tgt in cash_links(blocks) if tgt is None]

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
    if problems.unapplicable_splits:
        n += len(problems.unapplicable_splits)
        print(f"\n{prefix} {len(problems.unapplicable_splits)} Split row(s) with an "
              f"undefined ratio (blank or zero 'Cost Per Share'). The split was NOT "
              f"applied, so the position is stuck at its pre-split value:")
        for row_id, pf, sym in problems.unapplicable_splits[:10]:
            print(f"    Id {row_id}: {pf} / {sym}")
    if problems.malformed_rows:
        n += len(problems.malformed_rows)
        print(f"\n{prefix} {len(problems.malformed_rows)} row(s) whose cell count "
              f"does not match the header — SKIPPED, because a short row's missing "
              f"columns read as empty and turn it into a ghost block:")
        for line_no, width in problems.malformed_rows[:10]:
            print(f"    line {line_no}: {width} cell(s)")
    if problems.unresolved_links:
        n += len(problems.unresolved_links)
        print(f"\n{prefix} {len(problems.unresolved_links)} OutgoingCashLink value(s) "
              f"point at an Id that is not in this file. Positions are unaffected, "
              f"but the cash pairing is incomplete (§5):")
        for src_id, target in problems.unresolved_links[:10]:
            print(f"    Id {src_id} → {target!r} (not found)")
    if problems.duplicate_ids:
        n += len(problems.duplicate_ids)
        print(f"\n{prefix} {len(problems.duplicate_ids)} duplicated Id(s). §1 states "
              f"Id is unique within a file, and OutgoingCashLink resolution relies "
              f"on it — a duplicate means links may resolve to the wrong row:")
        for row_id, count in problems.duplicate_ids[:10]:
            print(f"    Id {row_id!r}: {count} rows")
    if problems.unknown_types:
        n += len(problems.unknown_types)
        print(f"\n{prefix} {len(problems.unknown_types)} unrecognised transaction "
              f"type(s). Rows of these types were SKIPPED, so the positions below "
              f"are missing whatever they were meant to do:")
        for ttype, count in sorted(problems.unknown_types.items()):
            print(f"    {ttype!r}: {count} row(s)")
        print("    If the app added a type, this parser is out of date.")
    # Not counted as an error: this is documented format behaviour and the parser
    # handles it correctly by keeping both blocks. Reported so that a caller who
    # keys on the pair is not caught out.
    if problems.duplicate_pairs:
        print(f"\nnote: {len(problems.duplicate_pairs)} (Portfolio, Symbol) pair(s) "
              f"occupy more than one block — normal for this format (README §8), "
              f"but anything that keys on the pair rather than iterating blocks "
              f"will lose a position:")
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


def _argparser():
    ap = argparse.ArgumentParser(
        prog="msp_export_parser.py",
        description="Parse a My Stocks Portfolio & Market CSV export.",
        epilog="exit codes: 0 = clean, 1 = parsed but problems found, "
               "2 = input unusable")
    ap.add_argument("path", nargs="?", help="the exported CSV")
    ap.add_argument("--self-test", action="store_true",
                    help="check against the bundled example-export.csv and exit")
    ap.add_argument("--portfolio", metavar="NAME",
                    help="restrict the listing to one portfolio")
    ap.add_argument("--raw", nargs=2, metavar=("PORTFOLIO", "SYMBOL"),
                    help="dump every transaction of a single block")
    return ap


def main():
    # argparse rather than hand-rolled parsing: an unrecognised option used to be
    # ignored outright, so a typo'd flag ran the default listing and exited 0.
    ap = _argparser()
    ns = ap.parse_args()

    if ns.self_test:
        sys.exit(self_test())
    if not ns.path:
        ap.error("a CSV path is required (or pass --self-test)")

    try:
        blocks, header, problems = parse(ns.path)
    except OSError as exc:
        # Not just FileNotFoundError: a directory raises IsADirectoryError, an
        # unreadable file raises PermissionError, and the exit-code contract
        # promises 2 for all of them.
        print(f"error: cannot read {ns.path}: {exc.strerror or exc}", file=sys.stderr)
        sys.exit(2)
    except NotAnMspExport as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)

    if ns.raw:
        pf, sym = ns.raw
        hits = [b for b in blocks if (b.portfolio, b.symbol) == (pf, sym)]
        if not hits:
            print(f"error: no block for {pf} / {sym}", file=sys.stderr)
            sys.exit(2)
        for b in hits:
            print(f"[{b.portfolio}/{b.symbol}] {b.name} "
                  f"currency={b.currency} last={b.last_price}")
            for txn in b.txns:
                print(f"  {txn.id:>5s} {txn.date} {txn.ttype:18s} "
                      f"shares={txn.shares:>16,.4f} cost={txn.cost:>12,.4f} "
                      f"comm={txn.commission:>8,.2f} {txn.notes[:48]!r}")
            print(f"  => net_shares={b.net_shares():,.4f}  "
                  f"market_value={b.market_value():,.2f} {b.currency}")
        # This branch used to return early, skipping the check below entirely —
        # so a file with unknown transaction types dumped happily with exit 0
        # while the normal listing exited 1 on the very same file.
        _report_problems(problems)
        sys.exit(1 if problems else 0)

    names = {b.portfolio for b in blocks}
    if ns.portfolio and ns.portfolio not in names:
        print(f"error: no portfolio named {ns.portfolio!r}. Found: "
              f"{', '.join(sorted(names))}", file=sys.stderr)
        sys.exit(2)

    print(f"file:    {ns.path}")
    print(f"columns: {len(header)}   blocks: {len(blocks)}   "
          f"transactions: {sum(len(b.txns) for b in blocks)}")
    links = cash_links(blocks)
    print(f"cash links: {len(links)} "
          f"({sum(1 for _, tgt in links if tgt is None)} unresolved)")
    _report_problems(problems)
    print()

    for pf, bs in sorted(by_portfolio(blocks).items()):
        if ns.portfolio and pf != ns.portfolio:
            continue
        print(f"{'=' * 78}\n[{pf}]  {len(bs)} symbols")
        for b in bs:
            flows = " ".join(f"{k}={v:,.2f}" for k, v in b.cash_flow_by_type().items())
            print(f"  {b.symbol:14s} {b.kind():9s} net={b.net_shares():>16,.4f} "
                  f"@{b.last_price:<12,.4f} mv={b.market_value():>16,.2f} "
                  f"{b.currency:4s} n={len(b.txns):<4d} {flows}")

    # Positions still printed — partial output is useful — but the exit code has
    # to say the result cannot be trusted, or a calling script treats it as good.
    if problems:
        print(f"\nexiting non-zero: {problems.summary()}")
        sys.exit(1)


if __name__ == "__main__":
    main()
