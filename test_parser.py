#!/usr/bin/env python3
"""Test suite for msp_export_parser.

    python3 -m unittest -v
    python3 test_parser.py

Two kinds of test here. The first kind pins the documented format behaviour —
if one of these goes red, either the parser broke or the specification in
README.md is wrong, and both need looking at. The second kind pins the parser's
*failure* behaviour: this format's characteristic bug is silence, so a wrong
input has to produce a signal rather than a plausible-looking empty portfolio.

Copyright 2026 Izero. Apache-2.0 — see LICENSE.
"""
import os
import subprocess
import sys
import tempfile
import unittest

import msp_export_parser as P

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, "example-export.csv")
PARSER = os.path.join(HERE, "msp_export_parser.py")

HEADER = ",".join(P.COLUMNS_REFERENCE)


def _csv(*rows, header=HEADER):
    """Write a throwaway CSV and return its path."""
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", newline="") as f:
        f.write(header + "\n")
        for r in rows:
            f.write(r + "\n")
    return path


def _row(**kw):
    """One CSV row, addressed by column name so tests read as data not positions."""
    return ",".join(f'"{kw.get(c, "")}"' if kw.get(c, "") != "" else ""
                    for c in P.COLUMNS_REFERENCE)


def _snapshot(portfolio, symbol, price="10.00", rid="1"):
    return _row(Id=rid, Symbol=symbol, Portfolio=portfolio,
                Currency="USD", **{"Last Traded Price": price})


def _txn(portfolio, symbol, ttype, shares, rid="2", cost="1", date="2024-01-01 GMT+0800"):
    return _row(Id=rid, Symbol=symbol, Portfolio=portfolio, Type=ttype,
                Currency="USD", **{"Shares Owned": str(shares),
                                   "Cost Per Share": cost,
                                   "Transaction Date": date,
                                   "Last Traded Price": "10.00"})


class TestBundledExample(unittest.TestCase):
    """The synthetic export is the contract between README and the parser."""

    @classmethod
    def setUpClass(cls):
        cls.blocks, cls.header, cls.problems = P.parse(EXAMPLE)

    def test_every_expected_block_is_present(self):
        """Iterating only the blocks that showed up lets a truncated file pass."""
        found = {(b.portfolio, b.symbol) for b in self.blocks}
        self.assertEqual(found, set(P.EXPECTED_SELF_TEST),
                         "example-export.csv no longer matches EXPECTED_SELF_TEST")

    def test_positions(self):
        for b in self.blocks:
            with self.subTest(portfolio=b.portfolio, symbol=b.symbol):
                self.assertAlmostEqual(
                    b.net_shares(), P.EXPECTED_SELF_TEST[(b.portfolio, b.symbol)],
                    places=6)

    def test_example_exercises_every_known_type(self):
        """README claims the example covers every transaction type. Keep that true."""
        present = {t.ttype for b in self.blocks for t in b.txns}
        self.assertEqual(P.KNOWN_TYPES - present, set())

    def test_example_is_clean(self):
        self.assertFalse(self.problems, self.problems.summary())

    def test_cash_links_all_resolve(self):
        links = P.cash_links(self.blocks)
        self.assertTrue(links)
        self.assertEqual([src.id for src, tgt in links if tgt is None], [])


