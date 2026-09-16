# Motrix Edge

## Context
The goal of this project is to build a flexible and extensible EMS to optimize energy consumptions and other.

The goal of the architecture is, in one hand, to allow the writing of algorithms without caring about the
underlying IT architecture. In the other hand, to allow the writing of the underlying architecture without
caring about the algorithms.

The goal is to allow the writing of algorithms and the underlying architecture in parallel, allowing
the same algorithm to be used in different environments without changing the code.

## Implementation
To reach the goal, the architecture is based on the following components:
- **Algorithm**:
  - The actual core of the EMS.
- **Connectors**:
  - Allowing to connect the EMS to the devices.
- **Devices**:
  - The physical devices that are connected to the EMS.

## Tech Stack
- Python 3.12+, paho-mqtt 2.1.0, colorlog 6.9.0, jsonschema 4.23.0, influxdb-client 1.50.0
- Optional extras, one file each, none of them ever in `requirements.txt`: `requirements-api.txt` (fastapi + uvicorn — the `rest_api` service), `requirements-modbus.txt` (pymodbus — the `modbus_tcp` connector), `requirements-homeassistant.txt` (websocket-client — the `home_assistant` connector), `requirements-lora.txt` (pyserial — the `lora` serial connector; the `lorawan` one needs nothing, it is MQTT). `requirements-dev.txt` pulls in all four
- Entry point: `main.py` — run with `python main.py`
- Config: `config.json` validated against `config.schema.json` (JSON Schema Draft 2020-12)

## Project Structure
- `api/` — Abstract base classes (Connector, Device, Algorithm, Service, Parser, DevicesAccess) + capability interfaces (EnergyMeter, Switch, MetricSource) + the Stoppable cooperative-stop mixin + `options.py` (never-raising option coercion for plugin constructors)
- `config/` — Config loader, schema validation, enums
- `connectors/` — Protocol implementations (MQTT, HTTP polling, Modbus TCP, Home Assistant WebSocket, OpenEMS Edge REST subclassing the HTTP one, LoRaWAN-via-network-server subclassing the MQTT one, point-to-point LoRa over a serial radio, pseudo file replay)
- `devices/` — Device implementations (P1 meter, Shelly Plug S, generic Modbus register-map meter, Home Assistant entity, OpenEMS channel view, generic LoRa node with a config-driven payload map, pseudo — each of the middle four paired with a `*_switch` subclass, see the capability note below)
- `algorithms/` — Energy management logic (AutoToggle, DeviceChecker)
- `parsers/` — Data parsers (OBIS for P1 meters with CRC16-ARC)
- `devices_manager/` — Thread-safe singleton device registry
- `simulation/` — The replay clock: event time vs committed step time, plus the lockstep barrier
- `storage/` — Storage backends (CSV file, null, InfluxDB 2.x with batching + Flux reads)
- `storage_manager/` — Storage fan-out with per-backend error isolation
- `supervisor/` — Thread supervision: restart policy + worker lifecycle
- `services/` — The fifth plugin axis: supervised workers that own no devices (read-only REST API)
- `__metaclasses/` — Singleton patterns

