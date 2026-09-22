"""InfluxDB storage backend: batching, flattening, and the close() flush contract.

Every test runs with no InfluxDB server. `storage.influxdb.InfluxDBClient` is the
single construction site, so patching that one name removes all I/O.
"""
import inspect
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# influxdb-client is in requirements.txt, but a venv created before it was pinned does not
# have it — and a bare ModuleNotFoundError here aborts collection for the ENTIRE suite
# ("Interrupted: 1 error during collection", zero tests run), not just this file.
pytest.importorskip("influxdb_client", reason="pip install -r requirements.txt")

from api.capabilities import MetricSource
from devices.p1 import P1
from simulation.clock import SimulationClock
from storage.influxdb import InfluxDBBackend
from tests.conftest import StubDevice

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "storage", "influxdb.schema.json")

SHELLY_PAYLOAD = {"power": "75", "energy": "1234", "status": True, "overtemperature": False}
P1_PAYLOAD = {
    "model_id": "ISK5",
    "data": [
        {
            "obis": {"medium": 1, "channel": 0, "class": 1, "instance": 8, "attribute": 1},
            "data": [{"value": 12.5, "unit": "kWh"}],
        }
    ],
}
PSEUDO_PAYLOAD = {"topic": "sensors/1", "payload": "hello", "parsed": None}


class MetricStubDevice(StubDevice, MetricSource):
    """A device that names its own readings."""
    metrics: dict = {}

    def get_metrics(self) -> dict:
        return self.metrics


def make_backend(**kwargs) -> InfluxDBBackend:
    defaults = dict(name="influx", url="http://influx.test:8086", token="tok", org="motrix", bucket="motrix")
    defaults.update(kwargs)
    return InfluxDBBackend(**defaults)


def wire(mock_client_cls):
    """The (client, write_api) mocks reachable through the patched InfluxDBClient."""
    client = mock_client_cls.return_value
    return client, client.write_api.return_value


def written_points(write_api) -> list:
    return [call.kwargs["record"] for call in write_api.write.call_args_list]


def only_point(write_api):
    points = written_points(write_api)
    assert len(points) == 1, f"expected exactly one point, got {len(points)}"
    return points[0]


# Point keeps its state in private attributes; funnel every access through these so a
# library change is a one-line fix rather than a shotgun edit.
def fields_of(point) -> dict:
    return point._fields


def tags_of(point) -> dict:
    return point._tags


def measurement_of(point) -> str:
    return point._name


def flux_record(values: dict):
    record = MagicMock()
    record.values = values
    return record


def flux_table(*value_dicts):
    table = MagicMock()
    table.records = [flux_record(values) for values in value_dicts]
    return table


def write_once(backend, mock_client_cls, data=None, device_name="d1"):
    """Drive one device write and return the (client, write_api) mocks."""
    device = StubDevice(name=device_name)
    backend.write_device_data(device, data if data is not None else {"power": 1})
    return wire(mock_client_cls)


