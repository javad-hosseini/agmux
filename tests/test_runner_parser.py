from pathlib import Path
from runner import build_prompt, build_status_text, parse_ndjson_line


def test_build_prompt_plan_mode():
    prompt = build_prompt("Investigate bug", "PLAN", Path("/tmp/outbox"))
    assert "Investigate bug" in prompt
    assert "READ-ONLY / PLAN mode" in prompt
    assert "/tmp/outbox/implementation-plan.md" in prompt


def test_build_prompt_execute_mode():
    prompt = build_prompt("Fix the bug", "EXECUTE", Path("/tmp/outbox"))
    assert "Fix the bug" in prompt
    assert "EXECUTION mode" in prompt
    assert "/tmp/outbox/walkthrough.md" in prompt


def test_build_prompt_with_btw():
    prompt = build_prompt(
        "Run migration",
        "PLAN",
        Path("/tmp/outbox"),
        btw_notes=["Note 1: use python3.12", "Note 2: dry-run first"],
    )
    assert "[USER ASIDE / BY-THE-WAY CONTEXT:" in prompt
    assert "- Note 1: use python3.12" in prompt
    assert "- Note 2: dry-run first" in prompt


def test_parse_ndjson_init():
    line = '{"event":"init","conversation_id":"conv-abc-123","init":{"cwd":"/repo"}}'
    parsed = parse_ndjson_line(line)
    assert parsed["conversation_id"] == "conv-abc-123"


def test_parse_ndjson_tool():
    line = (
        '{"event":"step_update","step_update":{'
        '"conversation_id":"conv-abc-123",'
        '"step_index":2,'
        '"step_type":"tool",'
        '"tool_name":"run_command",'
        '"tool_info":{"name":"run_command","parameters":{"CommandLine":"git status"}}'
        '}}'
    )
    parsed = parse_ndjson_line(line)
    assert parsed["tool_name"] == "run_command"
    assert parsed["tool_target"] == "git status"


def test_parse_ndjson_agent_response():
    line = (
        '{"event":"step_update","step_update":{'
        '"conversation_id":"conv-abc-123",'
        '"step_index":1,'
        '"step_type":"agent_response",'
        '"text_delta":"Hello world"'
        '}}'
    )
    parsed = parse_ndjson_line(line)
    assert parsed["text_delta"] == "Hello world"


def test_parse_ndjson_result():
    line = '{"event":"result","result":{"conversation_id":"conv-abc-123","status":"SUCCESS","response":"Done!"}}'
    parsed = parse_ndjson_line(line)
    assert parsed["final_result"] == "Done!"
    assert parsed["conversation_id"] == "conv-abc-123"


def test_build_status_text():
    t1 = build_status_text("dentino", 5, "PLAN", "run_command", "git diff")
    assert "dentino" in t1
    assert "#5" in t1
    assert "PLAN" in t1
    assert "run_command" in t1
    assert "git diff" in t1

    t2 = build_status_text("agmux", 1, "EXECUTE")
    assert "EXECUTE" in t2
    assert "Analyzing prompt" in t2


def test_format_activity_log_history():
    from runner import format_activity_log

    steps = [
        {"name": "run_command", "target": "ls -la", "state": "DONE"},
        {"name": "view_file", "target": "db.py", "state": "DONE"},
        {"name": "run_command", "target": "git diff", "state": "ACTIVE"},
    ]
    log_text = format_activity_log("agmux", 2, "PLAN", steps, is_finished=False)
    assert "Command History:" in log_text
    assert "1. ✅ ⚙️ `run_command`: `ls -la`" in log_text
    assert "2. ✅ 📄 `view_file`: `db.py`" in log_text
    assert "3. ⏳ ⚙️ `run_command`: `git diff`" in log_text

    finished_text = format_activity_log("agmux", 2, "PLAN", steps, is_finished=True)
    assert "Finished (3 commands executed)" in finished_text
    assert "3. ✅ ⚙️ `run_command`: `git diff`" in finished_text

