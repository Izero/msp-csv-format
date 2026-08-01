# MSP CSV Export Format

An unofficial specification for the CSV file exported by **My Stocks Portfolio &
Market** (MSP, by Peeksoft), plus a dependency-free reference parser.

- iOS App Store ID `923544282` · Android package `co.peeksoft.stocks`
- <https://mystocksportfolio.app> · <https://help.mystocksportfolio.app>
- **Derived from exports produced by iOS version 2.522.0**

## Why this exists

Credit first: MSP is an unusually capable portfolio tracker. Multi-currency
accounting, short positions, four cost-basis methods (FIFO / LIFO / weighted
average / specific lots), futures, a manual FX override on individual
transactions — that is a deeper feature set than most personal finance apps
attempt, and it is the reason the export is worth parsing at all. The developers
also publish the app's translation strings openly at
<https://github.com/mystocksportfolio/translations>, which is rare, and which
turned out to be the single most useful piece of evidence for what these columns
mean.

The gap is documentation of the export itself. The official help center confirms
that CSV import and export exist, but publishes **no field-level specification**
for the export. The only official CSV column documentation is the in-app string
`csvImport_recognizedHeadersHelp`, and that describes an 8-column *import* format
(Symbol / Portfolio / Shares / Type / Price / Commission / Date / Notes) — a
different thing from the 19–20 column *export*.

So if you want to read your own exported data — move it into a spreadsheet, feed
it to a tax tool, reconcile it against a broker statement — you work the format
out yourself first. This document is that work, written down, so the next person
doesn't have to repeat it. Everything here was inferred by cross-checking
exports against each other and against what the app displays — a single real
portfolio, ~6,900 transactions across ~36 portfolios, 11 weekly exports, plus the
public translation strings. Nothing was taken from a specification, because there
isn't one.

Every claim is tagged with how it is known:

| Tag | Meaning |
|---|---|
| **[Official]** | Stated in the official help pages or the public string file at <https://github.com/mystocksportfolio/translations> |
| **[Verified]** | Reproduced against real export data; the arithmetic checks out |
| **[Convention]** | A user bookkeeping habit the format permits, not a rule the app enforces |
| **[Unconfirmed]** | None of the above — still a guess |

## Quick start

```bash
python3 msp_export_parser.py --self-test
```

That parses the bundled `example-export.csv` (a synthetic file covering every
transaction type and every trap below) and checks the derived positions:

```
parsed 5 blocks, 14 transactions, 20 columns

  ok   Main     ACME       net=      150.00
  ok   Main     GLOBEX.L   net=        0.00
  ok   Main     USD=CASH   net=   -4,956.00
  ok   Margin   EURUSD=X   net=  -70,250.00
  ok   Main     ^GSPC      net=      250.00
```

Against your own export:

```bash
python3 msp_export_parser.py path/to/MSP-Portfolios-YYYY-MM-DD.csv
python3 msp_export_parser.py path/to/export.csv --portfolio Main
python3 msp_export_parser.py path/to/export.csv --raw Margin EURUSD=X
```

Python 3.8+, standard library only.

## 1. File structure

```
Id,Symbol,Name,...,OutgoingCashLink       <- header (1 line)
,,,,,,,,,,,,,,,,,,,                       <- blank row
"1","ACME",...,,,,,,,,,                   <- snapshot row (no Transaction Date)
"2","ACME",...,"2024-03-15 GMT+0800",...  <- transaction row
"3","ACME",...                            <- transaction row
,,,,,,,,,,,,,,,,,,,                       <- blank row (block separator)
"6","GLOBEX.L",...                        <- next block's snapshot row
```

- **A block is one `(Portfolio, Symbol)` pair.**
- The first row of each block is a **snapshot row**: `Transaction Date` is empty,
  and it carries `Name` / `Exchange` / `Currency` / `Last Traded Price`.
  **It does not carry a position** — `Shares Owned` is blank. The position must be
  derived by replaying the transactions beneath it. There is no shortcut. [Verified]
- Blocks are separated by blank rows. The number of commas in a blank row is not
  fixed, so skip any row where every cell is empty rather than matching an exact
  width. [Verified]
- `Id` is unique and monotonically increasing **within one file**; snapshot rows
  consume an Id too. It is **renumbered between exports** — see §7. [Verified]