class TestConstruction:
    def test_no_client_is_built_in_init(self):
        with patch("storage.influxdb.InfluxDBClient") as MockClient:
            make_backend()
            MockClient.assert_not_called()

    def test_defaults(self):
        backend = InfluxDBBackend("influx")
        assert backend.url == InfluxDBBackend.DEFAULT_URL
        assert backend.bucket == "motrix"
        assert backend.measurement == "device_data"
        assert backend.decisions_measurement == "algorithm_decisions"
        assert backend.field_separator == "."
        assert backend.parse_numeric_strings is True
        assert backend.batch_size == 500

    def test_max_close_wait_default_is_not_the_library_five_minutes(self):
        # StorageManager.close_all() is not bounded by the shutdown timeout, so the
        # library's 300_000ms default would hang shutdown when InfluxDB is unreachable.
        assert make_backend().max_close_wait_ms == 15_000

    def test_retry_budget_defaults_to_the_close_bound(self):
        # The library's 180_000ms default outlives close(): force-closing the writer
        # does not cancel an in-flight retry, and those threads are not daemons.
        backend = make_backend(max_close_wait_ms=8_000)
        assert backend.max_retry_time_ms == 8_000

    def test_explicit_retry_budget_is_respected(self):
        assert make_backend(max_retry_time_ms=3_000).max_retry_time_ms == 3_000

    def test_retry_budget_above_the_close_bound_warns(self, caplog):
        with caplog.at_level(logging.WARNING):
            make_backend(max_close_wait_ms=5_000, max_retry_time_ms=60_000)
        assert "shutdown can hang" in caplog.text

    def test_missing_url_falls_back_and_warns(self, caplog):
        with caplog.at_level(logging.WARNING):
            backend = InfluxDBBackend("influx")
        assert backend.url == InfluxDBBackend.DEFAULT_URL
        assert "No InfluxDB url configured" in caplog.text

    def test_missing_token_warns(self, caplog):
        with caplog.at_level(logging.WARNING):
            make_backend(token=None)
        assert "No InfluxDB token configured" in caplog.text

    def test_missing_org_warns(self, caplog):
        with caplog.at_level(logging.WARNING):
            make_backend(org=None)
        assert "No InfluxDB org configured" in caplog.text

    def test_numeric_option_accepts_string(self):
        # ${VAR} interpolation always yields strings, never ints.
        assert make_backend(batch_size="42").batch_size == 42

    def test_numeric_option_accepts_none(self):
        # A whole-value ${VAR} that resolves empty arrives as None.
        assert make_backend(batch_size=None).batch_size == 500

    def test_invalid_numeric_option_warns_and_keeps_default(self, caplog):
        with caplog.at_level(logging.WARNING):
            backend = make_backend(batch_size="abc")
        assert backend.batch_size == 500
        assert "'batch_size'" in caplog.text

    def test_out_of_range_numeric_option_warns_and_keeps_default(self, caplog):
        with caplog.at_level(logging.WARNING):
            backend = make_backend(flush_interval_ms=0)
        assert backend.flush_interval_ms == 10_000
        assert "'flush_interval_ms'" in caplog.text

    @pytest.mark.parametrize("value,expected", [
        (True, True), (False, False), ("true", True), ("FALSE", False),
        ("1", True), ("0", False), ("on", True), ("off", False), (None, True),
    ])
    def test_bool_option_coercion(self, value, expected):
        assert make_backend(parse_numeric_strings=value).parse_numeric_strings is expected

    def test_invalid_bool_option_warns_and_keeps_default(self, caplog):
        with caplog.at_level(logging.WARNING):
            backend = make_backend(parse_numeric_strings="maybe")
        assert backend.parse_numeric_strings is True
        assert "'parse_numeric_strings'" in caplog.text

    @pytest.mark.parametrize("junk", ["abc", [], {}, -1, 3.7, object()])
    def test_junk_numeric_options_never_raise(self, junk):
        # A raise out of __init__ is contained by main.create_classes, but the backend is
        # then skipped and the run records nothing — silently, since storage is optional.
        assert make_backend(batch_size=junk).batch_size > 0

    def test_empty_and_none_static_tags_are_dropped(self):
        # InfluxDB reads an empty tag value as "tag absent", silently splitting the series.
        backend = make_backend(tags={"site": "site-a", "zone": None, "rack": ""})
        assert backend.tags == {"site": "site-a"}

    def test_unknown_option_raises_type_error(self):
        # The kwargs contract: create_classes reports this as "could not be instantiated".
        with pytest.raises(TypeError):
            make_backend(bogus=1)

    def test_backend_is_hashable(self):
        # create_classes collects instances into a set.
        assert len({make_backend(), make_backend()}) == 2


