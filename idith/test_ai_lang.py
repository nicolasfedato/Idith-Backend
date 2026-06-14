from idith import ai_lang


def test_normalize_lang_defaults_to_it():
    assert ai_lang.normalize_lang(None) == "it"
    assert ai_lang.normalize_lang("") == "it"
    assert ai_lang.normalize_lang("IT") == "it"


def test_normalize_lang_en_variants():
    assert ai_lang.normalize_lang("en") == "en"
    assert ai_lang.normalize_lang("EN") == "en"
    assert ai_lang.normalize_lang("en-US") == "en"


def test_build_language_system_prompt_contains_mandatory():
    assert "MANDATORY" in ai_lang.build_language_system_prompt("en")
    assert "English" in ai_lang.build_language_system_prompt("en")
    assert "Italian" in ai_lang.build_language_system_prompt("it")


def test_localize_skips_italian_when_lang_it():
    assert ai_lang.localize_assistant_reply("Ciao mondo", "it", api_key="fake") == "Ciao mondo"


def test_chat_returns_english_template():
    assert "Confirm?" in ai_lang.chat("pending_confirm_setting", "en", details="Stop Loss 5%")
    assert "Confermi" in ai_lang.chat("pending_confirm_setting", "it", details="Stop Loss 5%")


def test_build_pending_confirm_prompt_en():
    prompt = ai_lang.build_pending_confirm_prompt(
        {"sl": 5.0, "risk_pct": 10.0},
        lang="en",
    )
    assert "You are setting" in prompt
    assert "Confirm?" in prompt
    assert "Stai impostando" not in prompt


def test_summary_labels_en():
    labels = ai_lang.all_summary_labels("en")
    assert labels["pair"] == "Pair"
    assert labels["market_type"] == "Market type"
