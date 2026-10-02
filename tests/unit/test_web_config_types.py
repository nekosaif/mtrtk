"""`ConfigValues` in web/src/lib/types.ts names every Settings field.

GET /api/config is an untyped dict, so the OpenAPI contract test cannot catch a field the web
type forgot; this one reads the interface's keys and compares them with `Settings`.
"""

import re
from pathlib import Path

from mtrtk.config import Settings

TYPES = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "types.ts"


def test_config_values_names_every_settings_field() -> None:
    text = TYPES.read_text()
    start = text.index("export interface ConfigValues {")
    block = text[start : text.index("\n}\n", start)]
    keys = set(re.findall(r"^  (\w+)\??:", block, re.MULTILINE))
    fields = set(Settings.model_fields)
    assert sorted(fields - keys) == [], "Settings fields missing from ConfigValues"
    assert sorted(keys - fields) == [], "ConfigValues keys that are not Settings fields"