class TestFlatten:
    def test_flat_dict(self):
        assert make_backend()._flatten_data({"a": 1, "b": "x"}) == {"a": 1.0, "b": "x"}

    def test_nested_dict_uses_separator(self):
        assert make_backend()._flatten_data({"a": {"b": {"c": 1}}}) == {"a.b.c": 1.0}

    def test_custom_separator(self):
        assert make_backend(field_separator="_")._flatten_data({"a": {"b": 1}}) == {"a_b": 1.0}

    def test_list_uses_index_components(self):
        assert make_backend()._flatten_data({"a": [10, 20]}) == {"a.0": 10.0, "a.1": 20.0}

    def test_bool_is_not_coerced_to_number(self):
        # bool subclasses int, so the isinstance order in _flatten is load-bearing.
        result = make_backend()._flatten_data({"on": True, "off": False})
        assert result["on"] is True and result["off"] is False

    def test_bool_nested_in_list_stays_bool(self):
        assert make_backend()._flatten_data({"a": [True]})["a.0"] is True

    def test_none_is_dropped(self):
        assert make_backend()._flatten_data({"a": None, "b": 1}) == {"b": 1.0}

    def test_nested_none_is_dropped(self):
        assert make_backend()._flatten_data({"a": {"b": None}}) == {}

    def test_int_and_float_both_become_float(self):
        # An int/float alternation on one field key rejects the entire batch with a 422.
        result = make_backend()._flatten_data({"a": 1, "b": 1.5})
        assert result == {"a": 1.0, "b": 1.5}
        assert all(isinstance(value, float) for value in result.values())

    def test_numeric_string_becomes_float(self):
        assert make_backend()._flatten_data({"power": "75"}) == {"power": 75.0}

    @pytest.mark.parametrize("text,expected", [("-3", -3.0), ("1.5e3", 1500.0), (".5", 0.5), ("+2", 2.0)])
    def test_numeric_string_forms(self, text, expected):
        assert make_backend()._flatten_data({"v": text}) == {"v": expected}

    def test_non_numeric_string_stays_string(self):
        assert make_backend()._flatten_data({"v": "on"}) == {"v": "on"}

    @pytest.mark.parametrize("text", ["nan", "inf", "Infinity", "1_0", "0x10"])
    def test_float_accepts_these_but_we_do_not(self, text):
        # float() would take all of these; a device id of "1_0" becoming 10.0 is the
        # kind of bug that never gets found.
        assert make_backend()._flatten_data({"v": text}) == {"v": text}

    def test_numeric_parsing_can_be_disabled(self):
        assert make_backend(parse_numeric_strings=False)._flatten_data({"power": "75"}) == {"power": "75"}

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_floats_are_dropped(self, value):
        assert make_backend()._flatten_data({"v": value}) == {}

    def test_empty_containers_produce_no_fields(self):
        assert make_backend()._flatten_data({"a": {}, "b": [], "c": {"d": {}}}) == {}

    def test_empty_data_produces_no_fields(self):
        assert make_backend()._flatten_data({}) == {}

    def test_shelly_payload(self):
        assert make_backend()._flatten_data(SHELLY_PAYLOAD) == {
            "power": 75.0, "energy": 1234.0, "status": True, "overtemperature": False,
        }

    def test_p1_payload(self):
        assert make_backend()._flatten_data(P1_PAYLOAD) == {
            "model_id": "ISK5",
            "data.0.obis.medium": 1.0,
            "data.0.obis.channel": 0.0,
            "data.0.obis.class": 1.0,
            "data.0.obis.instance": 8.0,
            "data.0.obis.attribute": 1.0,
            "data.0.data.0.value": 12.5,
            "data.0.data.0.unit": "kWh",
        }

    def test_pseudo_payload(self):
        assert make_backend()._flatten_data(PSEUDO_PAYLOAD) == {"topic": "sensors/1", "payload": "hello"}

    def test_exotic_value_becomes_string(self):
        moment = datetime(2026, 7, 31, 12, 0)
        assert make_backend()._flatten_data({"t": moment}) == {"t": str(moment)}

    def test_depth_limit_stops_recursion(self):
        nested = {"a": 1}
        for _ in range(15):
            nested = {"a": nested}
        result = make_backend()._flatten_data(nested)
        assert len(result) == 1
        assert isinstance(next(iter(result.values())), str)


