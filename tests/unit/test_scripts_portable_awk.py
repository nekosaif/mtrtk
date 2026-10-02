"""Shell scripts must run under both mawk (Debian/Ubuntu's default awk) and gawk.

Found by CI on 2026-10-02: scripts/check-exposure.sh defined an awk function called `xor`.
mawk accepts it; gawk (on GitHub's runners and many hosts) has a built-in `xor` and refuses the
whole program, so every check in the script failed there while passing locally.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = sorted(
    p for p in [*ROOT.glob("scripts/*.sh"), *ROOT.glob("*.sh"), *ROOT.glob("docker/*.sh")]
)
# gawk built-ins a user-defined function may not reuse (gawk manual, "Built-in Functions").
GAWK_BUILTINS = {
    "and",
    "or",
    "xor",
    "compl",
    "lshift",
    "rshift",
    "asort",
    "asorti",
    "gensub",
    "patsplit",
    "strftime",
    "systime",
    "mktime",
    "isarray",
    "typeof",
    "bindtextdomain",
    "dcgettext",
    "dcngettext",
    "length",
    "substr",
    "index",
    "split",
    "sub",
    "gsub",
    "match",
    "sprintf",
    "tolower",
    "toupper",
    "int",
    "sqrt",
    "exp",
    "log",
    "sin",
    "cos",
    "atan2",
    "rand",
    "srand",
    "close",
    "fflush",
    "system",
}
FUNC = re.compile(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_awk_function_shadows_a_gawk_builtin(script: Path) -> None:
    clashes = sorted({m.group(1) for m in FUNC.finditer(script.read_text())} & GAWK_BUILTINS)
    assert not clashes, f"{script.name} defines awk function(s) gawk refuses: {clashes}"
