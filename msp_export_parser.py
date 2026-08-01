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


# An unparseable cell in one of these changes a number. Anywhere else it does
# not — Commission feeds only total_commission(), which no position depends on —
# and real exports genuinely contain non-numeric Commission cells (§8). Treating
# those as errors would mean a healthy export never exits 0, which trains people
# to ignore the exit code.
# Types that only open a position. Deciding which lot to draw from is a
# closing-side question, so an Accounting method here has nothing to act on.
# (§2 used to say "sell-side only"; real exports also carry it on Dividend and
# Interest rows, so the claim was narrower than the data. Opening rows are the
# part that actually held up.)
POSITION_OPENING_TYPES = frozenset({"Buy", "Sell Short"})

# Share counts are binary floats, so a position closed in decimal parts does not
# land on exactly 0: Buy 0.3, Sell 0.1, Sell 0.2 leaves -2.78e-17. An exact
# `!= 0` test reads that as an open position.
#
# ⚠ **1e-9 is an assumption, not a measurement.** It is comfortably above the
# residue that decimal arithmetic produces at the magnitudes this format carries
# (the example above is 1e-17, eight orders below) and comfortably below any
# holding anyone tracks — but the app documents no precision, so there is no
# derivation behind the exact figure. A genuine 5e-10 position would read as
# flat. If you deal in sizes that small, or if a future export turns out to
# carry more decimal places than observed here, change it: it is used only by
# `Block.is_flat()`, so the blast radius is one method.
POSITION_EPSILON = 1e-9

_DATE_FORMAT = re.compile(r"^\d{4}-\d{2}-\d{2} GMT[+-]\d{4}$")
_TIME_FORMAT = re.compile(r"^\d{2}:\d{2}:\d{2}$")

POSITION_CRITICAL_COLUMNS = frozenset({
    "Shares Owned", "Cost Per Share", "Last Traded Price"})


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


