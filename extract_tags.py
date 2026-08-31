#!/usr/bin/env python3
"""Extract special tags into special_tags.json (clean, no jinja false positives).
Actual tag strings never pass through the agent's output."""
import json
import re
from pathlib import Path

JINJA = Path("/home/thomas2018/models/Qwen3.8-27B-AWQ-INT4/chat_template.jinja")
OWUI_MW = Path("/home/thomas2018/openwebui/lib/python3.12/site-packages/open_webui/utils/middleware.py")
OUT = Path("/home/thomas2018/hermes_tool_filter/special_tags.json")

def extract_jinja_tags(text: str) -> list[str]:
    tags = []
    for m in re.finditer(r"<([a-zA-Z/][^<>{}]*?)>", text):
        tag = m.group(0)
        # 過濾 jinja 模板代碼片段（含 + 字串連接或引號的都不是真標籤）
        if "+" in tag or "'" in tag or '"' in tag:
            continue
        if tag not in tags:
            tags.append(tag)
    return tags

def extract_owui_pairs(text: str, var_name: str) -> list[list[str]]:
    m = re.search(rf"{var_name}\s*=\s*\[(.*?)\]", text, re.DOTALL)
    if not m:
        return []
    return [[p.group(1), p.group(2)] for p in re.finditer(r"\('([^']*)',\s*'([^']*)'\)", m.group(1))]

jinja_tags = extract_jinja_tags(JINJA.read_text(encoding="utf-8"))
owui_text = OWUI_MW.read_text(encoding="utf-8")
reasoning_pairs = extract_owui_pairs(owui_text, "DEFAULT_REASONING_TAGS")
solution_pairs = extract_owui_pairs(owui_text, "DEFAULT_SOLUTION_TAGS")
code_interp_pairs = extract_owui_pairs(owui_text, "DEFAULT_CODE_INTERPRETER_TAGS")

# 合併所有需要 neutralize 的 tag（開+關都列）
all_tags = list(dict.fromkeys(jinja_tags))
for pair in reasoning_pairs + solution_pairs + code_interp_pairs:
    for t in pair:
        if t not in all_tags:
            all_tags.append(t)
# filter 自己的 details tag
for t in ("<details", "</details>"):
    if t not in all_tags:
        all_tags.append(t)

data = {
    "_comment": "特殊標籤清單（自動提取自 jinja + OWUI 0.11.1）。filter 用此清單 neutralize tool result body 內的標籤，避免 OWUI tag 偵測通拉跳脫。",
    "source": {
        "jinja": str(JINJA),
        "owui_middleware": str(OWUI_MW),
    },
    "model_jinja_tags": jinja_tags,
    "owui_reasoning_tags": reasoning_pairs,
    "owui_solution_tags": solution_pairs,
    "owui_code_interpreter_tags": code_interp_pairs,
    "filter_details_tags": ["<details", "</details>"],
    "all_tags_to_neutralize": all_tags,
}

OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Wrote {OUT}")
print(f"jinja_tags={len(jinja_tags)}, reasoning_pairs={len(reasoning_pairs)}, total_to_neutralize={len(all_tags)}")
print("all_tags_to_neutralize:")
for t in all_tags:
    print(f"  {t}")
