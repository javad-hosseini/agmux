import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from artifacts import (
    collect_changed_files,
    deliver_artifacts,
    snapshot_dir,
)
from db import get_session, log_audit, update_session
from queue_manager import queue_mgr

logger = logging.getLogger(__name__)

AGY_BIN_CONFIG = os.getenv("AGY_BIN", "")
AGY_BIN = AGY_BIN_CONFIG or shutil.which("agy") or "/home/javad/.local/bin/agy"
BRAIN_ROOT = Path(os.getenv("BRAIN_ROOT", Path.home() / ".gemini" / "antigravity" / "brain"))
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
OUTBOX_ROOT = DATA_DIR / "artifacts"
ARTIFACTS_BASE_URL = os.getenv("ARTIFACTS_BASE_URL", "").rstrip("/") or None

# Active subprocess registry: session_id -> Process
ACTIVE_PROCS: Dict[int, asyncio.subprocess.Process] = {}


def build_prompt(
    user_prompt: str,
    mode: str,
    outbox_dir: Path,
    btw_notes: Optional[List[str]] = None,
) -> str:
    parts = []
    if btw_notes:
        notes_str = "\n".join(f"- {n}" for n in btw_notes)
        parts.append(f"[USER ASIDE / BY-THE-WAY CONTEXT:\n{notes_str}]\n")

    parts.append(user_prompt)

    if mode == "PLAN":
        directive = (
            "\n\n[SYSTEM DIRECTIVE: OPERATIONAL CONSTRAINT: You are in READ-ONLY / PLAN mode. "
            "Inspect code, logs, and git state freely, and write your proposed implementation plan to "
            f"`{outbox_dir}/implementation-plan.md`. "
            "DO NOT modify repository source files, run database migrations, or restart services.]"
        )
    else:
        directive = (
            "\n\n[SYSTEM DIRECTIVE: OPERATIONAL CONSTRAINT: You are in EXECUTION mode. "
            "You are authorized to modify repository files, run test suites, and write your summary walkthrough to "
            f"`{outbox_dir}/walkthrough.md`. "
            "Do not deploy to production unless explicitly instructed.]"
        )
    parts.append(directive)
    return "".join(parts)


def parse_ndjson_line(line: str) -> Dict[str, Any]:
    """Parse a single NDJSON line and extract structured information."""
    res = {
        "conversation_id": None,
        "step_index": None,
        "step_type": None,
        "state": None,
        "tool_name": None,
        "tool_target": None,
        "text_delta": None,
        "final_result": None,
        "error": None,
    }
    try:
        evt = json.loads(line)
    except json.JSONDecodeError:
        res["text_delta"] = line
        return res

    event_type = evt.get("event")

    # Conversation ID extraction
    if "conversation_id" in evt:
        res["conversation_id"] = evt["conversation_id"]

    # Init event
    if event_type == "init":
        init_obj = evt.get("init", {})
        if "conversation_id" in init_obj:
            res["conversation_id"] = init_obj["conversation_id"]

    # Step update event
    elif event_type == "step_update":
        su = evt.get("step_update", {})
        if "conversation_id" in su and not res["conversation_id"]:
            res["conversation_id"] = su["conversation_id"]

        res["step_index"] = su.get("step_index")
        res["step_type"] = su.get("step_type")
        res["state"] = su.get("state")

        st = res["step_type"]
        if st == "agent_response":
            delta = su.get("text_delta")
            if delta:
                res["text_delta"] = delta
        elif st == "tool":
            t_name = su.get("tool_name") or su.get("tool_info", {}).get("name")
            res["tool_name"] = t_name
            params = su.get("tool_info", {}).get("parameters", {})
            if isinstance(params, dict):
                target = (
                    params.get("CommandLine")
                    or params.get("Path")
                    or params.get("query")
                    or params.get("subagent_id")
                    or params.get("name")
                )
                if target:
                    res["tool_target"] = str(target)[:80]

    # Result event
    elif event_type == "result":
        r = evt.get("result", {})
        if "conversation_id" in r and not res["conversation_id"]:
            res["conversation_id"] = r["conversation_id"]
        res["final_result"] = r.get("response")
        if r.get("status") == "ERROR":
            res["error"] = r.get("error") or "Execution failed"

    return res


