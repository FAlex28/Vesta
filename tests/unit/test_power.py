from app.integrations.power import parse_throttled, read_power_status


def test_parse_throttled():
    assert parse_throttled(0x0).state == "POWER_OK"
    assert parse_throttled(0x1).state == "LOW_VOLTAGE"
    assert parse_throttled(0x50005).state == "LOW_VOLTAGE"
    assert parse_throttled(0x50000).state == "POWER_UNSTABLE"
    thermal = parse_throttled(0x6)
    assert thermal.state == "POWER_OK" and thermal.thermal_throttling


def test_unknown_when_no_vcgencmd():
    assert read_power_status(lambda: None).state == "POWER_STATUS_UNKNOWN"
    assert read_power_status(lambda: "garbage").state == "POWER_STATUS_UNKNOWN"

    def boom():
        raise OSError("no such file")

    assert read_power_status(boom).state == "POWER_STATUS_UNKNOWN"


def test_no_battery_percentage_exposed():
    st = parse_throttled(0)
    assert not any("percent" in f or "battery" in f for f in st.__dataclass_fields__)
