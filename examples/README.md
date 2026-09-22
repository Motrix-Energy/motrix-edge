# examples/

Everything here is public, hardware-free, and safe to copy into an issue. It has two
jobs: give a stranger a working EMS run in one command, and give the
[Motrix Edge View](../docs/storage-format.md#consumers) repository a fixture to test against.

```bash
python main.py --config examples/auto_toggle/config.json
```

That replays three simulated devices through the `AutoToggle` algorithm and writes CSV
output to `data/storage/`. No broker, no hardware, no network. It finishes in about a
second.

---

## `auto_toggle/` — the golden fixture

Not a hand-written sample: `expected/` holds the **actual bytes** `storage/csv_file.py`
writes, produced by running `main.py` over the two committed replay files. That is what
makes it an interface rather than an illustration — if the output format moves,
`tests/test_storage_contract.py` fails, and the failure message says which repository
has to be told.

| File | What it is |
|---|---|
| `config.json` | The configuration. Three `${VAR:-default}` paths let the generator redirect it; run it plainly and it writes to `data/storage`, so the quickstart cannot dirty the fixture. |
| `replay.naive.csv` | 12 timesteps × 3 devices, **naive** timestamps (no UTC offset). |
| `replay.offset.csv` | 6 timesteps × 3 devices, **offset-aware** timestamps across a DST transition. |
| `expected/device_data.csv` | 54 readings. One header, two timestamp domains. |
| `expected/algorithm_decisions.csv` | 18 decisions — six `off`, then twelve `on`. |
| `expected/control.log` | The pseudo connector's debug log. **Not CSV** — see below. |

### Regenerating

```bash
python examples/generate.py           # rewrite in place
python examples/generate.py --check   # rebuild into a temp dir and diff; exit 1 on drift
```

`generate.py` runs `main.py` **twice**, in two separate processes, and appends both runs
to one output directory. Each part of that is load-bearing:

- **Two runs**, because appending a naive run and an aware run to one directory is how
  the fixture reproduces the most awkward property of the real format — a single
  `timestamp` column holding two different time domains. That is not contrived: it is
  what an operator restart produces, and what `data/storage/device_data.csv` in a
  working checkout already looks like.
- **Two files**, because `PseudoConnector` sorts entries by timestamp and comparing a
  naive datetime with an aware one is a `TypeError`. The connector normalises the sort
  key rather than crashing, but a replay mixing the two is a warning, not a model input.
- **Two processes**, because `DevicesManager` and `SimulationClock` are singletons and
  would leak state from the first replay into the second.

The fixture is byte-reproducible. If `--check` reports drift you did not intend, that is
the point of it.

### What each device is there to prove

| Device | Kind | Shape it contributes |
|---|---|---|
| `p1_meter` | `p1` | Deeply nested payload built from **positional arrays** — `data[3].obis.class` names a different register the moment the meter emits a different number of lines, which is why `MetricSource` exists. Its gas register also disappears halfway through the naive run, so a consumer meets a field that is present in earlier rows and absent from later ones for the same device. |
| `shelly_plug` | `shelly_plug` | Flat payload with **string-typed numbers** (`"power": "118.4"`). It reports the relay's own state instead of its power at two timesteps, so `power` is *held* rather than missing for those — and `status` appears mid-file as a boolean. The only `Switch`, so it is the decision target. |
| `pseudo_sensor` | `pseudo` | **Double-encoded payload**: `payload` stays a JSON *string* and `parsed` is the decoded object, so charting `payload` finds nothing and charting `parsed.value` finds the reading. One timestep sends a non-JSON payload, which is accepted and stored with `parsed: null`. |

The algorithm is **`AutoToggle`** — public, capability-based, seventeen lines. Its
commands are the bare strings `on` and `off`, **not JSON**, which is the single most
useful thing in the fixture for a consumer: `command` is an opaque string, and code that
assumes `JSON.parse` breaks on the project's own worked example. The meter's tariff-1
register crosses AutoToggle's 500 kWh threshold at naive step 7, so the decision stream
carries a visible `off` → `on` transition rather than eighteen identical rows.

### `control.log` is not CSV

`PseudoConnector`'s optional `control_log` is a raw `f.write` — no header, no quoting.
Every JSON command contains commas, so a CSV parser mis-splits it; split on the first two
commas only, or better, do not read it at all. Its line terminator is **`\n` on every
platform**: the connector passes `newline="\n"`, so text mode cannot translate it to
`os.linesep` and `--check` does not decide drift by which machine ran it. LF here, CRLF in
the two CSVs — this file is not CSV, and there the `csv` module writes the terminator.

It is a debug log, not an interchange format; the authoritative decision record is
`algorithm_decisions.csv`, and the two duplicate each other whenever a `csv_file` backend
is configured. It is included here so a consumer can recognise it and decline to parse it.

---

## `edge_cases/` — the adversarial fixture

Authored, not observed. The EMS cannot produce most of these rows, but a `docker kill`
mid-write, a truncating disk, a hand-edit, an Excel round-trip, or two files concatenated
from different EMS versions all can — and a viewer meets them on a real site.

They are kept out of `auto_toggle/expected/` deliberately: a "golden" file containing a
deliberately broken row stops being a statement about what the EMS emits.
`edge_cases/README.md` documents every row, and `tests/test_storage_contract.py` asserts
the counts match, so an undocumented row fails.

Rewrite them with `python examples/generate.py --edge-cases`. They are not part of
`--check`, because changing them is a deliberate act rather than drift.

---

## `example_replay.csv`

A four-row replay, small enough to read in one screen — the shape of the format and
nothing else. It predates the golden fixture and is kept as the minimal illustration.
For anything that needs to be *representative*, use `auto_toggle/` instead.

---

## `MANIFEST.json`

SHA-256 of every generated file plus `storage_format_version`. The Motrix Edge View repository
vendors a copy of these fixtures and verifies it against this same manifest, so a
version bump is visible on both sides. See [`docs/storage-format.md`](../docs/storage-format.md).
