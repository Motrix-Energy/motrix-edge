from collections import deque
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import NamedTuple, Optional
from uuid import uuid4

from __metaclasses.singleton import Singleton

# Bounded because an EMS runs for months. 1000 decisions is far more than any polling
# client needs between two requests, and what falls off the back is *reported* through
# `missed` rather than silently vanishing — the same discipline docs/storage-format.md §7
# applies to gaps in the CSV: state the loss, never let a consumer infer it.
DEFAULT_CAPACITY = 1000


@dataclass(frozen=True, slots=True)
class Decision:
	"""One algorithm decision, as recorded at the storage funnel.

	Four of the five fields are named after `algorithm_decisions.csv`'s columns
	(`storage/csv_file.py`) on purpose: a consumer that already reads the CSV needs no new
	vocabulary, and the same decision produces an identical row through either surface.

	`seq` is the only field with no CSV equivalent. It is transport, not data — see
	`DecisionLog.page` for why the cursor cannot be a timestamp.
	"""
	seq: int
	timestamp: datetime
	algorithm: str
	device: str
	command: str


class DecisionPage(NamedTuple):
	"""A window onto the log, plus everything a caller needs to interpret it.

	Every counter is read in the *same* lock acquisition as `decisions`. Reading them
	separately is a torn read: a decision landing between the two calls makes `missed`
	describe a buffer state that never existed.
	"""
	decisions: list[Decision]
	total: int
	retained: int
	oldest_seq: Optional[int]
	missed: int
	capacity: int
	next_cursor: int
	has_more: bool
	epoch: str


class DecisionCounts(NamedTuple):
	"""Totals for the health endpoint, without materialising any record.

	Mirrors `DeviceCounts` (`api/devices_access.py`) — the health payload asks for a
	handful of integers on every poll and must not pay for a copy to get them.
	"""
	total: int
	retained: int
	capacity: int


def decision_timestamp() -> datetime:
	"""Event time for an algorithm decision: the timestep the algorithm was stepping.

	Deliberately the committed step rather than the dispatch clock — a decision computed
	while processing T belongs to T even if the replay has moved on. Shared with
	`StorageBackend._decision_timestamp` so the rule has one implementation: the endpoint
	and the CSV must never disagree about when a decision happened.
	"""
	from simulation.clock import SimulationClock
	return SimulationClock().get_step_time() or datetime.now()


class DecisionLog(metaclass=Singleton):
	"""A bounded, in-memory history of algorithm decisions.

	Core state, not a storage backend. Two things follow from that, and both are the point:

	- It is written from `StorageManager.write_algorithm_decision` — the single funnel every
	  backend already sees — so `/decisions` and `algorithm_decisions.csv` hold the same
	  decisions in the same order by construction, rather than by two call sites agreeing.
	- The append sits *outside* the fan-out, so decisions are recorded even when no storage
	  backend is configured at all. A ring-buffer backend would instead have made a REST
	  endpoint's existence depend on an unrelated `storage` entry, and left a client unable
	  to tell "no decisions yet" from "not configured" — which is the blindness this exists
	  to remove, relocated one level up.

	Thread safety: algorithms write from their own supervised threads while the API reads
	from FastAPI's threadpool. One lock covers the deque and the counter, and **the clock is
	read before the lock is taken** — `SimulationClock` holds a condition variable, and these
	two must never nest. Same discipline `services/rest_api.py` states for `devices_lock`.

	`Singleton.__call__` ignores constructor arguments after the first call, so `capacity` is
	effectively the module constant in a running process. That is a further reason it is not
	a config knob: a service option could only ever resize an already-constructed instance.
	"""

	def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
		self._capacity = max(1, capacity)
		self._records: deque[Decision] = deque(maxlen=self._capacity)
		self._total = 0
		self._lock = Lock()
		# Identity of this sequence's origin. `seq` is per-process and restarts at 1, so a
		# client holding a cursor of 5000 across a restart would otherwise wait forever for
		# a seq that will not come back for hours — silently, with no error anywhere.
		# Opaque to consumers: compared for equality, never parsed.
		self._epoch = uuid4().hex

	def record(self, algorithm: str, device: str, command: str) -> Decision:
		"""Append one decision and return it. Must not raise — it is on an algorithm's thread.

		`command` is coerced with `str()` and otherwise left alone: not parsed, not truncated.
		That is exactly what `csv.writer` does to it, which is what keeps this surface and the
		CSV byte-identical for a non-str command.
		"""
		timestamp = decision_timestamp()
		with self._lock:
			self._total += 1
			decision = Decision(
				seq=self._total,
				timestamp=timestamp,
				algorithm=str(algorithm),
				device=str(device),
				command=str(command),
			)
			self._records.append(decision)
			return decision

	def page(self, after: int = 0, limit: int = DEFAULT_CAPACITY) -> DecisionPage:
		"""Decisions with `seq` greater than `after`, oldest first, at most `limit` of them.

		**The cursor is a sequence number and not a timestamp**, and that is the whole design.
		`docs/storage-format.md` §4 states rows are not monotonic and duplicate timestamps are
		legal — and under `speed=0` every decision in one timestep carries the *identical*
		committed step time. An inclusive timestamp cursor therefore re-delivers a whole
		timestep on every poll; an exclusive one drops all but the first decision in it.
		A monotonic `seq` has neither failure.

		`after` clamps at 0, so `after=-1` means "from the oldest still retained".

		`limit=0` is a **seek-to-head probe**: it delivers nothing and returns the current head
		as `next_cursor`, which is how a client starts streaming from now without backfilling.
		It is the one case where the cursor advances past undelivered records, and it is only
		ever reached by asking for it explicitly.

		When more records are newer than `after` than `limit` allows, the **oldest** are
		returned. Returning the newest would skip the middle silently, and `has_more` tells
		the caller to come back immediately rather than wait out its poll interval.
		"""
		after = max(0, after)
		limit = max(0, min(limit, self._capacity))
		with self._lock:
			total = self._total
			retained = len(self._records)
			oldest_seq = self._records[0].seq if self._records else None
			# Evicted between where the caller left off and what is still held. Positive only
			# once a cursor has fallen off the back of the ring.
			missed = max(0, oldest_seq - after - 1) if oldest_seq is not None else 0
			if limit == 0:
				return DecisionPage(
					decisions=[],
					total=total,
					retained=retained,
					oldest_seq=oldest_seq,
					missed=missed,
					capacity=self._capacity,
					next_cursor=total,
					has_more=False,
					epoch=self._epoch,
				)
			newer = [decision for decision in self._records if decision.seq > after]
			decisions = newer[:limit]
			return DecisionPage(
				decisions=decisions,
				total=total,
				retained=retained,
				oldest_seq=oldest_seq,
				missed=missed,
				capacity=self._capacity,
				# The last record actually delivered. Never a record the caller did not get,
				# so a client that stores this verbatim cannot skip.
				next_cursor=decisions[-1].seq if decisions else after,
				has_more=len(newer) > limit,
				epoch=self._epoch,
			)

	def counts(self) -> DecisionCounts:
		"""Totals only. O(1), for the health payload."""
		with self._lock:
			return DecisionCounts(total=self._total, retained=len(self._records), capacity=self._capacity)

	def epoch(self) -> str:
		"""Opaque identity of this process's sequence. A change means the EMS restarted."""
		return self._epoch
