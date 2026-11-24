import json

import pytest

from fakes import FakeHttp, gemini_text
from http_client import HttpResponse
from tools.registry import ToolContext, _weather, _web_search


def _ctx(http, provider="tavily", key="tvly-test", prefs=None, llm=None):
    return ToolContext(repo=None, user_id=1, update_id=None,
                       profile={"preferences": prefs or {}, "facts": []}, default_timezone="America/Chicago",
                       llm=llm, models={"search": ["m"]}, usage=[], http=http, tavily_api_key=key,
                       search_provider=provider)


def _tavily(status=200, **body):
    data = {"answer": "Lakers won 110-102.", "results": [
        {"title": "Game recap", "url": "https://example.com/recap", "content": "x" * 2000, "score": 0.9}], **body}
    return lambda url, payload: HttpResponse(status, json.dumps(data))


async def test_tavily_search_returns_answer_and_trimmed_results():
    http = FakeHttp()
    http.routes = {"https://api.tavily.com": _tavily()}
    out = await _web_search(_ctx(http), {"query": "latest Lakers score"})
    assert out["provider"] == "tavily" and out["answer"].startswith("Lakers")
    assert out["results"][0]["url"] == "https://example.com/recap"
    assert len(out["results"][0]["snippet"]) == 600
    payload = http.calls[0][1]
    assert payload["topic"] == "news" and payload["time_range"] == "week"  # "latest" -> news


async def test_tavily_missing_key_and_quota_errors():
    assert "missing TAVILY_API_KEY" in (await _web_search(_ctx(FakeHttp(), key=None), {"query": "q"}))["error"]
    http = FakeHttp()
    http.routes = {"https://api.tavily.com": _tavily(status=432)}
    assert "quota" in (await _web_search(_ctx(http), {"query": "q"}))["error"]


async def test_auto_falls_back_from_gemini_quota_to_tavily():
    from llm.gemini import GeminiClient

    http = FakeHttp(lambda m, b: HttpResponse(429, '{"error":{"message":"quota"}}'))
    http.routes = {"https://api.tavily.com": _tavily()}
    out = await _web_search(_ctx(http, provider="auto", llm=GeminiClient("k", http)), {"query": "q"})
    assert out["provider"] == "tavily"


async def test_gemini_grounding_sources_are_parsed():
    from llm.gemini import GeminiClient

    grounded = {"candidates": [{"content": {"parts": [{"text": "Sunny."}]}, "groundingMetadata": {
        "webSearchQueries": ["weather"], "groundingChunks": [{"web": {"uri": "https://w.example", "title": "W"}}]}}],
        "usageMetadata": {}}
    http = FakeHttp(lambda m, b: HttpResponse(200, json.dumps(grounded)))
    out = await _web_search(_ctx(http, provider="gemini", llm=GeminiClient("k", http)), {"query": "q"})
    assert out["provider"] == "google" and out["sources"] == [{"title": "W", "url": "https://w.example"}]


def _open_meteo(country_code="US"):
    geo = {"results": [{"name": "Chicago", "admin1": "Illinois", "country": "United States",
                        "country_code": country_code, "latitude": 41.85, "longitude": -87.65}]}
    fc = {"current": {"time": "2026-11-23T10:00", "temperature_2m": 35.2, "apparent_temperature": 28.0,
                      "relative_humidity_2m": 70, "weather_code": 3, "wind_speed_10m": 12.0},
          "daily": {"time": ["2026-11-23"], "weather_code": [61], "temperature_2m_max": [40.1],
                    "temperature_2m_min": [30.5], "precipitation_probability_max": [80], "precipitation_sum": [0.3],
                    "wind_speed_10m_max": [18.0], "sunrise": ["2026-11-23T06:45"], "sunset": ["2026-11-23T16:25"]}}
    return {"https://geocoding-api.open-meteo.com": lambda u, p: HttpResponse(200, json.dumps(geo)),
            "https://api.open-meteo.com": lambda u, p: HttpResponse(200, json.dumps(fc))}


async def test_weather_uses_fahrenheit_in_us_and_saved_location():
    http = FakeHttp()
    http.routes = _open_meteo()
    out = await _weather(_ctx(http, prefs={"location": "Chicago"}), {})
    assert out["place"] == "Chicago, Illinois, United States"
    assert out["now"]["temperature"] == "35.2°F" and out["now"]["conditions"] == "overcast"
    assert out["forecast"][0]["conditions"] == "light rain" and out["forecast"][0]["sunset"] == "16:25"
    assert "temperature_unit=fahrenheit" in http.calls[-1][0]


async def test_weather_metric_preference_and_missing_location():
    http = FakeHttp()
    http.routes = _open_meteo()
    await _weather(_ctx(http, prefs={"units": "metric"}), {"location": "Chicago"})
    assert "fahrenheit" not in http.calls[-1][0]
    assert "ask the user" in (await _weather(_ctx(FakeHttp()), {}))["error"]


async def test_weather_unknown_place():
    http = FakeHttp()
    http.routes = {"https://geocoding-api.open-meteo.com": lambda u, p: HttpResponse(200, "{}")}
    assert "could not find" in (await _weather(_ctx(http), {"location": "Atlantis"}))["error"]