**Column count varies by app version.** A 2026-05 export had 19 columns; a 2026-07
export had 20, the new one being `Purchase Exchange Currencies` (empty throughout).
**Resolve columns by header name. Never hardcode indices.** [Verified]

## 2. Columns

| Column | Source | Meaning |
|---|---|---|
| `Id` | [Verified] | Unique row number within the file. `OutgoingCashLink` points at it. **Not stable across exports** (§7) |
| `Symbol` | [Verified] | Yahoo Finance ticker, or one of the virtual symbols in §3 |
| `Name` | [Verified] | Instrument name; may be empty |
| `Display Symbol` | [Verified] | Empty in every export examined, both versions |
| `Exchange` | [Verified] | Exchange code; may be empty |
| `Portfolio` | [Verified] | Account name, free text, chosen by the user |
| `Currency` | [Verified] | The instrument's quote currency |
| `Last Traded Price` | [Verified] | Price at export time. **The same symbol shares one price across the whole file**, regardless of which portfolio holds it |
| `Shares Owned` | [Verified] | **Meaning depends on `Type`** (§4). It is not "shares currently held" — it is this row's quantity *or amount* |
| `Cost Per Share` | [Verified] | Execution price for this row. On `Dividend` / `Interest` rows it is 0 or 1 and carries no information |
| `Commission` | [Verified] | Commission for this row |
| `Transaction Date` | [Verified] | `YYYY-MM-DD GMT+HHMM`. **Empty means this is a snapshot row** |
| `Transaction Time` | [Verified] | `HH:MM:SS` |
| `Purchase Exchange Rate` | [Official] | Manual FX override for this transaction (instrument currency → home currency). Empty means the app's automatic rate was used |
| `Purchase Exchange Currencies` | [Unconfirmed] | Added in the 20-column version; empty in all data examined |
| `Type` | [Official]+[Verified] | Transaction type — see §4 |
| `Accounting` | [Official] | Cost-basis method: `FIFO` (default) / `LIFO` / `Weighted Average` / `Specific Lots`. **Only appears on sell-side rows** |
| `Accounting Execution Ids` | [Unconfirmed] | Lot identifiers used with `Specific Lots`. Almost always empty |
| `Notes` | [Verified] | Free text. In practice this is where the *intent* of a transaction lives — FX conversion details, interest rates, roll arithmetic |
| `OutgoingCashLink` | [Verified] | The `Id` of a paired cash transaction — see §5 |

## 3. Virtual symbols

Three symbol shapes are not real tradable instruments:

| Shape | Example | Meaning |
|---|---|---|
| `XXX=CASH` | `USD=CASH` | [Official] A cash position in that currency. The official string `portfolio_thisIsCurrencyCashPosition` reads "This is a %s cash position". `Last Traded Price` is always 1 |
| `XXXYYY=X` | `EURUSD=X` | Yahoo FX pair. Tradable in the app like any instrument, which makes it usable for FX bookkeeping (§4, `Sell Short`) |
| `^INDEX` | `^GSPC` | An index. Not tradable in reality, but usable as a stand-in for a futures position (§6) |

Real futures tickers (`ES=F`, `ZF=F`) and Yahoo mutual-fund codes (`0P…`) also
appear and behave like ordinary securities.

## 4. Transaction types

Ten values appear in real exports. The official help documents seven —
**`Interest`, `Sell All`, and `Buy to Cover All` are not mentioned anywhere in the
official documentation.** Their semantics below are from data.

| Type | Effect on position | `Shares Owned` holds | Source |
|---|---|---|---|
| `Buy` | **+shares** | share count | [Official] |
| `Sell` | **−shares** | share count | [Official] |
| `Sell Short` | **−shares** | share count | [Official] (UI label is "Short") |
| `Buy to Cover` | **+shares** | share count | [Official] |
| `Sell All` | **flatten to zero** | **unreliable — do not use** | [Verified] |
| `Buy to Cover All` | **flatten to zero** | **unreliable — do not use** | [Verified] |
| `Dividend Reinvest` | **+shares** | share count | [Verified] |
| `Dividend` | **none** | **cash amount** | [Verified] |
| `Interest` | **none** | **cash amount** | [Verified] |
| `Split` | **× shares ÷ cost** | numerator of the ratio | [Verified] |

