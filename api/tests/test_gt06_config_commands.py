"""app/gt06_config_commands.py -- the GT06 configuration command catalog
(SERVER/APN/TIMEZONE/UPLOAD/FILELIST/UPLOADSW/TIMER/ANGLEREP/SOSALM/
COREKITSW/...). A pure module (no database), tested with plain pytest --
same approach as the pure parsing tests on the Go side
(parseGPSBlock/parseVideoEventGroups)."""
import pytest
from pydantic import ValidationError

from app.gt06_config_commands import HIGH_RISK_COMMAND_KEYS, build_raw_command

IMEI = "490154203237518"


def test_corekitsw():
    assert build_raw_command("corekitsw", {}, IMEI) == "COREKITSW,0#"


def test_server_uses_confirmed_production_defaults():
    # mode=1/port=5023 are the defaults confirmed against a physical JC261 --
    # NOT the generic vendor-guide example (mode=0, port 47755).
    text = build_raw_command("server", {"host": "203.0.113.10"}, IMEI)
    assert text == "SERVER,1,203.0.113.10,5023#"


def test_server_custom_mode_and_port():
    text = build_raw_command("server", {"mode": 0, "host": "example.com", "port": 47755}, IMEI)
    assert text == "SERVER,0,example.com,47755#"


def test_server_rejects_invalid_host():
    with pytest.raises(ValidationError):
        build_raw_command("server", {"host": "not a host; DROP TABLE#"}, IMEI)


def test_apn_matches_vendor_documented_template():
    # Example from the vendor guide: "APN,internet,internet,,,,,,,,,,,,#" with
    # empty user/password -- confirms the template's comma count matches
    # exactly.
    text = build_raw_command("apn", {"name": "internet", "apn": "internet"}, IMEI)
    assert text == "APN,internet,internet,,,,,,,,,,,,#"


def test_apn_with_credentials():
    text = build_raw_command("apn", {"name": "internet", "apn": "internet", "user": "u", "password": "p"}, IMEI)
    assert text == "APN,internet,internet,,,,,,u,,p,,,,#"


def test_upload_url_includes_imei():
    # Default port 8083 -- the platform's own ALARM_CLIP_LISTEN_ADDR port, not
    # a generic example from a third-party guide (see the UploadUrlParams
    # docstring).
    text = build_raw_command("upload_url", {"host": "203.0.113.10"}, IMEI)
    assert text == f"UPLOAD,http://203.0.113.10:8083/upload/{IMEI}#"


def test_filelist_url_includes_imei():
    text = build_raw_command("filelist_url", {"host": "203.0.113.10", "port": 8083}, IMEI)
    assert text == f"FILELIST,http://203.0.113.10:8083/filelist/{IMEI}#"


def test_uploadsw_on_and_off():
    assert build_raw_command("uploadsw", {"alarm_type": "SOS", "enabled": True}, IMEI) == "UPLOADSW,SOS,ON#"
    assert build_raw_command("uploadsw", {"alarm_type": "CRASH", "enabled": False}, IMEI) == "UPLOADSW,CRASH,OFF#"


def test_uploadsw_rejects_unknown_alarm_type():
    with pytest.raises(ValidationError):
        build_raw_command("uploadsw", {"alarm_type": "PANIC", "enabled": True}, IMEI)


def test_timezone_valid_offset():
    assert build_raw_command("timezone", {"offset": "-07:00"}, IMEI) == "TIMEZONE,-07:00#"


def test_timezone_rejects_malformed_offset():
    with pytest.raises(ValidationError):
        build_raw_command("timezone", {"offset": "garbage"}, IMEI)


def test_timer_within_bounds():
    assert build_raw_command("timer", {"seconds": 60}, IMEI) == "TIMER,ON,60#"


def test_timer_rejects_out_of_bounds():
    with pytest.raises(ValidationError):
        build_raw_command("timer", {"seconds": 1}, IMEI)
    with pytest.raises(ValidationError):
        build_raw_command("timer", {"seconds": 999999}, IMEI)


def test_anglerep():
    assert build_raw_command("anglerep", {"degrees": 10}, IMEI) == "ANGLEREP,ON,10#"


def test_sosalm():
    assert build_raw_command("sosalm", {}, IMEI) == "SOSALM,ON,0#"


def test_high_risk_keys_include_rservice_and_update_firmware():
    # Contract the frontend uses to decide the "type the device label"
    # friction (same pattern as engine cut) -- if this changes by accident,
    # the safety friction silently disappears. RSERVICE (RTMP destination)
    # and UPDATE (firmware) can leave the device unusable just like
    # SERVER/APN.
    assert HIGH_RISK_COMMAND_KEYS == frozenset({"server", "apn", "rservice", "update_firmware"})


