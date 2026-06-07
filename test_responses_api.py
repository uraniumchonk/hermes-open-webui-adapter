#!/usr/bin/env python3
"""
測試 Responses API 端點的實際行為。
"""
import aiohttp
import asyncio
import json

API_KEY = "hermes_coder"
BASE = "http://127.0.0.1:30001"

async def test_non_stream():
    print("=" * 60)
    print("Test 1: POST /v1/responses (non-streaming)")
    print("=" * 60)
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            f"{BASE}/v1/responses",
            json={
                "model": "qwen-27b-default",
                "input": "你好，請簡單回應三個字",
                "instructions": "你是測試助手。",
                "stream": False,
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
        ) as resp:
            print(f"Status: {resp.status}")
            print(f"Content-Type: {resp.headers.get('content-type', '')}")
            body = await resp.json()
            print(json.dumps(body, indent=2, ensure_ascii=False))
            return body

async def test_stream():
    print("\n" + "=" * 60)
    print("Test 2: POST /v1/responses (streaming)")
    print("=" * 60)
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            f"{BASE}/v1/responses",
            json={
                "model": "qwen-27b-default",
                "input": "你好，請簡單回應三個字",
                "instructions": "你是測試助手。",
                "stream": True,
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
        ) as resp:
            print(f"Status: {resp.status}")
            print(f"Content-Type: {resp.headers.get('content-type', '')}")
            print("\n--- SSE Events ---")
            async for line in resp.content:
                decoded = line.decode("utf-8", errors="replace")
                if decoded.strip():
                    print(f"  {decoded.strip()}")

async def test_get_response(response_id):
    print("\n" + "=" * 60)
    print(f"Test 3: GET /v1/responses/{response_id}")
    print("=" * 60)
    async with aiohttp.ClientSession() as sess:
        async with sess.get(
            f"{BASE}/v1/responses/{response_id}",
            headers={"Authorization": f"Bearer {API_KEY}"},
        ) as resp:
            print(f"Status: {resp.status}")
            body = await resp.json()
            print(json.dumps(body, indent=2, ensure_ascii=False)[:2000])

async def test_delete_response(response_id):
    print("\n" + "=" * 60)
    print(f"Test 4: DELETE /v1/responses/{response_id}")
    print("=" * 60)
    async with aiohttp.ClientSession() as sess:
        async with sess.delete(
            f"{BASE}/v1/responses/{response_id}",
            headers={"Authorization": f"Bearer {API_KEY}"},
        ) as resp:
            print(f"Status: {resp.status}")
            body = await resp.json()
            print(json.dumps(body, indent=2, ensure_ascii=False))

async def test_with_conversation_history():
    print("\n" + "=" * 60)
    print("Test 5: POST /v1/responses with conversation_history")
    print("=" * 60)
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            f"{BASE}/v1/responses",
            json={
                "model": "qwen-27b-default",
                "input": "剛才說了什麼？",
                "instructions": "你是測試助手。",
                "conversation_history": [
                    {"role": "user", "content": "你好"},
                    {"role": "assistant", "content": "喵～你好呀！"},
                ],
                "stream": False,
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
        ) as resp:
            print(f"Status: {resp.status}")
            body = await resp.json()
            print(json.dumps(body, indent=2, ensure_ascii=False)[:2000])

async def test_with_previous_response_id(resp_id):
    print("\n" + "=" * 60)
    print(f"Test 6: POST /v1/responses with previous_response_id={resp_id}")
    print("=" * 60)
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            f"{BASE}/v1/responses",
            json={
                "model": "qwen-27b-default",
                "input": "剛才你說了什麼？",
                "previous_response_id": resp_id,
                "stream": False,
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
        ) as resp:
            print(f"Status: {resp.status}")
            body = await resp.json()
            print(json.dumps(body, indent=2, ensure_ascii=False)[:2000])

async def main():
    # Test 1: Non-streaming
    result = await test_non_stream()
    resp_id = result.get("id", "")
    
    # Test 2: Streaming
    await test_stream()
    
    # Test 3: GET
    if resp_id:
        await test_get_response(resp_id)
    
    # Test 5: conversation_history
    await test_with_conversation_history()
    
    # Test 6: previous_response_id
    # Create a fresh response first
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            f"{BASE}/v1/responses",
            json={
                "model": "qwen-27b-default",
                "input": "我喜歡貓咪",
                "instructions": "你是測試助手。",
                "stream": False,
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
        ) as resp:
            body = await resp.json()
            prev_id = body.get("id", "")
    
    if prev_id:
        await test_with_previous_response_id(prev_id)
    
    # Test 4: DELETE (最後才刪除)
    if resp_id:
        await test_delete_response(resp_id)
    
    print("\n✅ All tests done!")

if __name__ == "__main__":
    asyncio.run(main())