### The `Sell All` trap

**This is the most expensive mistake available in this format.**

`Sell All` and `Buy to Cover All` flatten the position unconditionally. The
`Shares Owned` column on those rows cannot be used as a delta. Across 105 such
rows in one real export:

| What `Shares Owned` contained | Rows |
|---|---|
| `0` — no information at all | 100 |
| the exact pre-close balance | 4 |
| the balance off by a rounding tail | 1 |

The same column, on the same transaction type, is filled two incompatible ways.
Only the "flatten" reading works for both.

Treating it as a delta corrupts the portfolio **silently**. In one measured case
it produced phantom positions in **50 different instruments** — some going
negative (a nonexistent short position worth six figures), others left holding
stock that had been fully sold, because subtracting the common `0` leaves the
position untouched.

Two cases that settle it:

- A `Sell All` row whose Notes recorded a full FX conversion of the entire
  balance. Subtraction left a residual short; the position was demonstrably closed.
- A `Sell All` row with `shares = 0` on an instrument with several hundred shares
  outstanding. Subtracting 0 leaves the whole position in place forever.

The bundled `example-export.csv` includes this case (`Main / GLOBEX.L`) so you can
check your own implementation against it.

### `Dividend` and `Interest` hold amounts, not share counts

Neither type moves the position. `Shares Owned` carries a **cash amount** —
positive for income, negative for an expense. `Cost Per Share` on these rows is 0
or 1 and means nothing; do not multiply by it. [Verified]

Because the app has no dedicated field for fees, taxes, or futures roll
differentials, a common convention is to book them as negative `Dividend` or
negative `Interest` rows against the relevant instrument. [Convention] That keeps
the cost basis of the underlying position continuous instead of breaking it into
segments. If you are computing income, filter on the sign — or you will net real
dividends against commission.

### `Dividend Reinvest` direction

The direction is **+1**, the same as `Buy`. [Verified]

This one needed a three-way reconciliation to settle, because the rows in the
sample were all *negative* — the type was being used to capitalise accrued
interest into a loan principal, not to reinvest a dividend.

The check: the same liability was tracked in two independent ways in the same
file — once as a short FX pair (§3) and once as a `XXX=CASH` balance. Aligning
both to the same date, the difference between them was exactly the `Dividend
Reinvest` row's `shares`. With direction +1 the two bookkeeping styles reconcile
to 0.00; with −1 they diverge by twice the amount.

## 5. `OutgoingCashLink`

An `Id` pointing at the paired cash-side transaction. [Verified] The pairings that
occur:

| Security side | → Cash side |
|---|---|
| `Buy` a security | `Sell XXX=CASH` (cash out) |
| `Sell` a security | `Buy XXX=CASH` (cash in) |
| `Interest` on cash | `Buy XXX=CASH` |
| `Dividend` on a security | `Buy XXX=CASH` |

**Both sides are always in the same portfolio.** No cross-portfolio pairing was
observed in any export.

