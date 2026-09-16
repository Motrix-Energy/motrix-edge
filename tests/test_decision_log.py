"""The in-memory decision history behind GET /decisions.

Pure core: no web framework, no `importorskip`. `api/decisions.py` deliberately imports
nothing outside the standard library and the singleton metaclass, so this runs on a machine
with neither fastapi nor uvicorn installed.
"""
from datetime import datetime

import pytest

from api.decisions import DEFAULT_CAPACITY, Decision, DecisionLog, decision_timestamp
from simulation.clock import SimulationClock
from tests.conftest import run_in_thread


@pytest.fixture
def log():
    """A fresh log (the autouse singleton fixture clears the instance between tests)."""
    return DecisionLog()


def record_many(log: DecisionLog, count: int, algorithm: str = "AutoToggle") -> None:
    for index in range(count):
        log.record(algorithm, "shelly_plug", "on" if index % 2 else "off")


class TestRecording:
    def test_seq_starts_at_one_and_increases(self, log):
        first = log.record("AutoToggle", "shelly_plug", "off")
        second = log.record("AutoToggle", "shelly_plug", "on")
        assert (first.seq, second.seq) == (1, 2)

    def test_record_returns_the_stored_decision(self, log):
        decision = log.record("AutoToggle", "shelly_plug", "off")
        assert isinstance(decision, Decision)
        assert (decision.algorithm, decision.device, decision.command) == ("AutoToggle", "shelly_plug", "off")

    def test_total_counts_every_record_ever(self, log):
        record_many(log, 5)
        assert log.counts().total == 5

    def test_command_is_coerced_with_str_like_csv_writer(self, log):
        """`csv.writer` stringifies whatever it is handed; this must match it exactly, or the
        endpoint and algorithm_decisions.csv disagree for a non-str command."""
        decision = log.record("AutoToggle", "shelly_plug", {"setpoint": 21.5})
        assert decision.command == str({"setpoint": 21.5})

    def test_command_is_never_truncated(self, log):
        command = "x" * 5000
        assert log.record("A", "d", command).command == command

    def test_empty_log_reports_nothing(self, log):
        page = log.page()
        assert (page.decisions, page.total, page.retained, page.oldest_seq) == ([], 0, 0, None)


class TestTimestamps:
    def test_uses_the_committed_step_time_under_a_replay(self, log):
        moment = datetime(2024, 1, 15, 10, 0, 0)
        SimulationClock().publish_step(moment)
        assert log.record("AutoToggle", "shelly_plug", "on").timestamp == moment

    def test_falls_back_to_the_wall_clock_with_no_replay(self, log):
        before = datetime.now()
        stamped = log.record("AutoToggle", "shelly_plug", "on").timestamp
        assert before <= stamped <= datetime.now()

    def test_timestamp_is_never_none(self, log):
        """Stronger than /devices' simulation_time, which is null outside a replay. A client
        therefore never has to borrow a clock to place a decision, and never drops one."""
        assert log.record("A", "d", "on").timestamp is not None

    def test_naive_step_time_stays_naive(self, log):
        SimulationClock().publish_step(datetime(2024, 1, 15, 10, 0, 0))
        assert log.record("A", "d", "on").timestamp.tzinfo is None

    def test_offset_aware_step_time_keeps_its_offset(self, log):
        moment = datetime.fromisoformat("2024-03-31T03:00:00+02:00")
        SimulationClock().publish_step(moment)
        assert log.record("A", "d", "on").timestamp.utcoffset() == moment.utcoffset()

    def test_decision_timestamp_is_shared_with_the_storage_backend(self):
        """One implementation of "a decision belongs to the step it was computed in". Two
        would drift, and the endpoint would disagree with the CSV about when things happened."""
        from api.storage_backend import StorageBackend
        moment = datetime(2024, 1, 15, 10, 30, 0)
        SimulationClock().publish_step(moment)
        assert decision_timestamp() == moment
        assert StorageBackend._decision_timestamp(None) == moment

    def test_decisions_in_one_step_share_a_timestamp_and_differ_in_seq(self, log):
        """The test that justifies the cursor being a sequence number.

        Under speed=0 every decision in a timestep carries the identical committed step time,
        so a timestamp cursor either re-delivers the whole step on every poll (inclusive) or
        drops all but the first of it (exclusive). Neither is a paging strategy.
        """
        SimulationClock().publish_step(datetime(2024, 1, 15, 10, 0, 0))
        first = log.record("AutoToggle", "shelly_plug", "on")
        second = log.record("AutoToggle", "heat_relay", "off")
        assert first.timestamp == second.timestamp
        assert first.seq != second.seq


