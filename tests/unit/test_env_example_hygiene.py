"""Credential hygiene guard (added after two real incidents this session:
CoinDCX API credentials, then a Telegram bot token, both accidentally
pasted into .env.example instead of .env). This test scans .env.example
structurally -- key names and placeholder markers only -- and never reads
or asserts against real secret values, so it works identically whether or
not real credentials are configured anywhere on the machine running it.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
GITIGNORE_PATH = REPO_ROOT / ".gitignore"

SECRET_KEY_PATTERN = re.compile(r"(API_KEY|API_SECRET|BOT_TOKEN|CHAT_ID|SECRET_KEY|PASSWORD)$")
PLACEHOLDER_MARKERS = ("your-", "changeme", "change-me", "example", "-here")
# Deliberately does NOT include generic filler like "xxx"/"yyy" -- a real
# high-entropy secret has a non-trivial chance of containing 3 repeated
# characters somewhere, which would silently defeat this check. Every
# placeholder actually used in .env.example matches one of the markers
# above (verified 2026-08-26); keep this list in sync with that file.


def _parse_env_file(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def _looks_like_placeholder(value: str) -> bool:
    if value == "":
        return True
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def test_env_example_has_no_populated_secrets():
    """Every secret-shaped key (API_KEY/API_SECRET/BOT_TOKEN/CHAT_ID/
    SECRET_KEY/PASSWORD) in .env.example must be empty or a recognizable
    placeholder -- never a real-looking value. This would have caught both
    incidents this session immediately."""
    assert ENV_EXAMPLE_PATH.exists(), ".env.example is missing"
    values = _parse_env_file(ENV_EXAMPLE_PATH)

    violations = []
    for key, value in values.items():
        if SECRET_KEY_PATTERN.search(key) and not _looks_like_placeholder(value):
            violations.append(f"{key} (len={len(value)})")  # length only, never the value itself

    assert violations == [], (
        f".env.example appears to contain real secret value(s) for: {violations}. "
        "Real credentials must only ever live in .env (gitignored), never in .env.example."
    )


def test_env_is_gitignored():
    assert GITIGNORE_PATH.exists(), ".gitignore is missing"
    patterns = [line.strip() for line in GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()]
    assert ".env" in patterns, ".env must be listed in .gitignore"


def test_no_git_tracked_file_is_named_env():
    """If a git repository exists, .env itself must never be a tracked
    file (belt-and-suspenders alongside the .gitignore check -- a file
    added before .gitignore existed would otherwise slip through). No-ops
    cleanly if no repository has been initialized yet."""
    git_dir = REPO_ROOT / ".git"
    if not git_dir.exists():
        pytest_skip_reason = "no .git directory -- repository not yet initialized, nothing can be tracked"
        import pytest
        pytest.skip(pytest_skip_reason)

    import subprocess
    result = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    tracked = set(result.stdout.splitlines())
    assert ".env" not in tracked, ".env must never be a git-tracked file"
