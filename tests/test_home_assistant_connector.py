"""Home Assistant WebSocket connector: handshake, dispatch, control, and the stop() bound."""
import json
import logging
import threading
from unittest.mock import MagicMock, patch

import pytest

# websocket-client is not in requirements.txt on purpose (see requirements-homeassistant.txt).
# A hard import here aborts collection for the WHOLE suite, not just this file — which is
# exactly what tests/test_storage_influxdb.py did before it was guarded.
pytest.importorskip("websocket", reason="pip install -r requirements-homeassistant.txt")

from websocket import WebSocketException, WebSocketTimeoutException

from connectors.home_assistant import HomeAssistantConnector
from tests.conftest import StubDevice, assert_stops, run_in_thread, wait_until

STATE = {
    "entity_id": "sensor.grid",
    "state": "1234.5",
    "attributes": {"unit_of_measurement": "kWh", "friendly_name": "Grid"},
    "last_updated": "2026-08-14T10:00:00+00:00",
}


class FakeWebSocket:
    """A scripted socket: hands out frames in order, then blocks until released.

    Blocking rather than raising at the end of the script is what makes the stop() test
    real — the connector must be sitting inside recv() when abort() arrives, which is the
    situation the abort()-not-close() choice exists for. `abort()` releases the wait and
    makes the pending recv() raise, exactly as shutting the socket down does.
    """

    def __init__(self, frames=(), block_when_empty: bool = True):
        self.frames = list(frames)
        self.sent: list[dict] = []
        self.closed = False
        self.aborted = False
        self.connected = True
        self._block_when_empty = block_when_empty
        self._released = threading.Event()

    def recv(self):
        while self.frames:
            frame = self.frames.pop(0)
            if isinstance(frame, Exception):
                raise frame
            return frame
        if not self._block_when_empty:
            raise WebSocketTimeoutException("quiet")
        self._released.wait(2)
        raise WebSocketException("socket shut down")

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def close(self, **kwargs):
        self.closed = True
        self._released.set()

    def abort(self):
        self.aborted = True
        self._released.set()

    def commands(self, message_type: str) -> list[dict]:
        return [message for message in self.sent if message.get("type") == message_type]


def handshake(*extra, ok: bool = True) -> list:
    auth = {"type": "auth_ok", "ha_version": "2026.8"} if ok else {"type": "auth_invalid", "message": "Invalid access token"}
    return [json.dumps({"type": "auth_required", "ha_version": "2026.8"}), json.dumps(auth), *extra]


def result(message_id: int, payload=None) -> str:
    return json.dumps({"id": message_id, "type": "result", "success": True, "result": payload})


def event(state: dict) -> str:
    return json.dumps({
        "id": 1, "type": "event",
        "event": {"event_type": "state_changed", "time_fired": "2026-08-14T10:00:01+00:00",
                  "data": {"entity_id": state["entity_id"], "old_state": None, "new_state": state}},
    })


def make_connector(**kwargs) -> HomeAssistantConnector:
    defaults = dict(name="HA 1", host="ha.local", access_token="tok", receive_timeout=1)
    defaults.update(kwargs)
    return HomeAssistantConnector(**defaults)


def make_device(name: str = "grid", entity_id="sensor.grid", **kwargs) -> StubDevice:
    return StubDevice(name=name, listener_options={"entity_id": entity_id}, **kwargs)


def run_session(connector, socket, until):
    """Drive start() against a fake socket until `until` holds, then stop it."""
    with patch("connectors.home_assistant.create_connection", return_value=socket):
        thread = run_in_thread(connector.start)
        held = wait_until(until)
        assert_stops(connector, thread)
    return held


