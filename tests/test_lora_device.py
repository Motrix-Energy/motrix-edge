"""LoRa node: envelope unwrapping, the declarative byte codec, and the gap contract.

No importorskip: this device imports nothing outside the stdlib — no paho, no pyserial — and
that is exactly what makes a LoRa node replayable from a CSV on a machine with no radio and
no broker, and what keeps this file running in a core-only checkout.
"""
import json
import logging

import pytest

from api.capabilities import EnergyMeter, MetricSource, Switch
from devices.lora import LoRa
from devices.lora_switch import LoRaSwitch

# hex 00 01 86 A0 | 01 2C | 81  — every number below is hand-checkable from these seven bytes,
# which is what makes the fixture reviewable:
#   energy  0x000186A0 = 100000 * 0.01 = 1000.0 kWh
#   power   0x012C     = 300    * 0.001 = 0.3 kW
#   0x81    = 0b10000001 -> bit 0 True, bit 7 True
PAYLOAD_B64 = "AAGGoAEsgQ=="

FIELDS = [
    {"name": "energy_kwh", "offset": 0, "data_type": "UINT32", "scale": 0.01, "unit": "kWh", "role": "energy_import_kwh"},
    {"name": "power_kw", "offset": 4, "data_type": "UINT16", "scale": 0.001, "unit": "kW"},
    {"name": "relay_on", "offset": 6, "data_type": "UINT8", "bit": 0},
    {"name": "alarm", "offset": 6, "data_type": "UINT8", "bit": 7},
]

CHIRPSTACK_UPLINK = json.dumps({
    "deduplicationId": "3ac7e3c4-4401-4b8d-9386-a5c902f9202d",
    "deviceInfo": {"deviceName": "Test device", "devEui": "70b3d57ed0001234"},
    "devAddr": "00189440",
    "fPort": 1,
    "fCnt": 42,
    "data": PAYLOAD_B64,
    "rxInfo": [{"gatewayId": "0016c001f153a14c", "rssi": -95, "snr": 7.5}],
    "txInfo": {"frequency": 868100000},
})

# Note the UPPERCASE devEUI: The Things Stack reports it that way and ChirpStack does not,
# which is what the case-insensitive identity check exists for.
TTS_UPLINK = json.dumps({
    "end_device_ids": {"device_id": "meter-01", "dev_eui": "70B3D57ED0001234", "dev_addr": "00BCB929"},
    "received_at": "2026-08-14T15:15:46.014773143Z",
    "uplink_message": {
        "f_port": 1, "f_cnt": 42, "frm_payload": PAYLOAD_B64,
        "rx_metadata": [{"gateway_ids": {"gateway_id": "gtw1"}, "rssi": -95, "snr": 7.5}],
        "settings": {"frequency": "868100000"},
    },
})

TTS_UPLINK_PORT_2 = json.dumps({
    "end_device_ids": {"device_id": "meter-01", "dev_eui": "70B3D57ED0001234"},
    "uplink_message": {"f_port": 2, "f_cnt": 43, "frm_payload": "Bw==", "rx_metadata": [{"rssi": -99}]},
})

AWS_UPLINK = json.dumps({
    "PayloadData": PAYLOAD_B64,
    "WirelessMetadata": {"LoRaWAN": {"FPort": 1, "DevEui": "70b3d57ed0001234"}},
})

CHIRPSTACK_DECODED_ONLY = json.dumps({
    "deviceInfo": {"devEui": "70b3d57ed0001234"},
    "fPort": 1,
    "data": "",
    "object": {"energy": {"total_kwh": 1000.0}, "relay": True, "battery": {"millivolts": 3600}},
})

TTS_UPLINK_EMPTY = json.dumps({
    "end_device_ids": {"dev_eui": "70b3d57ed0001234"},
    "uplink_message": {"f_port": 1, "frm_payload": ""},
})

WRONG_DEVICE = json.dumps({
    "deviceInfo": {"devEui": "70b3d57ed0009999"},
    "fPort": 1,
    "data": PAYLOAD_B64,
})

MALFORMED = "not json at all"


def make_node(protocol: str = "lorawan", cls=LoRa, controller=None, **listener) -> LoRa:
    defaults = {"profile": "chirpstack", "dev_eui": "70b3d57ed0001234", "fields": FIELDS}
    defaults.update(listener)
    return cls("node", {"name": "lns", "protocol": protocol}, defaults, {} if controller is None else controller)


