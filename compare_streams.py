#!/usr/bin/env python3
"""Compare raw Hermes Gateway vs hermes_tool_filter output for tool call streaming."""
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
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as sess:
            async with sess.post(raw_url, json=data, headers=headers) as resp:
                print(f"Status: {resp.status}, CT: {resp.headers.get('content-type')}")
                buffer = ""
                chunks = []
                async for line in resp.content:
                    buffer += line.decode("utf-8", errors="replace")
                    while "\n\n" in buffer:
                        frame, buffer = buffer.split("\n\n", 1)
                        chunks.append(frame)
                        if len(chunks) <= 20:
                            # Show event type and content preview
                            lines = frame.strip().split("\n")
                            for l in lines:
                                if l.startswith("event: "):
                                    print(f"  EVENT: {l[7:]}")
                                elif l.startswith("data:"):
                                    d = l[5:].strip()
                                    if d == "[DONE]":
                                        print(f"  DATA: [DONE]")
                                    else:
                                        preview = d[:120] + "..." if len(d) > 120 else d
                                        print(f"  DATA: {preview}")
                        if frame == "data: [DONE]":
                            break
                if chunks:
                    print(f"\n  Total chunks: {len(chunks)}")
                    # Show last chunk
                    last = chunks[-1] if chunks else "N/A"
                    print(f"  Last: {last[:100]}")
    except Exception as e:
        print(f"  ERROR: {e}")
    
    print("\n" + "=" * 60)
    print("2. Through hermes_tool_filter (port 9099/30000)")
    print("=" * 60)
    
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as sess:
            async with sess.post(tools_url, json=data, headers=headers) as resp:
                print(f"Status: {resp.status}, CT: {resp.headers.get('content-type')}")
                buffer = ""
                chunks = []
                async for line in resp.content:
                    buffer += line.decode("utf-8", errors="replace")
                    while "\n\n" in buffer:
                        frame, buffer = buffer.split("\n\n", 1)
                        chunks.append(frame)
                        if len(chunks) <= 20:
                            lines = frame.strip().split("\n")
                            for l in lines:
                                if l.startswith("event: "):
                                    print(f"  EVENT: {l[7:]}")
                                elif l.startswith("data:"):
                                    d = l[5:].strip()
                                    if d == "[DONE]":
                                        print(f"  DATA: [DONE]")
                                    else:
                                        preview = d[:120] + "..." if len(d) > 120 else d
                                        print(f"  DATA: {preview}")
                        if frame == "data: [DONE]":
                            break
                if chunks:
                    print(f"\n  Total chunks: {len(chunks)}")
                    last = chunks[-1] if chunks else "N/A"
                    print(f"  Last: {last[:100]}")
    except Exception as e:
        print(f"  ERROR: {e}")

asyncio.run(compare_streams())
