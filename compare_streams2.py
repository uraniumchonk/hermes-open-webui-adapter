#!/usr/bin/env python3
"""Compare raw Hermes Gateway vs hermes_tool_filter output - with aggressive timeouts."""
import asyncio
import aiohttp
import json

async def compare_streams():
    tools_url = "http://127.0.0.1:9099/30000/v1/chat/completions"
    raw_url = "http://127.0.0.1:30000/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer hermes_main_key",
    }
    data = {
        "model": "hermes-agent",
        "messages": [{"role": "user", "content": "請幫我執行命令 echo hello"}],
        "stream": True,
    }
    
    print("=" * 60)
    print("1. RAW Hermes Gateway (port 30000)")
    print("=" * 60)
    
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as sess:
            async with sess.post(raw_url, json=data, headers=headers) as resp:
                print(f"Status: {resp.status}, CT: {resp.headers.get('content-type')}")
                first_line = await resp.content.readline()
                print(f"First line: {first_line[:200]}")
                # Try to get more
                try:
                    second_line = await asyncio.wait_for(resp.content.readline(), timeout=5)
                    print(f"Second line: {second_line[:200]}")
                except:
                    print("Second line timeout")
    except Exception as e:
        print(f"  ERROR: {e}")
    
    print("\n" + "=" * 60)
    print("2. Through hermes_tool_filter (port 9099/30000)")
    print("=" * 60)
    
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as sess:
            async with sess.post(tools_url, json=data, headers=headers) as resp:
                print(f"Status: {resp.status}, CT: {resp.headers.get('content-type')}")
                
                buffer = ""
                chunk_count = 0
                async for line in resp.content:
                    buffer += line.decode("utf-8", errors="replace")
                    while "\n\n" in buffer:
                        frame, buffer = buffer.split("\n\n", 1)
                        chunk_count += 1
                        if chunk_count <= 15 and frame.strip():
                            print(f"CHUNK {chunk_count}: {frame[:200]}")
                        if frame.strip() == "data: [DONE]":
                            break
                    if chunk_count >= 15:
                        # Wait a bit for more
                        await asyncio.sleep(0.5)
                        break
                
                print(f"\nTotal chunks: {chunk_count}")
    except Exception as e:
        print(f"  ERROR: {e}")

asyncio.run(compare_streams())