class TestEnvelopeUnwrap:
    def test_chirpstack(self):
        node = make_node()
        assert node.receive(CHIRPSTACK_UPLINK) is True
        assert node.get_metrics() == {"energy_kwh": 1000.0, "power_kw": 0.3, "relay_on": True, "alarm": True}

    def test_things_stack(self):
        node = make_node(profile="things_stack")
        assert node.receive(TTS_UPLINK) is True
        assert node.get_metrics()["energy_kwh"] == 1000.0

    def test_the_same_payload_reaches_the_same_values_through_both_envelopes(self):
        """The whole point of the profile table: the wrapper differs, the reading does not."""
        chirpstack, tts = make_node(), make_node(profile="things_stack")
        chirpstack.receive(CHIRPSTACK_UPLINK)
        tts.receive(TTS_UPLINK)
        assert chirpstack.get_metrics() == tts.get_metrics()

    def test_a_custom_profile_reaches_an_unlisted_server(self):
        """AWS IoT Core for LoRaWAN, in three lines of config and no code."""
        node = make_node(profile="custom", paths={
            "payload": "PayloadData",
            "f_port": "WirelessMetadata.LoRaWAN.FPort",
            "dev_eui": "WirelessMetadata.LoRaWAN.DevEui",
        })
        assert node.receive(AWS_UPLINK) is True
        assert node.get_metrics()["energy_kwh"] == 1000.0

    def test_metadata_rides_along_in_the_uplink_block(self):
        node = make_node()
        node.receive(CHIRPSTACK_UPLINK)
        uplink = node.data["uplink"]
        assert uplink["rssi"] == -95 and uplink["snr"] == 7.5
        assert uplink["f_port"] == 1 and uplink["f_cnt"] == 42
        assert uplink["payload_hex"] == "000186a0012c81"

    def test_the_star_index_takes_the_first_gateway_that_answers(self):
        """rxInfo is an array of gateway receptions; index 0 is whichever the server listed
        first, so a hard-coded 0 changes meaning when a second gateway comes online."""
        node = make_node()
        payload = json.loads(CHIRPSTACK_UPLINK)
        payload["rxInfo"] = [{"gatewayId": "quiet"}, {"gatewayId": "loud", "rssi": -70}]
        node.receive(json.dumps(payload))
        assert node.data["uplink"]["rssi"] == -70

    def test_a_missing_path_is_none_not_a_crash(self):
        node = make_node(profile="custom", paths={"payload": "PayloadData", "rssi": "nope.not.here"})
        assert node.receive(AWS_UPLINK) is True
        assert node.data["uplink"]["rssi"] is None

    def test_an_unknown_profile_falls_back_and_warns(self, caplog):
        with caplog.at_level(logging.WARNING):
            node = make_node(profile="helium")
        assert node.receive(CHIRPSTACK_UPLINK) is True
        assert any("Unknown profile" in r.message for r in caplog.records)


