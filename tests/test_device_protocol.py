"""`Device`'s protocol-refusal seam, and the sweep that keeps every device honest to it.

Its own file, the way tests/test_connector_delivery.py owns the connector seam: this is
shared behaviour eleven device classes inherit, and each device's own test file asserts only
what it declares. No importorskip — every device module here is stdlib-only; the
optional-dependency rule applies to connectors.
"""
import logging
from typing import Any, Optional

import pytest

from api import conformance
from api.device import Device
from devices.ha_entity import HaEntity
from devices.ha_switch import HaSwitch
from devices.lora import LoRa
from devices.lora_switch import LoRaSwitch
from devices.modbus_meter import ModbusMeter
from devices.modbus_switch import ModbusSwitch
from devices.openems import Openems
from devices.openems_switch import OpenemsSwitch
from devices.p1 import P1
from devices.pseudo import Pseudo
from devices.shelly_plug import ShellyPlug


def _build(cls, protocol: Any = "mqtt", name: str = "d"):
    """Construct any device with the minimum options every one of them accepts.

    Deliberately not the per-file `make_*` factories: this exercises only `__init__`'s
    protocol check and `receive()`'s refusal, so a device's own option payload is noise here
    — and building the classes directly sweeps the `*_switch` subclasses too, which no
    factory covers.
    """
    return cls(
        name=name,
        connector_options={"name": "c", "protocol": protocol},
        listener_options={},
        controller_options={},
    )


def _errors(caplog):
    return [r for r in caplog.records if r.levelname == "ERROR"]


class Declaring(Device):
    """A device that claims two protocols and has something specific to say about a third."""
    SUPPORTED_PROTOCOLS = ("mqtt", "lorawan")
    UNSERVABLE_PROTOCOLS = {"lora": "a specific reason"}
    PROTOCOL_REFUSAL = "the generic reason"

    def __init__(self, name="declaring", connector_options=None, **_):
        super().__init__(name, connector_options if connector_options is not None else {}, {}, {})

    def receive(self, *args, **kwargs) -> Optional[bool]:
        if self.refuse_unserved_protocol(*args, **kwargs):
            return False
        return True


class Claiming(Declaring):
    """Declares nothing, the way devices/pseudo.py does."""
    SUPPORTED_PROTOCOLS = ()
    UNSERVABLE_PROTOCOLS = {}
    PROTOCOL_REFUSAL = ""


class TestTheDeclaration:
    def test_a_device_that_claims_nothing_is_never_refused(self):
        # An empty declaration means "does not dispatch on protocol", not "nothing works".
        for protocol in ["mqtt", "zigbee", None, 5, ["mqtt"]]:
            assert Claiming.protocol_refusal(protocol) is None, protocol

    def test_a_device_that_claims_nothing_says_nothing_at_construction(self, caplog):
        with caplog.at_level(logging.ERROR):
            _build(Claiming, "zigbee")
        assert _errors(caplog) == []

    @pytest.mark.parametrize("protocol", ["mqtt", "lorawan"])
    def test_a_declared_protocol_is_served(self, protocol):
        assert Declaring.protocol_refusal(protocol) is None

    def test_precedence_runs_supported_then_table_then_pseudo_then_generic(self):
        assert Declaring.protocol_refusal("mqtt") is None
        assert Declaring.protocol_refusal("lora") == "a specific reason"
        assert "emulates" in Declaring.protocol_refusal("pseudo")
        assert Declaring.protocol_refusal("zigbee") == "the generic reason"

    def test_a_devices_own_pseudo_entry_beats_the_shared_sentence(self):
        # The one ordering the test above cannot see, because `Declaring`'s table has no
        # pseudo entry: a device with something better to say about a replay must be able to
        # say it, so the table is consulted before the base's shared sentence.
        class Replayable(Declaring):
            UNSERVABLE_PROTOCOLS = {"pseudo": "this device replays from a capture instead"}
        assert Replayable.protocol_refusal("pseudo") == "this device replays from a capture instead"

    def test_the_pseudo_sentence_names_the_devices_own_first_protocol(self):
        # The one refusal that reads the same for every device, so it lives on the base — and
        # it has to name the transport *this* device would be emulating.
        assert 'emulates: "mqtt"' in Declaring.protocol_refusal("pseudo")

    def test_a_device_with_no_generic_sentence_still_says_something_true(self):
        class Terse(Declaring):
            PROTOCOL_REFUSAL = ""
        assert Terse.protocol_refusal("zigbee") == "it reads 'mqtt' or 'lorawan', and nothing else"


class TestNothingOnThisPathRaises:
    @pytest.mark.parametrize("protocol", [["mqtt"], {"mqtt": True}, {"a", "b"}])
    def test_an_unhashable_protocol_is_refused_rather_than_raised(self, protocol):
        # Config only *warns* when the schema refuses `protocol`, so a list or an object
        # reaches the constructor. `in` on a dict would hash it and raise TypeError out of
        # __init__ — which main.create_classes catches, dropping the device silently instead
        # of creating one that says why it is useless.
        assert Declaring.protocol_refusal(protocol) == "the generic reason"
        assert _build(Declaring, protocol).receive("payload") is False

    @pytest.mark.parametrize("protocol", [None, "", 5, True])
    def test_an_absent_or_odd_protocol_is_refused_rather_than_raised(self, protocol):
        assert _build(Declaring, protocol).receive("payload") is False

    def test_connector_options_that_are_not_a_dict_do_not_raise(self, caplog):
        # AttributeError is one of the three types create_classes catches, so this would make
        # the device vanish without a word — the failure the ERROR exists to report.
        with caplog.at_level(logging.ERROR):
            device = Declaring(connector_options="not a dict")
        assert len(_errors(caplog)) == 1
        assert device.receive("payload") is False

    def test_the_error_is_said_once_at_construction_and_never_per_payload(self, caplog):
        with caplog.at_level(logging.DEBUG):
            device = _build(Declaring, "zigbee")
            for _ in range(20):
                device.receive("payload")
        assert len(_errors(caplog)) == 1
        assert len([r for r in caplog.records if r.levelname == "DEBUG" and "Refusing" in r.message]) == 20


