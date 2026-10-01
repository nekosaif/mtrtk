from mtrtk.rover.drivers.sbg.ids import CLASS, CMD, CMD_NAME, LOG, LOG_NAME


def test_generated_ids() -> None:
    assert LOG["EKF_NAV"] == 8 and LOG["GPS1_SAT"] == 50
    assert CMD["OUTPUT_CONF"] == 30 and CMD["GNSS_1_INSTALLATION"] == 46
    assert CLASS["CMD_0"] == 0x10 and CLASS["LOG_CMD_0"] == 0x10 and CLASS["LOG_ECOM_0"] == 0
    assert LOG_NAME[31] == "GPS1_RAW" and CMD_NAME[0] == "ACK"


def test_log_ids_come_from_the_ecom_0_enum_only() -> None:
    """The NMEA / FAST_IMU enums reuse the SBG_ECOM_LOG_ prefix with overlapping values."""
    assert "NMEA_GGA" not in LOG and "FAST_IMU_DATA" not in LOG and "ECOM_NUM_MESSAGES" not in LOG
    assert LOG["STATUS"] == 1 and LOG["IMU_SHORT"] == 44 and LOG["SESSION_INFO"] == 55
