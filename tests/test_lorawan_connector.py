"""LoRaWAN connector: topic synthesis, downlink envelopes, throttling, and the MQTT seams.

No importorskip — paho-mqtt is in requirements.txt, and the connector subclasses
MQTTConnector, so the transport is mocked exactly as tests/test_mqtt_connector.py mocks it:
@patch("connectors.mqtt.Client").
"""
import json
import logging
from unittest.mock import MagicMock, patch

from connectors.lorawan import LoRaWANConnector
from connectors.mqtt import MQTTConnector
from tests.conftest import StubDevice, wait_until


def make_connector(**kwargs) -> LoRaWANConnector:
    defaults = dict(name="LoRaWAN 1", host="lns.example", port=1883)
    defaults.update(kwargs)
    return LoRaWANConnector(**defaults)


def make_device(name: str = "meter", **listener) -> StubDevice:
    defaults = {"dev_eui": "70b3d57ed0001234"}
    defaults.update(listener)
    return StubDevice(name=name, listener_options=defaults)


def writable(name: str = "relay", listener=None, **controller) -> StubDevice:
    return StubDevice(
        name=name, is_writable=True,
        listener_options=listener if listener is not None else {"dev_eui": "70b3d57ed0001234"},
        controller_options=controller,
    )


class TestConstruction:
    def test_it_is_an_mqtt_connector(self):
        assert isinstance(make_connector(), MQTTConnector)

    def test_unknown_profile_falls_back_to_custom(self, caplog):
        with caplog.at_level(logging.WARNING):
            connector = make_connector(profile="helium")
        assert connector.profile_name == "custom"
        assert any("Unknown LoRaWAN profile" in r.message for r in caplog.records)

    def test_tenant_is_folded_into_the_application_id(self):
        connector = make_connector(profile="things_stack", application_id="myapp", tenant_id="ttn")
        assert connector.application_id == "myapp@ttn"

    def test_an_application_id_already_carrying_a_tenant_is_left_alone(self):
        connector = make_connector(profile="things_stack", application_id="myapp@ttn", tenant_id="ttn")
        assert connector.application_id == "myapp@ttn"

    def test_things_stack_username_defaults_to_the_application_id(self, caplog):
        """TTS authenticates as {app}@{tenant} with an API key as the password. An operator
        who set only the key has given us everything; the alternative is a silent auth
        failure."""
        with caplog.at_level(logging.INFO):
            connector = make_connector(profile="things_stack", application_id="myapp@ttn", password="NNSXS.abc")
        assert connector.username == "myapp@ttn"
        assert any("as the MQTT username" in r.message for r in caplog.records)

    def test_an_explicit_username_is_not_overwritten(self):
        connector = make_connector(profile="things_stack", application_id="myapp@ttn", username="someone")
        assert connector.username == "someone"

    def test_things_stack_without_an_application_id_warns(self, caplog):
        with caplog.at_level(logging.WARNING):
            make_connector(profile="things_stack")
        assert any("no application_id" in r.message for r in caplog.records)

    def test_tls_port_without_tls_warns(self, caplog):
        """The symptom otherwise is an immediate silent disconnect, which is miserable."""
        with caplog.at_level(logging.WARNING):
            make_connector(profile="things_stack", application_id="a", port=8883, tls=False)
        assert any("without tls" in r.message for r in caplog.records)

    def test_string_min_downlink_interval_is_coerced(self):
        """Config validates the plugin schema BEFORE resolving ${VAR}, so a
        "${LORAWAN_THROTTLE}" declared `number` reaches the constructor as a str."""
        assert make_connector(min_downlink_interval="30").min_downlink_interval == 30.0

    def test_zero_throttle_says_so_once(self, caplog):
        with caplog.at_level(logging.INFO):
            make_connector()
        assert any("downlinks are not throttled" in r.message for r in caplog.records)

    def test_custom_profile_without_a_topic_warns(self, caplog):
        with caplog.at_level(logging.WARNING):
            make_connector(profile="custom")
        assert any("no custom_uplink_topic" in r.message for r in caplog.records)


