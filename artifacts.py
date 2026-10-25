import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional
from aiogram import Bot
from aiogram.types import FSInputFile

logger = logging.getLogger(__name__)

BLOCKED_PATTERNS = {
    ".env",
    ".pem",
    ".key",
    "id_rsa",
    ".sqlite",
    ".db",
    "credentials.json",
    "secrets.",
}

# Common secret signatures (API keys, Private keys, DB connection strings)
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bghp_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"(?:postgres|postgresql|mysql|mongodb|redis)://[^:\s]+:[^@\s]+@[^\s/]+"),
]


def is_blocked(path: Path) -> bool:
    name_lower = path.name.lower()
    return any(pattern in name_lower for pattern in BLOCKED_PATTERNS)


def snapshot_dir(directory: Path) -> Dict[Path, int]:
    if not directory.exists() or not directory.is_dir():
        return {}
    snapshot: Dict[Path, int] = {}
    try:
        for p in directory.rglob("*"):
            if p.is_file() and not is_blocked(p):
                try:
                    snapshot[p.resolve()] = p.stat().st_mtime_ns
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError) as e:
        logger.warning(f"Failed to scan directory {directory}: {e}")
    return snapshot


def collect_changed_files(
    before: Dict[Path, int], after_dirs: List[Path]
) -> List[Path]:
    changed: List[Path] = []
    for d in after_dirs:
        current = snapshot_dir(d)
        for path, mtime in current.items():
            if path not in before or before[path] != mtime:
                changed.append(path)
    # Deduplicate and sort by modification timestamp
    seen = set()
    deduped = []
    for p in sorted(changed, key=lambda x: x.stat().st_mtime_ns if x.exists() else 0):
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped


def scrub_file_secrets(file_path: Path) -> bool:
    if not file_path.exists() or not file_path.is_file():
        return False
    # Only scrub readable text files under 2 MB
    if file_path.suffix.lower() not in {
        ".md",
        ".txt",
        ".html",
        ".json",
        ".log",
        ".patch",
        ".diff",
        ".yaml",
        ".yml",
        ".xml",
    }:
        return False

    try:
        size = file_path.stat().st_size
        if size == 0 or size > 2_000_000:
            return False

        content = file_path.read_text(encoding="utf-8", errors="ignore")
        modified = False
        for pattern in SECRET_PATTERNS:
            if pattern.search(content):
                content = pattern.sub("[REDACTED_SECRET]", content)
                modified = True

        if modified:
            file_path.write_text(content, encoding="utf-8")
            return True
    except Exception as e:
        logger.warning(f"Error scrubbing secrets in {file_path}: {e}")
    return False


def format_file_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{size_bytes} B"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


async def deliver_artifacts(
    bot: Bot,
    chat_id: int,
    files: List[Path],
    base_url: Optional[str] = None,
    outbox_dir: Optional[Path] = None,
) -> None:
    for file_path in files:
        if not file_path.exists() or not file_path.is_file():
            continue
        try:
            size = file_path.stat().st_size
            if size == 0:
                continue
            if size > 45 * 1024 * 1024:
                await bot.send_message(
                    chat_id,
                    f"⚠️ Artifact `{file_path.name}` ({format_file_size(size)}) exceeds Telegram 45 MB upload limit.",
                )
                continue

            scrub_file_secrets(file_path)

            caption = f"📎 `{file_path.name}` ({format_file_size(size)})"

            # If web base URL is configured and file is in outbox, append browser preview link
            if base_url and outbox_dir:
                try:
                    rel = file_path.relative_to(outbox_dir)
                    web_link = f"{base_url.rstrip('/')}/{rel}"
                    caption += f"\n🌐 [Open in Browser]({web_link})"
                except ValueError:
                    pass

            inp = FSInputFile(file_path)
            if file_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                try:
                    await bot.send_photo(chat_id, inp, caption=caption, parse_mode="Markdown")
                    continue
                except Exception as photo_err:
                    logger.debug(f"Photo send failed, falling back to document: {photo_err}")

            await bot.send_document(chat_id, inp, caption=caption, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to deliver artifact {file_path}: {e}")
            await bot.send_message(chat_id, f"⚠️ Failed to send artifact `{file_path.name}`: {e}")

