#!/usr/bin/env python3
"""
檢查 Open WebUI 的 Responses 模式設定與行為
"""

import asyncio
import json
import httpx

OWUI_URL = "http://127.0.0.1:30010"
EMAIL = "87000861+uraniumchonk@users.noreply.github.com"
PASSWORD = "Th19731112"

async def check_openwebui():
    async with httpx.AsyncClient(timeout=15) as client:
        # 1. 登入取得 Token
        print("=" * 60)
        print("1. 登入 Open WebUI")
        print("=" * 60)
        
        resp = await client.post(
            f"{OWUI_URL}/api/v1/auths/signin",
            json={"email": EMAIL, "password": PASSWORD},
        )
        
        if resp.status_code != 200:
            print(f"Login failed: {resp.status_code} {resp.text[:200]}")
            return
        
        data = resp.json()
        token = data["token"]
        print(f"✅ Login OK, token: {token[:30]}...")
        
        headers = {"Authorization": f"Bearer {token}"}
        
        # 2. 檢查 Config
        print()
        print("=" * 60)
        print("2. 檢查 Config / Connections")
        print("=" * 60)
        
        resp = await client.get(f"{OWUI_URL}/api/v1/config", headers=headers)
        if resp.status_code == 200:
            config = resp.json()
            print(f"Config keys: {list(config.keys())}")
            # 看有沒有 connections 或 openai 相關
            for key in config:
                val = config[key]
                if isinstance(val, dict) and any(k in str(val).lower() for k in ['api', 'url', 'key', 'responses']):
                    print(f"  {key}: {json.dumps(val, indent=4)[:500]}")
                elif isinstance(val, list):
                    print(f"  {key}: list[{len(val)}]")
        else:
            print(f"Config failed: {resp.status_code}")
        
        # 3. 檢查 OpenAI connections
        print()
        print("=" * 60)
        print("3. 檢查 OpenAI Connections")
        print("=" * 60)
        
        # 檢查多個可能的端點
        endpoints = [
            "/api/v1/connections",
            "/api/v1/openai",
            "/api/v1/openai/connections",
            "/api/v1/models",
            "/api/v1/config",
        ]
        
        for ep in endpoints:
            resp = await client.get(f"{OWUI_URL}{ep}", headers=headers)
            try:
                data = resp.json()
                if isinstance(data, dict) and 'api_type' in data:
                    print(f"  {ep}: api_type={data.get('api_type', 'N/A')}")
                    print(f"    data: {json.dumps(data, indent=2)[:500]}")
                elif isinstance(data, list) and len(data) > 0:
                    for item in data:
                        if isinstance(item, dict) and 'api_type' in item:
                            print(f"  {ep}: name={item.get('name')}, api_type={item.get('api_type')}, url={item.get('url')}")
                        else:
                            print(f"  {ep}: list[{len(data)}] items")
                            if data:
                                print(f"    sample keys: {list(data[0].keys())[:10]}")
                else:
                    print(f"  {ep}: status={resp.status_code}, type={type(data).__name__}")
                    if isinstance(data, dict):
                        print(f"    keys: {list(data.keys())[:10]}")
            except:
                print(f"  {ep}: status={resp.status_code}, not JSON")
        
        # 4. 檢查 databases 中的實際設定
        print()
        print("=" * 60)
        print("4. 檢查 Open WebUI 的資料庫")
        print("=" * 60)
        
        # 找到 Open WebUI 的資料庫位置
        import subprocess
        result = subprocess.run(
            ["find", "/home/user", "-name", "*.db", "-path", "*webui*", "-o", "-name", "*.db", "-path", "*open*webui*", "-o", "-name", "webui.db"],
            capture_output=True, text=True, timeout=10
        )
        print(f"DB search: {result.stdout[:500]}")

if __name__ == "__main__":
    asyncio.run(check_openwebui())
