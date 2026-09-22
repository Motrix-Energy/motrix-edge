"""What `config.json`'s top-level `version` means, and what this build does about it.

## It versions the document, not the program

`version` declares **the version of the `config.json` format the file was written
against** — never the EMS release that reads it. Two reasons, and the first is decisive:
there is nothing else it could mean. This repository has no release constant. There is no
`__version__`, no VERSION file and no package metadata; release identity lives in git tags
and in the pinned container image tags `docker-compose.yml` reads, and the only other
version constant in the tree is `storage/csv_file.py`'s `STORAGE_FORMAT_VERSION`, which
versions an output format by exactly this logic. The second reason is that the release
reading makes the key actively harmful: an operator would have to edit `config.json` on
every upgrade of a file that `docker-compose.yml` mounts **read-only** on purpose.

So the number answers one question — *can this build understand the shape of this
document?* — and `CONFIG_FORMAT_VERSION` below is this build's answer.

## Absence is not a claim

The key is optional in `config.schema.json`, and a file that omits it says nothing about
its own format, so this build says nothing back: `Config.version` is `None` and no
verdict is reported. That split is the whole point of this module. The constant it
replaced, `Config.DEFAULT_VERSION`, did two unrelated jobs at once — the value assumed
when the key was absent *and* the value compared against — which is why a file that said
nothing passed silently while a file that declared `"1.0.0"` honestly was warned about.
The project's own worked example, `examples/auto_toggle/config.json`, was the file being
warned about.

`None` for absent is also what `motrix-edge-view`'s `src/core/topology.ts` already reports
for the same bytes, via `stringOrNull(root['version'])`. The two now agree on every input,
including the empty string — see `Config.__init__`'s `and declared`.

## What a bump means

- **MAJOR** — a top-level key removed or renamed, an existing key's meaning or type
  changed, or an optional key made required. A document of another major may be read
  wrongly rather than incompletely, so it is an ERROR.
- **MINOR** — additive only: a new optional top-level key, a new plugin axis. A build
  understands every document of an equal or older minor by construction, so only a
  *newer* minor is worth a word.
- **PATCH** — no shape change at all. Never reported, in either direction.

One rule has to be stated explicitly or this constant rots within a month: **a plugin's
own options are not versioned by this number.** They live in that plugin's
`*.schema.json` beside its constructor signature, and `tests/test_config.py` keeps those
two in lockstep. Adding an option to `connectors/mqtt.py` is neither a MAJOR nor a MINOR
bump here.

## Migration is a non-goal, and the seam is the deliverable

There is no migrator, deliberately. A migrator migrates *from* a released format *to*
another, and this project has released exactly one format and never bumped it — so the
mechanism would ship with zero migrations and zero tests of a real one, designed against
an imagined break that the first actual break will not fit. It would also have nowhere to
write: `docker-compose.yml` mounts `config.json` read-only so that the container can write
readings and not rewrite its own wiring, and an in-memory migrator means the process runs
wiring the operator has never seen, in a system that closes relays.

What is left behind instead is the shape a migrator would need: one parse point, one pure
verdict computed before any plugin option has been read, and the declared string preserved
verbatim on `Config.version`. Adding a migration becomes a branch on `Compatibility`, not
a refactor.

## Nothing here raises

`Config` is constructed in `Main.__init__`, *before* `main()`'s `try/finally` is entered,
so an exception escaping it has no shutdown path — the same reasoning `api/options.py`'s
module docstring gives for `RestartPolicy.from_runtime`. That is the whole argument here:
unlike a plugin constructor, which `main.create_classes` contains to its own entry, this
code has nothing above it to catch anything. One mistyped version string would end the run.
This module warns and reports; it never raises.

It lives here rather than in `api/options.py` because that module scopes itself to values
that reach a plugin constructor after interpolation, and its coerce-or-default signature
would collapse "absent" and "unreadable" into one defaulted value — which is precisely the
distinction everything above depends on.
"""
from enum import Enum, auto
from re import compile
from typing import Any, Optional

# The format this build understands. Bump it by the table in this module's docstring, and
# when you do, update `config.schema.json`'s `$comment` on `version` in the same edit —
# `tests/test_config.py::TestConfigFormatVersion` fails when those two disagree.
CONFIG_FORMAT_VERSION: str = "1.0.0"

