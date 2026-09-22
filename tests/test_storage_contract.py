"""The EMS↔Viewer interface, pinned.

**A failure in this file is a cross-repo breaking change, not a broken test.** Read
`docs/storage-format.md` before editing anything here — an external app in another
language parses the two files this asserts on, and the fixture under `examples/` is
vendored into that repository.

Deliberately separate from `tests/test_storage_csv.py`, which unit-tests the backend.
That file reads everything through `csv.DictReader`, which is order-insensitive and
newline-normalising, so it is structurally blind to a changed line terminator, a changed
quoting rule, a changed encoding, a reordered column, a column inserted in the middle —
and it never asserts anything at all about the `timestamp` column. This file is the one
that notices.
"""

import csv
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from simulation.clock import SimulationClock
from storage.csv_file import STORAGE_FORMAT_VERSION, CsvFileBackend
from tests.conftest import StubDevice

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
FIXTURE = EXAMPLES / "auto_toggle"
EXPECTED = FIXTURE / "expected"
EDGE_CASES = EXAMPLES / "edge_cases"
FORMAT_DOC = REPO_ROOT / "docs" / "storage-format.md"

DEVICE_DATA_HEADER = b"timestamp,device_name,data_json\r\n"
DECISIONS_HEADER = b"timestamp,algorithm,device,command\r\n"

# What examples/generate.py builds. Kept here as literals rather than imported so that a
# change to the generator's own numbers has to be made twice, on purpose.
EXPECTED_READINGS = 54
EXPECTED_DECISIONS = 18

DRIFT_MESSAGE = (
	"The storage output format changed. If that was intentional: bump "
	"STORAGE_FORMAT_VERSION in storage/csv_file.py, run `python examples/generate.py`, "
	"update docs/storage-format.md, and open an issue on Motrix Edge View to re-vendor its "
	"fixture. See docs/storage-format.md."
)


@pytest.fixture
def backend(tmp_path):
	return CsvFileBackend("contract", output_dir=str(tmp_path / "storage"))


def read_rows(path):
	with open(path, newline="", encoding="utf-8") as f:
		return list(csv.DictReader(f))


class TestHeaderAndDialect:
	"""Byte-level, because that is the level a consumer in another language meets."""

	def test_header_constants(self):
		assert CsvFileBackend.DEVICE_DATA_HEADERS == ["timestamp", "device_name", "data_json"]
		assert CsvFileBackend.ALGORITHM_DECISIONS_HEADERS == ["timestamp", "algorithm", "device", "command"]

	def test_filenames(self):
		assert CsvFileBackend.DEVICE_DATA_FILE == "device_data.csv"
		assert CsvFileBackend.ALGORITHM_DECISIONS_FILE == "algorithm_decisions.csv"

	def test_device_data_header_bytes_including_crlf(self, backend, tmp_path):
		"""Read in binary. In text mode Python normalises the terminator and a \\r\\n ->
		\\n regression reads as green."""
		device = StubDevice(name="d1")
		device.data = {"power": 1}
		backend.write_device_data(device, device.data)

		raw = (tmp_path / "storage" / "device_data.csv").read_bytes()
		assert raw.startswith(DEVICE_DATA_HEADER)

	def test_decisions_header_bytes_including_crlf(self, backend, tmp_path):
		backend.write_algorithm_decision("AutoToggle", "plug", "on")
		raw = (tmp_path / "storage" / "algorithm_decisions.csv").read_bytes()
		assert raw.startswith(DECISIONS_HEADER)

	def test_every_row_ends_crlf_and_never_bare_lf(self, backend, tmp_path):
		for i in range(3):
			backend.write_algorithm_decision("AutoToggle", "plug", f"cmd{i}")
		raw = (tmp_path / "storage" / "algorithm_decisions.csv").read_bytes()
		assert raw.count(b"\r\n") == 4  # header + 3 rows
		assert raw.count(b"\n") == raw.count(b"\r\n")  # no bare LF anywhere

	def test_no_bom(self, backend, tmp_path):
		backend.write_algorithm_decision("AutoToggle", "plug", "on")
		raw = (tmp_path / "storage" / "algorithm_decisions.csv").read_bytes()
		assert not raw.startswith(b"\xef\xbb\xbf")

	def test_comma_quote_and_newline_round_trip(self, backend, tmp_path):
		"""QUOTE_MINIMAL with doubled quotes, and a quoted field may span physical lines."""
		nasty = 'set 42, then "wait"\r\nand stop'
		backend.write_algorithm_decision('Algo, with comma', 'device "quoted"', nasty)

		rows = read_rows(tmp_path / "storage" / "algorithm_decisions.csv")
		assert len(rows) == 1
		assert rows[0]["algorithm"] == "Algo, with comma"
		assert rows[0]["device"] == 'device "quoted"'
		assert rows[0]["command"] == nasty

	def test_utf8_encoding_is_explicit(self, backend, tmp_path):
		"""Without an explicit encoding= this writes in the process locale — cp1252 on
		Windows, where a name like this raises UnicodeEncodeError outright."""
		device = StubDevice(name="Compteur électrique")
		device.data = {"tension": "230,4 V — nominale"}
		backend.write_device_data(device, device.data)

		rows = read_rows(tmp_path / "storage" / "device_data.csv")
		assert rows[0]["device_name"] == "Compteur électrique"
		assert json.loads(rows[0]["data_json"])["tension"] == "230,4 V — nominale"


