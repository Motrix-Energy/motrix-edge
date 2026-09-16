import logging

import pytest

from parsers.obis import OBISParser, crc16_arc, string_to_type
from tests.conftest import build_p1_telegram as _build_p1_telegram


def _recrc(telegram: str) -> str:
    """Recompute the CRC of a telegram whose header was rewritten."""
    marker = telegram.rindex("!")
    body = telegram[:marker + 1]
    return body + format(crc16_arc(body.encode()), "04X") + telegram[marker + 5:]


@pytest.fixture
def parser():
    return OBISParser()


class TestCRC16ARC:
    def test_known_vector(self):
        # Standard CRC-16/ARC test vector: "123456789" -> 0xBB3D
        assert crc16_arc(b"123456789") == 0xBB3D

    def test_empty_input(self):
        assert crc16_arc(b"") == 0x0000

    def test_single_byte(self):
        result = crc16_arc(b"A")
        assert isinstance(result, int)
        assert 0 <= result <= 0xFFFF


class TestStringToType:
    def test_integer(self):
        assert string_to_type("42") == 42
        assert isinstance(string_to_type("42"), int)

    def test_float(self):
        assert string_to_type("3.14") == 3.14
        assert isinstance(string_to_type("3.14"), float)

    def test_string(self):
        assert string_to_type("abc") == "abc"
        assert isinstance(string_to_type("abc"), str)

    def test_zero(self):
        assert string_to_type("0") == 0
        assert isinstance(string_to_type("0"), int)

    def test_negative_int(self):
        # int("-5") works
        assert string_to_type("-5") == -5


class TestOBISParser:
    def test_valid_single_obis_entry(self, parser):
        obis_data = "1-0:1.8.1(001234.567*kWh)\r\n"
        telegram = _build_p1_telegram(obis_data)
        result = parser.parse(telegram)

        assert result != {}
        assert result["model_id"] == "ISK"
        assert result["data"][0]["obis"]["medium"] == 1
        assert result["data"][0]["obis"]["channel"] == 0
        assert result["data"][0]["obis"]["class"] == 1
        assert result["data"][0]["obis"]["instance"] == 8
        assert result["data"][0]["obis"]["attribute"] == 1
        assert result["data"][0]["data"][0]["value"] == 1234.567
        assert result["data"][0]["data"][0]["unit"] == "kWh"

    def test_value_without_unit(self, parser):
        obis_data = "1-0:1.8.1(12345)\r\n"
        telegram = _build_p1_telegram(obis_data)
        result = parser.parse(telegram)

        assert result != {}
        assert result["data"][0]["data"][0]["value"] == 12345
        assert "unit" not in result["data"][0]["data"][0]

    def test_multiple_obis_entries(self, parser):
        obis_data = "1-0:1.8.1(001234.567*kWh)\r\n1-0:1.8.2(005678.910*kWh)\r\n"
        telegram = _build_p1_telegram(obis_data)
        result = parser.parse(telegram)

        assert result != {}
        assert len(result["data"]) == 2
        assert result["data"][0]["obis"]["attribute"] == 1
        assert result["data"][1]["obis"]["attribute"] == 2

    def test_invalid_transmission_format(self, parser):
        result = parser.parse("garbage data")
        assert result == {}

    def test_empty_string(self, parser):
        result = parser.parse("")
        assert result == {}

    def test_crc_mismatch(self, parser):
        obis_data = "1-0:1.8.1(001234.567*kWh)\r\n"
        telegram = _build_p1_telegram(obis_data)
        # Tamper with the CRC (replace last 6 chars with bad CRC)
        tampered = telegram[:-6] + "0000\r\n"
        # Only fails if 0000 doesn't happen to be the real CRC
        if tampered != telegram:
            result = parser.parse(tampered)
            assert result == {}

    def test_crc_field_present(self, parser):
        obis_data = "1-0:1.8.1(001234.567*kWh)\r\n"
        telegram = _build_p1_telegram(obis_data)
        result = parser.parse(telegram)

        assert "crc16" in result
        assert isinstance(result["crc16"], str)
        assert len(result["crc16"]) == 4

    def test_multiple_values_per_obis(self, parser):
        # Multiple values per OBIS: each value followed by space or \r\n
        obis_data = "1-0:1.8.1(001234.567*kWh) (005678.910*kWh)\r\n"
        telegram = _build_p1_telegram(obis_data)
        result = parser.parse(telegram)

        assert result != {}
        assert len(result["data"][0]["data"]) == 2


