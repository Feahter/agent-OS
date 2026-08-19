import json
import os
import sys
import time


mode = sys.argv[1]
fault = sys.argv[2] if len(sys.argv) > 2 else None

if fault == "timeout":
    time.sleep(2)
elif fault == "crash":
    print("simulated process crash", file=sys.stderr)
    raise SystemExit(23)
elif fault == "rate-limit":
    print("429 too many requests: rate limit exceeded", file=sys.stderr)
    raise SystemExit(29)
elif fault == "corrupt":
    print("{not-json")
    raise SystemExit(0)
elif fault == "event-error":
    print(
        json.dumps(
            {
                "type": "error",
                "sessionID": "opencode-session",
                "error": {"name": "ProviderError", "message": "provider failed"},
            }
        )
    )
    raise SystemExit(0)
elif fault == "event-rate-limit":
    print(
        json.dumps(
            {
                "type": "error",
                "sessionID": "opencode-session",
                "error": {"status": 429, "message": "too many requests"},
            }
        )
    )
    raise SystemExit(0)

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
elif mode == "opencode":
    arguments = sys.argv[2:]
    if arguments[:4] != ["run", "--format", "json", "--pure"]:
        print("missing OpenCode JSON run protocol", file=sys.stderr)
        raise SystemExit(31)
    if os.environ.get("OPENCODE_DISABLE_PROJECT_CONFIG") != "true":
        print("project config was not disabled", file=sys.stderr)
        raise SystemExit(32)
    permissions = json.loads(os.environ.get("OPENCODE_PERMISSION", "{}"))
    expected = {
        "*": "deny",
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "bash": "deny",
        "edit": "deny",
        "question": "deny",
        "plan_enter": "deny",
        "plan_exit": "deny",
        "webfetch": "deny",
    }
    if permissions != expected:
        print("unexpected OpenCode permissions", file=sys.stderr)
        raise SystemExit(33)
    print(
        json.dumps(
            {
                "type": "text",
                "sessionID": "opencode-session",
                "part": {"type": "text", "text": '{"draft":"ignored"}'},
            }
        )
    )
    print(
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": "opencode-session",
                "part": {
                    "type": "step-finish",
                    "cost": 0.01,
                    "tokens": {"total": 4, "input": 2, "output": 2, "reasoning": 0},
                },
            }
        )
    )
    print(
        json.dumps(
            {
                "type": "text",
                "sessionID": "opencode-session",
                "part": {"type": "text", "text": '{"answer":"opencode"}'},
            }
        )
    )
    print(
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": "opencode-session",
                "part": {
                    "type": "step-finish",
                    "cost": 0.02,
                    "tokens": {"total": 7, "input": 4, "output": 3, "reasoning": 0},
                },
            }
        )
    )
else:
    print("unknown fake mode", file=sys.stderr)
    raise SystemExit(2)
