"""Layer 4: pure cross-market arbitrage detection (primary, low-risk strategy).

This is genuine "surebet" arbitrage, not a directional value bet: for a given
event, we look at every mutually-exclusive outcome, take whichever venue
(best-priced sportsbook, or Polymarket's ask) is cheaper for that outcome,
and check whether the combined cost of covering *every* outcome is less than
$1. If so, staking proportionally to each leg's implied probability locks in
the same profit regardless of which outcome actually happens -- this is the
standard multi-way "surebet" formula generalized to include Polymarket as one
of the venues.

Per the user's request, the minimum edge threshold is kept deliberately LOW
(configurable, default 0.5%) so the bot fires often on small, low-risk
margins rather than waiting for large, rare gaps.
"""

from __future__ import annotations

from collections import defaultdict

from arbbot.models import GapSignal, MatchedMarket, Side, SignalType


def _group_by_event(matches: list[MatchedMarket]) -> dict[str, list[MatchedMarket]]:
    grouped: dict[str, list[MatchedMarket]] = defaultdict(list)
    for m in matches:
        event_id = m.sportsbook_quotes[0].event_id
        grouped[event_id].append(m)
    return grouped


def detect_arbitrage(
    matches: list[MatchedMarket],
    min_edge_pct: float = 0.5,
    max_edge_pct: float = 8.0,
    fee_buffer_pct: float = 1.0,
) -> list[GapSignal]:
    signals: list[GapSignal] = []

    for event_id, outcome_matches in _group_by_event(matches).items():
        if len(outcome_matches) < 2:
            continue  # need >=2 mutually-exclusive outcomes to arb across

        legs = []
        for m in outcome_matches:
            best_book_quote = m.best_sportsbook_quote
            book_prob = 1.0 / best_book_quote.decimal_odds
            poly_ask = m.polymarket_quote.best_ask

            if poly_ask <= book_prob:
                legs.append(
                    {
                        "outcome_name": m.polymarket_quote.outcome_name,
                        "venue": "polymarket",
                        "prob": poly_ask,
                        "matched_market": m,
                    }
                )
            else:
                legs.append(
                    {
                        "outcome_name": best_book_quote.outcome_name,
                        "venue": "sportsbook",
                        "prob": book_prob,
                        "matched_market": m,
                        "bookmaker": best_book_quote.bookmaker,
                    }
                )

        total_prob = sum(leg["prob"] for leg in legs)
        raw_edge_pct = (1.0 - total_prob) * 100.0
        edge_pct = raw_edge_pct - fee_buffer_pct

        if edge_pct < min_edge_pct or raw_edge_pct > max_edge_pct:
            continue

        venues_used = {leg["venue"] for leg in legs}
        if len(venues_used) < 2:
            continue  # not a cross-market opportunity (pure single-venue line-shopping)

        for leg in legs:
            leg["stake_fraction"] = leg["prob"] / total_prob

        poly_fraction = sum(l["stake_fraction"] for l in legs if l["venue"] == "polymarket")
        book_fraction = sum(l["stake_fraction"] for l in legs if l["venue"] == "sportsbook")

        primary_poly_leg = max(
            (l for l in legs if l["venue"] == "polymarket"), key=lambda l: l["stake_fraction"], default=None
        )
        if primary_poly_leg is None:
            continue

        signals.append(
            GapSignal(
                signal_type=SignalType.ARBITRAGE,
                matched_market=primary_poly_leg["matched_market"],
                edge_pct=edge_pct,
                sportsbook_stake_fraction=book_fraction,
                polymarket_stake_fraction=poly_fraction,
                polymarket_side=Side.YES,
                metadata={
                    "event_id": event_id,
                    "legs": [{k: v for k, v in leg.items() if k != "matched_market"} for leg in legs],
                    "total_implied_prob": total_prob,
                },
            )
        )

    return signals
