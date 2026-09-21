"""Private adventure navigation, sharing one Discord message across pages."""
import asyncio

import discord

from core.rpg_help import HELP_TOPICS, guide_embed
from core.rpg_character import CharacterError, ITEMS, JOBS, item_display_name, item_level, item_sell_price, item_sellable, item_text


BACKPACK_CATEGORIES = ('全部', '裝備', '料理素材', '製作材料', '換金道具', '釣竿')

# Keep this below Discord's 25-option select limit. Ten shortcuts use two rows
# on the home view while leaving room for navigation and utilities.
FAVORITE_PAGES = {
    'character': ('角色', '裝備、技能、配置與轉職'),
    'equipment': ('裝備／能力', '穿戴裝備並查看能力'),
    'skills': ('技能', '設定技能與自動施放策略'),
    'loadouts': ('出戰配置', '保存或套用整套配置'),
    'jobs': ('轉職', '切換職業或領取補給'),
    'training': ('訓練假人', '測試自己的戰鬥表現'),
    'items': ('物品', '背包與商店入口'),
    'backpack': ('背包', '查看、給予或使用物品'),
    'shop': ('商店', '購買與出售裝備'),
    'use_items': ('使用道具', '使用背包中的消耗品'),
    'life': ('生活', '釣魚、農耕與煉金人偶'),
    'fishing': ('釣魚', '管理釣魚行程'),
    'farming': ('農耕', '管理田地與作物'),
    'alchemy': ('煉金人偶', '製作與管理煉金人偶'),
    'expedition': ('人偶遠征', '派遣人偶遠征'),
    'provisions': ('準備料理', '製作酒館料理'),
    'travel': ('冒險', '討伐與城鎮設施入口'),
    'raids': ('討伐', '建立特殊討伐房間'),
    'divination': ('瑪格的占卜室', '選擇持續六小時的混合塔羅命運'),
    'tavern': ('冒險者酒館', '料理、懸賞與請客'),
    'tailor': ('漢娜的裁縫所', '染色與刺繡加工'),
    'crystals': ('顏料結晶', '管理裁縫所顏料結晶'),
}
MAX_FAVORITES = 10
PARENT_PAGES = {
    'jobs': ('character', '返回角色'),
    'backpack': ('items', '返回物品'),
}
ALCHEMY_PAGE_LABELS = {
    'overview': '煉金人偶', 'body': '煉金人偶・素體', 'cores': '煉金人偶・思考核心',
    'gacha': '煉金人偶・技能石轉蛋', 'powder': '煉金人偶・粉塵兌換',
    'decompose': '煉金人偶・技能石分解', 'fuel': '煉金人偶・燃料',
    'automation': '煉金人偶・自動化', 'triggers': '煉金人偶・觸發條件',
    'stats': '煉金人偶・能力說明',
}
TAILOR_MODE_LABELS = {
    'dye': '裁縫所・裝備染色', 'embroidery': '裁縫所・飾品刺繡',
    'craft': '裁縫所・生活飾品', 'affinity': '裁縫所・漢娜好感度',
}
CRYSTAL_MODE_LABELS = {
    'inventory': '顏料結晶・一覽', 'socket': '顏料結晶・鑲嵌',
    'sell': '顏料結晶・出售', 'give': '顏料結晶・給予',
}


def favorite_label(route):
    if route in FAVORITE_PAGES:
        return FAVORITE_PAGES[route][0]
    base, separator, detail = route.partition(':')
    if not separator:
        return None
    if base == 'alchemy':
        return ALCHEMY_PAGE_LABELS.get(detail)
    if base == 'tailor':
        return TAILOR_MODE_LABELS.get(detail)
    if base == 'crystals':
        return CRYSTAL_MODE_LABELS.get(detail)
    if base == 'shop' and detail in ('gold', 'proof'):
        return '金幣商店' if detail == 'gold' else '討伐之證兌換'
    if base == 'fishing' and detail == 'records':
        return '釣魚・大魚圖鑑'
    if base == 'skills':
        if detail == 'basic':
            return '技能・普通攻擊目標'
        if detail == 'passive':
            return '技能・職業被動'
        if detail.startswith('replace-') and detail[8:] in ('1', '2', '3'):
            return f'技能・更換槽 {detail[8:]}'
        if detail.startswith('slot-') and detail[5:] in ('1', '2', '3'):
            return f'技能・策略槽 {detail[5:]}'
    if base == 'help' and detail in HELP_TOPICS:
        return f'說明・{HELP_TOPICS[detail][0]}'
    if base == 'backpack' and detail in BACKPACK_CATEGORIES:
        return f'背包・{detail}'
    return None


