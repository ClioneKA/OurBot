import discord
import io
import logging
from typing import Optional
from discord import app_commands
from discord.app_commands import Choice
from core.classes import Cog_Extension
from core.text_fit_draw import draw_text_auto


BASEIMAGE_MAPPING = {
    "普通": "BaseImages\\base.png",
    "開心": "BaseImages\\开心.png",
    "生氣": "BaseImages\\生气.png",
    "無語": "BaseImages\\无语.png",
    "臉紅": "BaseImages\\脸红.png",
    "病嬌": "BaseImages\\病娇.png",
    "閉眼": "BaseImages\\闭眼.png",
    "難受": "BaseImages\\难受.png",
    "害怕": "BaseImages\\害怕.png",
    "激動": "BaseImages\\激动.png",
    "驚訝": "BaseImages\\惊讶.png",
    "哭泣": "BaseImages\\哭泣.png",
}
BASE_OVERLAY_FILE = "BaseImages\\base_overlay.png"
GEN_LOCK = False


class Anan(Cog_Extension):

    def process_text_and_image(self, text: str, emotion: str) -> Optional[bytes]:
        if text == "":
            return None

        logging.info("从文本生成图片: " + text)
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
            logging.error("生成图片失败: %s", e)
            return None

    def generate_image(self, user_input, emotion):

        if user_input == "":
            logging.info("未检测到文本或图片输入，取消生成")
            return

        logging.info("开始尝试生成图片...")

        png_bytes = self.process_text_and_image(user_input, emotion)

        if png_bytes is None:
            logging.error("生成图片失败！未生成 PNG 字节。")
            return

        logging.info("成功地生成图片！")
        return png_bytes

    @app_commands.command(name="安安傳話筒", description="請安安幫你說不想直接說的話")
    @app_commands.describe(text="輸入要說的話", emotion="安安的表情")
    @app_commands.choices(
        emotion=[
            Choice(name="普通", value="普通"),
            Choice(name="開心", value="開心"),
            Choice(name="生氣", value="生氣"),
            Choice(name="無語", value="無語"),
            Choice(name="臉紅", value="臉紅"),
            Choice(name="病嬌", value="病嬌"),
            Choice(name="閉眼", value="閉眼"),
            Choice(name="難受", value="難受"),
            Choice(name="害怕", value="害怕"),
            Choice(name="激動", value="激動"),
            Choice(name="驚訝", value="驚訝"),
            Choice(name="哭泣", value="哭泣"),
        ],
    )
    async def genImage(self, interaction: discord.Interaction, text: str, emotion: str):
        image_bytes = self.generate_image(text, emotion)
        file = discord.File(fp=io.BytesIO(image_bytes), filename="anan.jpg")
        await interaction.response.send_message(file=file)


async def setup(bot):
    await bot.add_cog(Anan(bot))
