import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


def test_legacy_api_key_populates_both_provider_configs(monkeypatch, tmp_path):
    from utils import load_config

    monkeypatch.setenv("OMBRE_API_KEY", "legacy-key")
    monkeypatch.delenv("OMBRE_COMPRESS_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_EMBED_API_KEY", raising=False)

    config = load_config(str(tmp_path / "missing.yaml"))

    assert config["dehydration"]["api_key"] == "legacy-key"
    assert config["embedding"]["api_key"] == "legacy-key"

def test_render_local_is_bounded_and_strips_wikilinks():
    from tools.breath.render import render_local
    from utils import count_tokens_approx

    rendered = render_local(
        "[[潘小满]]是家里的猫。" * 200,
        {"name": "宠物记忆", "domain": ["家庭"]},
        max_tokens=80,
    )

    assert "[[" not in rendered
    assert "潘小满" in rendered
    assert count_tokens_approx(rendered) <= 80


def test_pulse_defaults_to_stats_without_listing_buckets(monkeypatch):
    from tools import _runtime as rt
    from tools.anchor.core import pulse

    bucket_mgr = SimpleNamespace(
        get_stats=AsyncMock(
            return_value={
                "permanent_count": 15,
                "dynamic_count": 67,
                "archive_count": 0,
                "feel_count": 0,
                "plan_count": 0,
                "letter_count": 0,
                "total_size_kb": 702.8,
            }
        ),
        list_all=AsyncMock(side_effect=AssertionError("summary pulse must not list buckets")),
    )
    decay_engine = SimpleNamespace(ensure_started=AsyncMock(), is_running=True)
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(rt, "decay_engine", decay_engine)

    result = asyncio.run(pulse())

    assert "固化桶: 15 个" in result
    assert "动态桶: 67 个" in result
    assert "details=true" in result
    bucket_mgr.list_all.assert_not_awaited()


def test_query_search_never_calls_dehydration_api(monkeypatch):
    from tools import _runtime as rt
    from tools.breath import search as search_module

    bucket = {
        "id": "abc123def456",
        "content": "潘小满是小乖重要的家庭成员。" * 80,
        "metadata": {
            "name": "潘小满",
            "domain": ["家庭"],
            "tags": ["猫"],
            "importance": 10,
            "valence": 0.8,
            "arousal": 0.3,
            "type": "dynamic",
        },
    }
    bucket_mgr = SimpleNamespace(
        search=AsyncMock(return_value=[bucket]),
        touch=AsyncMock(),
        list_all=AsyncMock(return_value=[]),
        get=AsyncMock(return_value=None),
    )
    dehydrator = SimpleNamespace(
        dehydrate=AsyncMock(side_effect=AssertionError("read path called external LLM"))
    )
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(rt, "dehydrator", dehydrator)
    embedding_engine = SimpleNamespace(
        search_similar=AsyncMock(
            side_effect=AssertionError("exact keyword hit called embedding API")
        )
    )
    monkeypatch.setattr(rt, "embedding_engine", embedding_engine)
    monkeypatch.setattr(rt, "fire_webhook", None)
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(search_module.random, "random", lambda: 1.0)

    result = asyncio.run(
        search_module.surface_search(
            query="潘小满",
            max_results=1,
            max_tokens=100,
            domain="",
            valence=-1,
            arousal=-1,
            tag_filter=[],
        )
    )

    assert "潘小满" in result
    dehydrator.dehydrate.assert_not_awaited()
    embedding_engine.search_similar.assert_not_awaited()
    bucket_mgr.touch.assert_awaited_once_with("abc123def456")
