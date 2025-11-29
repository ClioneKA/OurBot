import os
import requests
from typing import Optional

API_KEY = os.getenv("MINIMAX_API_KEY")
VOICE_ID = "moss_audio_8434cf0e-cc87-11f0-9bff-daa50e7d99bd"
URL = "https://api.minimax.io/v1/t2a_v2"


def generate_sound(text: str) -> Optional[bytes]:
    token = f"Bearer {API_KEY}"
    headers = {"Authorization": token, "Content-Type": "application/json"}
    payload = {
        "model": "speech-2.6-turbo",
        "text": text,
        "voice_setting": {"voice_id": VOICE_ID},
    }
    resp = requests.post(URL, json=payload, headers=headers, timeout=30)
    res = resp.json()
    return bytes.fromhex(res["data"]["audio"])
