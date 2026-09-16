from datetime import datetime, timezone
from math import isfinite
from re import compile
from threading import Lock
from typing import Any, Optional, override

from influxdb_client import InfluxDBClient, Point, WriteOptions, WritePrecision
from influxdb_client.client.exceptions import InfluxDBError
from influxdb_client.client.write_api import WriteApi

from api.capabilities import MetricSource
from api.device import Device
from api.options import bool_option, int_option
from api.payload import MAX_DEPTH
from api.storage_backend import StorageBackend

# A deliberately stricter numeric test than float(), which also accepts "nan",
# "inf", "Infinity", "1_0" (-> 10.0) and surrounding whitespace. A device id of
# "1_0" silently becoming a number is the kind of bug that never gets found.
_NUMERIC = compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


class InfluxDBBackend(StorageBackend):
	"""Streams device data and algorithm decisions into InfluxDB 2.x.

	Unlike the write-through CsvFileBackend, points are queued by the client's
	background batching writer and flushed every flush_interval_ms or once
	batch_size points are pending — so close() is load-bearing here: it is what
	flushes the tail of the run.

	The client is built lazily on first write, never in __init__: main.create_classes
	only catches TypeError/AttributeError/ModuleNotFoundError, so anything else
	escaping a constructor takes the whole EMS down, and storage is optional.
	"""
	DEFAULT_URL = "http://localhost:8086"
	DEFAULT_BUCKET = "motrix"
	DEFAULT_MEASUREMENT = "device_data"
	DEFAULT_DECISIONS_MEASUREMENT = "algorithm_decisions"
	DEFAULT_SEPARATOR = "."

	# Tag names are class constants rather than options on purpose: read()'s Flux
	# hardcodes `r.device == _device`, and Flux's static record typing makes a
	# bind-parameter tag key (`r[_tagKey]`) impossible — so a configurable tag
	# name would be a config value that silently breaks read().
	DEVICE_TAG = "device"
	KIND_TAG = "device_kind"
	ALGORITHM_TAG = "algorithm"
	COMMAND_FIELD = "command"
	# A numeric companion to the command string: Grafana cannot count or window a
	# measurement that only ever carries a string field.
	COUNT_FIELD = "value"

	ERROR_PAYLOAD_CHARS = 500

	# Annotated-CSV scaffolding that pivot() leaves on every record. An explicit set
	# rather than a "starts with _" heuristic, so a device field named _foo survives.
	RESERVED_COLUMNS = frozenset({"result", "table", "_start", "_stop", "_time", "_measurement", "_field", "_value"})

	@override
	def __init__(
		self,
		name: str,
		url: Optional[str] = None,
		token: Optional[str] = None,
		org: Optional[str] = None,
		bucket: Optional[str] = None,
		measurement: Optional[str] = None,
		decisions_measurement: Optional[str] = None,
		tags: Optional[dict[str, Any]] = None,
		field_separator: Optional[str] = None,
		parse_numeric_strings: Any = True,
		batch_size: Any = 500,
		flush_interval_ms: Any = 10_000,
		retry_interval_ms: Any = 5_000,
		max_retries: Any = 5,
		max_close_wait_ms: Any = 15_000,
		max_retry_time_ms: Any = None,
		timeout_ms: Any = 10_000,
	) -> None:
		super().__init__(name)  # first: the coercion helpers below log through self.LOGGER
		# No default in this signature is load-bearing. Config resolves ${VAR} into a
		# str, or into None when a whole-value token resolves empty, so any option can
		# arrive as None — the `or DEFAULT` fallbacks are what actually apply defaults.
		if url is None:
			self.LOGGER.warning(f"No InfluxDB url configured, falling back to {self.DEFAULT_URL}")
		self.url = url or self.DEFAULT_URL
		self.token = token
		self.org = org
		self.bucket = bucket or self.DEFAULT_BUCKET
		self.measurement = measurement or self.DEFAULT_MEASUREMENT
		self.decisions_measurement = decisions_measurement or self.DEFAULT_DECISIONS_MEASUREMENT
		self.field_separator = field_separator or self.DEFAULT_SEPARATOR
		# Empty tag values are dropped rather than written: InfluxDB treats an empty
		# tag value as "tag absent", which silently splits the series in two.
		self.tags = {
			str(key): str(value)
			for key, value in (tags or {}).items()
			if value is not None and str(value) != ""
		}
		self.parse_numeric_strings = bool_option(self.LOGGER, "parse_numeric_strings", parse_numeric_strings, True)
		self.batch_size = int_option(self.LOGGER, "batch_size", batch_size, 500, minimum=1)
		self.flush_interval_ms = int_option(self.LOGGER, "flush_interval_ms", flush_interval_ms, 10_000, minimum=1)
		self.retry_interval_ms = int_option(self.LOGGER, "retry_interval_ms", retry_interval_ms, 5_000, minimum=0)
		self.max_retries = int_option(self.LOGGER, "max_retries", max_retries, 5, minimum=0)
		# The library default is 300_000 ms. StorageManager.close_all() is not bounded
		# by runtime.shutdown_timeout_seconds, so with InfluxDB unreachable that default
		# would hang shutdown for five minutes.
		self.max_close_wait_ms = int_option(self.LOGGER, "max_close_wait_ms", max_close_wait_ms, 15_000, minimum=1)
		# Defaults to max_close_wait_ms rather than the library's 180_000 ms, and the two
		# are coupled on purpose. close() force-closes the writer at max_close_wait_ms,
		# but that does not cancel an in-flight retry: the client's write threads are not
		# daemons, so a longer retry budget keeps the interpreter alive past "Shutdown
		# complete" — measured at 213s of overrun with the library defaults.
		self.max_retry_time_ms = int_option(self.LOGGER, "max_retry_time_ms", max_retry_time_ms, self.max_close_wait_ms, minimum=1)
		if self.max_retry_time_ms > self.max_close_wait_ms:
			self.LOGGER.warning(f"max_retry_time_ms ({self.max_retry_time_ms}) exceeds max_close_wait_ms ({self.max_close_wait_ms}), shutdown can hang by the difference while a retry is in flight")
		self.timeout_ms = int_option(self.LOGGER, "timeout_ms", timeout_ms, 10_000, minimum=1)

		# Both are fatal at write time but invisible at construction time: the client
		# builds fine, write() returns instantly, and nothing ever lands.
		if not self.token:
			self.LOGGER.warning("No InfluxDB token configured, an authenticated server will reject every write")
		if not self.org:
			self.LOGGER.warning("No InfluxDB org configured, InfluxDB 2.x requires one for writes and queries")

		self._reserved_columns = self.RESERVED_COLUMNS | {self.DEVICE_TAG, self.KIND_TAG} | set(self.tags)
		self._lock = Lock()
		self._client: Optional[InfluxDBClient] = None
		self._write_api: Optional[WriteApi] = None
		self._disabled = False
		self._closed = False
		self._closed_warned = False
		self._write_failing = False
		self._empty_devices: set[str] = set()

	# --- flattening ------------------------------------------------------------

	def _flatten_data(self, data: dict[str, Any]) -> dict[str, Any]:
		"""Turn arbitrary nested device data into a flat {field_key: value} dict.

		Device payloads have no common shape — ShellyPlug is flat with string-typed
		numbers, P1 is deeply nested OBIS lists, Pseudo carries a raw payload — so
		flattening is generic. It also snapshots the values, which matters because the
		dict handed to write_device_data *is* device.data and keeps being mutated.
		"""
		fields: dict[str, Any] = {}
		self._flatten(data, "", fields, 0)
		fields.pop("", None)  # a non-dict at the root would land under the empty key
		return fields

	def _flatten(self, value: Any, prefix: str, out: dict[str, Any], depth: int) -> None:
		if value is None:
			return  # InfluxDB has no null field type
		if isinstance(value, bool):
			out[prefix] = value  # before int: bool is a subclass of int
			return
		if isinstance(value, (int, float)):
			number = self._as_float(value)
			if number is not None:
				out[prefix] = number
			return
		if isinstance(value, str):
			if self.parse_numeric_strings and _NUMERIC.fullmatch(value.strip()):
				number = self._as_float(value)
				if number is not None:
					out[prefix] = number
					return
			out[prefix] = value
			return
		if depth >= MAX_DEPTH:
			self.LOGGER.debug(f"Nesting limit reached at '{prefix}', storing its repr")
			out[prefix] = str(value)
			return
		if isinstance(value, dict):
			# list(): device.data is live and mutated by other threads. Materialising
			# the items in one C-level call avoids "dictionary changed size during
			# iteration" without holding a lock across the whole walk.
			for key, item in list(value.items()):
				self._flatten(item, self._join(prefix, str(key)), out, depth + 1)
			return
		if isinstance(value, (list, tuple)):
			for index, item in enumerate(list(value)):
				self._flatten(item, self._join(prefix, str(index)), out, depth + 1)
			return
		out[prefix] = str(value)  # datetime, Decimal, anything exotic

	def _join(self, prefix: str, component: str) -> str:
		return f"{prefix}{self.field_separator}{component}" if prefix else component

	@staticmethod
	def _as_float(value: Any) -> Optional[float]:
		"""Every number becomes a float.

		InfluxDB pins a field's type on first write, and an int/float alternation on one
		key is a type conflict: the server answers 422 and drops the offending points
		(a partial write — the rest of the batch still lands).
		parsers.obis.string_to_type returns int or float depending on whether the meter
		printed a decimal point, so that alternation is guaranteed in production.
		NaN and infinities are not representable in line protocol.
		"""
		try:
			number = float(value)
		except (TypeError, ValueError, OverflowError):
			return None
		return number if isfinite(number) else None

	# --- writing ---------------------------------------------------------------

	@staticmethod
	def _as_utc(moment: datetime) -> datetime:
		"""Normalise an event time to an aware UTC datetime.

		Both clocks hand out naive *local* datetimes: datetime.now(), and the replay
		clock, which connectors/pseudo.py parses off a CSV carrying no offset.
		influxdb-client stamps a naive datetime as if it were already UTC
		(DateHelper.to_utc does value.replace(tzinfo=utc)), so a naive Brussels value
		would be filed two hours early in summer. astimezone() reads a naive value in
		the system zone and converts — and is a correct no-op for an aware datetime.
		"""
		return moment.astimezone(timezone.utc)

	def _apply_static_tags(self, point: Point) -> None:
		for key, value in self.tags.items():
			point.tag(key, value)

	def _device_fields(self, device: Device, data: dict[str, Any]) -> dict[str, Any]:
		"""Field set for a device reading, preferring the device's own naming.

		A MetricSource names its own readings, which is the only way to key data whose
		structure encodes meaning by position — P1's OBIS list would otherwise become
		`data.7.obis.class`, where index 7 is a different register the moment the meter
		emits a different number of lines. The result still goes through the flattener:
		that is where bool-before-int ordering, float coercion and NaN dropping live,
		and a capability must not bypass them.
		"""
		if isinstance(device, MetricSource):
			return self._flatten_data(device.get_metrics())
		return self._flatten_data(data)

	@override
	def write_device_data(self, device: Device, data: dict[str, Any]) -> None:
		fields = self._device_fields(device, data)  # outside the lock: pure CPU
		if not fields:
			self._warn_empty_once(device.name)
			return
		point = Point(self.measurement).tag(self.DEVICE_TAG, device.name).tag(self.KIND_TAG, type(device).__name__)
		self._apply_static_tags(point)
		for key, value in fields.items():
			point.field(key, value)
		self._enqueue(point, self._data_timestamp())

	@override
	def write_algorithm_decision(self, algorithm: str, device: str, command: str) -> None:
		point = Point(self.decisions_measurement).tag(self.ALGORITHM_TAG, algorithm).tag(self.DEVICE_TAG, device)
		self._apply_static_tags(point)
		point.field(self.COMMAND_FIELD, command)
		point.field(self.COUNT_FIELD, 1.0)
		self._enqueue(point, self._decision_timestamp())

	def _enqueue(self, point: Point, moment: datetime) -> None:
		point.time(self._as_utc(moment), write_precision=WritePrecision.NS)
		with self._lock:
			if self._closed:
				self._warn_closed_once()
				return
			if not self._ensure_write_api():
				return
			# Cheap under the lock: in batching mode write() only pushes onto the
			# client's queue and does no I/O. CsvFileBackend holds its lock across a
			# full open/write/close per row, so this is strictly less contended.
			self._write_api.write(bucket=self.bucket, record=point, write_precision=WritePrecision.NS)

	def _warn_empty_once(self, device_name: str) -> None:
		"""A device that yields no storable field is invisible in Grafana with no error
		anywhere, so say it once — but only once, this runs per inbound message."""
		if device_name in self._empty_devices:
			self.LOGGER.debug(f"No storable fields for '{device_name}', nothing written")
			return
		self._empty_devices.add(device_name)
		self.LOGGER.warning(f"Device '{device_name}' produced no storable fields, nothing written")

	def _warn_closed_once(self) -> None:
		if self._closed_warned:
			self.LOGGER.debug("Backend is closed, discarding a point")
			return
		self._closed_warned = True
		self.LOGGER.warning("Backend is closed, discarding points from a late writer")

	# --- lifecycle -------------------------------------------------------------

	def _ensure_client(self) -> bool:
		"""Build the client on first use. False means this backend is switched off."""
		# The latch is checked before the cached handle: _ensure_write_api can disable
		# the backend after the client itself was built successfully.
		if self._disabled:
			return False
		if self._client is not None:
			return True
		try:
			self._client = InfluxDBClient(url=self.url, token=self.token, org=self.org, timeout=self.timeout_ms)
		except Exception as e:
			# Latched, not retried: a client that cannot be constructed is a config
			# problem, not a transient one, and retrying would log one ERROR per
			# inbound MQTT message forever.
			self._disabled = True
			self.LOGGER.error(f"InfluxDB client for '{self.url}' could not be created, this backend is disabled for the rest of the run: {e}")
			return False
		self.LOGGER.info(f"InfluxDB client ready: {self.url} (org='{self.org}', bucket='{self.bucket}')")
		return True

	def _ensure_write_api(self) -> bool:
		if self._disabled:
			return False
		if self._write_api is not None:
			return True
		if not self._ensure_client():
			return False
		try:
			self._write_api = self._client.write_api(
				write_options=WriteOptions(
					batch_size=self.batch_size,
					flush_interval=self.flush_interval_ms,
					retry_interval=self.retry_interval_ms,
					max_retries=self.max_retries,
					max_retry_time=self.max_retry_time_ms,
					# Derived, not an option: the budget is only checked between sleeps,
					# so an individual backoff longer than the budget is exactly how much
					# a doomed batch can overshoot it. Capping the two together bounds
					# the post-shutdown linger to roughly max_retry_time_ms.
					max_retry_delay=self.max_retry_time_ms,
					max_close_wait=self.max_close_wait_ms,
				),
				success_callback=self._on_write_success,
				error_callback=self._on_write_error,
				retry_callback=self._on_write_retry,
			)
		except Exception as e:
			self._disabled = True
			self.LOGGER.error(f"InfluxDB write API could not be created, this backend is disabled for the rest of the run: {e}")
			return False
		self.LOGGER.info(f"InfluxDB batching writer armed (batch_size={self.batch_size}, flush_interval={self.flush_interval_ms}ms)")
		return True

	@override
	def close(self) -> None:
		"""Flush whatever is still batched, then drop the connection. Idempotent."""
		with self._lock:
			if self._closed:
				return
			self._closed = True
			write_api, client = self._write_api, self._client
			self._write_api = self._client = None
		# The flush blocks on network I/O for up to max_close_wait_ms, so it happens
		# outside the lock and after the handles are cleared: a straggler writer hits
		# the _closed early-return instead of queueing behind a flush.
		if write_api is not None:
			try:
				write_api.close()
			except Exception as e:
				self.LOGGER.error(f"Failed to flush pending InfluxDB writes: {e}")
		if client is not None:
			try:
				client.close()
			except Exception as e:
				self.LOGGER.error(f"Failed to close the InfluxDB client: {e}")
		self.LOGGER.info("InfluxDB backend closed")

	# --- write callbacks -------------------------------------------------------
	# The batching writer fails asynchronously on its own thread, so these are the
	# only channel through which a failed write reaches the logs.

	def _on_write_success(self, conf: tuple, data: str) -> None:
		if self._write_failing:
			self._write_failing = False
			self.LOGGER.info(f"InfluxDB writes recovered (bucket '{self.bucket}')")
		self.LOGGER.debug(f"InfluxDB batch written: {len(data)} bytes to '{self.bucket}'")

	def _on_write_error(self, conf: tuple, data: str, exception: InfluxDBError) -> None:
		# First failure ERROR, repeats WARNING: the loss is ongoing so it must stay
		# visible, but every batch after the first is the same incident.
		message = f"InfluxDB rejected a batch of {len(data)} bytes for bucket '{self.bucket}', that data is lost: {exception}"
		if self._write_failing:
			self.LOGGER.warning(message)
		else:
			self._write_failing = True
			self.LOGGER.error(message)
			self._explain_field_type_conflict(exception)
		# The only forensic handle for a field-type conflict, which reports as a bare 422.
		self.LOGGER.debug(f"Dropped line protocol (truncated): {data[:self.ERROR_PAYLOAD_CHARS]}")

	def _explain_field_type_conflict(self, exception: InfluxDBError) -> None:
		"""Turn the most common and most cryptic rejection into advice.

		InfluxDB pins a field's type per measurement, so two devices publishing
		different types under one field name collide — `payload` carrying a JSON string
		on one device and a number on another is enough. The server names the field and
		both types, but not what to do about it.
		"""
		if "field type conflict" not in str(exception):
			return
		self.LOGGER.error(
			f"That is a field type conflict: two devices write the same field name with different types into measurement "
			f"'{self.measurement}'. Only the conflicting points are dropped, the rest of the batch lands. Fix it by setting "
			f"'parse_numeric_strings': false (keeps every string a string), by giving the devices separate measurements, or "
			f"by making the device emit a stable type."
		)

	def _on_write_retry(self, conf: tuple, data: str, exception: InfluxDBError) -> None:
		self.LOGGER.warning(f"Retrying an InfluxDB batch of {len(data)} bytes for bucket '{self.bucket}': {exception}")

	# --- reading ---------------------------------------------------------------

	READ_QUERY = """
from(bucket: _bucket)
	|> range(start: _start, stop: _stop)
	|> filter(fn: (r) => r._measurement == _measurement)
	|> filter(fn: (r) => r.device == _device)
	|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
	|> sort(columns: ["_time"])
"""

	@override
	def read(self, device: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
		"""Read stored device data within a time range.

		This is not a lossless round-trip of what write_device_data was given, and it
		differs from CsvFileBackend.read() in three documented ways:
		  - fields come back *flattened* and all-float, exactly as stored, so a P1
		    payload returns {"data.0.obis.medium": 1.0, ...}, not the original nesting;
		  - timestamp is an aware UTC ISO string, where CsvFileBackend returns a naive
		    local one;
		  - Flux's stop bound is exclusive, where CsvFileBackend's end is inclusive.
		"""
		with self._lock:
			if self._closed or not self._ensure_client():
				return []
			client = self._client
		params = {
			"_bucket": self.bucket,
			"_measurement": self.measurement,
			# A bind parameter, never string interpolation: device names come from
			# operator-supplied config and a quote would otherwise break the query.
			"_device": device,
			# Same naive-is-UTC trap as the write path: the client's bind serializer
			# routes datetimes through the same DateHelper.to_utc.
			"_start": start.astimezone(timezone.utc),
			"_stop": end.astimezone(timezone.utc),
		}
		try:
			tables = client.query_api().query(self.READ_QUERY, params=params)
		except Exception as e:
			self.LOGGER.error(f"InfluxDB query for device '{device}' failed: {e}")
			return []
		rows: list[dict[str, Any]] = []
		for table in tables:
			for record in table.records:
				values = dict(record.values)
				moment = values.get("_time")
				rows.append({
					"timestamp": moment.isoformat() if isinstance(moment, datetime) else None,
					"device_name": values.get(self.DEVICE_TAG, device),
					"data": {
						key: value for key, value in values.items()
						# pivot back-fills null into rows that never carried the field
						if key not in self._reserved_columns and value is not None
					},
				})
		return rows
