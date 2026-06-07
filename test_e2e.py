#!/usr/bin/env python3
"""
完整端到端測試 - 所有功能驗證
"""
import aiohttp
import asyncio
import json

PROXY_BASE = "http://127.0.0.1:9099/30000"
PROXY_KEY = "hermes_chat_key"

results = {"pass": 0, "fail": 0}

async def run_test(name, test_fn):
    try:
        ok = await test_fn()
        if ok:
            results["pass"] += 1
            print(f"  ✅ {name}")
        else:
            results["fail"] += 1
            print(f"  ❌ {name}")
    except Exception as e:
        results["fail"] += 1
        print(f"  ❌ {name}: {e}")

# ── Chat Completions Tests ──

async def test_chat_basic():
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            f"{PROXY_BASE}/v1/chat/completions",
            json={"model": "hermes-agent", "messages": [{"role": "user", "content": "hi"}], "stream": False},
            headers={"Authorization": f"Bearer {PROXY_KEY}"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
            return 'choices' in data and data['choices'][0]['message'].get('content')

async def test_chat_stream():
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            f"{PROXY_BASE}/v1/chat/completions",
            json={"model": "hermes-agent", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers={"Authorization": f"Bearer {PROXY_KEY}"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                return False
            text = ""
            async for line in resp.content:
                line_str = line.decode('utf-8', errors='replace').strip()
                if line_str.startswith("data:"):
                    data_str = line_str[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        d = json.loads(data_str)
                        c = d.get('choices', [{}])[0].get('delta', {}).get('content', '')
                        text += c
                    except:
                        pass
            return len(text) > 0

async def test_chat_tools():
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            f"{PROXY_BASE}/v1/chat/completions",
            json={"model": "hermes-agent", "messages": [{"role": "user", "content": "搜尋BTC價格"}],
                  "tools": [{"type": "function", "function": {"name": "web_search", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}],
                  "stream": False},
            headers={"Authorization": f"Bearer {PROXY_KEY}"},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            data = await resp.json()
            return 'choices' in data

# ── Responses API Tests ──

async def test_responses_basic():
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            f"{PROXY_BASE}/v1/responses",
            json={"model": "hermes-agent", "input": "hi", "stream": False},
            headers={"Authorization": f"Bearer {PROXY_KEY}"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
            return 'output' in data and len(data['output']) > 0

async def test_responses_stream():
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            f"{PROXY_BASE}/v1/responses",
            json={"model": "hermes-agent", "input": "hi", "stream": True},
            headers={"Authorization": f"Bearer {PROXY_KEY}"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                return False
            text = ""
            async for line in resp.content:
                line_str = line.decode('utf-8', errors='replace').strip()
                if line_str.startswith("data:"):
                    data_str = line_str[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        d = json.loads(data_str)
                        if 'delta' in d:
                            text += d['delta']
                    except:
                        pass
            return len(text) > 0

async def test_responses_tools():
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            f"{PROXY_BASE}/v1/responses",
            json={"model": "hermes-agent", "input": "搜尋BTC", "stream": False,
                  "tools": [{"type": "function", "name": "web_search", "description": "搜尋",
                           "strict": False, "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]},
            headers={"Authorization": f"Bearer {PROXY_KEY}"},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            data = await resp.json()
            return 'output' in data and len(data['output']) > 0

async def test_passthrough_models():
    async with aiohttp.ClientSession() as sess:
        async with sess.get(
            f"{PROXY_BASE}/v1/models",
            headers={"Authorization": f"Bearer {PROXY_KEY}"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
            # 回傳格式: {"object":"list","data":[...]}
            return 'data' in data and len(data['data']) > 0

async def main():
    print("=" * 70)
    print("🧪 完整端到端測試")
    print("=" * 70)
    
    await run_test("Chat Completions 基本", test_chat_basic)
    await run_test("Chat Completions 串流", test_chat_stream)
    await run_test("Chat Completions + Tools", test_chat_tools)
    await run_test("Responses API 基本", test_responses_basic)
    await run_test("Responses API 串流", test_responses_stream)
    await run_test("Responses API + Tools", test_responses_tools)
    await run_test("Passthrough /v1/models", test_passthrough_models)
    
    print("\n" + "=" * 70)
    print(f"📊 結果: {results['pass']} 通過, {results['fail']} 失敗")
    if results['fail'] == 0:
        print("🎉 所有測試通過！")
    else:
        print(f"⚠️  有 {results['fail']} 個測試失敗")
    print("=" * 70)

asyncio.run(main())
