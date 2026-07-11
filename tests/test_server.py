import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


async def wait_until(predicate, timeout=3.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class GatewayConfigurationTests(unittest.TestCase):
    def test_channel_configuration_enables_auto_start(self):
        with patch.dict(server.os.environ, {}, clear=True):
            self.assertTrue(
                server.should_auto_start_gateway({"TELEGRAM_BOT_TOKEN": "token"})
            )

    def test_explicit_false_disables_auto_start(self):
        with patch.dict(server.os.environ, {}, clear=True):
            self.assertFalse(
                server.should_auto_start_gateway(
                    {"TELEGRAM_BOT_TOKEN": "token", "GATEWAY_AUTO_START": "false"}
                )
            )

    def test_explicit_true_enables_auto_start_without_channel(self):
        with patch.dict(server.os.environ, {}, clear=True):
            self.assertTrue(
                server.should_auto_start_gateway({"GATEWAY_AUTO_START": "true"})
            )

    def test_anthropic_credentials_are_masked(self):
        masked = server.mask_secrets(
            {
                "ANTHROPIC_API_KEY": "sk-ant-api-secret",
                "ANTHROPIC_TOKEN": "oauth-secret-token",
            }
        )
        self.assertEqual(masked["ANTHROPIC_API_KEY"], "sk-ant-a***")
        self.assertEqual(masked["ANTHROPIC_TOKEN"], "oauth-se***")


class GatewayManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_planned_hermes_restart_is_respawned_immediately(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            attempts = Path(tmpdir) / "attempts"
            script = """
from pathlib import Path
import sys
import time

path = Path(sys.argv[1])
attempt = int(path.read_text()) + 1 if path.exists() else 1
path.write_text(str(attempt))
print(f"gateway attempt {attempt}", flush=True)
if attempt == 1:
    raise SystemExit(75)
time.sleep(60)
"""
            manager = server.GatewayManager(
                command=(sys.executable, "-c", script, str(attempts)),
                restart_min_delay=0.01,
                restart_max_delay=0.02,
                stable_runtime=0.05,
                stop_timeout=1.0,
            )

            await manager.start()
            await wait_until(
                lambda: attempts.exists()
                and attempts.read_text() == "2"
                and manager.state == "running"
            )

            self.assertEqual(manager.restart_count, 1)
            self.assertEqual(manager.last_exit_code, 75)
            self.assertTrue(manager.desired_running)
            self.assertIn(
                "Gateway stopped (exit code 75); restarting in 0s", manager.logs
            )

            await manager.stop()
            self.assertEqual(manager.state, "stopped")
            self.assertFalse(manager.desired_running)
            self.assertIsNone(manager.process)

    async def test_health_is_degraded_while_expected_gateway_restarts(self):
        manager = server.GatewayManager()
        manager._desired_running = True
        manager.state = "restarting"

        with patch.object(server, "gateway", manager):
            response = await server.health(None)

        self.assertEqual(response.status_code, 503)

        manager.state = "running"
        with patch.object(server, "gateway", manager):
            response = await server.health(None)

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
