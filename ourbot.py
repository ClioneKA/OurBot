import asyncio
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from core.settings import get_settings

ROOT = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


def sync_guild_ids(value):
    """Optional explicit sync targets; unrelated to AI reply restrictions."""
    ids = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if not item.isascii() or not item.isdecimal() or not 0 < int(item) < 2**64:
            raise ValueError("DISCORD_SYNC_GUILD_IDS 必須是逗號分隔的有效伺服器 ID")
        if int(item) not in ids:
            ids.append(int(item))
    return ids


class OurBot(commands.Bot):
    def __init__(self, guild_ids=()):
        super().__init__(
            command_prefix=[], intents=discord.Intents.all(),
            allowed_installs=discord.app_commands.AppInstallationType(guild=True, user=False),
        )
        self.sync_guild_ids = tuple(guild_ids)

    async def setup_hook(self):
        # Login has completed; load every extension before publishing any commands.
        get_settings()
        for path in sorted((ROOT / "cmds").glob("*.py")):
            if path.name.startswith("_"):
                continue
            name = f"cmds.{path.stem}"
            try:
                await self.load_extension(name)
            except Exception:
                logger.exception("功能模組載入失敗：%s；停止啟動，不同步不完整的指令", name)
                raise
            logger.info("已載入功能模組：%s", name)

        local_commands = self.tree.get_commands()
        if not local_commands:
            raise RuntimeError("沒有載入任何應用程式指令，停止同步以避免清空遠端指令")
        logger.info("本機指令 %d 個：%s", len(local_commands),
                    ", ".join(command.name for command in local_commands))

        if not self.sync_guild_ids:
            await self.clear_old_guild_commands()

        # Global by default. Explicit guild targets allow immediate server testing.
        targets = [discord.Object(id=gid) for gid in self.sync_guild_ids] or [None]
        for guild in targets:
            scope = f"伺服器 {guild.id}" if guild else "全域"
            if guild is not None:
                self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
            except discord.Forbidden:
                logger.exception("%s 指令同步被拒絕；確認 bot 已加入目標伺服器，"
                                 "並以 bot、applications.commands scope 授權", scope)
                raise
            except discord.HTTPException as exc:
                logger.exception("%s 指令同步失敗（HTTP %s，Discord code %s）",
                                 scope, exc.status, exc.code)
                raise
            logger.info("%s 指令同步成功，Discord 回傳 %d 個：%s", scope, len(synced),
                        ", ".join(f"{command.name} ({command.id})" for command in synced))
            if guild is None:
                await self.verify_global_commands(synced)

    async def verify_global_commands(self, synced):
        try:
            # Read raw JSON: some discord.py versions shift zero-based context
            # flags while parsing AppCommand; to_dict also omits permissions.
            remote = await self.http.get_global_commands(self.application_id)
        except discord.HTTPException:
            logger.exception("全域同步已回應成功，但遠端查核失敗")
            return
        logger.info("全域遠端查核：Application ID %s，GET 回傳 %d 個指令",
                    self.application_id, len(remote))
        if {int(c.id) for c in synced} != {int(c['id']) for c in remote}:
            logger.warning("遠端清單與剛同步的指令 ID 不一致，請檢查是否有其他程序覆寫指令")
        for payload in remote:
            installs = payload.get('integration_types')
            contexts = payload.get('contexts')
            logger.info("遠端原始指令 %s (%s)：integration_types=%s，contexts=%s，default_member_permissions=%s",
                        payload['name'], payload['id'], installs, contexts,
                        payload.get('default_member_permissions'))
            if (installs is not None and 0 not in installs) or (contexts is not None and 0 not in contexts):
                logger.warning("指令 %s 的遠端設定不允許伺服器安裝或伺服器頻道使用", payload['name'])

    async def clear_old_guild_commands(self):
        # setup_hook runs before the Gateway fills self.guilds; use the REST API.
        try:
            guilds = [guild async for guild in self.fetch_guilds(limit=None)]
        except Exception:
            logger.exception("無法取得伺服器清單；停止清理與全域同步")
            raise
        for guild in guilds:
            try:
                old_commands = await self.tree.fetch_commands(guild=guild)
                if not old_commands:
                    continue
                logger.info("清除伺服器 %s 的 %d 個舊指令：%s", guild.id,
                            len(old_commands), ", ".join(c.name for c in old_commands))
                self.tree.clear_commands(guild=guild)
                remaining = await self.tree.sync(guild=guild)
                if remaining:
                    raise RuntimeError(f"伺服器 {guild.id} 清理後仍有指令，停止全域同步")
                logger.info("伺服器 %s 舊指令已清除", guild.id)
            except Exception:
                logger.exception("伺服器 %s 舊指令清理失敗；停止全域同步", guild.id)
                raise

    async def on_ready(self):
        logger.info("目前登入身份：%s；Application ID：%s；已加入 %d 個伺服器",
                    self.user, self.application_id, len(self.guilds))


async def run():
    # Existing asset and database paths are relative to the project directory.
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env")
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise ValueError("尚未設定 DISCORD_TOKEN，請檢查專案根目錄的 .env")
    guild_ids = sync_guild_ids(os.getenv("DISCORD_SYNC_GUILD_IDS", ""))
    get_settings()
    async with OurBot(guild_ids) as bot:
        await bot.start(token)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(ROOT / "log.txt", encoding="utf-8"),
                  logging.StreamHandler()],
    )
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("機器人已停止")
    except Exception:
        logger.exception("機器人啟動或執行失敗")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