class TestByteCodec:
    def _decode(self, payload_hex: str, **field) -> object:
        import base64
        raw = base64.b64encode(bytes.fromhex(payload_hex)).decode()
        node = make_node(dev_eui=None, fields=[{"name": "v", "offset": 0, **field}])
        node.receive(json.dumps({"fPort": 1, "data": raw}))
        return node.data["fields"]["v"]["value"]

    def test_unsigned_integers(self):
        assert self._decode("ff", data_type="UINT8") == 255
        assert self._decode("0100", data_type="UINT16") == 256
        assert self._decode("010000", data_type="UINT24") == 65536
        assert self._decode("00000100", data_type="UINT32") == 256

    def test_signed_integers(self):
        assert self._decode("ff", data_type="INT8") == -1
        assert self._decode("ffff", data_type="INT16") == -1
        assert self._decode("ffffff", data_type="INT24") == -1
        assert self._decode("ffffffff", data_type="INT32") == -1

    def test_uint24_is_a_real_type_here(self):
        """struct has no 24-bit format, which is why integers go through int.from_bytes. A
        24-bit counter is common on LoRa precisely because 32 bits is one byte too many."""
        assert self._decode("0186a0", data_type="UINT24") == 100000

    def test_floats(self):
        assert self._decode("3f800000", data_type="FLOAT32") == pytest.approx(1.0)
        assert self._decode("3ff0000000000000", data_type="FLOAT64") == pytest.approx(1.0)

    def test_little_endian(self):
        assert self._decode("0001", data_type="UINT16", byte_order="little") == 256
        assert self._decode("0000803f", data_type="FLOAT32", byte_order="little") == pytest.approx(1.0)

    def test_bool_is_any_non_zero_byte(self):
        assert self._decode("00", data_type="BOOL") is False
        assert self._decode("07", data_type="BOOL") is True

    def test_string_and_raw(self):
        assert self._decode("41424300", data_type="STRING", length=4) == "ABC"
        assert self._decode("0a1b", data_type="RAW", length=2) == [10, 27]

    def test_scale_and_bias(self):
        """A temperature is raw * 0.1 - 40, not (raw - 400) * 0.1 — the order a vendor
        decoder writes it."""
        assert self._decode("01f4", data_type="UINT16", scale=0.1, bias=-40) == pytest.approx(10.0)

    def test_offset_reads_bytes_not_words(self):
        assert self._decode("00ff", data_type="UINT8") == 0
        node = make_node(dev_eui=None, fields=[{"name": "v", "offset": 1, "data_type": "UINT8"}])
        node.receive(json.dumps({"fPort": 1, "data": "AP8="}))
        assert node.data["fields"]["v"]["value"] == 255


class TestBitFields:
    def test_lsb_is_bit_zero(self):
        """What a datasheet's 'bit 0 = alarm' means."""
        node = make_node(dev_eui=None, fields=[
            {"name": "b0", "offset": 0, "data_type": "UINT8", "bit": 0},
            {"name": "b1", "offset": 0, "data_type": "UINT8", "bit": 1},
            {"name": "b7", "offset": 0, "data_type": "UINT8", "bit": 7},
        ])
        node.receive(json.dumps({"fPort": 1, "data": "gQ=="}))  # 0x81
        assert node.get_metrics() == {"b0": True, "b1": False, "b7": True}

    def test_a_bit_with_a_scale_warns_and_ignores_the_scale(self, caplog):
        """Scaling a flag would turn True into a number downstream."""
        with caplog.at_level(logging.WARNING):
            node = make_node(dev_eui=None, fields=[{"name": "b", "offset": 0, "data_type": "UINT8", "bit": 0, "scale": 10}])
        node.receive(json.dumps({"fPort": 1, "data": "AQ=="}))
        assert node.data["fields"]["b"]["value"] is True
        assert any("the scale is ignored" in r.message for r in caplog.records)


class TestFPortMultiplexing:
    """Nodes multiplex payload layouts by port by design — 1 periodic, 2 status."""

    def _multiport(self) -> LoRa:
        return make_node(profile="things_stack", fields=[
            {"name": "energy_kwh", "offset": 0, "data_type": "UINT32", "scale": 0.01, "f_port": 1, "role": "energy_import_kwh"},
            {"name": "status", "offset": 0, "data_type": "UINT8", "f_port": 2},
            {"name": "always", "offset": 0, "data_type": "UINT8"},
        ])

    def test_only_the_matching_port_decodes(self):
        node = self._multiport()
        node.receive(TTS_UPLINK)
        assert "energy_kwh" in node.get_metrics() and "status" not in node.get_metrics()

    def test_an_off_port_field_is_carried_forward(self):
        """Replacing self.data wholesale the way modbus_meter does would erase the energy
        reading on every status uplink and make get_total_energy_kwh() return 0.0
        intermittently forever, with nothing erroring anywhere."""
        node = self._multiport()
        node.receive(TTS_UPLINK)
        assert node.receive(TTS_UPLINK_PORT_2) is True
        assert node.get_metrics()["energy_kwh"] == 1000.0
        assert node.get_metrics()["status"] == 7
        assert node.get_total_energy_kwh() == 1000.0

    def test_a_carried_value_keeps_the_f_cnt_that_produced_it(self):
        """So a reader can tell a fresh value from a carried one."""
        node = self._multiport()
        node.receive(TTS_UPLINK)
        node.receive(TTS_UPLINK_PORT_2)
        assert node.data["fields"]["energy_kwh"]["f_cnt"] == 42
        assert node.data["fields"]["status"]["f_cnt"] == 43

    def test_an_ungated_field_decodes_on_every_port(self):
        node = self._multiport()
        node.receive(TTS_UPLINK_PORT_2)
        assert node.get_metrics()["always"] == 7

    def test_an_uplink_with_no_eligible_field_is_a_gap(self):
        node = make_node(fields=[{"name": "only_p9", "offset": 0, "data_type": "UINT8", "f_port": 9}])
        assert node.receive(CHIRPSTACK_UPLINK) is False


