import asyncio
import logging
import os
from pathlib import Path
import time
from weakref import WeakSet

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.rpg import MAX_LEVEL, RPGStore, VoiceTracker, eligible_voice_members, level_floor, level_for, scaled_chat_xp
from core.settings import get_settings
from core.rpg_menu import AdventureView
from core.rpg_character import Characters, CharacterError, ITEMS, STAT_NAMES, equipment_slot_text, item_display_name, item_text, stage_level
from core.rpg_battle import Tactics, TARGETS, FIXED_TARGETS, condition_text, rule_skill, skill_description
from core.rpg_loadouts import Loadouts
from core.rpg_raids import RaidService
from core.rpg_total_raids import TotalRaidService, TOTAL_RAID_BOSSES
from core.rpg_total_battle import TotalRaidError
from core.rpg_fishing import BIG_FISH, Fishing, SPOTS
from core.rpg_farming import Farming, LOCATIONS, PLANTS
from core.rpg_expeditions import Expeditions
from core.rpg_provisions import Provisions
from core.rpg_divination import Divinations
from core.rpg_tavern import TavernService, TavernView
from core.rpg_notification_view import FarmingNotificationView, FishingNotificationView
from core.rpg_invites import AdventurerInvitations, AdventurerInvitationView
from core.rpg_spaces import AdventureSpaceService
from core.rpg_painted_maze import MODE_NAME
from core.rpg_painted_maze_service import PaintedMazeService
from core.rpg_crystals import crystal_affix_name, crystal_effect_text


