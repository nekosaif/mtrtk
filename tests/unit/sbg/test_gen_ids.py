"""scripts/gen-sbg-ids.py must fail loudly rather than emit silently wrong ids."""

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "gen-sbg-ids.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_sbg_ids", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BLOCK = """typedef enum _SbgEComLog
{
    SBG_ECOM_LOG_STATUS = 1,                /*!< Status. */
    SBG_ECOM_LOG_UTC_TIME = 2,              // trailing line comment
    /* SBG_ECOM_LOG_OLD = 3, a commented-out member */
    // SBG_ECOM_LOG_OLDER = 4,
    /*
    SBG_ECOM_LOG_IN_A_BLOCK = 5,
    */
    SBG_ECOM_LOG_EKF_NAV = 0x08,
    SBG_ECOM_LOG_ECOM_NUM_MESSAGES
} SbgEComLog;
"""


def test_members_skip_comments_and_the_count_sentinel() -> None:
    gen = load()
    block = gen.enum_block(BLOCK, "SbgEComLog")
    assert gen.members(block, "LOG") == {"STATUS": 1, "UTC_TIME": 2, "EKF_NAV": 8}


def test_member_without_explicit_value_fails_loudly() -> None:
    gen = load()
    text = BLOCK.replace("SBG_ECOM_LOG_EKF_NAV = 0x08,", "SBG_ECOM_LOG_EKF_NAV,")
    with pytest.raises(ValueError, match="EKF_NAV"):
        gen.members(gen.enum_block(text, "SbgEComLog"), "LOG")


def test_committed_ids_match_the_header_they_name() -> None:
    """Regenerating from the pinned header reproduces the committed ids.py (needs the header;
    the test downloads nothing and is skipped unless SBG_IDS_HEADER points at a local copy)."""
    header = os.environ.get("SBG_IDS_HEADER")
    if not header:
        pytest.skip("set SBG_IDS_HEADER to a copy of sbgEComIds.h to run")
    gen = load()
    raw = Path(header).read_bytes()
    committed = (SCRIPT.parents[1] / "src/mtrtk/rover/drivers/sbg/ids.py").read_text()
    assert gen.generate(raw, "5.8.935-stable") == committed
