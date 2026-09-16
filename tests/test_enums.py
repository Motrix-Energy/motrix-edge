import logging

from config.enums.environment import Environment
from config.enums.logger_level import LoggerLevel
from config.enums.mqtt_version import MQTTVersion
from paho.mqtt.enums import MQTTProtocolVersion


class TestEnvironment:
    def test_all_values(self):
        assert Environment.PROD == "prod"
        assert Environment.TEST == "test"
        assert Environment.DEV == "dev"

    def test_from_string(self):
        assert Environment("prod") is Environment.PROD
        assert Environment("dev") is Environment.DEV


class TestLoggerLevel:
    def test_all_values(self):
        assert LoggerLevel.DEBUG == "debug"
        assert LoggerLevel.INFO == "info"
        assert LoggerLevel.WARNING == "warning"
        assert LoggerLevel.ERROR == "error"
        assert LoggerLevel.CRITICAL == "critical"

    def test_logging_level_mapping(self):
        assert LoggerLevel.DEBUG.logging_level() == logging.DEBUG
        assert LoggerLevel.INFO.logging_level() == logging.INFO
        assert LoggerLevel.WARNING.logging_level() == logging.WARNING
        assert LoggerLevel.ERROR.logging_level() == logging.ERROR
        assert LoggerLevel.CRITICAL.logging_level() == logging.CRITICAL


class TestMQTTVersion:
    def test_all_values(self):
        assert MQTTVersion.MQTTv31 == "3.1.0"
        assert MQTTVersion.MQTTv311 == "3.1.1"
        assert MQTTVersion.MQTTv5 == "5.0.0"

    def test_protocol_version_mapping(self):
        assert MQTTVersion.MQTTv31.mqtt_protocol_version() == MQTTProtocolVersion.MQTTv31
        assert MQTTVersion.MQTTv311.mqtt_protocol_version() == MQTTProtocolVersion.MQTTv311
        assert MQTTVersion.MQTTv5.mqtt_protocol_version() == MQTTProtocolVersion.MQTTv5
