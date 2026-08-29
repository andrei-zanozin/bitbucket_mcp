import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp.server.mcpserver.exceptions import ToolError

import bitbucket_mcp


class ConfigurationTest(unittest.TestCase):
    def load_settings(
        self, config: str | None, environment: dict[str, str] | None = None
    ) -> bitbucket_mcp.Settings:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "bitbucket_mcp.py"
            if config is not None:
                script.with_name("config.yml").write_text(config, encoding="utf-8")
            with (
                patch.object(bitbucket_mcp, "__file__", str(script)),
                patch.dict(os.environ, environment or {}, clear=True),
            ):
                return bitbucket_mcp._load_settings()

    def test_loads_required_settings_and_defaults(self) -> None:
        settings = self.load_settings(
            """
bitbucket:
  base_url: https://bitbucket.example.com/
  token: token
  user_slug: user
"""
        )

        self.assertEqual(
            settings,
            bitbucket_mcp.Settings(
                bitbucket=bitbucket_mcp.BitbucketSettings(
                    "https://bitbucket.example.com/rest/api/1.0",
                    "token",
                    "user",
                    30.0,
                )
            ),
        )

    def test_loads_environment_references_and_optional_settings(self) -> None:
        settings = self.load_settings(
            """
bitbucket:
  base_url: ${BITBUCKET_BASE_URL}
  token: ${BITBUCKET_TOKEN}
  user_slug: ${BITBUCKET_USER}
  api_prefix: ${BITBUCKET_API_PREFIX}
  timeout: 5
""",
            {
                "BITBUCKET_BASE_URL": "https://bitbucket.example.com",
                "BITBUCKET_TOKEN": "token",
                "BITBUCKET_USER": "user",
                "BITBUCKET_API_PREFIX": "/rest/custom",
            },
        )

        self.assertEqual(
            settings,
            bitbucket_mcp.Settings(
                bitbucket=bitbucket_mcp.BitbucketSettings(
                    "https://bitbucket.example.com/rest/custom",
                    "token",
                    "user",
                    5.0,
                )
            ),
        )

    def test_loads_anonymous_proxy(self) -> None:
        settings = self.load_settings(
            """
bitbucket:
  base_url: https://bitbucket.example.com
  token: token
  user_slug: user
proxy:
  server: https://proxy.example.com:8443
"""
        )

        self.assertEqual(
            settings.proxy,
            bitbucket_mcp.ProxySettings("https://proxy.example.com:8443"),
        )

    def test_loads_authenticated_proxy_from_environment(self) -> None:
        settings = self.load_settings(
            """
bitbucket:
  base_url: https://bitbucket.example.com
  token: token
  user_slug: user
proxy:
  server: ${PROXY_SERVER}
  username: ${PROXY_USERNAME}
  password: ${PROXY_PASSWORD}
""",
            {
                "PROXY_SERVER": "https://proxy.example.com",
                "PROXY_USERNAME": "proxy-user",
                "PROXY_PASSWORD": "proxy-password",
            },
        )

        self.assertEqual(
            settings.proxy,
            bitbucket_mcp.ProxySettings(
                "https://proxy.example.com", "proxy-user", "proxy-password"
            ),
        )

    def test_rejects_missing_configuration(self) -> None:
        with self.assertRaisesRegex(
            bitbucket_mcp.ConfigurationError, "Missing configuration file"
        ):
            self.load_settings(None)

    def test_rejects_unreadable_configuration(self) -> None:
        with (
            patch.object(Path, "read_text", side_effect=OSError),
            self.assertRaisesRegex(
                bitbucket_mcp.ConfigurationError, "Cannot read configuration file"
            ),
        ):
            bitbucket_mcp._load_settings()

    def test_rejects_invalid_configuration(self) -> None:
        cases = {
            "invalid YAML": ("bitbucket: [", "not valid YAML"),
            "missing mapping": ("other: value", "'bitbucket' mapping"),
            "missing base URL": (
                "bitbucket:\n  token: token\n  user_slug: user",
                "base_url must be a non-empty string",
            ),
            "missing token": (
                "bitbucket:\n  base_url: https://example.com\n  user_slug: user",
                "token must be a non-empty string",
            ),
            "missing user": (
                "bitbucket:\n  base_url: https://example.com\n  token: token",
                "user_slug must be a non-empty string",
            ),
            "missing environment variable": (
                "bitbucket:\n  base_url: https://example.com\n  token: ${MISSING_TOKEN}\n  user_slug: user",
                "MISSING_TOKEN must be a non-empty string",
            ),
            "invalid URL": (
                "bitbucket:\n  base_url: example.com\n  token: token\n  user_slug: user",
                "base_url must resolve",
            ),
            "malformed URL": (
                "bitbucket:\n  base_url: http://example.com:bad\n  token: token\n  user_slug: user",
                "base_url must resolve",
            ),
            "invalid API prefix": (
                "bitbucket:\n  base_url: https://example.com\n  token: token\n  user_slug: user\n  api_prefix: ../api",
                "api_prefix must be a relative URL path",
            ),
            "invalid timeout": (
                "bitbucket:\n  base_url: https://example.com\n  token: token\n  user_slug: user\n  timeout: 0",
                "timeout must be a number",
            ),
            "invalid proxy mapping": (
                "bitbucket:\n  base_url: https://example.com\n  token: token\n  user_slug: user\nproxy:",
                "proxy must be a mapping",
            ),
            "missing proxy server": (
                "bitbucket:\n  base_url: https://example.com\n  token: token\n  user_slug: user\nproxy: {}",
                "server must be a non-empty string",
            ),
            "non-HTTPS proxy": (
                "bitbucket:\n  base_url: https://example.com\n  token: token\n  user_slug: user\nproxy:\n  server: http://proxy.example.com",
                "server must resolve to an HTTPS URL",
            ),
            "proxy with credentials in URL": (
                "bitbucket:\n  base_url: https://example.com\n  token: token\n  user_slug: user\nproxy:\n  server: https://user:password@proxy.example.com",
                "server must resolve to an HTTPS URL",
            ),
            "incomplete proxy authentication": (
                "bitbucket:\n  base_url: https://example.com\n  token: token\n  user_slug: user\nproxy:\n  server: https://proxy.example.com\n  username: user",
                "username and password must be configured together",
            ),
        }

        for name, (config, message) in cases.items():
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(bitbucket_mcp.ConfigurationError, message),
            ):
                self.load_settings(config)

    def test_tool_validation_still_uses_tool_error(self) -> None:
        with self.assertRaises(ToolError):
            bitbucket_mcp._required_string("", "text")

    def test_main_validates_configuration_before_server_launch(self) -> None:
        error = bitbucket_mcp.ConfigurationError("invalid configuration")
        previous_settings = object()
        with (
            patch.object(bitbucket_mcp, "settings", previous_settings, create=True),
            patch.object(bitbucket_mcp, "_load_settings", side_effect=error),
            patch.object(bitbucket_mcp.mcp, "run") as run,
            self.assertRaises(bitbucket_mcp.ConfigurationError),
        ):
            bitbucket_mcp.main()

        run.assert_not_called()

    def test_main_starts_server_after_configuration_validation(self) -> None:
        loaded_settings = bitbucket_mcp.Settings(
            bitbucket=bitbucket_mcp.BitbucketSettings(
                "https://example.com/rest/api/1.0", "token", "user", 30.0
            )
        )

        def assert_settings_initialized() -> None:
            self.assertIs(bitbucket_mcp.settings, loaded_settings)

        with (
            patch.object(bitbucket_mcp, "settings", create=True),
            patch.object(
                bitbucket_mcp, "_load_settings", return_value=loaded_settings
            ) as load_settings,
            patch.object(
                bitbucket_mcp.mcp, "run", side_effect=assert_settings_initialized
            ) as run,
        ):
            bitbucket_mcp.main()

        load_settings.assert_called_once_with()
        run.assert_called_once_with()