@patch("storage.influxdb.InfluxDBClient")
class TestWriteDeviceData:
    def test_client_built_lazily_on_first_write(self, MockClient):
        backend = make_backend()
        MockClient.assert_not_called()
        write_once(backend, MockClient)
        MockClient.assert_called_once()

    def test_client_constructor_arguments(self, MockClient):
        write_once(make_backend(timeout_ms=1234), MockClient)
        kwargs = MockClient.call_args.kwargs
        assert kwargs["url"] == "http://influx.test:8086"
        assert kwargs["token"] == "tok"
        assert kwargs["org"] == "motrix"
        assert kwargs["timeout"] == 1234

    def test_client_built_only_once_across_writes(self, MockClient):
        backend = make_backend()
        client, write_api = wire(MockClient)
        for i in range(3):
            backend.write_device_data(StubDevice(name=f"d{i}"), {"power": i + 1})
        MockClient.assert_called_once()
        client.write_api.assert_called_once()
        assert write_api.write.call_count == 3

    def test_write_options_are_forwarded_in_milliseconds(self, MockClient):
        backend = make_backend(batch_size=7, flush_interval_ms=1234, retry_interval_ms=99,
                               max_retries=2, max_close_wait_ms=4321, max_retry_time_ms=2000)
        client, _ = write_once(backend, MockClient)
        options = client.write_api.call_args.kwargs["write_options"]
        assert options.batch_size == 7
        assert options.flush_interval == 1234
        assert options.retry_interval == 99
        assert options.max_retries == 2
        assert options.max_close_wait == 4321
        assert options.max_retry_time == 2000
        # Derived: a single backoff must not outlast the whole retry budget.
        assert options.max_retry_delay == 2000

    def test_callbacks_are_registered(self, MockClient):
        backend = make_backend()
        client, _ = write_once(backend, MockClient)
        kwargs = client.write_api.call_args.kwargs
        assert kwargs["success_callback"] == backend._on_write_success
        assert kwargs["error_callback"] == backend._on_write_error
        assert kwargs["retry_callback"] == backend._on_write_retry

    def test_point_measurement_and_tags(self, MockClient):
        _, write_api = write_once(make_backend(), MockClient, device_name="shelly1")
        point = only_point(write_api)
        assert measurement_of(point) == "device_data"
        assert tags_of(point) == {"device": "shelly1", "device_kind": "StubDevice"}

    def test_custom_measurement(self, MockClient):
        _, write_api = write_once(make_backend(measurement="readings"), MockClient)
        assert measurement_of(only_point(write_api)) == "readings"

    def test_static_tags_applied(self, MockClient):
        _, write_api = write_once(make_backend(tags={"site": "site-a"}), MockClient)
        assert tags_of(only_point(write_api))["site"] == "site-a"

    def test_fields_are_the_flattened_payload(self, MockClient):
        _, write_api = write_once(make_backend(), MockClient, data=SHELLY_PAYLOAD)
        assert fields_of(only_point(write_api)) == {
            "power": 75.0, "energy": 1234.0, "status": True, "overtemperature": False,
        }

    def test_bucket_and_precision_passed_to_write(self, MockClient):
        _, write_api = write_once(make_backend(bucket="mybucket"), MockClient)
        kwargs = write_api.write.call_args.kwargs
        assert kwargs["bucket"] == "mybucket"
        assert kwargs["write_precision"] == "ns"

    def test_empty_payload_is_not_written(self, MockClient):
        backend = make_backend()
        backend.write_device_data(StubDevice(name="d1"), {})
        # Nothing to write means nothing to connect to either.
        MockClient.assert_not_called()

    def test_empty_payload_warns_once_per_device(self, MockClient, caplog):
        backend = make_backend()
        device = StubDevice(name="d1")
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                backend.write_device_data(device, {})
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "no storable fields" in warnings[0].message

    def test_values_are_snapshotted_not_aliased(self, MockClient):
        # data IS device.data, which keeps being mutated in place after the call.
        backend = make_backend()
        device = StubDevice(name="d1")
        device.data = {"power": "10"}
        backend.write_device_data(device, device.data)
        device.data["power"] = "999"
        assert fields_of(only_point(wire(MockClient)[1]))["power"] == 10.0

    def test_client_construction_failure_disables_backend_and_logs_once(self, MockClient, caplog):
        MockClient.side_effect = RuntimeError("bad url")
        backend = make_backend()
        with caplog.at_level(logging.ERROR):
            for i in range(5):
                backend.write_device_data(StubDevice(name=f"d{i}"), {"power": 1})
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "disabled for the rest of the run" in errors[0].message
        # Latched: no repeated attempts to construct it either.
        MockClient.assert_called_once()

    def test_write_api_construction_failure_disables_backend(self, MockClient, caplog):
        client, _ = wire(MockClient)
        client.write_api.side_effect = RuntimeError("nope")
        backend = make_backend()
        with caplog.at_level(logging.ERROR):
            for i in range(3):
                backend.write_device_data(StubDevice(name=f"d{i}"), {"power": 1})
        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
        assert client.write_api.call_count == 1

    def test_concurrent_writes_are_serialised(self, MockClient):
        # MQTT spawns one thread per inbound message.
        backend = make_backend()
        _, write_api = wire(MockClient)
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                backend.write_device_data(StubDevice(name=f"d{index}"), {"power": index})
            except BaseException as e:  # pragma: no cover - only on failure
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert write_api.write.call_count == 20
        MockClient.assert_called_once()

    def test_mutation_during_flatten_does_not_raise(self, MockClient):
        class MutatingDict(dict):
            def items(self):
                snapshot = list(super().items())
                self["late"] = 1  # a concurrent receive() landing mid-walk
                return snapshot

        backend = make_backend()
        backend.write_device_data(StubDevice(name="d1"), MutatingDict({"power": 1}))
        assert fields_of(only_point(wire(MockClient)[1])) == {"power": 1.0}


