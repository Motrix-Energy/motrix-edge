# Contributing

Motrix Edge is a plugin architecture: algorithms, connectors, devices, services, and storage
backends are independent classes loaded dynamically from `config.json` — no registration code, no
central imports.
The full test suite runs in seconds with **no broker and no hardware**; a green `pytest` is the bar for
every contribution.

Contributing to the *viewer* instead? Motrix Edge View has its own recipes and its own load-bearing
rules in
[`CONTRIBUTING.md`](https://github.com/Motrix-Energy/motrix-edge-view/blob/main/CONTRIBUTING.md).
Both repositories share a [code of conduct](CODE_OF_CONDUCT.md) and the standing rule that nothing
from a real installation is ever committed; security issues go through [`SECURITY.md`](SECURITY.md),
never a pull request.

## How plugins load

`main.create_classes()` (see `main.py`) wires every axis the same way: a config entry names a module,
the module is imported from the axis's package, and a class inside it is matched by naming convention.

| Axis | Config array | Config key | Package | Class-name suffix | Example |
|---|---|---|---|---|---|
| Connector | `connectors` | `protocol` | `connectors/` | `Connector` | `mqtt` → `connectors/mqtt.py` → `MQTTConnector` |
| Device | `devices` | `kind` | `devices/` | *(none)* | `shelly_plug` → `devices/shelly_plug.py` → `ShellyPlug` |
| Algorithm | `algorithms` | `class` | `algorithms/` | *(none)* | `auto_toggle` → `algorithms/auto_toggle.py` → `AutoToggle` |
| Storage | `storage` | `class` | `storage/` | `Backend` | `csv_file` → `storage/csv_file.py` → `CsvFileBackend` |
| Service | `services` | `class` | `services/` | `Service` | `rest_api` → `services/rest_api.py` → `RestApiService` |

**The naming rule**: take the module name, append the suffix, strip underscores, and match against the
module's class names **case-insensitively**. So `connectors/foo_bar.py` must contain a class whose
lowercased name is `foobarconnector` — i.e. `FooBarConnector` (but `FOOBarConnector` would work too).
The class must subclass the axis's ABC (`api/connector.py`, `api/device.py`, `api/algorithm.py`,
`api/storage_backend.py`, `api/service.py`) or it is rejected with a logged error.

**The kwargs contract** (load-bearing): every key in a config entry's `options` object is passed as a
keyword argument to the constructor:

```python
actual_class(config_entry["name"], **config_entry.get("options", {}))
```

Your constructor signature *is* your options schema. Give optional options default values; an unknown
key raises `TypeError` and the plugin is skipped with a *"could not be instantiated"* log line.

## Recipe: add a new connector

Everything lives in `connectors/` plus one test file — no central file needs editing.

1. **Create `connectors/<protocol>.py`** with `class <Protocol>Connector(Connector)`:

	```python
	from typing import override

	from api.connector import Connector
	from api.device import Device


	class FooBarConnector(Connector):
		@override
		def __init__(self, name: str, host: str, port: int = 1234) -> None:
			super().__init__(name)
			self.host = host
			self.port = port
	```

2. **Import optional dependencies at module top, not inside `start()`.** If your transport
	needs a library that is not in `requirements.txt` — `pymodbus`, `websocket-client` — ship
	it in its own `requirements-<name>.txt` (copy `requirements-modbus.txt`) and import it at
	module top. `main.create_classes` catches `ModuleNotFoundError` and skips the plugin with
	one honest error line while the rest of the EMS starts normally; the same import inside a
	supervised `start()` is a *crash* — five restarts with backoff, then CRITICAL — and once
	the restart budget is spent the worker counts as finished, which makes `main` shut the
	whole run down over a dependency that was optional by design. Runtime *resources* (a
	socket, a client object) still belong in `start()`. Add a `-r` line to
	`requirements-dev.txt` so the suite exercises your connector on a dev checkout.

3. **Implement `start()`** — it blocks; `main` runs it in its own supervised daemon thread.
	- Call `self.on_connected()` once the transport is up (marks write-only devices connected).
	- For each inbound message: find the target device (you received the mapping via
	  `inject_devices()`; per-device routing hints live in `device.listener_options`) and call
	  **`self.deliver(device, payload)`** — or `self.deliver(device, topic, payload)` if your
	  transport has a routing key. That is the only place a connector calls `device.receive()`.
	  It forwards what `receive()` returned to `on_device_data_received`, which marks the device
	  data-ready/connected, publishes it to `DevicesManager` (so algorithms see it) and fans the
	  data out to storage. A device returns `False` when the payload gave it nothing usable, and
	  forwarding that is what keeps a corrupt frame out of storage instead of republishing the
	  device's previous reading under a new timestamp.
	- **`deliver()` is also the guard**, and that is why it exists rather than the two calls you
	  would otherwise write by hand. A device is a plugin: it is contracted never to raise, but
	  one that does used to end the run — the exception escaped your `start()`, the supervisor
	  spent its restart budget, and `main` read the finished worker as "all connectors finished"
	  and shut the EMS down over one device's bug. `deliver()` logs that with its traceback,
	  rate-limits the repeat, drops the reading and returns `False`, so branch on it if you track
	  per-device failure state. Everything else in `connectors/` stays narrow: catch only what
	  *your* code can raise, because a bug of yours should reach the supervisor.

4. **Make `start()` stoppable.** The framework never kills a thread; it asks it to wind down (see
	*Contracts* below). Loop on `while not self.is_stopping():` and sleep through
	`self.wait_stop(seconds)` instead of `time.sleep(seconds)`. If your loop blocks somewhere an
	event can't reach — a foreign transport event loop, say — override `stop()` to unblock it:

	```python
	@override
	def stop(self) -> None:
		super().stop()                  # always first: sets the stop event
		self.transport.disconnect()     # whatever makes start() return
	```

	Returning from `start()` is a *normal completion*, not a failure — the supervisor logs it and
	leaves it alone. Raising is a crash and gets restarted (see *Contracts*).