def test_unknown_command_key_raises_keyerror():
    with pytest.raises(KeyError):
        build_raw_command("does_not_exist", {}, IMEI)


# --- Commands confirmed by a second independent vendor-documentation source ---


def test_mileage_without_initial_value():
    assert build_raw_command("mileage", {}, IMEI) == "MILEAGE,ON#"


def test_mileage_with_initial_value():
    assert build_raw_command("mileage", {"initial_meters": 86154000}, IMEI) == "MILEAGE,ON,86154000#"


def test_timesync():
    assert build_raw_command("timesync", {}, IMEI) == "TIMESYNC,gps#"


def test_timer_acc_off():
    assert build_raw_command("timer_acc_off", {"seconds": 3600}, IMEI) == "TIMER1,ON,3600#"


def test_accrep_on_and_off():
    assert build_raw_command("accrep", {"enabled": True}, IMEI) == "ACCREP,ON#"
    assert build_raw_command("accrep", {"enabled": False}, IMEI) == "ACCREP,OFF#"


def test_crashalm_sensitivity():
    assert build_raw_command("crashalm", {"sensitivity": 1}, IMEI) == "CRASHALM,ON,1#"


def test_crashalm_rejects_out_of_range_sensitivity():
    with pytest.raises(ValidationError):
        build_raw_command("crashalm", {"sensitivity": 4}, IMEI)


def test_rapid_sensitivity_commands():
    assert build_raw_command("rapidacc_sensitivity", {"sensitivity": 2}, IMEI) == "RAPIDACC,2#"
    assert build_raw_command("rapiddec_sensitivity", {"sensitivity": 2}, IMEI) == "RAPIDDEC,2#"
    assert build_raw_command("rapidturn_sensitivity", {"sensitivity": 1}, IMEI) == "RAPIDTURN,1#"


def test_rapidtest():
    text = build_raw_command("rapidtest", {"accel_threshold": 30, "decel_threshold": 40, "turn_threshold": 70}, IMEI)
    assert text == "RAPIDTEST,30,40,70#"


def test_reboot():
    assert build_raw_command("reboot", {}, IMEI) == "REBOOT#"


def test_uart_maps_literals_to_documented_codes():
    # UART,<A>,<B>,<C>,<D>,<E>,<F># -- documented reference example
    # "UART,1,0,60,100,1,0".
    text = build_raw_command(
        "uart",
        {
            "trigger_mode": "trigger_on_close",
            "acc_state": "any",
            "interval_seconds": 60,
            "max_speed_kmh": 100,
            "action": "short_video",
            "voice_broadcast": "none",
        },
        IMEI,
    )
    assert text == "UART,1,0,60,100,1,0#"


def test_uart_disabled_and_photo_and_door_sensor():
    text = build_raw_command(
        "uart",
        {
            "trigger_mode": "disabled",
            "acc_state": "acc_off",
            "interval_seconds": 30,
            "max_speed_kmh": 0,
            "action": "photo",
            "voice_broadcast": "door_sensor",
        },
        IMEI,
    )
    assert text == "UART,0,2,30,0,2,2#"


def test_rservice_uses_confirmed_production_defaults():
    # port=1935/app="live" are this platform's real values (ZLMediaKit +
    # GT06_VIDEO_APP) -- never the reference guide's example, which pointed
    # to ANOTHER platform's RTMP server.
    text = build_raw_command("rservice", {"host": "fleet.example.com"}, IMEI)
    assert text == "RSERVICE,rtmp://fleet.example.com:1935/live#"


def test_rservice_rejects_invalid_host():
    with pytest.raises(ValidationError):
        build_raw_command("rservice", {"host": "not a host#"}, IMEI)


# --- Commands from a single source, not yet confirmed against hardware ---


def test_senalm():
    assert build_raw_command("senalm", {"sensitivity": 2}, IMEI) == "SENALM,ON,2#"


def test_recordaudio_and_recordaudio_sub():
    assert build_raw_command("recordaudio", {"enabled": True}, IMEI) == "RECORDAUDIO,1#"
    assert build_raw_command("recordaudio", {"enabled": False}, IMEI) == "RECORDAUDIO,0#"
    assert build_raw_command("recordaudio_sub", {"enabled": True}, IMEI) == "RECORDAUDIO_SUB,1#"


def test_volume():
    assert build_raw_command("volume", {"level": 0}, IMEI) == "VOLUME,0#"


