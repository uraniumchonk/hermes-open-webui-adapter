#!/bin/bash
curl -s -N http://127.0.0.1:9099/30000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer *** \
  -H "User-Agent: Conduit-Dart" \
  -d '{"model":"hermes-agent","messages":[{"role":"user","content":"請幫我執行命令 echo hello"}],"stream":true}'
