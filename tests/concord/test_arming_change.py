"""Who armed or disarmed: stored state, REST shape, MQTT topic, [users]."""

import configparser
import json
import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from concord232 import concord
from concord232.concord import AlarmPanelInterface, compute_checksum
from concord232.main import _load_user_names, _setup_mqtt
from concord232.mqtt_events import PanelMqttPublisher, make_arming_change_handler
from concord232.server import api

FIXED = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
FIXED_ISO = "2026-11-23T12:00:00+00:00"

PARTITION_KEYS = {
    "number",
    "area",
    "arming_level",
    "arming_level_code",
    "partition_text",
    "zones",
}
CHANGE_KEYS = {
    "user_number",
    "user_info",
    "is_keyfob",
    "arming_level",
    "arming_level_code",
    "at",
}


def _arming_msg(
    partition: int, user_number: int, level: int, keyfob: bool = False
) -> list[int]:
    """ARM_LEVEL (0x22/0x01) frame as the panel sends it, with checksum."""
    msg = [0x08, 0x22, 0x01, partition, 0, 1 if keyfob else 0, user_number, level]
    msg.append(compute_checksum(msg))
    return msg


@pytest.fixture
def panel(monkeypatch: pytest.MonkeyPatch) -> AlarmPanelInterface:
    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return FIXED

    monkeypatch.setattr(concord, "datetime", _FixedDatetime)
    return AlarmPanelInterface("fake", 0.25, logging.getLogger("test"))


@pytest.fixture
def rest(panel: AlarmPanelInterface):
    panel.partitions[1] = {
        "partition_number": 1,
        "area_number": 0,
        "arming_level": "Off",
        "arming_level_code": 1,
        "partition_text": "",
    }
    api.CONTROLLER = panel
    with api.app.test_client() as client:
        yield client
    api.CONTROLLER = None


def test_no_change_recorded_at_startup(panel: AlarmPanelInterface) -> None:
    assert panel.last_arming_changes == {}


def test_arming_message_is_decoded_into_stored_state(
    panel: AlarmPanelInterface,
) -> None:
    panel.handle_message(_arming_msg(partition=1, user_number=5, level=3))

    assert panel.last_arming_changes == {
        1: {
            "user_number": 5,
            "user_info": "Regular User 5",
            "is_keyfob": False,
            "arming_level": "Away/Full",
            "arming_level_code": 3,
            "at": FIXED_ISO,
        }
    }


def test_latest_change_wins_and_partitions_are_independent(
    panel: AlarmPanelInterface,
) -> None:
    panel.handle_message(_arming_msg(partition=1, user_number=5, level=3))
    panel.handle_message(_arming_msg(partition=2, user_number=7, level=2))
    panel.handle_message(_arming_msg(partition=1, user_number=246, level=1))

    assert panel.last_arming_changes[1]["user_number"] == 246
    assert panel.last_arming_changes[1]["user_info"] == "System Master Code"
    assert panel.last_arming_changes[1]["arming_level"] == "Off"
    assert panel.last_arming_changes[2]["user_number"] == 7
    assert panel.last_arming_changes[2]["arming_level"] == "Home/Perimeter"


def test_change_is_recorded_before_partition_data_arrives(
    panel: AlarmPanelInterface,
) -> None:
    assert panel.partitions == {}
    panel.handle_message(_arming_msg(partition=1, user_number=5, level=3))

    assert panel.last_arming_changes[1]["user_number"] == 5
    assert panel.partitions == {}


def test_keyfob_is_flagged_and_not_given_a_user_name(
    panel: AlarmPanelInterface,
) -> None:
    panel.user_names = {5: "User 5"}
    panel.handle_message(_arming_msg(partition=1, user_number=5, level=1, keyfob=True))

    change = panel.last_arming_changes[1]
    assert change["is_keyfob"] is True
    assert change["user_number"] == 5
    assert "user_name" not in change


def test_users_mapping_adds_user_name(panel: AlarmPanelInterface) -> None:
    panel.user_names = {5: "User 5"}
    panel.handle_message(_arming_msg(partition=1, user_number=5, level=3))
    panel.handle_message(_arming_msg(partition=2, user_number=6, level=3))

    assert panel.last_arming_changes[1]["user_name"] == "User 5"
    assert set(panel.last_arming_changes[1]) == CHANGE_KEYS | {"user_name"}
    # No entry for user 6: the key is left out rather than guessed.
    assert set(panel.last_arming_changes[2]) == CHANGE_KEYS