5. **Implement `send(device, payload)`** — the control path, invoked when an algorithm switches a
	device. Read routing info (topic, endpoint, …) from `device.controller_options`.

	**Catch everything you can inside it.** `send()` runs on the *algorithm's* thread —
	`Algorithm.control_device` → `DevicesManager.control` → `Device.control` → here — and
	nothing in that chain catches. A raise crashes the algorithm's supervised worker because
	a relay did not answer, or because an operator typed a value the hardware cannot hold, so
	the coercion belongs inside the `try` too, not just the transport call.

6. **Optionally override `inject_devices(devices)`** to precompute per-device state from
	`listener_options` (see `HttpApiConnector` for an example). Always call `super().inject_devices(devices)`
	first — it stores the mapping and sets the `device.connector` back-reference used for control.

	If your protocol is another connector's protocol in different dress, **subclass it**
	rather than reimplementing it. This is the pattern here, not an exception, and there are
	two worked examples:

	- `OpenemsConnector(HttpApiConnector)` — an OpenEMS Edge is an HTTP endpoint polled on an
	  interval, so it overrides only `resolve_endpoint()` (the seam turning a device's options
	  into a path) plus `send()`, and inherits the poll loop, the schedule and the
	  failure/recovery tracking untouched.
	- `LoRaWANConnector(MQTTConnector)` — a network server *is* an MQTT broker with an opinion
	  about topics and payload wrapping, so it overrides `resolve_listener()` (a device's
	  devEUI into a subscription filter and a routing regex) and `resolve_downlink()` (a
	  Switch token into base64 inside a vendor JSON envelope), and inherits the connect
	  ladder, paho's reconnect, the resubscribe-on-reconnect and the bounded per-device dispatch.

	If the seam you need does not exist yet, **extract it with a default body that reproduces
	the parent's current behaviour**, and add a `TestParentSeam` class asserting the parent is
	unchanged — that is how both of the above were added without touching a live deployment.

	Note that `tests/test_config.py`'s schema-lockstep check inspects **your** `__init__`, not
	the parent's: an inherited option you want configurable has to be re-declared in your
	signature and forwarded to `super()`, or your schema cannot legally declare it.

7. **Ship `connectors/<protocol>.schema.json`** — a standalone JSON Schema (draft 2020-12) for your
	options object, keys matching your constructor kwargs exactly. `Config` validates every config
	entry with your protocol against it at load time (warnings, never fatal) — and
	`tests/test_config.py` asserts the schema stays in lockstep with your constructor.
	`additionalProperties: false` is recommended: an option key you don't accept would fail at
	instantiation anyway, so let the config warning say it first. Copy `connectors/pseudo.schema.json`
	as a starting point. A connector with no schema file simply gets no options validation.

8. **Config entry**:

	```json
	{
		"connectors": [
			{
				"name": "my_foo_bar",
				"protocol": "foo_bar",
				"options": {
					"host": "example.org",
					"port": 4321
				}
			}
		]
	}
	```

9. **Tests**: copy `tests/test_mqtt_connector.py` (mocks the transport client — the right template for
	broker/network protocols), `tests/test_modbus_tcp_connector.py` (mocks one client class),
	`tests/test_home_assistant_connector.py` (a scripted fake socket),
	`tests/test_lora_connector.py` (a scripted fake serial port, plus the `TestStart` shape for
	a connector that reconnects), `tests/test_lorawan_connector.py` (subclassing an existing
	connector, with the `TestParentSeam` class that keeps the parent honest) or
	`tests/test_pseudo_connector.py` (file-driven). `tests/test_shutdown.py`
	is the template for proving `stop()` unblocks `start()`. `pytest` must stay green with no hardware
	and no network.

	If your connector needs an optional dependency, guard the test module with
	`pytest.importorskip("<dep>")` **before any project import that pulls it in** — a bare
	`ModuleNotFoundError` at module top aborts collection for the *entire* suite, not just your
	file. Do **not** add your connector to `tests/test_shutdown.py`: it imports every connector
	at module top, so an import there would abort collection whenever the extra is absent.

	**Working outside this repository?** The test files above open with
	`from tests.conftest import …`, which resolves only in this checkout. The axis-generic
	doubles and the threading harness they use — `StubDevice`, `StubConnector`,
	`StubStorageBackend`, `StubAlgorithm`, `make_devices_access`, `run_in_thread`,
	`wait_until`, `assert_stops`, `write_replay` — all live in `api/testing.py` and import
	from anywhere; `conftest.py` only re-exports them. The checks this repository runs over
	its own plugins are in `api/conformance.py`, and `check_loads` is the one worth running
	first: it drives the real loader, so it catches the class-name and `issubclass` mistakes
	that no schema check can see. Neither module is a supported API yet.

	The four concrete-device factories (`make_p1`, `make_shelly`, `make_pseudo`,
	`build_p1_telegram`) stay in `tests/conftest.py` — they build shipped devices, and `api/`
	must not import an axis. `tests/test_shutdown.py` uses `make_pseudo`, so copy that one
	helper across rather than importing it.

## Recipe: add a new device

Everything lives in `devices/` plus one test file — no central file needs editing.

1. **Create `devices/<kind>.py`** with `class <Kind>(Device[, Capability])`:

	```python
	from typing import Any, override

	from api.device import Device
	from api.capabilities import EnergyMeter  # optional — implement a capability


	class AcmeMeter(Device, EnergyMeter):
		@override
		def __init__(self, name: str, connector_options: dict[str, Any], listener_options: dict[str, Any], controller_options: dict[str, Any]) -> None:
			super().__init__(name, connector_options, listener_options, controller_options)
	```

