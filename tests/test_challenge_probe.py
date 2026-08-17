import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from challenge_probe import (_build_report, _delta, _passed, _render_lines,  # noqa: E402
                             _json_dump)


def _sample_report():
    config = {"pid": "1214739369", "url": "https://www.ozon.ru/product/1214739369",
              "port": 9334, "profile": "probe"}
    steps = [
        {"scenario": 0, "passed": False, "steps": [
            {"step": "0-open", "url": "u", "sig": "access_challenge",
             "pass": False, "challenge": True,
             "cookies_before": 10, "cookies_after": 12,
             "cookies_added": ["a"], "cookies_removed": [],
             "elapsed_s": 5.1}]},
        {"scenario": 1, "passed": True, "steps": [
            {"step": "1a-open", "url": "u", "sig": "access_challenge",
             "pass": False, "challenge": True,
             "cookies_before": 12, "cookies_after": 12,
             "cookies_added": [], "cookies_removed": [],
             "elapsed_s": 5.2},
            {"step": "1b-repeat-same-tab", "url": "u", "sig": None,
             "pass": True, "challenge": False,
             "cookies_before": 12, "cookies_after": 12,
             "cookies_added": [], "cookies_removed": [],
             "elapsed_s": 1.3}]},
    ]
    return _build_report(config, "2026-08-17T12:00:00+00:00", steps,
                         verdict="pass", first_pass=1,
                         control={"scenario": 1, "passed": True},
                         duration_s=78.4)


def test_delta_added_removed():
    d = _delta(["a", "b", "c"], ["a", "d"])
    assert d["added"] == ["d"]
    assert d["removed"] == ["b", "c"]


def test_delta_empty():
    assert _delta([], []) == {"added": [], "removed": []}


def _mk_step(pass_ok):
    return {"step": "s", "url": "u", "sig": None if pass_ok else "challenge",
            "pass": pass_ok, "challenge": not pass_ok,
            "cookies_before": 0, "cookies_after": 0,
            "cookies_added": [], "cookies_removed": [], "elapsed_s": 1.0}


def test_passed_by_final_step():
    home = {"step": "home", "url": "ozon.ru/", "sig": None, "pass": False,
            "challenge": False, "cookies_before": 0, "cookies_after": 0,
            "cookies_added": [], "cookies_removed": [], "elapsed_s": 1.0}
    assert _passed([home, _mk_step(True)]) is True
    assert _passed([home, _mk_step(False)]) is False
    assert _passed([]) is False


def test_build_report_fields():
    r = _sample_report()
    assert r["verdict"] == "pass"
    assert r["first_pass_scenario"] == 1
    assert r["control"] == {"scenario": 1, "passed": True}
    assert r["config"]["port"] == 9334
    assert r["duration_s"] == 78.4


def test_render_lines_contains_key_rows():
    lines = _render_lines(_sample_report())
    text = "\n".join(lines)
    assert "scenario 0 (FAIL)" in text
    assert "scenario 1 (PASS)" in text
    assert "CHALLENGE sig='access_challenge'" in text
    assert "1b-repeat-same-tab: PASS" in text
    assert "cookies 10->12 +['a']" in text
    assert "verdict=pass first_pass_scenario=1 control=scenario 1 PASS" in text


def test_render_lines_aborted():
    r = _sample_report()
    r["aborted"] = "3 access_challenge подряд — рекомендую cooldown, стоп"
    lines = _render_lines(r)
    assert any("ABORTED:" in line for line in lines)


def test_json_dump_roundtrip():
    r = _sample_report()
    out = _json_dump(r)
    assert '"verdict": "pass"' in out
    import json
    assert json.loads(out)["config"]["pid"] == "1214739369"