def test_load_user_names_from_config() -> None:
    config = configparser.ConfigParser()
    config.read_string("[users]\n1 = User 1\n2 = 100% User 2\n3 =\nbogus = User X\n")
    logger = MagicMock()

    assert _load_user_names(config, logger) == {1: "User 1", 2: "100% User 2"}
    logger.warning.assert_called_once()


def test_load_user_names_without_section() -> None:
    assert _load_user_names(configparser.ConfigParser(), MagicMock()) == {}


def test_rest_last_arming_change_is_null_before_first_change(rest) -> None:
    partitions = rest.get("/partitions").get_json()["partitions"]

    assert len(partitions) == 1
    assert set(partitions[0]) == PARTITION_KEYS | {"last_arming_change"}
    assert partitions[0]["last_arming_change"] is None


def test_rest_exposes_last_arming_change(rest, panel: AlarmPanelInterface) -> None:
    before = rest.get("/partitions").get_json()["partitions"][0]
    panel.user_names = {5: "User 5"}
    panel.handle_message(_arming_msg(partition=1, user_number=5, level=3))
    after = rest.get("/partitions").get_json()["partitions"][0]

    assert after["last_arming_change"] == {
        "user_number": 5,
        "user_info": "Regular User 5",
        "user_name": "User 5",
        "is_keyfob": False,
        "arming_level": "Away/Full",
        "arming_level_code": 3,
        "at": FIXED_ISO,
    }
    # The existing fields keep their names; the panel's own handler (not
    # this feature) is what updates the two arming level fields.
    assert set(after) == set(before)
    for key in ("number", "area", "partition_text", "zones"):
        assert after[key] == before[key]
    assert after["arming_level"] == "Away/Full"
    assert after["arming_level_code"] == 3


def test_rest_tolerates_controller_without_arming_state() -> None:
    api.CONTROLLER = MagicMock()
    api.CONTROLLER.zones = {}
    partition = {
        "partition_number": 1,
        "area_number": 0,
        "arming_level": "Off",
        "arming_level_code": 1,
        "partition_text": "",
    }
    try:
        assert api.show_partition(partition)["last_arming_change"] is None
    finally:
        api.CONTROLLER = None


def test_mqtt_publishes_retained_arming_change(panel: AlarmPanelInterface) -> None:
    client = MagicMock()
    publisher = PanelMqttPublisher(client=client, topic_prefix="home/concord232")
    panel.register_message_handler(
        "ARM_LEVEL", make_arming_change_handler(publisher, panel.last_arming_changes)
    )
    panel.user_names = {5: "User 5"}

    panel.handle_message(_arming_msg(partition=2, user_number=5, level=3))

    client.publish.assert_called_once()
    args, kwargs = client.publish.call_args
    assert args[0] == "home/concord232/partition/2/arming_change"
    assert kwargs == {"qos": 1, "retain": True}
    assert json.loads(args[1]) == {
        "schema_version": 1,
        **panel.last_arming_changes[2],
    }
    assert json.loads(args[1])["user_name"] == "User 5"
    assert json.loads(args[1])["at"] == FIXED_ISO


def test_mqtt_handler_ignores_partition_without_record() -> None:
    client = MagicMock()
    publisher = PanelMqttPublisher(client=client, topic_prefix="concord232")

    make_arming_change_handler(publisher, {})({"partition_number": 1})

    client.publish.assert_not_called()


def test_setup_mqtt_registers_arming_change_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concord232 import main as main_mod

    monkeypatch.setattr(main_mod, "mqtt", MagicMock())
    ctrl = MagicMock()
    _setup_mqtt(
        ctrl,
        host="broker.local",
        port=1883,
        username="",
        password="",
        topic_prefix="concord232",
        client_id="concord232-test",
        publish_touchpad=False,
        publish_zones=False,
        discovery_prefix="",
        tls=False,
        logger=logging.getLogger("test"),
    )

    registered = [c.args[0] for c in ctrl.register_message_handler.call_args_list]
    assert registered == ["ALARM", "ARM_LEVEL"]


def test_nothing_published_without_mqtt(panel: AlarmPanelInterface) -> None:
    # Without _setup_mqtt the only ARM_LEVEL handler is the in-memory recorder.
    assert panel.message_handlers["ARM_LEVEL"] == [panel._record_arming_change]
