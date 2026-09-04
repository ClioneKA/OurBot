"""Typed behavior settings, loaded once at startup from config/settings.toml."""
from dataclasses import dataclass, field, fields
from functools import lru_cache
import math
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.8–3.10
    import tomli as tomllib


DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config/settings.toml"


class SettingsError(ValueError):
    """Configuration error safe to display without printing setting values."""


@dataclass(frozen=True)
class SearchSettings:
    enabled: bool = field(default=True, metadata={})
    model: str = field(default='gpt-5.6-luna', metadata={'nonempty': True})
    daily_limit: int = field(default=2, metadata={'minimum': 1, 'maximum': 100})


@dataclass(frozen=True)
class LimitSettings:
    user_cooldown_seconds: int = field(default=30, metadata={'minimum': 1, 'maximum': 3600})
    channel_cooldown_seconds: int = field(default=5, metadata={'minimum': 1, 'maximum': 3600})
    global_requests_per_minute: int = field(default=20, metadata={'minimum': 1, 'maximum': 1000})
    global_requests_per_hour: int = field(default=100, metadata={'minimum': 1, 'maximum': 10000})
    max_concurrent_requests: int = field(default=2, metadata={'minimum': 1, 'maximum': 20})
    user_daily_limit: int = field(default=20, metadata={'minimum': 1, 'maximum': 1000})
    daily_timezone_offset_hours: int = field(default=8, metadata={'minimum': -12, 'maximum': 14})


@dataclass(frozen=True)
class MemorySettings:
    summary_enabled: bool = field(default=True, metadata={})
    summary_interval: int = field(default=10, metadata={'minimum': 1, 'maximum': 1000})
    summary_batch_size: int = field(default=200, metadata={'minimum': 10, 'maximum': 1000})
    summary_max_output_tokens: int = field(default=2000, metadata={'minimum': 200, 'maximum': 8000})
    summary_model: str = field(default='', metadata={})
    affinity_daily_changes: int = field(default=3, metadata={'minimum': 1, 'maximum': 20})


@dataclass(frozen=True)
class MediaSettings:
    image_replies_enabled: bool = field(default=True, metadata={})
    voice_replies_enabled: bool = field(default=True, metadata={})
    direct_only: bool = field(default=True, metadata={})
    image_max_chars: int = field(default=180, metadata={'minimum': 20, 'maximum': 1000})
    image_cooldown_seconds: int = field(default=30, metadata={'minimum': 1, 'maximum': 3600})
    tts_max_chars: int = field(default=100, metadata={'minimum': 10, 'maximum': 1000})
    tts_cooldown_seconds: int = field(default=120, metadata={'minimum': 1, 'maximum': 86400})
    tts_user_daily_limit: int = field(default=5, metadata={'minimum': 1, 'maximum': 1000})
    tts_guild_daily_limit: int = field(default=30, metadata={'minimum': 1, 'maximum': 10000})


@dataclass(frozen=True)
class AISettings:
    model: str = field(default='gpt-5.6-luna', metadata={'nonempty': True})
    reply_chance: float = field(default=0.25, metadata={'minimum': 0.0, 'maximum': 1.0})
    direct_reply_chance: float = field(default=1.0, metadata={'minimum': 0.0, 'maximum': 1.0})
    history_size: int = field(default=20, metadata={'minimum': 2, 'maximum': 100})
    max_input_chars: int = field(default=1000, metadata={'minimum': 50, 'maximum': 4000})
    max_output_tokens: int = field(default=250, metadata={'minimum': 32, 'maximum': 2000})
    search: SearchSettings = field(default_factory=SearchSettings)
    limits: LimitSettings = field(default_factory=LimitSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    media: MediaSettings = field(default_factory=MediaSettings)


@dataclass(frozen=True)
class TTSSettings:
    voice_id: str = field(default='moss_audio_8434cf0e-cc87-11f0-9bff-daa50e7d99bd', metadata={'nonempty': True})
    model: str = field(default='speech-2.6-turbo', metadata={'nonempty': True})


@dataclass(frozen=True)
class Settings:
    ai: AISettings = field(default_factory=AISettings)
    tts: TTSSettings = field(default_factory=TTSSettings)


def _section(cls, raw, path):
    if not isinstance(raw, dict):
        raise SettingsError(f"{path} 必須是 TOML 區塊")
    definitions = {item.name: item for item in fields(cls)}
    unknown = set(raw) - set(definitions)
    if unknown:
        raise SettingsError(f"{path} 包含未知設定：{', '.join(sorted(unknown))}")
    values = {}
    for name, value in raw.items():
        item = definitions[name]
        label = f"{path}.{name}"
        expected = item.type
        if hasattr(expected, '__dataclass_fields__'):
            values[name] = _section(expected, value, label)
            continue
        if expected is float and type(value) in (int, float):
            value = float(value)
        if type(value) is not expected:
            raise SettingsError(f"{label} 必須是 {expected.__name__}")
        if expected is float and not math.isfinite(value):
            raise SettingsError(f"{label} 必須是有限數字")
        if 'minimum' in item.metadata:
            low, high = item.metadata['minimum'], item.metadata['maximum']
            if not low <= value <= high:
                raise SettingsError(f"{label} 必須介於 {low} 和 {high}")
        if item.metadata.get('nonempty') and not value.strip():
            raise SettingsError(f"{label} 不得為空白")
        values[name] = value
    return cls(**values)


def load_settings(path=DEFAULT_PATH) -> Settings:
    """Read and validate a file; omitted fields use documented code defaults."""
    try:
        with Path(path).open('rb') as stream:
            raw = tomllib.load(stream)
    except (OSError, ValueError) as exc:
        raise SettingsError(f"無法讀取設定檔 {path}，請確認檔案存在且 TOML 格式正確") from exc
    return _section(Settings, raw, 'settings')


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Shared startup snapshot. Environment variables do not override TOML."""
    return load_settings()
