"""
Hoval Connect API Client - Python Example

Usage:
    from hoval_client import HovalClient
    client = HovalClient("email@example.com", "password")
    values = client.get_live_values("YOUR_PLANT_ID", "520.50.0", "HV")
"""

import time

import requests

# Identify this client. Hoval's Azure Application Gateway refuses a short list of
# User-Agents outright, and `python-requests/<ver>` — what this module would send
# by default — is on it, exactly like Home Assistant's own string. You get the
# gateway's HTML 403 page before the request ever reaches Hoval, which looks
# nothing like an auth error. Set a User-Agent naming your own software.
USER_AGENT = "hoval-connect-api-example/1.0 (+https://github.com/trcyberoptic/hoval-connect-api)"


class HovalClient:
    BASE_URL = "https://azure-iot-prod.hoval.com/core"
    IDP_URL = "https://akwc5scsc.accounts.ondemand.com/oauth2/token"
    CLIENT_ID = "991b54b2-7e67-47ef-81fe-572e21c59899"
    TIMEOUT = (10, 30)  # Connect/read timeout, in seconds.
    MAX_PLANT_PAGES = 50

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self._id_token = None
        self._id_token_exp = 0
        self._pat_cache: dict[str, tuple[str, float]] = {}

    def _get_id_token(self) -> str:
        if self._id_token and time.time() < self._id_token_exp - 60:
            return self._id_token

        resp = requests.post(
            self.IDP_URL,
            data={
                "grant_type": "password",
                "client_id": self.CLIENT_ID,
                "username": self.email,
                "password": self.password,
                "scope": "openid",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        self._id_token = data["id_token"]
        self._id_token_exp = time.time() + data.get("expires_in", 1800)
        return self._id_token

    def _get_plant_access_token(self, plant_id: str) -> str:
        cached = self._pat_cache.get(plant_id)
        if cached and time.time() < cached[1] - 60:
            return cached[0]

        resp = requests.get(
            f"{self.BASE_URL}/v1/plants/{plant_id}/settings",
            headers={
                "Authorization": f"Bearer {self._get_id_token()}",
                "User-Agent": USER_AGENT,
            },
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        token = resp.json()["token"]
        self._pat_cache[plant_id] = (token, time.time() + 900)
        return token

    def _headers(self, plant_id: str | None = None) -> dict:
        h = {
            "Authorization": f"Bearer {self._get_id_token()}",
            "User-Agent": USER_AGENT,
        }
        if plant_id:
            h["X-Plant-Access-Token"] = self._get_plant_access_token(plant_id)
        return h

    def get_plants(self) -> list:
        """Read all account pages; never silently return a partial plant list."""
        plants = []
        for page in range(self.MAX_PLANT_PAGES):
            resp = requests.get(
                f"{self.BASE_URL}/api/my-plants",
                params={"size": 12, "page": page},
                headers=self._headers(),
                timeout=self.TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            content = self._list_payload(data)
            plants.extend(content)
            if isinstance(data, list):
                if page:
                    raise ValueError("Plant response shape changed during pagination")
                return plants
            last = data.get("last", True)
            if not isinstance(last, bool):
                raise ValueError("Invalid plant pagination flag")
            if last:
                return plants
            if not content:
                raise ValueError("Empty non-final plant page")
        raise ValueError("Plant pagination limit reached; refusing a partial result")

    @staticmethod
    def _list_payload(data) -> list:
        content = data.get("content") if isinstance(data, dict) else data
        if not isinstance(content, list) or any(not isinstance(row, dict) for row in content):
            raise ValueError("Expected a list of objects or a page with list content")
        return content

    @staticmethod
    def is_circuit_selectable(circuit: dict) -> bool:
        selectable = circuit.get("isSelectable", circuit.get("selectable", False))
        return selectable is True

    def get_circuits(self, plant_id: str) -> list:
        resp = requests.get(
            f"{self.BASE_URL}/v3/plants/{plant_id}/circuits",
            headers=self._headers(plant_id),
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        return self._list_payload(resp.json())

    def get_live_values(self, plant_id: str, circuit_path: str, circuit_type: str) -> list:
        resp = requests.get(
            f"{self.BASE_URL}/v3/api/statistics/live-values/{plant_id}",
            params={"circuitPath": circuit_path, "circuitType": circuit_type},
            headers=self._headers(plant_id),
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def get_weather(self, plant_id: str) -> list:
        resp = requests.get(
            f"{self.BASE_URL}/v2/api/weather/forecast/{plant_id}",
            headers=self._headers(plant_id),
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def get_plant_events(self, plant_id: str) -> list:
        resp = requests.get(
            f"{self.BASE_URL}/v1/plant-events/{plant_id}",
            headers=self._headers(),
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def is_online(self, plant_id: str) -> bool:
        """Partner-only endpoint; regular accounts should read the plant's isOnline field."""
        resp = requests.get(
            f"{self.BASE_URL}/business/plants/{plant_id}/is-online",
            headers=self._headers(plant_id),
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()


def main() -> int:
    """Read credentials interactively or from the environment, never from argv."""
    import getpass
    import os
    import sys

    if len(sys.argv) != 1:
        print("Run without arguments. Use HOVAL_EMAIL/HOVAL_PASSWORD or the prompts.")
        return 2

    email = os.environ.get("HOVAL_EMAIL") or input("Hoval account email: ")
    password = os.environ.get("HOVAL_PASSWORD") or getpass.getpass("Hoval account password: ")
    client = HovalClient(email, password)

    plants = client.get_plants()
    print(f"Plants: {plants}")

    for plant in plants:
        pid = plant["plantExternalId"]
        print(f"\n--- Plant {pid} ({plant.get('description', '')}) ---")
        print(f"Online: {plant.get('isOnline')}")

        circuits = client.get_circuits(pid)
        for circuit in circuits:
            if client.is_circuit_selectable(circuit):
                path = circuit["path"]
                ctype = circuit["type"]
                print(f"\nCircuit: {circuit.get('name', ctype)} ({path})")
                values = client.get_live_values(pid, path, ctype)
                for v in values:
                    print(f"  {v['key']}: {v['value']}")

        print(f"\nWeather: {client.get_weather(pid)}")
        print(f"Events: {client.get_plant_events(pid)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
