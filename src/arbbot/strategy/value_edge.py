"""Layer 5: statistical value-edge detection (secondary, opt-in, higher variance).

Unlike the arbitrage layer, this is a directional bet: it treats the
consensus de-vigged probability across multiple independent sportsbooks as
the "true" probability and looks for cases where Polymarket's price has
drifted away from it. This is NOT risk-free -- the sportsbook consensus can
itself be wrong -- so it is disabled by default (see config.yaml
strategy.value_edge.enabled) and gated behind a minimum-number-of-agreeing-
books requirement plus a consensus-agreement check to reduce false positives.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import pstdev

from arbbot.models import GapSignal, MatchedMarket, Side, SignalType
from arbbot.odds.normalization import devig_multiplicative


def _group_by_event(matches: list[MatchedMarket]) -> dict[str, list[MatchedMarket]]:
    grouped: dict[str, list[MatchedMarket]] = defaultdict(list)
    for m in matches:
        event_id = m.sportsbook_quotes[0].event_id
        grouped[event_id].append(m)
    return grouped


def detect_value_edges(
    matches: list[MatchedMarket],
    min_books_agreeing: int = 3,
    min_edge_pct: float = 3.0,
    max_consensus_stdev_pct: float = 4.0,
) -> list[GapSignal]:
    signals: list[GapSignal] = []

    for event_id, outcome_matches in _group_by_event(matches).items():
        if len(outcome_matches) < 2:
            continue

        by_bookmaker: dict[str, dict[str, float]] = defaultdict(dict)
        for m in outcome_matches:
            for q in m.sportsbook_quotes:
                by_bookmaker[q.bookmaker][q.outcome_name] = q.implied_prob

        outcome_names = {m.polymarket_quote.outcome_name for m in outcome_matches}
        per_outcome_devigged: dict[str, list[float]] = defaultdict(list)
        for raw_by_outcome in by_bookmaker.values():
            if set(raw_by_outcome.keys()) != outcome_names:
                continue  # incomplete book for this event, skip for consensus
            names = list(raw_by_outcome.keys())
            devigged = devig_multiplicative([raw_by_outcome[n] for n in names])
            for n, p in zip(names, devigged):
                per_outcome_devigged[n].append(p)

        for m in outcome_matches:
            name = m.polymarket_quote.outcome_name
            samples = per_outcome_devigged.get(name, [])
            if len(samples) < min_books_agreeing:
                continue

            consensus_prob = sum(samples) / len(samples)
            consensus_stdev_pct = pstdev(samples) * 100.0 if len(samples) > 1 else 0.0
            if consensus_stdev_pct > max_consensus_stdev_pct:
                continue  # books disagree too much to trust a consensus fair price

            poly_price = m.polymarket_quote.best_ask
            edge_pct = (consensus_prob - poly_price) * 100.0
            if edge_pct < min_edge_pct:
                continue  # Polymarket not cheap enough vs. consensus to bother

            confidence = min(1.0, len(samples) / (min_books_agreeing * 2)) * max(
                0.0, 1.0 - consensus_stdev_pct / max_consensus_stdev_pct
            )

            signals.append(
                GapSignal(
                    signal_type=SignalType.VALUE_EDGE,
                    matched_market=m,
                    edge_pct=edge_pct,
                    confidence=confidence,
                    sportsbook_stake_fraction=0.0,
                    polymarket_stake_fraction=1.0,
                    polymarket_side=Side.YES,
                    metadata={
                        "event_id": event_id,
                        "consensus_prob": consensus_prob,
                        "consensus_books": len(samples),
                        "consensus_stdev_pct": consensus_stdev_pct,
                        "poly_ask": poly_price,
                    },
                )
            )

    return signals
