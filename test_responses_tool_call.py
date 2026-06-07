#!/usr/bin/env python3
"""
測試 Responses API 的 Tool Calling 行為。
"""
import aiohttp
import asyncio
import json

API_KEY="hermes_coder"
BASE = "http://127.0.0.1:30001"

async def test_tool_call_non_stream():
    print("=" * 60)
    print("Test A: Tool Calling - Non-streaming")
    print("=" * 60)
    async with aiohttp.ClientSession() as sess:
        # Step 1: 要求模型呼叫工具
        async with sess.post(
            f"{BASE}/v1/responses",
            json={
                "model": "qwen-27b-default",
                "input": "現在幾點了？請用 terminal 執行 date 命令。",
                "instructions": "你是測試助手。你可以使用工具。",
                "tools": [
                    {
                        "type": "function",
                        "name": "terminal",
                        "description": "Execute shell commands on a Linux environment.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "command": {"type": "string", "description": "The command to execute"}
                            },
                            "required": ["command"]
                        }
                    }
                ],
                "stream": False,
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
        ) as resp:
            print(f"Step 1 Status: {resp.status}")
            body = await resp.json()
            print(json.dumps(body, indent=2, ensure_ascii=False))
            
            # 檢查是否有 tool calls
            output_items = body.get("output", [])
            tool_calls = []
            for item in output_items:
                if item.get("type") == "message":
                    for content in item.get("content", []):
                        if content.get("type") == "function_call":
                            tool_calls.append(content)
            
            if tool_calls:
                print(f"\n✅ 找到 {len(tool_calls)} 個 tool call")
                for tc in tool_calls:
                    print(json.dumps(tc, indent=2, ensure_ascii=False))
                
                # Step 2: 模擬工具結果回傳
                print("\n" + "=" * 60)
                print("Step 2: 回傳工具結果")
                print("=" * 60)
                resp_id = body.get("id", "")
                
                # 構建 input 包含 tool results
                tool_results = []
                for tc in tool_calls:
                    tool_results.append({
                        "type": "function_call_output",
                        "call_id": tc.get("id", tc.get("call_id", "")),
                        "output": "2026-06-08 星期一 12:34:56",
                    })
                
                async with sess.post(
                    f"{BASE}/v1/responses",
                    json={
                        "model": "qwen-27b-default",
                        "input": tool_results,
                        "previous_response_id": resp_id,
                        "tools": [
                            {
                                "type": "function",
                                "name": "terminal",
                                "description": "Execute shell commands on a Linux environment.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "command": {"type": "string", "description": "The command to execute"}
                                    },
                                    "required": ["command"]
                                }
                            }
                        ],
                        "stream": False,
                    },
                    headers={"Authorization": f"Bearer {API_KEY}"},
                ) as resp2:
                    print(f"Step 2 Status: {resp2.status}")
                    body2 = await resp2.json()
                    print(json.dumps(body2, indent=2, ensure_ascii=False)[:3000])
            else:
                print("\n❌ 模型沒有產生 tool call，可能不需要工具")

async def test_tool_call_stream():
    print("\n" + "=" * 60)
    print("Test B: Tool Calling - Streaming")
    print("=" * 60)
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            f"{BASE}/v1/responses",
            json={
                "model": "qwen-27b-default",
                "input": "現在幾點了？請用 terminal 執行 date 命令。",
                "instructions": "你是測試助手。你可以使用工具。",
                "tools": [
                    {
                        "type": "function",
                        "name": "terminal",
                        "description": "Execute shell commands on a Linux environment.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "command": {"type": "string", "description": "The command to execute"}
                            },
                            "required": ["command"]
                        }
                    }
                ],
                "stream": True,
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
        ) as resp:
            print(f"Status: {resp.status}")
            print("\n--- SSE Events ---")
            async for line in resp.content:
                decoded = line.decode("utf-8", errors="replace")
                if decoded.strip():
                    print(f"  {decoded.strip()}")

async def main():
    await test_tool_call_non_stream()
    await test_tool_call_stream()
    print("\n✅ Tool calling tests done!")

if __name__ == "__main__":
    asyncio.run(main())