def navigation_label(route):
    return '首頁' if route == 'home' else favorite_label(route)


def canonical_favorite_routes(routes):
    legacy = {
        'skills': 'skills:slot-1', 'shop': 'shop:gold',
        'backpack': 'backpack:全部', 'alchemy': 'alchemy:overview',
        'tailor': 'tailor:dye', 'crystals': 'crystals:socket',
    }
    return tuple(dict.fromkeys(legacy.get(route, route) for route in routes))


def remember_current_page(view):
    route = getattr(view, 'current_route', None)
    if route:
        view.navigation_history = tuple(getattr(view, 'navigation_history', ())) + (route,)


def add_favorite_toggle(view, row, page):
    """Add an in-place favorite toggle to a player-owned menu page."""
    view.current_route = page
    store = getattr(view.cog, 'store', None)
    if store is None:
        store = getattr(getattr(view.cog, 'characters', None), 'store', None)
    if store is None:
        return
    saved = store.menu_favorites(view.guild_id, view.owner.id)
    favorites = canonical_favorite_routes(saved)
    if favorites != saved:
        store.set_menu_favorites(view.guild_id, view.owner.id, favorites)
    selected = page in favorites
    button = discord.ui.Button(
        label='★ 移除最愛' if selected else '☆ 加入最愛', row=row,
        style=discord.ButtonStyle.primary if selected else discord.ButtonStyle.secondary)

    async def callback(interaction):
        if interaction.guild_id != view.guild_id or interaction.user.id != view.owner.id:
            await interaction.response.send_message('請使用 /冒險 開啟自己的安安大冒險。', ephemeral=True)
            return
        async with view.lock:
            if getattr(view, 'closed', False) or view.is_finished():
                await interaction.response.send_message('面板已關閉，請重新使用 /冒險。', ephemeral=True)
                return
            current = list(canonical_favorite_routes(
                store.menu_favorites(view.guild_id, view.owner.id)))
            if page in current:
                current.remove(page)
                selected_now = False
            else:
                if len(current) >= MAX_FAVORITES:
                    await interaction.response.send_message(
                        f'最愛最多 {MAX_FAVORITES} 個；請先到其他頁面移除一個。', ephemeral=True)
                    return
                current.append(page)
                selected_now = True
            store.set_menu_favorites(view.guild_id, view.owner.id, current)
            button.label = '★ 移除最愛' if selected_now else '☆ 加入最愛'
            button.style = (discord.ButtonStyle.primary if selected_now
                            else discord.ButtonStyle.secondary)
            await interaction.response.edit_message(view=view)

    button.callback = callback
    view.add_item(button)


def add_help(view, row, topic, return_page):
    button = discord.ui.Button(label='玩法說明', row=row)

    async def callback(interaction):
        if not await view.interaction_check(interaction):
            return
        async with view.lock:
            if getattr(view, 'closed', False) or view.is_finished():
                await interaction.response.send_message('面板已關閉，請重新使用 /冒險。', ephemeral=True)
                return
            await navigate(view, interaction, 'help', help_topic=topic, help_return=return_page)

    button.callback = callback
    view.add_item(button)


def add_back(view, row, page='home', label='返回主選單'):
    history = tuple(getattr(view, 'navigation_history', ()))
    target = history[-1] if history else page
    target_label = navigation_label(target)
    button = discord.ui.Button(label=f'返回{target_label}' if target_label else label, row=row)
    button.adventure_back = True
    async def callback(interaction):
        if interaction.guild_id != view.guild_id or interaction.user.id != view.owner.id:
            await interaction.response.send_message('請使用 /冒險 開啟自己的安安大冒險。', ephemeral=True)
            return
        async with view.lock:
            if getattr(view, 'closed', False) or view.is_finished():
                await interaction.response.send_message('面板已關閉，請重新使用 /冒險。', ephemeral=True)
                return
            await navigate(view, interaction, target, _history=history[:-1])
    button.callback = callback
    view.add_item(button)


