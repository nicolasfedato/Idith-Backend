"""Test intent detectors riutilizzabili (raccomandazione, conferma, rifiuto)."""

from __future__ import annotations

import pytest

from idith.intent_detectors import (
    is_confirmation,
    is_recommendation_request,
    is_rejection,
    normalize_intent_text,
)


@pytest.mark.parametrize(
    "text",
    [
        "non so",
        "non saprei",
        "consigliami",
        "scegli tu",
        "fai tu",
        "cosa consigli",
        "cosa mi consigli",
        "migliore",
        "consigliato",
        "i dont know",
        "i don't know",
        "not sure",
        "recommend",
        "recommend me",
        "what do you recommend",
        "suggest",
        "suggest one",
        "choose for me",
        "you decide",
        "best",
        "please choose for me",
        "could you recommend something",
    ],
)
def test_is_recommendation_request_accepts_phrases(text: str):
    assert is_recommendation_request(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "recomend me",
        "recomend",
        "sugest one",
        "chose for me",
        "not shure",
        "consigliami",
    ],
)
def test_is_recommendation_request_fuzzy_typos(text: str):
    assert is_recommendation_request(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "BTCUSDT",
        "15m",
        "metti sl 2%",
        "what is a timeframe",
        "set leverage 10",
    ],
)
def test_is_recommendation_request_rejects_operational_inputs(text: str):
    assert is_recommendation_request(text) is False


@pytest.mark.parametrize(
    "text",
    ["si", "sì", "yes", "y", "ok", "okay", "confermo", "conferma", "confirm", "confirmed", "proceed", "procedi", "va bene", "default"],
)
def test_is_confirmation_accepts_tokens(text: str):
    assert is_confirmation(text) is True


@pytest.mark.parametrize("text", ["yes.", " OK! ", "Sì", "yes please", "ok use it"])
def test_is_confirmation_normalizes_and_contains(text: str):
    assert is_confirmation(text) is True


@pytest.mark.parametrize("text", ["yess", "okk", "conferm"])
def test_is_confirmation_fuzzy_typos(text: str):
    assert is_confirmation(text) is True


@pytest.mark.parametrize("text", ["no", "n", "cancel", "annulla", "stop", "non confermo"])
def test_is_rejection_accepts_tokens(text: str):
    assert is_rejection(text) is True


@pytest.mark.parametrize("text", ["no thanks", "please cancel"])
def test_is_rejection_contains(text: str):
    assert is_rejection(text) is True


def test_normalize_strips_accents_apostrophes_and_punctuation():
    assert normalize_intent_text(" Sì! ") == "si"
    assert normalize_intent_text("Confirm.") == "confirm"
    assert normalize_intent_text("I don't know") == "i dont know"