class TestUrl:
    def test_url_is_built_from_host_and_port(self):
        assert make_connector(port="8123").url == "ws://ha.local:8123/api/websocket"

    def test_ssl_selects_wss(self):
        assert make_connector(ssl=True).url.startswith("wss://")

    def test_explicit_url_wins(self):
        connector = make_connector(url="wss://ha.example.org/proxy/api/websocket", host="ignored")
        assert connector.url == "wss://ha.example.org/proxy/api/websocket"

    def test_string_port_is_coerced(self):
        """Config validates the plugin schema BEFORE resolving ${VAR}."""
        assert make_connector(port="8124").url.endswith(":8124/api/websocket")

    def test_receive_timeout_floor_is_enforced(self, caplog):
        with caplog.at_level(logging.WARNING):
            connector = make_connector(receive_timeout=0)
        assert connector.receive_timeout == 30.0

    def test_zero_reconnect_backoff_is_refused(self):
        assert make_connector(reconnect_backoff_seconds=0).reconnect_backoff_seconds == 1.0


class TestRouting:
    def test_entity_id_string_and_list_both_route(self):
        connector = make_connector()
        one = make_device("a", "sensor.one")
        many = make_device("b", ["sensor.two", "sensor.three"])
        connector.inject_devices({"a": one, "b": many})
        assert set(connector._by_entity) == {"sensor.one", "sensor.two", "sensor.three"}

    def test_two_devices_may_watch_one_entity(self):
        """A raw view and an energy view of one sensor is legitimate; keying one device per
        entity would silently drop the second."""
        connector = make_connector()
        first, second = make_device("a"), make_device("b")
        connector.inject_devices({"a": first, "b": second})
        assert connector._by_entity["sensor.grid"] == [first, second]

    def test_device_without_entity_id_warns(self, caplog):
        connector = make_connector()
        with caplog.at_level(logging.WARNING):
            connector.inject_devices({"x": StubDevice(name="x")})
        assert any("has no listener_options.entity_id" in r.message for r in caplog.records)

    def test_write_only_device_is_not_routed(self):
        connector = make_connector()
        connector.inject_devices({"x": StubDevice(name="x", is_readable=False, is_writable=True,
                                                  listener_options={"entity_id": "switch.x"})})
        assert connector._by_entity == {}


class TestHandshake:
    def test_auth_frame_carries_the_token_and_no_id(self):
        connector = make_connector()
        connector.inject_devices({"grid": make_device()})
        socket = FakeWebSocket(handshake())
        run_session(connector, socket, lambda: socket.commands("get_states"))

        auth = socket.sent[0]
        assert auth == {"type": "auth", "access_token": "tok"}, "ids exist only in the command phase"

    def test_subscribe_precedes_the_snapshot(self):
        """Lossless ordering: a snapshot taken after the subscription can only be as new as
        the subscription point, so nothing that changes in between is missed."""
        connector = make_connector()
        connector.inject_devices({"grid": make_device()})
        socket = FakeWebSocket(handshake())
        run_session(connector, socket, lambda: socket.commands("get_states"))

        types = [message["type"] for message in socket.sent]
        assert types[:3] == ["auth", "subscribe_events", "get_states"]
        assert socket.sent[1]["event_type"] == "state_changed"

    def test_command_ids_are_strictly_increasing(self):
        connector = make_connector()
        connector.inject_devices({"grid": make_device()})
        socket = FakeWebSocket(handshake())
        run_session(connector, socket, lambda: socket.commands("get_states"))

        ids = [message["id"] for message in socket.sent if "id" in message]
        assert ids == sorted(set(ids)), "Home Assistant refuses a reused or out-of-order id"

    def test_auth_invalid_returns_without_retrying(self, caplog):
        """A bad token is deterministic: retrying achieves nothing and Home Assistant
        IP-bans repeated failed logins, which would lock the EMS out after a fix."""
        connector = make_connector()
        connector.inject_devices({"grid": make_device()})
        socket = FakeWebSocket(handshake(ok=False))

        with caplog.at_level(logging.CRITICAL):
            with patch("connectors.home_assistant.create_connection", return_value=socket) as create:
                thread = run_in_thread(connector.start)
                thread.join(2)

        assert not thread.is_alive(), "auth_invalid must end the worker, not loop"
        assert create.call_count == 1, "no retry"
        assert any("will NOT retry" in r.message for r in caplog.records)

    def test_missing_token_never_opens_a_socket(self, caplog):
        connector = make_connector(access_token=None)
        with caplog.at_level(logging.CRITICAL):
            with patch("connectors.home_assistant.create_connection") as create:
                connector.start()
        create.assert_not_called()
        assert any("No access_token configured" in r.message for r in caplog.records)

    def test_verify_ssl_false_disables_verification(self):
        connector = make_connector(url="wss://ha.local/api/websocket", verify_ssl=False)
        connector.inject_devices({"grid": make_device()})
        socket = FakeWebSocket(handshake())
        with patch("connectors.home_assistant.create_connection", return_value=socket) as create:
            thread = run_in_thread(connector.start)
            wait_until(lambda: create.called)
            assert_stops(connector, thread)
        assert create.call_args.kwargs["sslopt"]["check_hostname"] is False

    def test_multithread_is_requested_explicitly(self):
        """It decides whether two algorithm threads can interleave frames on the wire; an
        upstream default change would corrupt the protocol silently."""
        connector = make_connector()
        connector.inject_devices({"grid": make_device()})
        socket = FakeWebSocket(handshake())
        with patch("connectors.home_assistant.create_connection", return_value=socket) as create:
            thread = run_in_thread(connector.start)
            wait_until(lambda: create.called)
            assert_stops(connector, thread)
        assert create.call_args.kwargs["enable_multithread"] is True