2. **Implement `receive(self, *args, **kwargs)`** — connectors call it with the raw transport data
	(MQTT passes `(topic, payload)`, HTTP, Modbus and OpenEMS pass `(payload,)`, Home Assistant
	passes `(entity_id, payload)`). If you serve more than one
	transport, list them in `SUPPORTED_PROTOCOLS` rather than branching on the protocol — see the
	refusal paragraph below; no shipped device carries a `match` on it any more. Parse into
	`self.data`, and return. **You do not call `update_device`** — the connector's `on_device_data_received()` hook
	publishes the device to `DevicesManager` for you, so a well-formed `receive()` is all algorithms need.

	**Every argument is a `str`, and that is a contract, not an accident.** `PseudoConnector`
	replays a `topic` and a `payload` column out of a CSV, both strings, so a connector that
	handed its device a pre-parsed `dict` would force a second, replay-only parse path — and
	the backtest, which is what every regression fixture uses, would then never exercise the
	production parser. `connectors/modbus_tcp.py` serialises its register words to JSON for
	exactly this reason. Accept **both** arities even if your connector only ever sends one:
	`PseudoConnector` chooses between them per row — a replay row with a non-empty `topic`
	column is dispatched as `receive(topic, payload)` and one without as `receive(payload)` —
	so a device that accepts only the two-argument form turns a topic-less replay into one
	logged `TypeError` per row and no readings at all. `devices/p1.py`'s `receive_mqtt`
	handles both in two lines.

	`protocol` is injected by `Config` from the connector the device is wired to, and you do **not**
	need a branch for `pseudo`: a replay connector declares `emulates` (see `connectors/pseudo.schema.json`) and
	impersonates the transport it stands in for, so your device is backtestable through a replay file
	without knowing it. Payloads reach you byte for byte, including multi-line ones.

	**A protocol you cannot serve is refused, never raised — and you declare it rather than
	branch on it.** Set `SUPPORTED_PROTOCOLS` on your class, add `UNSERVABLE_PROTOCOLS` or
	`PROTOCOL_REFUSAL` if a generic sentence would waste an operator's afternoon, and make
	`if self.refuse_unserved_protocol(*args, **kwargs): return False` the first line of
	`receive()`. `Device.__init__` logs the ERROR for you, once, at startup on the main thread,
	where an operator reads logs and where it fires whether or not a payload ever arrives. That
	declaration is the single source of truth — there is no `match` on `protocol` left in any
	device — and `tests/test_device_protocol.py` sweeps every class to keep it honest. A device
	that dispatches on no protocol at all declares nothing and is never asked
	(`devices/pseudo.py`).

	Do not raise. `Connector.deliver()` will catch it, but being caught by the guard meant for
	a *device bug* is the wrong way for a plain config mistake to surface: it logs a traceback
	and rate-limits the repeat, where the refusal says the one sentence that names the fix. And
	do not raise in `__init__` at all. `main.create_classes` contains it — it logs the
	traceback and skips that one entry — but a skipped entry is a device that silently does
	not exist, which is worse than one that visibly never reports: an algorithm summing
	`EnergyMeter`s would compute a site total short one meter with nothing anywhere saying
	so. `devices/p1.py` is the worked example.

	**Never assign an unusable parse result to `self.data`, and return `False` when you didn't.**
	Malformed input is routine — a CRC error on a noisy line, a truncated frame, a topic you don't
	model — and overwriting the last good reading with an empty one turns a gap in the record into a
	reading of nothing. Returning `False` tells the framework the same thing about *publication*:
	without it the connector republishes your unchanged `self.data` under the new timestamp, so a
	stalled meter shows up in storage as a flat line rather than missing data. Returning `None` still
	means accepted, so a device written before this contract keeps working. See `devices/p1.py` for
	the failure path and `devices/shelly_plug.py` for the unmodelled-topic one. Losing a sample is
	acceptable; inventing one is not.

3. **Declare capabilities** so algorithms consume you by capability, not by `kind` (`api/capabilities.py`).
	Set `is_writable = True` and implement `Switch` if you accept control commands; implement
	`EnergyMeter` (or another capability ABC) if you expose readings. `control()` is inherited from
	`Device` — it routes to `connector.send()` and already refuses non-writable devices.

	**Put `Switch` only on a class that is genuinely writable, and split the class if
	writability is a config decision.** `Switch` is a *type* claim algorithms act on directly:
	`algorithms/auto_toggle.py` selects actuators with `isinstance(device, Switch) and
	device.data`, with **no** `is_writable` check. So a read-only device subclassing `Switch`
	is picked as an actuator and commanded **every tick**. `Device.control` refuses each
	command and answers `False`, and `Algorithm.control_device` records a decision only when
	it answers `True` — so the versioned storage contract (`docs/storage-format.md`) stays
	clean. What that gate cannot fix is the algorithm's own belief that it holds an actuator:
	it goes on asking a thermometer to switch on, forever, and no feedback path tells it
	otherwise. Deriving `is_writable` per instance does not help either, because `isinstance`
	is class-level. The
	`api/conformance.py`'s `check_switch_honesty` is the sweep that enforces this; run it over
	your own classes. The
	pattern to copy is a base read device plus a thin subclass: `ModbusMeter` /
	`ModbusSwitch`, `HaEntity` / `HaSwitch`, `Openems` / `OpenemsSwitch`, each subclass about
	fifteen lines. The plugin loader makes this free — no central registration.

	`EnergyMeter` is different and safe to carry unconditionally: the contract says return
	`0.0` when the data is well-formed but holds no kWh register, so a device with no energy
	source configured stays harmless in an algorithm that sums every `EnergyMeter` it sees.
	Reserve `None` for a *configured* source that cannot be read, which is the case a silent
	`0.0` would hide by deflating a site total.

	Implement **`MetricSource`** if your `self.data` needs naming before it can be stored — that is,
	if its *structure* carries meaning. A time-series database keys on field names, so a payload
	whose position is significant becomes unreadable once flattened generically: `P1`'s OBIS list
	would store `data.7.obis.class`, where index 7 is a different register the moment the meter emits
	a different number of lines. `get_metrics()` returns a flat `{name: scalar}` view with names that
	are stable across messages (see `devices/p1.py`, which keys by OBIS code). A device whose data is
	already flat and stably keyed — `ShellyPlug`, `Pseudo` — does not need it; the generic path stays
	correct for it.

4. **Ship `devices/<kind>.schema.json`** — a standalone JSON Schema (draft 2020-12) for your **options
	object**: the three keys `connector_options` / `listener_options` / `controller_options` spread into
	your constructor. `Config` validates every device of your `kind` against it at load time (warnings,
	never fatal), and `tests/test_config.py` keeps it in lockstep with the constructor. Copy
	`devices/pseudo.schema.json` as a starting point. A device with no schema file simply gets no
	options validation.

