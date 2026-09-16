import csv
import os
from datetime import datetime
from json import dumps, loads
from threading import Lock
from typing import Any, TextIO, override

from api.device import Device
from api.payload import MAX_DEPTH, finite_or_str
from api.storage_backend import StorageBackend

# The on-disk contract these two files implement, documented in docs/storage-format.md
# and mirrored in examples/MANIFEST.json. MAJOR changes on a header, column, dialect or
# timestamp change, or when data_json/command change meaning; MINOR is additive only.
# It is deliberately never written *into* a CSV — a version row would break every naive
# DictReader and PapaParse consumer, which is exactly the audience it protects. A format
# version is a property of the writer, not of a row.
# tests/test_storage_contract.py keeps the three copies in lockstep.
STORAGE_FORMAT_VERSION = "1.0"


def _finite_safe(value: Any, depth: int = 0) -> Any:
	"""Replace non-finite floats with their str() form: "nan", "inf", "-inf".

	The rule and the depth bound both live in `api/payload.py`, shared with the InfluxDB
	and REST walkers. Past the bound the value is handed back untouched and json.dumps
	decides — see the note there on why that is not a truncation rule.
	"""
	if depth >= MAX_DEPTH:
		return value
	if isinstance(value, float):
		return finite_or_str(value)
	if isinstance(value, dict):
		return {key: _finite_safe(item, depth + 1) for key, item in value.items()}
	if isinstance(value, (list, tuple)):
		return [_finite_safe(item, depth + 1) for item in value]
	return value


def _json_dumps(data: Any) -> str:
	"""Serialise a device payload to JSON that a strict parser will accept.

	json.dumps defaults to allow_nan=True and emits bare NaN / Infinity / -Infinity —
	valid Python, invalid JSON. A consumer then loses the whole row's payload, not the
	one offending field, because the cell no longer parses at all.

	The strict dump is tried first: it is the C fast path and non-finite values are
	rare, so only the failing payload pays for the re-walk.
	"""
	try:
		return dumps(data, allow_nan=False)
	except ValueError:
		return dumps(_finite_safe(data), allow_nan=False)


class CsvFileBackend(StorageBackend):
	"""Appends device data and algorithm decisions to CSV files.

	Thread-safe. Creates the output directory, and each file, on first write.

	One handle is held open per file for the life of the backend rather than reopened
	per row: a backtest at `speed=0` writes one row per device per timestep, and an
	open/stat/write/close cycle under a global lock made that the dominant per-reading
	cost, scaling with rows rather than with runs. Every row is still flushed, so the
	on-disk bytes are identical and a crash loses nothing — what is saved is the syscalls
	around the write, not the durability.
	"""
	DEVICE_DATA_FILE = "device_data.csv"
	DEVICE_DATA_HEADERS = ["timestamp", "device_name", "data_json"]
	ALGORITHM_DECISIONS_FILE = "algorithm_decisions.csv"
	ALGORITHM_DECISIONS_HEADERS = ["timestamp", "algorithm", "device", "command"]

	@override
	def __init__(self, name: str, output_dir: str = "data/storage") -> None:
		super().__init__(name)
		self.output_dir = output_dir
		self._lock = Lock()
		self._writers: dict[str, tuple[TextIO, Any]] = {}

	def _writer(self, filename: str, headers: list[str]) -> Any:
		"""Handle and csv.writer for `filename`, opening and writing the header if needed.

		Caller holds the lock. The header decision is made here, at open time, from the
		size on disk — so a second backend pointed at the same directory appends to a file
		the first one already started without re-emitting its header.
		"""
		existing = self._writers.get(filename)
		if existing is not None:
			return existing[1]
		os.makedirs(self.output_dir, exist_ok=True)
		filepath = os.path.join(self.output_dir, filename)
		write_header = not os.path.exists(filepath) or os.path.getsize(filepath) == 0
		# encoding is explicit, not defaulted: without it Python writes in the process
		# locale encoding, which is cp1252 on Windows — a device named "Compteur
		# électrique" raises UnicodeEncodeError, and an accented payload becomes
		# mojibake for anything reading the file as UTF-8.
		handle = open(filepath, "a", newline="", encoding="utf-8")
		writer = csv.writer(handle)
		if write_header:
			writer.writerow(headers)
		self._writers[filename] = (handle, writer)
		return writer

	def _append_row(self, filename: str, headers: list[str], row: list[str]) -> None:
		with self._lock:
			writer = self._writer(filename, headers)
			writer.writerow(row)
			self._writers[filename][0].flush()

	@override
	def close(self) -> None:
		"""Close every open handle. Idempotent; a write afterwards simply reopens.

		StorageManager.close_all() calls this from main's `finally`, and calls it at most
		once itself — but a backend used directly (a test, a script) may be closed twice,
		and reopening is the honest behaviour for an append-only file.
		"""
		with self._lock:
			for handle, _ in self._writers.values():
				try:
					handle.close()
				except OSError as e:
					self.LOGGER.warning(f"Failed to close {handle.name}: {e}")
			self._writers.clear()

	@override
	def write_device_data(self, device: Device, data: dict[str, Any]) -> None:
		self._append_row(
			self.DEVICE_DATA_FILE,
			self.DEVICE_DATA_HEADERS,
			[self._data_timestamp().isoformat(), device.name, _json_dumps(data)],
		)

	@override
	def write_algorithm_decision(self, algorithm: str, device: str, command: str) -> None:
		self._append_row(
			self.ALGORITHM_DECISIONS_FILE,
			self.ALGORITHM_DECISIONS_HEADERS,
			[self._decision_timestamp().isoformat(), algorithm, device, command],
		)

	@override
	def read(self, device: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
		filepath = os.path.join(self.output_dir, self.DEVICE_DATA_FILE)
		if not os.path.exists(filepath):
			return []
		results: list[dict[str, Any]] = []
		with open(filepath, newline="", encoding="utf-8") as f:
			reader = csv.DictReader(f)
			for row in reader:
				if row["device_name"] != device:
					continue
				try:
					ts = datetime.fromisoformat(row["timestamp"])
				except ValueError:
					continue
				if start <= ts <= end:
					try:
						data = loads(row["data_json"])
					except Exception:
						data = row["data_json"]
					results.append({"timestamp": ts.isoformat(), "device_name": row["device_name"], "data": data})
		return results