# A DSMR 5.0 telegram, trimmed to the constructs that matter. Every line here is
# standard-issue meter output, and none of it parsed before 2026-08-02.
DSMR5_LINES = (
    "0-0:96.1.1(4B384547303034303436333935353037)\r\n"
    "1-0:1.8.1(000123.456*kWh)\r\n"
    "1-0:1.8.2(000234.567*kWh)\r\n"
    "0-0:96.14.0(0002)\r\n"
    "1-0:1.7.0(01.193*kW)\r\n"
    "0-0:96.13.0()\r\n"
    "1-0:99.97.0(2)(0-0:96.7.19)(101208152415W)(0000000240*s)(101208151004W)(0000000301*s)\r\n"
    "1-0:32.7.0(220.1*V)\r\n"
    "0-1:24.2.3(101209112500W)(12785.123*m3)\r\n"
)


def _entry(result: dict, code: str) -> dict:
    """Look an OBIS entry up by its A-B:C.D.E code."""
    for entry in result["data"]:
        obis = entry["obis"]
        rebuilt = f"{obis['medium']}-{obis['channel']}:{obis['class']}.{obis['instance']}.{obis['attribute']}"
        if rebuilt == code:
            return entry
    raise AssertionError(f"{code} not in {[e['obis'] for e in result['data']]}")


class TestDSMR5Constructs:
    """Constructs a real DSMR 5.0 meter emits. FULL_PATTERN has to match the whole data
    region in one go, so any one of these failing rejects the entire telegram."""

    def test_adjacent_value_blocks(self, parser):
        # The M-Bus gas register: capture timestamp then reading, no separator between.
        telegram = _build_p1_telegram("0-1:24.2.3(101209112500W)(12785.123*m3)\r\n")
        result = parser.parse(telegram)

        assert result != {}
        blocks = result["data"][0]["data"]
        assert len(blocks) == 2
        assert blocks[0]["value"] == "101209112500W"
        assert "unit" not in blocks[0]
        assert blocks[1]["value"] == 12785.123
        assert blocks[1]["unit"] == "m3"

    def test_space_separated_blocks_still_parse(self, parser):
        # The form the repo's own real telegram uses — relaxing must not replace it.
        telegram = _build_p1_telegram("0-1:24.2.3(101209112500W) (12785.123*m3)\r\n")
        result = parser.parse(telegram)

        assert len(result["data"][0]["data"]) == 2

    def test_empty_value(self, parser):
        # 0-0:96.13.0() is the text-message register with no message pending.
        telegram = _build_p1_telegram("0-0:96.13.0()\r\n")
        result = parser.parse(telegram)

        assert result != {}
        assert result["data"][0]["data"] == [{"value": ""}]

    def test_value_containing_an_obis_reference(self, parser):
        # The power-failure log always embeds an OBIS code as a value, so it carries
        # both "-" and ":" — characters the old value class excluded.
        telegram = _build_p1_telegram("1-0:99.97.0(2)(0-0:96.7.19)(101208152415W)(0000000240*s)\r\n")
        result = parser.parse(telegram)

        assert result != {}
        blocks = result["data"][0]["data"]
        assert [b["value"] for b in blocks] == [2, "0-0:96.7.19", "101208152415W", 240]
        assert blocks[3]["unit"] == "s"

    def test_full_dsmr5_telegram(self, parser):
        telegram = _build_p1_telegram(DSMR5_LINES)
        result = parser.parse(telegram)

        assert result != {}
        assert len(result["data"]) == 9
        assert _entry(result, "1-0:1.8.1")["data"][0]["value"] == 123.456
        assert _entry(result, "0-0:96.13.0")["data"][0]["value"] == ""
        assert _entry(result, "0-1:24.2.3")["data"][1]["value"] == 12785.123
        assert len(_entry(result, "1-0:99.97.0")["data"]) == 6