@patch("storage.influxdb.InfluxDBClient")
class TestMetricSource:
    """A device that names its own readings overrides the generic flattener — the only
    way to key data whose structure encodes meaning by position."""

    def test_p1_writes_obis_named_fields_not_positional_ones(self, MockClient):
        p1 = P1(name="p1", connector_options={"name": "c", "protocol": "mqtt"},
                listener_options={}, controller_options={})
        p1.data = {
            "model_id": "ISK",
            "data": [{
                "obis": {"medium": 1, "channel": 0, "class": 1, "instance": 8, "attribute": 1},
                "data": [{"value": 1234.567, "unit": "kWh"}],
            }],
        }
        make_backend().write_device_data(p1, p1.data)
        fields = fields_of(only_point(wire(MockClient)[1]))
        assert fields == {"energy_import_t1_kwh": 1234.567}
        assert not any(key.startswith("data.") for key in fields)

    def test_metrics_still_go_through_the_flattener(self, MockClient):
        # The capability must not bypass bool-before-int, float coercion or NaN dropping.
        device = MetricStubDevice(name="d1")
        device.metrics = {"a": "75", "b": True, "c": 1, "d": float("nan")}
        make_backend().write_device_data(device, {"ignored": 1})
        assert fields_of(only_point(wire(MockClient)[1])) == {"a": 75.0, "b": True, "c": 1.0}

    def test_metrics_win_over_the_raw_payload(self, MockClient):
        device = MetricStubDevice(name="d1")
        device.metrics = {"named": 1.0}
        make_backend().write_device_data(device, {"positional": {"0": 9}})
        assert fields_of(only_point(wire(MockClient)[1])) == {"named": 1.0}

    def test_a_device_without_the_capability_still_uses_the_flattener(self, MockClient):
        _, write_api = write_once(make_backend(), MockClient, data={"nested": {"x": 1}})
        assert fields_of(only_point(write_api)) == {"nested.x": 1.0}

    def test_empty_metrics_are_not_written(self, MockClient):
        p1 = P1(name="p1", connector_options={"name": "c", "protocol": "mqtt"},
                listener_options={}, controller_options={})
        p1.data = {}
        # Authoritative: it never falls back to positional keys behind the user's back.
        make_backend().write_device_data(p1, {"model_id": "ISK"})
        MockClient.assert_not_called()


@patch("storage.influxdb.InfluxDBClient")
class TestWriteAlgorithmDecision:
    def test_written_to_the_decisions_measurement(self, MockClient):
        backend = make_backend()
        backend.write_algorithm_decision("auto_toggle", "shelly1", "on")
        assert measurement_of(only_point(wire(MockClient)[1])) == "algorithm_decisions"

    def test_custom_decisions_measurement(self, MockClient):
        backend = make_backend(decisions_measurement="decisions")
        backend.write_algorithm_decision("auto_toggle", "shelly1", "on")
        assert measurement_of(only_point(wire(MockClient)[1])) == "decisions"

    def test_tags_and_fields(self, MockClient):
        backend = make_backend(tags={"site": "site-a"})
        backend.write_algorithm_decision("auto_toggle", "shelly1", "on")
        point = only_point(wire(MockClient)[1])
        assert tags_of(point) == {"algorithm": "auto_toggle", "device": "shelly1", "site": "site-a"}
        # The numeric companion is what lets Grafana count decisions.
        assert fields_of(point) == {"command": "on", "value": 1.0}


