from enum import StrEnum

# noinspection PyUnresolvedReferences
from paho.mqtt.enums import MQTTProtocolVersion


class MQTTVersion(StrEnum):
	MQTTv31 = "3.1.0"
	MQTTv311 = "3.1.1"
	MQTTv5 = "5.0.0"

	def mqtt_protocol_version(self) -> MQTTProtocolVersion:
		return {
			self.MQTTv31: MQTTProtocolVersion.MQTTv31,
			self.MQTTv311: MQTTProtocolVersion.MQTTv311,
			self.MQTTv5: MQTTProtocolVersion.MQTTv5,
		}[self]
