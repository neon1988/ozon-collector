"""Contract for audit 2026-10-18, step 9: collect.py JS literals are separate files.

The module constants are assembled from ozon_collector/js/*.js via _load_js and
must be an EXACT copy of the file: the literals inflated collect.py to ~3100
lines, but the behavior (the JS text) must not change.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ozon_collector import collect as C

GOLDEN = json.loads((Path(__file__).with_name("js_golden.json")).read_text(encoding="utf-8"))


def test_js_constants_are_loaded_from_files():
    for attr, fname in C._JS_FILES.items():
        path = C._JS_DIR / fname
        assert path.is_file(), f"{fname} отсутствует"
        assert path.read_text(encoding="utf-8") == getattr(C, attr), \
            f"{attr}: содержимое {fname} разошлось с константой"


def test_js_files_are_utf8_no_bom_and_nonempty():
    for fname in C._JS_FILES.values():
        raw = (C._JS_DIR / fname).read_bytes()
        assert raw, f"{fname} пуст"
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{fname}: BOM"
        raw.decode("utf-8")  # UnicodeDecodeError if the file is not UTF-8


def test_all_js_attrs_are_registered_in_js_files():
    """The other side of the registry: a new JS constant must be added to
    _JS_FILES, otherwise the file stays inlined in collect.py."""
    missing = [name for name in vars(C)
               if name.endswith("_JS") and isinstance(getattr(C, name), str)
               and name not in C._JS_FILES]
    assert missing == [], f"не в _JS_FILES: {missing}"


def test_js_files_match_golden_hashes():
    """Golden sha256 taken from the HEAD literals during the step 9 extraction:
    each file must stay byte-identical to the literal it replaced. Registry
    equality tests alone are tautological (they compare the loader with the
    loaded file), golden hashes freeze the content independently."""
    for fname, digest in GOLDEN.items():
        actual = hashlib.sha256((C._JS_DIR / fname).read_bytes()).hexdigest()
        assert actual == digest, f"{fname}: содержимое разошлось с golden-хешем"


def test_golden_hashes_cover_the_whole_registry():
    assert sorted(GOLDEN) == sorted(C._JS_FILES.values())