def test_volume_rejects_out_of_range():
    with pytest.raises(ValidationError):
        build_raw_command("volume", {"level": 16}, IMEI)


def test_exdevicesw_uses_2_and_0_not_1_and_0():
    # The documented example uses EXDEVICESW,2 to turn on and EXDEVICESW,0
    # to turn off -- not a simple 1/0 boolean.
    assert build_raw_command("exdevicesw", {"enabled": True}, IMEI) == "EXDEVICESW,2#"
    assert build_raw_command("exdevicesw", {"enabled": False}, IMEI) == "EXDEVICESW,0#"


def test_sensor_and_shock():
    assert build_raw_command("sensor", {"value": 255}, IMEI) == "SENSOR,255#"
    assert build_raw_command("shock", {"value": 20}, IMEI) == "SHOCK,20#"


def test_mile():
    assert build_raw_command("mile", {"use_mph": True}, IMEI) == "MILE,1#"
    assert build_raw_command("mile", {"use_mph": False}, IMEI) == "MILE,0#"


def test_defense_time_and_shutdowntime():
    assert build_raw_command("defense_time", {"minutes": 2}, IMEI) == "DEFENSE_TIME,2#"
    assert build_raw_command("shutdowntime", {"minutes": 30}, IMEI) == "SHUTDOWNTIME,30#"


def test_exbatalm():
    assert build_raw_command("exbatalm", {"mode": 0, "voltage": 115}, IMEI) == "EXBATALM,0,115#"


def test_fatigue():
    assert build_raw_command("fatigue", {"param_a": 4, "param_b": 5}, IMEI) == "FATIGUE,ON,4,5#"


def test_filter():
    assert build_raw_command("filter", {"seconds": 5}, IMEI) == "FILTER,CRASH,5#"


def test_collide_matches_real_example_positionally():
    text = build_raw_command(
        "collide", {"p1": 0, "p2": 225, "p3": 0, "p4": 15, "p5": 6, "p6": 70, "p7": 200}, IMEI
    )
    assert text == "COLLIDE,ON,0,225,0,15,6,70,200#"


def test_video_capture_in_and_out():
    assert build_raw_command("video_capture", {"direction": "in", "duration_seconds": 3}, IMEI) == "Video,in,3s#"
    assert build_raw_command("video_capture", {"direction": "out", "duration_seconds": 3}, IMEI) == "Video,out,3s#"


def test_picture_capture_modes():
    assert build_raw_command("picture_capture", {"mode": "inout"}, IMEI) == "Picture,inout#"
    assert build_raw_command("picture_capture", {"mode": "in"}, IMEI) == "Picture,in#"
    assert build_raw_command("picture_capture", {"mode": "out"}, IMEI) == "Picture,out#"


def test_speed():
    assert build_raw_command("speed", {"p1": 10, "p2": 90, "p3": 1}, IMEI) == "SPEED,ON,10,90,1#"


def test_update_firmware_accepts_real_jimi_ota_url():
    url = "https://jimi-ota.oss-cn-hongkong.aliyuncs.com/JC261_OTA/C261_V1.6.3_250310.1430_TO_C261_V1.8.1.2_250904.1907/update.zip"
    assert build_raw_command("update_firmware", {"url": url}, IMEI) == f"UPDATE,{url}#"


def test_update_firmware_accepts_url_encoded_paths():
    # One of the documented reference URLs has encoded spaces (%20) in the
    # path -- confirms the validation regex does not reject them.
    url = "https://jimi-ota.oss-cn-hongkong.aliyuncs.com/JC261%20Firmware/T%20Card%20Upgrade/KMC28_0_0_STD_JM_C261_V1.9.1.4_260331.1617/update.zip"
    assert build_raw_command("update_firmware", {"url": url}, IMEI) == f"UPDATE,{url}#"


def test_update_firmware_rejects_non_aliyuncs_host():
    # Never an arbitrary host -- not even one that only "looks like"
    # aliyuncs.com through a subdomain/suffix trick.
    with pytest.raises(ValidationError):
        build_raw_command("update_firmware", {"url": "https://evil.example.com/update.zip"}, IMEI)
    with pytest.raises(ValidationError):
        build_raw_command("update_firmware", {"url": "https://aliyuncs.com.evil.example.com/update.zip"}, IMEI)


def test_update_firmware_rejects_non_https():
    with pytest.raises(ValidationError):
        build_raw_command("update_firmware", {"url": "http://jimi-ota.oss-cn-hongkong.aliyuncs.com/update.zip"}, IMEI)


def test_wakeup_query():
    assert build_raw_command("wakeup_query", {}, IMEI) == "WAKEUP_QUERY#"
