# arbbot

A multi-layer bot that detects pricing gaps between traditional sportsbook
odds markets and Polymarket, and (optionally) trades on them. It defaults to
**paper trading** and treats live trading as an explicit, multi-step opt-in.

## Read this first

- **Not financial advice, and not risk-free in practice.** The "arbitrage"
  strategy below is mathematically riskless *if both legs fill at the quoted
  price*. In the real world, prices move between when you detect a gap and
  when you execute, legs can partially fill, and fees/slippage eat into
  margin. Treat every edge estimate as optimistic.
- **Sportsbook terms of service.** Most retail sportsbooks (DraftKings,
  FanDuel, BetMGM, etc.) explicitly prohibit bots, automated wagering, and
  arbitrage/"advantage" betting in their ToS, and will limit stakes or close
  accounts found doing it. This bot therefore does **not** auto-place
  sportsbook bets — see "Execution model" below. If you want full automation
  on that leg, you need a venue whose terms and API actually permit it (e.g.
  a licensed betting exchange), and you are responsible for confirming that.
- **Regulatory compliance is on you.** Sports betting and prediction-market
  regulation (including Polymarket's terms, KYC requirements, and
  jurisdictional availability) varies by country/state and changes over
  time. Confirm you're legally permitted to use both venues from your
  jurisdiction before running this with real funds.
- **You can lose money**, including through software bugs, API changes,
  stale data, or venues resolving a market differently than expected. Start
  in paper mode, then backtest, then (if at all) go live with a small
  bankroll.

## Architecture: 10 layers

```
 1. Data ingestion        odds/*, polymarket/*          async, concurrent per-sport polling
 2. Normalization/de-vig  odds/normalization.py         american<->decimal<->prob, vig removal
 3. Market matching       matching/market_matcher.py    fuzzy team/date matching across venues
 4. Arbitrage detection   strategy/arbitrage.py         riskless cross-market "surebet" gaps (primary)
 5. Value-edge detection  strategy/value_edge.py         multi-book consensus vs Polymarket (opt-in)
 6. Filter pipeline       strategy/filters.py            staleness / liquidity / spread / exposure
 7. Confidence scoring    strategy/confidence.py         multi-factor 0-1 score -> sizing input
 8. Risk management       risk/*                         Kelly/proportional sizing + kill switches
 9. Execution             execution/*                    concurrent order placement, paper by default
10. Monitoring            monitoring/*                   win rate, P&L, latency percentiles
```

`main.py` wires these into a single asyncio polling loop.

### Layer 4: why this is "low risk, keep gaps low, high frequency"

For a given event, the bot looks at every mutually-exclusive outcome and, for
each one, takes whichever venue (best sportsbook price, or Polymarket's ask)
is cheaper. If the combined cost of covering *every* outcome is less than
$1, staking proportionally to each leg's implied probability locks in the
**same profit regardless of which outcome happens** — this is the standard
multi-way "surebet" formula, just with Polymarket treated as one of the
venues. `strategy/arbitrage.py` computes this directly; a synthetic backtest
of it (`tests/test_backtest.py::test_pure_arbitrage_backtest_has_perfect_win_rate`)
confirms every accepted trade wins by construction.

Because the edge threshold (`strategy.arbitrage.min_edge_pct`, default
**0.5%**) is kept deliberately low rather than waiting for large gaps, the
bot fires more often on small, low-variance margins — more frequency, lower
profit per trade, same risk profile per trade. `strategy.value_edge` is a
separate, disabled-by-default layer for directional statistical bets (higher
variance, higher potential edge); enable it only if you understand it's not
riskless.

### Execution model

- **Polymarket leg**: can be automated. `polymarket/clob_execution.py` uses
  the official `py-clob-client` SDK (never hand-rolled signing) to place
  real limit orders once live trading is explicitly authorized.
- **Sportsbook leg**: NOT auto-placed against retail sportsbooks by default.
  `execution/base.py`'s `ManualAlertSportsbookExecutionClient` fires an
  instant alert (webhook/log) with the exact bet to place so a human can act
  on it fast, instead of silently submitting bets that would violate most
  books' terms. If you trade through a betting exchange with a real,
  ToS-compliant order API, implement an `ExecutionClient` for it.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"        # add "[live]" too if/when you set up live Polymarket trading
cp .env.example .env           # fill in only what you need (see below)
```

- `ODDS_API_KEY` (from https://the-odds-api.com) — optional. Without it, the
  bot uses a built-in synthetic odds generator (`ARBBOT_USE_MOCK_DATA=true`
  is implied automatically when the key is absent).
- Polymarket market data (Gamma + CLOB reads) needs **no key**.
- Everything else in `.env.example` is only required for live trading.

## Running

**Backtest (synthetic data, no network, no keys needed):**
```bash
python scripts/run_backtest.py --events 500
```

**Paper trading (safe — never places real orders, forces `mode: paper`):**
```bash
python scripts/run_paper.py --config config/config.yaml
# or, fully offline:
ARBBOT_USE_MOCK_DATA=true python scripts/run_paper.py
```

**Tests:**
```bash
pytest
```

## Configuration

All strategy/risk knobs live in `config/config.yaml` (see inline comments);
secrets live only in `.env` / environment variables (`config.py`), never in
YAML. Key defaults are intentionally conservative: small per-trade caps, a
5% max daily loss kill switch, and a consecutive-loss kill switch.

## Going live

Live trading requires **both**:
1. `mode: live` in `config.yaml`, **and**
2. `I_UNDERSTAND_THE_RISKS=true` in your environment.

Either one alone is not enough — this dual opt-in exists so a single
misconfigured file or env var can't silently start moving real money
(`config.py::is_live_trading_authorized`). Even then, only the Polymarket
leg is automated; see "Execution model" above. Before enabling this, re-read
"Read this first."