This matches the official UI strings: `withdrawCashFromPortfolioToPurchase`,
`depositCashToPortfolioFromSale`, and `portfolio_link_cashFound` ("Linked cash
transaction found").

⚠ **Coverage is low.** In the sample, only 248 of ~6,900 transactions carried a
link — the cash leg of everything else was recorded by hand with no
machine-readable relationship. Any cash-flow analysis built on this column alone
will see a small fraction of the actual flows.

## 6. Futures and index positions

For futures and index blocks, `Shares Owned` stores **value per point × number of
contracts**. [Verified] The useful consequence:

```
shares × last_traded_price == notional value
```

No contract multiplier needs to be applied — it is already baked into `shares`.
To recover the contract count, divide by the value per point.

This was verified on two instruments with different point values and different
currencies; both reconciled exactly.

⚠ One counter-example exists: a metals futures symbol *without* the `=F` suffix
whose `shares` did not correspond to the expected contract multiplier. If your
symbol lacks `=F`, verify before trusting the product. [Unconfirmed]

## 7. `Id` is renumbered between exports

**`Id` is only valid inside a single file.** [Verified]

Comparing two exports taken two days apart: of the 6,575 transactions present in
both, **1,022 (15.5%) had a different `Id`**.

Within a single block the Ids are usually stable — it is the file-level numbering
that shifts, apparently because the export renumbers by internal ordering, so any
change in the relative position of a portfolio or symbol displaces everything
after it.

Consequences:

- `OutgoingCashLink` points at an `Id`, so **link resolution is only valid within
  one file**. Never resolve last week's link value against this week's export.
- To diff transactions across exports, use a content key such as
  `(Portfolio, Symbol, Transaction Date, Shares Owned, Type)`.

This was found the hard way: using `Id` as a key to find "new transactions"
surfaced a row from six months earlier that had simply been renumbered.

## 8. Known limitations

1. **The export does not include the "exclude from portfolio" flag.** The app lets
   you mark an account as excluded from totals, but that state is absent from the
   CSV. There is no way to tell from the file alone which portfolios belong in a
   net-worth figure. If you are building an aggregate, you need to maintain that
   list yourself.
2. **Snapshot rows carry no position.** Always replay the transactions.
3. **One price per symbol per file.** Cross-portfolio valuation is internally
   consistent, but the price is the one at export time, not the price on any
   transaction date.
4. **`Sell All` / `Buy to Cover All` must be read as "flatten"** (§4).
5. **Closed and dormant accounts keep non-zero balances.** Summing the whole file
   without filtering treats historical residue as current holdings.
6. **The same instrument can appear in several portfolios.** If two of them
   represent the same real-world holding, naive summing double-counts it. The
   format cannot tell you which is which.
7. **`Cost Per Share` on `Dividend` / `Interest` rows is meaningless** (§4).
8. **`Id` is not a cross-file key** (§7).

## Official references

- Transaction types — <https://help.mystocksportfolio.app/help-guide/adding-and-editing-items/add-a-transaction>
- Cost basis methods — <https://help.mystocksportfolio.app/help-guide/calculations/unrealized-and-realized>
- FX rate override — <https://help.mystocksportfolio.app/help-guide/multi-currency-portfolios/forex-rate-at-time-of-transaction>
- Import and export — <https://help.mystocksportfolio.app/help-guide/backup-and-import/import-and-export-data>
- Public translation strings, the best third-party evidence for field semantics —
  <https://github.com/mystocksportfolio/translations>

Prior art: [Ghostfolio-MSP-Importer](https://github.com/TheekshanaA/Ghostfolio-MSP-Importer)
handles `Buy` / `Sell` / `Sell All`, and does not attempt `OutgoingCashLink`.

## Disclaimer

This is an **unofficial** description of an observed file format, and it comes with
no guarantee of correctness.

Every statement here was worked out by repeated inference — reading exports,
forming a hypothesis about what a column means, then checking it against other
rows, other exports, and what the app itself displays. That process is good enough
to have caught several non-obvious traps, and not good enough to be called
authoritative. Some readings could be wrong. Some are marked `[Unconfirmed]`
precisely because they are still guesses.

Scope of the evidence: exports from **iOS version 2.522.0**, one portfolio, eleven
weekly files. Other app versions may behave differently, and the format may change
at any time without notice.

### Not affiliated

Not produced, reviewed, endorsed, sponsored, or supported by Peeksoft LLC.
"My Stocks Portfolio", "MSP", and "Peeksoft" are trademarks of their respective
owners, used here only to identify the format this project reads.

### How this was produced

Every statement was derived from CSV files the author exported from their **own
account**, using the app's **own built-in export feature**, containing only the
author's own data. Specifically, this work involved none of the following:

- no decompilation, disassembly, or binary analysis
- no inspection or modification of application source code
- no interception or analysis of network traffic
- no circumvention of any technological protection measure
- no access to any other user's data
- no non-public material of any kind

The only external references are the vendor's own public help center and public
translations repository, both linked above. Two short UI strings are quoted in §3
and §5 to identify what a column means; no code, assets, or substantial text from
the app is reproduced here. The reference parser is original work.

### If you are Peeksoft

If anything in this repository concerns you, open an issue or contact the
maintainer. It will be addressed promptly and in good faith.

---

**Verify against your own export before relying on any of this**, especially for
anything involving money.

The reference parser is original work and contains no code from the app.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
Copyright 2026 Izero.

Covers the specification text, the parser, and the example file alike. Note that
Apache 2.0 requires downstream users to carry the `NOTICE` file forward, so the
trademark and non-affiliation statement travels with any fork.
