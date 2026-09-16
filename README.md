<img src="docs/assets/motrix-mark.svg" alt="" width="132">

# Motrix Edge

**EDGE** · Energy management at the site.

[![Licence](https://img.shields.io/badge/licence-Apache--2.0-159E88?style=flat-square&labelColor=0B1E2D)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-159E88?style=flat-square&labelColor=0B1E2D)](requirements.txt)
[![Storage format](https://img.shields.io/badge/storage%20format-1.0-2FE6C8?style=flat-square&labelColor=0B1E2D)](docs/storage-format.md)

An extensible energy management system, built so that the **algorithm** and the
**infrastructure underneath it** can be written in parallel, by different people, without
either one reading the other's code.

An algorithm asks an energy meter for its total and tells a switch to close. Whether that
meter is a P1 port behind an MQTT broker, an HTTP-polled inverter, or last winter replayed
from a CSV is a question the algorithm never has to answer — and never gets to ask. The
sentence holds turned around: a new connector ships without touching a single algorithm.

```mermaid
flowchart LR
  DEV["Meters · switches · inverters"]
  EDGE["Motrix Edge<br/>connectors · algorithms · supervision"]
  VIEW["Motrix Edge View<br/>timeline · charts"]
  DEV -->|"MQTT · Modbus · HTTP · LoRa"| EDGE
  EDGE -->|"device_data.csv<br/>algorithm_decisions.csv"| VIEW
  EDGE -.->|"REST · /health /devices /decisions"| VIEW
  style DEV  fill:#12293A,stroke:#8FA3B0,stroke-width:1px,color:#AFC2CD
  style EDGE fill:#0B1E2D,stroke:#2FE6C8,stroke-width:2px,color:#E6EEF2
  style VIEW fill:#0B1E2D,stroke:#FFB443,stroke-width:2px,color:#E6EEF2
```

---

## Quickstart

```bash
pip install -r requirements.txt
python main.py --config examples/auto_toggle/config.json
```

About a second. No broker, no hardware, no network. It replays three simulated devices
through the `AutoToggle` algorithm and writes two files:

| File | |
|---|---|
| `data/storage/device_data.csv` | 36 readings — 12 timesteps × 3 devices |
| `data/storage/algorithm_decisions.csv` | 12 decisions — six `off`, then six `on`, as the meter crosses the threshold |

Load both into [Motrix Edge View](https://github.com/Motrix-Energy/motrix-edge-view) to see them on a timeline. What each device in
that run is there to prove — nested payloads, string-typed numbers, a register that
disappears halfway through — is documented in [`examples/README.md`](examples/README.md).
(The committed fixture beside it is larger, 54 readings and 18 decisions: it is *two* runs
appended, one with naive timestamps and one offset-aware, which is how it reproduces a
single `timestamp` column holding two time domains.)

Python 3.12+.

---

## The five plugin axes

| Axis | What it is | Ships with |
|---|---|---|
| `connectors/` | Speaks a protocol, owns the devices declared against it | `mqtt`, `http_api`, `modbus_tcp`, `home_assistant` (WebSocket), `openems` (Edge REST), `lorawan` (network server over MQTT, subclassing the MQTT one), `lora` (serial radio), `pseudo` (file replay) |
| `devices/` | Parses one device's payloads, exposes capabilities | `p1` (OBIS + CRC16-ARC), `shelly_plug`, `modbus_meter` / `modbus_switch` (register map from config), `ha_entity` / `ha_switch`, `openems` / `openems_switch`, `lora` / `lora_switch` (payload map from config, either LoRa transport), `pseudo` |
| `algorithms/` | The actual EMS logic | `auto_toggle`, `device_checker` |
| `storage/` | Optional persistence, zero or more at once | `csv_file`, `influxdb`, `null` |
| `services/` | Supervised worker that owns no devices | `rest_api` (read-only introspection) |

Everything is declared in `config.json` and loaded by name: `main.py` imports
`<package>.<name>` and instantiates the class inside it whose name matches the entry
(`shelly_plug` → `ShellyPlug`, `csv_file` → `CsvFileBackend`). There is no registry to
edit and no import to add — a new plugin is a new module plus a config entry, which is
the whole point. All five recipes are in [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## The configuration is the wiring

```json
{
  "connectors": [
    { "name": "broker", "protocol": "mqtt",
      "options": { "host": "${MQTT_HOST}", "port": 1883, "version": "3.1.1" } }
  ],
  "devices": [
    { "name": "shelly_plug", "kind": "shelly_plug",
      "options": {
        "connector_options": { "name": "broker" },
        "listener_options": { "pattern": "shellies/.*", "subscription": "shellies/plug-s/#" },
        "controller_options": { "topic": "shellies/plug-s/relay/0/command" }
      } }
  ],
  "algorithms": [
    { "name": "AutoToggle", "class": "auto_toggle",
      "options": { "delay_seconds": 900, "required_devices": ["shelly_plug"] } }
  ],
  "storage": [
    { "name": "csv", "class": "csv_file", "options": { "output_dir": "data/storage" } }
  ]
}
```

A device names its connector; nothing else connects them. `main.py` groups devices by that
name and calls `inject_devices()` — so a connector never looks a device up, and a device
never constructs a transport.

- **Validation is two-layer.** `config.schema.json` covers the document's shape; the
  per-plugin options are validated against the schema shipped *next to the plugin*
  (`connectors/mqtt.schema.json`, `devices/p1.schema.json`, …). Adding a connector never
  touches the root schema.
- **`${VAR}` and `${VAR:-default}` resolve from the environment** after validation, so
  secrets stay out of the file. `python main.py` reads `os.environ` directly — it does not
  load `.env`; see [`.env.example`](.env.example) for how to export it.
- **Order is config order.** Plugins are instantiated into a list, never a set, so two
  decisions landing in the same timestep land in the same order on every run.

---

## Writing an algorithm

This is `algorithms/auto_toggle.py`, in full:

```python
class AutoToggle(Algorithm):
	@override
	def main(self) -> None:
		super().main()
		total: float = 0
		for device in self.devices.values():
			if isinstance(device, EnergyMeter):
				energy = device.get_total_energy_kwh()
				if energy is None:
					self.LOGGER.warning(f"Skipping {device.name}: no usable energy data")
					continue
				total += energy
		self.LOGGER.info(f"Total: {total}")
		for device in self.devices.values():
			if isinstance(device, Switch) and device.data:
				self.control_device(device, device.COMMAND_ON if total > 500 else device.COMMAND_OFF)
```

The load-bearing line is `isinstance(device, EnergyMeter)`. Algorithms check
**capabilities** from `api/capabilities.py` — never a concrete device class. `P1` *is* an
`EnergyMeter`; so is any meter written next year, and this algorithm picks it up without a
diff. The base class handles the rest: `loop()` runs `main()` every `delay_seconds`, waits
for `required_devices` to become ready first, and polls a stop event so `Ctrl+C` lands
between steps rather than inside one.

Three capabilities exist today — `EnergyMeter`, `Switch`, and `MetricSource` (the
device→storage variant: a device names its own readings when payload *position* carries no
meaning, as with a P1 meter keyed by OBIS code).

---

## Running against a replay

The `pseudo` connector replays a timestamped CSV or JSON file. Algorithms cannot tell —
that is the test.

```json
{ "name": "replay", "protocol": "pseudo",
  "options": { "replay_file": "examples/auto_toggle/replay.naive.csv", "speed": 0 } }
```

`speed: 0` does **not** mean "as fast as the file reads". The connector advances an *event
clock* before dispatching each entry (what storage stamps readings with), commits a *step
clock* after the whole timestep (what triggers algorithms), and then blocks on
`simulation/clock.py`'s barrier until every algorithm has finished that step. So `speed: 0`
means *as fast as the algorithms allow*, and a slow `main()` cannot cause the replay to run
ahead and skip timesteps — that, and readings being stamped with the timestep they were
dispatched in rather than the previous one, is what `tests/test_replay_determinism.py` pins
down. Byte-reproducibility of a whole run is checked separately, by
`python examples/generate.py --check`. A step that overruns `step_timeout_seconds` is logged
and the replay advances anyway, warning that determinism is gone.

Both file formats and every speed mode are documented option by option in
[`connectors/pseudo.schema.json`](connectors/pseudo.schema.json).

---

## Storage, and the contract with the viewer

Storage is optional and plural — declare zero backends, or `csv_file` and `influxdb` at
once. `StorageManager` fans out with per-backend error isolation, so a dead InfluxDB does
not take the CSV down with it.

[`docs/storage-format.md`](docs/storage-format.md) is **normative** for the CSV output:
dialect, lazy header, the naive-vs-offset-aware timestamp ambiguity, gap semantics,
flattening. It is versioned by `STORAGE_FORMAT_VERSION` (currently `1.0`), and
`examples/auto_toggle/expected/` holds the byte-exact output of a real run.
`tests/test_storage_contract.py` fails when those bytes move, and the failure message names
the repository that has to be told. That fixture — not this prose — is the interface with
Motrix Edge View.

Two properties are worth knowing before trusting a chart: **a gap means "no reading
received"** (the EMS writes no row for a rejected payload, so a stalled meter reads as
missing rather than flat), and **`command` is an opaque string** (`AutoToggle` emits the
bare words `on` and `off`, not JSON).

---

## Live introspection

A config that declares the `rest_api` service gets five read-only endpoints in-process:
`/health`, `/devices`, `/devices/{name}`, `/workers`, `/decisions` — readiness, capabilities,
restart counts, replay-clock progress, and the decision log. It exists for the state that never
reaches storage: a device that has never produced a reading writes zero rows, so no dashboard
can tell *silent* from *not configured*.

`/decisions` is the one route that serves history rather than a snapshot. Decisions are discrete
events, so a client polling snapshots sees only the ones that happened to be current when it
looked; the route is cursored by `seq` — never by timestamp, since step times are not monotonic
and duplicate freely under `speed=0` — and states its own gaps rather than leaving them inferred.

Every payload is an explicit field allowlist, never `vars(obj)` — a device snapshot shares
the live connector, and that object holds a broker password.

> **The viewer's nginx is the only authentication in this system.** It gates `/api/*` with
> Basic auth and proxies to `edge:8000` across the compose network, which is why the Edge API
> port is not published, why the service ships no CORS middleware and no auth of its own,
> and why uncommenting the `ports:` block on `edge` puts an ungated copy of every route
> beside the gated one. Publish that port and authentication becomes the first thing to add
> here.

`fastapi`/`uvicorn` live in `requirements-api.txt`, deliberately not in `requirements.txt`:
a CSV-only EMS should not install a web framework. Without them the service is one clean
per-entry skip at startup and the EMS runs on.

---

## Docker

```bash
docker compose up -d                              # EMS alone
docker compose --profile mqtt up -d               # + mosquitto
docker compose --profile monitoring up -d         # + influxdb + grafana
docker compose --profile viewer up -d             # + the gated viewer on 127.0.0.1:8080
```

`config.json` is mounted read-only, `data/` read-write, secrets arrive as environment
variables ([`.env.example`](.env.example) documents which are read *inside* the container
and which are substituted by the docker CLI *before* one exists — they are not
interchangeable). The image carries its own `HEALTHCHECK`; compose adds none, because two
definitions of health is drift waiting to happen.

---

## Development

```bash
pip install -r requirements-dev.txt
pytest
pytest -m "not slow"    # skips the ones that spawn a second interpreter
ruff check .
```

`requirements-dev.txt` pulls in every optional extra — `requirements-api.txt` (fastapi +
uvicorn, the `rest_api` service), `requirements-modbus.txt` (pymodbus, the `modbus_tcp`
connector) and `requirements-homeassistant.txt` (websocket-client, the `home_assistant`
connector). None of them belongs in `requirements.txt`: an MQTT-and-CSV site should install
neither a web framework nor a protocol stack for hardware it does not have. Each plugin
imports its dependency at module top, so a missing extra is one clean per-entry skip at
startup and the EMS runs on without it.

Optional dependencies are guarded with `importorskip`, so a bare checkout stays green and
`pytest -rs` says which tests were skipped and why. The formatter is deliberately not part
of the workflow: this codebase indents with tabs, and a reformat would bury every real
change in whitespace.

---

## Layout

| | |
|---|---|
| `api/` | The abstractions: `Connector`, `Device`, `Algorithm`, `Service`, `Parser`, `StorageBackend`, plus `capabilities.py`, the `Stoppable` mixin, and never-raising option coercion |
| `config/` | Loader, two-layer schema validation, `${VAR}` resolution, enums |
| `connectors/` `devices/` `algorithms/` `storage/` `services/` | The five plugin axes, one module per plugin |
| `parsers/` | `obis.py` — P1 telegram parsing with CRC16-ARC |
| `devices_manager/` | Thread-safe singleton device registry |
| `storage_manager/` | Fan-out with per-backend error isolation |
| `supervisor/` | Restart policy and worker lifecycle |
| `simulation/` | Event clock, step clock, lockstep barrier |
| `examples/` | The golden fixture and the adversarial one |

Each connector, algorithm and service runs in its own supervised daemon thread. A crash is
logged with its traceback and restarted with bounded backoff; a *clean return* is not
restarted. `SIGTERM`/`SIGINT` calls `stop()` on every worker, joins with a bound, then
closes storage. Liveness is connector-shaped — `main` waits on the connectors only, so a
service never keeps a finished replay alive.

---

## Documentation

| | |
|---|---|
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Plugin conventions and all five extension recipes |
| [`docs/storage-format.md`](docs/storage-format.md) | Normative output contract |
| [`examples/README.md`](examples/README.md) | What the fixtures prove, and how to regenerate them |
| [`SECURITY.md`](SECURITY.md) | The one-gate model, what is in scope, and how to report privately |
| [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) | Contributor Covenant 2.1 |
| [`CLAUDE.md`](CLAUDE.md) | Orientation for AI assistants working in this repository |

---

## The Motrix family

| | |
|---|---|
| **Motrix Edge** | This repository. The runtime — connectors, algorithms, supervision, storage. One site, local-first, no internet required. |
| **Motrix Edge View** | [`motrix-edge-view`](https://github.com/Motrix-Energy/motrix-edge-view). The viewer — Edge's two CSVs and its live REST state, on one synchronised timeline. |

Both ends of that arrow are pinned to the same **storage format 1.0**:
`STORAGE_FORMAT_VERSION` here, `motrixStorageFormat` in the viewer's `package.json`, and one
golden fixture vendored on both sides. A format bump fails a test rather than a chart.

---

## License

Apache License 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

Dependencies are not vendored. Everything in `requirements.txt` and the four optional
extras is resolved from PyPI at install time, so no third-party licence text is reproduced
in this repository.