class TestDataJson:
	def test_non_finite_floats_are_valid_json(self, backend, tmp_path):
		"""json.dumps defaults to allow_nan=True and emits bare NaN / Infinity, which is
		invalid JSON — and costs a consumer the whole row's payload, not the one field."""
		device = StubDevice(name="d1")
		device.data = {"power": float("nan"), "energy": float("inf"),
					   "drift": float("-inf"), "ok": 1.5}
		backend.write_device_data(device, device.data)

		cell = read_rows(tmp_path / "storage" / "device_data.csv")[0]["data_json"]
		assert "NaN" not in cell and "Infinity" not in cell
		# Same encoding services/rest_api.py:_jsonable emits, so both surfaces agree.
		assert json.loads(cell) == {"power": "nan", "energy": "inf", "drift": "-inf", "ok": 1.5}

	def test_nested_non_finite_is_repaired_too(self, backend, tmp_path):
		device = StubDevice(name="d1")
		device.data = {"phases": [{"v": float("nan")}, {"v": 230.1}]}
		backend.write_device_data(device, device.data)

		cell = read_rows(tmp_path / "storage" / "device_data.csv")[0]["data_json"]
		assert json.loads(cell) == {"phases": [{"v": "nan"}, {"v": 230.1}]}

	def test_ordinary_payloads_are_untouched(self, backend, tmp_path):
		device = StubDevice(name="d1")
		device.data = {"b": 2, "a": {"nested": [1, 2, 3]}}
		backend.write_device_data(device, device.data)

		cell = read_rows(tmp_path / "storage" / "device_data.csv")[0]["data_json"]
		assert json.loads(cell) == {"b": 2, "a": {"nested": [1, 2, 3]}}
		assert cell == '{"b": 2, "a": {"nested": [1, 2, 3]}}'  # key order preserved, no sorting