@patch("storage.influxdb.InfluxDBClient")
class TestTimestamp:
    def test_naive_wall_clock_is_converted_to_utc(self, MockClient):
        _, write_api = write_once(make_backend(), MockClient)
        moment = only_point(write_api)._time
        assert moment.tzinfo is not None
        assert moment.utcoffset() == timedelta(0)

    def test_replay_event_time_overrides_wall_clock(self, MockClient):
        replayed = datetime(2024, 6, 1, 12, 0, 0)  # naive local, as parsed from a replay CSV
        SimulationClock().set_event_time(replayed)
        _, write_api = write_once(make_backend(), MockClient)
        # influxdb-client would stamp a naive value as UTC, shifting it by the local offset.
        assert only_point(write_api)._time == replayed.astimezone(timezone.utc)

    def test_aware_event_time_passes_through(self, MockClient):
        aware = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        SimulationClock().set_event_time(aware)
        _, write_api = write_once(make_backend(), MockClient)
        assert only_point(write_api)._time == aware

    def test_device_data_uses_the_event_clock_not_the_step_clock(self, MockClient):
        # The whole point of splitting the clock: a reading dispatched during timestep T
        # is stamped T, not the previously committed step.
        clock = SimulationClock()
        clock.publish_step(datetime(2024, 6, 1, 12, 0, 0))
        clock.set_event_time(datetime(2024, 6, 1, 12, 15, 0))
        _, write_api = write_once(make_backend(), MockClient)
        assert only_point(write_api)._time == datetime(2024, 6, 1, 12, 15, 0).astimezone(timezone.utc)

    def test_decisions_use_the_step_clock_not_the_event_clock(self, MockClient):
        # A decision computed while processing T belongs to T even if dispatch moved on.
        clock = SimulationClock()
        clock.publish_step(datetime(2024, 6, 1, 12, 0, 0))
        clock.set_event_time(datetime(2024, 6, 1, 12, 15, 0))
        backend = make_backend()
        backend.write_algorithm_decision("auto_toggle", "shelly1", "on")
        _, write_api = wire(MockClient)
        assert only_point(write_api)._time == datetime(2024, 6, 1, 12, 0, 0).astimezone(timezone.utc)

    def test_event_time_falls_back_to_the_step_clock(self, MockClient):
        # A third-party connector that only publishes steps still stamps sensibly.
        SimulationClock().publish_step(datetime(2024, 6, 1, 12, 0, 0))
        _, write_api = write_once(make_backend(), MockClient)
        assert only_point(write_api)._time == datetime(2024, 6, 1, 12, 0, 0).astimezone(timezone.utc)

    def test_point_precision_is_nanoseconds(self, MockClient):
        # Influx dedupes on (measurement, tags, field, time): a coarser precision would
        # silently drop two messages landing in the same millisecond.
        _, write_api = write_once(make_backend(), MockClient)
        assert only_point(write_api)._write_precision == "ns"


class TestCallbacks:
    def test_error_logs_error_then_warning(self, caplog):
        backend = make_backend()
        with caplog.at_level(logging.WARNING):
            backend._on_write_error(("b", "o", "ns"), "payload", Exception("boom"))
            backend._on_write_error(("b", "o", "ns"), "payload", Exception("boom"))
        levels = [r.levelno for r in caplog.records]
        assert levels == [logging.ERROR, logging.WARNING]
        assert "that data is lost" in caplog.records[0].message

    def test_error_names_the_bucket(self, caplog):
        backend = make_backend(bucket="mybucket")
        with caplog.at_level(logging.ERROR):
            backend._on_write_error(("b", "o", "ns"), "payload", Exception("boom"))
        assert "mybucket" in caplog.text

    def test_error_logs_truncated_payload_at_debug(self, caplog):
        backend = make_backend()
        with caplog.at_level(logging.DEBUG):
            backend._on_write_error(("b", "o", "ns"), "x" * 5000, Exception("boom"))
        debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert len(debug) == 1
        assert len(debug[0].message) < 700

    def test_success_after_error_logs_recovery(self, caplog):
        backend = make_backend()
        with caplog.at_level(logging.INFO):
            backend._on_write_error(("b", "o", "ns"), "payload", Exception("boom"))
            backend._on_write_success(("b", "o", "ns"), "payload")
        assert "recovered" in caplog.text

    def test_success_without_prior_error_is_quiet(self, caplog):
        backend = make_backend()
        with caplog.at_level(logging.INFO):
            backend._on_write_success(("b", "o", "ns"), "payload")
        assert "recovered" not in caplog.text

    def test_field_type_conflict_gets_actionable_advice(self, caplog):
        # Verified live against InfluxDB 2.7: two devices writing `payload` as a JSON
        # string and as a number collide, and the bare 422 explains nothing.
        backend = make_backend()
        conflict = Exception('field type conflict: input field "payload" on measurement '
                             '"device_data" is type float, already exists as type string')
        with caplog.at_level(logging.ERROR):
            backend._on_write_error(("b", "o", "ns"), "payload", conflict)
        assert "parse_numeric_strings" in caplog.text
        assert "Only the conflicting points are dropped" in caplog.text

    def test_other_errors_get_no_conflict_advice(self, caplog):
        backend = make_backend()
        with caplog.at_level(logging.ERROR):
            backend._on_write_error(("b", "o", "ns"), "payload", Exception("connection refused"))
        assert "parse_numeric_strings" not in caplog.text

    def test_retry_logs_warning(self, caplog):
        backend = make_backend()
        with caplog.at_level(logging.WARNING):
            backend._on_write_retry(("b", "o", "ns"), "payload", Exception("later"))
        assert "Retrying" in caplog.text


