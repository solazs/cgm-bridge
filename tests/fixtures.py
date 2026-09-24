"""Payloads in the exact shape Juggluco's Nightscout v1 uploader produces.

Hand-written from the format strings in Juggluco's uploader.cpp / common.cpp (not copied data):

    entries:    {"type":"sgv","device":..,"dateString":..,"date":<ms>,"sgv":..,"delta":%.3f,
                 "direction":..,"noise":1,"filtered":..,"unfiltered":..,"rssi":100}
    treatments: {"_id":..,"date":<ms>,"eventType":"<none>","enteredBy":"Juggluco",
                 "created_at":..,<insulin | carbs | "notes":"<label> <value>">}

Entries arrive as one array per sensor; a treatment arrives as a single object.
"""

T0 = 1_790_000_000_000  # 2026-09-21T14:13:20Z, in milliseconds

ENTRIES = (
    b'[{"type":"sgv","device":"3MH00ABCDE","dateString":"2026-09-21T16:13:20.000+0200",'
    b'"date":1790000000000,"sgv":112,"delta":-1.993,"direction":"Flat","noise":1,'
    b'"filtered":112000,"unfiltered":112000,"rssi":100},'
    b'{"type":"sgv","device":"3MH00ABCDE","dateString":"2026-09-21T16:14:20.000+0200",'
    b'"date":1790000060000,"sgv":115,"delta":3.000,"direction":"FortyFiveUp","noise":1,'
    b'"filtered":115000,"unfiltered":115000,"rssi":100}]'
)

# printf("%.3f", NAN) and an undetermined trend ("" direction).
ENTRIES_WITH_NAN = (
    b'[{"type":"sgv","device":"3MH00ABCDE","dateString":"2026-09-21T16:15:20.000+0200",'
    b'"date":1790000120000,"sgv":118,"delta":nan,"direction":"","noise":1,'
    b'"filtered":118000,"unfiltered":118000,"rssi":100},'
    b'{"type":"sgv","device":"3MH00ABCDE","dateString":"2026-09-21T16:16:20.000+0200",'
    b'"date":1790000180000,"sgv":119,"delta":-nan,"direction":"Flat","noise":1,'
    b'"filtered":119000,"unfiltered":119000,"rssi":100}]'
)

TREATMENT_RAPID = (
    b'{"_id":"ba0e12bbbbbbbbbbbbbbbbbb","date":1790000000000,"eventType":"<none>",'
    b'"enteredBy":"Juggluco","created_at":"2026-09-21T14:13:20.000Z",'
    b'"notes":"Rapid-Acting","carbs":null,"insulin":4,"insulinType":"Fast Insulin"}'
)

TREATMENT_CARBS = (
    b'{"_id":"ba0e13bbbbbbbbbbbbbbbbbb","date":1790000060000,"eventType":"<none>",'
    b'"enteredBy":"Juggluco","created_at":"2026-09-21T14:14:20.000Z",'
    b'"carbs":45,"insulin":null}'
)

TREATMENT_BLOOD = (
    b'{"_id":"ba0e14bbbbbbbbbbbbbbbbbb","date":1790000120000,"eventType":"<none>",'
    b'"enteredBy":"Juggluco","created_at":"2026-09-21T14:15:20.000Z",'
    b'"notes":"Blood 7.2","carbs":null,"insulin":null}'
)