class TestTimestampFormat:
	"""The column tests/test_storage_csv.py has never once looked at."""

	def test_naive_event_time_is_written_verbatim_and_stays_naive(self, backend, tmp_path):
		moment = datetime(2024, 1, 15, 10, 0, 0)
		clock = SimulationClock()
		clock.publish_step(moment)
		clock.set_event_time(moment)

		device = StubDevice(name="d1")
		backend.write_device_data(device, {})

		cell = read_rows(tmp_path / "storage" / "device_data.csv")[0]["timestamp"]
		assert cell == moment.isoformat() == "2024-01-15T10:00:00"
		assert datetime.fromisoformat(cell).tzinfo is None

	def test_offset_aware_event_time_keeps_its_offset(self, backend, tmp_path):
		moment = datetime(2024, 3, 31, 3, 30, tzinfo=timezone(timedelta(hours=2)))
		clock = SimulationClock()
		clock.publish_step(moment)
		clock.set_event_time(moment)

		backend.write_device_data(StubDevice(name="d1"), {})

		cell = read_rows(tmp_path / "storage" / "device_data.csv")[0]["timestamp"]
		assert cell == "2024-03-31T03:30:00+02:00"
		assert datetime.fromisoformat(cell).utcoffset() == timedelta(hours=2)

	def test_without_a_clock_the_stamp_is_naive(self, backend, tmp_path):
		"""Shape, never the value — this one is the wall clock."""
		backend.write_device_data(StubDevice(name="d1"), {})

		cell = read_rows(tmp_path / "storage" / "device_data.csv")[0]["timestamp"]
		assert datetime.fromisoformat(cell).tzinfo is None
		assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?", cell)

	def test_microseconds_appear_only_when_non_zero(self, backend, tmp_path):
		"""isoformat() is not fixed-width. A consumer parsing a fixed offset breaks here."""
		clock = SimulationClock()
		clock.publish_step(datetime(2024, 1, 15, 10, 0, 0))
		clock.set_event_time(datetime(2024, 1, 15, 10, 0, 0))
		backend.write_device_data(StubDevice(name="d1"), {})
		clock.set_event_time(datetime(2024, 1, 15, 10, 0, 0, 284157))
		backend.write_device_data(StubDevice(name="d2"), {})

		rows = read_rows(tmp_path / "storage" / "device_data.csv")
		assert rows[0]["timestamp"] == "2024-01-15T10:00:00"
		assert rows[1]["timestamp"] == "2024-01-15T10:00:00.284157"

	def test_a_decision_carries_the_step_it_was_computed_in(self, backend, tmp_path):
		"""Not the dispatch clock: a decision computed while processing T belongs to T
		even once the replay has moved on. Pinned here at the *file* level; only
		test_replay_determinism.py checks it at the API level."""
		clock = SimulationClock()
		clock.publish_step(datetime(2024, 1, 15, 10, 0, 0))
		clock.set_event_time(datetime(2024, 1, 15, 10, 15, 0))  # dispatch has moved on

		backend.write_algorithm_decision("AutoToggle", "plug", "on")

		cell = read_rows(tmp_path / "storage" / "algorithm_decisions.csv")[0]["timestamp"]
		assert cell == "2024-01-15T10:00:00"

	def test_one_file_holds_both_timestamp_shapes(self, backend, tmp_path):
		"""The property that makes this column awkward, asserted as *intended* rather than
		tolerated: a replay's aware stamps, then a wall-clock naive one after reset()."""
		clock = SimulationClock()
		clock.publish_step(datetime(2024, 3, 31, 3, 30, tzinfo=timezone(timedelta(hours=2))))
		backend.write_algorithm_decision("AutoToggle", "plug", "on")
		clock.reset()
		backend.write_algorithm_decision("AutoToggle", "plug", "off")

		rows = read_rows(tmp_path / "storage" / "algorithm_decisions.csv")
		shapes = [datetime.fromisoformat(r["timestamp"]).tzinfo is not None for r in rows]
		assert shapes == [True, False]