class TestNetworkServerDecode:
    def test_source_reads_the_servers_own_codec_output(self):
        node = make_node(fields=[
            {"name": "energy_kwh", "source": "energy.total_kwh", "role": "energy_import_kwh"},
            {"name": "relay", "source": "relay"},
            {"name": "battery_v", "source": "battery.millivolts", "scale": 0.001},
        ])
        assert node.receive(CHIRPSTACK_DECODED_ONLY) is True
        assert node.get_metrics() == {"energy_kwh": 1000.0, "relay": True, "battery_v": 3.6}

    def test_role_works_on_the_decoded_path(self):
        node = make_node(fields=[{"name": "e", "source": "energy.total_kwh", "role": "energy_import_kwh"}])
        node.receive(CHIRPSTACK_DECODED_ONLY)
        assert node.get_total_energy_kwh() == 1000.0

    def test_source_and_offset_together_warns_and_source_wins(self, caplog):
        with caplog.at_level(logging.WARNING):
            node = make_node(fields=[{"name": "e", "offset": 0, "source": "energy.total_kwh"}])
        node.receive(CHIRPSTACK_DECODED_ONLY)
        assert node.get_metrics()["e"] == 1000.0
        assert any("both offset and source" in r.message for r in caplog.records)

    def test_a_field_with_neither_is_dropped(self, caplog):
        with caplog.at_level(logging.WARNING):
            node = make_node(fields=[{"name": "nowhere"}])
        assert any("neither offset nor source" in r.message for r in caplog.records)
        assert node.receive(CHIRPSTACK_UPLINK) is False

    def test_a_missing_decoded_path_warns_once(self, caplog):
        node = make_node(fields=[
            {"name": "good", "source": "relay"},
            {"name": "bad", "source": "nope.nothing"},
        ])
        with caplog.at_level(logging.WARNING):
            node.receive(CHIRPSTACK_DECODED_ONLY)
            node.receive(CHIRPSTACK_DECODED_ONLY)
        assert len([r for r in caplog.records if "nothing at decoded path" in r.message]) == 1


class TestIdentity:
    def test_an_uplink_from_another_node_is_rejected(self, caplog):
        """Defence in depth against a mis-synthesised routing regex or a fan-out
        subscription — and the only identity check that survives replay, where
        PseudoConnector routes by device name and never evaluates a topic."""
        node = make_node()
        with caplog.at_level(logging.WARNING):
            assert node.receive(WRONG_DEVICE) is False
        assert any("not the declared" in r.message for r in caplog.records)
        assert node.data == {}

    def test_an_uppercase_dev_eui_still_matches(self):
        """TTS reports it uppercase, ChirpStack lowercase, and the operator pasted whichever
        the datasheet showed."""
        node = make_node(profile="things_stack", dev_eui="70b3d57ed0001234")
        assert node.receive(TTS_UPLINK) is True

    def test_separators_in_the_declared_eui_are_tolerated(self):
        node = make_node(dev_eui="70-B3-D5-7E-D0-00-12-34")
        assert node.receive(CHIRPSTACK_UPLINK) is True

    def test_without_a_declared_eui_everything_is_accepted(self):
        node = make_node(dev_eui=None)
        assert node.receive(WRONG_DEVICE) is True