def format_activity_log(
    project_slug: str,
    session_id: int,
    mode: str,
    steps: List[Dict[str, Any]],
    is_finished: bool = False,
    error: Optional[str] = None,
) -> str:
    mode_icon = "🔍" if mode == "PLAN" else "🚀"
    
    if error:
        status_line = f"❌ **[{project_slug} | #{session_id} | {mode}]** Failed"
    elif is_finished:
        status_line = (
            f"✅ **[{project_slug} | #{session_id} | {mode}]** Finished "
            f"({len(steps)} command{'s' if len(steps) != 1 else ''} executed)"
        )
    else:
        status_line = f"{mode_icon} **[{project_slug} | #{session_id} | {mode}]** Running..."

    if not steps:
        if is_finished:
            return f"{status_line}\n\n_No external commands were executed._"
        return f"{status_line}\n\n⏳ _Analyzing prompt and codebase..._"

    lines = [status_line, "", "📋 **Command History:**"]
    step_lines = []
    for i, s in enumerate(steps, 1):
        name = s.get("name") or "tool"
        target = s.get("target")
        state = s.get("state")
        
        icon = "⚙️" if "command" in name.lower() else ("📄" if "file" in name.lower() else "🔧")
        state_icon = "⏳" if state == "ACTIVE" and not is_finished else "✅"
        target_str = f": `{target}`" if target else ""
        step_lines.append(f"{i}. {state_icon} {icon} `{name}`{target_str}")

    full_body = "\n".join(step_lines)
    if len(full_body) > 3400:
        truncated_count = len(step_lines) - 18
        selected = (
            step_lines[:3]
            + [f"_... ({truncated_count} intermediate commands hidden) ..._"]
            + step_lines[-15:]
        )
        lines.extend(selected)
    else:
        lines.extend(step_lines)

    if error:
        lines.append(f"\n⚠️ _Error:_ `{error[:200]}`")

    return "\n".join(lines)


# Backwards compatibility alias
def build_status_text(
    project_slug: str,
    session_id: int,
    mode: str,
    tool_name: Optional[str] = None,
    tool_target: Optional[str] = None,
) -> str:
    steps = []
    if tool_name:
        steps.append({"name": tool_name, "target": tool_target, "state": "ACTIVE"})
    return format_activity_log(project_slug, session_id, mode, steps, is_finished=False)