## Key Patterns
- Plugin architecture: classes loaded dynamically from config via module name convention (documented in `CONTRIBUTING.md`; per-connector option schemas in `connectors/*.schema.json`)
- Supervised concurrency: each connector/algorithm runs in its own supervised daemon thread — a crash is logged with its traceback and restarted with bounded backoff; a clean return is not restarted (`supervisor/supervisor.py`, tuned via the config `runtime` block)
- Graceful shutdown: SIGTERM/SIGINT → `stop()` on every worker (`api/stoppable.py`), bounded join, then `StorageManager.close_all()`; workers poll `is_stopping()` and sleep via `wait_stop()`
- Device readiness: `threading.Event` for data_ready and connected states
- Bidirectional control: `device.control(cmd)` → `connector.send(device, cmd)`
- Config-driven wiring: connectors receive devices via `inject_devices()`
- Capability interfaces: algorithms `isinstance`-check capabilities from `api/capabilities.py` (EnergyMeter, Switch), never concrete device classes — devices implement them (e.g. `P1(Device, EnergyMeter)`). `MetricSource` is the device→storage variant: a device names its own readings when its payload structure carries meaning (P1 keys by OBIS code, not list position)
- **`Switch` only on a class that is genuinely writable.** It is a type claim algorithms act on directly — `algorithms/auto_toggle.py` selects actuators with `isinstance(device, Switch) and device.data`, with no `is_writable` check — and `Algorithm.control_device` writes the decision to storage *unconditionally*: it calls `devices_manager.control()` first (`api/algorithm.py:136`) and writes at `:138` regardless, because `Device.control` **logs and returns** rather than raising when the device is not writable (`api/device.py:103-106`). A read-only device subclassing `Switch` therefore puts a false row in `algorithm_decisions.csv` every tick, corrupting the versioned storage contract. Deriving `is_writable` per instance does not help, because `isinstance` is class-level. Hence the `*_switch` subclasses: `modbus_switch`, `ha_switch`, `openems_switch`
- Optional dependencies at module top: a plugin needing a library outside `requirements.txt` imports it at module top so `main.create_classes` catches the `ModuleNotFoundError` and logs one per-entry skip — the same import inside a supervised `start()` is a crash loop, and once the restart budget is spent the worker counts as finished, which shuts the whole run down. Tests for such a plugin guard with `pytest.importorskip("<dep>")` **before any project import**, or a bare `ModuleNotFoundError` aborts collection for the entire suite
- Deterministic replay: the pseudo connector advances an **event clock** before dispatching each entry (what storage stamps readings with) and commits a **step clock** after the whole timestep (what triggers algorithms), then blocks on `simulation/clock.py`'s barrier until every algorithm has finished that step — so `speed=0` means "as fast as the algorithms allow"
- Liveness is connector-shaped: `main` waits on the connectors only, so a service (a supervised worker owning no devices) never keeps a finished replay alive — that is why the read-only API is a fifth axis and not a `Connector` with stub methods. A config with services and no connectors exits immediately, with a warning
- Read-only introspection: `services/rest_api.py` serves `/health`, `/devices`, `/devices/{name}`, `/workers`, `/decisions` in-process, for the live state that never reaches storage (readiness, capabilities, restart counts, barrier progress). Every payload is an explicit field allowlist — never `vars(obj)`, because a device snapshot shares the live connector and would reach its password
- Decision history is **core state, not a storage backend**: `api/decisions.py`'s `DecisionLog` singleton is written from `StorageManager.write_algorithm_decision` — the one funnel every backend already sees, so `/decisions` and `algorithm_decisions.csv` hold the same decisions in the same order by construction. The append sits *outside* `_fan_out`, so decisions are recorded even with zero storage backends configured; a ring-buffer *backend* would instead have made the endpoint's existence depend on an unrelated `storage` entry. The cursor is a monotonic per-process `seq`, never a timestamp — under `speed=0` every decision in one timestep carries the identical committed step time

## Key Documents
- `CONTRIBUTING.md` — plugin conventions and all five extension recipes (connector, device, storage, service, algorithm)
- `docs/storage-format.md` — **normative** contract for the CSV output, versioned by `STORAGE_FORMAT_VERSION`; the interface the external Motrix Edge View app consumes. `examples/` holds the byte-exact golden fixture, and `tests/test_storage_contract.py` fails when it moves
- **The viewer's nginx is the only authentication in this system.** The `viewer` compose profile gates `/api/*` with Basic auth and proxies to `edge:8000` across `motrix-net`, which is why the API port is not published, why `services/rest_api.py` ships no CORS middleware and no auth of its own, and why uncommenting the `ports:` block on `edge` puts an ungated copy of every route beside the gated one. The service's own docstring names the right trigger for changing that: *publish the port and authentication becomes the first thing to add here* — a condition that has not been met