class TestDocumentedBehaviour(unittest.TestCase):
    """One test per claim the specification makes about position arithmetic."""

    def _net(self, *rows):
        path = _csv(*rows)
        try:
            blocks, _, _ = P.parse(path)
            return blocks[0].net_shares()
        finally:
            os.unlink(path)

    def test_sell_all_flattens_even_when_shares_is_zero(self):
        """§4, the expensive one. Subtracting 0 would leave the position intact."""
        self.assertEqual(self._net(
            _snapshot("Main", "ACME"),
            _txn("Main", "ACME", "Buy", 300, rid="2"),
            _txn("Main", "ACME", "Sell All", 0, rid="3")), 0.0)

    def test_sell_all_flattens_when_shares_holds_the_balance(self):
        """Same type, the other filling style. Both must flatten."""
        self.assertEqual(self._net(
            _snapshot("Main", "ACME"),
            _txn("Main", "ACME", "Buy", 300, rid="2"),
            _txn("Main", "ACME", "Sell All", 300, rid="3")), 0.0)

    def test_buy_to_cover_all_flattens_a_short(self):
        self.assertEqual(self._net(
            _snapshot("Margin", "EURUSD=X"),
            _txn("Margin", "EURUSD=X", "Sell Short", 50000, rid="2"),
            _txn("Margin", "EURUSD=X", "Buy to Cover All", 0, rid="3")), 0.0)

    def test_split_multiplies_by_the_ratio(self):
        """shares:cost = new:old."""
        self.assertEqual(self._net(
            _snapshot("Main", "ACME"),
            _txn("Main", "ACME", "Buy", 100, rid="2"),
            _txn("Main", "ACME", "Split", 2, rid="3", cost="1")), 200.0)

    def test_dividend_and_interest_do_not_move_the_position(self):
        self.assertEqual(self._net(
            _snapshot("Main", "ACME"),
            _txn("Main", "ACME", "Buy", 100, rid="2"),
            _txn("Main", "ACME", "Dividend", 45, rid="3"),
            _txn("Main", "ACME", "Interest", 12, rid="4")), 100.0)

    def test_negative_value_reverses_the_direction(self):
        """§4 sign convention. abs() here would give 90 instead of 110."""
        self.assertEqual(self._net(
            _snapshot("Main", "USD=CASH", price="1"),
            _txn("Main", "USD=CASH", "Buy", 100, rid="2"),
            _txn("Main", "USD=CASH", "Sell", -10, rid="3")), 110.0)

    def test_dividend_reinvest_adds(self):
        self.assertEqual(self._net(
            _snapshot("Margin", "EURUSD=X"),
            _txn("Margin", "EURUSD=X", "Sell Short", 1000, rid="2"),
            _txn("Margin", "EURUSD=X", "Dividend Reinvest", -50, rid="3")), -1050.0)

    def test_snapshot_row_carries_no_position(self):
        path = _csv(_snapshot("Main", "ACME", price="42.00"))
        try:
            blocks, _, _ = P.parse(path)
            self.assertEqual(blocks[0].net_shares(), 0.0)
            self.assertEqual(blocks[0].last_price, 42.0)
        finally:
            os.unlink(path)

    def test_market_value_needs_no_contract_multiplier(self):
        """§6: shares already holds value-per-point x contracts."""
        path = _csv(_snapshot("Main", "^GSPC", price="5200.00"),
                    _txn("Main", "^GSPC", "Buy", 250, rid="2"))
        try:
            blocks, _, _ = P.parse(path)
            self.assertEqual(blocks[0].market_value(), 250 * 5200.0)
            self.assertEqual(blocks[0].kind(), "index")
        finally:
            os.unlink(path)

    def test_blank_separator_rows_are_skipped_whatever_their_width(self):
        path = _csv(_snapshot("Main", "ACME"), ",,,", ",,,,,,,,,,,,,,,,,,,",
                    _txn("Main", "ACME", "Buy", 5, rid="2"))
        try:
            blocks, _, _ = P.parse(path)
            self.assertEqual(len(blocks), 1)
            self.assertEqual(blocks[0].net_shares(), 5.0)
        finally:
            os.unlink(path)

    def test_columns_resolve_by_name_not_position(self):
        """Column count changes between app versions; order must not be assumed."""
        cols = list(reversed(P.COLUMNS_REFERENCE))
        row = ",".join(f'"{"ACME" if c == "Symbol" else "Main" if c == "Portfolio" else ""}"'
                       for c in cols)
        path = _csv(row, header=",".join(cols))
        try:
            blocks, _, _ = P.parse(path)
            self.assertEqual((blocks[0].portfolio, blocks[0].symbol), ("Main", "ACME"))
        finally:
            os.unlink(path)


