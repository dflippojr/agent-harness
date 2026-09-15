"""Example app: a shopping list that lives in this script, driven by an agent on the harness.

The agent runs on the tower; the list and the tools stay here. Context (the pantry) is injected at session start.

    set HARNESS_URL=https://tower.your-tailnet.ts.net   (or http://127.0.0.1:8100 on the tower)
    set HARNESS_TOKEN=ha-...                            (Settings → Apps, scope "sessions")
    python sdk/examples/shopping_list_app.py "Plan a simple dinner for four and add what's missing to my list"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness_client import Harness, tool  # noqa: E402

shopping: list[str] = ["milk"]
PANTRY = "rice, eggs, olive oil, garlic, salt, pepper, frozen peas"


@tool("Show the current shopping list", required=[])
def list_items() -> str:
    return "\n".join(f"- {item}" for item in shopping) or "(empty)"


@tool("Add one item to the shopping list", item={"type": "string", "description": "e.g. 2 lemons"})
def add_item(item: str) -> str:
    if item.lower() in (i.lower() for i in shopping):
        return f"{item} is already on the list"
    shopping.append(item)
    return f"added {item}"


def main() -> None:
    prompt = " ".join(sys.argv[1:]) or "Plan a simple dinner for four and add what's missing to my shopping list."
    h = Harness(os.environ.get("HARNESS_URL", "http://127.0.0.1:8100"), os.environ["HARNESS_TOKEN"])

    def show(event: dict) -> None:
        if event["type"] == "app_tool_call":
            print(f"  agent -> {event['data']['name']}({event['data']['args']})")
        elif event["type"] == "status":
            print(f"  [{event['data']['status']}]")

    result = h.run(prompt, context={"Pantry (already at home)": PANTRY}, tools=[list_items, add_item],
                   metadata={"example": "shopping_list_app"}, on_event=show)
    print(f"\nsession {result.session['id']} {result.status}\n\n{result.answer}\n\nShopping list now:")
    print(list_items.fn())


if __name__ == "__main__":
    main()