class BitbucketAPITest(unittest.IsolatedAsyncioTestCase):
    settings = bitbucket_mcp.BitbucketSettings(
        "https://bitbucket.example.com/rest/api/1.0", "token", "user", 30.0
    )

    async def test_disables_environment_proxy_without_proxy_settings(self) -> None:
        client = AsyncMock()
        with patch.object(bitbucket_mcp.httpx, "AsyncClient", return_value=client) as init:
            async with bitbucket_mcp.BitbucketAPI(self.settings):
                pass

        self.assertIsNone(init.call_args.kwargs["proxy"])
        self.assertFalse(init.call_args.kwargs["trust_env"])
        client.aclose.assert_awaited_once_with()

    async def test_configures_authenticated_proxy(self) -> None:
        client = AsyncMock()
        proxy_settings = bitbucket_mcp.ProxySettings(
            "https://proxy.example.com:8443", "proxy-user", "proxy-password"
        )
        with patch.object(bitbucket_mcp.httpx, "AsyncClient", return_value=client) as init:
            async with bitbucket_mcp.BitbucketAPI(self.settings, proxy_settings):
                pass

        proxy = init.call_args.kwargs["proxy"]
        self.assertEqual(proxy.url, bitbucket_mcp.httpx.URL(proxy_settings.server))
        self.assertEqual(proxy.auth, ("proxy-user", "proxy-password"))
        self.assertFalse(init.call_args.kwargs["trust_env"])


if __name__ == "__main__":
    unittest.main()
