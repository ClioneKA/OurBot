import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import Dict, Optional

import requests

from core.settings import get_settings


logger = logging.getLogger(__name__)

URL = "https://api.minimax.io/v1/t2a_v2"
_cache_locks: Dict[str, asyncio.Lock] = {}


def is_tts_configured() -> bool:
    return bool(os.getenv("MINIMAX_API_KEY", "").strip())


def _generate_sound_sync(text: str, emotion: Optional[str]) -> Optional[bytes]:
    api_key = os.getenv("MINIMAX_API_KEY", "").strip()
    if not api_key:
        logger.warning("未設定 MINIMAX_API_KEY，無法產生語音")
        return None

    voice_setting = {
        "voice_id": get_settings().tts.voice_id,
    }
    if emotion:
        voice_setting["emotion"] = emotion

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": get_settings().tts.model,
        "text": text,
        "voice_setting": voice_setting,
    }

    try:
        response = requests.post(
            URL, headers=headers, json=payload, timeout=(10, 45)
        )
        response.raise_for_status()
        result = response.json()
        audio_hex = result.get("data", {}).get("audio")
        if not isinstance(audio_hex, str) or not audio_hex:
            logger.error("MiniMax TTS 回傳缺少音訊資料：%s", result)
            return None
        return bytes.fromhex(audio_hex)
    except (requests.RequestException, ValueError, TypeError):
        logger.exception("MiniMax TTS 請求失敗")
        return None


async def generate_sound(
    text: str, emotion: Optional[str] = None
) -> Optional[bytes]:
    """在線程池呼叫 MiniMax，避免阻塞 Discord 事件迴圈。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _generate_sound_sync, text, emotion)


async def get_cached_sound(
    text: str,
    emotion: Optional[str] = None,
    cache_dir: str = "gen_sounds",
) -> Optional[bytes]:
    voice_id = get_settings().tts.voice_id
    model = get_settings().tts.model
    cache_key = hashlib.sha256(
        f"{model}\0{voice_id}\0{emotion or ''}\0{text}".encode("utf-8")
    ).hexdigest()
    directory = Path(cache_dir)
    path = directory / f"{cache_key}.mp3"
    lock = _cache_locks.setdefault(cache_key, asyncio.Lock())

    async with lock:
        if path.is_file():
            try:
                return path.read_bytes()
            except OSError:
                logger.exception("讀取 TTS 快取失敗：%s", path)

        audio = await generate_sound(text, emotion)
        if audio is None:
            return None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path.write_bytes(audio)
        except OSError:
            logger.exception("寫入 TTS 快取失敗：%s", path)
        return audio
