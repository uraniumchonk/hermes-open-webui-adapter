#!/usr/bin/env python3
"""
測試修改後的 API Server SSE 輸出，檢查 completed 事件是否包含 result 和 arguments。
"""
import asyncio
import aiohttp
import json

async def test_api_server():
    url = "http://127.0.0.1:30000/v1/chat/completions"
    headers = {
        "Authorization": "Bearer hermes_chat_key",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "qwen-27b-default",
        "messages": [
            {"role": "user", "content": "用 web_search 搜尋台中明天天氣"}
        ],
        "stream": True,
        "max_tokens": 300,
    }
    
    print("=" * 80)
    print("開始測試 API Server SSE 輸出...")
    print("=" * 80)
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            print(f"\nHTTP Status: {resp.status}\n")
            
            async for line in resp.content:
                line_str = line.decode('utf-8', errors='replace').strip()
                
                if not line_str or line_str.startswith(':'):
                    continue
                
                if line_str.startswith('event: hermes.tool.progress'):
                    print(f"\n{line_str}")
                    # 讀取下一行 (data:)
                    next_line = (await resp.content.readline()).decode('utf-8', errors='replace').strip()
                    print(f"{next_line}")
                    
                    try:
                        data = json.loads(next_line[5:])
                        print(f"  解析結果: {json.dumps(data, ensure_ascii=False, indent=2)}")
                    except:
                        pass
                
                if line_str.startswith('data:') and '[DONE]' in line_str:
                    print(f"\n{line_str}")
                    break
    
    print("\n" + "=" * 80)

if __name__ == "__main__":
    asyncio.run(test_api_server())
