"""Adventurers' tavern bounties and public round-of-drinks offers."""
import asyncio
from dataclasses import dataclass
import os
import time
import uuid

import discord

from core.rpg_character import CharacterError
from core.rpg_menu import add_help, add_back
from core.rpg_provisions import effect_text, guest_reward_target


@dataclass(frozen=True)
class DrinkPackage:
    name: str
    price: int
    capacity: int
    description: str


DRINK_PACKAGES = {
    'table': DrinkPackage('米莉亞的電影同樂會', 500, 5, '請一桌冒險者參加'),
    'hall': DrinkPackage('梅露露的暖心茶會', 1_000, 10, '請酒館大廳的冒險者參加'),
    'festival': DrinkPackage('諾亞的豪華監獄餐會', 2_000, 20, '請全場冒險者參加'),
}
DRINK_XP_PERCENT = 5
DRINK_CLAIM_SECONDS = 2 * 60 * 60
DRINK_EFFECT_SECONDS = 24 * 60 * 60
BOUNTY_PRICES = {'regular': 2_000, 'mid': 5_000, 'high': 8_000}


class TavernStore:
    def __init__(self, store):
        self.store, self.db = store, store.db
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_tavern_drinks (
                id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, host_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL, message_id INTEGER, package_id TEXT NOT NULL,
                capacity INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                status TEXT NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_tavern_drink_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                offer_id TEXT NOT NULL, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                claimed_at REAL NOT NULL, valid_until REAL NOT NULL, consumed_raid_id TEXT)''')
            claim_columns = {row[1] for row in self.db.execute(
                'PRAGMA table_info(rpg_tavern_drink_claims)')}
            if 'id' not in claim_columns:
                self.db.execute('ALTER TABLE rpg_tavern_drink_claims RENAME TO rpg_tavern_drink_claims_legacy')
                self.db.execute('''CREATE TABLE rpg_tavern_drink_claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offer_id TEXT NOT NULL, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    claimed_at REAL NOT NULL, valid_until REAL NOT NULL, consumed_raid_id TEXT)''')
                self.db.execute('''INSERT INTO rpg_tavern_drink_claims
                    (offer_id,guild_id,user_id,claimed_at,valid_until,consumed_raid_id)
                    SELECT offer_id,guild_id,user_id,claimed_at,valid_until,consumed_raid_id
                    FROM rpg_tavern_drink_claims_legacy''')
                self.db.execute('DROP TABLE rpg_tavern_drink_claims_legacy')
            self.db.execute('''CREATE INDEX IF NOT EXISTS rpg_tavern_drink_claims_offer
                ON rpg_tavern_drink_claims(offer_id,claimed_at,id)''')
            self.db.execute('''CREATE INDEX IF NOT EXISTS rpg_tavern_drink_claims_active
                ON rpg_tavern_drink_claims(guild_id,user_id,consumed_raid_id,valid_until)''')

    def recover_drafts(self):
        rows = self.db.execute(
            "SELECT id FROM rpg_tavern_drinks WHERE status='posting' AND message_id IS NULL").fetchall()
        for (offer_id,) in rows:
            self.cancel_offer(offer_id, refund=True)

    def create_offer(self, guild, host, channel, package_id, now=None):
        package = DRINK_PACKAGES.get(package_id)
        if package is None:
            raise CharacterError('無效的請客方案。')
        now = time.time() if now is None else now
        offer = dict(id=uuid.uuid4().hex, guild_id=guild, host_id=host, channel_id=channel,
                     message_id=None, package_id=package_id, capacity=package.capacity,
                     created_at=now, expires_at=now + DRINK_CLAIM_SECONDS, status='posting')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            paid = self.db.execute('''UPDATE rpg_wallets SET gold=gold-?
                WHERE guild_id=? AND user_id=? AND gold>=?''', (package.price, guild, host, package.price))
            if not paid.rowcount:
                raise CharacterError(f'金幣不足，請客需要 {package.price:,} 金幣。')
            self.db.execute('''INSERT INTO rpg_tavern_drinks
                (id,guild_id,host_id,channel_id,message_id,package_id,capacity,created_at,expires_at,status)
                VALUES (?,?,?,?,?,?,?,?,?,?)''', tuple(offer.values()))
        return offer

    def publish_offer(self, offer_id, message_id):
        with self.db:
            changed = self.db.execute("UPDATE rpg_tavern_drinks SET message_id=?,status='open' "
                                      "WHERE id=? AND status='posting'", (message_id, offer_id))
            if not changed.rowcount:
                raise CharacterError('這次請客已經失效。')
        return self.offer(offer_id)

    def cancel_offer(self, offer_id, refund=False):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            offer = self.offer(offer_id)
            if not offer or offer['status'] in ('cancelled', 'refunded'):
                return offer
            status = 'refunded' if refund else 'cancelled'
            self.db.execute('UPDATE rpg_tavern_drinks SET status=? WHERE id=?', (status, offer_id))
            if refund:
                package = DRINK_PACKAGES[offer['package_id']]
                self.db.execute('''INSERT INTO rpg_wallets(guild_id,user_id,gold) VALUES (?,?,?)
                    ON CONFLICT(guild_id,user_id) DO UPDATE SET gold=gold+excluded.gold''',
                                (offer['guild_id'], offer['host_id'], package.price))
        return self.offer(offer_id)

    def offer(self, offer_id):
        row = self.db.execute('''SELECT id,guild_id,host_id,channel_id,message_id,package_id,
            capacity,created_at,expires_at,status FROM rpg_tavern_drinks WHERE id=?''', (offer_id,)).fetchone()
        if not row:
            return None
        keys = ('id', 'guild_id', 'host_id', 'channel_id', 'message_id', 'package_id',
                'capacity', 'created_at', 'expires_at', 'status')
        return dict(zip(keys, row))

    def claim_count(self, offer_id):
        return self.db.execute('SELECT COUNT(*) FROM rpg_tavern_drink_claims WHERE offer_id=?',
                               (offer_id,)).fetchone()[0]

    def claimants(self, offer_id):
        return [row[0] for row in self.db.execute('''SELECT user_id
            FROM rpg_tavern_drink_claims WHERE offer_id=? ORDER BY claimed_at,id''',
            (offer_id,)).fetchall()]

    def open_offers(self, now=None):
        now = time.time() if now is None else now
        rows = self.db.execute("SELECT id FROM rpg_tavern_drinks WHERE status='open' "
                               'AND message_id IS NOT NULL AND expires_at>?', (now,)).fetchall()
        return [self.offer(row[0]) for row in rows]

    def active_effect(self, guild, user, now=None):
        now = time.time() if now is None else now
        row = self.db.execute('''SELECT c.offer_id,c.valid_until,o.package_id
            FROM rpg_tavern_drink_claims c
            JOIN rpg_tavern_drinks o ON o.id=c.offer_id
            WHERE c.guild_id=? AND c.user_id=? AND c.consumed_raid_id IS NULL
            AND c.valid_until>? ORDER BY c.claimed_at,c.id LIMIT 1''',
                              (guild, user, now)).fetchone()
        if not row:
            return None
        offer_id, valid_until, package_id = row
        return dict(offer_id=offer_id, valid_until=valid_until, package_id=package_id,
                    name=DRINK_PACKAGES[package_id].name, xp_percent=DRINK_XP_PERCENT)

    def claim(self, offer_id, guild, user, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            offer = self.offer(offer_id)
            if not offer or offer['guild_id'] != guild:
                raise CharacterError('找不到這次請客。')
            if offer['status'] != 'open' or now >= offer['expires_at']:
                raise CharacterError('這次請客已經結束了。')
            active = self.db.execute('''SELECT 1 FROM rpg_tavern_drink_claims
                WHERE guild_id=? AND user_id=? AND consumed_raid_id IS NULL AND valid_until>? LIMIT 1''',
                                     (guild, user, now)).fetchone()
            if active:
                raise CharacterError('你已經有尚未使用的飲料效果，不能重複領取。')
            if self.claim_count(offer_id) >= offer['capacity']:
                raise CharacterError('這次請客已經客滿了。')
            self.db.execute('''INSERT INTO rpg_tavern_drink_claims
                (offer_id,guild_id,user_id,claimed_at,valid_until,consumed_raid_id)
                VALUES (?,?,?,?,?,NULL)''',
                (offer_id, guild, user, now, now + DRINK_EFFECT_SECONDS))
        return self.offer(offer_id)

    def prepare_for_raid(self, raid_id, guild, users, now=None):
        now = time.time() if now is None else now
        result = {}
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for user in users:
                row = self.db.execute('''SELECT id FROM rpg_tavern_drink_claims
                    WHERE guild_id=? AND user_id=?
                    AND (consumed_raid_id=? OR (consumed_raid_id IS NULL AND valid_until>?))
                    ORDER BY claimed_at,id LIMIT 1''', (guild, user, raid_id, now)).fetchone()
                if not row:
                    continue
                changed = self.db.execute('''UPDATE rpg_tavern_drink_claims SET consumed_raid_id=?
                    WHERE id=? AND user_id=?
                    AND (consumed_raid_id IS NULL OR consumed_raid_id=?)''',
                                          (raid_id, row[0], user, raid_id))
                if changed.rowcount:
                    result[user] = dict(xp_percent=DRINK_XP_PERCENT)
        return result


async def _refresh_offer(interaction, view, exhausted, key):
    if not exhausted:
        try:
            await interaction.response.edit_message(embed=view.embed(), view=view,
                                                    allowed_mentions=discord.AllowedMentions.none())
        except discord.NotFound:
            pass  # Another guest may have finished the offer and deleted it.
        return
    await interaction.response.defer()
    try:
        await interaction.edit_original_response(embed=view.embed(), view=None,
                                                  allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass
    await asyncio.sleep(5)
    try:
        await interaction.delete_original_response()
    except discord.NotFound:
        pass
    except discord.HTTPException:
        # Keep a failed deletion from leaving an unusable claim button behind.
        try:
            await interaction.edit_original_response(embed=view.embed(), view=None,
                                                      allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
    view.stop()
    view.tavern.views.pop(key, None)


class DrinkOfferView(discord.ui.View):
    def __init__(self, tavern, offer_id):
        super().__init__(timeout=None)
        self.tavern, self.offer_id = tavern, offer_id
        self.claim_button.custom_id = f'tavern:drink:{offer_id}'

    def embed(self):
        offer = self.tavern.store.offer(self.offer_id)
        package = DRINK_PACKAGES[offer['package_id']]
        claimants = self.tavern.store.claimants(self.offer_id)
        embed = discord.Embed(title=f'冒險者酒館｜{package.name}', color=0xC47A3A,
            description=(f'<@{offer["host_id"]}> 請大家喝一杯！\n\n'
                         f'共準備 **{offer["capacity"]}** 杯，每次乾杯可取得「下一場討伐經驗 +{DRINK_XP_PERCENT}%」。\n'
                         '效果保留 24 小時，完成下一場討伐後消耗；'
                         '若公告仍開放，消耗後可再次乾杯。'))
        guest_list = '\n'.join(f'{index}. <@{user_id}>'
                               for index, user_id in enumerate(claimants, 1)) or '尚無人乾杯'
        embed.add_field(name=f'乾杯紀錄 {len(claimants)}/{offer["capacity"]} 杯',
                        value=f'{guest_list}\n<t:{int(offer["expires_at"])}:R> 截止', inline=False)
        return embed

    @discord.ui.button(label='一起乾杯', style=discord.ButtonStyle.success, custom_id='tavern:drink')
    async def claim_button(self, interaction, button):
        if interaction.guild_id is None or interaction.user.bot:
            await interaction.response.send_message('只有伺服器成員可以入席。', ephemeral=True)
            return
        try:
            if not self.tavern.cog.store.has_player(interaction.guild_id, interaction.user.id):
                raise CharacterError('請先接受邀請函，正式成為冒險者。')
            offer = self.tavern.store.claim(self.offer_id, interaction.guild_id, interaction.user.id)
            await _refresh_offer(interaction, self,
                self.tavern.store.claim_count(self.offer_id) >= offer['capacity'], self.offer_id)
        except CharacterError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)


class MealOfferView(discord.ui.View):
    def __init__(self, tavern, meal_id):
        super().__init__(timeout=None)
        self.tavern, self.meal_id = tavern, meal_id
        self.claim_button.custom_id = f'tavern:meal:{meal_id}'
        self.discard_button.custom_id = f'tavern:meal:discard:{meal_id}'

    def embed(self):
        meal = self.tavern.cog.provisions.meal(self.meal_id)
        data = meal['data']
        claimants = self.tavern.cog.provisions.claimants(self.meal_id)
        ingredient_counts = {}
        for key in data['ingredients']:
            ingredient_counts[key] = ingredient_counts.get(key, 0) + 1
        from core.rpg_character import ITEMS
        ingredients = '、'.join(f'{ITEMS[key].name} ×{amount}'
                               for key, amount in ingredient_counts.items())
        secondary = f'／{data["secondary_tag"]}' if data.get('secondary_tag') else ''
        reward_guests = guest_reward_target(meal['capacity'])
        embed = discord.Embed(
            title=f'冒險者酒館｜{data["grade"]} 級・{data["name"]}', color=0xE09B37,
            description=(f'<@{meal["host_id"]}> 完成了一桌料理！\n\n'
                         f'**{data["primary_tag"]}{secondary}**｜{effect_text(data["effect"])}\n'
                         f'美味度 {data["score"]}｜每人持續 {data["duration"]} 場討伐\n'
                         f'食材：{ingredients}\n\n'
                         '料理效果保留 24 小時；正式開戰時消耗一場。\n'
                         f'前 {reward_guests} 位不同客人享用後，會共同替料理者解鎖剩餘 75% 料理 XP。'))
        guest_list = '\n'.join(f'{index}. <@{user_id}>'
                               for index, user_id in enumerate(claimants, 1)) or '尚無人享用'
        deadline = ('發布人已倒掉料理，停止領取。' if meal['status'] == 'cancelled'
                    else f'<t:{int(meal["expires_at"])}:R> 截止')
        embed.add_field(name=f'享用紀錄 {len(claimants)}/{meal["capacity"]} 人',
                        value=f'{guest_list}\n{deadline}', inline=False)
        embed.set_footer(text='發布人可按「倒掉」提早結束；食材不退還，已領取的效果與已取得的料理 XP 保留。')
        return embed

    @discord.ui.button(label='倒掉', style=discord.ButtonStyle.danger, custom_id='tavern:meal:discard')
    async def discard_button(self, interaction, button):
        if interaction.guild_id is None or interaction.user.bot:
            await interaction.response.send_message('只有發布人可以倒掉這桌料理。', ephemeral=True)
            return
        try:
            self.tavern.cog.provisions.discard(
                self.meal_id, interaction.guild_id, interaction.user.id)
            await _refresh_offer(interaction, self, True, f'meal:{self.meal_id}')
        except CharacterError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    @discord.ui.button(label='一起享用', style=discord.ButtonStyle.success, custom_id='tavern:meal')
    async def claim_button(self, interaction, button):
        if interaction.guild_id is None or interaction.user.bot:
            await interaction.response.send_message('只有伺服器成員可以入席。', ephemeral=True)
            return
        try:
            if not self.tavern.cog.store.has_player(interaction.guild_id, interaction.user.id):
                raise CharacterError('請先接受邀請函，正式成為冒險者。')
            provisions = self.tavern.cog.provisions
            provisions.claim(self.meal_id, interaction.guild_id, interaction.user.id)
            await _refresh_offer(interaction, self,
                len(provisions.claimants(self.meal_id)) >= provisions.meal(self.meal_id)['capacity'],
                f'meal:{self.meal_id}')
        except CharacterError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)


class TavernService:
    def __init__(self, cog):
        self.cog, self.store = cog, TavernStore(cog.store)
        self.views = {}
        try:
            self.environment_channel_ids = tuple(dict.fromkeys(
                int(value.strip()) for value in
                os.getenv('RPG_TAVERN_CHANNEL_IDS', '').split(',') if value.strip()))
        except ValueError as exc:
            raise ValueError('RPG_TAVERN_CHANNEL_IDS 必須是逗號分隔的頻道 ID') from exc

    def public_channel(self, interaction):
        guild = interaction.guild
        if guild is None:
            raise CharacterError('酒館公開功能只能在伺服器內使用。')
        space_store = getattr(getattr(self.cog, 'spaces', None), 'store', None)
        space = space_store.get(guild.id) if space_store else None
        managed = (space.tavern_channel_id,) if space and space.tavern_channel_id else ()
        for channel_id in managed + self.environment_channel_ids:
            channel = guild.get_channel(channel_id)
            if channel is not None and callable(getattr(channel, 'send', None)):
                return channel
        raise CharacterError('這個伺服器尚未設定可用的酒館公開頻道。')

    def start(self):
        self.store.recover_drafts()
        self.cog.provisions.recover_drafts()
        for offer in self.store.open_offers():
            view = self.offer_view(offer['id'])
            self.cog.bot.add_view(view, message_id=offer['message_id'])
        for meal in self.cog.provisions.open_meals():
            view = self.meal_view(meal['id'])
            self.cog.bot.add_view(view, message_id=meal['message_id'])

    def close(self):
        for view in self.views.values():
            view.stop()

    def offer_view(self, offer_id):
        if offer_id not in self.views:
            self.views[offer_id] = DrinkOfferView(self, offer_id)
        return self.views[offer_id]

    def meal_view(self, meal_id):
        key = f'meal:{meal_id}'
        if key not in self.views:
            self.views[key] = MealOfferView(self, meal_id)
        return self.views[key]

    async def buy_round(self, interaction, package_id):
        channel = self.public_channel(interaction)
        offer = self.store.create_offer(interaction.guild_id, interaction.user.id,
                                        channel.id, package_id)
        view = self.offer_view(offer['id'])
        try:
            message = await channel.send(embed=view.embed(), view=view,
                allowed_mentions=discord.AllowedMentions.none())
            self.store.publish_offer(offer['id'], message.id)
            return message, self.store.offer(offer['id'])
        except (Exception, asyncio.CancelledError):
            self.store.cancel_offer(offer['id'], refund=True)
            view.stop()
            raise

    async def serve_meal(self, interaction, ingredients):
        channel = self.public_channel(interaction)
        meal = self.cog.provisions.cook(interaction.guild_id, interaction.user.id,
                                        channel.id, ingredients)
        view = self.meal_view(meal['id'])
        try:
            message = await channel.send(embed=view.embed(), view=view,
                allowed_mentions=discord.AllowedMentions.none())
            self.cog.provisions.publish(meal['id'], message.id)
            return message, self.cog.provisions.meal(meal['id'])
        except (Exception, asyncio.CancelledError):
            self.cog.provisions.cancel(meal['id'], refund=True)
            view.stop()
            raise

    async def post_bounty(self, guild, user, pool):
        if pool not in BOUNTY_PRICES:
            raise CharacterError('無效的懸賞類型。')
        return await self.cog.raids.summon_bounty(guild, user, pool, BOUNTY_PRICES[pool])


class TavernView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.closed, self.lock = False, asyncio.Lock()
        self.rebuild()

    def _button(self, label, action, row, style=discord.ButtonStyle.secondary):
        button = discord.ui.Button(label=label, row=row, style=style)
        async def callback(interaction):
            await self.handle(interaction, action)
        button.callback = callback
        self.add_item(button)

    def rebuild(self):
        self.clear_items()
        self._button('一般懸賞（2,000）', 'bounty:regular', 0, discord.ButtonStyle.danger)
        self._button('中階懸賞（5,000）', 'bounty:mid', 0, discord.ButtonStyle.danger)
        self._button('高階懸賞（8,000）', 'bounty:high', 0, discord.ButtonStyle.danger)
        self._button('準備料理', 'cooking', 0, discord.ButtonStyle.success)
        for package_id, package in DRINK_PACKAGES.items():
            self._button(f'{package.name}（{package.price:,}／{package.capacity} 杯）',
                         f'drink:{package_id}', 1, discord.ButtonStyle.success)
        add_help(self, 3, 'life', 'tavern')
        add_back(self, 3)
        self._button('重新整理', 'refresh', 3)
        self._button('關閉', 'close', 3)

    def embed(self, notice=None):
        drink = self.cog.tavern.store.active_effect(self.guild_id, self.owner.id)
        meal = self.cog.provisions.active_effect(self.guild_id, self.owner.id)
        embed = discord.Embed(title='安安大冒險｜冒險者酒館', color=0xC47A3A,
            description=('**張貼懸賞**\n發起者會自動報名。懸賞討伐保留經驗與掉落，但不發金幣、'
                         '不影響頻道動態難度，也不重排正常討伐；到點的正常討伐會等懸賞結束後發布。\n\n'
                         '**請大家喝一杯**\n公開請客，入席者取得下一場討伐經驗 +5%。'
                         '領取時間 2 小時，效果保留 24 小時且不能囤積；消耗後可再次領取。'
                         '飲料與料理分開計算，可以各持有一份並在同場討伐生效。\n\n'
                         '**準備料理**\n選擇五份魚、作物、水草、藥草、肉類或調味料，依標籤與評分公開開桌。\n\n'
                         f'持有金幣：**{self.cog.store.gold(self.guild_id, self.owner.id):,}**'))
        drink_text = '目前沒有飲料效果。'
        if drink:
            drink_text = (f'**{drink["name"]}**｜討伐 XP +{drink["xp_percent"]}%\n'
                          f'<t:{int(drink["valid_until"])}:R> 到期')
        meal_text = '目前沒有料理效果。'
        if meal:
            secondary = f'／{meal["secondary_tag"]}' if meal['secondary_tag'] else ''
            meal_text = (f'**{meal["grade"]} 級・{meal["name"]}**｜'
                         f'{meal["primary_tag"]}{secondary}\n'
                         f'{effect_text(meal["effect"])}\n'
                         f'剩餘 **{meal["remaining"]}** 場｜'
                         f'<t:{int(meal["valid_until"])}:R> 到期')
        embed.add_field(name='目前飲料效果', value=drink_text, inline=False)
        embed.add_field(name='目前料理效果', value=meal_text, inline=False)
        if notice:
            embed.add_field(name='酒館消息', value=notice, inline=False)
        embed.set_footer(text='請客與料理會發布至酒館專用頻道；懸賞會發布至對應的討伐頻道。')
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請使用 /酒館 開啟自己的酒館面板。', ephemeral=True)
            return False
        return True

    async def handle(self, interaction, action):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('酒館面板已關閉，請使用 /酒館 重新進入。', ephemeral=True)
                return
            if action == 'home':
                from core.rpg_menu import navigate
                await navigate(self, interaction, 'home')
                return
            if action == 'close':
                self.closed = True
                self.stop()
                await interaction.response.edit_message(content='你離開了冒險者酒館。', embed=None, view=None)
                return
            if action == 'refresh':
                await interaction.response.edit_message(embed=self.embed(), view=self)
                return
            if action == 'cooking':
                from core.rpg_menu import navigate
                await navigate(self, interaction, 'provisions')
                return
            await interaction.response.defer(ephemeral=True)
            try:
                if action.startswith('bounty:'):
                    pool = action.split(':', 1)[1]
                    channel, _, raid = await self.cog.tavern.post_bounty(interaction.guild, self.owner, pool)
                    notice = f'已花費 {BOUNTY_PRICES[pool]:,} 金幣在 {channel.mention} 張貼懸賞，並自動報名。'
                elif action.startswith('drink:'):
                    package_id = action.split(':', 1)[1]
                    message, _ = await self.cog.tavern.buy_round(interaction, package_id)
                    package = DRINK_PACKAGES[package_id]
                    notice = f'已花費 {package.price:,} 金幣請客：{message.jump_url}'
                else:
                    raise CharacterError('無效的酒館操作。')
            except CharacterError as exc:
                notice = str(exc)
            except discord.HTTPException:
                notice = '酒館公告發布失敗，沒有成功的消費已退還。'
            self.rebuild()
            await self.origin.edit_original_response(embed=self.embed(notice), view=self)

    async def on_timeout(self):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop()
            try:
                await self.origin.edit_original_response(content='酒館面板已逾時，請使用 /酒館 重新進入。', view=None)
            except discord.HTTPException:
                pass