class TestFailureModes:
    def test_malformed_json_keeps_the_last_reading(self, caplog):
        """Losing a sample is acceptable; inventing one is not."""
        node = make_node()
        node.receive(CHIRPSTACK_UPLINK)
        before = dict(node.data)
        with caplog.at_level(logging.WARNING):
            assert node.receive(MALFORMED) is False
        assert node.data == before
        assert any("Unreadable uplink" in r.message for r in caplog.records)

    def test_repeated_parse_failures_fall_to_debug(self, caplog):
        node = make_node()
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                node.receive(MALFORMED)
        assert len([r for r in caplog.records if "Unreadable uplink" in r.message]) == 1

    def test_recovery_is_announced(self, caplog):
        node = make_node()
        node.receive(MALFORMED)
        with caplog.at_level(logging.INFO):
            node.receive(CHIRPSTACK_UPLINK)
        assert any("recovered" in r.message for r in caplog.records)

    def test_an_empty_uplink_is_not_a_failure(self, caplog):
        """An empty uplink is how a Class A node opens a receive window to collect a queued
        downlink. No reading came of it, but nothing is broken either."""
        node = make_node(profile="things_stack")
        with caplog.at_level(logging.WARNING):
            assert node.receive(TTS_UPLINK_EMPTY) is False
        assert not caplog.records
        assert node._parse_failing is False

    def test_unpadded_base64_still_decodes(self):
        """Every network server pads; a hand-edited replay row will not, and that is a
        'works live, fails in replay' bug class for one line of code."""
        node = make_node(dev_eui=None, fields=[{"name": "v", "offset": 0, "data_type": "UINT8"}])
        assert node.receive(json.dumps({"fPort": 1, "data": "AQ"})) is True
        assert node.data["fields"]["v"]["value"] == 1

    def test_an_oversized_payload_is_an_envelope_mismatch(self, caplog):
        node = make_node(dev_eui=None, max_payload_bytes=4)
        with caplog.at_level(logging.WARNING):
            assert node.receive(CHIRPSTACK_UPLINK) is False
        assert any("over max_payload_bytes" in r.message for r in caplog.records)

    def test_a_short_payload_warns_once_per_field(self, caplog):
        """The normal case for a short status uplink, not an error — a five-minute reporter
        would otherwise emit 288 warnings a day."""
        node = make_node(dev_eui=None, fields=[
            {"name": "ok", "offset": 0, "data_type": "UINT8"},
            {"name": "past_the_end", "offset": 10, "data_type": "UINT16"},
        ])
        with caplog.at_level(logging.WARNING):
            node.receive(CHIRPSTACK_UPLINK)
            node.receive(CHIRPSTACK_UPLINK)
        assert len([r for r in caplog.records if "past_the_end" in r.message]) == 1
        assert node.get_metrics() == {"ok": 0}

    def test_a_bad_field_map_never_raises(self):
        """A constructor that raises is contained by main.create_classes, but containment
        costs the whole node: the entry is skipped and nothing here reports at all."""
        node = make_node(fields="not a list")
        assert node.receive(CHIRPSTACK_UPLINK) is False

    def test_a_duplicate_field_name_keeps_the_first(self):
        node = make_node(dev_eui=None, fields=[
            {"name": "v", "offset": 0, "data_type": "UINT8"},
            {"name": "v", "offset": 1, "data_type": "UINT8"},
        ])
        node.receive(CHIRPSTACK_UPLINK)
        assert node.get_metrics() == {"v": 0}