class TestFailsLoudly(unittest.TestCase):
    """This format's characteristic bug is silence. Every case here used to pass
    quietly and produce wrong numbers."""

    def test_wrong_file_raises_instead_of_parsing_to_nothing(self):
        """A three-column CSV once parsed 'successfully' into empty positions."""
        path = _csv('"1","FOO","Main"', header="Id,Symbol,Portfolio")
        try:
            with self.assertRaises(P.NotAnMspExport) as cm:
                P.parse(path)
            self.assertIn("Type", str(cm.exception))
        finally:
            os.unlink(path)

    def test_empty_file_raises(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            with self.assertRaises(P.NotAnMspExport):
                P.parse(path)
        finally:
            os.unlink(path)

    def test_unknown_transaction_type_is_reported(self):
        """A type the app might add later must not be skipped in silence."""
        path = _csv(_snapshot("Main", "ACME"),
                    _txn("Main", "ACME", "Buy", 100, rid="2"),
                    _txn("Main", "ACME", "Spinoff", 50, rid="3"))
        try:
            blocks, _, problems = P.parse(path)
            self.assertEqual(problems.unknown_types, {"Spinoff": 1})
            self.assertEqual(blocks[0].unknown_types(), ["Spinoff"])
            self.assertTrue(problems)
        finally:
            os.unlink(path)

    def test_unparseable_number_is_reported_not_swallowed(self):
        path = _csv(_snapshot("Main", "ACME"),
                    _txn("Main", "ACME", "Buy", "n/a", rid="2"))
        try:
            _, _, problems = P.parse(path)
            self.assertEqual(problems.unparseable, [("2", "Shares Owned", "n/a")])
        finally:
            os.unlink(path)

    def test_blank_number_is_not_a_problem(self):
        """An empty cell is a legitimate zero and must not be confused with a
        parse failure — that distinction is the whole point."""
        path = _csv(_snapshot("Main", "ACME"),
                    _txn("Main", "ACME", "Buy", "", rid="2"))
        try:
            _, _, problems = P.parse(path)
            self.assertEqual(problems.unparseable, [])
        finally:
            os.unlink(path)

    def test_duplicate_portfolio_symbol_pair_is_reported(self):
        path = _csv(_snapshot("Main", "ACME", rid="1"),
                    _txn("Main", "ACME", "Buy", 10, rid="2"),
                    _snapshot("Main", "ACME", rid="3"),
                    _txn("Main", "ACME", "Buy", 5, rid="4"))
        try:
            blocks, _, problems = P.parse(path)
            self.assertEqual(len(blocks), 2)
            self.assertEqual(problems.duplicate_pairs, [("Main", "ACME")])
        finally:
            os.unlink(path)

    def test_problems_is_falsy_when_clean(self):
        _, _, problems = P.parse(EXAMPLE)
        self.assertFalse(problems)
        self.assertEqual(problems.summary(), "none")

    def test_nan_and_infinity_are_rejected(self):
        """float() accepts both without complaint; a NaN then poisons every
        later sum and compares false against everything, so it slips past
        sanity checks rather than tripping them."""
        for bad in ("NaN", "Infinity", "-inf"):
            with self.subTest(value=bad):
                path = _csv(_snapshot("Main", "ACME"),
                            _txn("Main", "ACME", "Buy", bad, rid="2"))
                try:
                    blocks, _, problems = P.parse(path)
                    self.assertEqual(problems.unparseable, [("2", "Shares Owned", bad)])
                    self.assertEqual(blocks[0].net_shares(), 0.0)
                finally:
                    os.unlink(path)

    def test_every_column_the_parser_reads_is_required(self):
        """Dropping a column changes the answer instead of raising, because a
        name-based lookup resolves it to "". Measured: without Cost Per Share a
        2-for-1 split is skipped and a 150-share position reads as 50."""
        for column in P.REQUIRED_COLUMNS:
            with self.subTest(missing=column):
                cols = [c for c in P.COLUMNS_REFERENCE if c != column]
                path = _csv(header=",".join(cols))
                try:
                    with self.assertRaises(P.NotAnMspExport) as cm:
                        P.parse(path)
                    self.assertIn(column, str(cm.exception))
                finally:
                    os.unlink(path)

    def test_split_with_undefined_ratio_is_reported(self):
        """The last silent path in the parser. `shares:cost = new:old`, so a zero
        or blank denominator makes the ratio undefined; guarding the division by
        skipping the row leaves the position at its pre-split value, which is a
        plausible number with no error attached. Split is one of only two
        order-sensitive types, which makes silence here expensive."""
        for cost in ("", "0"):
            with self.subTest(cost=cost):
                path = _csv(_snapshot("Main", "ACME"),
                            _txn("Main", "ACME", "Buy", 100, rid="2"),
                            _txn("Main", "ACME", "Split", 2, rid="3", cost=cost))
                try:
                    blocks, _, problems = P.parse(path)
                    self.assertEqual(problems.unapplicable_splits,
                                     [("3", "Main", "ACME")])
                    self.assertTrue(problems)
                    self.assertEqual(blocks[0].net_shares(), 100.0,
                                     "position stays pre-split — hence the report")
                finally:
                    os.unlink(path)

    def test_split_with_a_negative_side_is_reported(self):
        """A truthiness guard lets -1 through: `Buy 100` then `Split 2:-1` used to
        read as a 200-share SHORT, exit 0. Same phantom short §4 warns about,
        different door. Both sides of the ratio must be positive."""
        for shares, cost in [(2, "-1"), (-2, "1"), (-2, "-1"), (0, "1")]:
            with self.subTest(shares=shares, cost=cost):
                path = _csv(_snapshot("Main", "ACME"),
                            _txn("Main", "ACME", "Buy", 100, rid="2"),
                            _txn("Main", "ACME", "Split", shares, rid="3", cost=cost))
                try:
                    blocks, _, problems = P.parse(path)
                    self.assertEqual(problems.unapplicable_splits,
                                     [("3", "Main", "ACME")])
                    self.assertEqual(blocks[0].net_shares(), 100.0,
                                     "must not invert the position")
                finally:
                    os.unlink(path)

    def test_valid_split_is_not_reported(self):
        path = _csv(_snapshot("Main", "ACME"),
                    _txn("Main", "ACME", "Buy", 100, rid="2"),
                    _txn("Main", "ACME", "Split", 2, rid="3", cost="1"))
        try:
            blocks, _, problems = P.parse(path)
            self.assertEqual(problems.unapplicable_splits, [])
            self.assertEqual(blocks[0].net_shares(), 200.0)
        finally:
            os.unlink(path)

    def test_thousands_separators_are_accepted(self):
        for raw, want in [("1,234", 1234.0), ("1,234,567", 1234567.0),
                          ("-1,234.56", -1234.56), ("999", 999.0)]:
            with self.subTest(raw=raw):
                errors = []
                self.assertEqual(P._num(raw, on_error=errors.append), want)
                self.assertEqual(errors, [])

    def test_comma_that_is_not_a_thousands_separator_is_rejected(self):
        """"1,5" is one-and-a-half under a comma-decimal locale. Stripping the
        comma turns it into fifteen — a tenfold error with no signal. §8 lists
        the locale assumption; this makes it audible instead of theoretical."""
        for raw in ("1,5", "1,23", "12,34", "1,2345"):
            with self.subTest(raw=raw):
                errors = []
                self.assertEqual(P._num(raw, on_error=errors.append), 0.0)
                self.assertEqual(errors, [raw])

    def test_unparseable_is_graded_by_column(self):
        """§8 records percentage strings in Commission as real export data. If
        that graded as an error, a healthy export would never exit 0 — which
        teaches people to ignore the exit code."""
        critical = _csv(_snapshot("Main", "ACME"),
                        _txn("Main", "ACME", "Buy", "n/a", rid="2"))
        try:
            _, _, problems = P.parse(critical)
            self.assertEqual(len(problems.unparseable), 1)
            self.assertTrue(problems, "Shares Owned decides a position")
        finally:
            os.unlink(critical)

        incidental = _csv(
            _snapshot("Main", "ACME"),
            _row(Id="2", Symbol="ACME", Portfolio="Main", Type="Buy", Currency="USD",
                 **{"Shares Owned": "100", "Cost Per Share": "1",
                    "Commission": "0.5%", "Last Traded Price": "10",
                    "Transaction Date": "2024-01-01 GMT+0800"}))
        try:
            blocks, _, problems = P.parse(incidental)
            self.assertEqual(problems.unparseable, [])
            self.assertEqual(len(problems.unparseable_incidental), 1)
            self.assertFalse(problems, "no position depends on Commission")
            self.assertEqual(blocks[0].net_shares(), 100.0)
        finally:
            os.unlink(incidental)

    def test_position_with_no_price_is_an_error(self):
        """Same consequence as a missing snapshot — market value 0 on a position
        that is not zero — so it gets the same treatment. Likelier in practice
        too: a delisted ticker leaves the price blank."""
        path = _csv(_row(Id="1", Symbol="ACME", Portfolio="Main", Currency="USD"),
                    _txn("Main", "ACME", "Buy", 100, rid="2"))
        try:
            blocks, _, problems = P.parse(path)
            self.assertEqual(problems.unpriced_positions,
                             [("Main", "ACME", 100.0, "blank")])
            self.assertEqual(blocks[0].market_value(), 0.0)
            self.assertTrue(problems)
        finally:
            os.unlink(path)

    def test_no_price_with_no_position_is_not_reported(self):
        """A flat block at price 0 says nothing wrong — nothing to value."""
        path = _csv(_row(Id="1", Symbol="ACME", Portfolio="Main", Currency="USD"))
        try:
            _, _, problems = P.parse(path)
            self.assertEqual(problems.unpriced_positions, [])
            self.assertFalse(problems)
        finally:
            os.unlink(path)

    def test_orphan_block_is_not_double_counted_as_unpriced(self):
        path = _csv(_txn("Main", "ACME", "Buy", 100, rid="2"))
        try:
            _, _, problems = P.parse(path)
            self.assertEqual(problems.orphan_blocks, [("Main", "ACME")])
            self.assertEqual(problems.unpriced_positions, [])
        finally:
            os.unlink(path)

    def test_total_commission_under_reports_on_non_numeric_cells(self):
        """Pinning the documented behaviour, not endorsing it: the accessor
        cannot warn, so its docstring points at problems.unparseable_incidental."""
        path = _csv(_snapshot("Main", "ACME"),
                    _row(Id="2", Symbol="ACME", Portfolio="Main", Type="Buy",
                         Currency="USD",
                         **{"Shares Owned": "100", "Cost Per Share": "1",
                            "Commission": "5%", "Last Traded Price": "10",
                            "Transaction Date": "2024-01-01 GMT+0800"}))
        try:
            blocks, _, problems = P.parse(path)
            self.assertEqual(blocks[0].total_commission(), 0.0)
            self.assertEqual(len(problems.unparseable_incidental), 1)
            self.assertIn("unparseable_incidental",
                          P.Block.total_commission.__doc__)
            self.assertEqual(blocks[0].unreadable_commissions(), ["2"],
                             "an API caller must be able to find this without "
                             "reaching into Problems")
        finally:
            os.unlink(path)

    def test_snapshot_without_identity_is_an_error(self):
        """A snapshot with no Symbol or Portfolio still opens a block, and its
        contents print as "[]  1 symbols"."""
        path = _csv(_row(Id="1", Symbol="", Portfolio="", Currency="USD",
                         **{"Last Traded Price": "10"}))
        try:
            _, _, problems = P.parse(path)
            self.assertEqual(problems.incomplete_snapshots,
                             [(2, ["Symbol", "Portfolio"])])
            self.assertTrue(problems)
        finally:
            os.unlink(path)

    def test_non_numeric_id_is_a_notice(self):
        """i.isdigit() excluded these from the ordering check, so they were
        invisible — but §1 records Id as a positive integer."""
        path = _csv(_snapshot("Main", "ACME", rid="abc"),
                    _txn("Main", "ACME", "Buy", 10, rid="xyz"))
        try:
            _, _, problems = P.parse(path)
            self.assertEqual([i for _, i in problems.non_numeric_ids], ["abc", "xyz"])
            self.assertFalse(problems)
        finally:
            os.unlink(path)

    def test_cross_portfolio_cash_link_is_a_notice(self):
        """§5 observed every pairing inside one portfolio. A crossing one moves
        no position, so it is worth saying without being an error."""
        path = _csv(
            _snapshot("A", "ACME"),
            _row(Id="2", Symbol="ACME", Portfolio="A", Type="Buy", Currency="USD",
                 **{"Shares Owned": "1", "Cost Per Share": "1", "Last Traded Price": "10",
                    "Transaction Date": "2024-01-01 GMT+0800", "OutgoingCashLink": "4"}),
            _snapshot("B", "USD=CASH", price="1", rid="3"),
            _row(Id="4", Symbol="USD=CASH", Portfolio="B", Type="Sell", Currency="USD",
                 **{"Shares Owned": "1", "Cost Per Share": "1", "Last Traded Price": "1",
                    "Transaction Date": "2024-01-01 GMT+0800"}))
        try:
            _, _, problems = P.parse(path)
            self.assertEqual(problems.cross_portfolio_links, [("2", "A", "B")])
            self.assertEqual(problems.unresolved_links, [])
            self.assertFalse(problems)
        finally:
            os.unlink(path)

    def test_duplicate_pairs_are_reported_but_not_an_error(self):
        """Documented format behaviour (README §8), handled correctly by keeping
        both blocks — so it must not make the file 'bad'."""
        path = _csv(_snapshot("Main", "ACME", rid="1"),
                    _txn("Main", "ACME", "Buy", 10, rid="2"),
                    _snapshot("Main", "ACME", rid="3"),
                    _txn("Main", "ACME", "Buy", 5, rid="4"))
        try:
            _, _, problems = P.parse(path)
            self.assertEqual(problems.duplicate_pairs, [("Main", "ACME")])
            self.assertFalse(problems, "a duplicate pair alone must not be an error")
        finally:
            os.unlink(path)


class TestStructuralIntegrity(unittest.TestCase):
    """Claims §1 makes about the file's shape, which the parser relies on."""

    def test_duplicate_ids_are_reported(self):
        """§1 states Id uniqueness as [Verified] and cash_links() trusts it — a
        dict keyed on Id keeps the last of any duplicates, silently."""
        path = _csv(_snapshot("Main", "ACME", rid="1"),
                    _txn("Main", "ACME", "Buy", 500, rid="9"),
                    _txn("Main", "ACME", "Buy", 700, rid="9"))
        try:
            _, _, problems = P.parse(path)
            self.assertEqual(problems.duplicate_ids, [("9", 2)])
            self.assertTrue(problems)
        finally:
            os.unlink(path)

    def test_short_row_is_skipped_and_reported(self):
        """A short row's missing columns read as "", so an absent Transaction
        Date turns it into a ghost block with an empty portfolio name."""
        path = _csv(_snapshot("Main", "ACME"), '"2","ACME"')
        try:
            blocks, _, problems = P.parse(path)
            self.assertEqual(len(blocks), 1, "no ghost block")
            self.assertEqual([w for _, w in problems.malformed_rows], [2])
        finally:
            os.unlink(path)

    def test_unresolved_cash_link_is_reported(self):
        path = _csv(_snapshot("Main", "ACME"),
                    _row(Id="2", Symbol="ACME", Portfolio="Main", Type="Buy",
                         Currency="USD",
                         **{"Shares Owned": "1", "Cost Per Share": "1",
                            "Transaction Date": "2024-01-01 GMT+0800",
                            "Last Traded Price": "10", "OutgoingCashLink": "999"}))
        try:
            _, _, problems = P.parse(path)
            self.assertEqual(problems.unresolved_links, [("2", "999")])
        finally:
            os.unlink(path)

    def test_transactions_without_a_snapshot_are_reported(self):
        """The comment on this branch used to say it failed loudly. It did not:
        the block got no price, so every market value in it was 0 — which reads
        like a real answer — and the file exited 0."""
        path = _csv(_txn("Main", "ACME", "Buy", 100, rid="2"))
        try:
            blocks, _, problems = P.parse(path)
            self.assertEqual(problems.orphan_blocks, [("Main", "ACME")])
            self.assertFalse(blocks[0].has_snapshot)
            self.assertEqual(blocks[0].market_value(), 0.0)
            self.assertTrue(problems)
        finally:
            os.unlink(path)

    def test_repeated_column_name_raises(self):
        """Lookup is by name, so a duplicate shadows the first occurrence. A
        second blank 'Transaction Date' made every transaction look like a
        snapshot: 22 blocks, 0 transactions, exit 0."""
        path = _csv(header=",".join(list(P.COLUMNS_REFERENCE) + ["Transaction Date"]))
        try:
            with self.assertRaises(P.NotAnMspExport) as cm:
                P.parse(path)
            self.assertIn("repeated column name", str(cm.exception))
        finally:
            os.unlink(path)

    def test_blank_id_is_reported(self):
        path = _csv(_snapshot("Main", "ACME"),
                    _txn("Main", "ACME", "Buy", 10, rid=""))
        try:
            _, _, problems = P.parse(path)
            self.assertEqual(len(problems.blank_ids), 1)
            self.assertTrue(problems)
        finally:
            os.unlink(path)

    def test_non_monotonic_id_is_a_notice_not_an_error(self):
        """§1 records Ids as increasing, but nothing here depends on it, so a
        violation says the file differs from the spec without making a number
        wrong."""
        path = _csv(_snapshot("Main", "ACME", rid="9"),
                    _txn("Main", "ACME", "Buy", 10, rid="3"))
        try:
            _, _, problems = P.parse(path)
            self.assertEqual(problems.non_monotonic_ids, [(9, 3)])
            self.assertFalse(problems, "a notice must not make the file bad")
        finally:
            os.unlink(path)

    def test_non_utf8_file_raises(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "wb") as f:
            f.write(HEADER.encode("utf-16"))
        try:
            with self.assertRaises(P.NotAnMspExport) as cm:
                P.parse(path)
            self.assertIn("not UTF-8", str(cm.exception))
        finally:
            os.unlink(path)


class TestSpecInvariants(unittest.TestCase):
    """Claims §2, §3 and §4 make, which the parser used to assume.

    The self-audit was uneven: §1 and §5 got checked, these did not.
    """

    def test_one_price_per_symbol(self):
        """§2 [Verified]. Break it and two portfolios value the same holding
        differently — anyone summing across portfolios gets a wrong total."""
        path = _csv(_row(Id="1", Symbol="ACME", Portfolio="Main", Currency="USD",
                         **{"Last Traded Price": "65"}),
                    _txn("Main", "ACME", "Buy", 100, rid="2"),
                    _row(Id="3", Symbol="ACME", Portfolio="Other", Currency="USD",
                         **{"Last Traded Price": "12"}),
                    _txn("Other", "ACME", "Buy", 100, rid="4"))
        try:
            _, _, problems = P.parse(path)
            self.assertEqual(problems.inconsistent_prices, [("ACME", [12.0, 65.0])])
            self.assertTrue(problems)
        finally:
            os.unlink(path)

    def test_cash_is_priced_at_par(self):
        """§3 [Official]: a cash position's price is always 1. At 3 the block's
        market value is triple the balance."""
        path = _csv(_row(Id="1", Symbol="USD=CASH", Portfolio="Main", Currency="USD",
                         **{"Last Traded Price": "3"}),
                    _txn("Main", "USD=CASH", "Buy", 1000, rid="2"))
        try:
            blocks, _, problems = P.parse(path)
            self.assertEqual(problems.cash_priced_off_par, [("Main", "USD=CASH", 3.0)])
            self.assertEqual(blocks[0].market_value(), 3000.0)
            self.assertTrue(problems)
        finally:
            os.unlink(path)

    def test_accounting_on_an_opening_row_is_a_notice(self):
        """Checking §2 against real data corrected the claim itself: Accounting
        is not sell-side only — Dividend and Interest carry it too. What holds is
        that rows which only open a position never do."""
        path = _csv(_snapshot("Main", "ACME"),
                    _row(Id="2", Symbol="ACME", Portfolio="Main", Type="Buy",
                         Currency="USD", Accounting="FIFO",
                         **{"Shares Owned": "1", "Cost Per Share": "1",
                            "Last Traded Price": "10",
                            "Transaction Date": "2024-01-01 GMT+0800"}))
        try:
            _, _, problems = P.parse(path)
            self.assertEqual([c for c, _ in problems.spec_deviations],
                             ["§2 Accounting appears on closing-side rows"])
            self.assertFalse(problems)
        finally:
            os.unlink(path)

    def test_date_and_cost_deviations_are_notices(self):
        path = _csv(_snapshot("Main", "ACME"),
                    _row(Id="2", Symbol="ACME", Portfolio="Main", Type="Dividend",
                         Currency="USD",
                         **{"Shares Owned": "5", "Cost Per Share": "89.7",
                            "Last Traded Price": "10",
                            "Transaction Date": "2024/01/01"}))
        try:
            _, _, problems = P.parse(path)
            claims = {c for c, _ in problems.spec_deviations}
            self.assertIn("§2 Transaction Date is 'YYYY-MM-DD GMT+HHMM'", claims)
            self.assertIn("§4 Cost Per Share on Dividend/Interest is 0 or 1", claims)
            self.assertFalse(problems)
        finally:
            os.unlink(path)


class TestCashLinkSemantics(unittest.TestCase):
    """§5 describes the pairings it saw rather than stating a rule. Writing the
    check showed the table was narrower than the data — Sell All and Sell Short
    carry links too. These two invariants hold across all of them."""

    def _linked(self, src_type, tgt_type, tgt_symbol="USD=CASH"):
        path = _csv(
            _snapshot("Main", "ACME"),
            _row(Id="2", Symbol="ACME", Portfolio="Main", Type=src_type,
                 Currency="USD",
                 **{"Shares Owned": "1", "Cost Per Share": "1",
                    "Last Traded Price": "10", "OutgoingCashLink": "4",
                    "Transaction Date": "2024-01-01 GMT+0800"}),
            _snapshot("Main", tgt_symbol, price="1", rid="3"),
            _row(Id="4", Symbol=tgt_symbol, Portfolio="Main", Type=tgt_type,
                 Currency="USD",
                 **{"Shares Owned": "1", "Cost Per Share": "1",
                    "Last Traded Price": "1",
                    "Transaction Date": "2024-01-01 GMT+0800"}))
        try:
            return [c for c, _ in P.parse(path)[2].spec_deviations]
        finally:
            os.unlink(path)

    def test_correct_pairings_are_silent(self):
        self.assertEqual(self._linked("Buy", "Sell"), [])
        self.assertEqual(self._linked("Sell", "Buy"), [])
        self.assertEqual(self._linked("Sell All", "Buy"), [])

    def test_wrong_direction_is_reported(self):
        self.assertEqual(self._linked("Buy", "Buy"),
                         ["§5 link direction matches the cash flow"])

    def test_target_must_be_a_cash_block(self):
        self.assertEqual(self._linked("Buy", "Sell", tgt_symbol="OTHER"),
                         ["§5 a cash link points at a =CASH block"])

    def test_display_symbol_should_be_empty(self):
        path = _csv(_row(Id="1", Symbol="ACME", Portfolio="Main", Currency="USD",
                         **{"Last Traded Price": "10", "Display Symbol": "ACME.US"}))
        try:
            _, _, problems = P.parse(path)
            self.assertEqual([c for c, _ in problems.spec_deviations],
                             ["§2 Display Symbol is empty in every export examined"])
            self.assertFalse(problems)
        finally:
            os.unlink(path)


class TestFloatingPointTolerance(unittest.TestCase):

    def test_a_decimal_position_closes_flat(self):
        """0.3 - 0.1 - 0.2 is -2.78e-17 in binary floating point. An exact
        `!= 0` test reads that as an open position."""
        path = _csv(_row(Id="1", Symbol="ACME", Portfolio="Main", Currency="USD"),
                    _txn("Main", "ACME", "Buy", 0.3, rid="2"),
                    _txn("Main", "ACME", "Sell", 0.1, rid="3"),
                    _txn("Main", "ACME", "Sell", 0.2, rid="4"))
        try:
            blocks, _, problems = P.parse(path)
            self.assertNotEqual(blocks[0].net_shares(), 0.0, "float residue is real")
            self.assertTrue(blocks[0].is_flat(), "but the position is closed")
            self.assertEqual(problems.unpriced_positions, [])
        finally:
            os.unlink(path)


class TestPriceZeroIsNotOneThing(unittest.TestCase):

    def _parse(self, price_cell):
        row = ({"Last Traded Price": price_cell} if price_cell is not None else {})
        path = _csv(_row(Id="1", Symbol="ACME", Portfolio="Main", Currency="USD", **row),
                    _txn("Main", "ACME", "Buy", 100, rid="2"))
        try:
            return P.parse(path)[2]
        finally:
            os.unlink(path)

    def test_unparseable_price_is_not_billed_twice(self):
        for raw in ("N/A", "0.0.0"):
            with self.subTest(raw=raw):
                problems = self._parse(raw)
                self.assertEqual(len(problems.unparseable), 1)
                self.assertEqual(problems.unpriced_positions, [],
                                 "already reported as unparseable")

    def test_zero_is_recognised_however_it_is_written(self):
        """The reason is decided by _num(), not by inspecting the string. A
        separate string test missed "0,000" and "0e0" and double-billed
        "0.0.0" — a second opinion on what counts as a number is a copy of the
        parsing rules, and it drifted."""
        for raw in ("0", "0.00", "0,000", "0e0", "+0", "-0.0"):
            with self.subTest(raw=raw):
                self.assertEqual(self._parse(raw).unpriced_positions,
                                 [("Main", "ACME", 100.0, "explicit 0")])

    def test_blank_price_says_blank(self):
        problems = self._parse(None)
        self.assertEqual(problems.unpriced_positions,
                         [("Main", "ACME", 100.0, "blank")])

    def test_explicit_zero_says_so(self):
        problems = self._parse("0")
        self.assertEqual(problems.unpriced_positions,
                         [("Main", "ACME", 100.0, "explicit 0")])


class TestCommandLineExitCodes(unittest.TestCase):
    """0 = clean, 1 = parsed but problems, 2 = input unusable.

    Exit codes are the contract for anything scripting this parser, and they are
    not reachable from the API tests above — every defect fixed in this class was
    an exit code that said "fine" about output that was not.
    """

    def _run(self, *args):
        return subprocess.run([sys.executable, PARSER, *args],
                              capture_output=True, text=True)

    def _tmp(self, *rows, header=HEADER):
        path = _csv(*rows, header=header)
        self.addCleanup(os.unlink, path)
        return path

    def test_clean_file_exits_zero(self):
        self.assertEqual(self._run(EXAMPLE).returncode, 0)

    def test_self_test_exits_zero(self):
        self.assertEqual(self._run("--self-test").returncode, 0)

    def test_no_arguments_exits_two(self):
        self.assertEqual(self._run().returncode, 2)

    def test_missing_file_exits_two(self):
        self.assertEqual(self._run("/nonexistent/nope.csv").returncode, 2)

    def test_wrong_schema_exits_two(self):
        path = self._tmp('"1","FOO","Main"', header="Id,Symbol,Portfolio")
        r = self._run(path)
        self.assertEqual(r.returncode, 2)
        self.assertIn("does not look like an MSP export", r.stderr)

    def test_unknown_option_exits_two(self):
        """A hand-rolled parser ignored these outright and ran the default listing."""
        r = self._run(EXAMPLE, "--bogus")
        self.assertEqual(r.returncode, 2)

    def test_unknown_portfolio_exits_two(self):
        r = self._run(EXAMPLE, "--portfolio", "NoSuchAccount")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no portfolio named", r.stderr)

    def test_raw_on_missing_block_exits_two(self):
        self.assertEqual(self._run(EXAMPLE, "--raw", "Nope", "NOPE").returncode, 2)

    def test_unknown_type_exits_one(self):
        path = self._tmp(_snapshot("Main", "ACME"),
                         _txn("Main", "ACME", "Spinoff", 50, rid="2"))
        r = self._run(path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("unrecognised transaction type", r.stdout)

    def test_raw_does_not_bypass_the_problem_check(self):
        """--raw used to return before the check, so the same file exited 0 here
        and 1 through the normal listing."""
        path = self._tmp(_snapshot("Main", "ACME"),
                         _txn("Main", "ACME", "Spinoff", 50, rid="2"))
        self.assertEqual(self._run(path, "--raw", "Main", "ACME").returncode, 1)

    def test_undefined_split_exits_one(self):
        path = self._tmp(_snapshot("Main", "ACME"),
                         _txn("Main", "ACME", "Buy", 100, rid="2"),
                         _txn("Main", "ACME", "Split", 2, rid="3", cost=""))
        r = self._run(path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("undefined ratio", r.stdout)

    def test_orphan_block_exits_one(self):
        path = self._tmp(_txn("Main", "ACME", "Buy", 100, rid="2"))
        r = self._run(path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no snapshot row", r.stdout)

    def test_repeated_header_exits_two(self):
        path = self._tmp(header=",".join(list(P.COLUMNS_REFERENCE) + ["Type"]))
        self.assertEqual(self._run(path).returncode, 2)

    def test_non_monotonic_id_exits_zero(self):
        path = self._tmp(_snapshot("Main", "ACME", rid="9"),
                         _txn("Main", "ACME", "Buy", 10, rid="3"))
        r = self._run(path)
        self.assertEqual(r.returncode, 0)
        self.assertIn("not monotonically increasing", r.stdout)

    def test_percentage_commission_exits_zero_with_a_note(self):
        path = self._tmp(
            _snapshot("Main", "ACME"),
            _row(Id="2", Symbol="ACME", Portfolio="Main", Type="Buy", Currency="USD",
                 **{"Shares Owned": "100", "Cost Per Share": "1", "Commission": "0.5%",
                    "Last Traded Price": "10",
                    "Transaction Date": "2024-01-01 GMT+0800"}))
        r = self._run(path)
        self.assertEqual(r.returncode, 0)
        self.assertIn("note:", r.stdout)

    def test_unpriced_position_exits_one(self):
        path = self._tmp(_row(Id="1", Symbol="ACME", Portfolio="Main", Currency="USD"),
                         _txn("Main", "ACME", "Buy", 100, rid="2"))
        r = self._run(path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("price of 0", r.stdout)

    def test_snapshot_without_identity_exits_one(self):
        path = self._tmp(_row(Id="1", Symbol="", Portfolio="", Currency="USD",
                              **{"Last Traded Price": "10"}))
        self.assertEqual(self._run(path).returncode, 1)

    def test_directory_argument_exits_two(self):
        """The exit-code table promises 2 for unreadable input; a directory
        raises IsADirectoryError, not FileNotFoundError."""
        r = self._run(HERE)
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot read", r.stderr)

    def test_non_utf8_exits_two(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "wb") as f:
            f.write(HEADER.encode("utf-16"))
        self.addCleanup(os.unlink, path)
        r = self._run(path)
        self.assertEqual(r.returncode, 2)
        self.assertIn("not UTF-8", r.stderr)

    def test_duplicate_pair_alone_exits_zero(self):
        path = self._tmp(_snapshot("Main", "ACME", rid="1"),
                         _txn("Main", "ACME", "Buy", 10, rid="2"),
                         _snapshot("Main", "ACME", rid="3"),
                         _txn("Main", "ACME", "Buy", 5, rid="4"))
        r = self._run(path)
        self.assertEqual(r.returncode, 0)
        self.assertIn("note:", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
