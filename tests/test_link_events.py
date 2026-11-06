"""analysis.link_events: PM_Action writes, including a write that starts below 0x1011 and
runs through it.

Run: python3 -m pytest tests/test_link_events.py
"""
from swi3s_studio.analysis import link_events
from swi3s_studio.model import RegisterMap


def _write(address, data):
    return {"command": "WriteA32", "crc_valid": True, "has_address": True,
            "address": address, "data": bytes(data), "device_mask": 1}


def test_a_multi_byte_write_through_pm_action_is_an_event():
    rm = RegisterMap.load()
    alone = link_events([_write(0x1011, [0x02])], rm)
    through = link_events([_write(0x1010, [0x00, 0x02])], rm)
    assert len(alone) == 1 and len(through) == 1
    assert alone[0]["action"] == through[0]["action"] == "Force_Resync"   # the register's label
    assert through[0]["value"] == 2


def test_without_a_register_map_an_action_reads_as_its_hex_value():
    events = link_events([_write(0x1011, [0x02]), _write(0x1010, [0x00, 0x02])], None)
    assert [e["action"] for e in events] == ["0x2", "0x2"]
    assert [e["value"] for e in events] == [2, 2]


def test_writes_that_stop_short_of_or_start_after_pm_action_are_not_events():
    rm = RegisterMap.load()
    assert link_events([_write(0x1010, [0x02]), _write(0x1012, [0x02]),
                        _write(0x100F, [0x01, 0x02])], rm) == []
