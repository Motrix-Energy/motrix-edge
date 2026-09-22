from collections import Counter
from datetime import date, datetime
from threading import Lock
from time import monotonic
from typing import Any, Optional, override

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from uvicorn import Config as UvicornConfig, Server

from api import capabilities
from api.algorithm import Algorithm
from api.capabilities import EnergyMeter, MetricSource
from api.connector import Connector
from api.decisions import DEFAULT_CAPACITY, Decision, DecisionLog
from api.device import Device
from api.devices_access import DevicesAccess
from api.options import bool_option, int_option
from api.payload import MAX_DEPTH, finite_or_str
from api.service import Service
from api.stoppable import Stoppable
from simulation.clock import SimulationClock
from supervisor.supervisor import SupervisedWorker, Supervisor

# Discovered rather than listed: a capability added to api/capabilities.py shows up in
# GET /devices with no edit here, the same decentralisation the plugin axes get. The
# __module__ test drops names merely imported into that module (ABC, Any).
_CAPABILITIES: tuple[type, ...] = tuple(
	obj for obj in vars(capabilities).values()
	if isinstance(obj, type) and obj.__module__ == capabilities.__name__
)

# Every response is live state behind a reverse proxy that would otherwise be free to
# cache it — including a payload carrying a physical meter's equipment identifier.
_NO_STORE = {"Cache-Control": "no-store"}


