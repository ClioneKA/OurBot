import logging
from pathlib import Path
import time
from typing import Optional
from weakref import WeakSet

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.rpg import MAX_LEVEL, RPGStore, VoiceTracker, eligible_voice_members, level_floor, level_for
from core.settings import get_settings
from core.rpg_equipment_view import EquipmentView
from core.rpg_skill_view import SkillView
from core.rpg_shop_view import ShopView
from core.rpg_character import Characters, CharacterError, ITEMS, JOBS, STAT_NAMES, stage_level, item_text
from core.rpg_battle import Tactics, SKILLS, CONDITIONS, TARGETS
from core.rpg_raids import RaidService


class RPG(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.settings = get_settings().rpg
        self.store = RPGStore(Path(__file__).resolve().parent.parent / 'data/rpg.db')
        self.characters = Characters(self.store, self.settings)
        self.tracker = VoiceTracker()
        self.equipment_views = WeakSet()
        self.skill_views = WeakSet()
        self.shop_views = WeakSet()
        self.tactics = Tactics(self.store)
        self.ai_model = get_settings().ai.model
        self.raids = RaidService(self)

    async def cog_load(self):
        self.voice_tick.start()
        self.raids.start()

    async def cog_unload(self):
        self.voice_tick.cancel()
        await self.raids.close()
        for view in (*self.equipment_views, *self.skill_views, *self.shop_views):
            view.closed = True
            view.stop()
        self.store.close()

    @commands.Cog.listener()
    async def on_message(self, message):
        if (not self.settings.enabled or message.guild is None or message.author.bot
                or message.webhook_id is not None or message.is_system()
                or len(''.join(message.content.split())) < self.settings.text_min_chars):
            return
        self.store.award_text(message.guild.id, message.author.id, time.time(),
                              self.settings.text_xp, self.settings.text_cooldown_seconds)

    def update_voice(self, guild):
        if not self.settings.enabled or guild.unavailable:
            self.tracker.clear(guild.id)
            return
        eligible = eligible_voice_members(guild, self.settings.voice_min_members)
        awards = self.tracker.update(guild.id, eligible, time.monotonic(),
                                     self.settings.voice_xp_per_minute)
        if awards:
            self.store.award_voice(awards)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if self.bot.is_ready():
            self.update_voice(member.guild)

    @commands.Cog.listener()
    async def on_disconnect(self):
        self.tracker.clear()

    @commands.Cog.listener()
    async def on_guild_unavailable(self, guild):
        self.tracker.clear(guild.id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        self.tracker.clear(guild.id)

    @tasks.loop(seconds=15)
    async def voice_tick(self):
        if not self.bot.is_ready():
            self.tracker.clear()
            return
        for guild in self.bot.guilds:
            try:
                self.update_voice(guild)
            except Exception:
                logging.exception('RPG voice XP update failed for guild %s', guild.id)
                self.tracker.clear(guild.id)

    @voice_tick.before_loop
    async def before_voice_tick(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name='角色', description='查看自己或其他成員的冒險等級與經驗值')
    @app_commands.guild_only()
    @app_commands.describe(member='想查看的伺服器成員，留空查看自己')
    async def profile(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        member = member or interaction.user
        embed = self.character_embed(interaction.guild_id, member)
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    def character_embed(self, guild_id, member):
        xp = self.store.xp(guild_id, member.id)
        level = level_for(xp)
        state = self.characters.snapshot(guild_id, member.id)
        embed = discord.Embed(title=f'Lv.{level}・{state["title"]}', color=0x8B5CF6)
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        if level == MAX_LEVEL:
            embed.add_field(name='升級進度', value=f'{"▰" * 10}\n已達最高等級 Lv.{MAX_LEVEL}', inline=False)
        else:
            progress = xp - level_floor(level)
            required = level_floor(level + 1) - level_floor(level)
            filled = min(10, progress * 10 // required)
            embed.add_field(name='升級進度', value=f'{"▰" * filled}{"▱" * (10 - filled)}\n{progress:,} / {required:,} XP', inline=False)
            embed.add_field(name='距離下一級', value=f'{required - progress:,} XP')
        embed.add_field(name='累積經驗', value=f'{xp:,} XP')
        embed.add_field(name='金幣', value=f'{self.store.gold(guild_id, member.id):,} 金幣')
        embed.add_field(name='基礎能力＋飾品加成', value='\n'.join(
            f'{name}：{total}（{base} + {bonus}）' for name, total, base, bonus in
            zip(STAT_NAMES, state['total'], state['base'], state['bonus'])), inline=False)
        embed.add_field(name='戰鬥能力', value='｜'.join(
            f'{name} {value}{"%" if name.endswith("率") else ""}'
            for name, value in state['combat'].items()), inline=False)
        embed.add_field(name='武器／套裝直接加成', value='、'.join(
            f'{name} +{value}' for name, value in state['combat_bonus'].items() if value) or '無', inline=False)
        embed.add_field(name='武器穩定度', value=(f'{state["stability"][0]}–{state["stability"][1]}% 傷害'
                        if '武器' in state['equipped'] else '未裝備武器，無法造成傷害'))
        embed.add_field(name=f'裝備欄・飾品 {state["capacity"]} 格', value='\n'.join(
            f'{slot}：{ITEMS[state["equipped"][slot]].name if slot in state["equipped"] else "空"}'
            for slot in state['slots']), inline=False)
        if state['job'] == '民兵':
            embed.description = 'Lv.10 起可使用 `/轉職` 選擇職業。'
        elif state['stage'] < 3:
            embed.description = f'下次晉升：Lv.{stage_level(state["stage"] + 1, self.settings)}；進階裝備可從 `/商店` 購買取得。'
        embed.set_footer(text='透過文字聊天與語音參與累積經驗' if self.settings.enabled else '目前暫停取得經驗值')
        return embed

    @app_commands.command(name='排行榜', description='查看本伺服器經驗值前十名')
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        rows = self.store.leaders(interaction.guild_id)
        lines = [f'{index}. <@{user_id}> — Lv.{level_for(xp)} · {xp:,} XP'
                 for index, (user_id, xp) in enumerate(rows, 1)]
        embed = discord.Embed(title='冒險者排行榜', description='\n'.join(lines) or '還沒有冒險者，開始聊天來獲得經驗吧！', color=0xF59E0B)
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='冒險說明', description='查看文字與語音的經驗值規則')
    @app_commands.guild_only()
    async def rules(self, interaction: discord.Interaction):
        s = self.settings
        await interaction.response.send_message(
            f'**冒險者指南**\n'
            f'文字：至少 {s.text_min_chars} 個非空白字元，每 {s.text_cooldown_seconds} 秒可獲得 {s.text_xp} XP（跨頻道共用冷卻）。\n'
            f'語音：同一般語音頻道至少 {s.voice_min_members} 位符合條件的真人，每完整分鐘 {s.voice_xp_per_minute} XP。\n'
            'AFK 頻道、機器人、靜音、拒聽及舞台頻道不計入；資格中斷時，不足一分鐘的時間歸零。\n'
            '語音依參與時間計算，不偵測實際說話；機器人離線期間不補發。\n'
            f'採用 RuneScape 標準經驗曲線，上限 Lv.{MAX_LEVEL}；滿級仍可累積 XP 參與排名。\n'
            'Lv.2：83 XP；Lv.99：13,034,431 XP；Lv.120：104,273,167 XP。\n'
            '使用 `/角色` 查看進度、`/排行榜` 查看前十名。\n'
            f'Lv.1–9 為民兵；Lv.10 可 `/轉職` 選擇早期職業，Lv.{s.regular_level}／{s.veteran_level}／{s.elite_level} 自動晉升無前綴／老練／精銳。\n'
            '四職業：裝甲步兵（均衡近戰）、騎士（高生命防禦）、弓兵（靈巧輸出）、僧侶（信仰治療）。\n'
            '裝備：武器、套裝各一件；飾品格依階段為 1／2／3／4 格。同件飾品不能重複穿戴。\n'
            '`/背包` 查看物品、`/裝備` 開啟面板穿戴或卸下；`/領取補給` 只提供早期裝備；進階裝備由 `/商店` 購買取得。\n'
            '目前換職免費，保留 XP 與背包，重新計算能力並換上職業裝備；飾品需重新穿戴。\n'
            '安安會在指定頻道發布魔物，點擊報名，5 分鐘後自動討伐。使用 `/技能` 開啟面板查看與調整自動施放。\n'
            '成功討伐可獲得金幣，金額以活動公告為準；使用 `/角色` 或 `/裝備` 查看餘額。\n'
            + ('目前正常累積經驗值。' if s.enabled else '目前暫停取得經驗值。'), ephemeral=True)

    @app_commands.command(name='轉職', description='Lv.10 起選擇職業，免費換職並穿上職業裝備')
    @app_commands.guild_only()
    @app_commands.choices(job=[app_commands.Choice(name=job, value=job) for job in JOBS])
    @app_commands.describe(job='選擇職業；換職會卸下目前裝備，XP 與背包保留')
    async def change_job(self, interaction: discord.Interaction, job: str):
        try:
            state = self.characters.change_job(interaction.guild_id, interaction.user.id, job)
            message = (f'你現在是 **{state["title"]}**！已穿上早期職業武器與套裝。\n'
                       f'可用飾品格：{state["capacity"]}。使用 `/背包` 查看補給飾品，再用 `/裝備` 開啟能力與裝備面板。')
        except CharacterError as exc:
            message = str(exc)
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name='領取補給', description='領取早期職業裝備與新手飾品，每件限領一次')
    @app_commands.guild_only()
    async def claim_supplies(self, interaction: discord.Interaction):
        try:
            granted = self.characters.claim(interaction.guild_id, interaction.user.id)
            message = ('已放入背包：' + '、'.join(ITEMS[key].name for key in granted)
                       + '。使用 `/裝備` 開啟能力與裝備面板。') if granted else '早期補給都已領取；進階裝備請到 /商店 購買取得。'
        except CharacterError as exc:
            message = str(exc)
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name='背包', description='查看自己的裝備與加成，每頁十件')
    @app_commands.guild_only()
    @app_commands.describe(page='頁碼，從 1 開始')
    async def backpack(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 100] = 1):
        owned = self.characters.inventory(interaction.guild_id, interaction.user.id)
        pages = max(1, (len(owned) + 9) // 10)
        if page > pages:
            await interaction.response.send_message(f'背包只有 {pages} 頁。', ephemeral=True)
            return
        state = self.characters.snapshot(interaction.guild_id, interaction.user.id)
        equipped = set(state['equipped'].values())
        counts = self.characters.inventory_counts(interaction.guild_id, interaction.user.id)
        lines = []
        for key in owned[(page - 1) * 10:page * 10]:
            item = ITEMS[key]
            requirement = f'{item.job} Lv.{stage_level(item.stage, self.settings)}' if item.job else '全職業通用'
            lines.append(f'**{item.name}** ×{counts[key]} {"【已裝備】" if key in equipped else ""}\n'
                         f'{item.slot}｜{requirement}｜{item_text(item)}')
        embed = discord.Embed(title=f'背包 {page}/{pages}', description='\n\n'.join(lines) or
                              '背包是空的。Lv.10 轉職後會取得職業裝備與飾品。', color=0x8B5CF6)
        embed.set_footer(text='使用 /裝備 開啟面板選擇物品；換頁使用 /背包 page')
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name='裝備', description='開啟裝備與能力值視窗，使用選單穿戴或卸下裝備')
    @app_commands.guild_only()
    async def equip_item(self, interaction: discord.Interaction):
        view = EquipmentView(self, interaction)
        self.equipment_views.add(view)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='商店', description='使用金幣購買目前職業的進階武器與套裝')
    @app_commands.guild_only()
    async def shop(self, interaction: discord.Interaction):
        view = ShopView(self, interaction)
        self.shop_views.add(view)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @app_commands.command(name='生成討伐', description='管理員立即在目前的討伐頻道生成魔物，五分鐘後開戰')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.rename(kind='類型', name='名稱', strength='強度', victory_xp='經驗',
                         victory_gold='金幣', drop_percent='掉落率')
    @app_commands.describe(kind='不填則隨機抽取怪物類型', name='這場怪物的名稱，最多20字',
        strength='血量、攻擊、防禦倍率，0.1–10，預設1', victory_xp='勝利每人最終經驗，覆蓋類型預設獎勵',
        victory_gold='勝利每人最終金幣，覆蓋類型預設獎勵', drop_percent='飾品掉落百分比，0–100；史萊姆群固定不掉落')
    @app_commands.choices(kind=[app_commands.Choice(name=k, value=k) for k in ('巨獸', '毒蛛', '史萊姆群')])
    async def spawn_raid(self, interaction: discord.Interaction, kind: str = None,
                         name: app_commands.Range[str, 1, 20] = None,
                         strength: app_commands.Range[float, 0.1, 10.0] = 1.0,
                         victory_xp: app_commands.Range[int, 0, 100000] = None,
                         victory_gold: app_commands.Range[int, 0, 1000000] = None,
                         drop_percent: app_commands.Range[int, 0, 100] = None):
        if interaction.guild is None or not interaction.permissions.administrator:
            await interaction.response.send_message('只有伺服器管理員可以生成討伐。', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            message = await self.raids.spawn(interaction.channel, kind=kind, name=name, strength=strength,
                                            victory_xp=victory_xp, victory_gold=victory_gold, drop_percent=drop_percent)
        except CharacterError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            logging.exception('Administrator raid spawn failed')
            await interaction.followup.send('生成討伐失敗，請確認機器人的頻道權限後再試。', ephemeral=True)
            return
        await interaction.followup.send(f'討伐已發布，五分鐘後開戰：{message.jump_url}', ephemeral=True)

    def skills_embed(self, guild, user):
        state = self.characters.snapshot(guild, user)
        embed = discord.Embed(title=f'{state["title"]}・自動技能', color=0x8B5CF6,
                              description='每回合由優先 1 開始檢查，施放第一個符合條件且冷卻結束的技能；否則普攻。')
        for rule in self.tactics.rules(guild, user, state['job']):
            skill = SKILLS[state['job']][rule.slot - 1]
            embed.add_field(name=f'優先 {rule.priority}｜槽 {rule.slot}：{skill.name}｜{"開" if rule.enabled else "關"}',
                            value=f'{skill.description}\n冷卻 {skill.cooldown} 回合｜{CONDITIONS[rule.condition]}｜目標：{TARGETS[rule.target]}', inline=False)
        embed.set_footer(text='在 /技能 面板調整。冷卻 2 表示完整等待兩回合。自身技能作用於自己；護衛作用全隊；範圍攻擊作用全體敵人，皆忽略目標選項。')
        return embed

    @app_commands.command(name='技能', description='開啟技能面板，查看並調整自動施放策略')
    @app_commands.guild_only()
    async def skills(self, interaction: discord.Interaction):
        view = SkillView(self, interaction)
        self.skill_views.add(view)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(RPG(bot))
