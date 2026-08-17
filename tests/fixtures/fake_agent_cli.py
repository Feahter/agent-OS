import json
import sys


mode = sys.argv[1]

if mode == "claude":
    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "structured_output": {"answer": "claude"},
                "result": "unused",
                "session_id": "claude-session",
                "total_cost_usd": 0.01,
                "usage": {"input_tokens": 2, "output_tokens": 3},
            }
        )
    )
elif mode == "pi":
    print(json.dumps({"type": "session", "id": "pi-session"}))
    print(
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": '{"answer":"pi"}'}],
                    "stopReason": "stop",
                    "usage": {"totalTokens": 7, "cost": {"total": 0.02}},
                },
            }
        )
    )
elif mode == "codex":
    print(json.dumps({"type": "thread.started", "thread_id": "codex-session"}))
    print(json.dumps({"type": "turn.started"}))
    print(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item-1",
                    "type": "agent_message",
                    "text": '{"answer":"codex"}',
                },
            }
        )
    )
    print(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 5, "cached_input_tokens": 3, "output_tokens": 3},
            }
        )
    )
else:
    print("unknown fake mode", file=sys.stderr)
    raise SystemExit(2)