5. **Config entry**:

	```json
	{
		"devices": [
			{
				"name": "kitchen_meter",
				"kind": "acme_meter",
				"options": {
					"connector_options": { "name": "my_mqtt" },
					"listener_options": { "pattern": "acme/kitchen/.*", "subscription": "acme/kitchen/#" },
					"controller_options": {}
				}
			}
		]
	}
	```

	`connector_options.name` must reference a connector declared in the same config — `Config` wires the
	two and injects that connector's `protocol` into `connector_options` for `receive()` to dispatch on.

6. **Tests**: copy `tests/test_p1_device.py` (a meter with a parser) or `tests/test_shelly_plug.py`
	(a switch). `pytest` must stay green with no hardware and no network.

	**Working outside this repository?** The test files above open with
	`from tests.conftest import …`, which resolves only in this checkout. The axis-generic
	doubles and the threading harness they use — `StubDevice`, `StubConnector`,
	`StubStorageBackend`, `StubAlgorithm`, `make_devices_access`, `run_in_thread`,
	`wait_until`, `assert_stops`, `write_replay` — all live in `api/testing.py` and import
	from anywhere; `conftest.py` only re-exports them. The checks this repository runs over
	its own plugins are in `api/conformance.py`, and `check_loads` is the one worth running
	first: it drives the real loader, so it catches the class-name and `issubclass` mistakes
	that no schema check can see. Neither module is a supported API yet.

## Recipe: add a new storage backend

Everything lives in `storage/` plus one test file — no central file needs editing.

1. **Create `storage/<class>.py`** with `class <Name>Backend(StorageBackend)`:

	```python
	from datetime import datetime
	from typing import Any, override

	from api.device import Device
	from api.storage_backend import StorageBackend


	class TimescaleBackend(StorageBackend):
		@override
		def __init__(self, name: str, dsn: str, table: str = "ems") -> None:
			super().__init__(name)
			self.dsn = dsn
			self.table = table
	```

2. **Implement the three sinks/source** (`api/storage_backend.py`):
	- `write_device_data(device, data)` — called on every device `receive()`.
	- `write_algorithm_decision(algorithm, device, command)` — called when an algorithm controls a device.
	- `read(device, start, end)` — return stored rows within a time range (or `[]`).

	Backends run behind `StorageManager`, which fans every write out to all backends and **isolates
	failures per backend** — one backend raising never blocks the others (and `read()` returns the first
	non-empty result), so just do your job and let exceptions surface. There is no `DevicesManager` or
	readiness contract here — a storage backend is a sink/source, not a participant in the runtime.

	Two things it *may* consult, both optional. `write_device_data` receives the live `Device`, so a
	backend whose schema is its field names can prefer `MetricSource.get_metrics()` over the raw
	payload (`storage/influxdb.py` does; `CsvFileBackend`, which serialises the payload verbatim, has
	no reason to). And **neither write method takes a timestamp** — derive one from
	`self._data_timestamp()` for a device reading or `self._decision_timestamp()` for an algorithm
	decision. Those are two different clocks under a replay: a reading belongs to the timestep being
	dispatched, a decision to the timestep the algorithm was processing. Never reach for
	`datetime.now()` directly, or your backend will stamp backtests with the wall clock.

	**`CsvFileBackend` is special: its output is a published interface.** An external app
	(Motrix Edge View) parses those two files, so their headers, dialect, timestamp shape and gap
	semantics are frozen by `docs/storage-format.md` and pinned byte-for-byte by
	`tests/test_storage_contract.py` against the fixture in `examples/`. Changing that backend's
	output is a cross-repo breaking change — bump `STORAGE_FORMAT_VERSION` and read the doc first.

	**The two filenames are reserved, and that applies to your backend too.** `device_data.csv` and
	`algorithm_decisions.csv` under a backend's `output_dir` are format 1.0 by name *and* by column
	shape, and the viewer identifies them by shape — so a backend that writes either name with
	those columns produces a file the viewer will read as a Motrix Edge run, with none of the
	byte-exactness `tests/test_storage_contract.py` guarantees. Only a backend that passes that
	fixture comparison should emit them. Anything else: pick your own names, or your own directory.

	`StorageManager` warns at startup when two registered backends resolve to the same `output_dir`,
	because the failure it catches needs no third party: two `csv_file` entries on one directory
	interleave their rows into one file, and the header is decided from the size on disk at open
	time, so the second one appends to a file the first already started with nothing marking the
	seam. It is a warning and not a refusal — storage is a side channel and must never decide
	whether the run happens.
	Every *other* backend answers only to its own store and is free to shape output as it likes.

	**Override `close()` if you buffer.** `StorageManager.close_all()` calls it once at shutdown;
	anything still in memory when it returns is lost. The default is a no-op, which is correct for a
	write-through backend like `CsvFileBackend` (one `open`/`close` per row) but wrong for a batching
	client — flush there. `storage/influxdb.py` is the worked example: it swaps its handles out under
	the lock, then flushes outside it, and **bounds every wait it can** (`max_close_wait_ms`, plus a
	retry budget capped to match). A network client's defaults are usually chosen for throughput, not
	for shutdown — left alone, the InfluxDB client's kept the process alive for 213s past
	*"Shutdown complete"*, because a force-closed writer does not cancel an in-flight retry and those
	threads are not daemons. If your backend talks to a network service, time a real shutdown against
	an unreachable one before you call it done.

3. **Ship `storage/<class>.schema.json`** — a standalone JSON Schema (draft 2020-12) for your options
	object, keys matching your constructor kwargs exactly. `Config` validates every config entry with
	your `class` against it at load time (warnings, never fatal), and `tests/test_config.py` keeps it in
	lockstep with the constructor. Copy `storage/csv_file.schema.json` (or `storage/null.schema.json`
	for a no-options backend) as a starting point. A backend with no schema file simply gets no options
	validation.

4. **Config entry**:

	```json
	{
		"storage": [
			{
				"name": "prod_timescale",
				"class": "timescale",
				"options": {
					"dsn": "postgresql://ems@timescale:5432/ems",
					"table": "ems"
				}
			}
		]
	}
	```

