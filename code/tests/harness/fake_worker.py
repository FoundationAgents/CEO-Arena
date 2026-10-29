"""No-network NDJSON worker used by the Python adapter contract test."""

import json
import os
import sys
import time

from ceo_arena.protocol import DECISION_PROTOCOL_VERSION

COMMIT = "1be9fd55a9326d5e7b09786195e5669e311e61b4"


def write(message: object) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


company_id = os.environ["CEO_ARENA_COMPANY_ID"]
write(
    {
        "type": "ready",
        "company_id": company_id,
        "opencode_commit": COMMIT,
        "decision_protocol_version": (
            "ceos-decision-v4"
            if os.environ["CEO_ARENA_MODEL"] == "legacy"
            else DECISION_PROTOCOL_VERSION
        ),
    }
)

for raw in sys.stdin:
    message = json.loads(raw)
    if message["type"] == "shutdown":
        break
    if message["type"] != "decide":
        continue
    if os.environ["CEO_ARENA_MODEL"] == "fail":
        sys.stderr.write(f"api_key={os.environ['CEO_ARENA_API_KEY']}\n")
        sys.stderr.flush()
        raise SystemExit(3)
    if os.environ["CEO_ARENA_MODEL"] == "slow":
        time.sleep(10)
    if os.environ["CEO_ARENA_MODEL"] == "env-leak" and os.getenv("UNRELATED_SECRET"):
        raise SystemExit(4)
    request_id = message["request_id"]
    query_id = "q1"
    write(
        {
            "type": "query",
            "request_id": request_id,
            "query_id": query_id,
            "sql": "SELECT company_id, world_version FROM company",
            "parameters": [],
            "row_limit": 10,
        }
    )
    query = json.loads(sys.stdin.readline())
    assert query["type"] == "query_result" and query["query_id"] == query_id
    row = query["result"]["rows"][0]
    decision = {
        "company_id": row[0],
        "world_version": row[1],
        "products": [],
        "marketing": [],
        "enterprise_bids": [],
        "market_research": [],
    }
    write(
        {
            "type": "result",
            "request_id": request_id,
            "result": {
                "status": "sealed",
                "decision": decision,
                "usage": {
                    "provider_turns": 1,
                    "tool_calls": 2,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "latency_ms": 0,
                },
            },
        }
    )
