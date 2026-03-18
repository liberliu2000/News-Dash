from src.config import Settings


def test_settings_defaults():
    settings = Settings()
    assert settings.max_news_items > 0
    assert settings.llm_model
    assert settings.min_request_interval_seconds > 0
    assert settings.summary_max_chars > 0