def _zero_price_reason(raw):
    """Why a price parsed to 0: "blank", "explicit 0", or None if unparseable.

    Asks `_num()` rather than inspecting the string, because a second opinion on
    what counts as a number is a copy of the parsing rules that will drift from
    them. The string version missed "0,000" and "0e0" (both parse to zero) and
    double-billed "0.0.0" as unparseable *and* an explicit zero.
    """
    if not raw.strip():
        return "blank"
    failed = []
    _num(raw, on_error=failed.append)
    if failed:
        return None                     # already reported as unparseable
    return "explicit 0"


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
    # The snapshot cell exactly as written, so callers can tell a blank price
    # (unknown) from an explicit "0" from something unparseable.
    last_price_raw: str = ""
    txns: list = field(default_factory=list)
    # False when transactions appeared with no snapshot row above them. Such a
    # block has no price, so every market value in it is 0 — worth knowing
    # before you sum them.
    has_snapshot: bool = True

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

    def is_flat(self):
        """True when the position is zero within floating-point tolerance.

        Use this rather than `net_shares() != 0`. Decimal share counts do not
        close to exactly zero in binary floating point — see POSITION_EPSILON.
        """
        return abs(self.net_shares()) < POSITION_EPSILON

    def unreadable_commissions(self):
        """Ids whose Commission cell is present but cannot be read as a number.

        Non-empty means `total_commission()` is understated, because those cells
        read as 0. This exists so an API caller can find out without reaching
        into `Problems`: the CLI prints a notice, an accessor cannot.
        """
        bad = []
        for txn in self.txns:
            _num(txn.raw.get("Commission", ""),
                 on_error=lambda _raw, _t=txn: bad.append(_t.id))
        return bad

    def unapplicable_splits(self):
        """`Split` rows whose ratio cannot be applied.

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
        """Sum of the Commission column across this block's transactions.

        ⚠ **Non-numeric cells read as 0, so this can under-report silently.**
        Real exports contain percentage strings in Commission (README §8), and
        the parser grades those as a *notice* rather than an error because no
        position depends on the column — which means a caller using this
        accessor never sees the warning a CLI user does. Check
        `problems.unparseable_incidental` before trusting the total.
        """
        return sum(t.commission for t in self.txns)
        # See unreadable_commissions() for whether this total is complete.

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

    Split in two. **Errors** mean a position or a market value may be wrong;
    `bool(problems)` reads only these, and the CLI exits non-zero on them.
    **Notices** leave positions and market values intact — documented format
    behaviour, or a deviation from what the specification records.

    ⚠ **"Leaves positions intact" is not "changes nothing".** A non-numeric
    `Commission` is a notice because no position depends on that column, but it
    still reads as 0, so `Block.total_commission()` under-reports. That
    accessor's docstring says so; this grading cannot, because a caller may
    never reach it. If you use a number outside position and market value,
    check `unparseable_incidental` yourself.

    **The grading is by column, not by each row's actual consequence.** A bad
    `Cost Per Share` on a `Buy` affects nothing (only `Split` reads it), and a
    bad `Shares Owned` on a `Sell All` is discarded by the flatten rule — both
    are errors anyway. Refining that would mean a table of which column matters
    for which type, kept in step with `net_shares()` by hand; a drifting copy of
    that logic is a worse failure than a conservative error. Measured against
    real exports, the conservative version produces no false alarms.

    The point of collecting any of it is that every failure mode here is
    otherwise completely silent — the file parses, the exit code is 0, and the
    numbers are simply incorrect.
    """
    # --- errors: something is wrong and the numbers may be affected ---
    unparseable: list = field(default_factory=list)   # (row_id, column, raw_value)
    unknown_types: dict = field(default_factory=dict)  # type name -> row count
    unapplicable_splits: list = field(default_factory=list)  # (row_id, portfolio, symbol)
    malformed_rows: list = field(default_factory=list)  # (line_number, cell_count)
    duplicate_ids: list = field(default_factory=list)   # (id, occurrences)
    blank_ids: list = field(default_factory=list)      # line numbers
    orphan_blocks: list = field(default_factory=list)  # (portfolio, symbol)
    incomplete_snapshots: list = field(default_factory=list)  # (line, [fields])
    unpriced_positions: list = field(default_factory=list)  # (portfolio, symbol, net)
    inconsistent_prices: list = field(default_factory=list)  # (symbol, [prices])
    cash_priced_off_par: list = field(default_factory=list)  # (portfolio, symbol, price)
    unresolved_links: list = field(default_factory=list)  # (source_id, target_id)

    # --- notices: documented behaviour, or deviations that change nothing ---
    unparseable_incidental: list = field(default_factory=list)  # (row_id, col, raw)
    duplicate_pairs: list = field(default_factory=list)  # (portfolio, symbol)
    non_monotonic_ids: list = field(default_factory=list)  # (previous, current)
    non_numeric_ids: list = field(default_factory=list)  # (line, id)
    cross_portfolio_links: list = field(default_factory=list)  # (src_id, from, to)
    spec_deviations: list = field(default_factory=list)  # (claim, detail)

    def __bool__(self):
        """True when something is actually *wrong*.

        `duplicate_pairs` is deliberately excluded. A (Portfolio, Symbol) pair
        occupying two blocks is documented format behaviour (README §8), not a
        defect, and this parser handles it correctly by keeping both. It is
        reported so that callers who key on the pair are not caught out, but a
        file containing one is not a bad file.
        """
        return bool(self.unparseable or self.unknown_types
                    or self.unapplicable_splits or self.malformed_rows
                    or self.duplicate_ids or self.blank_ids
                    or self.orphan_blocks or self.unresolved_links
                    or self.incomplete_snapshots or self.unpriced_positions
                    or self.inconsistent_prices or self.cash_priced_off_par)

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
        if self.blank_ids:
            bits.append(f"{len(self.blank_ids)} row(s) with a blank Id")
        if self.orphan_blocks:
            bits.append(f"{len(self.orphan_blocks)} block(s) with no snapshot row")
        if self.incomplete_snapshots:
            bits.append(f"{len(self.incomplete_snapshots)} snapshot row(s) missing "
                        f"an identifying field")
        if self.unpriced_positions:
            bits.append(f"{len(self.unpriced_positions)} position(s) with no price")
        if self.inconsistent_prices:
            bits.append(f"{len(self.inconsistent_prices)} symbol(s) priced "
                        f"inconsistently")
        if self.cash_priced_off_par:
            bits.append(f"{len(self.cash_priced_off_par)} cash block(s) not priced at 1")
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

    repeated = sorted({n for n, c in Counter(header).items() if c > 1})
    if repeated:
        # A dict keyed on column name keeps the last of any duplicate, so a second
        # (blank) "Transaction Date" made every transaction row look like a
        # snapshot: 22 blocks, 0 transactions, exit 0.
        raise NotAnMspExport(
            f"{path} has repeated column name(s): {', '.join(repeated)}. "
            f"Column lookup is by name, so a duplicate silently shadows the "
            f"first occurrence.")

    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise NotAnMspExport(
            f"{path} does not look like an MSP export. Missing column(s): "
            f"{', '.join(missing)}. Found {len(header)} column(s): "
            f"{', '.join(header) if header else '(none)'}")

    idx = {name: i for i, name in enumerate(header)}
    problems = Problems()

    def get(row, name):
        i = idx.get(name)
        if i is None or i >= len(row):
            return ""
        return row[i].strip()

    def num(row, name):
        def record(raw):
            entry = (get(row, "Id") or "?", name, raw)
            if name in POSITION_CRITICAL_COLUMNS:
                problems.unparseable.append(entry)
            else:
                problems.unparseable_incidental.append(entry)
        return _num(get(row, name), on_error=record)

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
        all_ids.append((line_no, get(row, "Id")))
        if get(row, "Display Symbol"):
            problems.spec_deviations.append(
                ("§2 Display Symbol is empty in every export examined",
                 f"line {line_no}: {get(row, 'Display Symbol')!r}"))
        if not get(row, "Transaction Date"):
            blank = [c for c in ("Symbol", "Portfolio") if not get(row, c)]
            if blank:
                # A snapshot with no Symbol or Portfolio still opens a block, and
                # everything beneath it lands in a nameless bucket that prints as
                # "[]  1 symbols" and exits 0.
                problems.incomplete_snapshots.append((line_no, blank))
            current = Block(
                portfolio=get(row, "Portfolio"),
                symbol=get(row, "Symbol"),
                name=get(row, "Name"),
                exchange=get(row, "Exchange"),
                currency=get(row, "Currency"),
                last_price=num(row, "Last Traded Price"),
                last_price_raw=get(row, "Last Traded Price"),
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
            # Transactions with no snapshot row above them. The block has no
            # price, so its market value is 0 — which looks like a real answer.
            # The comment here used to claim this failed loudly; it did not.
            current = Block(portfolio=txn.portfolio, symbol=txn.symbol,
                            name="", exchange="", currency=txn.currency,
                            last_price=0.0, has_snapshot=False)
            blocks.append(current)
            problems.orphan_blocks.append((txn.portfolio, txn.symbol))
        if txn.accounting and txn.ttype in POSITION_OPENING_TYPES:
            problems.spec_deviations.append(
                ("§2 Accounting appears on closing-side rows",
                 f"Id {txn.id}: {txn.ttype} carries {txn.accounting!r}"))
        raw_date = txn.raw.get("Transaction Date", "")
        if not _DATE_FORMAT.match(raw_date):
            problems.spec_deviations.append(
                ("§2 Transaction Date is 'YYYY-MM-DD GMT+HHMM'",
                 f"Id {txn.id}: {raw_date!r}"))
        if txn.time and not _TIME_FORMAT.match(txn.time):
            problems.spec_deviations.append(
                ("§2 Transaction Time is 'HH:MM:SS'", f"Id {txn.id}: {txn.time!r}"))
        if txn.ttype in CASH_ONLY_TYPES and txn.cost not in (0.0, 1.0):
            problems.spec_deviations.append(
                ("§4 Cost Per Share on Dividend/Interest is 0 or 1",
                 f"Id {txn.id}: {txn.cost}"))
        current.txns.append(txn)

    # §1 states Id uniqueness within a file as [Verified]. cash_links() trusts it
    # — a dict keyed on Id silently keeps the last of any duplicates — so a file
    # that breaks the claim is not the thing the specification describes.
    problems.duplicate_ids = [(i, n) for i, n in Counter(i for _, i in all_ids).items()
                              if n > 1 and i]
    problems.blank_ids = [line for line, i in all_ids if not i]
    # §1 also claims Ids increase monotonically. Nothing here depends on that, so
    # a violation is reported as a notice: it means the file is not quite what the
    # specification describes, without making any number wrong.
    problems.non_numeric_ids = [(line, i) for line, i in all_ids
                                if i and not i.isdigit()]
    numeric = [(line, int(i)) for line, i in all_ids if i.isdigit()]
    problems.non_monotonic_ids = [(numeric[k - 1][1], numeric[k][1])
                                  for k in range(1, len(numeric))
                                  if numeric[k][1] <= numeric[k - 1][1]]

    # A snapshot row with a blank price has exactly the consequence orphan_blocks
    # exists to report — market value 0 on a position that is not zero, which
    # reads like an answer. `has_snapshot` filters out the orphans, which have
    # their own bucket. This is likelier than a missing snapshot in practice:
    # delisted tickers and symbols the quote source dropped both go blank.
    # Three ways a price can be 0, and they do not deserve the same treatment.
    # An unparseable cell is already reported as such, so repeating it here would
    # bill one mistake twice; blank means unknown; an explicit "0" is a claim the
    # file is making. The last two are worth saying, with which one it was.
    problems.unpriced_positions = [
        (b.portfolio, b.symbol, b.net_shares(), reason)
        for b, reason in ((b, _zero_price_reason(b.last_price_raw)) for b in blocks)
        if b.has_snapshot and b.last_price == 0 and not b.is_flat()
        and reason is not None]

    # §2 [Verified]: one price per symbol across the whole file. Break it and two
    # portfolios holding the same instrument value it differently — anyone summing
    # across portfolios (which §8.6 already warns about for double counting) gets a
    # wrong total with nothing to show for it.
    by_symbol = defaultdict(set)
    for b in blocks:
        if b.last_price:
            by_symbol[b.symbol].add(b.last_price)
    problems.inconsistent_prices = [(s, sorted(v)) for s, v in by_symbol.items()
                                    if len(v) > 1]

    # §3 [Official]: a cash position's price is always 1. At any other value the
    # block's market value is a multiple of the balance it is supposed to be.
    problems.cash_priced_off_par = [
        (b.portfolio, b.symbol, b.last_price) for b in blocks
        if b.symbol.endswith("=CASH") and b.last_price not in (0.0, 1.0)]

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
    resolved = cash_links(blocks)
    problems.unresolved_links = [(src.id, src.cash_link)
                                 for src, tgt in resolved if tgt is None]
    # §5 records every pairing in the sample sitting inside one portfolio, with no
    # cross-portfolio link observed at all. One that crosses moves no position, so
    # it is a notice — but the cash-flow reading of §5 does not hold for that file.
    problems.cross_portfolio_links = [
        (src.id, src.portfolio, tgt.portfolio) for src, tgt in resolved
        if tgt is not None and src.portfolio != tgt.portfolio]

    # §5's table describes the pairings it observed rather than stating a rule,
    # and checking it showed the table is narrower than the data: eleven exports
    # contain six source types, not the four listed (Sell All and Sell Short also
    # carry links). Two things do hold across all 1798 of them, so those are what
    # gets checked:
    #   - the target is always a =CASH block
    #   - a Buy source pairs with Sell CASH (money out); every other source pairs
    #     with Buy CASH (money in)
    for src, tgt in resolved:
        if tgt is None:
            continue
        if not tgt.symbol.endswith("=CASH"):
            problems.spec_deviations.append(
                ("§5 a cash link points at a =CASH block",
                 f"Id {src.id}: → {tgt.symbol} ({tgt.ttype})"))
            continue
        expected = "Sell" if src.ttype == "Buy" else "Buy"
        if tgt.ttype != expected:
            problems.spec_deviations.append(
                ("§5 link direction matches the cash flow",
                 f"Id {src.id}: {src.ttype} → {tgt.ttype} {tgt.symbol}, "
                 f"expected {expected}"))

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

    ⚠ **Snapshot rows consume an Id (§1) but are not transactions, so they are
    not resolution targets here.** A link pointing at a snapshot row's Id would
    therefore come back unresolved. Across eleven real exports — 1798 links —
    that never happened, and neither did a link pointing at an Id absent from
    the file. Both remain observations rather than guarantees.
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
        print(f"\n{prefix} {len(problems.unapplicable_splits)} Split row(s) whose "
              f"ratio cannot be applied — 'Shares Owned' and 'Cost Per Share' must "
              f"BOTH be positive (blank, zero or negative on either side). The split "
              f"was NOT applied, so the position is stuck at its pre-split value:")
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
              f"did not resolve to a transaction in this file — the Id is either "
              f"absent, or belongs to a snapshot row, which is not a transaction. "
              f"Positions are unaffected; the cash pairing is not (§5):")
        for src_id, target in problems.unresolved_links[:10]:
            print(f"    Id {src_id} → {target!r} (not found)")
    if problems.orphan_blocks:
        n += len(problems.orphan_blocks)
        print(f"\n{prefix} {len(problems.orphan_blocks)} block(s) have transactions "
              f"but no snapshot row above them. Those blocks carry no price, so "
              f"every market value in them is 0 — which reads like a real answer:")
        for pf, sym in problems.orphan_blocks[:10]:
            print(f"    {pf} / {sym}")
    if problems.unpriced_positions:
        n += len(problems.unpriced_positions)
        print(f"\n{prefix} {len(problems.unpriced_positions)} block(s) hold a "
              f"non-zero position at a price of 0, so their market value reads 0. "
              f"'blank' means the snapshot cell was empty (unknown); "
              f"'explicit 0' means the file states zero:")
        for pf, sym, net, why in problems.unpriced_positions[:10]:
            print(f"    {pf} / {sym}: net {net:,.4f}, price {why}")
    if problems.inconsistent_prices:
        n += len(problems.inconsistent_prices)
        print(f"\n{prefix} {len(problems.inconsistent_prices)} symbol(s) carry more "
              f"than one price in this file. §2 records one price per symbol; "
              f"without it, summing a holding across portfolios is wrong:")
        for sym, prices in problems.inconsistent_prices[:10]:
            print(f"    {sym}: {', '.join(str(x) for x in prices)}")
    if problems.cash_priced_off_par:
        n += len(problems.cash_priced_off_par)
        print(f"\n{prefix} {len(problems.cash_priced_off_par)} cash block(s) priced "
              f"at something other than 1 (§3). Their market value is a multiple "
              f"of the balance:")
        for pf, sym, price in problems.cash_priced_off_par[:10]:
            print(f"    {pf} / {sym} @ {price}")
    if problems.blank_ids:
        n += len(problems.blank_ids)
        print(f"\n{prefix} {len(problems.blank_ids)} row(s) have a blank Id. §1 "
              f"states every row consumes a unique Id, and cash-link resolution "
              f"needs it:")
        print(f"    line(s) {', '.join(str(x) for x in problems.blank_ids[:15])}")
    if problems.incomplete_snapshots:
        n += len(problems.incomplete_snapshots)
        print(f"\n{prefix} {len(problems.incomplete_snapshots)} snapshot row(s) with "
              f"no Symbol or no Portfolio. They still open a block, so everything "
              f"beneath them lands in a bucket with no name:")
        for line_no, fields in problems.incomplete_snapshots[:10]:
            print(f"    line {line_no}: blank {', '.join(fields)}")
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
    # Notices below: nothing here makes a number wrong.
    if problems.unparseable_incidental:
        cols = sorted({c for _, c, _ in problems.unparseable_incidental})
        print(f"\nnote: {len(problems.unparseable_incidental)} non-numeric cell(s) "
              f"in {', '.join(cols)} — read as 0. No position depends on these "
              f"columns; real exports do contain such values (§8):")
        for row_id, col, raw in problems.unparseable_incidental[:5]:
            print(f"    Id {row_id} {col}: {raw!r}")
        if len(problems.unparseable_incidental) > 5:
            print(f"    ... and {len(problems.unparseable_incidental) - 5} more")
    if problems.spec_deviations:
        from collections import Counter as _C
        grouped = _C(claim for claim, _ in problems.spec_deviations)
        rows = len({detail.split(":")[0] for _, detail in problems.spec_deviations})
        print(f"\nnote: {len(problems.spec_deviations)} deviation(s) from what the "
              f"specification records, across {rows} row(s). No number is affected:")
        for claim, count in grouped.most_common():
            first = next(d for c, d in problems.spec_deviations if c == claim)
            print(f"    {claim} — {count} row(s), first: {first}")
    if problems.non_numeric_ids:
        print(f"\nnote: {len(problems.non_numeric_ids)} Id(s) are not integers "
              f"(first: {problems.non_numeric_ids[0][1]!r} on line "
              f"{problems.non_numeric_ids[0][0]}). §1 records Id as a positive "
              f"integer; these are excluded from the ordering check.")
    if problems.cross_portfolio_links:
        print(f"\nnote: {len(problems.cross_portfolio_links)} cash link(s) cross "
              f"portfolios. §5 records every pairing in the sample staying inside "
              f"one portfolio, so this file reads differently for cash flow:")
        for src_id, a, b in problems.cross_portfolio_links[:5]:
            print(f"    Id {src_id}: {a} → {b}")
    if problems.non_monotonic_ids:
        first_prev, first_cur = problems.non_monotonic_ids[0]
        print(f"\nnote: Id is not monotonically increasing "
              f"({len(problems.non_monotonic_ids)} place(s), first at "
              f"{first_prev} → {first_cur}). §1 records it as increasing; nothing "
              f"in this parser depends on that, so this is informational.")
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
