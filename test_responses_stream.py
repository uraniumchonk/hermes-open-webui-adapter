#!/usr/bin/env python3
"""
測試 Responses API 串流模式
"""
import aiohttp
import asyncio
import json

PROXY_BASE = "http://127.0.0.1:9099/30000"
PROXY_KEY = "hermes_chat_key"

GATEWAY_BASE = "http://127.0.0.1:30000"
GATEWAY_KEY = "hermes_chat_key"

async def test_stream_direct():
    """測試 1: 直接呼叫 Gateway 串流"""
    print("=" * 70)
    print("🧪 測試 1: 直接呼叫 Gateway - Responses API 串流")
    print("=" * 70)
    async with aiohttp.ClientSession() as sess:
        payload = {"model": "hermes-agent", "input": "用三句話介紹自己", "stream": True}
        try:
            async with sess.post(
                f"{GATEWAY_BASE}/v1/responses", json=payload,
                headers={"Authorization": f"Bearer {GATEWAY_KEY}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                print(f"Status: {resp.status}")
                print(f"Content-Type: {resp.headers.get('content-type')}")
                text_collected = ""
                event_count = 0
                async for line in resp.content:
                    line_str = line.decode('utf-8', errors='replace').strip()
                    if line_str.startswith("event:"):
                        event_count += 1
                    if line_str.startswith("data:"):
                        data_str = line_str[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            if 'delta' in data:
                                text_collected += data['delta']
                        except:
                            pass
                print(f"Events: {event_count}")
                print(f"Text: {text_collected[:300]}")
                return True
        except Exception as e:
            print(f"❌ 測試失敗: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return False

async def test_stream_proxy():
    """測試 2: 經過 Proxy 串流"""
    print("\n" + "=" * 70)
    print("🧪 測試 2: 經過 Proxy (9099) - Responses API 串流")
    print("=" * 70)
    async with aiohttp.ClientSession() as sess:
        payload = {"model": "hermes-agent", "input": "用三句話介紹自己", "stream": True}
        try:
            async with sess.post(
                f"{PROXY_BASE}/v1/responses", json=payload,
                headers={"Authorization": f"Bearer {PROXY_KEY}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                print(f"Status: {resp.status}")
                print(f"Content-Type: {resp.headers.get('content-type')}")
                text_collected = ""
                event_count = 0
                async for line in resp.content:
                    line_str = line.decode('utf-8', errors='replace').strip()
                    if line_str.startswith("event:"):
                        event_count += 1
                        print(f"  Event: {line_str}")
                    if line_str.startswith("data:"):
                        data_str = line_str[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            if 'delta' in data:
                                text_collected += data['delta']
                        except:
                            pass
                print(f"Events: {event_count}")
                print(f"Text: {text_collected[:300]}")
                return True
        except Exception as e:
            print(f"❌ 測試失敗: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return False

async def main():
    r1 = await test_stream_direct()
    r2 = await test_stream_proxy()
    print("\n" + "=" * 70)
    print("📊 總結:")
    print(f"  直接串流: {'✅' if r1 else '❌'}")
    print(f"  Proxy 串流: {'✅' if r2 else '❌'}")
    print("=" * 70)

asyncio.run(main())