# `config.schema.json` constrains `version` to `^\d+\.\d+\.\d+$` with no bound on component
# length, and this pattern is deliberately narrower: `int()` raises ValueError above
# `sys.get_int_max_str_digits()` (4300 by default, and the Dockerfile's python:3.12-slim
# does not change it), so a schema-valid 4301-digit component would be a raise out of
# `Config.__init__` from a module that promises never to raise. Nine digits is past any
# version anyone will write and keeps the promise without a try/except around a constant.
# `motrix-edge-view`'s `parseConfigVersion` carries the same bound for the same reason, so
# the two implementations agree on every input rather than only on realistic ones.
#
# `[0-9]` spelled out, never `\d`: Python's `\d` matches every Unicode decimal digit, so it
# would read "٢.٠.٠" as 2.0.0 — where `config.schema.json`'s own `^\d+\.\d+\.\d+$` is a JSON
# Schema pattern, and JSON Schema regexes are ECMA-262, whose `\d` is exactly `[0-9]`. The
# Unicode-aware spelling was therefore wider than both the schema this validates against and
# the viewer that reads the same file, which would make the EMS log a major-version ERROR
# about a document the viewer says nothing about at all.
_VERSION_PATTERN = compile(r"([0-9]{1,9})\.([0-9]{1,9})\.([0-9]{1,9})")


class Compatibility(Enum):
	"""What this build has to say about a document's declared format version.

	A verdict, not a log line: the message and its level belong to the caller, so a future
	migrator can branch on the same value without parsing English.
	"""
	UNDECLARED = auto()
	"""No `version` key, `null`, or an empty string. The file made no claim."""
	UNREADABLE = auto()
	"""A value present — including one of another JSON type — but not three integer
	components, so no comparison was made."""
	COMPATIBLE = auto()
	"""Same major, and a minor this build already understands. Includes any patch difference."""
	FORWARD_MINOR = auto()
	"""Same major, newer minor: written against a format this build predates."""
	INCOMPATIBLE_OLDER = auto()
	"""An older major. Keys this build reads may mean something else in that format."""
	INCOMPATIBLE_NEWER = auto()
	"""A newer major. This build is the thing that is out of date."""


def parse(value: Any) -> Optional[tuple[int, int, int]]:
	"""`(major, minor, patch)`, or None when the value is not exactly three components.

	Never raises, for anything — that is the contract the whole module rests on. A
	non-string, `None`, the empty string, `1.0`, `v1.0.0`, `1.0.0-rc1`, a non-ASCII decimal
	digit and a component too long to be an `int` all come back as None rather than as a
	best-effort guess, because a comparison against a value you failed to read is how a
	malformed string becomes a confident wrong verdict.
	"""
	if not isinstance(value, str):
		return None
	match = _VERSION_PATTERN.fullmatch(value)
	if match is None:
		return None
	return int(match.group(1)), int(match.group(2)), int(match.group(3))


def compare(declared: Any) -> Compatibility:
	"""Judge a declared version against `CONFIG_FORMAT_VERSION`. Pure: it logs nothing."""
	# An absent key and an empty string are the same absence — `topology.ts`'s
	# `stringOrNull` folds them together too, and matching it is what makes the two
	# repositories report the same thing about the same bytes. A value of another JSON
	# type is **not** an absence: the operator wrote something, so it is unreadable rather
	# than unsaid, and staying silent about it would hide the one case the schema's "not of
	# type string" error and this module are both trying to surface.
	if declared is None or declared == "":
		return Compatibility.UNDECLARED
	parsed = parse(declared)
	if parsed is None:
		return Compatibility.UNREADABLE
	# The build's own constant is parsed rather than split, so a typo in it is caught by
	# the same grammar every document is held to.
	supported = parse(CONFIG_FORMAT_VERSION)
	if supported is None:  # pragma: no cover — pinned by test_the_build_constant_is_readable
		return Compatibility.UNDECLARED
	if parsed[0] < supported[0]:
		return Compatibility.INCOMPATIBLE_OLDER
	if parsed[0] > supported[0]:
		return Compatibility.INCOMPATIBLE_NEWER
	if parsed[1] > supported[1]:
		return Compatibility.FORWARD_MINOR
	# Equal, an older minor, or any patch difference: MINOR is additive-only and PATCH
	# changes no shape, so this build reads every one of them.
	return Compatibility.COMPATIBLE