class TestTelegramTerminator:
    """The CRC covers "/" through "!"; everything after it is transport framing."""

    def test_missing_trailing_crlf(self, parser):
        # What a replay CSV loader or a broker that trims whitespace hands over.
        telegram = _build_p1_telegram("1-0:1.8.1(001234.567*kWh)\r\n")
        result = parser.parse(telegram.rstrip("\r\n"))

        assert result != {}
        assert result["data"][0]["data"][0]["value"] == 1234.567

    def test_trailing_content_after_the_crlf(self, parser):
        # The CRC must be computed from the match, not from the end of the string —
        # a fixed offset silently reads the padding into the checksum.
        telegram = _build_p1_telegram("1-0:1.8.1(001234.567*kWh)\r\n")
        result = parser.parse(telegram + "\r\n\x00")

        assert result != {}
        assert result["data"][0]["data"][0]["value"] == 1234.567

    def test_corrupt_telegram_without_a_terminator_still_fails_crc(self, parser):
        # Relaxing the terminator must not turn the CRC check into a formality.
        telegram = _build_p1_telegram("1-0:1.8.1(001234.567*kWh)\r\n")
        tampered = telegram.rstrip("\r\n")[:-4] + "0000"
        if tampered != telegram.rstrip("\r\n"):
            assert parser.parse(tampered) == {}


class TestPartialTelegram:
    """One register the grammar does not cover must not cost every other reading.

    A vendor extension, a firmware quirk or a new DSMR revision is a routine reason for
    a single line to be unreadable, and discarding the whole telegram over it threw away
    energy readings that parsed perfectly well.
    """

    GOOD = "1-0:1.8.1(001234.567*kWh)\r\n"
    ALSO_GOOD = "1-0:32.7.0(220.1*V)\r\n"

    def test_unreadable_line_is_skipped_and_the_rest_survives(self, parser):
        telegram = _build_p1_telegram(f"{self.GOOD}not-an-obis-record\r\n{self.ALSO_GOOD}")
        result = parser.parse(telegram)

        assert len(result["data"]) == 2
        assert _entry(result, "1-0:1.8.1")["data"][0]["value"] == 1234.567
        assert _entry(result, "1-0:32.7.0")["data"][0]["value"] == 220.1

    def test_skipping_is_reported(self, parser, caplog):
        telegram = _build_p1_telegram(f"{self.GOOD}not-an-obis-record\r\n")
        with caplog.at_level(logging.WARNING):
            parser.parse(telegram)

        assert "not-an-obis-record" in caplog.text

    def test_a_recurring_bad_line_is_reported_once(self, parser, caplog):
        # DSMR 5 meters emit a telegram a second; an unsupported register would
        # otherwise produce a warning per second, forever.
        telegram = _build_p1_telegram(f"{self.GOOD}1-0:99.99.9(a)(b)garbage\r\n")
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                parser.parse(telegram)

        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    def test_obis_code_without_a_value_is_skipped(self, parser):
        telegram = _build_p1_telegram(f"{self.GOOD}1-0:2.8.1\r\n")
        result = parser.parse(telegram)

        assert len(result["data"]) == 1

    def test_trailing_content_after_the_values_is_skipped(self, parser):
        # All-or-nothing per line: a half-understood record is worse than a dropped one.
        telegram = _build_p1_telegram(f"{self.GOOD}1-0:2.8.1(12*kWh)unexpected\r\n")
        result = parser.parse(telegram)

        assert len(result["data"]) == 1
        assert _entry(result, "1-0:1.8.1")

    def test_nothing_readable_is_still_a_failure(self, parser, caplog):
        # A telegram we cannot read at all must not look like a meter reporting zero.
        telegram = _build_p1_telegram("total garbage\r\nmore garbage\r\n")
        with caplog.at_level(logging.ERROR):
            result = parser.parse(telegram)

        assert result == {}
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_a_corrupt_telegram_is_still_rejected_whole(self, parser):
        # Skipping lines must not weaken the checksum: a bad CRC is still a bad message.
        telegram = _build_p1_telegram(self.GOOD)
        tampered = telegram.replace("001234.567", "009999.999")
        assert parser.parse(tampered) == {}


