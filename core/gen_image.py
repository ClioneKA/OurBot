import os
import requests
import hashlib
import io
import logging
from typing import Optional
from discord.app_commands import Choice
from core.text_fit_draw import draw_text_auto

BASEIMAGE_MAPPING = {
    "普通": "BaseImages/base.png",
    "開心": "BaseImages/happy.png",
    "生氣": "BaseImages/angry.png",
    "無語": "BaseImages/speechless.png",
    "臉紅": "BaseImages/flush.png",
    "病嬌": "BaseImages/yandere.png",
    "閉眼": "BaseImages/eyeclosed.png",
    "難受": "BaseImages/uncomfortable.png",
    "害怕": "BaseImages/scared.png",
    "激動": "BaseImages/excited.png",
    "驚訝": "BaseImages/shocked.png",
    "哭泣": "BaseImages/cry.png",
}
BASE_OVERLAY_FILE = "BaseImages/base_overlay.png"


def generate_image(text: str, emotion: str) -> Optional[bytes]:
    if text == "" or str == "":
        return None

    try:
        return draw_text_auto(
            image_source=BASEIMAGE_MAPPING[emotion],
            image_overlay=BASE_OVERLAY_FILE,
            top_left=(119, 450),
            bottom_right=(398, 625),
            text=text,
            color=(0, 0, 0),
            max_font_height=64,
            font_path="font.ttf",
            wrap_algorithm="original",  # 添加这一行以使用配置的算法
        )
    except Exception as e:
        logging.error("Gen failed: %s", e)
        return None
