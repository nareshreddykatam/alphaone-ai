"""Phase 4, section 52: no user-facing text anywhere (frontend pages,
Telegram bot messages, API routers, signal-generation reasoning) may claim
"guaranteed profit," "90% accurate," "risk-free," "guaranteed returns," or
similar. Scans the actual source files rather than trusting memory, so
this catches a future regression even if nobody remembers this rule.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN_PATTERNS = [
    re.compile(r"guaranteed\s+(profit|return|income|win)", re.IGNORECASE),
    re.compile(r"\brisk[- ]free\b", re.IGNORECASE),
    re.compile(r"\d+%\s*accurate", re.IGNORECASE),
    re.compile(r"\bguaranteed\s+signals?\b", re.IGNORECASE),
    re.compile(r"\bcannot\s+lose\b", re.IGNORECASE),
    re.compile(r"\bsure\s+thing\b", re.IGNORECASE),
]

SCAN_DIRS = [
    REPO_ROOT / "apps" / "web" / "app",
    REPO_ROOT / "apps" / "web" / "components",
    REPO_ROOT / "apps" / "api" / "routers",
    REPO_ROOT / "services" / "telegram",
    REPO_ROOT / "services" / "signal_engine",
]
SCAN_EXTENSIONS = (".tsx", ".ts", ".py")


def _iter_scan_files():
    for directory in SCAN_DIRS:
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.suffix in SCAN_EXTENSIONS and path.is_file():
                yield path


def test_no_forbidden_financial_claim_language_anywhere_user_facing():
    violations = []
    for path in _iter_scan_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in FORBIDDEN_PATTERNS:
            match = pattern.search(text)
            if match:
                violations.append(f"{path.relative_to(REPO_ROOT)}: matched {pattern.pattern!r} -> {match.group(0)!r}")

    assert violations == [], "Forbidden financial-claim language found:\n" + "\n".join(violations)