class TestLazyHeaderAndAppend:
	def test_two_backends_one_directory_one_header(self, tmp_path):
		out = str(tmp_path / "storage")
		CsvFileBackend("a", output_dir=out).write_algorithm_decision("A", "d", "on")
		CsvFileBackend("b", output_dir=out).write_algorithm_decision("B", "d", "off")

		raw = (tmp_path / "storage" / "algorithm_decisions.csv").read_bytes()
		assert raw.count(DECISIONS_HEADER) == 1
		assert len(read_rows(tmp_path / "storage" / "algorithm_decisions.csv")) == 2

	def test_a_pre_existing_empty_file_gets_a_header(self, tmp_path):
		out = tmp_path / "storage"
		out.mkdir()
		(out / "algorithm_decisions.csv").write_bytes(b"")

		CsvFileBackend("a", output_dir=str(out)).write_algorithm_decision("A", "d", "on")

		assert (out / "algorithm_decisions.csv").read_bytes().startswith(DECISIONS_HEADER)

	def test_a_pre_existing_non_empty_file_does_not(self, tmp_path):
		"""The append-across-runs case: restart the EMS and the second run continues the
		first file rather than re-heading it."""
		out = tmp_path / "storage"
		out.mkdir()
		(out / "algorithm_decisions.csv").write_bytes(DECISIONS_HEADER + b"2024-01-01T00:00:00,A,d,on\r\n")

		CsvFileBackend("a", output_dir=str(out)).write_algorithm_decision("B", "d", "off")

		assert (out / "algorithm_decisions.csv").read_bytes().count(DECISIONS_HEADER) == 1

	def test_zero_writes_means_no_file_at_all(self, tmp_path):
		"""A consumer must read *missing* as "no data", never as an error."""
		out = tmp_path / "storage"
		CsvFileBackend("a", output_dir=str(out)).write_device_data(StubDevice(name="d"), {})

		assert (out / "device_data.csv").exists()
		assert not (out / "algorithm_decisions.csv").exists()