@patch("storage.influxdb.InfluxDBClient")
class TestClose:
    def test_close_flushes_before_closing_the_client(self, MockClient):
        backend = make_backend()
        client, write_api = write_once(backend, MockClient)
        order: list[str] = []
        write_api.close.side_effect = lambda: order.append("flush")
        client.close.side_effect = lambda: order.append("client")
        backend.close()
        assert order == ["flush", "client"]

    def test_close_is_idempotent(self, MockClient):
        # StorageManager.close_all() guards this too, but a backend must not rely on it.
        backend = make_backend()
        client, write_api = write_once(backend, MockClient)
        backend.close()
        backend.close()
        write_api.close.assert_called_once()
        client.close.assert_called_once()

    def test_close_without_any_write_builds_nothing(self, MockClient):
        make_backend().close()
        MockClient.assert_not_called()

    def test_flush_failure_still_closes_the_client(self, MockClient):
        backend = make_backend()
        client, write_api = write_once(backend, MockClient)
        write_api.close.side_effect = RuntimeError("unreachable")
        backend.close()  # must not raise
        client.close.assert_called_once()

    def test_write_after_close_is_dropped_and_warns_once(self, MockClient, caplog):
        # MQTT's dispatch threads are daemons the shutdown join may abandon, so late
        # writers are expected.
        backend = make_backend()
        _, write_api = write_once(backend, MockClient)
        backend.close()
        write_api.write.reset_mock()
        with caplog.at_level(logging.WARNING):
            for i in range(3):
                backend.write_device_data(StubDevice(name=f"d{i}"), {"power": 1})
        write_api.write.assert_not_called()
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    def test_read_after_close_returns_empty(self, MockClient):
        backend = make_backend()
        write_once(backend, MockClient)
        backend.close()
        assert backend.read("d1", datetime(2020, 1, 1), datetime(2030, 1, 1)) == []