class RestApiService(Service):
	"""Read-only HTTP view of the live runtime, served in-process by uvicorn.

	It exists for the state that never reaches storage. A device that is configured but
	has never produced a reading writes no rows, so no time-series dashboard can tell
	*silent* from *not configured* — absence of data is not data. The same goes for
	readiness and connectedness, for which capabilities a device satisfies, for supervisor
	restart and crash counts, and for replay-barrier progress. Everything here is read
	from the live objects; the service holds no state of its own and writes nothing.

	`/decisions` is the one deliberate exception to "state that never reaches storage": that
	history *is* written, to `algorithm_decisions.csv`. It is served anyway because every
	other endpoint is a snapshot of *now*, and a decision is a discrete event — a client
	polling on an interval sees only the ones that happened to be current when it looked.
	The buffer it reads is core state (`api/decisions.py`), fed from the storage funnel so
	the two surfaces cannot disagree, and recorded whether or not any backend is configured.

	**It touches nothing it was not handed.** The only two runtime references are the
	`devices_manager` and `supervisor` injected by `main`. Notably it does not import
	`Config`: no plugin in this repo does, and a plugin calling `Config()` in a unit test
	would construct a real one against ./config.json. If version information is ever
	wanted here, `main` adds a handle to `arguments=`.

	FastAPI and uvicorn are imported at **module top**, deliberately inverting the
	`InfluxDBBackend` lazy-construction idiom: `main.create_classes` catches
	`ModuleNotFoundError` and reports the plugin cleanly while the rest of the EMS starts
	normally, whereas the same import inside `start()` would be a supervised crash — five
	restarts with backoff, then CRITICAL — because an optional extra was not installed.
	The uvicorn `Server` is still built in `start()`: that is a runtime resource, and the
	InfluxDB reasoning does apply to it. The FastAPI app, by contrast, is built in
	`__init__`, so it can be driven by a test that never binds a socket.

	No CORS and no auth, by construction rather than by omission: the port is not
	published to the host (see `docker-compose.yml`) and nginx reverse-proxies `/api/*` to
	it inside the compose network, so a browser only ever sees one origin. Publish that
	port and authentication becomes the first thing to add here.

	**Lock discipline** (`devices_manager/devices_manager.py` and `simulation/clock.py`
	both state it): never hold `devices_lock` while touching the simulation clock, and
	never nest the two. Every handler here builds its response from a flat sequence of
	independent accessor calls, combining the values afterwards. The tempting
	"optimisation" — a single snapshot call that takes `devices_lock` and reads the clock
	inside it — is the deadlock.
	"""
	DEFAULT_HOST = "127.0.0.1"
	DEFAULT_PORT = 8000
	# Below runtime.shutdown_timeout_seconds (default 10) on purpose. uvicorn's own
	# default is None, i.e. unbounded: a keep-alive connection held open by the proxy
	# would outlive the supervisor's grace period and be reported as a straggler.
	DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5
	_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})

	@override
	def __init__(
		self,
		name: str,
		devices_manager: DevicesAccess,
		supervisor: Supervisor,
		host: Optional[str] = None,
		port: Any = None,
		root_path: Optional[str] = None,
		access_log: Any = False,
		docs: Any = True,
		shutdown_timeout_seconds: Any = None,
	) -> None:
		super().__init__(name, devices_manager, supervisor)  # first: the coercion below logs through self.LOGGER
		# No signature default here is load-bearing. Config resolves ${VAR} to a str, or
		# to None when a whole-value token resolves empty, so any option can arrive as
		# None — the `or DEFAULT` fallbacks are what actually apply the defaults.
		self.host = host or self.DEFAULT_HOST
		self.port = int_option(self.LOGGER, "port", port, self.DEFAULT_PORT, minimum=0, maximum=65535)
		self.root_path = root_path or ""
		self.access_log = bool_option(self.LOGGER, "access_log", access_log, False)
		self.docs = bool_option(self.LOGGER, "docs", docs, True)
		self.shutdown_timeout_seconds = int_option(
			self.LOGGER, "shutdown_timeout_seconds", shutdown_timeout_seconds,
			self.DEFAULT_SHUTDOWN_TIMEOUT_SECONDS, minimum=0
		)
		if self.host not in self._LOOPBACK:
			self.LOGGER.warning(
				f"REST API binds {self.host}: device data and site topology are served "
				f"unauthenticated to everything that can reach that address. In the compose "
				f"deployment that is safe only because the port is not published and the "
				f"viewer's nginx holds the only gate — publishing it removes that gate"
			)
		self._started_at = monotonic()
		self._server_lock = Lock()
		self._server: Optional[Server] = None
		self.app = self._build_app()

	# --- lifecycle -------------------------------------------------------------

	@override
	def start(self) -> None:
		if self.is_stopping():
			self.LOGGER.info("Stop requested before startup, not binding")
			return
		server = Server(UvicornConfig(
			app=self.app,
			host=self.host,
			port=self.port,
			root_path=self.root_path,
			# None, not the uvicorn default: that default installs its own handlers with
			# propagate=False, producing a second, uncoloured log stream beside the one
			# main.py configures. With no config its loggers propagate to the root ones.
			log_config=None,
			access_log=self.access_log,
			# Explicit: "auto" would pick uvloop if it were ever installed, and setting up
			# an event loop policy mutates process-global state from this thread.
			loop="asyncio",
			# Nothing to start or stop inside the app — the EMS owns the lifecycle.
			lifespan="off",
			# "auto" imports whichever websocket implementation happens to be installed
			# (and warns about the deprecated ones). This API serves five GET routes.
			ws="none",
			timeout_graceful_shutdown=self.shutdown_timeout_seconds,
			server_header=False,
		))
		with self._server_lock:
			self._server = server
			if self.is_stopping():  # stop() raced us between the guard above and here
				server.should_exit = True
		self.LOGGER.info(f"REST API listening on http://{self.host}:{self.port}{self.root_path}")
		try:
			# uvicorn's capture_signals() is a no-op off the main thread, so this does not
			# fight main.py's SIGTERM/SIGINT handlers. Do NOT "fix" that with
			# Server.install_signal_handlers (removed in 0.29 — assigning over it is a
			# silent no-op) or UvicornConfig(install_signal_handlers=False) (a TypeError,
			# raised inside a supervised start(), i.e. a restart loop).
			server.run()
		except SystemExit as e:
			# uvicorn calls sys.exit(1) when it cannot bind. SupervisedWorker._run now has a
			# SystemExit branch, so left alone this would be logged CRITICAL and would set
			# _finished — but it would *not* be restarted, because a worker that exits is
			# treated as having given up rather than having failed. A port momentarily held
			# by something else deserves the bounded retry a crash gets, and converting it
			# here is the one thing that still buys it.
			raise RuntimeError(f"REST API could not bind {self.host}:{self.port}") from e
		finally:
			with self._server_lock:
				self._server = None
			self.LOGGER.info("REST API stopped")

	@override
	def stop(self) -> None:
		super().stop()  # always first: sets the stop event
		with self._server_lock:
			if self._server is not None:
				# The only way to unblock a running server.run() from another thread.
				# uvicorn's main loop polls this flag every 100ms, then winds down open
				# connections within timeout_graceful_shutdown.
				self._server.should_exit = True

	def is_serving(self) -> bool:
		"""True once the socket is bound and accepting."""
		with self._server_lock:
			return self._server is not None and bool(self._server.started)

	@property
	def bound_port(self) -> Optional[int]:
		"""The port actually in use, or None when not serving.

		Differs from `self.port` only when that is 0 — which is how a test binds without
		risking a collision. Reading it here rather than in the tests keeps uvicorn's
		internals behind one accessor.
		"""
		with self._server_lock:
			server = self._server
			if server is None or not server.started:
				return None
			for bound in server.servers:
				for sock in bound.sockets:
					return int(sock.getsockname()[1])
		return None

	# --- routing ---------------------------------------------------------------

	def _build_app(self) -> FastAPI:
		"""Wire the routes to bound methods.

		Bound methods rather than decorated module-level functions, for two reasons: a
		handler needs the runtime handles *this instance* was given, and a test can then
		call `service._devices()` and assert on a dict with no socket involved.

		Every handler is a plain `def`, never `async def`. They all take locks held by
		other threads — `DevicesManager.devices_lock`, and a deepcopy of every device
		underneath it. FastAPI runs a sync handler in a threadpool; an async one would
		block the event loop and stall every other request for the duration.

		`response_model=None` throughout: the interesting field, `device.data`, is
		whatever a plugin parsed, so a response model would document a handful of scalars
		and shrug at the one thing that matters. The serialisation contract is enforced by
		the allowlist functions below and by the tests over their exact key sets.
		"""
		app = FastAPI(
			title="Motrix Edge",
			description="Read-only view of the live runtime.",
			root_path=self.root_path,
			docs_url="/docs" if self.docs else None,
			redoc_url=None,
			openapi_url="/openapi.json" if self.docs else None,
		)
		# GET only, everywhere. `DevicesAccess.control()` is one careless route away from
		# turning a read-only observer into an actuation API.
		app.add_api_route("/health", self._health, methods=["GET"], response_model=None)
		app.add_api_route("/devices", self._devices, methods=["GET"], response_model=None)
		app.add_api_route("/devices/{name:path}", self._device, methods=["GET"], response_model=None)
		app.add_api_route("/workers", self._workers, methods=["GET"], response_model=None)
		app.add_api_route("/decisions", self._decisions, methods=["GET"], response_model=None)
		return app

	# --- endpoints -------------------------------------------------------------

	def _health(self) -> JSONResponse:
		payload = self._health_payload()
		# 503 only for "down". Docker's health check is binary and, under
		# `restart: unless-stopped`, an unhealthy verdict restarts the container — which
		# would throw away every other worker's state and a running replay. A worker
		# mid-backoff is already being remedied by the supervisor, so "degraded" is for
		# humans and dashboards and stays 200. "down" means the supervisor gave up
		# permanently, which is the one state a restart can actually fix.
		return self._json(payload, status_code=503 if payload["status"] == "down" else 200)

	def _devices(self) -> JSONResponse:
		devices = self.devices_manager.get_devices()
		return self._json({
			"count": len(devices),
			"simulation_time": _isoformat(self.devices_manager.get_simulation_time()),
			# Sorted so the payload is stable across requests — a viewer diffing it and a
			# test asserting on it both depend on that.
			"devices": [self._device_payload(d) for d in sorted(devices.values(), key=lambda d: d.name)],
		})

	def _device(self, name: str) -> JSONResponse:
		# get_device(), not get_devices() + filter: the latter deepcopies every device to
		# throw all but one away.
		device = self.devices_manager.get_device(name)
		if device is None:
			# The requested name is deliberately not echoed back.
			raise HTTPException(status_code=404, detail="Unknown device")
		return self._json({
			"simulation_time": _isoformat(self.devices_manager.get_simulation_time()),
			"device": self._device_payload(device),
		})

	def _workers(self) -> JSONResponse:
		# list(): main appends to the supervisor's worker list from the main thread while
		# this runs in uvicorn's threadpool. Snapshot rather than iterate live.
		workers = list(self.supervisor.workers)
		participants = frozenset(SimulationClock().participants())
		return self._json({
			"count": len(workers),
			"workers": [self._worker_payload(w, participants) for w in workers],
		})

	def _decisions(self, after: int = 0, limit: int = DEFAULT_CAPACITY) -> JSONResponse:
		"""Algorithm decisions newer than `after`, oldest first.

		The one piece of history this API serves, and the reason it exists: every other
		endpoint is a snapshot of *now*, so a client polling on an interval sees only the
		decisions that happened to be current when it looked. Decisions are discrete events —
		miss the poll, miss the event — which is why this is a cursored log and not a snapshot.

		**`after` is a sequence number, never a timestamp.** `docs/storage-format.md` §4 states
		rows are not monotonic and duplicate timestamps are legal, and under `speed=0` every
		decision in one timestep carries the *identical* committed step time. An inclusive
		timestamp cursor re-delivers the whole timestep on every poll; an exclusive one drops
		all but the first decision in it. `seq` has neither failure. See `DecisionLog.page`.

		Both parameters **clamp rather than reject**, so a polling client cannot wedge itself
		on an out-of-range value it computed. A non-integer still gets FastAPI's own 422: one
		dead request is the right cost for a malformed query, where `api/options.py`'s
		warn-and-default rule exists because a raise there would kill `main`.

		An empty log is `200` with `count: 0`, never 404 — "no decisions yet" and "this EMS is
		too old to have this route" must stay distinguishable, and that single status is the
		whole degradation contract for a client written against a newer EMS.
		"""
		page = DecisionLog().page(after=after, limit=limit)
		return self._json({
			"count": len(page.decisions),
			# Recorded since process start, i.e. the highest seq ever assigned here.
			"total": page.total,
			"retained": page.retained,
			"capacity": page.capacity,
			"oldest_seq": page.oldest_seq,
			# Records evicted between the caller's cursor and what is still held. Stated, so a
			# consumer never has to infer a loss — the rule docs/storage-format.md §7 applies
			# to gaps in the CSV, applied here.
			"missed": page.missed,
			# Server-assigned: pass it back verbatim as `after`. Never the client's own
			# max(seq), and never a record that was not delivered.
			"next_cursor": page.next_cursor,
			# The limit truncated this response — poll again now, do not wait out the interval.
			"has_more": page.has_more,
			# Identity of this process's sequence. A change means the EMS restarted and `seq`
			# began again at 1; without it a client holding a high cursor goes silently blank.
			"epoch": page.epoch,
			"simulation_time": _isoformat(self.devices_manager.get_simulation_time()),
			"decisions": [self._decision_payload(d) for d in page.decisions],
		})

	# --- payloads --------------------------------------------------------------

	def _health_payload(self) -> dict[str, Any]:
		# Read the worker fields directly rather than building full payloads: this is the
		# endpoint a Docker health check polls every 30s forever, and the axis-specific
		# extras are not part of the verdict.
		workers = list(self.supervisor.workers)
		clock = SimulationClock()
		# count_devices(), not get_devices(): the three numbers below are all this endpoint
		# needs, and the copying variant would deepcopy every device payload under
		# devices_lock to produce them — on the endpoint a Docker health check polls forever.
		devices = self.devices_manager.count_devices()
		# An independent accessor call, combined afterwards — never nested inside the clock
		# read above or count_devices(). See the lock discipline in the class docstring.
		decisions = DecisionLog().counts()
		states = Counter(self._worker_state(worker) for worker in workers)
		damaged = [worker for worker in workers if worker.crashes]
		if states["down"] or states["lost"]:
			status = "down"
		elif damaged:
			status = "degraded"
		else:
			status = "ok"
		return {
			"status": status,
			# Of this service, not of the process: main's start time is a local in the
			# __main__ block and is not reachable from here.
			"uptime_seconds": round(monotonic() - self._started_at, 1),
			"clock": {
				"simulated": clock.is_simulated(),
				"generation": clock.generation(),
				"step_time": _isoformat(clock.get_step_time()),
				"pending": clock.pending(),
			},
			"workers": {
				"total": len(workers),
				"running": states["running"],
				"finished": states["finished"],
				"down": states["down"],
				"lost": states["lost"],
				"crashed": len(damaged),
				"restarts": sum(worker.restarts for worker in workers),
			},
			"devices": {
				"total": devices.total,
				"connected": devices.connected,
				"data_ready": devices.data_ready,
			},
			# Three O(1) reads, so this stays cheap on the endpoint a Docker health check polls
			# forever. It lets a client notice it has fallen behind — or that `total` went
			# *down*, which means the process restarted — without polling /decisions at all.
			"decisions": {
				"total": decisions.total,
				"retained": decisions.retained,
				"capacity": decisions.capacity,
			},
		}

	def _device_payload(self, device: Device) -> dict[str, Any]:
		"""Explicit allowlist — every field is named on purpose.

		**Never `vars(device)`.** `Device.__deepcopy__` deliberately *shares* the live
		connector rather than copying it (`api/device.py`), so any attribute walk over a
		snapshot reaches `MQTTConnector.password`, which is a plain public attribute. The
		option dicts are excluded for the same reason: `listener_options` and
		`controller_options` are free-form config and routinely carry endpoints, topics and
		API keys. Device *data* is telemetry and is exposed; device *options* are
		configuration and are not.

		The only field with residual exposure is `data` itself — a P1 telegram carries the
		meter's equipment identifier. That is the point of the endpoint, and the mitigation
		is deployment: the port is not published to the host. This is the reason it must
		stay that way.
		"""
		options = device.connector_options if isinstance(device.connector_options, dict) else {}
		return {
			"name": device.name,
			# The config `kind` is not stored on a Device, so this is the class name: kind
			# "p1" appears here as "P1". Do not treat it as the config key.
			"class": type(device).__name__,
			"connector": options.get("name"),
			# The *emulated* protocol: a replay connector with `emulates: "mqtt"` makes its
			# devices report "mqtt". The `connector` field above disambiguates.
			"protocol": options.get("protocol"),
			"readable": bool(device.is_readable),
			"writable": bool(device.is_writable),
			"connected": device.is_connected(),
			"data_ready": device.is_data_ready(),
			"capabilities": [c.__name__ for c in _CAPABILITIES if isinstance(device, c)],
			"metrics": self._capability(device, MetricSource, "get_metrics"),
			"total_energy_kwh": self._capability(device, EnergyMeter, "get_total_energy_kwh"),
			"data": self._jsonable(device.data, 0),
		}

	def _decision_payload(self, decision: Decision) -> dict[str, Any]:
		"""Explicit allowlist, five keys. **Never `dataclasses.asdict`** — that is `vars()`
		wearing a hat, and it would hand a future field to every client the day it is added.

		Four of the keys are `algorithm_decisions.csv`'s own column names, on purpose: a
		consumer already reading the CSV needs no second vocabulary, and the same decision
		produces an identical row through either surface. `seq` is transport, not data.

		`command` is served **verbatim** — never parsed, never truncated. It is an opaque
		string (`docs/storage-format.md` §6): `AutoToggle` emits the bare words `on`/`off`,
		and pre-parsing a JSON one here would make a live decision and its CSV twin two
		different events.
		"""
		return {
			"seq": decision.seq,
			# Naive stays naive — the recorded step time, not a re-stamp, and never null.
			"timestamp": _isoformat(decision.timestamp),
			"algorithm": decision.algorithm,
			"device": decision.device,
			"command": decision.command,
		}

	def _worker_payload(self, worker: SupervisedWorker, participants: frozenset[str]) -> dict[str, Any]:
		"""One supervised worker, whichever axis it came from.

		One endpoint rather than the /connectors + /algorithms originally planned: the
		supervisor keeps a single list and draws no such distinction, so two endpoints
		would be the API inventing a taxonomy the runtime does not have — and would have
		gone blind to this very axis the day it was added.
		"""
		target = worker.worker
		payload: dict[str, Any] = {
			"name": worker.name,
			"axis": self._axis(target),
			"class": type(target).__name__,
			"state": self._worker_state(worker),
			"restarts": worker.restarts,
			"crashes": worker.crashes,
			"max_restarts": worker.policy.max_restarts,
			"restart_enabled": worker.policy.enabled,
			"stopping": target.is_stopping() if isinstance(target, Stoppable) else None,
		}
		if isinstance(target, Connector):
			# Names only: `Connector.devices` holds the live Device objects, and it is
			# unset until main calls inject_devices().
			payload["devices"] = sorted(getattr(target, "devices", {}))
		elif isinstance(target, Algorithm):
			payload["delay_seconds"] = target.delay_seconds
			# list(): required_devices is a mutable class attribute by default.
			payload["required_devices"] = list(target.required_devices)
			payload["wait_for_devices_timeout"] = target.wait_for_devices_timeout
			payload["runs"] = target.runs
			payload["last_run"] = _isoformat(target.last_run_at)
			payload["last_run_seconds"] = (
				round(target.last_run_seconds, 3) if target.last_run_seconds is not None else None
			)
			# Distinguishes "stuck on the barrier" from "left, and the replay moved on".
			payload["step_participant"] = target.name in participants
		# Algorithm.devices is deliberately absent: it is only assigned inside main(), so
		# it is unset until the first run and afterwards is a snapshot from an arbitrary
		# past moment. Reporting it would present stale data as current.
		return payload

	@staticmethod
	def _axis(target: Any) -> str:
		"""Which plugin axis a supervised worker came from."""
		if isinstance(target, Connector):
			return "connector"
		if isinstance(target, Algorithm):
			return "algorithm"
		if isinstance(target, Service):
			return "service"
		return "unknown"

	@staticmethod
	def _worker_state(worker: SupervisedWorker) -> str:
		"""running | finished | down | lost.

		`is_finished()` is set on all five of `SupervisedWorker._run`'s exit paths, so
		`completed_cleanly` is what separates a replay that ran to its end from a worker
		that exhausted its restart budget. The counters carry the damage either way: a
		worker that crashed twice, restarted, and later returned cleanly is `finished`
		with `crashes: 2`, which is the honest report.

		`lost` is the one no other surface can produce — a thread that died without the
		supervisor noticing, which is what a `SystemExit` escaping a library would do.
		"""
		if worker.is_finished():
			return "finished" if worker.completed_cleanly else "down"
		if worker.is_alive():
			return "running"
		return "lost"

	# --- serialisation ---------------------------------------------------------

	def _capability(self, device: Device, capability: type, method: str) -> Any:
		"""Call a capability method, or None when the device does not implement it.

		`api/capabilities.py` says these never raise, but a read-only endpoint is the
		wrong place to discover that a third-party device broke the contract: one
		malformed payload would turn the whole device list into a 500.
		"""
		if not isinstance(device, capability):
			return None
		try:
			return self._jsonable(getattr(device, method)(), 0)
		except Exception as e:
			self.LOGGER.warning(f"Device '{device.name}' raised in {method}(): {e}")
			return None

	def _jsonable(self, value: Any, depth: int) -> Any:
		"""Make an arbitrary device payload safe to serialise.

		Device payloads have no common shape and no serialisation contract — P1 is a
		nested OBIS tree, a future device could hold bytes or a datetime. An unserialisable
		value would raise inside the response encoder, i.e. a 500 for the entire list
		because one device is unusual; anything unknown degrades to `str()` instead. Never
		to an attribute dump: `str(connector)` yields an address, `vars(connector)` yields
		a password.

		The float branch is not cosmetic — see `api/payload.finite_or_str`. The depth bound
		is shared with the two storage walkers for the same reason: it is one policy, and
		when it was spelled out three times the three copies had already drifted.
		"""
		if depth >= MAX_DEPTH:
			return str(value)
		if value is None or isinstance(value, (bool, int, str)):
			return value  # bool before int: bool subclasses it
		if isinstance(value, float):
			return finite_or_str(value)
		if isinstance(value, (datetime, date)):
			return value.isoformat()
		if isinstance(value, dict):
			return {str(k): self._jsonable(v, depth + 1) for k, v in value.items()}
		if isinstance(value, (list, tuple, set, frozenset)):
			return [self._jsonable(item, depth + 1) for item in value]
		if isinstance(value, (bytes, bytearray)):
			return value.decode("utf-8", errors="replace")
		return str(value)

	@staticmethod
	def _json(payload: Any, status_code: int = 200) -> JSONResponse:
		return JSONResponse(payload, status_code=status_code, headers=_NO_STORE)


def _isoformat(moment: Optional[datetime]) -> Optional[str]:
	"""Both clocks produce naive *local* datetimes; keep them naive rather than inventing
	an offset the source never carried (the trap storage/influxdb.py documents)."""
	return moment.isoformat() if moment is not None else None