class TestDispatch:
    def test_snapshot_seeds_every_watched_device(self):
        connector = make_connector()
        device = make_device()
        device.receive = MagicMock(return_value=True)
        connector.inject_devices({"grid": device})
        socket = FakeWebSocket(handshake(result(1), result(2, [STATE])))

        assert run_session(connector, socket, lambda: device.receive.called)

        entity_id, payload = device.receive.call_args.args
        assert entity_id == "sensor.grid"
        assert json.loads(payload) == STATE, "the bare state object, not the event envelope"

    def test_state_changed_event_reaches_the_device(self):
        connector = make_connector()
        device = make_device()
        device.receive = MagicMock(return_value=True)
        connector.inject_devices({"grid": device})
        socket = FakeWebSocket(handshake(result(1), result(2, []), event({**STATE, "state": "9"})))

        assert run_session(connector, socket, lambda: device.receive.called)
        assert json.loads(device.receive.call_args.args[1])["state"] == "9"

    def test_receive_result_is_forwarded_to_the_framework_hook(self):
        connector = make_connector()
        device = make_device()
        device.receive = MagicMock(return_value=False)
        connector.on_device_data_received = MagicMock()
        connector.inject_devices({"grid": device})
        socket = FakeWebSocket(handshake(result(1), result(2, [STATE])))

        run_session(connector, socket, lambda: connector.on_device_data_received.called)
        connector.on_device_data_received.assert_called_with(device, False)

    def test_removed_entity_with_null_new_state_publishes_nothing(self):
        connector = make_connector()
        device = make_device()
        device.receive = MagicMock()
        connector.inject_devices({"grid": device})
        removal = json.dumps({"id": 1, "type": "event", "event": {
            "event_type": "state_changed",
            "data": {"entity_id": "sensor.grid", "old_state": STATE, "new_state": None}}})
        socket = FakeWebSocket(handshake(result(1), result(2, []), removal))

        run_session(connector, socket, lambda: socket.commands("get_states"))
        device.receive.assert_not_called()

    def test_unwatched_entity_is_ignored(self):
        connector = make_connector()
        device = make_device()
        device.receive = MagicMock()
        connector.inject_devices({"grid": device})
        other = event({**STATE, "entity_id": "light.hallway"})
        socket = FakeWebSocket(handshake(result(1), result(2, []), other))

        run_session(connector, socket, lambda: socket.commands("get_states"))
        device.receive.assert_not_called()

    def test_a_raising_device_does_not_end_the_session(self, caplog):
        """The supervisor would restart the whole connector and lose the subscription."""
        connector = make_connector()
        first = make_device("bad")
        first.receive = MagicMock(side_effect=ValueError("boom"))
        second = make_device("good")
        second.receive = MagicMock(return_value=True)
        connector.inject_devices({"bad": first, "good": second})
        socket = FakeWebSocket(handshake(result(1), result(2, [STATE])))

        with caplog.at_level(logging.ERROR):
            assert run_session(connector, socket, lambda: second.receive.called)
        # Connector.deliver owns this message now — one boundary, one wording, for every
        # connector. It names the device and the routing key, and carries the traceback.
        assert any("raised on 'sensor.grid'" in r.message for r in caplog.records)
        assert any("boom" in r.message for r in caplog.records)

    def test_unreadable_frame_is_logged_not_fatal(self, caplog):
        connector = make_connector()
        device = make_device()
        device.receive = MagicMock(return_value=True)
        connector.inject_devices({"grid": device})
        socket = FakeWebSocket(handshake("}{not json", result(1), result(2, [STATE])))

        with caplog.at_level(logging.WARNING):
            assert run_session(connector, socket, lambda: device.receive.called)
        assert any("Unreadable frame" in r.message for r in caplog.records)

    def test_refused_command_is_reported_by_name(self, caplog):
        connector = make_connector()
        connector.inject_devices({"grid": make_device()})
        refusal = json.dumps({"id": 1, "type": "result", "success": False,
                              "error": {"code": "id_reuse", "message": "already used"}})
        socket = FakeWebSocket(handshake(refusal))

        with caplog.at_level(logging.WARNING):
            run_session(connector, socket, lambda: socket.commands("get_states"))
        assert any("Home Assistant refused" in r.message for r in caplog.records)