class TestGoldenFixture:
	"""examples/auto_toggle/expected/ is the vendored interface. These assertions are what
	turn "the format moved" into a CI failure instead of a viewer that stopped parsing."""

	def test_the_fixture_exists(self):
		for name in ("device_data.csv", "algorithm_decisions.csv", "control.log"):
			assert (EXPECTED / name).is_file(), f"missing {name}; run `python examples/generate.py`"
			assert (EXPECTED / name).stat().st_size > 0

	def test_manifest_checksums_match(self):
		manifest = json.loads((EXAMPLES / "MANIFEST.json").read_text(encoding="utf-8"))
		for name, digest in manifest["files"].items():
			actual = hashlib.sha256((EXAMPLES / name).read_bytes()).hexdigest()
			assert actual == digest, f"{name} does not match MANIFEST.json. {DRIFT_MESSAGE}"

	def test_manifest_terminator_is_lf_on_every_platform(self):
		"""Nothing else compares these bytes. check()'s drift list covers TRACKED, which
		excludes the manifest, and the test above parses it as JSON — universal newlines
		hide the terminator from both, which is how an os.linesep one went unnoticed.
		examples/** is -text, so git stores whatever was written, and a platform flip is a
		diff on every line of a file whose checksums did not move."""
		raw = (EXAMPLES / "MANIFEST.json").read_bytes()
		assert b"\r\n" not in raw, (
			"MANIFEST.json must use LF line terminators on every platform; "
			"write_manifest() in examples/generate.py passes newline='\\n' for this"
		)

	def test_version_lockstep(self):
		manifest = json.loads((EXAMPLES / "MANIFEST.json").read_text(encoding="utf-8"))
		assert manifest["storage_format_version"] == STORAGE_FORMAT_VERSION

		doc = FORMAT_DOC.read_text(encoding="utf-8")
		assert f"**Storage format version: `{STORAGE_FORMAT_VERSION}`**" in doc, (
			f"docs/storage-format.md does not declare version {STORAGE_FORMAT_VERSION}"
		)

	@pytest.mark.slow
	def test_regenerating_produces_identical_bytes(self):
		"""The whole point: run the real main.py twice and diff the result against what is
		committed. `--check` builds into a temp directory, so this never touches the
		fixture — never invoke generate.py without it from a test.

		The suite's first subprocess test. sys.executable honours the venv; cwd is the
		repo root because the config's paths and the plugin imports both resolve there."""
		result = subprocess.run(
			[sys.executable, str(EXAMPLES / "generate.py"), "--check"],
			cwd=REPO_ROOT, capture_output=True, text=True,
		)
		assert result.returncode == 0, (result.stderr or result.stdout) + "\n" + DRIFT_MESSAGE

	def test_row_counts(self):
		assert len(read_rows(EXPECTED / "device_data.csv")) == EXPECTED_READINGS
		assert len(read_rows(EXPECTED / "algorithm_decisions.csv")) == EXPECTED_DECISIONS

	def test_every_payload_is_strictly_parseable_json(self):
		"""The generated tier carries no corrupt row — that is what makes it a statement
		about what the EMS emits. Adversarial content lives in examples/edge_cases/."""
		for row in read_rows(EXPECTED / "device_data.csv"):
			json.loads(row["data_json"])
			# json.loads *accepts* bare NaN/Infinity by default, so parsing successfully
			# proves nothing about a stricter consumer. Assert on the bytes instead.
			assert "NaN" not in row["data_json"]
			assert "Infinity" not in row["data_json"]

	def test_commands_are_bare_strings_not_json(self):
		"""AutoToggle emits `on`/`off`. Code that assumes JSON.parse(command) breaks on
		the project's own public worked example."""
		commands = {row["command"] for row in read_rows(EXPECTED / "algorithm_decisions.csv")}
		assert commands == {"on", "off"}

	def test_control_log_is_not_csv(self):
		"""Pinned so it cannot quietly become CSV and lull a consumer into parsing it."""
		raw = (EXPECTED / "control.log").read_text(encoding="utf-8")
		lines = raw.splitlines()
		assert lines, "control.log is empty"
		assert not lines[0].startswith("timestamp,"), "control.log must have no header"
		for line in lines:
			assert len(line.split(",")) >= 3

	# --- the edge-case roster, one named assertion each, so a regenerated fixture that
	# --- quietly lost a case fails with that case named rather than as a count mismatch.

	def test_roster_both_timestamp_shapes(self):
		stamps = [r["timestamp"] for r in read_rows(EXPECTED / "device_data.csv")]
		assert any(datetime.fromisoformat(s).tzinfo is None for s in stamps)
		assert any(datetime.fromisoformat(s).tzinfo is not None for s in stamps)

	def test_roster_dst_transition(self):
		offsets = {
			datetime.fromisoformat(r["timestamp"]).utcoffset()
			for r in read_rows(EXPECTED / "device_data.csv")
			if datetime.fromisoformat(r["timestamp"]).tzinfo is not None
		}
		assert offsets == {timedelta(hours=1), timedelta(hours=2)}

	def test_roster_nested_payload_through_an_array(self):
		row = next(r for r in read_rows(EXPECTED / "device_data.csv") if r["device_name"] == "p1_meter")
		data = json.loads(row["data_json"])
		assert isinstance(data["data"], list)
		assert isinstance(data["data"][0]["data"][0]["value"], float)

	def test_roster_string_typed_number(self):
		row = next(r for r in read_rows(EXPECTED / "device_data.csv") if r["device_name"] == "shelly_plug")
		assert isinstance(json.loads(row["data_json"])["power"], str)

	def test_roster_double_encoded_payload(self):
		row = next(r for r in read_rows(EXPECTED / "device_data.csv") if r["device_name"] == "pseudo_sensor")
		data = json.loads(row["data_json"])
		assert isinstance(data["payload"], str)
		assert json.loads(data["payload"]) == data["parsed"]

	def test_roster_a_non_json_payload_is_stored_with_parsed_null(self):
		rows = [r for r in read_rows(EXPECTED / "device_data.csv") if r["device_name"] == "pseudo_sensor"]
		assert any(json.loads(r["data_json"])["parsed"] is None for r in rows)

	def test_roster_a_field_disappears_for_the_same_device(self):
		rows = [r for r in read_rows(EXPECTED / "device_data.csv") if r["device_name"] == "p1_meter"]
		codes = [
			{
				f"{e['obis']['medium']}-{e['obis']['channel']}:{e['obis']['class']}."
				f"{e['obis']['instance']}.{e['obis']['attribute']}"
				for e in json.loads(r["data_json"])["data"]
			}
			for r in rows
		]
		assert any("0-1:24.2.3" in c for c in codes)
		assert any("0-1:24.2.3" not in c for c in codes)

	def test_roster_an_off_to_on_transition(self):
		commands = [r["command"] for r in read_rows(EXPECTED / "algorithm_decisions.csv")]
		assert "off" in commands and "on" in commands
		assert commands.index("off") < commands.index("on")

	def test_roster_quoted_fields_with_embedded_crlf(self):
		"""P1 telegrams are CRLF-delimited, so the data_json column really does contain
		quoted fields — the replay input side of the same property."""
		raw = (FIXTURE / "replay.naive.csv").read_bytes()
		assert b'"' in raw
		assert raw.count(b"\r\n") > EXPECTED_READINGS  # more line breaks than rows