class TestCapabilities:
    def test_it_declares_the_read_capabilities(self):
        node = make_node()
        assert isinstance(node, EnergyMeter) and isinstance(node, MetricSource)
        assert not isinstance(node, Switch)

    def test_energy_sums_every_field_carrying_the_role(self):
        node = make_node(dev_eui=None, fields=[
            {"name": "t1", "offset": 0, "data_type": "UINT16", "role": "energy_import_kwh"},
            {"name": "t2", "offset": 2, "data_type": "UINT16", "role": "energy_import_kwh"},
        ])
        node.receive(CHIRPSTACK_UPLINK)  # 0x0001 + 0x86a0
        assert node.get_total_energy_kwh() == 1 + 34464

    def test_no_role_reports_zero_not_none(self, caplog):
        """0.0 means 'no kWh source configured' and is harmless in an algorithm that sums
        every EnergyMeter; None is reserved for a configured source that cannot be read."""
        with caplog.at_level(logging.INFO):
            node = make_node(fields=[{"name": "power_kw", "offset": 4, "data_type": "UINT16"}])
        node.receive(CHIRPSTACK_UPLINK)
        assert node.get_total_energy_kwh() == 0.0
        assert any("will report 0.0" in r.message for r in caplog.records)

    def test_a_device_that_never_reported_is_none(self):
        assert make_node().get_total_energy_kwh() is None

    def test_a_non_numeric_energy_field_is_none(self, caplog):
        node = make_node(dev_eui=None, fields=[
            {"name": "serial", "offset": 0, "data_type": "STRING", "length": 4, "role": "energy_import_kwh"},
        ])
        node.receive(CHIRPSTACK_UPLINK)
        with caplog.at_level(logging.WARNING):
            assert node.get_total_energy_kwh() is None
        assert any("Non-numeric energy field" in r.message for r in caplog.records)

    def test_metrics_report_scalars_only(self):
        """A STRING field would put one unchanging string per node into a measurement; a RAW
        byte list is not a scalar at all. Bools stay — a relay state is a real metric."""
        node = make_node(dev_eui=None, fields=[
            {"name": "num", "offset": 0, "data_type": "UINT8"},
            {"name": "flag", "offset": 6, "data_type": "UINT8", "bit": 0},
            {"name": "text", "offset": 0, "data_type": "STRING", "length": 2},
            {"name": "bytes", "offset": 0, "data_type": "RAW", "length": 2},
        ])
        node.receive(CHIRPSTACK_UPLINK)
        assert node.get_metrics() == {"num": 0, "flag": True}
        assert "text" in node.data["fields"] and "bytes" in node.data["fields"]

    def test_metrics_on_a_device_that_never_reported(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert make_node().get_metrics() == {}

    def test_the_energy_role_means_the_same_thing_as_modbus(self):
        """Both are read through EnergyMeter by the same algorithms."""
        from devices.lora import ROLE_ENERGY_IMPORT_KWH
        from devices.modbus_meter import ROLE_ENERGY_IMPORT_KWH as MODBUS_ROLE
        assert ROLE_ENERGY_IMPORT_KWH == MODBUS_ROLE

    def test_an_unknown_role_is_dropped(self, caplog):
        with caplog.at_level(logging.WARNING):
            node = make_node(fields=[{"name": "v", "offset": 0, "data_type": "UINT8", "role": "energy_export_kwh"}])
        node.receive(CHIRPSTACK_UPLINK)
        assert node.get_total_energy_kwh() == 0.0
        assert any("Unknown role" in r.message for r in caplog.records)

    def test_a_role_with_a_mismatched_unit_warns_but_does_not_convert(self, caplog):
        """A typo in `unit` would otherwise rescale a revenue reading by 1000."""
        with caplog.at_level(logging.WARNING):
            node = make_node(dev_eui=None, fields=[
                {"name": "e", "offset": 0, "data_type": "UINT16", "unit": "Wh", "role": "energy_import_kwh"},
            ])
        node.receive(CHIRPSTACK_UPLINK)
        assert node.get_total_energy_kwh() == 1  # unconverted
        assert any("no conversion is applied" in r.message for r in caplog.records)


class TestDispatch:
    def test_both_arities_are_accepted(self):
        """A replay row with a non-empty topic column calls with two arguments, and the
        resulting TypeError would be swallowed by the replay loop's own handler."""
        assert make_node().receive(CHIRPSTACK_UPLINK) is True
        assert make_node().receive("application/1/device/x/event/up", CHIRPSTACK_UPLINK) is True

    def test_the_serial_protocol_uses_the_same_path(self):
        node = make_node(protocol="lora", profile="native")
        frame = json.dumps({"data": PAYLOAD_B64, "f_port": 1, "rssi": -102, "address": "70b3d57ed0001234"})
        assert node.receive(frame) is True
        assert node.get_metrics()["energy_kwh"] == 1000.0
        assert node.data["uplink"]["rssi"] == -102

    def test_plain_mqtt_is_accepted_as_a_read_path(self):
        """A plain mqtt connector plus a hand-written subscription is a working read path
        against any network server, with no profile and no LoRaWANConnector."""
        assert make_node(protocol="mqtt").receive(CHIRPSTACK_UPLINK) is True

    def test_an_unserved_protocol_is_refused_rather_than_raised(self):
        device = make_node(protocol="zigbee")
        assert device.receive(CHIRPSTACK_UPLINK) is False
        assert device.data == {}

    def test_the_refusal_is_reported_once_at_construction(self, caplog):
        with caplog.at_level(logging.ERROR):
            device = make_node(protocol="zigbee")
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert "zigbee" in errors[0].message

        caplog.clear()
        with caplog.at_level(logging.ERROR):
            for _ in range(5):
                device.receive(CHIRPSTACK_UPLINK)
        assert [r for r in caplog.records if r.levelname == "ERROR"] == []

    @pytest.mark.parametrize("protocol", ["lorawan", "lora", "mqtt"])
    def test_every_declared_protocol_is_served(self, protocol, caplog):
        # Three labels out of one handler — the case no derivation from method names could
        # produce, and the reason the declaration exists.
        with caplog.at_level(logging.ERROR):
            make_node(protocol=protocol)
        assert [r for r in caplog.records if r.levelname == "ERROR"] == []

    def test_an_empty_call_is_a_gap_not_a_crash(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert make_node().receive() is False
        assert any("Empty receive()" in r.message for r in caplog.records)


class TestReplayFidelity:
    """A capture taken straight off the broker must replay byte-identically.

    This is the property the whole envelope-on-the-device decision buys, and the reason the
    connector hands over the network server's JSON unchanged instead of unwrapping it: the
    replayable format is the real wire format, so `mosquitto_sub -v -t 'application/#' > x.csv`
    is a backtest input. Unwrapping in the connector would have made the replay format one
    this repo invented, which no real capture fits.
    """

    def test_a_raw_chirpstack_capture_replays_to_the_live_result(self, tmp_path):
        from connectors.pseudo import PseudoConnector
        from tests.conftest import write_replay

        live = make_node()
        live.receive(CHIRPSTACK_UPLINK)

        replayed = make_node()
        path = write_replay(
            tmp_path / "capture.csv",
            [("2026-08-14T10:00:00", "node", "application/app/device/70b3d57ed0001234/event/up",
              CHIRPSTACK_UPLINK)],
            quoted=True,  # the payload is JSON: it carries commas and quotes
        )
        connector = PseudoConnector("replay", replay_file=path, speed=0, emulates="lorawan")
        connector.inject_devices({"node": replayed})
        connector.start()

        assert replayed.data == live.data
        assert replayed.get_total_energy_kwh() == live.get_total_energy_kwh()

    def test_the_topic_column_is_not_used_for_identity(self, tmp_path):
        """PseudoConnector routes by device_name and never evaluates a topic, so an identity
        check reading the topic would exist only on the live path — the one a backtest never
        exercises. The devEUI comes out of the envelope instead, which survives replay."""
        from connectors.pseudo import PseudoConnector
        from tests.conftest import write_replay

        node = make_node()
        path = write_replay(
            tmp_path / "capture.csv",
            [("2026-08-14T10:00:00", "node", "", CHIRPSTACK_UPLINK)],  # no topic at all
            quoted=True,
        )
        connector = PseudoConnector("replay", replay_file=path, speed=0, emulates="lorawan")
        connector.inject_devices({"node": node})
        connector.start()

        assert node.get_metrics()["energy_kwh"] == 1000.0


class TestLoRaSwitch:
    def _switch(self, controller=None, protocol="lorawan") -> LoRaSwitch:
        return make_node(
            protocol=protocol, cls=LoRaSwitch,
            controller={"f_port": 10} if controller is None else controller,
        )

    def test_it_is_a_switch_and_writable(self):
        switch = self._switch()
        assert isinstance(switch, Switch)
        assert switch.is_writable is True

    def test_the_read_device_is_not_a_switch(self):
        """Load-bearing, and the reasoning lives in devices/lora_switch.py: `Switch` is a
        class-level type claim, so a read-only class inheriting it is selected as an actuator
        by every algorithm that looks for one."""
        assert not isinstance(make_node(), Switch)
        assert make_node().is_writable is False

    def test_it_inherits_the_decoding(self):
        switch = self._switch()
        assert switch.receive(CHIRPSTACK_UPLINK) is True
        assert switch.get_metrics()["energy_kwh"] == 1000.0

    def test_no_f_port_warns_once_at_startup(self, caplog):
        with caplog.at_level(logging.WARNING):
            self._switch(controller={})
        assert any("no controller_options.f_port" in r.message for r in caplog.records)

    def test_a_plain_mqtt_connector_warns_that_writes_will_not_work(self, caplog):
        with caplog.at_level(logging.WARNING):
            self._switch(protocol="mqtt")
        assert any("needs the 'lorawan' or 'lora' protocol" in r.message for r in caplog.records)