5. **Tests**: copy `tests/test_storage_csv.py` (a file-backed backend), `tests/test_storage_null.py`
	(the no-op), or `tests/test_storage_influxdb.py` (a network client, mocked at its single
	construction site). `pytest` must stay green with no external service — mock it or write to a
	temp dir.

	**Working outside this repository?** The test files above open with
	`from tests.conftest import …`, which resolves only in this checkout. The axis-generic
	doubles and the threading harness they use — `StubDevice`, `StubConnector`,
	`StubStorageBackend`, `StubAlgorithm`, `make_devices_access`, `run_in_thread`,
	`wait_until`, `assert_stops`, `write_replay` — all live in `api/testing.py` and import
	from anywhere; `conftest.py` only re-exports them. The checks this repository runs over
	its own plugins are in `api/conformance.py`, and `check_loads` is the one worth running
	first: it drives the real loader, so it catches the class-name and `issubclass` mistakes
	that no schema check can see. Neither module is a supported API yet.

## Recipe: add a new service

Everything lives in `services/` plus one test file — no central file needs editing. A service is a
supervised worker that owns **no devices**: it observes the runtime or exposes it, and the framework
hands it the two handles it needs.

1. **Create `services/<class>.py`** with `class <Name>Service(Service)`:

	```python
	from typing import override

	from api.devices_access import DevicesAccess
	from api.service import Service
	from supervisor.supervisor import Supervisor


	class HeartbeatService(Service):
		@override
		def __init__(self, name: str, devices_manager: DevicesAccess, supervisor: Supervisor,
					 path: str = "/app/data/ems.heartbeat", interval_seconds: float = 5) -> None:
			super().__init__(name, devices_manager, supervisor)
			self.path = path
			self.interval_seconds = interval_seconds
	```

	`devices_manager` and `supervisor` are injected by `main` through
	`create_classes(arguments=...)`. **Every** service receives both, used or not, because
	`arguments` is spread into every plugin on the axis — declare them and forward them, or your
	service is skipped with a *"could not be instantiated"* log line. A service that reaches for a
	singleton instead is lying about its dependencies and cannot be unit-tested.

2. **Import optional dependencies at module top, not inside `start()`.** This inverts the
	`InfluxDBBackend` lazy-construction idiom, on purpose. `create_classes` catches
	`ModuleNotFoundError` and skips the plugin with one honest error line while the rest of the EMS
	starts normally; the same import inside a supervised `start()` is a *crash* — five restarts with
	backoff, then CRITICAL — because an operator did not install an optional extra. Runtime
	*resources* (a server object, a socket, a file handle) still belong in `start()`.

3. **Implement `start()`** — it blocks; `main` runs it in its own supervised daemon thread. Loop on
	`while not self.is_stopping():` and sleep through `self.wait_stop(seconds)`, never
	`time.sleep(seconds)`. Returning is a *normal completion* and is never restarted; raising is a
	crash and gets restarted (see *Contracts*).

4. **Make `start()` unblockable.** The framework never kills a thread; it asks. If your loop sits
	inside a foreign blocking call, override `stop()` to reach into it:

	```python
	@override
	def stop(self) -> None:
		super().stop()                    # always first: sets the stop event
		self._server.should_exit = True   # whatever makes start() return
	```

	Two failure modes to design against, both real. A `stop()` arriving *before* `start()` must still
	be honoured — guard the top of `start()` with `if self.is_stopping(): return`, and re-check under
	a lock after building the resource, or a shutdown that races startup binds a port and leaks it.
	And a library that calls `sys.exit()` on failure raises `SystemExit`, which is a `BaseException`:
	the supervisor catches `Exception` and `threading.excepthook` silently swallows `SystemExit`, so
	the thread would die with **no log line, no crash, and no restart** — and without ever marking
	itself finished. Catch it and re-raise something that inherits from `Exception`.
	`services/rest_api.py` does both; uvicorn's failure-to-bind is exactly this case.

5. **Bound every wait you own.** `stop_all()` joins all workers within one shared grace period
	(`runtime.shutdown_timeout_seconds`, default 10s); anything you block on past that is reported as
	a straggler and left to die with the interpreter. A network server's own defaults are chosen for
	throughput, not shutdown — uvicorn's graceful-shutdown timeout defaults to *unbounded*, so
	`RestApiService` caps it at 5s for exactly this reason.

6. **Own no devices, and change nothing.** A service is an observer: no `inject_devices()`, no
	`send()`, and no place in the run's liveness. `main` waits on the **connectors** only, so your
	service never keeps a finished replay alive — that is why an API cannot simply be declared as a
	`Connector`, which would hang every backtest forever. The corollary is worth knowing before you
	debug it: **a config with services and no connectors exits immediately** (`main` logs a warning
	saying so). To hold a process open while poking at a service, run a `pseudo` connector with
	`"loop": true`.

	If you expose runtime state, serialise an **explicit allowlist of named fields** — never
	`vars(obj)`. `Device.__deepcopy__` deliberately *shares* the live connector, so any attribute walk
	over a device snapshot reaches `MQTTConnector.password`, and `InfluxDBBackend.token` is public
	too. Device *data* is telemetry and may be exposed; device *options* are configuration and may
	hold credentials.

7. **Ship `services/<class>.schema.json`** — a standalone JSON Schema (draft 2020-12) for your
	options object, keys matching your constructor kwargs exactly, **minus** `devices_manager` and
	`supervisor`, which never come from config: declaring either would let a config entry collide
	with the injected value and fail instantiation with *"got multiple values"*. `Config` validates
	every entry with your `class` against it at load time (warnings, never fatal), and
	`tests/test_config.py` keeps it in lockstep with the constructor. Copy
	`services/rest_api.schema.json`. Any option that can be written as `${VAR}` must also accept
	`"string"` and `"null"` — interpolation runs *after* validation and yields only strings or null —
	and must be coerced with `api/options.py` rather than a bare `int()`: a `ValueError` escaping your
	`__init__` is contained by `create_classes`, but the service is then skipped entirely — the API
	is simply not there, with nothing listening and nothing at the port to say why.

