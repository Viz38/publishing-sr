import asyncio
import random
import math
import logging
from typing import Dict, Tuple, Any

logger = logging.getLogger("stealth")

def get_human_delay(shape: float = 2.0, scale: float = 1.0) -> float:
    """Returns a delay in seconds following a Gamma distribution to mimic human hesitation."""
    return random.gammavariate(shape, scale)

def get_browser_profile(os_type: str = "windows") -> Dict[str, Any]:
    """Returns a hardware-coherent profile for a specific OS."""
    if os_type == "windows":
        return {
            "os": "windows",
            "screen_resolution": random.choice([(1920, 1080), (2560, 1440), (1366, 768)]),
            "device_scale_factor": 1,
            "hardware_concurrency": random.choice([4, 8, 16]),
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        }
    elif os_type == "mac":
        return {
            "os": "mac",
            "screen_resolution": random.choice([(1440, 900), (2560, 1600), (2880, 1800)]),
            "device_scale_factor": 2,
            "hardware_concurrency": random.choice([8, 10, 12]),
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        }
    # Default to windows
    return get_browser_profile("windows")

async def simulate_human_movement(page):
    """Simulates realistic non-linear mouse movements and scrolls on a page."""
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        
        # 1. Non-linear mouse movement (Bézier curve)
        steps = random.randint(3, 6)
        for _ in range(steps):
            start_x = random.randint(0, viewport["width"])
            start_y = random.randint(0, viewport["height"])
            end_x = random.randint(0, viewport["width"])
            end_y = random.randint(0, viewport["height"])
            
            await _move_mouse_bezier(page, start_x, start_y, end_x, end_y)
            await asyncio.sleep(get_human_delay(1.5, 0.2))
            
        # 2. Variable scrolls
        scroll_steps = random.randint(2, 5)
        for _ in range(scroll_steps):
            amount = random.randint(150, 500)
            await page.mouse.wheel(0, amount)
            await asyncio.sleep(get_human_delay(1.2, 0.4))
            
    except Exception as e:
        logger.warning(f"STEALTH_HUMAN_SIM_ERR: {e}")

async def _move_mouse_bezier(page, x1, y1, x2, y2, steps=25):
    """Moves mouse from (x1,y1) to (x2,y2) using a quadratic Bézier curve."""
    # Control point for the curve
    cx = (x1 + x2) / 2 + random.randint(-100, 100)
    cy = (y1 + y2) / 2 + random.randint(-100, 100)
    
    for i in range(steps + 1):
        t = i / steps
        # Quadratic Bézier formula: (1-t)^2 * P0 + 2(1-t)t * P1 + t^2 * P2
        x = (1-t)**2 * x1 + 2*(1-t)*t * cx + t**2 * x2
        y = (1-t)**2 * y1 + 2*(1-t)*t * cy + t**2 * y2
        
        await page.mouse.move(x, y)
        # Add micro-jitter to timing
        await asyncio.sleep(random.uniform(0.005, 0.015))