@patch("storage.influxdb.InfluxDBClient")
class TestRead:
    @staticmethod
    def _query_mock(MockClient, tables):
        client, _ = wire(MockClient)
        client.query_api.return_value.query.return_value = tables
        return client.query_api.return_value.query

    def test_device_name_is_a_bind_parameter_not_interpolated(self, MockClient):
        query = self._query_mock(MockClient, [])
        make_backend().read('evil" or true or "', datetime(2020, 1, 1), datetime(2030, 1, 1))
        flux = query.call_args.args[0]
        assert "evil" not in flux
        assert query.call_args.kwargs["params"]["_device"] == 'evil" or true or "'

    def test_query_pivots_and_sorts(self, MockClient):
        query = self._query_mock(MockClient, [])
        make_backend().read("d1", datetime(2020, 1, 1), datetime(2030, 1, 1))
        flux = query.call_args.args[0]
        assert "pivot(" in flux and "sort(" in flux

    def test_bucket_and_measurement_are_bound(self, MockClient):
        query = self._query_mock(MockClient, [])
        make_backend(bucket="b1", measurement="m1").read("d1", datetime(2020, 1, 1), datetime(2030, 1, 1))
        params = query.call_args.kwargs["params"]
        assert params["_bucket"] == "b1"
        assert params["_measurement"] == "m1"

    def test_naive_bounds_are_converted_to_utc(self, MockClient):
        query = self._query_mock(MockClient, [])
        start, end = datetime(2024, 6, 1, 0, 0), datetime(2024, 6, 2, 0, 0)
        make_backend().read("d1", start, end)
        params = query.call_args.kwargs["params"]
        assert params["_start"] == start.astimezone(timezone.utc)
        assert params["_stop"] == end.astimezone(timezone.utc)

    def test_records_are_mapped_to_the_csv_row_shape(self, MockClient):
        moment = datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc)
        self._query_mock(MockClient, [flux_table({
            "result": "_result", "table": 0,
            "_start": moment, "_stop": moment, "_time": moment,
            "_measurement": "device_data", "device": "d1", "device_kind": "ShellyPlug",
            "power": 75.0, "status": True,
        })])
        rows = make_backend().read("d1", datetime(2020, 1, 1), datetime(2030, 1, 1))
        assert rows == [{
            "timestamp": moment.isoformat(),
            "device_name": "d1",
            "data": {"power": 75.0, "status": True},
        }]

    def test_static_tag_columns_are_stripped_from_data(self, MockClient):
        moment = datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc)
        self._query_mock(MockClient, [flux_table({
            "result": "_result", "table": 0, "_time": moment,
            "_measurement": "device_data", "device": "d1", "site": "site-a", "power": 1.0,
        })])
        rows = make_backend(tags={"site": "site-a"}).read("d1", datetime(2020, 1, 1), datetime(2030, 1, 1))
        assert rows[0]["data"] == {"power": 1.0}

    def test_null_pivot_cells_are_dropped(self, MockClient):
        moment = datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc)
        self._query_mock(MockClient, [flux_table({
            "_time": moment, "device": "d1", "power": 1.0, "energy": None,
        })])
        rows = make_backend().read("d1", datetime(2020, 1, 1), datetime(2030, 1, 1))
        assert rows[0]["data"] == {"power": 1.0}

    def test_underscore_prefixed_device_field_survives(self, MockClient):
        # The reserved set is explicit rather than a "starts with _" heuristic.
        moment = datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc)
        self._query_mock(MockClient, [flux_table({"_time": moment, "device": "d1", "_foo": 3.0})])
        rows = make_backend().read("d1", datetime(2020, 1, 1), datetime(2030, 1, 1))
        assert rows[0]["data"] == {"_foo": 3.0}

    def test_multiple_tables_and_records(self, MockClient):
        moment = datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc)
        self._query_mock(MockClient, [
            flux_table({"_time": moment, "device": "d1", "power": 1.0},
                       {"_time": moment, "device": "d1", "power": 2.0}),
            flux_table({"_time": moment, "device": "d1", "power": 3.0}),
        ])
        rows = make_backend().read("d1", datetime(2020, 1, 1), datetime(2030, 1, 1))
        assert [row["data"]["power"] for row in rows] == [1.0, 2.0, 3.0]

    def test_empty_result_returns_empty_list(self, MockClient):
        self._query_mock(MockClient, [])
        assert make_backend().read("d1", datetime(2020, 1, 1), datetime(2030, 1, 1)) == []

    def test_query_failure_is_logged_and_returns_empty(self, MockClient, caplog):
        client, _ = wire(MockClient)
        client.query_api.return_value.query.side_effect = RuntimeError("unreachable")
        with caplog.at_level(logging.ERROR):
            rows = make_backend().read("d1", datetime(2020, 1, 1), datetime(2030, 1, 1))
        assert rows == []
        assert "query for device 'd1' failed" in caplog.text

    def test_read_when_client_cannot_be_built(self, MockClient):
        MockClient.side_effect = RuntimeError("bad url")
        assert make_backend().read("d1", datetime(2020, 1, 1), datetime(2030, 1, 1)) == []


class TestOptionsContract:
    """The reverse of tests/test_config.py::TestShippedStorageSchemas.

    That test asserts schema keys are a subset of the constructor parameters. This one
    asserts the other direction: a constructor kwarg with no schema entry would pass
    there and then be rejected at runtime by "additionalProperties": false.
    """

    def test_every_constructor_option_is_documented_in_the_schema(self):
        with open(SCHEMA_PATH) as f:
            schema = json.load(f)
        params = set(inspect.signature(InfluxDBBackend.__init__).parameters) - {"self", "name"}
        undocumented = params - set(schema["properties"])
        assert not undocumented, f"options missing from influxdb.schema.json: {undocumented}"
