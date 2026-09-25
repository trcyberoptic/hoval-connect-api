"""Exercise the standalone reader without network access or a requests dependency."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def example(monkeypatch):
    requests = SimpleNamespace(get=Mock(), post=Mock())
    monkeypatch.setitem(sys.modules, "requests", requests)
    spec = importlib.util.spec_from_file_location(
        "hoval_example_under_test", Path(__file__).parents[1] / "examples/hoval_client.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, requests


def response(data):
    result = Mock()
    result.json.return_value = data
    return result


def client_without_auth(module):
    client = module.HovalClient("example@example.com", "password")
    client._headers = Mock(return_value={})
    return client


def test_reads_all_plant_pages(example):
    module, requests = example
    client = client_without_auth(module)
    requests.get.side_effect = [
        response({"content": [{"plantExternalId": "one"}], "last": False}),
        response({"content": [{"plantExternalId": "two"}], "last": True}),
    ]
    assert client.get_plants() == [{"plantExternalId": "one"}, {"plantExternalId": "two"}]
    assert [call.kwargs["params"]["page"] for call in requests.get.call_args_list] == [0, 1]


@pytest.mark.parametrize(
    "bad_page",
    [None, {}, {"content": {}}, {"content": ["invalid"]}, [], {"content": [], "last": False}],
)
def test_bad_page_never_returns_partial_plants(example, bad_page):
    module, requests = example
    client = client_without_auth(module)
    requests.get.side_effect = [
        response({"content": [{"plantExternalId": "one"}], "last": False}),
        response(bad_page),
    ]
    with pytest.raises(ValueError):
        client.get_plants()


def test_pagination_cannot_loop_forever(example):
    module, requests = example
    client = client_without_auth(module)
    client.MAX_PLANT_PAGES = 2
    requests.get.return_value = response({"content": [{"plantExternalId": "one"}], "last": False})
    with pytest.raises(ValueError, match="pagination limit"):
        client.get_plants()
    assert requests.get.call_count == 2


def test_circuit_discovery_uses_v3_and_primary_selectable_field(example):
    module, requests = example
    client = client_without_auth(module)
    circuits = [{"path": "520.50.0", "isSelectable": True}]
    requests.get.return_value = response({"content": circuits})
    assert client.get_circuits("plant") == circuits
    assert requests.get.call_args.args[0].endswith("/v3/plants/plant/circuits")
    assert client.is_circuit_selectable(circuits[0])
    assert not client.is_circuit_selectable({"isSelectable": False, "selectable": True})
    assert not client.is_circuit_selectable({"isSelectable": "false"})
    assert not client.is_circuit_selectable({"isSelectable": None, "selectable": True})
    assert client.is_circuit_selectable({"selectable": True})


def test_all_requests_have_timeouts_and_identify_client(example):
    module, requests = example
    client = module.HovalClient("example@example.com", "password")
    requests.post.return_value = response({"id_token": "test-id", "expires_in": 1800})
    requests.get.side_effect = [
        response([]),
        response({"token": "test-pat"}),
        response([]),
        response([]),
        response([]),
        response([]),
        response(True),
    ]
    client.get_plants()
    client.get_circuits("plant")
    client.get_live_values("plant", "520.50.0", "HV")
    client.get_weather("plant")
    client.get_plant_events("plant")
    client.is_online("plant")
    for call in requests.get.call_args_list + requests.post.call_args_list:
        assert call.kwargs["timeout"] == (10, 30)
        assert call.kwargs["headers"]["User-Agent"] == module.USER_AGENT
    assert requests.post.call_count == 1  # Token cache is still used.


def test_cli_uses_environment_and_does_not_probe_partner_endpoint(example, monkeypatch):
    module, _ = example
    client = Mock()
    client.get_plants.return_value = [{"plantExternalId": "plant", "isOnline": True}]
    client.get_circuits.return_value = []
    constructor = Mock(return_value=client)
    monkeypatch.setattr(module, "HovalClient", constructor)
    monkeypatch.setattr(sys, "argv", ["hoval_client.py"])
    monkeypatch.setenv("HOVAL_EMAIL", "example@example.com")
    monkeypatch.setenv("HOVAL_PASSWORD", "test-secret")
    assert module.main() == 0
    constructor.assert_called_once_with("example@example.com", "test-secret")
    client.is_online.assert_not_called()


def test_cli_rejects_positional_credentials_without_echoing(example, monkeypatch, capsys):
    module, requests = example
    monkeypatch.setattr(sys, "argv", ["hoval_client.py", "private@example.com", "test-secret"])
    assert module.main() == 2
    output = capsys.readouterr().out
    assert "test-secret" not in output
    assert "private@example.com" not in output
    requests.post.assert_not_called()