async def execute_agy_turn(
    bot: Bot,
    chat_id: int,
    session: Dict[str, Any],
    project_path: str,
    user_prompt: str,
    status_msg_id: int,
) -> Tuple[str, List[Path], Path]:
    session_id = session["id"]
    project_slug = session["project_slug"]
    mode = session.get("mode", "PLAN")
    conv_id = session.get("agy_conv_id")

    outbox_dir = OUTBOX_ROOT / f"session_{session_id}"
    outbox_dir.mkdir(parents=True, exist_ok=True)

    watch_dirs = [outbox_dir]
    if conv_id and (BRAIN_ROOT / conv_id).exists():
        watch_dirs.append(BRAIN_ROOT / conv_id)

    before_snap = {}
    for d in watch_dirs:
        before_snap.update(snapshot_dir(d))

    btw_notes = queue_mgr.pop_btw_notes(session_id)
    full_prompt = build_prompt(user_prompt, mode, outbox_dir, btw_notes)

    # agy CLI command formulation
    cli_mode = "plan" if mode == "PLAN" else "accept-edits"
    cmd = [
        AGY_BIN,
        "-p",
        full_prompt,
        "--dangerously-skip-permissions",
        "--mode",
        cli_mode,
        "--output-format",
        "stream-json",
    ]
    if conv_id:
        cmd.extend(["--conversation", conv_id])

    await update_session(session_id, status="RUNNING", last_prompt=user_prompt)
    await log_audit(
        session_id,
        "PROMPT",
        {"prompt": user_prompt, "mode": mode, "conv_id": conv_id, "btw_notes": btw_notes},
    )

    logger.info(f"Executing session #{session_id} in {project_path}: {' '.join(cmd[:6])}...")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=project_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    ACTIVE_PROCS[session_id] = proc

    last_edit = time.monotonic()
    collected_text: List[str] = []
    final_result_text: Optional[str] = None
    stderr_lines: List[str] = []

    steps: List[Dict[str, Any]] = []
    step_index_map: Dict[int, int] = {}
    last_rendered_text: str = ""

    async def update_telegram_status(is_finished: bool = False, error: Optional[str] = None):
        nonlocal last_rendered_text
        text = format_activity_log(
            project_slug=project_slug,
            session_id=session_id,
            mode=mode,
            steps=steps,
            is_finished=is_finished,
            error=error,
        )
        if text == last_rendered_text and not is_finished:
            return
        last_rendered_text = text
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg_id,
                text=text,
                parse_mode="Markdown",
            )
        except TelegramAPIError as tg_err:
            logger.debug(f"Status message edit skipped: {tg_err}")

    async def read_stderr():
        if proc.stderr:
            async for line in proc.stderr:
                decoded = line.decode("utf-8", errors="replace").strip()
                if decoded:
                    stderr_lines.append(decoded)

    stderr_task = asyncio.create_task(read_stderr())

    try:
        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            parsed = parse_ndjson_line(line)

            # Update conversation ID as soon as observed
            new_cid = parsed["conversation_id"]
            if new_cid and not conv_id:
                conv_id = new_cid
                await update_session(session_id, agy_conv_id=conv_id)
                brain_dir = BRAIN_ROOT / conv_id
                if brain_dir.exists() and brain_dir not in watch_dirs:
                    watch_dirs.append(brain_dir)
                    before_snap.update(snapshot_dir(brain_dir))

            # Collect output
            if parsed["text_delta"]:
                collected_text.append(parsed["text_delta"])
            if parsed["final_result"]:
                final_result_text = parsed["final_result"]

            # Tool and step tracking
            t_name = parsed["tool_name"]
            if t_name:
                s_idx = parsed.get("step_index")
                t_target = parsed.get("tool_target")
                t_state = parsed.get("state") or "DONE"

                if s_idx is not None and s_idx in step_index_map:
                    pos = step_index_map[s_idx]
                    steps[pos]["state"] = t_state
                    if t_target and not steps[pos].get("target"):
                        steps[pos]["target"] = t_target
                else:
                    new_step = {
                        "index": s_idx,
                        "name": t_name,
                        "target": t_target,
                        "state": t_state,
                    }
                    steps.append(new_step)
                    if s_idx is not None:
                        step_index_map[s_idx] = len(steps) - 1

            # Debounced status edits (3.0s minimum interval)
            now = time.monotonic()
            if now - last_edit >= 3.0:
                last_edit = now
                await update_telegram_status(is_finished=False)

        await proc.wait()
        await stderr_task

        # Final edit marking commands completed
        await update_telegram_status(is_finished=True)

    except asyncio.CancelledError:
        logger.warning(f"Task for session #{session_id} cancelled.")
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                proc.kill()
        await update_telegram_status(is_finished=True, error="Operation cancelled by user.")
        raise
    except Exception as e:
        logger.error(f"Error in session #{session_id} execution: {e}", exc_info=True)
        await log_audit(session_id, "ERROR", {"error": str(e)})
        await update_telegram_status(is_finished=True, error=str(e))
        raise
    finally:
        ACTIVE_PROCS.pop(session_id, None)

    # Post-execution status update
    await update_session(session_id, status="IDLE")

    # Collect changed files (artifacts) to return to main.py
    changed_files = collect_changed_files(before_snap, watch_dirs)

    # Determine final response string
    result = final_result_text or "".join(collected_text).strip()
    if not result:
        if stderr_lines:
            result = "⚠️ Process exited with warnings/errors:\n" + "\n".join(stderr_lines[-10:])
        else:
            result = "✅ Turn finished with no text output. (Check any attached artifacts below)."

    await log_audit(
        session_id,
        "TURN_COMPLETED",
        {
            "exit_code": proc.returncode,
            "response_len": len(result),
            "steps_count": len(steps),
            "artifacts_count": len(changed_files),
        },
    )

    return result, changed_files, outbox_dir