class TestListenerResolution:
    def test_chirpstack_topic_and_regex(self):
        connector = make_connector(application_id="app-uuid")
        assert connector.resolve_listener(make_device()) == (
            "application/app-uuid/device/70b3d57ed0001234/event/up",
            "(?i)application/app\\-uuid/device/70b3d57ed0001234/event/up$",
        )

    def test_an_unset_application_id_becomes_a_single_level_wildcard(self):
        subscription, pattern = make_connector().resolve_listener(make_device())
        assert subscription == "application/+/device/70b3d57ed0001234/event/up"
        assert pattern == "(?i)application/[^/]+/device/70b3d57ed0001234/event/up$"

    def test_things_stack_keys_on_the_device_id(self):
        connector = make_connector(profile="things_stack", application_id="myapp@ttn")
        subscription, _ = connector.resolve_listener(make_device(device_id="meter-01"))
        assert subscription == "v3/myapp@ttn/devices/meter-01/up"

    def test_an_uppercase_eui_is_lowercased(self, caplog):
        """MQTT topic filters are case-sensitive and have no case-insensitive wildcard, so a
        devEUI pasted off a datasheet would never match ChirpStack's lowercased topic — and
        the symptom is total silence, with no error at any layer."""
        connector = make_connector(application_id="app")
        with caplog.at_level(logging.INFO):
            subscription, _ = connector.resolve_listener(make_device(dev_eui="70B3D57ED0001234"))
        assert subscription == "application/app/device/70b3d57ed0001234/event/up"
        assert any("normalised dev_eui" in r.message for r in caplog.records)

    def test_separators_are_stripped_from_an_eui(self):
        connector = make_connector(application_id="app")
        subscription, _ = connector.resolve_listener(make_device(dev_eui="70-B3-D5-7E-D0-00-12-34"))
        assert subscription == "application/app/device/70b3d57ed0001234/event/up"

    def test_a_device_id_is_never_normalised(self):
        """It is an opaque operator-chosen name, not a hex EUI."""
        connector = make_connector(profile="things_stack", application_id="app")
        subscription, _ = connector.resolve_listener(make_device(device_id="Meter-01"))
        assert subscription.endswith("/devices/Meter-01/up")

    def test_regex_metacharacters_in_an_identifier_are_escaped(self):
        """An unescaped '.' matches any character, which cross-routes one node's reading onto
        another device: a plausible wrong number attributed to the wrong meter."""
        import re
        connector = make_connector(profile="things_stack", application_id="app")
        _, pattern = connector.resolve_listener(make_device(device_id="meter.01"))
        assert re.compile(pattern).match("v3/app/devices/meterX01/up") is None
        assert re.compile(pattern).match("v3/app/devices/meter.01/up") is not None

    def test_the_pattern_is_anchored_at_the_end(self):
        """Pattern.match anchors the start only, so .../event/up would otherwise also match a
        future .../event/uplink."""
        import re
        _, pattern = make_connector(application_id="app").resolve_listener(make_device())
        assert re.compile(pattern).match("application/app/device/70b3d57ed0001234/event/uplink") is None

    def test_the_case_insensitive_flag_leads_the_pattern(self):
        """Python accepts a global inline flag only at position 0 — which is why the seam
        returns a string rather than a compiled Pattern."""
        _, pattern = make_connector().resolve_listener(make_device())
        assert pattern.startswith("(?i)")

    def test_a_device_without_the_profiles_identifier_warns(self, caplog):
        connector = make_connector()
        with caplog.at_level(logging.WARNING):
            assert connector.resolve_listener(StubDevice(name="orphan")) == (None, None)
        assert any("has no listener_options.dev_eui" in r.message for r in caplog.records)

    def test_declared_topics_win_verbatim(self):
        """This is what makes a network server no profile describes supportable with no code:
        the values are not escaped, not anchored, not case-folded."""
        connector = make_connector()
        device = make_device(subscription="lns/raw/#", pattern="lns/raw/.*")
        assert connector.resolve_listener(device) == ("lns/raw/#", "lns/raw/.*")

    def test_a_declared_pattern_alone_still_gets_a_synthesised_subscription(self):
        connector = make_connector(application_id="app")
        subscription, pattern = connector.resolve_listener(make_device(pattern="anything/.*"))
        assert subscription == "application/app/device/70b3d57ed0001234/event/up"
        assert pattern == "anything/.*"

    def test_two_devices_get_distinct_compiled_patterns(self):
        connector = make_connector(application_id="app")
        connector.inject_devices({
            "a": make_device("a", dev_eui="70b3d57ed0001234"),
            "b": make_device("b", dev_eui="70b3d57ed0009999"),
        })
        assert len(connector.callbacks) == 2

    def test_custom_profile_topic_template(self):
        connector = make_connector(
            profile="custom", application_id="app",
            custom_uplink_topic="lns/{application_id}/nodes/{dev_eui}/rx",
        )
        subscription, pattern = connector.resolve_listener(make_device())
        assert subscription == "lns/app/nodes/70b3d57ed0001234/rx"
        assert pattern == "(?i)lns/app/nodes/70b3d57ed0001234/rx$"