# Every concrete device, including the four *_switch subclasses that carry no receive() of
# their own and inherit their parent's declaration.
EVERY_DEVICE = [
    P1, ShellyPlug, HaEntity, HaSwitch, LoRa, LoRaSwitch,
    ModbusMeter, ModbusSwitch, Openems, OpenemsSwitch, Pseudo,
]


class TestEveryDeviceIsHonestAboutSwitch:
    """The other class-level claim every device makes, swept the same way.

    The check itself lives in `api/conformance.py` so a plugin in its own repository can
    run it; this is the in-repo caller that keeps the two from drifting. It reuses
    EVERY_DEVICE rather than re-listing eleven classes, for the reason the sweep below
    states: a twelfth device added to one list and not the other is the drift these sweeps
    exist to remove.
    """

    def test_no_device_inherits_switch_without_being_writable(self):
        report = conformance.check_switch_honesty(*EVERY_DEVICE)
        assert report.checked == tuple(cls.__name__ for cls in EVERY_DEVICE)
        assert report.ok, report.describe()

    def test_the_writable_non_switches_are_exactly_the_ones_we_know_about(self):
        """Pinned, not ignored. `is_writable` and `Switch` are different claims — `Switch`
        promises the COMMAND_ON/COMMAND_OFF tokens, `is_writable` promises only that a
        command can be sent — and `devices/pseudo.py` is deliberately the first without
        the second: it logs whatever string it is given. That is an advisory rather than a
        failure, and asserting the exact set is what stops a *new* writable device drifting
        into the same gap unnoticed."""
        report = conformance.check_switch_honesty(*EVERY_DEVICE)
        assert {finding.plugin for finding in report.advisories} == {"Pseudo"}


class TestEveryDeviceHonoursItsDeclaration:
    """The drift guard.

    The objection that once killed this seam was that a declaration would be "a second copy
    of the labels already in each device's `match`, with nothing testing that the two stay in
    step". The `match` is gone, so there is no second copy — and this sweep is the test that
    was asked for. It reads SUPPORTED_PROTOCOLS off the class rather than re-listing the
    labels in a table here; re-listing them is exactly the duplication the objection was
    about.
    """

    @pytest.mark.parametrize("cls", EVERY_DEVICE, ids=lambda c: c.__name__)
    def test_an_unserved_protocol_is_refused_rather_than_raised(self, cls):
        if not cls.SUPPORTED_PROTOCOLS:
            # devices/pseudo.py claims no protocol and so refuses none — it takes whatever a
            # replay hands it. That is the empty declaration working, not a gap in it.
            pytest.skip(f"{cls.__name__} does not dispatch on protocol")
        device = _build(cls, "carrier_pigeon")
        assert device.receive("a topic", "a payload") is False
        assert device.data == {}

    @pytest.mark.parametrize("cls", EVERY_DEVICE, ids=lambda c: c.__name__)
    def test_an_unserved_protocol_is_reported_once_at_construction(self, cls, caplog):
        if not cls.SUPPORTED_PROTOCOLS:
            pytest.skip(f"{cls.__name__} does not dispatch on protocol")
        with caplog.at_level(logging.ERROR):
            _build(cls, "carrier_pigeon")
        assert len(_errors(caplog)) == 1

    @pytest.mark.parametrize("cls", EVERY_DEVICE, ids=lambda c: c.__name__)
    def test_every_declared_protocol_is_accepted_without_complaint(self, cls, caplog):
        for protocol in cls.SUPPORTED_PROTOCOLS:
            caplog.clear()
            with caplog.at_level(logging.ERROR):
                _build(cls, protocol)
            assert _errors(caplog) == [], f"{cls.__name__} refuses its own {protocol!r}"

    @pytest.mark.parametrize("cls", EVERY_DEVICE, ids=lambda c: c.__name__)
    def test_a_table_entry_can_never_be_unreachable(self, cls):
        # A protocol in both collections would make its sentence dead code, because the
        # supported check runs first.
        assert not set(cls.SUPPORTED_PROTOCOLS) & set(cls.UNSERVABLE_PROTOCOLS)

    @pytest.mark.parametrize("cls", EVERY_DEVICE, ids=lambda c: c.__name__)
    def test_the_declaration_is_a_tuple_of_strings(self, cls):
        # `SUPPORTED_PROTOCOLS = ("mqtt")` is a string, and `in` would then match every
        # substring of it — "qt" would be a served protocol.
        assert isinstance(cls.SUPPORTED_PROTOCOLS, tuple)
        assert all(isinstance(p, str) for p in cls.SUPPORTED_PROTOCOLS)
