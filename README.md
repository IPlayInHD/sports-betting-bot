# arbbot

A multi-layer bot that detects pricing gaps across multiple markets and
(optionally) trades on them:
- **Sports**: sportsbook odds vs. Polymarket (riskless cross-market arbitrage)
- **Crypto**: cross-exchange price gaps for the same coin (Coinbase/Kraken/Binance)
- **Polymarket crypto** (`markets/polycrypto/`): Polymarket's crypto prediction
  markets vs. spot exchanges — a riskless YES+NO complement-arbitrage layer
  plus a model-based spot-anchored layer (see its section below)

It defaults to **paper trading** and treats live trading as an explicit,
multi-step opt-in. Forex, stocks/ETFs, and futures/options arbitrage are
planned but not yet implemented -- see "Roadmap" at the bottom.

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
- **Crypto arbitrage assumes pre-funded balances on every exchange you
  trade.** Moving crypto between exchanges takes minutes and costs network
  fees -- far too slow to capture a gap that exists right now. The bot buys
  on the cheap exchange and sells on the expensive one simultaneously,
  assuming you already hold cash/coin on both; it does not transfer funds
  between exchanges. You'll need to periodically rebalance manually (or with
  your own tooling) as inventory drifts toward whichever side is cheaper.
- **Exchange bot policies**: check each crypto exchange's API terms before
  running this live -- policies on automated trading vary by exchange and
  account tier.

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

`main.py` wires these into per-market asyncio polling loops (sports, crypto,
polycrypto), each with two frequency mechanisms from `strategy/adaptive.py`:

- **Adaptive burst polling**: when a cycle detects an opportunity, that loop
  immediately drops to `polling.burst_interval_sec` (default 0.4s) -- gaps
  cluster in time, so one hit is evidence the venue is temporarily lagging --
  then decays geometrically back to its base interval across quiet cycles.
  This concentrates the request budget where the action is instead of
  hammering every API at a fixed rate.
- **Cooldown throttle**: the flip side of polling faster. A persistent gap
  would otherwise re-fire every cycle and stack near-duplicate trades on one
  underlying mispricing (one correlated position pretending to be several
  independent ones); after an execution, re-detections of the same
  market/symbol are skipped for `cooldown_sec` and logged to the dashboard's
  opportunity feed with that reason.

The Polymarket CLOB read path also prices all outcome tokens concurrently
(bounded by a semaphore) rather than sequentially, so a scan of N markets is
bounded by the slowest request instead of the sum.

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
- **Crypto legs**: both automatable once live trading is authorized, via the
  `ccxt` library (`markets/crypto/execution.py`) -- one instance per
  exchange, using your own per-exchange API key/secret.

## Crypto cross-exchange arbitrage

