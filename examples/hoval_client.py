"""
Hoval Connect API Client - Python Example

Usage:
    from hoval_client import HovalClient
    client = HovalClient("email@example.com", "password")
    values = client.get_live_values("YOUR_PLANT_ID", "520.50.0", "HV")
"""

import time

import requests


class HovalClient:
    BASE_URL = "https://azure-iot-prod.hoval.com/core"
    IDP_URL = "https://akwc5scsc.accounts.ondemand.com/oauth2/token"
    CLIENT_ID = "991b54b2-7e67-47ef-81fe-572e21c59899"

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self._id_token = None
        self._id_token_exp = 0
        self._pat_cache: dict[str, tuple[str, float]] = {}

    def _get_id_token(self) -> str:
        if self._id_token and time.time() < self._id_token_exp - 60:
            return self._id_token

        resp = requests.post(self.IDP_URL, data={
            "grant_type": "password",
            "client_id": self.CLIENT_ID,
            "username": self.email,
            "password": self.password,
            "scope": "openid",
        })
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
            headers={"Authorization": f"Bearer {self._get_id_token()}"},
        )
        resp.raise_for_status()
        token = resp.json()["token"]
        self._pat_cache[plant_id] = (token, time.time() + 900)
        return token

    def _headers(self, plant_id: str | None = None) -> dict:
        h = {"Authorization": f"Bearer {self._get_id_token()}"}
        if plant_id:
            h["X-Plant-Access-Token"] = self._get_plant_access_token(plant_id)
        return h

    def get_plants(self) -> list:
        resp = requests.get(
            f"{self.BASE_URL}/api/my-plants?size=12&page=0",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def get_circuits(self, plant_id: str) -> list:
        resp = requests.get(
            f"{self.BASE_URL}/v1/plants/{plant_id}/circuits",
            headers=self._headers(plant_id),
        )
        resp.raise_for_status()
        return resp.json()

    def get_live_values(self, plant_id: str, circuit_path: str, circuit_type: str) -> list:
        resp = requests.get(
            f"{self.BASE_URL}/v3/api/statistics/live-values/{plant_id}",
            params={"circuitPath": circuit_path, "circuitType": circuit_type},
            headers=self._headers(plant_id),
        )
        resp.raise_for_status()
        return resp.json()

    def get_weather(self, plant_id: str) -> list:
        resp = requests.get(
            f"{self.BASE_URL}/v2/api/weather/forecast/{plant_id}",
            headers=self._headers(plant_id),
        )
        resp.raise_for_status()
        return resp.json()

    def get_plant_events(self, plant_id: str) -> list:
        resp = requests.get(
            f"{self.BASE_URL}/v1/plant-events/{plant_id}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def is_online(self, plant_id: str) -> bool:
        resp = requests.get(
            f"{self.BASE_URL}/business/plants/{plant_id}/is-online",
            headers=self._headers(plant_id),
        )
        resp.raise_for_status()
        return resp.json()


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print(f"Usage: python {sys.argv[0]} <email> <password>")
        sys.exit(1)

    client = HovalClient(sys.argv[1], sys.argv[2])

    plants = client.get_plants()
    print(f"Plants: {plants}")

    for plant in plants:
        pid = plant["plantExternalId"]
        print(f"\n--- Plant {pid} ({plant['description']}) ---")
        print(f"Online: {client.is_online(pid)}")

        circuits = client.get_circuits(pid)
        for circuit in circuits:
            if circuit.get("selectable"):
                path = circuit["path"]
                ctype = circuit["type"]
                print(f"\nCircuit: {circuit.get('name', ctype)} ({path})")
                values = client.get_live_values(pid, path, ctype)
                for v in values:
                    print(f"  {v['key']}: {v['value']}")

        print(f"\nWeather: {client.get_weather(pid)}")
        print(f"Events: {client.get_plant_events(pid)}")
