import time
from pathlib import Path
from artifacts import (
    collect_changed_files,
    format_file_size,
    is_blocked,
    scrub_file_secrets,
    snapshot_dir,
)


def test_is_blocked():
    assert is_blocked(Path(".env"))
    assert is_blocked(Path(".env.local"))
    assert is_blocked(Path("server.key"))
    assert is_blocked(Path("cert.pem"))
    assert is_blocked(Path("id_rsa"))
    assert is_blocked(Path("credentials.json"))
    assert is_blocked(Path("app.sqlite"))
    assert not is_blocked(Path("walkthrough.md"))
    assert not is_blocked(Path("implementation-plan.md"))
    assert not is_blocked(Path("diagram.png"))


def test_snapshot_and_collect(tmp_path):
    outbox = tmp_path / "artifacts"
    outbox.mkdir()

    f1 = outbox / "plan.md"
    f1.write_text("# Plan")

    before = snapshot_dir(outbox)
    assert f1.resolve() in before

    # No changes yet
    changed = collect_changed_files(before, [outbox])
    assert changed == []

    # Modify f1 and create f2
    time.sleep(0.01)
    f1.write_text("# Plan Updated")
    f2 = outbox / "walkthrough.md"
    f2.write_text("# Walkthrough")

    changed = collect_changed_files(before, [outbox])
    changed_resolved = [p.resolve() for p in changed]
    assert f1.resolve() in changed_resolved
    assert f2.resolve() in changed_resolved


def test_scrub_file_secrets(tmp_path):
    doc = tmp_path / "report.md"
    sensitive_content = (
        "Here is the report:\n"
        "GitHub token: ghp_1234567890abcdef1234567890abcdef\n"
        "OpenAI key: sk-abcdef1234567890abcdef1234567890\n"
        "Database URL: postgres://user:SuperSecretPassword123@db.example.com:5432/mydb\n"
        "All good."
    )
    doc.write_text(sensitive_content)

    scrubbed = scrub_file_secrets(doc)
    assert scrubbed is True

    result_text = doc.read_text()
    assert "SuperSecretPassword123" not in result_text
    assert "ghp_1234567890" not in result_text
    assert "sk-abcdef123456" not in result_text
    assert "[REDACTED_SECRET]" in result_text


def test_format_file_size():
    assert format_file_size(500) == "500 B"
    assert format_file_size(1024) == "1.0 KB"
    assert format_file_size(2 * 1024 * 1024) == "2.0 MB"

