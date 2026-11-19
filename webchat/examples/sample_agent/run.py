"""Sample reference script for the imported-Agent demo.

Input:
  argv[1]  the user question
  stdin    JSON: {question, agent: {...}, context: {hits, graph, query}}

Output:
  plain text / Markdown on stdout. Citation markers like [1] map to context.hits.
"""

import json
import sys

question = sys.argv[1] if len(sys.argv) > 1 else ""
payload = json.load(sys.stdin)
hits = payload.get("context", {}).get("hits", [])
agent = payload.get("agent", {})

keywords = ("回滚", "发布", "告警", "值班", "止损", "P1", "事故")
lines = [f"### {agent.get('name', '发布值守助手')}", "", f"**问题**：{question}", ""]

chosen = []
for index, hit in enumerate(hits, 1):
    text = " ".join((hit.get("text") or "").split())
    if any(word in text for word in keywords):
        chosen.append((index, hit, text))
if not chosen:
    chosen = [(index, hit, " ".join((hit.get("text") or "").split())) for index, hit in enumerate(hits[:2], 1)]

lines.append("**值守建议**")
for index, hit, text in chosen[:3]:
    lines.append(f"- {text[:220]} [{index}]")

lines += [
    "",
    "**接下来 3 步**",
    "1. 在发布平台回滚到上一个稳定版本；",
    "2. 观察错误率与 P99 五分钟；",
    "3. 在值班群同步事故编号、影响范围和回滚结果。",
]
print("\n".join(lines))