8. **Config entry** (config key is `class`):

	```json
	{
		"services": [
			{
				"name": "heartbeat",
				"class": "heartbeat",
				"options": {
					"path": "/app/data/ems.heartbeat",
					"interval_seconds": 5
				}
			}
		]
	}
	```

9. **Tests**: copy `tests/test_rest_api_service.py`. `tests/test_shutdown.py` is the template for
	proving `stop()` unblocks `start()`, including `stop()` before `start()`. If your service needs
	an optional dependency, guard the module with `pytest.importorskip("<dep>")` **before any project
	import** — a bare `ModuleNotFoundError` at module top aborts collection for the *entire* suite,
	not just your file. `pytest` must stay green with no network on a core-only checkout.

	**Working outside this repository?** The test files above open with
	`from tests.conftest import …`, which resolves only in this checkout. The axis-generic
	doubles and the threading harness they use — `StubDevice`, `StubConnector`,
	`StubStorageBackend`, `StubAlgorithm`, `make_devices_access`, `run_in_thread`,
	`wait_until`, `assert_stops`, `write_replay` — all live in `api/testing.py` and import
	from anywhere; `conftest.py` only re-exports them. The checks this repository runs over
	its own plugins are in `api/conformance.py`, and `check_loads` is the one worth running
	first: it drives the real loader, so it catches the class-name and `issubclass` mistakes
	that no schema check can see. Neither module is a supported API yet.

	The four concrete-device factories (`make_p1`, `make_shelly`, `make_pseudo`,
	`build_p1_telegram`) stay in `tests/conftest.py` — they build shipped devices, and `api/`
	must not import an axis. `tests/test_shutdown.py` uses `make_pseudo`, so copy that one
	helper across rather than importing it.

## Recipe: add a new algorithm

Everything lives in `algorithms/` plus one test file — no central file needs editing. An algorithm
consumes devices by **capability**, so the same code backtests over a CSV replay and runs live over
MQTT or HTTP without changing a line.

1. **Create `algorithms/<class>.py`** with `class <Name>(Algorithm)` (no suffix — `auto_toggle` →
	`AutoToggle`):

	```python
	from typing import override

	from api.algorithm import Algorithm
	from api.capabilities import EnergyMeter, Switch
	from api.devices_access import DevicesAccess


	class AutoToggle(Algorithm):
		@override
		def __init__(self, name: str, devices_manager: DevicesAccess, **kwargs) -> None:
			super().__init__(name, devices_manager, **kwargs)
	```

	Forward `**kwargs` to `super().__init__` — the base reads `delay_seconds`, `required_devices`, and
	`wait_for_devices_timeout` straight from your config `options`, and coerces all three through
	`api/options.py` so a `${VAR}` that arrives as a string warns instead of crashing your worker
	thread. The lockstep check follows `**kwargs` up the MRO, so those three count as *your*
	options for schema purposes even though you never name them.

2. **Implement `main()`** — one control step. Call `super().main()` first (it refreshes `self.devices`
	from `devices_manager.get_devices()`), then select devices by capability and act:

	```python
	@override
	def main(self) -> None:
		super().main()
		total = 0.0
		for device in self.devices.values():
			if isinstance(device, EnergyMeter):          # a capability, never a concrete class
				energy = device.get_total_energy_kwh()
				if energy is not None:
					total += energy
		for device in self.devices.values():
			if isinstance(device, Switch) and device.data:
				self.control_device(device, device.COMMAND_ON if total > 500 else device.COMMAND_OFF)
	```

	`isinstance`-check the capability ABCs in `api/capabilities.py` (`EnergyMeter`, `Switch`) — **never**
	`isinstance(device, P1)`. That is what keeps the algorithm hardware-agnostic. Actuate through
	`self.control_device(device, command)`: it routes the command to the **live** device (not your
	snapshot copy) and logs the decision to storage **when the command reached a transport**. It
	returns that verdict as a `bool`, so an algorithm that cares can see whether its command
	actually went anywhere; ignoring it is fine and is what `algorithms/auto_toggle.py` does.

3. **Gate on required devices.** Set the `required_devices` class attribute or pass it via config
	`options`; the framework blocks until each is data-ready and connected before the first `main()`,
	bounded by `wait_for_devices_timeout` (default 60s, `null` = wait forever). `required_devices` is only
	the readiness gate — selection inside `main()` stays capability-based.

4. **Write nothing about time or transport.** The base `loop()` steps `main()` once per committed
	timestep when a replay connector drives the clock, and on a `delay_seconds` wall-clock cadence
	otherwise — so one `main()` backtests and runs live unchanged. Under a replay the two are not
	merely similar: the replay runs **lockstep**, holding each timestep until your `main()` returns,
	so a slow algorithm slows the backtest instead of silently skipping timesteps. Read the moment
	with `devices_manager.get_simulation_time()`; it is stable for the whole of your `main()`.
	**Ship `algorithms/<class>.schema.json`.** All five axes validate options now. Copy
	`algorithms/auto_toggle.schema.json`: if your algorithm adds no options of its own it is that
	file unchanged, because the three base options are the whole contract. Declare every option
	the constructor chain accepts and nothing it does not — the check runs in both directions —
	and leave `devices_manager` out: `main` injects it, and declaring it would let a config entry
	collide with the injected value and fail instantiation with "got multiple values". An algorithm
	with no schema file simply gets no options validation, exactly as on the other four axes.

5. **Config entry** (config key is `class`):

	```json
	{
		"algorithms": [
			{
				"name": "Auto Toggle",
				"class": "auto_toggle",
				"options": {
					"required_devices": ["kitchen_meter", "patio_switch"],
					"delay_seconds": 900,
					"wait_for_devices_timeout": 120
				}
			}
		]
	}
	```