async def navigate(view, interaction, page='home', *, help_topic='intro', help_return=None,
                   return_page=None, _history=None):
    route = page
    page, _, detail = route.partition(':')
    source = getattr(view, 'current_route', getattr(view, 'page', 'home'))
    history = (tuple(getattr(view, 'navigation_history', ())) + (source,)
               if _history is None else tuple(_history))
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
    elif page == 'training':
        from core.rpg_training_view import TrainingView
        next_view = TrainingView(view.cog, view.origin)
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
        next_view = ItemUseView(view.cog, view.origin, return_page=return_page or 'backpack')
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
    elif page == 'raids':
        from core.rpg_raid_hub import RaidHubView
        next_view = RaidHubView(view.cog, view.origin)
    elif page == 'alchemy':
        from core.rpg_alchemy_view import AlchemyView
        next_view = AlchemyView(view.cog, view.origin)
    else:
        next_view = AdventureView(view.cog, view.origin, page,
                                  help_topic=detail or help_topic, help_return=help_return)
    if page == 'skills' and detail:
        if detail == 'basic':
            next_view.setting_basic = True
        elif detail == 'passive':
            next_view.setting_passive = True
        elif detail.startswith(('slot-', 'replace-')) and detail.rsplit('-', 1)[-1] in ('1', '2', '3'):
            next_view.slot = int(detail.rsplit('-', 1)[-1])
            next_view.choosing_skill = detail.startswith('replace-')
    elif page == 'shop' and detail in ('gold', 'proof'):
        next_view.currency = detail
    elif page == 'fishing' and detail == 'records':
        next_view.showing_records = True
    elif page == 'alchemy' and detail in ALCHEMY_PAGE_LABELS:
        next_view.page = detail
    elif page == 'tailor' and detail in TAILOR_MODE_LABELS:
        next_view.mode = detail
        next_view.option = 'red' if detail == 'dye' else 'heart'
    elif page == 'crystals' and detail in CRYSTAL_MODE_LABELS:
        next_view.mode = detail
    elif page == 'backpack' and detail in BACKPACK_CATEGORIES:
        next_view.category = detail
    next_view.navigation_history = history
    next_view.current_route = route
    next_view.rebuild()
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
        self.navigation_history = ()
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
            for i, (label, action) in enumerate((('角色', 'character'), ('物品', 'items'),
                                                 ('生活', 'life'), ('冒險', 'travel'),
                                                 ('說明', 'help'))):
                self.button(label, action, i // 3)
            for i, page in enumerate(self._favorites()):
                self.button('⭐ ' + favorite_label(page), page, 2 + i // 5)
        elif self.page == 'character':
            for i, (label, action) in enumerate((('裝備／能力', 'equipment'), ('技能', 'skills'),
                                                 ('出戰配置', 'loadouts'), ('轉職', 'jobs'),
                                                 ('訓練假人', 'training'))):
                self.button(label, action, i // 3)
        elif self.page == 'items':
            self.button('背包', 'backpack', 0)
            self.button('商店', 'shop', 0)
        elif self.page == 'help':
            from core.rpg_equipment_view import PanelSelect
            self.add_item(PanelSelect('help_topic', row=0, placeholder='選擇說明主題', options=[
                discord.SelectOption(label=label, value=key, description=summary,
                                     default=key == self.help_topic)
                for key, (label, summary) in HELP_TOPICS.items()]))
        elif self.page == 'life':
            self.button('釣魚', 'fishing', 0)
            self.button('農耕', 'farming', 0)
            self.button('煉金人偶', 'alchemy', 0)
        elif self.page == 'travel':
            self.button('討伐', 'raids', 0)
            self.button('瑪格的占卜室', 'divination', 0)
            self.button('冒險者酒館', 'tavern', 0)
            self.button('漢娜的裁縫所', 'tailor', 1)
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
        topic = {'character': 'growth', 'items': 'economy', 'jobs': 'growth',
                 'backpack': 'economy', 'life': 'gathering', 'travel': 'raids'}.get(self.page)
        if topic:
            add_help(self, 2, topic, self.page)
        if self.page != 'home':
            parent, label = PARENT_PAGES.get(self.page, ('home', '返回主選單'))
            add_back(self, 2, parent, label)
        utility_row = 4 if self.page == 'home' else 2
        favorite_route = (f'help:{self.help_topic}' if self.page == 'help' else
                          f'backpack:{self.category}' if self.page == 'backpack' else self.page)
        if favorite_label(favorite_route):
            add_favorite_toggle(self, utility_row, favorite_route)
        self.button('重新整理', 'refresh', utility_row)
        self.button('關閉', 'close', utility_row)

    def _favorites(self):
        store = getattr(self.cog, 'store', self.cog.characters.store)
        saved = store.menu_favorites(self.guild_id, self.owner.id)
        favorites = canonical_favorite_routes(saved)
        if favorites != saved:
            store.set_menu_favorites(self.guild_id, self.owner.id, favorites)
        return tuple(page for page in favorites if favorite_label(page))[:MAX_FAVORITES]

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
                identity = f'｜編號 #{entry.instance_id}' if entry.instance_id else ''
                lines.append(f'**{item_display_name(item)}** ×{entry.quantity} '
                             f'{"【已裝備】" if entry.instance_id in equipped else ""}\n'
                             f'{requirement}｜{item_text(item)}'
                             + (f'｜收購 {sale} 金幣／件' if item_sellable(item) else '') + identity)
            embed = discord.Embed(title=f'安安大冒險｜背包 {self.index + 1}/{self.pages}・{self.category}',
                                  description='\n\n'.join(lines) or '目前沒有此類物品。', color=0x8B5CF6)
        elif self.page == 'life':
            embed = discord.Embed(title='安安大冒險｜生活', description=
                '透過生活技能取得料理與製作素材。\n\n'
                '**釣魚**：選擇釣場與時間開始釣魚；Lv.80／100 可前往沉沒神殿潮池／星蝕外海。\n'
                '**農耕**：四塊既有田地可種植所有已解鎖植物；Lv.80 可為每塊田選擇豐收或研習專精。\n'
                '**煉金人偶**：製作素體、設定自動化，或派遣人偶遠征取得金幣與定向素體素材。\n'
                '魚、作物、水草與藥草都能帶到冒險者酒館，選擇五份食材製作公開料理。', color=0x38BDF8)
        elif self.page == 'character':
            embed = discord.Embed(title='安安大冒險｜角色', description=
                '管理裝備、技能與出戰配置，或進行轉職與傷害測試。\n'
                '公開名片與展示品請使用 `/冒險者` 設定。', color=0x8B5CF6)
        elif self.page == 'items':
            embed = discord.Embed(title='安安大冒險｜物品', description=
                '查看、使用或給予背包物品，並前往商店購買與出售裝備。', color=0xD97706)
        elif self.page == 'travel':
            embed = discord.Embed(title='安安大冒險｜冒險', description=
                '**討伐**\n建立魔女安息、魔女試煉、繪境迷宮或特殊召喚。\n\n'
                '**瑪格的占卜室**\n'
                '每日免費揭示三張塔羅牌並選擇一張，取得持續 6 小時的戰鬥、生活或特殊命運。\n'
                '再次揭牌依序花費 300、600、900……金幣。\n\n'
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
            if (action in ('home', 'character', 'items', 'equipment', 'skills', 'loadouts',
                          'training', 'backpack', 'shop', 'jobs', 'life', 'travel', 'raids',
                          'alchemy', 'divination', 'tavern', 'tailor', 'crystals', 'fishing',
                          'farming', 'expedition', 'provisions', 'help', 'give', 'use_items')
                    or favorite_label(action)):
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
                    if value != self.help_topic:
                        remember_current_page(self)
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
                    if value != self.category:
                        remember_current_page(self)
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