class TestDownlinkResolution:
    def test_chirpstack_topic_and_body(self):
        connector = make_connector(application_id="app")
        topic, body = connector.resolve_downlink(writable(f_port=10), "on")
        assert topic == "application/app/device/70b3d57ed0001234/command/down"
        assert json.loads(body) == {
            "devEui": "70b3d57ed0001234", "fPort": 10, "confirmed": False, "data": "AQ==",
        }

    def test_things_stack_body_is_a_downlinks_array(self):
        connector = make_connector(profile="things_stack", application_id="myapp@ttn")
        device = writable(listener={"device_id": "meter-01"}, f_port=15)
        topic, body = connector.resolve_downlink(device, "on")
        assert topic == "v3/myapp@ttn/devices/meter-01/down/replace"
        assert json.loads(body) == {
            "downlinks": [{"f_port": 15, "frm_payload": "AQ==", "priority": "NORMAL", "confirmed": False}],
        }

    def test_queue_mode_defaults_to_replace(self):
        """A downlink queued forty minutes ago saying 'on' must not be delivered after the
        algorithm has since decided 'off'."""
        connector = make_connector(profile="things_stack", application_id="app")
        topic, _ = connector.resolve_downlink(writable(listener={"device_id": "d"}, f_port=1), "on")
        assert topic.endswith("/down/replace")

    def test_queue_mode_push_is_honoured(self):
        connector = make_connector(profile="things_stack", application_id="app")
        device = writable(listener={"device_id": "d"}, f_port=1, queue_mode="push")
        topic, _ = connector.resolve_downlink(device, "on")
        assert topic.endswith("/down/push")

    def test_an_unknown_queue_mode_warns_and_defaults(self, caplog):
        connector = make_connector(profile="things_stack", application_id="app")
        device = writable(listener={"device_id": "d"}, f_port=1, queue_mode="yolo")
        with caplog.at_level(logging.WARNING):
            topic, _ = connector.resolve_downlink(device, "on")
        assert topic.endswith("/down/replace")
        assert any("Unknown queue_mode" in r.message for r in caplog.records)

    def test_off_maps_to_the_off_payload(self):
        connector = make_connector(application_id="app")
        _, body = connector.resolve_downlink(writable(f_port=10), "off")
        assert json.loads(body)["data"] == "AA=="  # 0x00

    def test_custom_on_off_payloads(self):
        connector = make_connector(application_id="app")
        device = writable(f_port=10, on_payload="ff00", off_payload="0000")
        assert json.loads(connector.resolve_downlink(device, "on")[1])["data"] == "/wA="
        assert json.loads(connector.resolve_downlink(device, "off")[1])["data"] == "AAA="

    def test_utf8_encoding(self):
        connector = make_connector(application_id="app")
        device = writable(f_port=10, payload_encoding="utf8", on_payload="ON")
        assert json.loads(connector.resolve_downlink(device, "on")[1])["data"] == "T04="

    def test_base64_encoding_passes_through(self):
        connector = make_connector(application_id="app")
        device = writable(f_port=10, payload_encoding="base64", on_payload="AQI=")
        assert json.loads(connector.resolve_downlink(device, "on")[1])["data"] == "AQI="

    def test_a_non_switch_token_is_the_literal_payload(self):
        connector = make_connector(application_id="app")
        _, body = connector.resolve_downlink(writable(f_port=10), "0a1b")
        assert json.loads(body)["data"] == "Chs="

    def test_confirmed_reaches_the_body(self):
        connector = make_connector(application_id="app")
        _, body = connector.resolve_downlink(writable(f_port=10, confirmed=True), "on")
        assert json.loads(body)["confirmed"] is True

    def test_a_string_confirmed_is_coerced(self):
        connector = make_connector(application_id="app")
        _, body = connector.resolve_downlink(writable(f_port=10, confirmed="true"), "on")
        assert json.loads(body)["confirmed"] is True

    def test_an_explicit_topic_overrides_the_profile(self):
        connector = make_connector(application_id="app")
        topic, _ = connector.resolve_downlink(writable(f_port=10, topic="lns/raw/down"), "on")
        assert topic == "lns/raw/down"

    def test_it_logs_a_queue_not_a_send(self, caplog):
        """The parent's 'Sent message to ...' is a lie here: an operator watching a relay not
        move for twenty minutes will start power-cycling working hardware."""
        connector = make_connector(application_id="app")
        with caplog.at_level(logging.INFO):
            connector.resolve_downlink(writable(f_port=10), "on")
        assert any("Queued downlink" in r.message for r in caplog.records)