class TestKeepalive:
    def test_a_quiet_socket_is_pinged_then_declared_dead(self, caplog):
        """A half-open connection would otherwise keep every device marked connected
        forever, which is the worst failure mode for an EMS because it looks healthy."""
        connector = make_connector(receive_timeout=1, max_missed_pongs=1, reconnect_backoff_seconds=0.1)
        connector.inject_devices({"grid": make_device()})
        socket = FakeWebSocket(handshake(), block_when_empty=False)

        with caplog.at_level(logging.WARNING):
            with patch("connectors.home_assistant.create_connection", return_value=socket):
                thread = run_in_thread(connector.start)
                wait_until(lambda: socket.commands("ping"))
                assert_stops(connector, thread)

        assert socket.commands("ping"), "a quiet socket must be probed"
        assert any("assuming the connection is dead" in r.message for r in caplog.records)


class TestSend:
    def _switch(self, **controller) -> StubDevice:
        return StubDevice(name="boiler", is_writable=True, controller_options=controller)

    def _sent_call(self, connector, socket, device, command):
        with patch("connectors.home_assistant.create_connection", return_value=socket):
            thread = run_in_thread(connector.start)
            assert wait_until(lambda: socket.commands("get_states"))
            connector.send(device, command)
            assert_stops(connector, thread)
        calls = socket.commands("call_service")
        return calls[0] if calls else None

    def test_on_and_off_need_no_controller_config_beyond_entity_id(self):
        connector = make_connector()
        connector.inject_devices({"grid": make_device()})
        socket = FakeWebSocket(handshake())
        call = self._sent_call(connector, socket, self._switch(entity_id="switch.boiler"), "on")

        assert call["domain"] == "homeassistant", "the domain-agnostic service covers every domain"
        assert call["service"] == "turn_on"
        assert call["target"] == {"entity_id": "switch.boiler"}

    def test_declared_domain_is_used(self):
        connector = make_connector()
        connector.inject_devices({"grid": make_device()})
        socket = FakeWebSocket(handshake())
        call = self._sent_call(connector, socket, self._switch(entity_id="light.k", domain="light"), "off")
        assert (call["domain"], call["service"]) == ("light", "turn_off")

    def test_an_arbitrary_token_maps_to_a_full_service_call(self):
        """{domain, service_on, service_off} would dead-end at climate.set_temperature."""
        connector = make_connector()
        connector.inject_devices({"grid": make_device()})
        socket = FakeWebSocket(handshake())
        device = self._switch(entity_id="climate.living", commands={
            "boost": {"domain": "climate", "service": "set_temperature",
                      "service_data": {"temperature": 23}},
        })
        call = self._sent_call(connector, socket, device, "boost")

        assert call["domain"] == "climate"
        assert call["service"] == "set_temperature"
        assert call["service_data"] == {"temperature": 23}

    def test_a_command_may_carry_its_own_target(self):
        connector = make_connector()
        connector.inject_devices({"grid": make_device()})
        socket = FakeWebSocket(handshake())
        device = self._switch(commands={"go": {"service": "turn_on", "target": {"area_id": "kitchen"}}})
        call = self._sent_call(connector, socket, device, "go")
        assert call["target"] == {"area_id": "kitchen"}

    def test_unmapped_token_is_refused_never_guessed(self, caplog):
        connector = make_connector()
        connector.inject_devices({"grid": make_device()})
        socket = FakeWebSocket(handshake())
        with caplog.at_level(logging.WARNING):
            call = self._sent_call(connector, socket, self._switch(entity_id="switch.b"), "boost")
        assert call is None
        assert any("No controller_options.commands entry" in r.message for r in caplog.records)

    def test_no_entity_id_and_no_target_is_refused(self, caplog):
        connector = make_connector()
        connector.inject_devices({"grid": make_device()})
        socket = FakeWebSocket(handshake())
        with caplog.at_level(logging.WARNING):
            call = self._sent_call(connector, socket, self._switch(), "on")
        assert call is None
        assert any("names no target" in r.message for r in caplog.records)

    def test_send_while_disconnected_is_logged_not_raised(self, caplog):
        """send() runs on the ALGORITHM's thread and nothing in that chain catches."""
        connector = make_connector()
        with caplog.at_level(logging.ERROR):
            connector.send(self._switch(entity_id="switch.b"), "on")  # must not raise
        assert any("Could not send 'on'" in r.message for r in caplog.records)

    def test_concurrent_sends_allocate_unique_increasing_ids(self):
        """The id and the frame are written under ONE lock: allocating under one and
        writing under another lets two threads reorder, and Home Assistant then refuses a
        perfectly valid command with error code id_reuse."""
        connector = make_connector()
        connector.inject_devices({"grid": make_device()})
        socket = FakeWebSocket(handshake())
        device = self._switch(entity_id="switch.boiler")

        with patch("connectors.home_assistant.create_connection", return_value=socket):
            thread = run_in_thread(connector.start)
            assert wait_until(lambda: socket.commands("get_states"))
            senders = [run_in_thread(lambda: connector.send(device, "on")) for _ in range(12)]
            for sender in senders:
                sender.join(2)
            assert_stops(connector, thread)

        ids = [message["id"] for message in socket.commands("call_service")]
        assert len(ids) == 12
        assert len(set(ids)) == 12, "no id reuse under concurrency"
        assert ids == sorted(ids), "frames must reach the wire in id order"


class TestStop:
    def test_stop_aborts_the_socket_to_unblock_recv(self):
        """close() loops on recv_frame() for up to 3s WITHOUT the readlock, racing the
        thread already blocked in recv(); abort() is the documented wake-up primitive."""
        connector = make_connector()
        connector.inject_devices({"grid": make_device()})
        socket = FakeWebSocket(handshake())

        with patch("connectors.home_assistant.create_connection", return_value=socket):
            thread = run_in_thread(connector.start)
            assert wait_until(lambda: socket.commands("get_states"))
            assert_stops(connector, thread)

        assert socket.aborted, "stop() must abort(), not close()"
        assert socket.closed, "the owning thread still closes it in start()'s finally"

    def test_stop_before_start_is_a_no_op(self):
        connector = make_connector()
        connector.stop()  # must not raise: _ws only exists once start() ran
        assert connector.is_stopping()
