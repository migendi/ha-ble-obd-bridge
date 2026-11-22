# EMMA BLE OBD Bridge

A Home Assistant app that reads vehicle telemetry from ELM-compatible BLE OBD adapters and publishes sensors via MQTT Discovery. The FFF0 profile was verified with a Veepeak OBDCheck BLE on HAEmma. The FFE0 and Nordic UART profiles have simulation coverage. This public package still needs its own ARM64 build and vehicle smoke test.

## Setup

1. Use Home Assistant OS on ARM64 with a local Bluetooth adapter and an MQTT broker. Enable the MQTT integration for discovery.
2. Once this repository is hosted on GitHub, add its URL in the Home Assistant app store under **Repositories**, then install **EMMA Veepeak OBD**.
3. Enter the adapter's Bluetooth address in `ble_address`. Leave `ble_profile` at `auto` or select `fff0`, `ffe0` or `nordic_uart`. Use the MQTT service provided by the Supervisor or enter the broker and its credentials in the app settings.
4. Plug in the adapter, turn on the ignition and disconnect other clients from the adapter. Start the app and check the logs and newly discovered sensors.

Engine speed, road speed, coolant temperature and ECU voltage are read periodically. Additional sensors depend on the vehicle's supported Mode 01 PIDs. Fuel level is reported as a percentage. Sensor attributes expose the time of the last valid measurement, which may predate the current trip when the vehicle is offline.

The app allows only the ELM setup commands and read requests implemented in the source. The recorder writes raw responses to `/share/OBD_Captures/obd_flight.jsonl` and rotates at 8 MiB with seven backups. Review recordings before sharing them because they contain vehicle telemetry.

## Tests

Run `python -m unittest discover -s tests -v` from the repository root. The tests use simulated ELM responses and mock BLE, MQTT and file logging. Hardware acceptance requires an ARM64 installation with AppArmor enabled and a physical adapter.