class TestDownlinkRefusals:
    def test_a_missing_f_port_is_refused(self, caplog):
        """Ports 1-223 are application-defined and there is nothing to guess from; a command
        on the wrong port is delivered and ignored, which looks exactly like Class A delay."""
        connector = make_connector(application_id="app")
        with caplog.at_level(logging.WARNING):
            assert connector.resolve_downlink(writable(), "on") is None
        assert any("no controller_options.f_port" in r.message for r in caplog.records)

    def test_an_out_of_range_f_port_is_refused(self, caplog):
        connector = make_connector(application_id="app")
        with caplog.at_level(logging.WARNING):
            assert connector.resolve_downlink(writable(f_port=224), "on") is None
        assert any("outside the application range" in r.message for r in caplog.records)

    def test_a_non_numeric_f_port_is_refused(self, caplog):
        connector = make_connector(application_id="app")
        with caplog.at_level(logging.WARNING):
            assert connector.resolve_downlink(writable(f_port="ten"), "on") is None
        assert any("non-numeric" in r.message for r in caplog.records)

    def test_a_string_f_port_is_accepted(self):
        """It arrives as a str whenever it was written as ${VAR}."""
        connector = make_connector(application_id="app")
        _, body = connector.resolve_downlink(writable(f_port="10"), "on")
        assert json.loads(body)["fPort"] == 10

    def test_bad_hex_is_logged_not_raised(self, caplog):
        """send() runs on the ALGORITHM's thread and nothing in that chain catches, so a
        ValueError from an operator's typo would be counted as an algorithm crash."""
        connector = make_connector(application_id="app")
        with caplog.at_level(logging.ERROR):
            assert connector.resolve_downlink(writable(f_port=10, on_payload="0x1"), "on") is None
        assert any("into a downlink" in r.message for r in caplog.records)

    def test_custom_profile_without_a_downlink_topic_refuses(self, caplog):
        connector = make_connector(profile="custom", custom_uplink_topic="a/{dev_eui}")
        with caplog.at_level(logging.WARNING):
            assert connector.resolve_downlink(writable(f_port=10), "on") is None
        assert any("declares no downlink topic" in r.message for r in caplog.records)

    def test_send_swallows_everything_and_publishes_nothing(self, caplog):
        connector = make_connector(application_id="app")
        connector.mqtt_client = MagicMock()
        with caplog.at_level(logging.WARNING):
            connector.send(writable(), "on")  # no f_port; must not raise
        connector.mqtt_client.publish.assert_not_called()


