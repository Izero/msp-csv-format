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
import tempfile
import unittest

import msp_export_parser as P

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, "example-export.csv")

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
            _snapshot("Main", "USD=CASH"),
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