6. **Tests**: copy `tests/test_auto_toggle.py` — it drives `main()` with capability **stubs** (a stub
	`EnergyMeter`, a stub `Switch`, and a no-capability device), so no hardware, broker, or concrete
	device class is involved. `pytest` must stay green.

	**Working outside this repository?** The test files above open with
	`from tests.conftest import …`, which resolves only in this checkout. The axis-generic
	doubles and the threading harness they use — `StubDevice`, `StubConnector`,
	`StubStorageBackend`, `StubAlgorithm`, `make_devices_access`, `run_in_thread`,
	`wait_until`, `assert_stops`, `write_replay` — all live in `api/testing.py` and import
	from anywhere; `conftest.py` only re-exports them. The checks this repository runs over
	its own plugins are in `api/conformance.py`, and `check_loads` is the one worth running
	first: it drives the real loader, so it catches the class-name and `issubclass` mistakes
	that no schema check can see. Neither module is a supported API yet.

## Publishing a plugin outside this repository

The five recipes above assume your plugin lives in this tree. It does not have to. A plugin in a
vendor sub-directory of an axis loads today, unmodified, with no registry entry, no packaging and no
pull request:

```
connectors/acme/solar.py            class SolarConnector(Connector)
connectors/acme/solar.schema.json
```

```json
{ "name": "roof", "protocol": "acme.solar", "options": { "host": "10.0.0.7" } }
```

**The config value is `"<vendor>.<name>"`, and it is frozen.** The dot is a directory separator:
`acme.solar` resolves to `<axis>/acme/solar.py` for the import and `<axis>/acme/solar.schema.json`
for the options schema. Everything else in this section is reversible; the string an operator types
into `config.json` is not, because that file is mounted read-only precisely so upgrades never ask
them to rewrite it. It is deliberately **not** `community.<vendor>.<name>`: the bare form survives
unchanged if plugins ever move to a separate `sys.path` root, into a `motrix_edge/` package, or into
wheels, whereas a hard-coded `community.` segment would have to be migrated in every deployment.

**Your repository.** Name it `motrix-edge-<axis>-<name>` — `motrix-edge-connector-solarvendor` — and
give it the GitHub topic `motrix-edge-plugin`. That topic is the whole discovery mechanism; there is
no index to be added to and no maintainer to wait for.

```
motrix-edge-connector-solarvendor/
	connectors/acme/solar.py
	connectors/acme/solar.schema.json
	tests/test_solar.py          # imports api.testing, runs api.conformance
	conftest.py                  # finds a motrix-edge checkout
	LICENSE                      # extensionless
	README.md
```

There is no template repository to fork, deliberately: this repository already ships 25 worked
plugins that the suite keeps green forever, and a template with no CI against `main` would rot
into teaching a layout that no longer loads. Copy the shipped plugin whose shape matches yours,
the way the recipes above tell you to. The only thing you cannot copy from in here is the
out-of-tree scaffolding — a `conftest.py` that finds a checkout and a CI job that clones it — and
both are written out on the docs site's [Publishing a plugin](https://motrix-energy.github.io/contribute/publishing-plugins/)
page. `tests/test_published_plugin.py` here is the executable proof that the convention works: it
builds a vendor package in a temp directory and drives the real loader through it, on the
connector, device and algorithm axes.

Keep `LICENSE` extensionless. `.dockerignore` strips `*.md`, so a `LICENSE.md` would be missing from
a locally built image — and the image is a distribution, so the notice has to travel with it.

**Retrieving one** is three commands and a config entry:

```bash
git clone --depth 1 https://github.com/someone/motrix-edge-connector-solarvendor /tmp/p
cp -r /tmp/p/connectors/acme connectors/
docker compose up -d --build
```

There is no compose edit, no bind mount, no `PYTHONPATH` and no derived image: compose builds `edge`
from `context: .`, and `.dockerignore` excludes no axis directory, so a vendored plugin is already in
the build context. Rebuilding is the status quo rather than a new imposition, because no
`motrix-edge` image is published today. The one real gap is a plugin with its own pip dependency;
there is no mechanism for that yet, and adding one waits until somebody actually needs it.

Bad options are a startup warning naming the key. A missing optional dependency is one skipped entry
and the rest of the EMS starts. A module that raises at import — or calls `sys.exit()` — is one
skipped entry and a traceback.

**What a shared algorithm can actually ask a device, and it is less than you expect.** An algorithm
is portable because it selects devices by capability, never by class, and the entire capability
vocabulary is three things: `EnergyMeter.get_total_energy_kwh()`, which is cumulative imported kWh
and nothing else; `Switch`, a marker interface meaning binary on/off actuated with a `str` token; and
`MetricSource.get_metrics()`, which flows to *storage* and is invisible to algorithms. The one hook a
generic config-driven device has for saying what a number means — `role` in
`devices/modbus_meter.schema.json`, `devices/modbus_switch.schema.json`, `devices/lora.schema.json`
and `devices/lora_switch.schema.json` — is a closed enum with exactly one member,
`energy_import_kwh`.

So a published algorithm can ask a device it has never seen two questions: how many kWh it has
imported in total, and whether it is switchable. There is no instantaneous power, no battery state of
charge, no setpoint or modulation, no tariff or price signal, no forecast and no curtailment limit.
Every richer reading a device already parses flows to storage and is unreachable from an algorithm.
Widening that vocabulary is a live question and worth more to algorithm sharing than any packaging
work, but it is a design decision about an energy domain model rather than a distribution one, and
nothing here changes it. Write your algorithm against what exists.

**If what you are sharing is data, it costs no Python at all.** A new *dialect* of a transport and a
new payload layout are both config, not code. `examples/connectors/lorawan.json` supports a LoRaWAN
network server `connectors/lorawan.py` has never heard of, using `profile: "custom"` and topic
templates from `config.json`; a new Modbus meter is a `registers` array; a new LoRa node is a `fields`
map. A shared JSON fragment cannot execute, cannot reach a credential and cannot crash the EMS, and
it is reviewable by reading it — so prefer it whenever it will do. Two rules if you publish one: it
must never contain `${`, because `Config` resolves that anywhere in the document and a fragment
setting `"unit": "${MQTT_PASSWORD}"` would ride a credential into `device_data.csv`; and it describes
hardware, never a deployment.

