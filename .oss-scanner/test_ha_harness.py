"""Load hoval_connect into a real Home Assistant, served by a fake Hoval cloud.

A starting point for reproducers, for the OSS Scanner and for anyone else. The
tests in tests/ stub Home Assistant out, so they cannot show what real core does
with a value: whether a service schema rejects it, what the diagnostics download
redacts, what an entity attribute ends up holding. This file can. Run it from the
repository root with the Home Assistant venv the scanner image installs:

    /opt/ha/bin/python -m pytest .oss-scanner/test_ha_harness.py

It is kept out of tests/ because CI does not install Home Assistant.

The cloud is a real aiohttp server on 127.0.0.1, and the integration's two base
URLs are pointed at it. Edit `responses` to craft what the cloud answers; every
request is recorded in `cloud` with its headers. `aioclient_mock` does not work
here: its fake response has no `content_length`, which `_request` reads on
every reply, so setup fails before the integration does anything.
"""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hoval_connect import api
from custom_components.hoval_connect.const import DOMAIN

PLANT = "123456789012345"


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    """Let Home Assistant load integrations from custom_components/."""


@pytest.fixture
async def cloud(socket_enabled, monkeypatch):
    """Serve the Hoval cloud and the IDP from one local server; yield the requests seen."""
    responses = {
        ("POST", "/oauth2/token"): {"id_token": "id-token"},
        ("GET", "/core/api/my-plants"): {
            "content": [{"plantExternalId": PLANT, "description": "Home", "isOnline": True}],
            "last": True,
        },
        ("GET", f"/core/v1/plants/{PLANT}/settings"): {"token": "plant-access-token"},
        ("GET", f"/core/v3/plants/{PLANT}/circuits"): [
            {
                "path": "520.50.0",
                "type": "HV",
                "name": "Ventilation",
                "isSelectable": True,
                "activeProgram": "week1",
                "operationMode": "ventilation",
                "targetValue": 50.0,
                "circuitStatus": "active",
            }
        ],
    }
    seen = []

    async def handler(request):
        seen.append((request.method, request.path_qs, dict(request.headers)))
        # Anything not listed (live values, programs, events, weather, ...) gets [].
        return web.json_response(responses.get((request.method, request.path), []))

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    monkeypatch.setattr(api, "BASE_URL", str(server.make_url("/core")))
    monkeypatch.setattr(api, "IDP_URL", str(server.make_url("/oauth2/token")))
    yield seen
    await server.close()


async def test_setup_against_fake_cloud(hass, cloud):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw"},
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert any(entity_id.startswith("fan.") for entity_id in hass.states.async_entity_ids())
    assert ("POST", "/oauth2/token") in [(method, path) for method, path, _ in cloud]