`markets/crypto/` runs alongside the sports pipeline as an independent
polling loop (`config.yaml`'s `markets.crypto` section). For each symbol
(BTC/USD, ETH/USD, ...), it checks every configured exchange's public
ticker, and if the highest bid on one exchange covers the lowest ask on
another plus both legs' taker fees, that's a genuine buy-low/sell-high gap
-- read `markets/crypto/exchanges.py` and `detector.py`'s docstrings for the
exact mechanics and the pre-funded-balance caveat above. Like sports
arbitrage, `min_edge_pct` defaults low (0.3%) to favor frequency over size.
Public price feeds (Coinbase, Kraken, Binance/Binance.US) need no API key;
live order placement needs one key/secret pair per exchange
(`CRYPTO_<EXCHANGE>_API_KEY`/`_SECRET` in `.env`).

## Polymarket crypto arbitrage (`markets/polycrypto/`)

Runs as a third independent loop (`config.yaml`'s `markets.polymarket_crypto`
section) scanning Polymarket's crypto prediction markets. Two layers:

**Layer A -- complement arbitrage (riskless, primary).** A binary Polymarket
market pays exactly $1/share to the winning side. Whenever `YES ask + NO ask
< $1` after the fee buffer, the bot buys both sides in equal share counts and
locks in the difference regardless of how the market resolves -- the
prediction-market version of the sports surebet, except both legs execute on
the *same* venue (no cross-venue matching risk, both automatable). Like the
other arbitrage layers, `min_edge_pct` defaults low (0.4%) to favor many
small resolution-proof gaps over rare big ones.

**Layer B -- spot-anchored gaps (statistical, NOT riskless).** Threshold
markets ("Will BTC be above $70,000 on July 31?") are parsed
(`polycrypto/parser.py`) and priced against a multi-exchange consensus spot
price using a zero-drift lognormal model with per-asset volatility
assumptions (`polycrypto/pricing.py` -- the same first-order model desks use
to sanity-check binary option quotes; touch markets use the reflection
principle). When Polymarket's quote disagrees with the model by a wide margin
(default: 6+ probability points AND a multi-factor confidence score >= 0.75
built from sigma-distance, liquidity, spread, and time-to-expiry), the bot
buys the cheap side, sized by fractional Kelly like the sports value-edge
layer. **The model can simply be wrong** (crypto vol is regime-dependent);
this layer is a directional bet, its positions stay OPEN until the market
resolves, and you can disable it with `spot_anchor.enabled: false` while
keeping the riskless complement layer.

## Win-rate-first mode (the default)

The top-level `conservative_mode: true` flag makes the bot trade **only the
riskless arbitrage layers** — sports arbitrage, crypto cross-exchange, and the
Polymarket YES+NO complement — every one of which wins by construction once
both legs fill. The directional/statistical layers (sports `value_edge`,
polycrypto `spot_anchor`) are forced off regardless of their own `enabled`
flags. This is the setting to leave on if your priority is a high win rate and
high trade frequency rather than chasing larger, riskier edges. Set it to
`false` to also run the higher-variance model layers.

### Gold and other stable assets

The crypto cross-exchange desk isn't limited to volatile coins. **PAXG (PAX
Gold)** is included by default: it's a token redeemable for one troy ounce of
gold, so it tracks the gold price (low volatility, stable trend) while still
trading cross-exchange — a calmer, higher-win-rate book that runs through the
exact same riskless buy-low/sell-high engine. Add more symbols in
`config.yaml`'s `markets.crypto.symbols` (e.g. `XAUT/USD` for Tether Gold, or
more majors); feeds that don't list a symbol simply return no quote for it.
This is the honest way to "add gold" on a small account — via a gold-tracking
token that fits the existing arbitrage model, not gold futures (which need a
funded futures account and don't offer retail-capturable arbitrage).

## Running on a small bankroll ($50-100)

Defaults are tuned for this (`risk.bankroll_usd: 50`):

- `risk.min_stake_usd` (default $1) skips any trade sized below Polymarket's
  ~$1 order minimum instead of submitting an unfillable order.
- The polycrypto family uses a 4% per-trade cap (vs 2% elsewhere) so that
  after confidence scaling, riskless complement trades still clear that
  minimum -- $1-2 stakes on a $50 bankroll.
- Expect profits proportional to size: a 0.5% edge on a $2 stake is a penny.
  At this scale the bot is primarily a learning/validation instrument; the
  same code and risk limits scale up if you ever choose to fund it further.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"        # add "[live]" for live Polymarket, "[live-crypto]" for live crypto exchanges
cp .env.example .env           # fill in only what you need (see below)
```

- `ODDS_API_KEY` (from https://the-odds-api.com) — optional, sports desk only.
  The crypto and Polymarket desks always run on **real** public data with no
  key. Without an odds key the **sports desk is simply disabled** (it needs a
  paid odds feed, and the bot will not fabricate sports data for it) while the
  crypto and Polymarket desks keep running live.
- Synthetic data is used **only** when you explicitly set
  `ARBBOT_USE_MOCK_DATA=true` — an offline demo mode that fabricates gaps for
  every desk. Leave it unset for genuine observation: real quotes, real gaps,
  real (paper) fills.
- Polymarket and crypto exchange market data (public tickers) need **no key**.
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

**Performance dashboard:** run this in a *second* terminal tab while the bot
(above) is running, then open http://127.0.0.1:8787 in your browser:
```bash
python scripts/run_dashboard.py
```
It's read-only and cannot place trades — it just reads the same local
`data/trades.db` file the bot writes to. The terminal-style view shows:

- KPI tiles: locked-in P&L, bankroll/exposure, win rate, trades (total and
  last hour), detections/hr, average edge, execution latency (p50/p95)
- the equity curve (cumulative locked-in P&L) and an edge-distribution
  histogram of executed trades
- a **live opportunity feed**: every detection the bot makes — executed *or*
  skipped, with the exact reason (cooldown, stake below minimum, exposure
  cap, risk halt…) — so you can see what it's seeing, not just what it traded
- an opportunity funnel (detected → executed/skipped), strategy mix, and a
  per-market-family breakdown (sports / crypto x-exchange / Polymarket crypto)
- engine cadence chips showing each loop's current adaptive polling interval
  (amber ⚡ = burst mode after a detection)

It also displays a prominent banner confirming whether the bot is in
**paper (simulation)** or **live** mode, and turns amber if the risk manager
has halted trading. No data is sent anywhere; both processes only talk to
`localhost`.

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
and crypto legs are automated (not sportsbook bets); see "Execution model"
above. Before enabling this, re-read "Read this first."

## Roadmap

Sports, crypto cross-exchange, and Polymarket-crypto arbitrage are
implemented and tested. Not yet built:

- **Forex** (triangular arbitrage): needs a broker API with live bid/ask
  (e.g. an OANDA demo account, which is free); real edge on major pairs is
  usually captured by HFT firms in milliseconds, so expect this to be mostly
  educational rather than profitable.
- **Stocks/ETFs** (ETF premium/discount vs. NAV): needs a funded,
  API-enabled brokerage account (Alpaca, Interactive Brokers) before any of
  this is even testable, and genuine retail-capturable equity arbitrage is
  rare since US equities share the same NBBO.
- **Futures & options** (cash-and-carry, put-call parity): needs a funded
  futures/options account and usually paid market data -- the most
  setup-heavy of the group.

Each would follow the same `markets/<name>/` pattern as crypto: a
data-feed abstraction with a mock + real adapter, a detector producing
`MarketOpportunity` records, and an execution client gated behind the same
paper-by-default / dual-opt-in-for-live safety model.