class TestPaging:
    def test_returns_everything_from_the_start(self, log):
        record_many(log, 5)
        assert [d.seq for d in log.page(after=0).decisions] == [1, 2, 3, 4, 5]

    def test_after_is_exclusive(self, log):
        record_many(log, 5)
        assert [d.seq for d in log.page(after=3).decisions] == [4, 5]

    def test_negative_after_clamps_to_the_oldest_retained(self, log):
        record_many(log, 3)
        assert [d.seq for d in log.page(after=-1).decisions] == [1, 2, 3]

    def test_limit_returns_the_oldest_not_the_newest(self, log):
        """Returning the newest would skip the middle with nothing reporting the skip."""
        record_many(log, 10)
        assert [d.seq for d in log.page(after=0, limit=3).decisions] == [1, 2, 3]

    def test_has_more_is_set_when_the_limit_truncates(self, log):
        record_many(log, 10)
        assert log.page(after=0, limit=3).has_more is True
        assert log.page(after=0, limit=10).has_more is False

    def test_next_cursor_is_the_last_record_delivered(self, log):
        record_many(log, 10)
        page = log.page(after=0, limit=4)
        assert page.next_cursor == page.decisions[-1].seq == 4

    def test_next_cursor_holds_still_when_nothing_is_newer(self, log):
        record_many(log, 5)
        assert log.page(after=5).next_cursor == 5

    def test_draining_in_pages_yields_every_record_exactly_once(self, log):
        record_many(log, 25)
        seen, cursor = [], 0
        while True:
            page = log.page(after=cursor, limit=7)
            seen.extend(d.seq for d in page.decisions)
            cursor = page.next_cursor
            if not page.has_more:
                break
        assert seen == list(range(1, 26))

    def test_limit_zero_seeks_to_head_without_delivering(self, log):
        """The capability probe: learn where the sequence is, start streaming from now."""
        record_many(log, 5)
        page = log.page(after=-1, limit=0)
        assert page.decisions == []
        assert page.next_cursor == 5
        assert page.has_more is False
        assert log.page(after=page.next_cursor).decisions == []

    def test_limit_is_clamped_to_capacity(self, log):
        record_many(log, 5)
        assert len(log.page(after=0, limit=10 ** 9).decisions) == 5

    def test_page_reports_capacity(self, log):
        assert log.page().capacity == DEFAULT_CAPACITY


class TestEviction:
    @pytest.fixture
    def small(self):
        return DecisionLog(capacity=10)

    def test_retains_at_most_capacity(self, small):
        record_many(small, 15)
        counts = small.counts()
        assert (counts.retained, counts.total, counts.capacity) == (10, 15, 10)

    def test_oldest_seq_moves_with_the_ring(self, small):
        record_many(small, 15)
        assert small.page().oldest_seq == 6

    def test_missed_is_zero_while_the_cursor_is_inside_the_buffer(self, small):
        record_many(small, 15)
        assert small.page(after=10).missed == 0

    def test_missed_counts_what_fell_off_behind_the_cursor(self, small):
        """Stated, not inferred — the same rule docs/storage-format.md §7 applies to gaps."""
        record_many(small, 15)
        # Retained is seq 6..15; a client left off at 2, so 3, 4 and 5 are gone.
        assert small.page(after=2).missed == 3

    def test_missed_is_zero_on_an_empty_log(self, small):
        assert small.page(after=99).missed == 0

    def test_eviction_does_not_lose_records_still_inside_the_window(self, small):
        record_many(small, 15)
        assert [d.seq for d in small.page(after=6).decisions] == list(range(7, 16))


class TestRestartDetection:
    def test_epoch_is_stable_within_a_process(self, log):
        record_many(log, 3)
        assert log.page().epoch == log.page().epoch == log.epoch()

    def test_a_fresh_log_has_a_different_epoch(self, log):
        """What makes a restart detectable. `total` alone cannot do it: once the new process
        passes the old cursor, a seq-based client silently skips records forever."""
        from __metaclasses.singleton import Singleton
        first_epoch = log.epoch()
        Singleton._instances.clear()
        assert DecisionLog().epoch() != first_epoch

    def test_a_cursor_beyond_total_is_visible_to_the_caller(self, log):
        record_many(log, 3)
        page = log.page(after=5000)
        assert page.decisions == []
        assert page.total < 5000


class TestConcurrency:
    def test_parallel_writers_produce_contiguous_unique_seqs(self, log):
        threads = [run_in_thread(lambda: record_many(log, 50)) for _ in range(4)]
        for thread in threads:
            thread.join(5)
        seqs = [d.seq for d in log.page(after=0, limit=DEFAULT_CAPACITY).decisions]
        assert log.counts().total == 200
        assert seqs == list(range(1, 201))

    def test_reading_while_writing_never_tears(self, log):
        """`page()` takes one lock for the records and every counter. Two acquisitions would
        let a write land between them and make `missed` describe a state that never existed."""
        writer = run_in_thread(lambda: record_many(log, 300))
        for _ in range(50):
            page = log.page(after=0)
            assert page.total >= page.retained
            assert len(page.decisions) <= page.retained
            if page.decisions:
                assert page.decisions[-1].seq <= page.total
        writer.join(5)