class RPG(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.settings = get_settings().rpg
        self.store = RPGStore(Path(__file__).resolve().parent.parent / 'data/rpg.db')
        self.characters = Characters(self.store, self.settings)
        self.spaces = AdventureSpaceService(self)
        self.invitations = AdventurerInvitations(self)
        self.fishing = Fishing(self.store)
        self.farming = Farming(self.store)
        self.expeditions = Expeditions(self.store, self.settings)
        self.provisions = Provisions(self.store)
        self.divinations = Divinations(self.store)
        self.tracker = VoiceTracker()
        self.menu_views = WeakSet()
        self.notification_views = WeakSet()
        self.tactics = Tactics(self.store)
        self.loadouts = Loadouts(self.store, self.characters, self.tactics)
        self.ai_model = get_settings().ai.model
        self.raids = RaidService(self)
        from core.memory import MemoryStore
        memory_path = Path(os.getenv('AI_MEMORY_DB', 'data/memory.db'))
        if not memory_path.is_absolute():
            memory_path = Path(__file__).resolve().parent.parent / memory_path
        self.commission_memory = MemoryStore(str(memory_path))
        self.tavern = TavernService(self)
        self.total_raids = TotalRaidService(self)
        self.painted_maze = PaintedMazeService(self)

    async def cog_load(self):
        self.invitations.restore_views()
        for guild_id, user_id, spot_id, duration_id, started_at in self.fishing.notified_active():
            view = FishingNotificationView(self, guild_id, user_id, spot_id, duration_id, started_at)
            self.bot.add_view(view)
            self.notification_views.add(view)
        for guild_id, user_id, location_id, plant_id, planted_at in self.farming.notified_active():
            view = FarmingNotificationView(self, guild_id, user_id, location_id, plant_id, planted_at)
            self.bot.add_view(view)
            self.notification_views.add(view)
        self.voice_tick.start()
        self.fishing_notification_tick.start()
        self.farming_notification_tick.start()
        self.raids.start()
        self.tavern.start()
        self.total_raids.start()
        self.painted_maze.start()

    async def cog_unload(self):
        self.voice_tick.cancel()
        self.fishing_notification_tick.cancel()
        self.farming_notification_tick.cancel()
        await self.raids.close()
        self.tavern.close()
        await self.total_raids.close()
        self.painted_maze.close()
        for view in tuple(self.menu_views):
            view.closed = True
            view.stop()
        for view in tuple(self.notification_views):
            view.stop()
        self.store.close()

    @commands.Cog.listener()
    async def on_message(self, message):
        if (not self.settings.enabled or message.guild is None or message.author.bot
                or message.webhook_id is not None or message.is_system()
                or len(''.join(message.content.split())) < self.settings.text_min_chars
                or not self.store.has_player(message.guild.id, message.author.id)):
            return
        self.store.award_text(message.guild.id, message.author.id, time.time(),
                              self.settings.text_xp, self.settings.text_cooldown_seconds,
                              self.settings.text_daily_xp_limit, scale=True)

    def update_voice(self, guild):
        if not self.settings.enabled or guild.unavailable:
            self.tracker.clear(guild.id)
            return
        eligible = eligible_voice_members(guild, self.settings.voice_min_members)
        eligible = {user_id for user_id in eligible if self.store.has_player(guild.id, user_id)}
        awards = self.tracker.update(guild.id, eligible, time.monotonic(),
                                     1)
        if awards:
            self.store.award_voice(awards, daily_limit=self.settings.voice_daily_xp_limit, now=time.time(),
                                   xp_per_minute=self.settings.voice_xp_per_minute)

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

    @tasks.loop(seconds=30)
    async def fishing_notification_tick(self):
        for guild_id, user_id, spot_id, duration_id, started_at in self.fishing.notifications_due():
            if not self.fishing.reserve_notification(guild_id, user_id):
                continue
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                guild = self.bot.get_guild(guild_id)
                guild_name = discord.utils.escape_markdown(guild.name if guild else str(guild_id))
                embed = discord.Embed(title='安安大冒險｜可以收竿了！', color=0x38BDF8,
                    description=f'你在 **{guild_name}** 的 **{SPOTS[spot_id].name}** 已經釣完了。')
                embed.set_footer(text='可直接使用下方按鈕，或回到伺服器的釣魚頁收竿。')
                view = FishingNotificationView(self, guild_id, user_id, spot_id, duration_id, started_at)
                self.notification_views.add(view)
                await asyncio.wait_for(user.send(embed=embed, view=view,
                                                 allowed_mentions=discord.AllowedMentions.none()), timeout=20)
            except (discord.HTTPException, asyncio.TimeoutError, AttributeError):
                logging.info('Fishing completion DM could not be delivered for guild %s user %s',
                             guild_id, user_id)

    @fishing_notification_tick.before_loop
    async def before_fishing_notification_tick(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=30)
    async def farming_notification_tick(self):
        for guild_id, user_id, location_id, plant_id, planted_at in self.farming.notifications_due():
            if not self.farming.reserve_notification(guild_id, user_id, location_id):
                continue
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                guild = self.bot.get_guild(guild_id)
                guild_name = discord.utils.escape_markdown(guild.name if guild else str(guild_id))
                embed = discord.Embed(title='安安大冒險｜植物成熟了！', color=0x65A30D,
                    description=(f'你在 **{guild_name}** 的 **{LOCATIONS[location_id]}** 種植的'
                                 f' **{PLANTS[plant_id].name}** 已經成熟。'))
                embed.set_footer(text='可直接使用下方按鈕，或回到伺服器的農耕頁收成。')
                view = FarmingNotificationView(self, guild_id, user_id, location_id, plant_id, planted_at)
                self.notification_views.add(view)
                await asyncio.wait_for(user.send(embed=embed, view=view,
                                                 allowed_mentions=discord.AllowedMentions.none()), timeout=20)
            except (discord.HTTPException, asyncio.TimeoutError, AttributeError):
                logging.info('Farming completion DM could not be delivered for guild %s user %s location %s',
                             guild_id, user_id, location_id)

    @farming_notification_tick.before_loop
    async def before_farming_notification_tick(self):
        await self.bot.wait_until_ready()


    @app_commands.command(name='冒險', description='開啟安安大冒險：角色、戰鬥、背包、商店、生活與移動')
    @app_commands.guild_only()
    async def adventure(self, interaction: discord.Interaction):
        if not self.store.has_player(interaction.guild_id, interaction.user.id):
            await interaction.response.send_message(
                '你還沒有正式加入安安大冒險。請先接受一封邀請函！', ephemeral=True)
            return
        view = AdventureView(self, interaction)
        self.menu_views.add(view)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='酒館', description='開啟冒險者酒館：準備料理、請大家喝一杯與張貼懸賞')
    @app_commands.guild_only()
    async def open_tavern(self, interaction: discord.Interaction):
        if not self.store.has_player(interaction.guild_id, interaction.user.id):
            await interaction.response.send_message(
                '你還沒有正式加入安安大冒險。請先接受一封邀請函！', ephemeral=True)
            return
        view = TavernView(self, interaction)
        self.menu_views.add(view)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='開啟繪境迷廊', description='管理員測試：以指定畫作建立繪境迷廊房間')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.rename(painting='畫作')
    @app_commands.choices(painting=[
        app_commands.Choice(name='未完成的畫作（城崎諾亞路線）', value='noah:unfinished'),
        app_commands.Choice(name='《氣球》的畫作（繪畫之影路線）', value='painting:balloon'),
    ])
    async def open_painted_maze(self, interaction: discord.Interaction,
                                painting: app_commands.Choice[str]):
        if not interaction.permissions.administrator:
            await interaction.response.send_message('這個測試指令僅限管理員使用。', ephemeral=True)
            return
        if not self.store.has_player(interaction.guild_id, interaction.user.id):
            await interaction.response.send_message('請先接受邀請函，正式成為冒險者。', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            room = await self.painted_maze.create(
                interaction, painting.value, require_entry=False)
        except (CharacterError, discord.HTTPException) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f'已建立 **{MODE_NAME} #{room["number"]}** 與私人討論串。', ephemeral=True)

    @app_commands.command(name='結束繪境迷廊', description='管理員強制結束目前私人討論串的迷廊房間')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def end_painted_maze(self, interaction: discord.Interaction):
        room = self.painted_maze.repo.by_thread(interaction.channel_id)
        if not room:
            await interaction.response.send_message('目前頻道不是進行中的繪境迷廊房間。', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await self.painted_maze.close_room(room['id'], interaction.user, administrator=True)
        except CharacterError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send('已強制結束並封存這個房間。', ephemeral=True)

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
        fishing_records = self.fishing.records(guild_id, member.id)
        if len(fishing_records) == len(BIG_FISH):
            embed.add_field(name='生活稱號', value='魔女島釣師', inline=False)
        now = time.time()
        embed.add_field(name='今日聊天經驗（台灣時間）', value=
                        f'文字：{self.store.daily_xp(guild_id, member.id, "text", now):,} / {scaled_chat_xp(self.settings.text_daily_xp_limit, xp):,} XP\n'
                        f'語音：{self.store.daily_xp(guild_id, member.id, "voice", now):,} / {scaled_chat_xp(self.settings.voice_daily_xp_limit, xp):,} XP\n'
                        f'目前文字每次 {scaled_chat_xp(self.settings.text_xp, xp):,} XP；語音每分鐘 {scaled_chat_xp(self.settings.voice_xp_per_minute, xp):,} XP。\n'
                        '上限隨目前等級成長；每日 00:00 重置，討伐經驗不計入。', inline=False)
        embed.add_field(name='基礎能力＋飾品加成', value='\n'.join(
            f'{name}：{total}（{base} + {bonus}）' for name, total, base, bonus in
            zip(STAT_NAMES, state['total'], state['base'], state['bonus'])), inline=False)
        combat_labels = {'命中率': '命中值', '閃避率': '閃避值'}
        embed.add_field(name='戰鬥能力', value='｜'.join(
            f'{combat_labels.get(name, name)} {value}'
            f'{"%" if name == "暴擊率" else ""}'
            for name, value in state['combat'].items())
            + f'｜暴擊傷害 {state["critical_damage_percent"]}%', inline=False)
        if 'goblin:badge' in state['equipped'].values():
            embed.add_field(name='戰團徽章', value='開戰每兩名參戰者（不足兩人進位）使五項能力各 +1，最多各 +5。整場固定，僅自身，上方能力尚未計入。', inline=False)
        embed.add_field(name='武器／套裝直接加成', value='、'.join(
            f'{name} +{value}' for name, value in state['combat_bonus'].items() if value) or '無', inline=False)
        if state.get('lifesteal'):
            embed.add_field(name='武器吸血', value=f'{state["lifesteal"]}%：依直接傷害實際扣血量回復自身 HP，逐次向下取整。')
        embed.add_field(name='武器穩定度', value=(f'{state["stability"][0]}–{state["stability"][1]}% 傷害'
                        if '武器' in state['equipped'] else '未裝備武器，無法造成傷害'))
        embed.add_field(name=f'裝備欄・飾品 {state["capacity"]} 格',
                        value=equipment_slot_text(state), inline=False)
        equipped_ids = set(state.get('equipped_instances', {}).values())
        socketed = [crystal for crystal in self.painted_maze.crystals.inventory(guild_id, member.id)
                    if crystal.equipment_instance_id in equipped_ids]
        if socketed:
            equipment_names = {
                identity: item_display_name(ITEMS[state['equipped'][slot]])
                for slot, identity in state['equipped_instances'].items()
            }
            embed.add_field(name='已鑲嵌顏料結晶', value='\n'.join(
                f'{equipment_names[crystal.equipment_instance_id]}｜{crystal_affix_name(crystal)}：'
                f'{crystal_effect_text(crystal)}' for crystal in socketed)[:1024], inline=False)
        if state['job'] == '民兵':
            embed.description = 'Lv.10 起可使用 `/冒險 → 轉職` 選擇職業。'
        elif state['stage'] < 3:
            embed.description = f'下次晉升：Lv.{stage_level(state["stage"] + 1, self.settings)}；進階裝備可從 `/冒險 → 商店` 購買取得。'
        embed.set_footer(text='透過文字聊天與語音參與累積經驗' if self.settings.enabled else '目前暫停取得經驗值')
        return embed

    @staticmethod
    def adventurer_comment(state, showcase=None):
        """A deterministic Anan-flavoured line. This never calls an AI model."""
        if '武器' not in state['equipped']:
            return '安安：「連武器都忘了帶……吾輩先站遠一點。」'
        showcase_comments = {
            'paint:red': '安安：「紅色還是有點可怕……不過，有諾亞的蝴蝶在就沒事。」',
            'paint:yellow': '安安：「諾亞還是喜歡這麼亮的顏色……吾輩已經習慣一點了。」',
            'paint:blue': '安安：「這種藍色很安靜……放在吾輩和諾亞的房間也可以。」',
            'paint:set': '安安：「又要讓諾亞畫畫了？……這次先開窗。吾輩會陪她畫完。」',
            'noah:unfinished': '安安：「諾亞還沒畫完。……沒關係，吾輩會在這裡等她。」',
            'painting:balloon': '安安：「『氣球』就是諾亞。……不管這幅畫有沒有魔法，吾輩都不會認錯。」',
        }
        if showcase in showcase_comments:
            return showcase_comments[showcase]
        if showcase and showcase.startswith('noah:'):
            if showcase.endswith(':red'):
                return '安安：「紅色還是有點可怕……但諾亞說會變成蝴蝶，那就沒關係。」'
            if showcase.endswith(':yellow'):
                return '安安：「亮得吾輩眼睛好痛……算了，諾亞高興就好。」'
            if showcase.endswith(':blue'):
                return '安安：「安靜的藍色……很像吾輩和諾亞待在房間裡的時候。」'
            return '安安：「是諾亞畫的。……不管魔法有沒有幫忙，都是真的。」'
        if showcase and showcase.startswith('maze:choice_box:'):
            return '安安：「繪境把選擇也裝進箱子裡了……你自己選，吾輩不替你決定。」'
        if showcase and showcase.startswith('maze:'):
            return '安安：「這些線條還沒有結局……沒關係，諾亞還在，總有一天會畫完。」'
        if showcase and showcase.startswith(('clock:', 'puppet:', 'plague:')):
            return '安安：「居然把這種戰利品帶回來了……有點厲害。」'
        comments = {
            '民兵': '安安：「還在摸索也沒關係，木棒拿穩就好了。」',
            '裝甲步兵': '安安：「站在最前面揮武器……光看就覺得很累。」',
            '騎士': '安安：「這個人應該會擋在大家前面……很可靠。」',
            '弓兵': '安安：「最好別亂跑，箭已經瞄準那邊了。」',
            '僧侶': '安安：「受傷的話就靠近一點……應該會被照顧吧。」',
        }
        return comments[state['job']]

    def adventurer_embed(self, guild_id, member):
        state = self.characters.snapshot(guild_id, member.id)
        showcase = self.characters.showcase(guild_id, member.id)
        roles = {
            '民兵': '初出茅廬的萬用冒險者',
            '裝甲步兵': '攻守兼備的前線鬥士',
            '騎士': '承受攻擊、守護隊友的前衛',
            '弓兵': '高速而靈巧的遠程輸出',
            '僧侶': '治療與強化隊伍的支援者',
        }
        embed = discord.Embed(title='安安大冒險｜冒險者名片', color=0x8B5CF6,
                              description=(f'**Lv.{state["level"]}・{state["title"]}**\n'
                                           f'{roles[state["job"]]}\n\n'
                                           f'{self.adventurer_comment(state, showcase)}'))
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.add_field(name='目前裝備', value=equipment_slot_text(state), inline=False)
        rules = sorted(self.tactics.rules(guild_id, member.id, state['job']), key=lambda rule: rule.slot)
        embed.add_field(name='已裝備技能', value='｜'.join(rule_skill(state['job'], rule).name for rule in rules),
                        inline=False)
        passive = self.tactics.passive(guild_id, member.id, state['job'])
        if passive:
            embed.add_field(name='職業被動', value=passive.name, inline=False)
        if showcase:
            item = self.characters.showcase_item(guild_id, member.id)
            detail = item.description or item_text(item)
            embed.add_field(name='展示品', value=f'**{item.name}**\n{detail}'[:1024], inline=False)
        else:
            embed.add_field(name='展示品', value='尚未設定', inline=False)
        fishing_record = self.fishing.display_record(guild_id, member.id)
        if fishing_record:
            fish_id = fishing_record['fish_id']
            embed.add_field(name='展示漁獲', value=(
                f'{BIG_FISH[fish_id].name}・{fishing_record["best_weight_g"] / 1000:.1f} kg\n'
                f'{SPOTS[fish_id].name}｜{ITEMS[fishing_record["best_rod_id"]].name}'), inline=False)
        embed.set_footer(text='不公開金幣、背包內容、每日活動量與自動戰鬥規則。')
        return embed

    @app_commands.command(name='冒險者', description='查看自己或其他成員的公開冒險者名片')
    @app_commands.guild_only()
    @app_commands.rename(member='成員')
    @app_commands.describe(member='要查看的成員；不填則查看自己')
    async def adventurer(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        if member.bot:
            await interaction.response.send_message('機器人沒有冒險者名片。', ephemeral=True)
            return
        if not self.store.has_player(interaction.guild_id, member.id):
            await interaction.response.send_message(f'{member.display_name} 還沒有開始安安大冒險。', ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())
            return
        await interaction.response.send_message(embed=self.adventurer_embed(interaction.guild_id, member),
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='邀請', description='張貼安安大冒險邀請函，或私訊指定成員')
    @app_commands.guild_only()
    @app_commands.rename(member='成員')
    @app_commands.describe(member='指定時只私訊該成員；不填則在目前頻道張貼公開邀請函')
    async def invite(self, interaction: discord.Interaction, member: discord.Member = None):
        if member is not None and member.bot:
            await interaction.response.send_message('不能邀請機器人成為冒險者。', ephemeral=True)
            return
        try:
            role = await self.invitations.role_for(interaction.guild)
        except CharacterError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        target = member.id if member is not None else None
        token = self.invitations.repo.create(interaction.guild_id, interaction.user.id, target)
        view = AdventurerInvitationView(self.invitations, token)
        if member is None:
            embed = discord.Embed(
                title='安安大冒險｜來自夏目安安的邀請函', color=0x8B5CF6,
                description=('夏目安安：「喂，吾輩手上正好有一封多出來的邀請函……才、才不是特地留給你的！\n\n'
                             '如果你願意踏進這場冒險，就按下 **接受邀請**。吾輩會替你準備冒險者身分與第一把木棒。」'))
            embed.add_field(name='邀請人', value=interaction.user.mention)
            embed.add_field(name='加入後取得', value=f'{role.mention}・Lv.1 民兵・T1 木棒', inline=False)
            embed.set_footer(text='接受後才會建立角色；加入前的聊天與語音不會累積冒險 XP。')
            try:
                await interaction.response.send_message(
                    embed=embed, view=view,
                    allowed_mentions=discord.AllowedMentions.none())
                message = await interaction.original_response()
                self.invitations.repo.bind_message(token, message.id)
            except Exception:
                self.invitations.repo.delete(token)
                raise
            return

        embed = discord.Embed(
            title='安安大冒險｜夏目安安寄來的邀請函', color=0x8B5CF6,
            description=(f'夏目安安：「{member.display_name}，有人覺得你有成為冒險者的資質，所以拜託吾輩把這封信交給你。\n\n'
                         '哼哼，按下 **接受邀請** 的話，吾輩就准你加入隊伍，還會送你一把新手木棒。可別讓吾輩失望喔！」'))
        embed.add_field(name='邀請人', value=interaction.user.display_name)
        embed.add_field(name='加入後取得', value=f'{role.name}・Lv.1 民兵・T1 木棒', inline=False)
        embed.set_footer(text=f'這封邀請只限 {member.display_name} 接受。')
        await interaction.response.defer(ephemeral=True)
        try:
            message = await member.send(embed=embed, view=view,
                                        allowed_mentions=discord.AllowedMentions.none())
            self.invitations.repo.bind_message(token, message.id)
        except (discord.Forbidden, discord.HTTPException):
            self.invitations.repo.delete(token)
            await interaction.followup.send('無法私訊這位成員；請請對方開啟伺服器成員私訊後再試。', ephemeral=True)
            return
        await interaction.followup.send(f'已將安安的邀請函私訊給 {member.display_name}。', ephemeral=True)

    @app_commands.command(name='排行榜', description='查看本伺服器經驗值前十名')
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        rows = self.store.leaders(interaction.guild_id)
        lines = [f'{index}. <@{user_id}> — Lv.{level_for(xp)} · {xp:,} XP'
                 for index, (user_id, xp) in enumerate(rows, 1)]
        embed = discord.Embed(title='安安大冒險｜冒險者排行榜', description='\n'.join(lines) or '還沒有冒險者，開始聊天來獲得經驗吧！', color=0xF59E0B)
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='討伐通知', description='向安安領取或取消討伐通知身分組')
    @app_commands.guild_only()
    @app_commands.rename(action='操作', kind='類型')
    @app_commands.choices(action=[app_commands.Choice(name='領取', value='subscribe'),
                                 app_commands.Choice(name='取消', value='unsubscribe')],
                         kind=[app_commands.Choice(name='一般討伐', value='regular'),
                               app_commands.Choice(name='中階討伐', value='mid'),
                               app_commands.Choice(name='高階討伐', value='high'),
                               app_commands.Choice(name='全部', value='all')])
    async def raid_notifications(self, interaction: discord.Interaction, action: str = 'subscribe', kind: str = 'regular'):
        if not self.store.has_player(interaction.guild_id, interaction.user.id):
            await interaction.response.send_message('請先接受邀請函，正式成為冒險者。', ephemeral=True)
            return
        if action not in ('subscribe', 'unsubscribe'):
            await interaction.response.send_message('請選擇領取或取消。', ephemeral=True)
            return
        if kind not in ('regular', 'mid', 'high', 'all'):
            await interaction.response.send_message('請選擇一般討伐、中階討伐、高階討伐或全部。', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            messages = [await self.raids.notifications.subscribe(interaction.guild, interaction.user,
                        action == 'subscribe', selected) for selected in
                        (('regular', 'mid', 'high') if kind == 'all' else (kind,))]
            message = '\n'.join(messages)
        except CharacterError as exc:
            message = str(exc)
        except discord.Forbidden:
            message = '身分組操作失敗，請確認安安有「管理身分組」權限，且位置高於討伐通知身分組。'
        except discord.HTTPException:
            message = 'Discord 暫時無法更新身分組，請稍後再試。'
        await interaction.followup.send(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='冒險區域', description='管理員檢查、建立或修復安安大冒險的分類、頻道與身分組')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.rename(action='操作')
    @app_commands.choices(action=[app_commands.Choice(name='查看狀態', value='status'),
                                 app_commands.Choice(name='建立／匯入', value='setup'),
                                 app_commands.Choice(name='修復分類與權限', value='repair')])
    async def adventure_space(self, interaction: discord.Interaction, action: str = 'status'):
        if interaction.guild is None or not interaction.permissions.administrator:
            await interaction.response.send_message('只有伺服器管理員可以管理冒險區域。', ephemeral=True)
            return
        if action not in ('status', 'setup', 'repair'):
            await interaction.response.send_message('請選擇查看狀態、建立／匯入或修復。', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            if action == 'setup':
                message = await self.spaces.setup(interaction.guild)
            elif action == 'repair':
                message = await self.spaces.repair(interaction.guild)
            else:
                message = self.spaces.status_text(interaction.guild)
        except CharacterError as exc:
            message = str(exc)
        except discord.Forbidden:
            message = '安安無法調整冒險區域，請確認「管理頻道」、「管理身分組」及身分組順位。'
        except discord.HTTPException:
            logging.exception('Adventure space update failed')
            message = 'Discord 暫時無法更新冒險區域，請稍後再試。'
        await interaction.followup.send(message, ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())


    @app_commands.command(name='生成討伐', description='管理員立即在目前的討伐頻道生成魔物，五分鐘後開戰')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.rename(kind='類型', name='名稱', strength='強度', victory_xp='經驗',
                         victory_gold='金幣', drop_percent='掉落率')
    @app_commands.describe(kind='不填則隨機抽取怪物類型', name='這場怪物的名稱，最多20字',
        strength='血量、攻擊、防禦倍率，0.1–10；會再乘頻道動態難度', victory_xp='勝利每人最終經驗，覆蓋類型預設獎勵',
        victory_gold='勝利每人最終金幣，覆蓋類型預設獎勵', drop_percent='專屬物品掉落百分比，0–100；史萊姆群固定不掉落')
    @app_commands.choices(kind=[app_commands.Choice(name=k, value=k) for k in
        ('巨獸', '毒蛛', '史萊姆群', '鐵殼魔像', '荊棘妖樹', '哥布林戰團', '月影妖狐', '血翼蝠王',
         '深淵鐘龍', '王城傀儡師', '瘟疫縫合獸', '赤雷與蒼炎', '吞城鯨',
         '熔爐鎧獸', '迷霧菌后', '星蝕巨神', '逆潮聖骸')])
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
                                            victory_xp=victory_xp, victory_gold=victory_gold,
                                            drop_percent=drop_percent, source='admin')
        except CharacterError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            logging.exception('Administrator raid spawn failed')
            await interaction.followup.send('生成討伐失敗，請確認機器人的頻道權限後再試。', ephemeral=True)
            return
        await interaction.followup.send(f'討伐已發布，五分鐘後開戰：{message.jump_url}', ephemeral=True)

    @app_commands.command(name='開始總力戰', description='管理員建立一個測試用總力戰房間')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.rename(boss='首領')
    @app_commands.choices(boss=[app_commands.Choice(name=name, value=name) for name in TOTAL_RAID_BOSSES])
    async def create_total_raid(self, interaction: discord.Interaction, boss: str = '訓練用假人'):
        if interaction.guild is None or not interaction.permissions.administrator:
            await interaction.response.send_message('只有伺服器管理員可以建立總力戰房間。', ephemeral=True)
            return
        if not self.store.has_player(interaction.guild_id, interaction.user.id):
            await interaction.response.send_message('請先接受邀請函，正式成為冒險者。', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            room, channel = await self.total_raids.create_room(interaction.guild, interaction.user, boss)
        except (CharacterError, TotalRaidError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.followup.send('無法建立總力戰頻道，請確認安安具有管理頻道權限。', ephemeral=True)
            return
        except discord.HTTPException:
            logging.exception('Total raid room creation failed')
            await interaction.followup.send('Discord 暫時無法建立總力戰房間，請稍後再試。', ephemeral=True)
            return
        await interaction.followup.send(
            f'已建立 {room["boss"]} #{room["number"]}：{channel.mention}', ephemeral=True)

    @app_commands.command(name='戰鬥統計', description='管理員查看近期討伐的平衡數據')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.rename(days='天數')
    async def battle_statistics(self, interaction: discord.Interaction,
                                days: app_commands.Range[int, 1, 365] = 30):
        if interaction.guild_id is None or not interaction.permissions.administrator:
            await interaction.response.send_message('只有伺服器管理員可以查看戰鬥統計。', ephemeral=True)
            return
        report = self.raids.repo.balance_report(interaction.guild_id, time.time() - days * 86400)
        count, wins, average_rounds, average_players = report['overall']
        if not count:
            await interaction.response.send_message(f'最近 {days} 天尚無新版戰鬥統計。', ephemeral=True)
            return
        embed = discord.Embed(title=f'安安大冒險｜最近 {days} 天戰鬥統計', color=0x3B82F6,
                              description=f'{count} 場｜勝率 {wins * 100 / count:.1f}%｜平均 {average_rounds:.1f} 回合｜平均 {average_players:.1f} 人')
        monster_lines = [f'{kind}：{battles} 場｜勝率 {victories * 100 / battles:.1f}%｜平均 {rounds:.1f} 回合｜強度 {strength:.2f}'
                         for kind, battles, victories, rounds, strength in report['monsters']]
        job_lines = [f'{job}：{battles} 人次｜勝率 {victories * 100 / battles:.1f}%｜'
                     f'實際傷害 {direct:.0f}｜輔助傷害 {support:.0f}｜治療 {healing:.0f}｜承傷 {taken:.0f}｜輔助承傷 {support_taken:.0f}'
                     for job, battles, victories, direct, support, healing, taken, support_taken in report['jobs']]
        embed.add_field(name='怪物表現', value='\n'.join(monster_lines)[:1024], inline=False)
        embed.add_field(name='職業每場平均', value='\n'.join(job_lines)[:1024] or '尚無玩家資料', inline=False)
        embed.set_footer(text='僅統計本功能上線後完成的討伐；樣本少時請勿單獨依勝率調整。')
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    def skills_embed(self, guild, user):
        state = self.characters.snapshot(guild, user)
        embed = discord.Embed(title=f'{state["title"]}・自動技能', color=0x8B5CF6,
                              description='每回合由優先 1 開始檢查，施放第一個符合條件且冷卻結束的技能；否則普攻。\n'
                              '指向特定對象的條件會先限制合法目標；沒有符合者便檢查下一個技能。\n'
                              '【準備】技能會在一般行動前結算，彼此仍依速度排序。\n'
                              '固定三個主動技能格；Lv.20 解鎖兩個進階技能。Lv.50 解鎖一個三選一職業被動格。')
        for rule in self.tactics.rules(guild, user, state['job']):
            skill = rule_skill(state['job'], rule)
            embed.add_field(name=f'優先 {rule.priority}｜槽 {rule.slot}：{skill.name}｜{"開" if rule.enabled else "關"}',
                            value=f'{skill_description(skill)}\n冷卻 {skill.cooldown} 回合｜{condition_text(rule.condition, rule.condition_value)}'
                                  f'｜目標：{FIXED_TARGETS.get(skill.effect, TARGETS[rule.target])}', inline=False)
        passive = self.tactics.passive(guild, user, state['job'])
        embed.add_field(name='普通攻擊',
                        value=f'目標：{TARGETS[self.tactics.basic_target(guild, user, state["job"])]}', inline=False)
        if passive:
            embed.add_field(name=f'職業被動｜{passive.name}', value=passive.description, inline=False)
        elif self.tactics.available_passives(guild, user, state['job']):
            embed.add_field(name='職業被動｜尚未選擇', value='Lv.50 已解鎖三個職業被動，請從技能面板選擇一個。', inline=False)
        embed.set_footer(text='在 /冒險 → 技能 面板調整。冷卻包含施放當回合；CD 2 在第 1 回合施放，第 3 回合可再用。自身技能作用於自己；護衛作用全隊；範圍攻擊作用全體敵人，皆忽略目標選項。「優先」目標不存在時會改選其他合法目標。')
        return embed


async def setup(bot):
    await bot.add_cog(RPG(bot))
