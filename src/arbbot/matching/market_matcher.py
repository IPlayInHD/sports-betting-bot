"""Layer 3: cross-venue market matching.

Sportsbooks and Polymarket describe the same real-world event completely
differently (structured home/away team keys vs. free-text question strings
like "Will the Lakers beat the Celtics on July 4?"). This module aligns them
using fuzzy text matching plus a date-proximity guard, so downstream layers
can compare a specific sportsbook outcome against a specific Polymarket token.
"""

from __future__ import annotations

import logging

from rapidfuzz import fuzz

from arbbot.models import MatchedMarket, PolymarketQuote, SportsbookQuote

logger = logging.getLogger(__name__)


def _outcome_similarity(outcome_name: str, question: str, market_outcome_name: str) -> float:
    """Combine similarity of the outcome name against both the Polymarket
    question text and its own outcome label, taking the stronger signal.
    """
    score_vs_question = fuzz.partial_ratio(outcome_name.lower(), question.lower()) / 100.0
    score_vs_outcome = fuzz.ratio(outcome_name.lower(), market_outcome_name.lower()) / 100.0
    return max(score_vs_question, score_vs_outcome)


def match_markets(
    sportsbook_quotes: list[SportsbookQuote],
    polymarket_quotes: list[PolymarketQuote],
    min_match_score: float = 0.82,
    max_date_skew_hours: float = 6.0,
) -> list[MatchedMarket]:
    """Group sportsbook quotes by (event, outcome) and pair each group with
    its best-matching Polymarket token, subject to a minimum fuzzy-match
    score and a maximum event-date skew.
    """
    grouped: dict[tuple[str, str], list[SportsbookQuote]] = {}
    for q in sportsbook_quotes:
        grouped.setdefault((q.event_id, q.outcome_name), []).append(q)

    matches: list[MatchedMarket] = []
    max_skew_sec = max_date_skew_hours * 3600.0

    for (event_id, outcome_name), quotes in grouped.items():
        commence_time = quotes[0].commence_time
        best_quote: PolymarketQuote | None = None
        best_score = 0.0

        for pm_quote in polymarket_quotes:
            if abs(pm_quote.end_date - commence_time) > max_skew_sec:
                continue
            score = _outcome_similarity(outcome_name, pm_quote.question, pm_quote.outcome_name)
            if score > best_score:
                best_score = score
                best_quote = pm_quote

        if best_quote is not None and best_score >= min_match_score:
            matches.append(
                MatchedMarket(
                    match_id=f"{event_id}:{outcome_name}",
                    sportsbook_quotes=quotes,
                    polymarket_quote=best_quote,
                    match_score=best_score,
                )
            )
        else:
            logger.debug(
                "No confident match for %s / %s (best_score=%.2f)", event_id, outcome_name, best_score
            )

    return matches
