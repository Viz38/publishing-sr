import pytest
from sr_common.stealth import get_human_delay, get_browser_profile
import statistics

def test_get_human_delay_distribution():
    # Collect a sample of delays
    delays = [get_human_delay() for _ in range(1000)]
    
    # Gamma distribution should have positive values and a long tail
    assert all(d > 0 for d in delays)
    assert statistics.mean(delays) > 0
    assert max(delays) > statistics.mean(delays) * 2

def test_get_browser_profile():
    profile = get_browser_profile("windows")
    assert profile["os"] == "windows"
    assert "screen_resolution" in profile
    assert "hardware_concurrency" in profile
    assert profile["hardware_concurrency"] in [4, 8, 16]
