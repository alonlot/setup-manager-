from setup_manager.fleet import match_rule as fleet_match
from setup_manager.agent.setup_agent import match_rule as agent_match
from setup_manager.usbip_local import parse_port_output

LINUX = """\
Imported USB devices
====================
Port 00: <Port in Use> at Full Speed(12Mbps)
       STMicroelectronics : ST-LINK/V2.1 (0483:374b)
       3-1 -> usbip://192.168.1.10:3240/1-1.2
           -> remote bus/dev 001/004
Port 08: <Port in Use> at Super Speed(5000Mbps)
       unknown vendor : unknown product (0bda:8153)
       4-1 -> usbip://[fe80::1]:3240/2-1
           -> remote bus/dev 002/002
"""

WINDOWS = """\
Imported USB devices
====================
Port 01: device in use at High Speed(480Mbps)
         SEGGER : J-Link (1366 : 0105)
         ?-? -> usbip://bench-2.lan:3240/1-3
            -> remote bus/dev 001/003
"""


def test_parse_linux():
    ports = parse_port_output(LINUX)
    assert len(ports) == 2
    p = ports[0]
    assert (p["port"], p["vid"], p["pid"], p["host"], p["remote_port"], p["busid"]) == \
        (0, "0483", "374b", "192.168.1.10", 3240, "1-1.2")
    assert p["name"] == "STMicroelectronics : ST-LINK/V2.1"
    assert p["local_busid"] == "3-1"
    assert (p["remote_busnum"], p["remote_devnum"]) == (1, 4)
    assert ports[1]["host"] == "fe80::1" and ports[1]["busid"] == "2-1" and ports[1]["name"] is None
    assert ports[1]["speed"].startswith("Super Speed")


def test_parse_windows():
    (p,) = parse_port_output(WINDOWS)
    assert (p["port"], p["vid"], p["pid"], p["host"], p["busid"]) == (1, "1366", "0105", "bench-2.lan", "1-3")


def test_parse_empty():
    assert parse_port_output("Imported USB devices\n====================\n") == []


def test_match_rule_same_in_agent_and_manager():
    dev = {"busid": "1-1.2", "vid": "0483", "pid": "374b", "serial": "ABC"}
    cases = {"1-1.2": True, "1-1": False, "0483:374b": True, "0483:*": True, "*:374b": True, "0483:0000": False,
             "0483:374b:abc": True, "0483:374b:XYZ": False, "garbage": False, "": False}
    for rule, want in cases.items():
        assert agent_match(rule, dev) is want, rule
        assert fleet_match(rule, dev) is want, rule
