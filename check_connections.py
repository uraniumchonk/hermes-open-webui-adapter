#!/usr/bin/env python3
"""獲取 Open WebUI 完整連接設定與 api_type"""

import asyncio
import json
import httpx

OWUI_URL = "http://127.0.0.1:30010"
EMAIL = "87000861+uraniumchonk@users.noreply.github.com"
PW = "Th19731112"

async def main():
    async with httpx.AsyncClient(timeout=15) as client:
        # 登入
        resp = await client.post(
            f"{OWUI_URL}/api/v1/auths/signin",
            json={"email": EMAIL, "password": PW},
        )
        if resp.status_code != 200:
            print(f"Login failed: {resp.status_code}")
            return
        token = resp.json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        
        # 獲取完整 config
        resp = await client.get(f"{OWUI_URL}/openai/config", headers=h)
        config = resp.json()
        
        print("=" * 70)
        print("Open WebUI OpenAI 連接設定")
        print("=" * 70)
        
        urls = config.get('OPENAI_API_BASE_URLS', [])
        keys = config.get('OPENAI_API_KEYS', [])
        configs = config.get('OPENAI_API_CONFIGS', {})
        
        for i, url in enumerate(urls):
            print(f"\n--- 連接 [{i}] ---")
            print(f"  URL: {url}")
            print(f"  Key: {keys[i] if i < len(keys) else 'N/A'}")
            
            conn_config = configs.get(str(i), configs.get(url, {}))
            if conn_config:
                at = conn_config.get('api_type', '(未設定=chat_completions)')
                print(f"  API Type: {at}")
                print(f"  Name: {conn_config.get('name', 'N/A')}")
                print(f"  Enable: {conn_config.get('enable', True)}")
                print(f"  Auth Type: {conn_config.get('auth_type', 'N/A')}")
                print(f"  Connection Type: {conn_config.get('connection_type', 'N/A')}")
                print(f"  Prefix ID: {conn_config.get('prefix_id', 'N/A')}")
                print(f"  Provider: {conn_config.get('provider', 'N/A')}")
                
                for k, v in conn_config.items():
                    if k not in ['enable','tags','prefix_id','model_ids','connection_type','auth_type','name','api_type','provider']:
                        print(f"  [{k}]: {v}")
            else:
                print(f"  (無其他設定)")
        
        print("\n" + "=" * 70)
        print("總結:")
        print(f"  總連接數: {len(urls)}")
        has_responses = any(
            configs.get(str(i), {}).get('api_type') == 'responses' 
            for i in range(len(urls))
        )
        print(f"  有 Responses 模式: {'是' if has_responses else '否 (全部預設 Chat Completions)'}")

if __name__ == "__main__":
    asyncio.run(main())
