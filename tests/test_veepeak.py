import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


SOURCE = Path(__file__).resolve().parents[1] / "emma_veepeak_obd/rootfs/app/veepeak.py"


class SimulatedElmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cache = Path(temp.name) / "last_values.json"
        mqtt = MagicMock()
        self.mqtt_client = mqtt.Client.return_value
        fake_bleak = SimpleNamespace(BleakScanner=MagicMock(), BleakClient=MagicMock())
        options = json.dumps({
            "ble_address": "02:00:00:00:00:01",
            "mqtt_username": "test",
        })
        with (
            patch.dict(sys.modules, {
                "bleak": fake_bleak,
                "paho": SimpleNamespace(mqtt=SimpleNamespace(client=mqtt)),
                "paho.mqtt": SimpleNamespace(client=mqtt),
                "paho.mqtt.client": mqtt,
            }),
            patch("pathlib.Path.read_text", side_effect=[options, FileNotFoundError, FileNotFoundError]),
            patch("pathlib.Path.mkdir"),
            patch("logging.handlers.RotatingFileHandler", return_value=MagicMock(level=0)),
        ):
            namespace = runpy.run_path(str(SOURCE))
        self.app = namespace["pub"].__globals__
        self.app["CACHE"] = self.cache
        self.app["record"] = MagicMock()

    def elm(self, reply):
        characteristic = SimpleNamespace(uuid=self.app["BLE_PROFILES"]["fff0"][1], properties=["write-without-response"])
        service = SimpleNamespace(
            uuid="0000fff0-0000-1000-8000-00805f9b34fb",
            characteristics=[characteristic, SimpleNamespace(
                uuid=self.app["BLE_PROFILES"]["fff0"][2], properties=["notify"],
            )],
        )
        client = SimpleNamespace(services=[service])
        elm = self.app["Elm"](client)

        async def write(target, payload, response):
            self.assertEqual(target, characteristic)
            self.assertEqual(payload, b"010C\r")
            self.assertFalse(response)
            if reply:
                elm.rx(None, reply)

        client.write_gatt_char = MagicMock(side_effect=write)
        return elm, client

    async def test_elm_reads_rpm_and_records_raw_response(self):
        elm, _ = self.elm(b"41 0C 1A F8\r>")
        response = await elm.cmd("010C")
        self.assertEqual(self.app["mode01"](response, 0x0C), [0x1A, 0xF8])
        self.assertEqual(self.app["record"].call_args.kwargs["raw"], "41 0C 1A F8\r>")

    async def test_elm_rejects_writes_before_transport(self):
        elm, client = self.elm(None)
        for command in ("04", "2E123400", "ATSH7E0", "010C\r04"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                await elm.cmd(command)
        client.write_gatt_char.assert_not_called()

    async def test_elm_logs_timeout(self):
        elm, _ = self.elm(None)
        with self.assertRaises(TimeoutError):
            await elm.cmd("010C", timeout=0.01)
        self.assertEqual(self.app["record"].call_args.args[0], "command_error")

    def test_last_valid_cache_and_discovery(self):
        self.app["setup_discovery"]()
        messages = self.mqtt_client.publish.call_args_list
        rpm = json.loads(messages[0].args[1])
        self.assertEqual(rpm["name"], "Engine speed")
        self.assertEqual(rpm["unique_id"], "emma_veepeak_rpm")
        self.app["pub"]("rpm", 1726)
        self.app["pub"]("rpm", "NO DATA")
        self.assertEqual(json.loads(self.cache.read_text())["rpm"]["value"], 1726)
        self.mqtt_client.publish.reset_mock()
        self.app["replay"]()
        self.mqtt_client.publish.assert_any_call("emma/obd/rpm", 1726, retain=True)

    def test_selects_known_profiles_and_write_mode(self):
        for profile, (service_id, tx_id, rx_id) in self.app["BLE_PROFILES"].items():
            with self.subTest(profile=profile):
                tx = SimpleNamespace(uuid=tx_id, properties=["write"])
                rx = SimpleNamespace(uuid=rx_id, properties=["notify"])
                if tx_id == rx_id:
                    tx.properties.append("notify")
                    rx = tx
                chars = [tx] if tx is rx else [tx, rx]
                service = SimpleNamespace(uuid=service_id, characteristics=chars)
                select = self.app["select_ble_transport"]
                self.assertEqual(select([service], "auto"), (profile, tx, rx, True))
                self.assertEqual(select([service], profile), (profile, tx, rx, True))
                with self.assertRaises(RuntimeError):
                    select([service], "fff0" if profile != "fff0" else "ffe0")

    def test_rejects_unknown_service_and_incomplete_profile(self):
        _, tx_id, rx_id = self.app["BLE_PROFILES"]["fff0"]
        tx = SimpleNamespace(uuid=tx_id, properties=["write"])
        rx = SimpleNamespace(uuid=rx_id, properties=["read"])
        service = SimpleNamespace(uuid="unknown", characteristics=[tx, rx])
        select = self.app["select_ble_transport"]
        with self.assertRaises(RuntimeError):
            select([service], "auto")
        service.uuid = self.app["BLE_PROFILES"]["fff0"][0]
        with self.assertRaises(RuntimeError):
            select([service], "auto")
