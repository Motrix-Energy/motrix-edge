from logging import Logger, getLogger
from re import Match, Pattern, compile
from typing import Optional, Union, override

from api.parser import Parser


class OBISParser(Parser):
	LOGGER: Logger

	OBIS_STRING: str = r"(?P<obis_medium>\w+)-(?P<obis_channel>\w+):(?P<obis_class>\w+)\.(?P<obis_instance>\w+)\.(?P<obis_attribute>\w+)"
	# In DSMR the parentheses *are* the delimiter, so exclude only what actually
	# delimits. A value can be empty (`0-0:96.13.0()`, the text-message register with
	# no message) and can hold `-` and `:` (the power-failure log embeds an OBIS
	# reference: `1-0:99.97.0(2)(0-0:96.7.19)(...)`). `*` stays out because it
	# introduces the unit; CR/LF stay out so a missing `)` fails cleanly instead of
	# swallowing the rest of the telegram. The trailing separator is optional, not
	# gone: consecutive blocks are usually adjacent — `(timestamp)(reading*m3)` — but
	# real meters also emit the space form, and `?` is greedy so a line's CRLF is
	# still consumed and the OBIS loop still lands on the next code.
	VALUE_STRING: str = r"\((?P<value>[^)*\r\n]*)(?:\*(?P<unit>\w+))?\)(?: |\r\n)?"

	# The identification is IEC 62056-21's `/XXXZ<identification>`: three manufacturer
	# characters, then Z — a *baud rate* identifier, not a version number, so requiring
	# a literal `5` locked out meters that use anything else — then a free-form
	# identification that real headers put spaces in (`/KMP5 KA6U001585065213`,
	# `/Ene5\XS210 ESMR 5.0`), hence "anything up to the line break" rather than \S+.
	#
	# The data region is captured wholesale rather than validated here, and each record
	# is checked line by line in parse(): requiring the grammar to match the whole
	# region in one pass meant a single unreadable line — a vendor extension, a firmware
	# quirk — discarded every other reading in the telegram. `!` terminates the data per
	# the spec and cannot appear inside it.
	#
	# The checksum is optional because DSMR 2/3 has none: those telegrams simply end at
	# `!`. The lookahead is what keeps that from weakening DSMR 4/5 — without it a
	# checksum corrupted into non-hex would match the "absent" branch and be waved
	# through unverified. Requiring a line break or end-of-input straight after means a
	# mangled checksum is a malformed telegram, as it should be. Nothing past `!` is
	# consumed, so a trimmed terminator and trailing padding both remain acceptable:
	# that framing sits outside the checksum's range and is not part of the message.
	FULL_PATTERN: Pattern = compile(r"/(?P<model_id>\w{3})\w(?P<device_id>[^\r\n]*)\r\n\r\n(?P<data>[^!]*)!(?P<crc16>[0-9A-Fa-f]{4})?(?=\r\n|\n|$)")
	OBIS_PATTERN: Pattern = compile(OBIS_STRING)
	VALUE_PATTERN: Pattern = compile(VALUE_STRING)
	LINE_PATTERN: Pattern = compile(r"\r\n|\n")

	@override
	def __init__(self) -> None:
		self.LOGGER = getLogger(__class__.__name__)
		self._unreadable_records: set[str] = set()
		self._unchecked_reported = False

	@override
	def parse(self, transmission: str) -> dict:
		full_match: Optional[Match[str]] = self.FULL_PATTERN.match(transmission)
		if not full_match:
			self.LOGGER.error("Invalid transmission")
			self.LOGGER.debug(f"{transmission=}")
			return {}
		crc16 = full_match.group("crc16")
		if crc16 is None:
			self._report_unchecked()
		else:
			given_crc: int = int(crc16, 16)
			# DSMR computes the CRC over "/" through "!" inclusive. Taken from where the
			# data group ends — the index of the "!" — rather than a fixed offset from
			# the end of the string: that offset assumed the message ended in precisely
			# `CCCC\r\n`, so a trimmed terminator or any trailing padding fed the wrong
			# bytes to the checksum and reported a corrupt telegram that was fine.
			computed_crc: int = crc16_arc(transmission[:full_match.end("data") + 1].encode())
			if computed_crc != given_crc:
				self.LOGGER.error(f"Invalid CRC: {hex(given_crc)=}, {hex(computed_crc)=}")
				self.LOGGER.debug(f"{transmission=}")
				return {}
		parse_data = []
		for line in self.LINE_PATTERN.split(full_match.group("data")):
			if not line:
				continue
			record = self._parse_record(line)
			if record is None:
				self._report_unreadable(line)
				continue
			parse_data.append(record)
		if not parse_data:
			# The checksum passed but nothing in it was readable. Returning a telegram
			# with no records would read downstream as a meter reporting nothing, which
			# is a very different claim from "this could not be understood".
			self.LOGGER.error("No readable OBIS record in transmission")
			self.LOGGER.debug(f"{transmission=}")
			return {}
		return {
			"model_id": string_to_type(full_match.group("model_id")),
			"device_id": string_to_type(full_match.group("device_id")),
			"data": parse_data,
			"crc16": full_match.group("crc16")
		}

	def _parse_record(self, line: str) -> Optional[dict]:
		"""One OBIS record, or None when the line does not conform.

		A record is all-or-nothing: the line must be an OBIS reference followed by value
		blocks and nothing else. Half-understanding a record we do not recognise — taking
		the values we could read and ignoring whatever followed them — would invent a
		reading rather than admit the line was unsupported.
		"""
		obis_match = self.OBIS_PATTERN.match(line)
		if not obis_match:
			return None
		rest = line[obis_match.end():]
		blocks = []
		value_match = self.VALUE_PATTERN.match(rest)
		while value_match:
			rest = rest[value_match.end():]
			block = {"value": string_to_type(value_match.group("value"))}
			if value_match.group("unit"):
				block["unit"] = string_to_type(value_match.group("unit"))
			blocks.append(block)
			value_match = self.VALUE_PATTERN.match(rest)
		if not blocks or rest:
			return None
		return {
			"obis": {
				"medium": string_to_type(obis_match.group("obis_medium")),
				"channel": string_to_type(obis_match.group("obis_channel")),
				"class": string_to_type(obis_match.group("obis_class")),
				"instance": string_to_type(obis_match.group("obis_instance")),
				"attribute": string_to_type(obis_match.group("obis_attribute"))
			},
			"data": blocks
		}

	def _report_unchecked(self) -> None:
		"""Note once that this meter's telegrams carry no checksum.

		Normal for DSMR 2/3 rather than a fault, so it is not a warning and not repeated
		— but it is worth saying once, because it means corruption on the line cannot be
		detected and every reading below is taken on trust.
		"""
		if self._unchecked_reported:
			return
		self._unchecked_reported = True
		self.LOGGER.info("Telegram carries no checksum (DSMR 2/3), so its integrity is not verified")

	def _report_unreadable(self, line: str) -> None:
		"""Report a skipped record once per register, then quietly.

		A meter that emits an unsupported register emits it in every telegram — once a
		second on DSMR 5 — so warning every time would bury the log in one recurring
		fact. Keyed on the OBIS reference so a register whose *value* changes each
		telegram is still recognised as the same unreadable record.
		"""
		obis_match = self.OBIS_PATTERN.match(line)
		key = obis_match.group(0) if obis_match else line
		message = f"Skipping unreadable OBIS record: {line!r}"
		if key in self._unreadable_records:
			self.LOGGER.debug(message)
			return
		self._unreadable_records.add(key)
		self.LOGGER.warning(message)


def string_to_type(value: str) -> Union[int, float, str]:
	"""
	This function is going to convert a string to a type
	:param value: the value to convert
	:return: the converted value
	"""
	try:
		return int(value)
	except ValueError:
		try:
			return float(value)
		except ValueError:
			return value


def crc16_arc(data: bytes) -> int:
	"""
	This function is the implementation of the CRC16-ARC algorithm https://crccalc.com/?crc=123456789&method=CRC-16/ARC&datatype=ascii&outtype=hex
	:param data: the data to calculate the CRC
	:return: the CRC16-ARC of the data
	"""
	poly: int = 0xA001
	crc: int = 0x0000
	for byte in data:
		crc ^= byte
		for _ in range(8):
			if crc & 0x0001:
				crc = (crc >> 1) ^ poly
			else:
				crc >>= 1
	return crc & 0xFFFF
