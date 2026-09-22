import logging
from threading import Event, active_count
from time import monotonic
from unittest.mock import MagicMock, patch

import pytest
from paho.mqtt.enums import MQTTProtocolVersion

from connectors.mqtt import _MAX_QUEUED_PER_DEVICE, MQTTConnector
from tests.conftest import STOP_TIMEOUT, StubDevice, wait_until


def make_mqtt_connector(**kwargs) -> MQTTConnector:
    defaults = dict(name="MQTT 1", host="broker.example", port=1883, version="3.1.1")
    defaults.update(kwargs)
    return MQTTConnector(**defaults)


class TestClientId:
    """F-4: client id must be derived from the connector name, not a shared 'EMS'."""

    @patch("connectors.mqtt.Client")
    def test_client_id_defaults_to_connector_name(self, MockClient):
        connector = make_mqtt_connector(name="MQTT 1")
        connector.start()
        MockClient.assert_called_once()
        assert MockClient.call_args.kwargs["client_id"] == "MQTT 1"

    @patch("connectors.mqtt.Client")
    def test_client_id_is_not_hardcoded_ems(self, MockClient):
        connector = make_mqtt_connector(name="broker-a")
        connector.start()
        assert MockClient.call_args.kwargs["client_id"] != "EMS"

    @patch("connectors.mqtt.Client")
    def test_explicit_client_id_overrides_name(self, MockClient):
        connector = make_mqtt_connector(name="MQTT 1", client_id="custom-id")
        connector.start()
        assert MockClient.call_args.kwargs["client_id"] == "custom-id"

    @patch("connectors.mqtt.Client")
    def test_distinct_connectors_get_distinct_client_ids(self, MockClient):
        make_mqtt_connector(name="broker-a").start()
        make_mqtt_connector(name="broker-b").start()
        client_ids = [call.kwargs["client_id"] for call in MockClient.call_args_list]
        assert client_ids == ["broker-a", "broker-b"]


class TestAuth:
    """F-4: username/password authentication."""

    @patch("connectors.mqtt.Client")
    def test_username_password_set_when_provided(self, MockClient):
        client = MockClient.return_value
        connector = make_mqtt_connector(username="user", password="secret")
        connector.start()
        client.username_pw_set.assert_called_once_with("user", "secret")

    @patch("connectors.mqtt.Client")
    def test_username_pw_not_set_when_absent(self, MockClient):
        client = MockClient.return_value
        connector = make_mqtt_connector()
        connector.start()
        client.username_pw_set.assert_not_called()


class TestTLS:
    """F-4: TLS support, including mutual (client-certificate) TLS."""

    @patch("connectors.mqtt.Client")
    def test_tls_set_when_tls_true(self, MockClient):
        client = MockClient.return_value
        connector = make_mqtt_connector(tls=True)
        connector.start()
        client.tls_set.assert_called_once_with(ca_certs=None, certfile=None, keyfile=None)

    @patch("connectors.mqtt.Client")
    def test_tls_set_with_ca_certs(self, MockClient):
        client = MockClient.return_value
        connector = make_mqtt_connector(ca_certs="/etc/ssl/ca.pem")
        connector.start()
        client.tls_set.assert_called_once_with(ca_certs="/etc/ssl/ca.pem", certfile=None, keyfile=None)

    @patch("connectors.mqtt.Client")
    def test_mutual_tls_passes_client_cert_and_key(self, MockClient):
        client = MockClient.return_value
        connector = make_mqtt_connector(certfile="/etc/ssl/client.crt", keyfile="/etc/ssl/client.key")
        connector.start()
        client.tls_set.assert_called_once_with(
            ca_certs=None, certfile="/etc/ssl/client.crt", keyfile="/etc/ssl/client.key"
        )

    @patch("connectors.mqtt.Client")
    def test_tls_not_set_when_unconfigured(self, MockClient):
        client = MockClient.return_value
        connector = make_mqtt_connector()
        connector.start()
        client.tls_set.assert_not_called()


