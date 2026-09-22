#!/usr/bin/env python3
"""Regenerate the EMS↔Viewer golden fixture under examples/auto_toggle/.

    python examples/generate.py            # rewrite the fixture in place
    python examples/generate.py --check    # rebuild into a temp dir and diff; exit 1 on drift

What this produces is not a hand-written sample. It is the **actual bytes** the EMS
writes, obtained by running `main.py` twice over two committed replay files, so a change
to `storage/csv_file.py` shows up here as a diff rather than as a viewer that quietly
stops parsing. `tests/test_storage_contract.py` runs the same thing and compares.

Two runs, not one, and that is the point of the whole design:

* `replay.naive.csv` carries naive timestamps, `replay.offset.csv` carries offset-aware
  ones across a DST transition. Appending both to one output directory reproduces the
  single most awkward property of the real format — one `timestamp` column holding two
  different time domains — which is exactly what happens when an operator restarts the
  EMS, and what `data/storage/device_data.csv` in this repo already looks like.
* They cannot be one file: `PseudoConnector` sorts entries by timestamp, and comparing a
  naive datetime with an aware one is a `TypeError`. The connector now normalises the
  sort key rather than crashing, but a replay that mixes the two is still a warning and
  not something a fixture should model as normal input.
* Each run must be its own **process**: `DevicesManager` and `SimulationClock` are
  singletons, so two replays in one interpreter would leak state across the boundary.

The algorithm is `AutoToggle` — capability-based, seventeen lines, and part of this repo.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXAMPLES_DIR.parent
sys.path.insert(0, str(REPO_ROOT))  # so `parsers` resolves when run as a script

from parsers.obis import crc16_arc  # noqa: E402
from storage.csv_file import CsvFileBackend, STORAGE_FORMAT_VERSION  # noqa: E402

AUTO_TOGGLE_DIR = EXAMPLES_DIR / "auto_toggle"
CONFIG = AUTO_TOGGLE_DIR / "config.json"
MANIFEST = EXAMPLES_DIR / "MANIFEST.json"

REPLAY_HEADERS = ["timestamp", "device_name", "topic", "payload"]

# Files the manifest covers, relative to examples/. Order is the manifest's order.
TRACKED = [
	"auto_toggle/replay.naive.csv",
	"auto_toggle/replay.offset.csv",
	"auto_toggle/expected/device_data.csv",
	"auto_toggle/expected/algorithm_decisions.csv",
	"auto_toggle/expected/control.log",
]

# --- the scenario -------------------------------------------------------------------
# Twelve naive timesteps then six offset-aware ones. The meter's tariff-1 register
# crosses AutoToggle's 500 kWh threshold at naive step 7, so the decision stream carries
# a visible off -> on transition rather than eighteen identical rows.

NAIVE_START = datetime(2024, 1, 15, 10, 0, 0)
NAIVE_STEP = timedelta(minutes=15)
NAIVE_COUNT = 12

# Europe/Brussels spring-forward: 02:00 CET does not exist, so the offsets change
# mid-file and local clock time jumps while UTC stays monotonic. A consumer that parses
# the offset gets this right; one that ignores it sees the series leap an hour.
OFFSET_STAMPS = [
	"2024-03-31T01:00:00+01:00",
	"2024-03-31T01:30:00+01:00",
	"2024-03-31T03:00:00+02:00",
	"2024-03-31T03:30:00+02:00",
	"2024-03-31T04:00:00+02:00",
	"2024-03-31T04:30:00+02:00",
]

ENERGY_T1_NAIVE = [480.0, 483.0, 486.5, 490.0, 493.5, 497.0, 501.0, 505.5, 510.0, 514.5, 519.0, 523.5]
ENERGY_T1_OFFSET = [527.0, 530.5, 534.0, 537.5, 541.0, 544.5]

POWER_W_NAIVE = [118.4, 121.9, 117.2, 130.6, 126.1, 119.8, 142.3, 155.0, 149.7, 138.2, 133.9, 128.5]
POWER_W_OFFSET = [124.6, 131.1, 127.8, 119.4, 145.2, 151.7]

ZONE_TEMP_NAIVE = [15.2, 15.8, 16.3, 16.9, 17.4, 17.1, 18.0, 18.6, 19.1, 19.5, 19.9, 20.2]
ZONE_TEMP_OFFSET = [12.4, 12.1, 11.8, 12.6, 13.3, 14.0]


def p1_telegram(energy_t1: float, energy_t2: float, power_kw: float,
				voltage_v: float, gas_m3: float | None, gas_stamp: str) -> str:
	"""A DSMR 5 telegram with a valid CRC16-ARC over the standard range.

	`gas_m3` is None for the later timesteps, which is how the fixture exercises a field
	that is present in earlier rows and absent from later ones for the same device —
	`P1.receive_mqtt` replaces `self.data` wholesale rather than merging into it.

	The gas register carries two adjacent value blocks, `(capture timestamp)(reading)`.
	The timestamp block is deliberately non-numeric so `P1.get_metrics()` drops it and
	`gas_m3` means the gas reading, which is the behaviour a consumer depends on.
	"""
	lines = [
		f"1-0:1.8.1({energy_t1:010.3f}*kWh)",
		f"1-0:1.8.2({energy_t2:010.3f}*kWh)",
		f"1-0:1.7.0({power_kw:06.3f}*kW)",
		f"1-0:32.7.0({voltage_v:05.1f}*V)",
	]
	if gas_m3 is not None:
		lines.append(f"0-1:24.2.3({gas_stamp})({gas_m3:09.3f}*m3)")
	body = "/ISK5\\2M550E-1012\r\n\r\n" + "".join(f"{line}\r\n" for line in lines) + "!"
	return f"{body}{crc16_arc(body.encode()):04X}\r\n"


def steps(stamps: list[str], energies: list[float], powers: list[float],
		  temps: list[float], *, naive: bool) -> list[tuple[str, str, str, str]]:
	"""One replay's rows: three devices per timestep, in a fixed order.

	Rows sharing a timestamp form one timestep — that is how `PseudoConnector` decides
	when to commit the step clock and hand the algorithms their turn.
	"""
	rows: list[tuple[str, str, str, str]] = []
	for i, stamp in enumerate(stamps):
		# --- the meter: nested payload, positional OBIS arrays, a disappearing register
		gas = 123.456 + i * 0.117 if (naive and i < 6) else None
		rows.append((
			stamp, "p1_meter", "p1/data",
			p1_telegram(
				energy_t1=energies[i],
				energy_t2=120.5 + i * 0.25,
				power_kw=powers[i] / 1000,
				voltage_v=229.4 + (i % 5) * 0.3,
				gas_m3=gas,
				gas_stamp="240115100000W",
			),
		))

		# --- the plug: string-typed numbers, and a field that appears mid-file.
		# It reports the relay's own state at two timesteps instead of its power, which
		# leaves `power` un-updated for that step: the value is *held*, not missing, and
		# distinguishing the two is the viewer's job.
		if naive and i == 2:
			rows.append((stamp, "shelly_plug", "shellies/plug-s/relay/0", "off"))
		elif naive and i == 7:
			rows.append((stamp, "shelly_plug", "shellies/plug-s/relay/0", "on"))
		elif not naive and i == 2:
			rows.append((stamp, "shelly_plug", "shellies/plug-s/temperature", "41.2"))
		else:
			rows.append((stamp, "shelly_plug", "shellies/plug-s/relay/0/power", f"{powers[i]:.1f}"))

		# --- the generic device: double-encoded payload. `payload` stays a JSON *string*
		# and `parsed` is the decoded object, so a consumer that charts `payload` finds
		# nothing and one that charts `parsed.value` finds the reading. One timestep sends
		# a non-JSON payload, which is accepted and stored with `parsed: null`.
		if naive and i == 5:
			payload = "sensor offline"
		else:
			payload = json.dumps({"value": temps[i], "humidity": 48.0 + i * 0.5})
		rows.append((stamp, "pseudo_sensor", "sensors/zone/temperature", payload))
	return rows


def write_replay(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	# newline="" hands line-ending control to csv, which writes CRLF on every platform —
	# the same dialect CsvFileBackend produces, so input and output agree.
	with open(path, "w", newline="", encoding="utf-8") as f:
		writer = csv.writer(f)
		writer.writerow(REPLAY_HEADERS)
		writer.writerows(rows)


def write_replays(target: Path) -> None:
	naive_stamps = [(NAIVE_START + i * NAIVE_STEP).isoformat() for i in range(NAIVE_COUNT)]
	write_replay(
		target / "replay.naive.csv",
		steps(naive_stamps, ENERGY_T1_NAIVE, POWER_W_NAIVE, ZONE_TEMP_NAIVE, naive=True),
	)
	write_replay(
		target / "replay.offset.csv",
		steps(OFFSET_STAMPS, ENERGY_T1_OFFSET, POWER_W_OFFSET, ZONE_TEMP_OFFSET, naive=False),
	)


def run_replay(replay_file: Path, storage_dir: Path, control_log: Path) -> None:
	"""One `python main.py` process against one replay file.

	sys.executable rather than "python" so a venv is honoured, and cwd=REPO_ROOT because
	the config's own paths and the plugin imports are both resolved from there.
	"""
	env = {
		**os.environ,
		"EMS_REPLAY_FILE": str(replay_file),
		"EMS_STORAGE_DIR": str(storage_dir),
		"EMS_CONTROL_LOG": str(control_log),
	}
	result = subprocess.run(
		[sys.executable, "main.py", "--config", str(CONFIG)],
		cwd=REPO_ROOT, env=env, capture_output=True, text=True,
	)
	if result.returncode != 0:
		sys.stderr.write(result.stdout)
		sys.stderr.write(result.stderr)
		raise SystemExit(f"main.py failed on {replay_file.name} (exit {result.returncode})")


def generate(target: Path) -> None:
	"""Rebuild replays and expected output under `target` (an auto_toggle directory)."""
	expected = target / "expected"
	# Removed rather than truncated: CsvFileBackend writes its header only when the file
	# is absent or empty, so a stale file would silently produce a headerless append.
	shutil.rmtree(expected, ignore_errors=True)
	expected.mkdir(parents=True, exist_ok=True)

	write_replays(target)
	control_log = expected / "control.log"
	# Run 1 creates the files; run 2 appends. One header, two timestamp domains.
	run_replay(target / "replay.naive.csv", expected, control_log)
	run_replay(target / "replay.offset.csv", expected, control_log)

	verify(target)


def verify(target: Path) -> None:
	"""Cheap sanity gates, so a broken fixture fails here and not three steps later."""
	expected = target / "expected"
	# Read each file once, then both check the bytes and parse them from memory. These are
	# small, but verify() runs inside check() on every CI invocation.
	readings = (expected / "device_data.csv").read_bytes()
	decisions = (expected / "algorithm_decisions.csv").read_bytes()

	expected_readings = (NAIVE_COUNT + len(OFFSET_STAMPS)) * 3
	expected_decisions = NAIVE_COUNT + len(OFFSET_STAMPS)
	# One header each; a logical row may span physical lines, so parse rather than count.
	# newline="" is what open() would have given us, so decode and let csv see the CRLFs.
	rows = list(csv.DictReader(io.StringIO(readings.decode("utf-8"), newline="")))
	if len(rows) != expected_readings:
		raise SystemExit(f"expected {expected_readings} reading rows, got {len(rows)}")
	decision_rows = list(csv.DictReader(io.StringIO(decisions.decode("utf-8"), newline="")))
	if len(decision_rows) != expected_decisions:
		raise SystemExit(f"expected {expected_decisions} decision rows, got {len(decision_rows)}")

	commands = [row["command"] for row in decision_rows]
	if "off" not in commands or "on" not in commands:
		raise SystemExit(f"expected an off->on transition in the decisions, got {set(commands)}")
	if readings.count(b"\r\n") == 0 or decisions.count(b"\r\n") == 0:
		raise SystemExit("expected CRLF line terminators")

	# The one file under expected/ that is not CSV, and whose terminator used to be
	# os.linesep. LF on every platform, or check()'s byte comparison below decides
	# drift by which machine ran it.
	if b"\r\n" in (expected / "control.log").read_bytes():
		raise SystemExit("control.log must use LF line terminators on every platform")


# --- the adversarial tier -----------------------------------------------------------
# Authored, not observed. The EMS cannot produce most of these — but a `docker kill`
# mid-row, a truncating disk, an Excel round-trip, a hand-edit or two files concatenated
# from different EMS versions all can, and a viewer meets them on a real site. Keeping
# them out of auto_toggle/expected/ is the point: a "golden" file containing a
# deliberately broken row stops being a statement about what the EMS emits.
#
# Rewriting these is a deliberate act, so it is not part of the default run and not part
# of --check: `python examples/generate.py --edge-cases`. Every row is documented, line
# for line, in examples/edge_cases/README.md, and the contract test asserts the counts
# match so an undocumented row fails.

EDGE_CASE_DIR = EXAMPLES_DIR / "edge_cases"

EDGE_READINGS = [
	("2024-02-01T08:00:00", "ok_device", '{"power": 12.5}'),
	("2024-02-01T08:00:05", "truncated_json", '{"power": 12.5'),
	("2024-02-01T08:00:10", "not_json", "not json at all"),
	("2024-02-01T08:00:15", "nonfinite", '{"power": NaN, "energy": Infinity, "drift": -Infinity}'),
	("", "empty_timestamp", '{"power": 1}'),
	("not-a-timestamp", "bad_timestamp", '{"power": 2}'),
	("2024-02-01T07:59:00", "out_of_order", '{"power": 3}'),
	("2024-02-01T08:00:20", "duplicate_key", '{"power": 4}'),
	("2024-02-01T08:00:20", "duplicate_key", '{"power": 5}'),
	("2024-02-01T08:00:25", 'comma, and "quote" device', '{"power": 6}'),
	("2024-02-01T08:00:30", "Compteur électrique", '{"tension": 230.4}'),
	("2024-02-01T08:00:35", "deep_nesting", '{"a":{"b":{"c":{"d":{"e":{"f":{"g":{"h":{"i":{"j":{"k":{"l":42}}}}}}}}}}}}'),
	("2024-02-01T08:00:40", "empty_payload", "{}"),
	("2024-02-01T08:00:45", "null_payload", "null"),
	("2024-02-01T08:00:50", "", '{"power": 7}'),
	("2024-02-01T08:00:55", "big_payload", '{"blob": "' + "A" * 2048 + '"}'),
]

EDGE_DECISIONS = [
	("2024-02-01T08:00:00", "AutoToggle", "shelly_plug", "on"),
	("2024-02-01T08:00:05", "AutoToggle", "shelly_plug", '{"setpoint": 21.5}'),
	("2024-02-01T08:00:10", "AutoToggle", "shelly_plug", "set 42, then wait"),
	("2024-02-01T08:00:15", "AutoToggle", "shelly_plug", "line one\r\nline two"),
	("2024-02-01T08:00:20", "Algo, with comma", "shelly_plug", "off"),
	("2024-02-01T08:00:25", "AutoToggle", 'device "quoted"', "on"),
	("", "AutoToggle", "shelly_plug", "on"),
	("2024-02-01T08:00:30", "AutoToggle", "", "on"),
	("2024-02-01T08:00:35", "AutoToggle", "shelly_plug", ""),
]


def write_edge_cases() -> None:
	EDGE_CASE_DIR.mkdir(parents=True, exist_ok=True)
	# Taken from the backend, never retyped: this is the one file whose job is to fail
	# loudly when the storage format moves, so a column rename must not leave these
	# fixtures silently sitting on the old headers while --check still passes.
	reading_headers = CsvFileBackend.DEVICE_DATA_HEADERS
	decision_headers = CsvFileBackend.ALGORITHM_DECISIONS_HEADERS

	with open(EDGE_CASE_DIR / "device_data.csv", "w", newline="", encoding="utf-8") as f:
		writer = csv.writer(f)
		writer.writerow(reading_headers)
		writer.writerows(EDGE_READINGS)
		# Ragged rows, written raw: csv.writer cannot emit a row of the wrong width, and
		# a short row is what a process killed mid-write leaves behind.
		f.write("2024-02-01T08:01:00,ragged_short\r\n")
		f.write('2024-02-01T08:01:05,ragged_long,"{""power"": 8}",extra,columns\r\n')

	with open(EDGE_CASE_DIR / "algorithm_decisions.csv", "w", newline="", encoding="utf-8") as f:
		writer = csv.writer(f)
		writer.writerow(decision_headers)
		writer.writerows(EDGE_DECISIONS)

	# Zero bytes: a backend that has not written yet leaves no file at all, but a
	# pre-created empty one is exactly the state the lazy header exists to handle.
	(EDGE_CASE_DIR / "device_data.empty.csv").write_bytes(b"")
	(EDGE_CASE_DIR / "device_data.headers_only.csv").write_bytes(b"timestamp,device_name,data_json\r\n")
	# The Excel round-trip. The EMS never writes a BOM; a consumer that ignores one reads
	# the first column name as "﻿timestamp" and finds no timestamp column at all.
	(EDGE_CASE_DIR / "device_data.bom.csv").write_bytes(
		"﻿timestamp,device_name,data_json\r\n2024-02-01T08:00:00,bom_device,\"{\"\"power\"\": 9}\"\r\n".encode()
	)
	# In its own file because it is the one case that breaks the *dialect* rather than the
	# content. A field opened with `"` and never closed leaves the parser inside a quoted
	# field at the line break, so csv.reader does not fail — it silently swallows every
	# following line into that one field. The second row below is there to be eaten:
	# mixed into the main file this would hide every row under it, and the contract test
	# asserts that file stays parseable.
	(EDGE_CASE_DIR / "device_data.unterminated_quote.csv").write_bytes(
		b'timestamp,device_name,data_json\r\n'
		b'2024-02-01T08:00:00,truncated_mid_field,"{""power"": 12.5\r\n'
		b'2024-02-01T08:00:05,eaten_by_the_row_above,"{""power"": 13.0}"\r\n'
	)


def write_manifest() -> None:
	# newline="\n" for the same reason the control log passes it: text mode would
	# hand the terminator to os.linesep, and examples/** is stored verbatim under -text,
	# so a contributor regenerating on the other platform would rewrite every line of a
	# file whose checksums had not changed.
	MANIFEST.write_text(
		json.dumps(
			{
				"$comment": (
					"Checksums of the golden fixture. Regenerate with `python examples/generate.py`; "
					"tests/test_storage_contract.py verifies them, and Motrix Edge View verifies "
					"its vendored copy against this same file."
				),
				"storage_format_version": STORAGE_FORMAT_VERSION,
				"files": {name: sha256(EXAMPLES_DIR / name) for name in TRACKED},
			},
			indent="\t",
		)
		+ "\n",
		encoding="utf-8",
		newline="\n",
	)
	# Read back rather than trust the argument above. Drop it and text mode translates
	# to os.linesep silently: nothing here byte-compares the manifest, so the first
	# symptom is the next contributor regenerating on the other platform and getting a
	# whole-file diff. Fail one line from the cause instead.
	if b"\r\n" in MANIFEST.read_bytes():
		raise SystemExit("MANIFEST.json must use LF line terminators on every platform")


def sha256(path: Path) -> str:
	return hashlib.sha256(path.read_bytes()).hexdigest()


def check() -> int:
	"""Rebuild into a temp directory and diff against what is committed."""
	with tempfile.TemporaryDirectory() as tmp:
		target = Path(tmp) / "auto_toggle"
		target.mkdir()
		generate(target)
		drift = [
			name for name in TRACKED
			if (EXAMPLES_DIR / name).read_bytes() != (target.parent / name).read_bytes()
		]
	if drift:
		sys.stderr.write(
			"The storage output format changed:\n"
			+ "".join(f"  - {name}\n" for name in drift)
			+ "\nIf that was intentional: bump STORAGE_FORMAT_VERSION in storage/csv_file.py,\n"
			  "run `python examples/generate.py`, update docs/storage-format.md, and open an\n"
			  "issue on Motrix Edge View to re-vendor its fixture.\n"
		)
		return 1
	print(f"fixture is current (storage format {STORAGE_FORMAT_VERSION})")
	return 0


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--check", action="store_true",
						help="rebuild into a temp dir and diff instead of writing")
	parser.add_argument("--edge-cases", action="store_true",
						help="also rewrite examples/edge_cases/ (authored fixtures, not EMS output)")
	args = parser.parse_args()
	if args.check:
		return check()
	if args.edge_cases:
		write_edge_cases()
		print("rewrote examples/edge_cases/ — update its README.md to match")
	generate(AUTO_TOGGLE_DIR)
	write_manifest()
	print(f"regenerated {len(TRACKED)} files (storage format {STORAGE_FORMAT_VERSION})")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
