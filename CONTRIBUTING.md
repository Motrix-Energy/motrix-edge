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
	  `inject_devices()`; per-device routing hints live in `device.listener_options`), call
	  `device.receive(payload)`, then `self.on_device_data_received(device, accepted)` **passing what
	  `receive()` returned** — this marks the device data-ready/connected, publishes it to
	  `DevicesManager` (so algorithms see it), and fans the data out to storage. Skip either call and
	  algorithms never see the data. A device returns `False` when the payload gave it nothing usable;
	  forwarding that is what keeps a corrupt frame out of storage instead of republishing the device's
	  previous reading under a new timestamp. The argument defaults to accepted, so a connector that
	  omits it behaves as before.

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
	  ladder, paho's reconnect, the resubscribe-on-reconnect and the threaded fan-out.

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
	passes `(entity_id, payload)`). Dispatch on
	`self.connector_options["protocol"]` if you support more than one, parse into `self.data`, and
	return. **You do not call `update_device`** — the connector's `on_device_data_received()` hook
	publishes the device to `DevicesManager` for you, so a well-formed `receive()` is all algorithms need.

	**Every argument is a `str`, and that is a contract, not an accident.** `PseudoConnector`
	replays a `topic` and a `payload` column out of a CSV, both strings, so a connector that
	handed its device a pre-parsed `dict` would force a second, replay-only parse path — and
	the backtest, which is what every regression fixture uses, would then never exercise the
	production parser. `connectors/modbus_tcp.py` serialises its register words to JSON for
	exactly this reason. Accept the two-argument arity even if your connector only ever sends
	one: a replay row with a non-empty `topic` column otherwise raises a `TypeError` that the
	replay loop swallows, and the backtest silently produces nothing.

	`protocol` is injected by `Config` from the connector the device is wired to, and you do **not**
	need a branch for `pseudo`: a replay connector declares `emulates` (see `connectors/pseudo.schema.json`) and
	impersonates the transport it stands in for, so your device is backtestable through a replay file
	without knowing it. Payloads reach you byte for byte, including multi-line ones.

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
	device.data`, with **no** `is_writable` check, and `Algorithm.control_device` writes the
	decision to storage *before* `Device.control` gets to refuse a non-writable device. So a
	read-only device subclassing `Switch` puts a row in `algorithm_decisions.csv` claiming an
	algorithm switched a thermometer on, **every tick** — a wrong entry in the versioned
	storage contract (`docs/storage-format.md`), not merely a noisy log. Deriving
	`is_writable` per instance does not fix it, because `isinstance` is class-level. The
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
	`__init__` escapes `create_classes` and kills the process.

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
	`wait_for_devices_timeout` straight from your config `options`.

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
	snapshot copy) and logs the decision to storage.

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
	Algorithms ship **no options schema** (the other three axes do); your constructor signature is the
	options contract, enforced by `TypeError` at load time.

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

## Contracts shared with the other axes

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
  genuinely done.

## Style

- Python **3.12+** (the code uses `typing.override` and PEP 701 f-strings).
- Indentation is **tabs**, in Python and JSON alike.
- Log through `self.LOGGER` (set up by the ABCs), never `print()`.
