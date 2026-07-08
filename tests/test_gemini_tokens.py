import asyncio
import aiohttp
import os
import json
from dotenv import load_dotenv

async def test_gemini():
    load_dotenv()
    api_key = os.getenv("TYPEA_GEMINI_API_KEY")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro-002:generateContent?key={api_key}"
    
    payload = {
        "contents": [{"parts": [{"text": "Hello world"}]}],
        "systemInstruction": {"parts": [{"text": "You are a helpful assistant."}]}
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            print(f"GenerateContent Status: {resp.status}")
            print(await resp.text())
            
    # Test Cache Create
    cache_url = f"https://generativelanguage.googleapis.com/v1beta/cachedContents?key={api_key}"
    cache_payload = {
        "model": "models/gemini-1.5-pro-002",
        "systemInstruction": {"parts": [{"text": "You are a helpful assistant."}]},
        "ttl": "3600s"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(cache_url, json=cache_payload) as resp:
            print(f"\nCachedContents Status: {resp.status}")
            print(await resp.text())

asyncio.run(test_gemini())