class TestDownlinkThrottle:
    def test_a_second_command_inside_the_window_is_dropped(self, caplog):
        connector = make_connector(application_id="app", min_downlink_interval=60)
        device = writable(f_port=10)
        assert connector.resolve_downlink(device, "on") is not None
        with caplog.at_level(logging.WARNING):
            assert connector.resolve_downlink(device, "on") is None
        assert any("min_downlink_interval" in r.message for r in caplog.records)

    def test_repeated_drops_fall_to_debug(self, caplog):
        """AutoToggle on a ten-second tick would otherwise log six times a minute, forever."""
        connector = make_connector(application_id="app", min_downlink_interval=60)
        device = writable(f_port=10)
        with caplog.at_level(logging.WARNING):
            connector.resolve_downlink(device, "on")  # accepted
            for _ in range(4):
                connector.resolve_downlink(device, "on")  # dropped
        drops = [r for r in caplog.records if "min_downlink_interval" in r.message]
        assert len(drops) == 1, "only the first drop of a run is worth a warning"

    def test_the_window_elapsing_allows_the_next_command(self, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr("connectors.lorawan.monotonic", lambda: clock["now"])
        connector = make_connector(application_id="app", min_downlink_interval=60)
        device = writable(f_port=10)
        assert connector.resolve_downlink(device, "on") is not None
        clock["now"] += 61
        assert connector.resolve_downlink(device, "on") is not None

    def test_recovery_is_announced_once(self, monkeypatch, caplog):
        clock = {"now": 1000.0}
        monkeypatch.setattr("connectors.lorawan.monotonic", lambda: clock["now"])
        connector = make_connector(application_id="app", min_downlink_interval=60)
        device = writable(f_port=10)
        connector.resolve_downlink(device, "on")
        connector.resolve_downlink(device, "on")  # dropped
        clock["now"] += 61
        with caplog.at_level(logging.INFO):
            connector.resolve_downlink(device, "on")
        assert any("resumed" in r.message for r in caplog.records)

    def test_devices_throttle_independently(self):
        connector = make_connector(application_id="app", min_downlink_interval=60)
        assert connector.resolve_downlink(writable("a", f_port=10), "on") is not None
        assert connector.resolve_downlink(writable("b", f_port=10), "on") is not None

    def test_a_per_device_interval_overrides_the_connector_default(self):
        connector = make_connector(application_id="app", min_downlink_interval=60)
        device = writable(f_port=10, min_downlink_interval=0)
        assert connector.resolve_downlink(device, "on") is not None
        assert connector.resolve_downlink(device, "on") is not None

    def test_no_throttle_by_default(self):
        connector = make_connector(application_id="app")
        device = writable(f_port=10)
        assert connector.resolve_downlink(device, "on") is not None
        assert connector.resolve_downlink(device, "on") is not None


class TestInheritedBehaviour:
    """Cheap proof the subclass did not break what it inherits."""

    @patch("connectors.mqtt.Client")
    def test_client_id_still_derives_from_the_name(self, MockClient):
        make_connector(name="lns-a").start()
        assert MockClient.call_args.kwargs["client_id"] == "lns-a"

    def test_on_connect_subscribes_to_the_synthesised_filters(self):
        connector = make_connector(application_id="app")
        connector.inject_devices({"meter": make_device()})
        client = MagicMock()
        connector.mqtt_client = client
        connector._on_connect(client, None, None, MagicMock(), None)
        topics = {topic for topic, qos in client.subscribe.call_args.args[0]}
        assert topics == {"application/app/device/70b3d57ed0001234/event/up"}

    def test_stop_still_disconnects(self):
        connector = make_connector()
        connector.mqtt_client = MagicMock()
        connector.stop()
        connector.mqtt_client.disconnect.assert_called_once()
        assert connector.is_stopping()

    def test_an_uplink_routes_through_the_inherited_dispatcher(self):
        """The bounded per-device dispatch is inherited verbatim, and a network server is
        exactly where an unbounded one bites: a retained-message flush after a reconnect
        replays every node's last uplink at once."""
        connector = make_connector(application_id="app")
        device = make_device()
        connector.inject_devices({"meter": device})
        device.receive = MagicMock(return_value=True)
        topic = "application/app/device/70b3d57ed0001234/event/up"
        try:
            connector.on_message(None, None, MagicMock(topic=topic, payload=b"{}"))
            assert wait_until(lambda: device.receive.call_count == 1)
            device.receive.assert_called_once_with(topic, "{}")
        finally:
            connector.stop()

    def test_send_publishes_the_synthesised_topic_and_body(self):
        connector = make_connector(application_id="app")
        client = MagicMock()
        connector.mqtt_client = client
        connector.send(writable(f_port=10), "on")
        topic, body = client.publish.call_args.args
        assert topic == "application/app/device/70b3d57ed0001234/command/down"
        assert json.loads(body)["fPort"] == 10


class TestParentSeam:
    """resolve_listener() and resolve_downlink() were extracted from MQTTConnector for this
    subclass; the parent must stay behaviour-identical."""

    def test_the_parent_still_reads_declared_listener_options(self):
        connector = MQTTConnector(name="mqtt", host="h", port=1883, version="3.1.1")
        device = StubDevice(listener_options={"subscription": "p1/#", "pattern": "p1/.*"})
        assert connector.resolve_listener(device) == ("p1/#", "p1/.*")

    def test_the_parent_still_reads_the_declared_downlink_topic(self):
        connector = MQTTConnector(name="mqtt", host="h", port=1883, version="3.1.1")
        device = StubDevice(is_writable=True, controller_options={"topic": "cmd/x"})
        assert connector.resolve_downlink(device, "on") == ("cmd/x", "on")
