#!/usr/bin/env python3
"""
完整測試 Responses API 路由 - 直接呼叫 gateway vs 經過 proxy
"""
import aiohttp
import asyncio
import json

GATEWAY_BASE = "http://127.0.0.1:30000"
GATEWAY_KEY = "hermes_chat_key"

PROXY_BASE = "http://127.0.0.1:9099/30000"
PROXY_KEY = "hermes_chat_key"

async def test_direct_gateway():
    print("=" * 70)
    print("🧪 測試 1: 直接呼叫 Gateway (30000) - Responses API")
    print("=" * 70)
    async with aiohttp.ClientSession() as sess:
        payload = {"model": "hermes-agent", "input": "只是測試回傳hello", "stream": False}
        try:
            async with sess.post(
                f"{GATEWAY_BASE}/v1/responses", json=payload,
                headers={"Authorization": f"Bearer {GATEWAY_KEY}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                print(f"Status: {resp.status}")
                data = await resp.json()
                print(f"Response keys: {list(data.keys())}")
                if 'error' in data:
                    print(f"❌ Error: {data['error']}")
                    return False
                print(f"Output items: {len(data.get('output', []))}")
                for item in data.get('output', []):
                    if item.get('type') == 'message':
                        for part in item.get('content', []):
                            if part.get('type') == 'output_text':
                                print(f"  ✅ Text: {part['text'][:200]}")
                return True
        except Exception as e:
            print(f"❌ 測試失敗: {type(e).__name__}: {e}")
            return False

async def test_via_proxy():
    print("\n" + "=" * 70)
    print("🧪 測試 2: 經過 Proxy (9099) - Responses API")
    print("=" * 70)
    async with aiohttp.ClientSession() as sess:
        payload = {"model": "hermes-agent", "input": "只是測試回傳hello", "stream": False}
        try:
            async with sess.post(
                f"{PROXY_BASE}/v1/responses", json=payload,
                headers={"Authorization": f"Bearer {PROXY_KEY}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                print(f"Status: {resp.status}")
                data = await resp.json()
                print(f"Response keys: {list(data.keys())}")
                if 'error' in data:
                    print(f"❌ Error: {data['error']}")
                    return False
                print(f"Output items: {len(data.get('output', []))}")
                for item in data.get('output', []):
                    if item.get('type') == 'message':
                        for part in item.get('content', []):
                            if part.get('type') == 'output_text':
                                print(f"  ✅ Text: {part['text'][:200]}")
                return True
        except Exception as e:
            print(f"❌ 測試失敗: {type(e).__name__}: {e}")
            return False

async def test_chat_completions_proxy():
    print("\n" + "=" * 70)
    print("🧪 測試 3: Chat Completions 經過 Proxy (確認未破壞)")
    print("=" * 70)
    async with aiohttp.ClientSession() as sess:
        payload = {
            "model": "hermes-agent",
            "messages": [{"role": "user", "content": "回傳 hello"}],
            "stream": False,
        }
        try:
            async with sess.post(
                f"{PROXY_BASE}/v1/chat/completions", json=payload,
                headers={"Authorization": f"Bearer {PROXY_KEY}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                print(f"Status: {resp.status}")
                data = await resp.json()
                if 'error' in data:
                    print(f"❌ Error: {data['error']}")
                    return False
                choices = data.get('choices', [])
                if choices:
                    msg = choices[0].get('message', {})
                    print(f"  ✅ Message: {msg.get('content', '')[:200]}")
                return True
        except Exception as e:
            print(f"❌ 測試失敗: {type(e).__name__}: {e}")
            return False

async def main():
    r1 = await test_direct_gateway()
    r2 = await test_via_proxy()
    r3 = await test_chat_completions_proxy()
    print("\n" + "=" * 70)
    print("📊 總結:")
    print(f"  直接呼叫 Gateway: {'✅' if r1 else '❌'}")
    print(f"  經過 Proxy Responses: {'✅' if r2 else '❌'}")
    print(f"  經過 Proxy Chat: {'✅' if r3 else '❌'}")
    print("=" * 70)

asyncio.run(main())
