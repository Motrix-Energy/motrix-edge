import csv
from collections import namedtuple
from datetime import datetime
from json import load
from typing import override

from api.connector import Connector
from api.device import Device
from devices_manager.devices_manager import DevicesManager
from simulation.clock import SimulationClock

ReplayEntry = namedtuple('ReplayEntry', ['timestamp', 'device_name', 'topic', 'payload'])


class PseudoConnector(Connector):
	"""Replays timestamped device payloads from a CSV or JSON file.

	Enables local algorithm development and backtesting without hardware.
	Algorithms are completely unaware they are running in simulation.

	The replay runs **lockstep** with the algorithms: after committing a timestep it
	waits for every algorithm to finish processing it before dispatching the next one.
	That is what makes a backtest deterministic — `speed=0` therefore means "as fast as
	the algorithms allow", not "as fast as the file can be read".
	"""
	replay_file: str
	speed: float
	loop: bool
	control_log: str | None
	step_timeout_seconds: float | None
	emulates: str | None

	@override
	def __init__(self, name: str, replay_file: str, speed: float = 1, loop: bool = False, control_log: str | None = None, step_timeout_seconds: float | None = 30.0, emulates: str | None = None) -> None:
		super().__init__(name)
		self.replay_file = replay_file
		self.speed = speed
		self.loop = loop
		self.control_log = control_log
		self.step_timeout_seconds = step_timeout_seconds
		# Which transport this replay stands in for. Devices dispatch on the protocol
		# Config injects into their connector_options, so without this a P1 or
		# ShellyPlug attached to a replay sees "pseudo", finds no branch for it and
		# raises — only devices that ignore protocol entirely could be replayed.
		# Recorded here because the connector is where the substitution happens;
		# Config is what consumes it when resolving device options.
		self.emulates = emulates
		self._late_steps = 0
		# Declared here rather than when the first step is published: __init__ runs on
		# the main thread during startup, before any worker exists. Waiting would leave
		# a window where an algorithm sees no clock, takes the wall-clock branch, and
		# vanishes into a delay_seconds sleep while the replay is already waiting on it.
		SimulationClock().start_simulation()

	def _load_replay_file(self) -> list[ReplayEntry]:
		ext = self.replay_file.rsplit('.', 1)[-1].lower() if '.' in self.replay_file else ''

		if ext == 'csv':
			return self._load_csv()
		elif ext == 'json':
			return self._load_json()
		else:
			raise ValueError(f"Unsupported replay file format: '.{ext}'. Use .csv or .json")

	def _sort_entries(self, entries: list[ReplayEntry]) -> list[ReplayEntry]:
		"""Order a replay by time, tolerating a file that mixes naive and aware stamps.

		Sorting on the datetimes directly raises TypeError the moment one row carries an
		offset and another does not — and the sort sits outside the per-row try, so it
		escapes start() as a supervised *crash*: five restarts with backoff, then
		CRITICAL, for a file the loader had already read row by row without complaint.

		astimezone() reads a naive value as system-local, which is this project's stated
		interpretation of one everywhere else (storage/influxdb.py:_as_utc converts the
		same way). Only the ordering key is normalised — the entries keep the exact
		timestamps they were read with, so what storage stamps a reading with is unchanged.
		"""
		if len({entry.timestamp.tzinfo is not None for entry in entries}) > 1:
			self.LOGGER.warning(
				f"'{self.replay_file}' mixes naive and offset-aware timestamps; "
				f"ordering naive rows as local time"
			)
		entries.sort(key=lambda e: e.timestamp.astimezone())
		return entries

	def _load_csv(self) -> list[ReplayEntry]:
		entries: list[ReplayEntry] = []
		with open(self.replay_file, newline='', encoding='utf-8') as f:
			reader = csv.DictReader(f)
			for row_num, row in enumerate(reader, start=2):
				try:
					timestamp = datetime.fromisoformat(row['timestamp'].strip())
					device_name = row['device_name'].strip()
					topic = row.get('topic', '').strip() or None
					# Not stripped: the payload is opaque device data, and trimming it
					# corrupts anything whitespace-delimited — a P1 telegram loses the
					# CRLF that terminates it. The structural columns above are stripped
					# because surrounding whitespace can never be meaningful there.
					payload = row.get('payload', '')
					entries.append(ReplayEntry(timestamp, device_name, topic, payload))
				# AttributeError included: csv.DictReader pads a short row with None, so a
				# truncated line makes .strip() fail — which would otherwise kill the
				# replay thread instead of skipping the one row that is malformed.
				except (AttributeError, KeyError, ValueError) as e:
					self.LOGGER.warning(f"Skipping malformed CSV row {row_num}: {e}")
		return self._sort_entries(entries)

	def _load_json(self) -> list[ReplayEntry]:
		entries: list[ReplayEntry] = []
		with open(self.replay_file, encoding='utf-8') as f:
			data = load(f)
		for idx, item in enumerate(data):
			try:
				timestamp = datetime.fromisoformat(item['timestamp'].strip())
				device_name = item['device_name'].strip()
				topic = item.get('topic', '').strip() or None
				payload = item.get('payload', '') if isinstance(item.get('payload'), str) else str(item.get('payload', ''))
				entries.append(ReplayEntry(timestamp, device_name, topic, payload))
			except (AttributeError, KeyError, ValueError) as e:
				self.LOGGER.warning(f"Skipping malformed JSON entry {idx}: {e}")
		return self._sort_entries(entries)

	@override
	def start(self) -> None:
		try:
			entries = self._load_replay_file()
		except FileNotFoundError:
			self.LOGGER.error(f"Replay file not found: {self.replay_file}")
			return
		except ValueError as e:
			self.LOGGER.error(str(e))
			return

		if not entries:
			self.LOGGER.warning(f"Replay file is empty: {self.replay_file}")
			return

		clock = SimulationClock()
		self.on_connected()
		self.LOGGER.info(f"Starting replay of {len(entries)} entries from {self.replay_file} (speed={self.speed}, loop={self.loop}, step_timeout={self.step_timeout_seconds})")

		warned_devices: set[str] = set()
		while not self.is_stopping():
			prev_timestamp: datetime | None = None
			for i, entry in enumerate(entries):
				if self.is_stopping():
					break
				# Sleep based on time delta
				if prev_timestamp is not None and self.speed > 0:
					delta = (entry.timestamp - prev_timestamp).total_seconds()
					if delta > 0 and self.wait_stop(delta / self.speed):
						break
				prev_timestamp = entry.timestamp

				# Event time is the timestamp of the data about to be dispatched, so a
				# storage backend stamps this reading with the moment it was produced.
				# Set before the device lookup so the unknown-device path below leaves
				# the clock correct rather than one entry behind.
				clock.set_event_time(entry.timestamp)

				# Look up device
				device = self.devices.get(entry.device_name)
				if device is None:
					if entry.device_name not in warned_devices:
						self.LOGGER.warning(f"Unknown device '{entry.device_name}' in replay file, skipping")
						warned_devices.add(entry.device_name)
				else:
					# Dispatch to device
					try:
						if entry.topic is not None:
							accepted = device.receive(entry.topic, entry.payload)
						else:
							accepted = device.receive(entry.payload)
						self.on_device_data_received(device, accepted)
					except Exception as e:
						self.LOGGER.error(f"Error replaying entry for {entry.device_name}: {e}")

				# Commit the timestep once all of its devices are dispatched, then wait
				# for the algorithms to finish it. Reached even for an unknown device:
				# skipping it there would stall every algorithm on a timestep whose last
				# row happens to name a device that is not in the config.
				next_ts = entries[i + 1].timestamp if i + 1 < len(entries) else None
				if next_ts != entry.timestamp:
					clock.publish_step(entry.timestamp)
					self._await_algorithms(clock, entry.timestamp)

			if self.is_stopping():
				clock.reset()
				self.LOGGER.info("Replay stopped")
				break
			if not self.loop:
				clock.reset()
				self.LOGGER.info("Replay complete")
				break
			self.LOGGER.info("Replay loop restarting")

	def _await_algorithms(self, clock: SimulationClock, moment: datetime) -> None:
		"""Hold the replay until every algorithm has finished this timestep.

		A timeout does not disable lockstep for the rest of the run: a determinism
		guarantee that silently stops holding after one slow step is worse than a run
		that is visibly stuck. First occurrence ERROR, repeats WARNING.
		"""
		late = clock.wait_for_completion(self.step_timeout_seconds, self.is_stopping)
		if not late:
			return
		self._late_steps += 1
		message = f"Algorithms {late} did not finish step {moment.isoformat()} within {self.step_timeout_seconds}s, advancing anyway — this backtest is no longer deterministic"
		if self._late_steps > 1:
			self.LOGGER.warning(message)
		else:
			self.LOGGER.error(message)

	@override
	def send(self, device: Device, payload: str) -> None:
		self.LOGGER.info(f"[PSEUDO SEND] {device.name}: {payload}")
		if self.control_log:
			try:
				with open(self.control_log, 'a', encoding='utf-8') as f:
					sim_time = DevicesManager().get_simulation_time()
					f.write(f"{(sim_time or datetime.now()).isoformat()},{device.name},{payload}\n")
			except OSError as e:
				self.LOGGER.error(f"Failed to write control log: {e}")