class TestAdversarialFixture:
	"""examples/edge_cases/ — what a file can contain, as opposed to what the EMS emits."""

	def test_every_file_keeps_a_valid_dialect(self):
		"""Content may be broken; the dialect must not be, or a consumer cannot even reach
		the broken row to skip it. The one deliberate exception is isolated in its own
		file."""
		for path in sorted(EDGE_CASES.glob("*.csv")):
			if path.name == "device_data.unterminated_quote.csv":
				continue
			with open(path, newline="", encoding="utf-8-sig") as f:
				list(csv.reader(f))  # must not raise

	def test_documented_row_counts(self):
		"""README.md documents every row; an undocumented one is a failure. The counts are
		asserted rather than the prose, but the prose is what makes them meaningful."""
		readme = (EDGE_CASES / "README.md").read_text(encoding="utf-8")
		assert "18 data rows" in readme
		assert "9 data rows" in readme

		with open(EDGE_CASES / "device_data.csv", newline="", encoding="utf-8") as f:
			assert len(list(csv.reader(f))) - 1 == 18
		with open(EDGE_CASES / "algorithm_decisions.csv", newline="", encoding="utf-8") as f:
			assert len(list(csv.reader(f))) - 1 == 9

	def test_ragged_rows_are_present(self):
		with open(EDGE_CASES / "device_data.csv", newline="", encoding="utf-8") as f:
			widths = {len(row) for row in csv.reader(f)}
		assert widths == {2, 3, 5}, "expected a short row and a long row beside the normal ones"

	def test_the_empty_and_headers_only_files(self):
		assert (EDGE_CASES / "device_data.empty.csv").read_bytes() == b""
		assert (EDGE_CASES / "device_data.headers_only.csv").read_bytes() == DEVICE_DATA_HEADER

	def test_the_bom_file_really_starts_with_a_bom(self):
		assert (EDGE_CASES / "device_data.bom.csv").read_bytes().startswith(b"\xef\xbb\xbf")

	def test_the_unterminated_quote_swallows_rather_than_raises(self):
		"""A field opened with `"` and never closed leaves the parser inside a quoted field
		at the line break. csv.reader does not fail — it silently eats every following line
		into that one field. Two physical rows come back as one, which is exactly why this
		lives in its own file: mixed in, it would hide every row below it."""
		with open(EDGE_CASES / "device_data.unterminated_quote.csv", newline="", encoding="utf-8") as f:
			rows = list(csv.reader(f))
		assert len(rows) == 2  # header + one row that ate the second
		assert "eaten_by_the_row_above" in rows[1][-1]