def _build_dsmr2_telegram(obis_lines: str, identification: str = "KMP5 KA6U001585065213") -> str:
    """DSMR 2/3: same shape, but the telegram ends at `!` with no checksum."""
    return f"/{identification}\r\n\r\n{obis_lines}!\r\n"


class TestTelegramHeader:
    """IEC 62056-21 identification, which every DSMR generation shares.

    `/XXXZ<identification>`: three manufacturer characters, then Z — a *baud rate*
    identifier, not a version number, so hard-coding `5` locked out meters that use
    anything else — then a free-form identification that may contain spaces.
    """

    def test_identification_may_contain_spaces(self, parser):
        # Real headers: "/KMP5 KA6U001585065213", "/Ene5\\XS210 ESMR 5.0".
        telegram = _build_p1_telegram("1-0:1.8.1(1*kWh)\r\n").replace(
            "/ISK5\\2M550E-1012", "/Ene5\\XS210 ESMR 5.0", 1)
        telegram = _recrc(telegram)
        result = parser.parse(telegram)

        assert result != {}
        assert result["model_id"] == "Ene"
        assert result["device_id"] == "\\XS210 ESMR 5.0"

    def test_baud_identifier_other_than_five(self, parser):
        telegram = _recrc(_build_p1_telegram("1-0:1.8.1(1*kWh)\r\n").replace("/ISK5", "/ISK2", 1))
        result = parser.parse(telegram)

        assert result != {}
        assert result["model_id"] == "ISK"


class TestDSMR2Telegram:
    """DSMR 2/3 carries no CRC — the telegram simply ends at `!`."""

    def test_parses_without_a_checksum(self, parser):
        result = parser.parse(_build_dsmr2_telegram(
            "0-0:96.1.1(4B384547)\r\n1-0:1.8.1(00012.345*kWh)\r\n"))

        assert len(result["data"]) == 2
        assert _entry(result, "1-0:1.8.1")["data"][0]["value"] == 12.345

    def test_absent_checksum_is_reported_as_absent(self, parser):
        result = parser.parse(_build_dsmr2_telegram("1-0:1.8.1(00012.345*kWh)\r\n"))
        assert result["crc16"] is None

    def test_the_missing_checksum_is_noted_once(self, parser, caplog):
        # Normal for the meter, so it is stated once rather than every telegram.
        with caplog.at_level(logging.INFO):
            for _ in range(4):
                parser.parse(_build_dsmr2_telegram("1-0:1.8.1(00012.345*kWh)\r\n"))

        notices = [r for r in caplog.records if "not verified" in r.message]
        assert len(notices) == 1

    def test_a_mangled_checksum_is_not_mistaken_for_an_absent_one(self, parser):
        """The regression an optional CRC invites: corrupting the checksum into
        non-hex must reject the telegram, not downgrade it to unchecked."""
        telegram = _build_p1_telegram("1-0:1.8.1(001234.567*kWh)\r\n")

        assert parser.parse(telegram[:-6] + "ZZZZ\r\n") == {}
        assert parser.parse(telegram[:-6] + "AB\r\n") == {}

    def test_a_checksummed_telegram_is_still_verified(self, parser):
        telegram = _build_p1_telegram("1-0:1.8.1(001234.567*kWh)\r\n")
        assert parser.parse(telegram[:-6] + "0000\r\n") == {}
