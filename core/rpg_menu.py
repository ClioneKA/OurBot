"""Private adventure navigation, sharing one Discord message across pages."""
import asyncio

import discord

from core.rpg_help import HELP_TOPICS, guide_embed
from core.rpg_character import CharacterError, ITEMS, JOBS, item_level, item_sell_price, item_sellable, item_text


BACKPACK_CATEGORIES = ('全部', '裝備', '料理素材', '製作材料', '換金道具', '釣竿')


def add_help(view, row, topic, return_page):
    button = discord.ui.Button(label='玩法說明', row=row)

    async def callback(interaction):
        if not await view.interaction_check(interaction):
            return
        async with view.lock:
            if view.closed or view.is_finished():
                await interaction.response.send_message('面板已關閉，請重新使用 /冒險。', ephemeral=True)
                return
            await navigate(view, interaction, 'help', help_topic=topic, help_return=return_page)

    button.callback = callback
    view.add_item(button)


def add_back(view, row):
    button = discord.ui.Button(label='返回主選單', row=row)
    async def callback(interaction):
        await view.handle(interaction, 'home')
    button.callback = callback
    view.add_item(button)


async def navigate(view, interaction, page='home', *, help_topic='intro', help_return=None):
    if page in ('give', 'sell'):
        from core.rpg_trade_view import TradeView
        next_view = TradeView(view.cog, view.origin, page)
    elif page == 'equipment':
        from core.rpg_equipment_view import EquipmentView
        next_view = EquipmentView(view.cog, view.origin)
    elif page == 'skills':
        from core.rpg_skill_view import SkillView
        next_view = SkillView(view.cog, view.origin)
    elif page == 'loadouts':
        from core.rpg_loadout_view import LoadoutView
        next_view = LoadoutView(view.cog, view.origin)
    elif page == 'shop':
        from core.rpg_shop_view import ShopView
        next_view = ShopView(view.cog, view.origin)
    elif page == 'fishing':
        from core.rpg_fishing_view import FishingView
        next_view = FishingView(view.cog, view.origin)
    elif page == 'farming':
        from core.rpg_farming_view import FarmingView
        next_view = FarmingView(view.cog, view.origin)
    elif page == 'expedition':
        from core.rpg_expedition_view import ExpeditionView
        next_view = ExpeditionView(view.cog, view.origin)
    elif page == 'provisions':
        from core.rpg_provision_view import ProvisionView
        next_view = ProvisionView(view.cog, view.origin)
    elif page == 'use_items':
        from core.rpg_item_use_view import ItemUseView
        next_view = ItemUseView(view.cog, view.origin)
    elif page == 'divination':
        from core.rpg_divination_view import DivinationView
        next_view = DivinationView(view.cog, view.origin)
    elif page == 'tavern':
        from core.rpg_tavern import TavernView
        next_view = TavernView(view.cog, view.origin)
    elif page == 'tailor':
        from core.rpg_tailor_view import TailorView
        next_view = TailorView(view.cog, view.origin)
    elif page == 'crystals':
        from core.rpg_crystal_view import CrystalTailorView
        next_view = CrystalTailorView(view.cog, view.origin)
    elif page == 'profile':
        from core.rpg_profile_view import ProfileView
        next_view = ProfileView(view.cog, view.origin)
    else:
        next_view = AdventureView(view.cog, view.origin, page,
                                  help_topic=help_topic, help_return=help_return)
    try:
        await interaction.response.edit_message(content=None, embed=next_view.embed(), view=next_view,
                                                allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        next_view.stop()
        raise
    view.cog.menu_views.add(next_view)
    view.closed = True
    view.stop()


class AdventureView(discord.ui.View):
    def __init__(self, cog, interaction, page='home', *, help_topic='intro', help_return=None):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.page, self.index, self.selected_job = page, 0, None
        self.help_topic = help_topic if help_topic in HELP_TOPICS else 'intro'
        self.help_return = help_return
        self.category = '全部'
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    def button(self, label, action, row=0, disabled=False):
        button = discord.ui.Button(label=label, row=row, disabled=disabled)
        async def callback(interaction):
            await self.handle(interaction, action)
        button.callback = callback
        self.add_item(button)

    def rebuild(self):
        self.clear_items()
        if self.page == 'home':
            for i, (label, action) in enumerate((('裝備／能力', 'equipment'), ('技能', 'skills'),
                ('出戰配置', 'loadouts'), ('背包', 'backpack'), ('商店', 'shop'), ('轉職', 'jobs'), ('生活', 'life'),
                ('移動', 'travel'), ('展示名片', 'profile'), ('說明', 'help'))):
                self.button(label, action, i // 3)
        elif self.page == 'help':
            from core.rpg_equipment_view import PanelSelect
            self.add_item(PanelSelect('help_topic', row=0, placeholder='選擇說明主題', options=[
                discord.SelectOption(label=label, value=key, description=summary,
                                     default=key == self.help_topic)
                for key, (label, summary) in HELP_TOPICS.items()]))
            if self.help_return:
                self.button('返回原功能', self.help_return, 1)
        elif self.page == 'life':
            self.button('釣魚', 'fishing', 0)
            self.button('農耕', 'farming', 0)
            self.button('遠征', 'expedition', 0)
        elif self.page == 'travel':
            self.button('瑪格的占卜室', 'divination', 0)
            self.button('冒險者酒館', 'tavern', 0)
            self.button('漢娜的裁縫所', 'tailor', 0)
        elif self.page == 'jobs':
            from core.rpg_equipment_view import PanelSelect
            state = self.cog.characters.snapshot(self.guild_id, self.owner.id)
            self.add_item(PanelSelect('job', row=0, placeholder='選擇職業', options=[
                discord.SelectOption(label=job, value=job, default=job == self.selected_job) for job in JOBS]))
            self.button('確認轉職', 'change_job', 1, state['level'] < 10 or not self.selected_job)
            self.button('重新領取補給', 'claim_supplies', 1)
        elif self.page == 'backpack':
            from core.rpg_equipment_view import PanelSelect
            all_owned = self.cog.characters.inventory_entries(self.guild_id, self.owner.id)
            owned = [entry for entry in all_owned
                     if self.category == '全部' or entry.item.category == self.category]
            self.pages = max(1, (len(owned) + 9) // 10)
            self.index = min(self.index, self.pages - 1)
            self.add_item(PanelSelect('category', row=0, placeholder='選擇背包分類', options=[
                discord.SelectOption(label=category, value=category, default=category == self.category)
                for category in BACKPACK_CATEGORIES]))
            self.button('上一頁', 'previous', 1, self.index == 0)
            self.button('下一頁', 'next', 1, self.index == self.pages - 1)
            self.button('給予物品', 'give', 1)
            self.button('使用道具', 'use_items', 1)
        topic = {'jobs': 'growth', 'backpack': 'combat', 'life': 'life', 'travel': 'life'}.get(self.page)
        if topic:
            add_help(self, 2, topic, self.page)
        if self.page != 'home':
            add_back(self, 2)
        self.button('重新整理', 'refresh', 2)
        self.button('關閉', 'close', 2)

    def embed(self, notice=None):
        if self.page == 'home':
            embed = self.cog.character_embed(self.guild_id, self.owner)
            embed.title = '安安大冒險｜' + embed.title
        elif self.page == 'backpack':
            chars = self.cog.characters
            all_owned = chars.inventory_entries(self.guild_id, self.owner.id)
            owned = [entry for entry in all_owned
                     if self.category == '全部' or entry.item.category == self.category]
            equipped = set(chars.snapshot(self.guild_id, self.owner.id)['equipped_instances'].values())
            lines = []
            for entry in owned[self.index * 10:(self.index + 1) * 10]:
                item = entry.item
                requirement = (f'{item.job} Lv.{item_level(item, self.cog.settings)}' if item.job
                               else '全職業通用' if item.category == '裝備' else item.category)
                sale = item_sell_price(item)
                identity = f' #{entry.instance_id}' if entry.instance_id else ''
                lines.append(f'**{item.name}{identity}** ×{entry.quantity} '
                             f'{"【已裝備】" if entry.instance_id in equipped else ""}\n'
                             f'{requirement}｜{item_text(item)}'
                             + (f'｜收購 {sale} 金幣／件' if item_sellable(item) else ''))
            embed = discord.Embed(title=f'安安大冒險｜背包 {self.index + 1}/{self.pages}・{self.category}',
                                  description='\n\n'.join(lines) or '目前沒有此類物品。', color=0x8B5CF6)
        elif self.page == 'life':
            embed = discord.Embed(title='安安大冒險｜生活', description=
                '透過生活技能取得料理與製作素材。\n\n'
                '**釣魚**：選擇釣場與時間開始釣魚，完成後收竿取得漁獲並提升獨立的釣魚等級。\n'
                '**遠征**：派遣 4／8／12 小時取得討伐之證、經驗與金幣；期間不能參加討伐，中斷沒有獎勵。\n'
                '**農耕**：中庭花圃自 Lv.1 開放，監獄菜園／廢棄溫室自 Lv.20／40 開放；可種植已解鎖植物，等級越高收成越多。\n'
                '魚、作物、水草與藥草都能帶到冒險者酒館，選擇五份食材製作公開料理。', color=0x38BDF8)
        elif self.page == 'travel':
            embed = discord.Embed(title='安安大冒險｜移動', description=
                '**瑪格的占卜室**\n'
                '支付金幣抽取一張塔羅牌，讓下一場討伐獲得特殊效果與額外經驗。\n'
                '每日不限次數，但每次占卜都會比前一次多花 300 金幣。\n\n'
                '**冒險者酒館**\n'
                '支付金幣張貼不發金幣的額外討伐懸賞，或在酒館專用頻道公開料理與請大家喝一杯。\n\n'
                '**漢娜的裁縫所**\n'
                '支付加工費替諾亞裝備染色，或在具有刺繡格的討伐飾品上縫製能力刺繡。', color=0x6D3A8D)
        elif self.page == 'jobs':
            state = self.cog.characters.snapshot(self.guild_id, self.owner.id)
            embed = discord.Embed(title='安安大冒險｜轉職', description=
                f'目前：Lv.{state["level"]}・{state["title"]}\nLv.10 起可免費轉職。\n'
                '裝甲步兵：均衡近戰｜騎士：承受傷害\n弓兵：靈巧輸出｜僧侶：治療支援\n\n'
                '確認後保留經驗、金幣與背包，重新計算能力，卸下目前裝備並穿上早期武器與套裝；飾品需重新穿戴。早期補給自動發放，每件限一次。', color=0x8B5CF6)
        else:
            embed = guide_embed(self.cog.settings, self.help_topic)
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='僅自己可操作；閒置 3 分鐘後關閉，使用 /冒險 重新開啟。')
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請使用 /冒險 開啟自己的安安大冒險。', ephemeral=True)
            return False
        return True

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('面板已關閉，請重新使用 /冒險。', ephemeral=True)
                return
            if action in ('home', 'equipment', 'skills', 'loadouts', 'backpack', 'shop', 'jobs', 'life', 'travel',
                          'divination', 'tavern', 'tailor', 'crystals', 'profile', 'fishing', 'farming', 'expedition', 'provisions', 'help', 'give', 'use_items'):
                await navigate(self, interaction, action)
                return
            if action == 'close':
                await interaction.response.edit_message(content='安安大冒險已關閉。', embed=None, view=None)
                self.closed = True
                self.stop()
                return
            notice = None
            try:
                if action == 'help_topic' and self.page == 'help' and value in HELP_TOPICS:
                    self.help_topic = value
                elif action == 'job' and self.page == 'jobs' and value in JOBS:
                    self.selected_job = value
                elif action == 'change_job' and self.page == 'jobs':
                    state = self.cog.characters.change_job(self.guild_id, self.owner.id, self.selected_job)
                    notice = f'你現在是 {state["title"]}！已穿上早期武器與套裝。'
                elif action == 'claim_supplies' and self.page == 'jobs':
                    granted = self.cog.characters.claim(self.guild_id, self.owner.id)
                    notice = ('已補發：' + '、'.join(ITEMS[key].name for key in granted)
                              if granted else '目前職業的免費補給都已在背包中。')
                elif action == 'previous' and self.page == 'backpack':
                    self.index = max(0, self.index - 1)
                elif action == 'next' and self.page == 'backpack':
                    self.index = min(self.pages - 1, self.index + 1)
                elif action == 'category' and self.page == 'backpack' and value in BACKPACK_CATEGORIES:
                    self.category, self.index = value, 0
            except CharacterError as exc:
                notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(notice), view=self)

    async def on_timeout(self):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop()
            try:
                await self.origin.edit_original_response(content='安安大冒險已逾時，請重新使用 /冒險。', view=None)
            except discord.HTTPException:
                pass