**Compatibility, until there is a version handshake.** There is none yet — no `api/version.py`, no
declared plugin API version — so the rule that matters is which changes here can break you. A
defaulted parameter added to an **upcall**, a method the framework offers you, is a minor change:
`on_device_data_received` gained `accepted` with a default for exactly this reason. A parameter added
to a **downcall**, a method the framework calls on your class, is a breaking change, because the
framework calls those positionally. That asymmetry is why `Device.control` returning `bool` and
`DevicesAccess.control` propagating it landed before any external plugin existed rather than after.

**Whether a plugin belongs in *this* repository instead.** A plugin is admitted here when it is a
**protocol rather than a product**, its specification is openly published, and it can be tested with
no hardware and no network. Those criteria fit `connectors/` exactly, and they are meant to: that
axis is where growth would otherwise be unbounded. They fit `devices/` badly, because a device is
inherently product-shaped — that is the point of the axis — so for `devices/` the bar is the last two
criteria plus a general one: the device must be useful to more than its manufacturer's customers, and
`devices/shelly_plug.py` is here as a worked example of the axis rather than as a precedent.
`algorithms/` and `storage/` are judged on the last two criteria alone. `services/` is the one axis
that cannot yet be published outside this tree at all, because `Service.__init__` is typed against a
concrete `Supervisor`; a new service is therefore admitted here on the same two universal criteria,
and the recipe above stands. Anything specific to one manufacturer's product, on any axis, belongs in
its author's own repository on the convention above — which is a statement of policy, not a
description of the current tree.

**What an operator is agreeing to when they install one.** A community plugin runs in-process, in a
supervised thread, with no sandbox — Python offers none worth the name. It can actuate any physical
device, read every other device's live state through the `DevicesManager` singleton whether or not one
was injected into it, reach a connector's credentials through a device's deliberately-shared live
connector, and write anything it likes to storage. The containment above changes the blast radius of
a *mistake* — one bad plugin is one skipped entry instead of a dead EMS — and nothing changes the
blast radius of malice. Containment is for bugs; for malice there is only provenance. `SECURITY.md`
says the same thing to the person deciding whether to install yours.

## Contracts shared with the other axes

- **The config format version is yours to bump, and plugin options are not part of it.**
  `config.json`'s optional top-level `version` declares the format of the *document*;
  `CONFIG_FORMAT_VERSION` in `config/version.py` is the format a build understands. Adding a
  **top-level** key is a MINOR bump; removing or renaming one, changing an existing key's
  meaning or type, or making an optional key required, is MAJOR. Adding or changing a
  **plugin's `options`** is neither — those live in that plugin's `*.schema.json` beside its
  constructor signature, which the kwargs contract above already covers. Bump the constant
  and `config.schema.json`'s `$comment` in the same edit; a test fails when they disagree.
- **Devices** only parse transport data into `self.data` inside `receive()`; the framework publishes
  the device to `DevicesManager` (so algorithms see it) via the connector's `on_device_data_received()`
  hook — device authors never call `update_device` themselves.
- **Readiness** is event-based: the `Connector` base hooks (`on_connected()`,
  `on_device_data_received()`) set the per-device `connected`/`data_ready` events that
  `wait_until_ready()` blocks on. Use the hooks; don't touch the events directly.
- **Capabilities** (`api/capabilities.py`) are the contract between algorithms and devices:
  algorithms `isinstance`-check capability ABCs (`EnergyMeter`, `Switch`), never concrete device
  classes. A new device that implements a capability (e.g. `class MyMeter(Device, EnergyMeter)`)
  works with every algorithm that consumes it; a new algorithm should select devices by
  capability, not by `kind`.
- **Algorithms** select devices by capability (see the algorithm recipe above), so they import no
  concrete device or connector class — the same `main()` runs over hardware or a replay.
- **Cooperative stop** (`api/stoppable.py`) is how connectors and algorithms shut down. On
  SIGTERM/SIGINT `main` calls `stop()` on every worker and joins them within one grace period
  (`runtime.shutdown_timeout_seconds`, default 10s), then closes storage. Nothing is killed: a worker
  that ignores `is_stopping()` is named in a warning and left to die with the interpreter. Algorithms
  get this for free — the base `loop()` is already cooperative — so only connectors with a custom
  blocking `start()` need to do anything.
- **Liveness**: `main` waits on the **connectors** only (`main.py`), then stops everything else. A
  service therefore never keeps a finished run alive — and a config with services but no connectors
  exits immediately, with a warning saying so. This is what makes `services/` a separate axis rather
  than a `Connector` with stub methods: a server that never returns would join that set and hang
  every completed replay.
- **Supervision** (`supervisor/supervisor.py`) watches every worker thread. A method that **raises**
  is logged at ERROR *with its traceback* through the configured logger and restarted with bounded
  exponential backoff (`runtime`: `restart`, `max_restarts`, `backoff_seconds`,
  `max_backoff_seconds`); past the cap it logs CRITICAL and stays down. A method that **returns** is
  a clean completion and is never restarted. Keep that distinction: raise on failure, return when
  genuinely done. Once a worker will not run again — it returned, exited, or is past its restart
  budget — the supervisor calls its `retire()` if it has one, exactly once, and never between a
  crash and its restart. `Algorithm.retire()` leaves the replay barrier, which a crashed algorithm
  otherwise keeps so the replay waits for its restart. The name is therefore reserved on every axis:
  do not give a plugin a `retire` attribute meaning anything else.

## Style

- Python **3.12+** (the code uses `typing.override` and PEP 701 f-strings).
- Indentation is **tabs**, in Python and JSON alike.
- Log through `self.LOGGER` (set up by the ABCs), never `print()`.

## Licensing

Apache-2.0, the same licence as the project — see `LICENSE` and `NOTICE`. **Inbound equals
outbound**: a contribution is offered under the licence the project already carries, so nothing
you send changes the terms anyone else receives it under. There is no CLA, no copyright
assignment and no sign-off requirement; you keep the copyright in what you write.

A plugin in its own repository is yours to license as you like. Apache-2.0 keeps it symmetric
with the runtime it imports, and an operator vendoring your directory into their checkout has one
fewer thing to reason about — but it is a recommendation, not a condition of the convention.