class TestSubscriptions:
    """F-4: subscribe to device-declared topic filters, never the blanket '#'."""

    def test_subscribes_to_declared_filters_not_hash(self):
        connector = make_mqtt_connector()
        d1 = StubDevice(name="p1", listener_options={"pattern": "p1/.*", "subscription": "p1/#"})
        d2 = StubDevice(
            name="shelly",
            listener_options={"pattern": "shellies/.*", "subscription": ["shellies/relay/0", "shellies/relay/1"]},
        )
        connector.inject_devices({"p1": d1, "shelly": d2})

        client = MagicMock()
        connector.mqtt_client = client
        connector._on_connect(client, None, None, MagicMock(), None)

        client.subscribe.assert_called_once()
        subscribed_topics = {topic for topic, qos in client.subscribe.call_args.args[0]}
        assert subscribed_topics == {"p1/#", "shellies/relay/0", "shellies/relay/1"}
        assert "#" not in subscribed_topics

    def test_readable_device_without_subscription_warns_and_is_skipped(self, caplog):
        connector = make_mqtt_connector()
        device = StubDevice(name="p1", listener_options={"pattern": "p1/.*"})
        with caplog.at_level(logging.WARNING):
            connector.inject_devices({"p1": device})
        assert any("has no listener_options.subscription" in r.message for r in caplog.records)
        assert connector.subscriptions == set()

    def test_on_connect_does_not_fall_back_to_hash_when_no_subscriptions(self, caplog):
        connector = make_mqtt_connector()
        connector.inject_devices({})
        client = MagicMock()
        connector.mqtt_client = client
        with caplog.at_level(logging.WARNING):
            connector._on_connect(client, None, None, MagicMock(), None)
        client.subscribe.assert_not_called()
        assert any("No subscription filters declared" in r.message for r in caplog.records)

    def test_write_only_device_subscription_is_ignored(self):
        connector = make_mqtt_connector()
        device = StubDevice(
            name="switch", is_readable=False, is_writable=True,
            listener_options={"subscription": "should/be/ignored"},
        )
        connector.inject_devices({"switch": device})
        assert connector.subscriptions == set()

    def test_pattern_and_subscription_are_independent(self):
        connector = make_mqtt_connector()
        device = StubDevice(name="p1", listener_options={"pattern": "p1/.*", "subscription": "p1/#"})
        connector.inject_devices({"p1": device})
        # the routing regex is compiled into callbacks; the MQTT filter is kept separately
        assert any(regex.pattern == "p1/.*" for regex in connector.callbacks)
        assert connector.subscriptions == {"p1/#"}


class TestSend:
    """Regression: send() publishes to the device's controller topic."""

    def test_send_publishes_to_controller_topic(self):
        connector = make_mqtt_connector()
        client = MagicMock()
        connector.mqtt_client = client
        device = StubDevice(name="switch", is_writable=True, controller_options={"topic": "cmd/switch"})
        connector.send(device, "on")
        client.publish.assert_called_once_with("cmd/switch", "on")

    def test_missing_topic_warns_and_publishes_nothing(self, caplog):
        """send() runs on the ALGORITHM's thread and nothing in that chain catches, so
        publish(None, ...) used to raise paho's ValueError straight into a supervised
        algorithm worker and spend its restart budget."""
        connector = make_mqtt_connector()
        client = MagicMock()
        connector.mqtt_client = client
        device = StubDevice(name="switch", is_writable=True)
        with caplog.at_level(logging.WARNING):
            connector.send(device, "on")  # must not raise
        assert any("has no controller_options.topic" in r.message for r in caplog.records)
        client.publish.assert_not_called()

    def test_send_before_start_warns_instead_of_attribute_error(self, caplog):
        """main constructs every worker before starting any of them, so an algorithm can
        fire a control before start() has built mqtt_client."""
        connector = make_mqtt_connector()
        device = StubDevice(name="switch", is_writable=True, controller_options={"topic": "cmd/switch"})
        with caplog.at_level(logging.WARNING):
            connector.send(device, "on")  # must not raise AttributeError
        assert any("Not connected yet" in r.message for r in caplog.records)

    def test_transport_error_is_logged_not_raised(self, caplog):
        connector = make_mqtt_connector()
        client = MagicMock()
        client.publish.side_effect = OSError("broker gone")
        connector.mqtt_client = client
        device = StubDevice(name="switch", is_writable=True, controller_options={"topic": "cmd/switch"})
        with caplog.at_level(logging.ERROR):
            connector.send(device, "on")  # must not raise
        assert any("Error sending 'on' to 'switch'" in r.message for r in caplog.records)


