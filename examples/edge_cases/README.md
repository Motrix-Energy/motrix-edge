# edge_cases/

Hand-authored. **The EMS cannot produce most of these rows** — that is the point. A
`docker kill` mid-write, a truncating disk, a hand-edit, an Excel round-trip, or two
files concatenated from different EMS versions all can, and a consumer meets them on a
real site. They live outside `auto_toggle/expected/` so that a "golden" file stays a
statement about what the EMS actually emits.

Rewrite with `python examples/generate.py --edge-cases`, then update this file — the row
counts below are asserted by `tests/test_storage_contract.py`, so an undocumented row is
a failure.

Every file here is valid CSV *dialect* (parseable by `csv.reader` / PapaParse) except
`device_data.unterminated_quote.csv`, which is the one case that breaks the dialect
rather than the content.

---

## `device_data.csv` — 18 data rows

| # | `device_name` | What it tests |
|---|---|---|
| 1 | `ok_device` | A well-formed control row, so "everything failed" is distinguishable from "the parser died". |
| 2 | `truncated_json` | `data_json` cut mid-object. A process killed while writing. |
| 3 | `not_json` | `data_json` that was never JSON. |
| 4 | `nonfinite` | Bare `NaN` / `Infinity` / `-Infinity`. Invalid JSON, and **historically reachable**: `json.dumps` defaults to `allow_nan=True`, so any device payload containing a non-finite float produced this until `storage/csv_file.py` started serialising strictly. Files written before that fix still contain it. A consumer that gives up on the row loses the whole payload rather than one field. |
| 5 | `empty_timestamp` | Empty timestamp cell. |
| 6 | `bad_timestamp` | Unparseable timestamp. |
| 7 | `out_of_order` | A row earlier than the one above it. **Not corruption** — several connector threads append under one lock, so file order is write order, not event order. Sort on read. |
| 8–9 | `duplicate_key` ×2 | Same `(timestamp, device_name)`, different payloads. Two connectors can deliver within one clock tick. |
| 10 | `comma, and "quote" device` | A device name containing a comma and doubled quotes. Config names are operator-chosen free text; nothing forbids either. |
| 11 | `Compteur électrique` | Non-ASCII in a name and a payload. The files are UTF-8; a consumer decoding as cp1252 gets mojibake. |
| 12 | `deep_nesting` | Twelve levels of nesting, past the depth bound `storage/influxdb.py` and `services/rest_api.py` both stop flattening at. |
| 13 | `empty_payload` | `{}` — valid JSON, no fields. A device that has connected but parsed nothing yet. |
| 14 | `null_payload` | `null` — valid JSON, not an object. |
| 15 | *(empty)* | Empty `device_name`. |
| 16 | `big_payload` | ~2 KB in one field. A timeline that serialises the whole payload to render a one-line preview pays for this on every visible row, every scroll frame. |
| 17 | `ragged_short` | Two fields instead of three. `csv.DictReader` fills the rest with `None`; PapaParse omits the keys. |
| 18 | `ragged_long` | Five fields instead of three. `csv.DictReader` collects the surplus under the `None` key. |

Rows 17 and 18 are written raw, because `csv.writer` cannot emit a row of the wrong
width — which is precisely why a consumer must not assume every row has one.

## `algorithm_decisions.csv` — 9 data rows

| # | What it tests |
|---|---|
| 1 | `command` is `on` — the bare, non-JSON string `AutoToggle` actually emits. |
| 2 | `command` is JSON. Both shapes are legal; `command` is an opaque string. |
| 3 | `command` containing a comma. Quoted by the writer; fatal to anything that splits on `,`. |
| 4 | `command` containing an embedded **CRLF**, so one logical row spans two physical lines. Only reachable in `command`: `json.dumps` escapes control characters, so `data_json` never contains a raw newline. |
| 5 | An algorithm name containing a comma. |
| 6 | A device name containing doubled quotes. |
| 7 | Empty timestamp. |
| 8 | Empty target device. |
| 9 | Empty command. |

## Single-purpose files

| File | What it tests |
|---|---|
| `device_data.empty.csv` | Zero bytes. Distinct from *absent*: a backend that has written nothing leaves **no file at all**, while a pre-created empty one is what the lazy header exists to handle. |
| `device_data.headers_only.csv` | A header and no rows. |
| `device_data.bom.csv` | A UTF-8 BOM before the header — the Excel round-trip. A consumer that ignores it reads the first column as `﻿timestamp` and concludes there is no timestamp column. The EMS never writes one. |
| `device_data.unterminated_quote.csv` | A field opened with `"` and never closed, so the parser is still inside a quoted field at the line break. `csv.reader` does not raise — it silently eats every following line into that one field, and the file's two data rows come back as one. The second row is named `eaten_by_the_row_above` for that reason. Isolated here because, mixed into `device_data.csv`, it would hide every row below it. |
