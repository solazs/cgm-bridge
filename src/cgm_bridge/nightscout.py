"""Parsing of Nightscout v1 upload payloads.

Written against what Juggluco actually sends (see tests/fixtures), but tolerant of other
Nightscout uploaders: both a single document and an array of documents are accepted.
"""

import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Plausible sensor range with generous margins. Libre sensors report 40-500 mg/dL; anything
# outside this is a corrupt value, not a reading.
MIN_MG_DL = 10
MAX_MG_DL = 1000

# C's printf renders NaN/Inf as `nan`, `-nan`, `inf`, which is not JSON. Replace such bare
# tokens in value position with null. Only used as a fallback when strict parsing fails.
_NONFINITE = re.compile(r"(?<=[:\[,])(\s*)[-+]?(?:nan|inf(?:inity)?)(?=\s*[,}\]])", re.IGNORECASE)

# Juggluco sends amounts without a dedicated Nightscout field as notes "<label> <value>",
# e.g. "Blood 7.2" for a fingerstick reading.
_LABELLED_AMOUNT = re.compile(
    r"^\s*(?P<label>.*?\S)\s+(?P<value>[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?)\s*$"
)


class PayloadError(ValueError):
    pass


@dataclass(frozen=True)
class GlucoseReading:
    device: str
    time: datetime
    mg_dl: int
    delta: float | None
    direction: str | None


@dataclass(frozen=True)
class Treatment:
    id: str
    time: datetime
    event_type: str | None
    insulin: float | None
    insulin_type: str | None
    carbs: float | None
    notes: str | None
    label: str | None
    amount: float | None
    entered_by: str | None
    raw: dict[str, Any] = field(compare=False)


def load_json(body: bytes) -> Any:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PayloadError("body is not UTF-8") from exc
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return json.loads(_NONFINITE.sub(r"\1null", text))
    except ValueError as exc:
        raise PayloadError(f"body is not valid JSON: {exc}") from exc


def _documents(doc: Any) -> list[Any]:
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        return [doc]
    raise PayloadError("expected a JSON object or array")


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _time(doc: dict[str, Any], *keys: str) -> datetime | None:
    """First usable timestamp among `keys`: epoch milliseconds or an ISO-8601 string."""
    for key in keys:
        value = doc.get(key)
        millis = _finite(value)
        if millis is not None and millis > 0:
            return datetime.fromtimestamp(millis / 1000, tz=UTC)
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
    return None


def parse_entries(doc: Any) -> tuple[list[GlucoseReading], int]:
    """Returns the sensor glucose readings and the number of documents skipped."""
    readings: list[GlucoseReading] = []
    skipped = 0
    for item in _documents(doc):
        if not isinstance(item, dict) or item.get("type", "sgv") != "sgv":
            skipped += 1
            continue
        when = _time(item, "date", "dateString", "sysTime")
        sgv = _finite(item.get("sgv"))
        if when is None or sgv is None or not MIN_MG_DL <= sgv <= MAX_MG_DL:
            skipped += 1
            continue
        readings.append(
            GlucoseReading(
                device=_text(item.get("device")) or "unknown",
                time=when,
                mg_dl=round(sgv),
                delta=_finite(item.get("delta")),
                direction=_text(item.get("direction")),
            )
        )
    return readings, skipped


def parse_treatments(doc: Any) -> tuple[list[Treatment], int]:
    """Returns the treatments and the number of documents skipped."""
    treatments: list[Treatment] = []
    skipped = 0
    for item in _documents(doc):
        if not isinstance(item, dict):
            skipped += 1
            continue
        ident = _text(item.get("_id")) or _text(item.get("identifier"))
        when = _time(item, "date", "timestamp", "created_at")
        if ident is None or when is None:
            skipped += 1
            continue
        insulin = _finite(item.get("insulin"))
        carbs = _finite(item.get("carbs"))
        notes = _text(item.get("notes"))
        label: str | None = None
        amount: float | None = None
        if insulin is not None:
            label, amount = _text(item.get("insulinType")), insulin
        elif carbs is not None:
            label, amount = "carbs", carbs
        elif notes is not None and (match := _LABELLED_AMOUNT.match(notes)):
            label = match["label"]
            amount = _finite(float(match["value"].replace(",", ".")))
        treatments.append(
            Treatment(
                id=ident,
                time=when,
                event_type=_text(item.get("eventType")),
                insulin=insulin,
                insulin_type=_text(item.get("insulinType")),
                carbs=carbs,
                notes=notes,
                label=label,
                amount=amount,
                entered_by=_text(item.get("enteredBy")) or _text(item.get("app")),
                raw=item,
            )
        )
    return treatments, skipped