class TestProtocolVersion:
    """MQTTVersion is a StrEnum, so an unknown value raises ValueError — and letting it
    escape would lose the broker connection and every device behind it for the whole run,
    over one unset environment variable."""

    def test_unset_env_reference_falls_back_to_the_default(self, caplog):
        """`"version": "${MQTT_VERSION}"` with the variable unset arrives as None."""
        with caplog.at_level(logging.WARNING):
            connector = make_mqtt_connector(version=None)  # must not raise
        assert any("Unknown MQTT version" in r.message for r in caplog.records)
        assert connector.protocol == MQTTProtocolVersion.MQTTv311

    def test_typo_falls_back_to_the_default(self, caplog):
        with caplog.at_level(logging.WARNING):
            connector = make_mqtt_connector(version="3.1.2")
        assert any("Unknown MQTT version" in r.message for r in caplog.records)
        assert connector.protocol == MQTTProtocolVersion.MQTTv311

    def test_declared_versions_are_honoured(self):
        assert make_mqtt_connector(version="5.0.0").protocol == MQTTProtocolVersion.MQTTv5
        assert make_mqtt_connector(version="3.1.0").protocol == MQTTProtocolVersion.MQTTv31


class TestOptionCoercion:
    """Config validates the plugin schema BEFORE resolving ${VAR}, so every scalar can arrive
    as a str. These two used to reach the transport uncoerced."""

    def test_a_string_port_is_coerced(self):
        """paho compares the port against an int inside connect(), so a "${MQTT_PORT}" was a
        TypeError on the supervised worker thread rather than a config warning."""
        assert make_mqtt_connector(port="8883").port == 8883

    def test_an_unresolved_port_falls_back_to_the_default(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert make_mqtt_connector(port=None).port == 1883

    def test_a_string_false_disables_tls(self):
        """The failure this prevents is silent, not loud: tls is only read for truthiness, so
        an operator disabling it through an environment variable got TLS anyway."""
        assert make_mqtt_connector(tls="false").tls is False
        assert make_mqtt_connector(tls="true").tls is True

    @patch("connectors.mqtt.Client")
    def test_a_string_false_does_not_reach_tls_set(self, MockClient):
        client = MockClient.return_value
        make_mqtt_connector(tls="false").start()
        client.tls_set.assert_not_called()


class TestStart:
    @patch("connectors.mqtt.Client")
    def test_no_host_returns_idle(self, MockClient, caplog):
        """paho raises ValueError('Invalid host') on an empty host, which the supervisor
        counts as a crash — and once the restart budget is spent the worker is finished,
        which is what main waits on. An unset ${MQTT_HOST} took the whole EMS down."""
        connector = make_mqtt_connector(host="")
        with caplog.at_level(logging.ERROR):
            connector.start()  # must return, not loop and not raise
        assert any("No host configured" in r.message for r in caplog.records)
        MockClient.assert_not_called()


@pytest.fixture
def wired():
    """A connector with routed StubDevices, wound down when the test ends.

    Stopping matters here in a way it does not elsewhere in this file: these tests start the
    real dispatcher threads, and one left parked inside a device that never returns would
    leak into every test after it.
    """
    connectors: list[MQTTConnector] = []

    def _make(*names: str, **device_kwargs):
        connector = make_mqtt_connector()
        devices = {
            name: StubDevice(
                name=name,
                listener_options={"pattern": f"{name}/.*", "subscription": f"{name}/#"},
                **device_kwargs,
            )
            for name in (names or ("meter",))
        }
        connector.inject_devices(devices)
        connectors.append(connector)
        return connector, devices

    yield _make
    for connector in connectors:
        connector.stop()


def publish(connector: MQTTConnector, topic: str, payload: str) -> None:
    """One broker message, delivered the way paho delivers it: straight into on_message."""
    connector.on_message(None, None, MagicMock(topic=topic, payload=payload.encode()))


def queued(connector: MQTTConnector, device_name: str) -> int:
    """How many payloads are waiting for a device. Reaches into the dispatcher on purpose:
    'the second message waited in the queue' and 'the second message ran concurrently' are
    indistinguishable from the outside, and that difference is the whole point here."""
    return connector._dispatchers[device_name].queue.qsize()


class TestDeviceContainment:
    """`_receive_and_notify` — the frame where a device exception stops.

    Nothing above it can catch: the dispatch thread is the top of its own stack and
    `on_message` returned the moment the payload was queued. Before `Connector.deliver`, a
    device exception went to `threading.excepthook` — a raw stderr traceback past every
    configured handler, no crash counted, nothing in /workers, and a device that silently
    stopped reporting. Containment here buys observability, not survival.
    """

    @staticmethod
    def _wired(**device_kwargs):
        connector = make_mqtt_connector()
        device = StubDevice(name="meter", listener_options={"pattern": "p1/.*", "subscription": "p1/#"}, **device_kwargs)
        connector.inject_devices({"meter": device})
        return connector, device

    def test_a_raising_device_does_not_end_the_session(self, caplog):
        connector, device = self._wired()
        device.receive = MagicMock(side_effect=ValueError("bad payload"))
        with caplog.at_level(logging.ERROR):
            connector._receive_and_notify(device, "p1/data", "telegram")  # must not raise
        assert any("raised on 'p1/data'" in r.message for r in caplog.records)

    def test_a_raising_device_is_still_marked_connected(self):
        connector, device = self._wired()
        device.receive = MagicMock(side_effect=ValueError("bad payload"))
        connector._receive_and_notify(device, "p1/data", "telegram")
        assert device.is_connected() is True
        assert device.is_data_ready() is False


class TestMessageRouting:
    """`on_message` → `_enqueue` → the device's dispatcher."""

    def test_a_message_reaches_the_matching_device(self, wired):
        connector, devices = wired()
        devices["meter"].receive = MagicMock(return_value=True)
        publish(connector, "meter/data", "telegram")
        assert wait_until(lambda: devices["meter"].receive.call_count == 1)
        devices["meter"].receive.assert_called_once_with("meter/data", "telegram")

    def test_a_message_matching_no_device_reaches_nobody(self, wired):
        connector, devices = wired()
        devices["meter"].receive = MagicMock()
        publish(connector, "shellies/plug", "on")
        assert connector._dispatchers == {}
        devices["meter"].receive.assert_not_called()

    def test_a_device_that_receives_nothing_costs_no_thread(self, wired):
        """Lazy by design: a connector whose start() returns on the no-host path would
        otherwise have spawned a thread per device for a transport that never delivers."""
        connector, devices = wired("meter", "plug")
        devices["meter"].receive = MagicMock(return_value=True)
        publish(connector, "meter/data", "telegram")
        assert wait_until(lambda: "meter" in connector._dispatchers)
        assert "plug" not in connector._dispatchers


class TestOrdering:
    """The correctness half. A thread per message let two payloads for one device run
    concurrently, and `Device.receive` writes `self.data` wholesale — so a meter could
    publish an older reading after a newer one, and `Connector.on_device_data_received`
    carried it into `DevicesManager` and the versioned CSV. One consumer per device makes
    arrival order a property of the mechanism instead of a property of timing."""

    def test_two_messages_for_one_device_are_parsed_in_arrival_order(self, wired):
        connector, devices = wired()
        seen: list[str] = []
        entered, release = Event(), Event()

        def receive(topic, payload):
            entered.set()
            release.wait(STOP_TIMEOUT)
            seen.append(payload)
            return True

        devices["meter"].receive = receive
        try:
            publish(connector, "meter/data", "first")
            assert wait_until(entered.is_set), "the dispatcher never picked the first payload up"
            publish(connector, "meter/data", "second")
            # The second payload is *waiting*, not running: under a thread per message it
            # would already have overtaken the first and written self.data behind it.
            assert wait_until(lambda: queued(connector, "meter") == 1)
            assert seen == []
        finally:
            release.set()
        assert wait_until(lambda: len(seen) == 2)
        assert seen == ["first", "second"]

    def test_a_slow_device_does_not_delay_another(self, wired):
        """What a single connector-wide dispatcher would have cost: one queue means one
        head, and a DSMR telegram with a CRC over a kilobyte blocks the plug behind it."""
        connector, devices = wired("meter", "plug")
        release = Event()
        devices["meter"].receive = lambda topic, payload: release.wait(STOP_TIMEOUT)
        devices["plug"].receive = MagicMock(return_value=True)
        try:
            publish(connector, "meter/data", "telegram")
            publish(connector, "plug/relay", "on")
            assert wait_until(lambda: devices["plug"].receive.call_count == 1)
        finally:
            release.set()


class TestBackpressure:
    """A full queue must cost readings, never the connection."""

    @staticmethod
    def _parked(connector, device):
        """Hold the dispatcher inside receive() and record what it eventually parses."""
        seen: list[str] = []
        entered, release = Event(), Event()

        def receive(topic, payload):
            entered.set()
            release.wait(STOP_TIMEOUT)
            seen.append(payload)
            return True

        device.receive = receive
        publish(connector, f"{device.name}/data", "parked")
        assert wait_until(entered.is_set), "the dispatcher never picked the first payload up"
        return seen, release

    def test_a_full_queue_does_not_block_the_paho_callback(self, wired):
        """`on_message` runs on the thread `loop_forever()` owns, and that thread also
        answers PINGREQ. A blocking `put()` here would stall keepalive for every device on
        the connector until the broker dropped the connection — the transport lost because
        one device parses slowly. The pre-fix failure mode for a naive queue is a hang, so
        the wall-clock bound below is the readable form of 'it returned at all'."""
        connector, devices = wired()
        _, release = self._parked(connector, devices["meter"])
        try:
            started = monotonic()
            for i in range(_MAX_QUEUED_PER_DEVICE * 2):
                publish(connector, "meter/data", str(i))
            elapsed = monotonic() - started
        finally:
            release.set()
        assert elapsed < STOP_TIMEOUT, f"on_message blocked for {elapsed:.2f}s behind a busy device"

    def test_the_thread_count_follows_the_device_list_not_the_traffic(self, wired):
        """The ceiling. A retained-message flush on reconnect used to be one OS thread per
        message; it is now one thread for the device, whatever the broker sends."""
        connector, devices = wired()
        _, release = self._parked(connector, devices["meter"])
        before = active_count()
        try:
            for i in range(_MAX_QUEUED_PER_DEVICE * 4):
                publish(connector, "meter/data", str(i))
            # `<=`, not `==`: a daemon thread left by an earlier test may retire during
            # this one. The claim is that nothing NEW was spawned, and 256 messages against
            # one device is where the old code would have proved otherwise.
            assert active_count() <= before
        finally:
            release.set()

    def test_a_full_queue_drops_the_oldest_and_keeps_the_rest_in_order(self, wired, caplog):
        """Oldest, not newest: the newest payload is the closest thing to the present state
        of the installation, and dropping it would do the very thing this dispatcher exists
        to prevent. Dropping from the head also leaves what remains in arrival order."""
        connector, devices = wired()
        seen, release = self._parked(connector, devices["meter"])
        overflow = 5
        with caplog.at_level(logging.WARNING):
            for i in range(_MAX_QUEUED_PER_DEVICE + overflow):
                publish(connector, "meter/data", str(i))
            release.set()
            assert wait_until(lambda: len(seen) == _MAX_QUEUED_PER_DEVICE + 1)

        assert seen == ["parked"] + [str(i) for i in range(overflow, _MAX_QUEUED_PER_DEVICE + overflow)]
        assert seen[-1] == str(_MAX_QUEUED_PER_DEVICE + overflow - 1), "the newest reading must survive"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, "edge-triggered: a chatty gateway must not write one WARNING per message"
        assert "receiving faster than it parses" in warnings[0].message

    def test_a_device_that_catches_up_is_reported_once(self, wired, caplog):
        connector, devices = wired()
        seen, release = self._parked(connector, devices["meter"])
        for i in range(_MAX_QUEUED_PER_DEVICE + 5):
            publish(connector, "meter/data", str(i))
        release.set()
        assert wait_until(lambda: len(seen) == _MAX_QUEUED_PER_DEVICE + 1)

        with caplog.at_level(logging.INFO):
            publish(connector, "meter/data", "caught-up")
            assert wait_until(lambda: len(seen) == _MAX_QUEUED_PER_DEVICE + 2)
        assert len([r for r in caplog.records if "is keeping up again" in r.message]) == 1


class TestDispatchShutdown:
    """`api/stoppable.py`'s contract, and `Supervisor.stop_all`'s reading of it.

    `SupervisedWorker.request_stop()` is called on *every* worker before *any* of them is
    joined, against one shared grace period. A stop() that waited for its dispatchers would
    spend that budget before the first join began — so the wait lives at the end of start(),
    on the thread the supervisor is already joining, and stop() only releases.
    """

    @staticmethod
    def _parked(connector, device):
        seen: list[str] = []
        entered, release = Event(), Event()

        def receive(topic, payload):
            entered.set()
            release.wait(STOP_TIMEOUT)
            seen.append(payload)
            return True

        device.receive = receive
        publish(connector, f"{device.name}/data", "parked")
        assert wait_until(entered.is_set), "the dispatcher never picked the first payload up"
        return seen, release

    def test_stop_returns_promptly_with_work_still_queued(self, wired):
        connector, devices = wired()
        _, release = self._parked(connector, devices["meter"])
        for i in range(20):
            publish(connector, "meter/data", str(i))
        try:
            started = monotonic()
            connector.stop()
            elapsed = monotonic() - started
        finally:
            release.set()
        # Well clear of the STOP_TIMEOUT the parked device holds for: a stop() that waited
        # on its dispatchers would land at ~2s, not under one.
        assert elapsed < 1.0, f"stop() blocked for {elapsed:.2f}s; it is called before any worker is joined"
        assert connector.is_stopping()

    def test_queued_work_is_abandoned_not_drained(self, wired, caplog):
        """A reading published while the EMS is shutting down has nobody left to act on it,
        and `Main.shutdown` closes the storage backends as soon as its join returns — so a
        write that did land would race a closing backend."""
        connector, devices = wired()
        seen, release = self._parked(connector, devices["meter"])
        for i in range(20):
            publish(connector, "meter/data", str(i))
        with caplog.at_level(logging.INFO):
            connector.stop()
        release.set()

        assert wait_until(lambda: not connector._dispatchers["meter"].thread.is_alive())
        assert seen == ["parked"], "the queued payloads must be abandoned, not replayed into storage"
        assert any("Abandoning 20 queued payload(s) for 'meter'" in r.message for r in caplog.records)

    def test_stop_unblocks_an_idle_dispatcher(self, wired):
        """An idle dispatcher is parked in a blocking `get()`, which the stop event alone
        cannot reach — the same reason MQTTConnector has to override stop() at all."""
        connector, devices = wired()
        devices["meter"].receive = MagicMock(return_value=True)
        publish(connector, "meter/data", "telegram")
        assert wait_until(lambda: devices["meter"].receive.call_count == 1)

        connector.stop()
        assert wait_until(lambda: not connector._dispatchers["meter"].thread.is_alive())

    def test_a_message_arriving_after_stop_is_dropped(self, wired):
        connector, devices = wired()
        devices["meter"].receive = MagicMock(return_value=True)
        connector.stop()
        publish(connector, "meter/data", "telegram")
        assert connector._dispatchers == {}, "a payload after stop() must not resurrect a dispatcher"
        devices["meter"].receive.assert_not_called()

    @patch("connectors.mqtt.Client")
    def test_start_winds_the_dispatchers_down_before_returning(self, MockClient, wired):
        connector, devices = wired()
        devices["meter"].receive = MagicMock(return_value=True)
        publish(connector, "meter/data", "telegram")
        assert wait_until(lambda: devices["meter"].receive.call_count == 1)

        connector.start()  # the mocked loop_forever() returns at once
        assert not connector._dispatchers["meter"].thread.is_alive()

    @patch("connectors.mqtt._DISPATCH_JOIN_TIMEOUT", 0.05)
    @patch("connectors.mqtt.Client")
    def test_a_wedged_dispatcher_is_reported_not_waited_on(self, MockClient, wired, caplog):
        """The same contract `Supervisor.stop_all` states for the workers themselves: a
        daemon thread still running at the deadline dies with the interpreter, and naming it
        is more use than hanging the shutdown on it."""
        connector, devices = wired()
        _, release = self._parked(connector, devices["meter"])
        try:
            with caplog.at_level(logging.WARNING):
                started = monotonic()
                connector.start()
                elapsed = monotonic() - started
        finally:
            release.set()
        assert elapsed < STOP_TIMEOUT
        assert any("did not finish within 0.05s" in r.message for r in caplog.records)
class TestSeamDefaults:
    """resolve_listener() and resolve_downlink() were extracted from inject_devices() and
    send() so connectors/lorawan.py can subclass this one. The parent must be
    behaviour-identical — these pin the default bodies."""

    def test_resolve_listener_returns_the_declared_options(self):
        connector = make_mqtt_connector()
        device = StubDevice(listener_options={"subscription": "p1/#", "pattern": "p1/.*"})
        assert connector.resolve_listener(device) == ("p1/#", "p1/.*")

    def test_resolve_listener_returns_none_for_each_absent_option(self):
        connector = make_mqtt_connector()
        assert connector.resolve_listener(StubDevice()) == (None, None)

    def test_a_missing_pattern_alone_does_not_warn(self, caplog):
        """The asymmetry is deliberate and pre-existing: a device may be routed by
        subscription alone, so only a missing subscription is worth a warning."""
        connector = make_mqtt_connector()
        with caplog.at_level(logging.WARNING):
            connector.inject_devices({"p1": StubDevice(name="p1", listener_options={"subscription": "p1/#"})})
        assert not any("pattern" in r.message for r in caplog.records)

    def test_resolve_downlink_returns_the_topic_and_the_payload_unchanged(self):
        connector = make_mqtt_connector()
        device = StubDevice(is_writable=True, controller_options={"topic": "cmd/switch"})
        assert connector.resolve_downlink(device, "on") == ("cmd/switch", "on")

    def test_duplicate_routing_patterns_are_reported(self, caplog):
        """re.compile is memoised, so an equal pattern string is the same dict key: the
        second device would silently replace the first and one of them would receive
        nothing for the life of the run."""
        connector = make_mqtt_connector()
        listener = {"pattern": "meter/.*", "subscription": "meter/#"}
        with caplog.at_level(logging.ERROR):
            connector.inject_devices({
                "a": StubDevice(name="a", listener_options=dict(listener)),
                "b": StubDevice(name="b", listener_options=dict(listener)),
            })
        assert any("same routing pattern" in r.message for r in caplog.records)
