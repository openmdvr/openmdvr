-- Adds WAKEUP_QUERY (wake the device from hibernation, per the Jimi IoT Hub
-- documentation) to the GT06 configuration command catalog; see
-- api/app/gt06_config_commands.py. Only widens the command_key CHECK.
ALTER TABLE device_config_commands DROP CONSTRAINT device_config_commands_command_key_check;
ALTER TABLE device_config_commands ADD CONSTRAINT device_config_commands_command_key_check CHECK (command_key IN (
    'corekitsw', 'server', 'apn', 'upload_url', 'filelist_url',
    'uploadsw', 'timezone', 'timer', 'anglerep', 'sosalm',
    'mileage', 'timesync', 'timer_acc_off', 'accrep', 'crashalm',
    'rapidacc_sensitivity', 'rapiddec_sensitivity', 'rapidturn_sensitivity',
    'rapidtest', 'reboot', 'uart', 'rservice',
    'senalm', 'recordaudio', 'recordaudio_sub', 'volume', 'exdevicesw',
    'sensor', 'shock', 'mile', 'defense_time', 'shutdowntime', 'exbatalm',
    'fatigue', 'filter', 'collide', 'video_capture', 'picture_capture',
    'speed', 'update_firmware',
    'wakeup_query'
));
