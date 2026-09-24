import hashlib
import json
from datetime import UTC, datetime

import pytest

from cgm_bridge.config import Config, ConfigError
from cgm_bridge.exporter import render_import
from cgm_bridge.nightscout import PayloadError, load_json, parse_entries, parse_treatments

from . import fixtures

LONG_SECRET = "a-long-enough-secret-of-32-chars-or-more"


NOW = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)


def test_juggluco_entries():
    readings, skipped = parse_entries(load_json(fixtures.ENTRIES), now=NOW)
    assert skipped == 0
    assert [(r.device, r.mg_dl, r.direction) for r in readings] == [
        ("3MH00ABCDE", 112, "Flat"),
        ("3MH00ABCDE", 115, "FortyFiveUp"),
    ]
    assert readings[0].time == datetime(2026, 9, 21, 14, 13, 20, tzinfo=UTC)
    assert readings[0].delta == pytest.approx(-1.993)


def test_undetermined_trend_has_no_delta():
    readings, skipped = parse_entries(load_json(fixtures.ENTRIES_UNDETERMINED), now=NOW)
    assert skipped == 0
    assert [(r.delta, r.direction) for r in readings] == [(None, None), (1.0, "Flat")]


def test_nan_literals_and_nul_characters_are_neutralised():
    doc = load_json(b'[{"_id":"x\\u0000y","date":1790000000000,"insulin":NaN,"notes":"a\\u0000b"}]')
    assert doc == [{"_id": "xy", "date": 1790000000000, "insulin": None, "notes": "ab"}]


def test_future_readings_are_skipped():
    now = datetime(2026, 9, 21, 14, 0, tzinfo=UTC)
    doc = [
        {"type": "sgv", "date": fixtures.T0, "sgv": 100},  # 13 min ahead: clock skew, kept
        {"type": "sgv", "date": fixtures.T0 + 3_600_000, "sgv": 100},  # an hour ahead
    ]
    readings, skipped = parse_entries(doc, now=now)
    assert (len(readings), skipped) == (1, 1)


def test_single_entry_object_and_date_string_fallback():
    doc = {"type": "sgv", "device": "x", "dateString": "2026-09-21T16:13:20.000+0200", "sgv": 99}
    readings, _ = parse_entries(doc, now=NOW)
    assert readings[0].time == datetime(2026, 9, 21, 14, 13, 20, tzinfo=UTC)


@pytest.mark.parametrize(
    "doc",
    [
        {"type": "mbg", "date": fixtures.T0, "mbg": 120},  # not a sensor reading
        {"type": "sgv", "date": fixtures.T0, "sgv": 0},  # out of range
        {"type": "sgv", "date": fixtures.T0, "sgv": 5000},
        {"type": "sgv", "sgv": 100},  # no time
        {"type": "sgv", "date": fixtures.T0, "sgv": "high"},
        {"type": "sgv", "date": fixtures.T0, "sgv": True},
        "not an object",
    ],
)
def test_unusable_entries_are_skipped(doc):
    readings, skipped = parse_entries([doc], now=NOW)
    assert (readings, skipped) == ([], 1)


def test_invalid_json_is_rejected():
    with pytest.raises(PayloadError):
        load_json(b"[{")
    with pytest.raises(PayloadError):
        load_json(b'[{"sgv":nan}]')  # C printf output is not JSON, and Juggluco never sends it
    with pytest.raises(PayloadError):
        load_json(b"\xff\xfe")
    with pytest.raises(PayloadError):
        parse_entries(load_json(b"42"))


def test_treatments():
    parsed = [
        parse_treatments(load_json(body))[0][0]
        for body in (fixtures.TREATMENT_RAPID, fixtures.TREATMENT_CARBS, fixtures.TREATMENT_BLOOD)
    ]
    rapid, carbs, blood = parsed
    assert (rapid.insulin, rapid.insulin_type, rapid.label, rapid.amount) == (
        4.0,
        "Fast Insulin",
        "Fast Insulin",
        4.0,
    )
    assert (carbs.carbs, carbs.label, carbs.amount) == (45.0, "carbs", 45.0)
    assert (blood.label, blood.amount, blood.notes) == ("Blood", 7.2, "Blood 7.2")
    assert blood.entered_by == "Juggluco"
    assert blood.raw["_id"] == "ba0e14bbbbbbbbbbbbbbbbbb"


def test_treatment_note_variants():
    doc = [
        {"_id": "a", "date": fixtures.T0, "notes": "Long walk 1,5"},
        {"_id": "b", "date": fixtures.T0, "notes": "just a note"},
        {"_id": "c", "created_at": "2026-09-21T14:13:20.000Z"},
        {"date": fixtures.T0, "carbs": 10},  # no id: cannot be updated or deleted later
    ]
    treatments, skipped = parse_treatments(doc)
    assert skipped == 1
    assert [(t.label, t.amount) for t in treatments] == [
        ("Long walk", 1.5),
        (None, None),
        (None, None),
    ]


def test_render_import_groups_by_device():
    t = datetime(2026, 9, 21, 14, 13, 20, tzinfo=UTC)
    body = render_import([("a", t, 100), ("b", t, 200), ("a", t, 101)], "cgm_glucose_mg_dl")
    lines = [json.loads(line) for line in body.decode().splitlines()]
    assert lines == [
        {
            "metric": {"__name__": "cgm_glucose_mg_dl", "device": "a"},
            "values": [100, 101],
            "timestamps": [fixtures.T0, fixtures.T0],
        },
        {
            "metric": {"__name__": "cgm_glucose_mg_dl", "device": "b"},
            "values": [200],
            "timestamps": [fixtures.T0],
        },
    ]


def test_config():
    with pytest.raises(ConfigError):
        Config.from_env({})
    with pytest.raises(ConfigError):
        Config.from_env({"CGM_BRIDGE_API_SECRET": "twelve-chars-is-not-enough"})
    with pytest.raises(ConfigError):
        Config.from_env({"CGM_BRIDGE_API_SECRET": LONG_SECRET, "CGM_BRIDGE_LISTEN_PORT": "x"})
    config = Config.from_env(
        {"CGM_BRIDGE_API_SECRET": LONG_SECRET, "CGM_BRIDGE_VM_URL": "http://vm:8428/"}
    )
    assert config.api_secret_sha1 == hashlib.sha1(LONG_SECRET.encode()).hexdigest()
    assert config.vm_url == "http://vm:8428"
    with pytest.raises(ConfigError):
        Config.from_env(
            {
                "CGM_BRIDGE_API_SECRET": LONG_SECRET,
                "CGM_BRIDGE_VM_URL": "file:///etc/passwd",
            }
        )
