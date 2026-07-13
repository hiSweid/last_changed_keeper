"""Shared test fixtures.

Not autouse: `recorder_mock` (where needed) must be requested before `hass`
is instantiated, so tests that load the config/options flow request
`recorder_mock` and `enable_custom_integrations` explicitly, in that order.
"""
