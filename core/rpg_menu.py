"""Private adventure navigation, sharing one Discord message across pages."""
import asyncio

import discord

from core.rpg_character import CharacterError, ITEMS, JOBS, item_level, item_sell_price, item_sellable, item_text


BACKPACK_CATEGORIES = ('全部', '裝備', '料理素材', '製作材料', '換金道具', '釣竿')


def add_back(view, row):
    button = discord.ui.Button(label='返回主選單', row=row)
    async def callback(interaction):
        await view.handle(interaction, 'home')
    button.callback = callback
    view.add_item(button)


async def navigate(view, interaction, page='home'):
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
        next_view = AdventureView(view.cog, view.origin, page)
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
    def __init__(self, cog, interaction, page='home'):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.page, self.index, self.selected_job = page, 0, None
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
        elif self.page == 'life':
            self.button('釣魚', 'fishing', 0)
            self.button('農耕', 'farming', 0)
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
            s = self.cog.settings
            embed = discord.Embed(title='安安大冒險｜冒險指南', description=
                f'每日聊天基礎上限：文字 {s.text_daily_xp_limit:,} XP、語音 {s.voice_daily_xp_limit:,} XP，分開計算，台灣時間 00:00 重置；討伐經驗不計入。\n'
                '聊天獎勵與上限乘上「當級升級需求 ÷ Lv.10 升級需求」的三次方根，最低 1 倍，小數捨去；依目前等級計算，滿級沿用 Lv.119 倍率。實際數額見角色能力頁。\n\n'
                f'文字至少 {s.text_min_chars} 個非空白字元，每 {s.text_cooldown_seconds} 秒基礎 {s.text_xp} XP，跨頻道共用冷卻。\n'
                f'一般語音至少 {s.voice_min_members} 位未靜音、未拒聽真人，每完整分鐘基礎 {s.voice_xp_per_minute} XP。AFK、舞台與 Bot 不計入；未滿分鐘及離線時間不補發，不偵測實際說話。\n\n'
                f'採 RuneScape 標準經驗曲線，上限 Lv.120。Lv.10 可轉職，Lv.{s.regular_level}／{s.veteran_level}／{s.elite_level} 晉升，轉職後飾品格 2／3／4／5 格（民兵 1 格）。\n\n'
                f'每職業固定三個主動技能格，Lv.{s.regular_level} 解鎖進階主動技能；Lv.{s.veteran_level} 解鎖一格三選一職業被動，可在技能面板自由更換。\n\n'
                '初始裝備木棒；空手無法造成傷害。武器／套裝增加戰鬥數值，飾品增加基礎能力，同名限穿一件。進階裝備從商店購買；魔像專屬武器、妖樹專屬套裝僅由討伐掉落。\n\n'
                '在頻道點擊報名，五分鐘後自動討伐；開戰前可調整裝備和技能。勝利獲得經驗、金幣與機率專屬物品（可重複），稀有史萊姆群報酬較高但不掉飾品；實際獎勵依公告。\n\n'
                '失敗或回合上限：以怪物結束時已削減 HP 比例發放勝利經驗與金幣，無條件捨去，無掉落；多隻怪物合計血量。\n\n'
                '集齊紅、黃、藍色噴漆罐後，可在背包組成噴漆罐套組並直接使用；中階討伐空閒時會消耗套組，在中階頻道召喚固定四階「城崎諾亞」並自動報名，且不重排正常中階討伐時間。諾亞裝備可在漢娜的裁縫所消耗單色噴漆並付費染色。\n\n'
                '討伐頻道動態難度：勝利緩慢增加，平手或戰敗依削減 HP 大幅下降，範圍 1–2.5 倍；HP／攻擊／防禦分別套用 100%／40%／10% 幅度。取消不調整，下一場套用；勝利經驗與金幣隨動態難度增加，掉落率不變。\n\n'
                '背包可依物品用途分類，並可給予同伺服器真人物品；商店收購一般裝備及生活物品。木棒與免費補給不可給予，但可用 0 金幣出售；釣竿不可給予或出售。穿戴中的那一件需先卸下。\n\n'
                '生活頁可選擇時間開始釣魚，也能在已解鎖農地種植；Lv.40 可前往監獄地下水路，並解鎖第三塊農地廢棄溫室。釣魚與農耕都可設定完成私訊。所有魚、作物、水草與藥草都能在酒館選取五份製成料理。\n\n'
                '移動頁可前往瑪格的占卜室；每天可不限次數支付逐次提高的金幣抽牌，為下一場討伐取得特殊效果與 10% 額外經驗。\n\n'
                '使用 /酒館 或從移動頁前往冒險者酒館，製作並分享料理、公開請最多 5／10／20 人喝一杯，或支付金幣張貼不發金幣且不影響正常排程的額外懸賞。料理與請客會發布到伺服器設定的酒館頻道。\n\n'
                '漢娜的裁縫所可消耗噴漆罐與 1,000 金幣替諾亞裝備染色；目前的討伐飾品各有一格刺繡格，可支付 500 金幣縫製一項基礎能力 +2 的刺繡，免費初始飾品無法刺繡。\n\n'
                '使用 /討伐通知 分別領取一般、中階或高階出怪通知，也可一次操作全部。使用 /排行榜 查看排名，各功能由主選單開啟。' + ('\n目前暫停聊天與語音經驗。' if not s.enabled else ''), color=0x8B5CF6)
            embed.add_field(name='基礎能力效果（含飾品加成）', value=
                '生命力：每點最大 HP +10（騎士也用於攻擊）。\n'
                '力氣：所有職業的攻擊基礎，各職業倍率不同。\n'
                '耐力：每點防禦 +3。\n'
                '信仰：每點治療量 +3（僧侶也用於攻擊）。\n'
                '武器／套裝的 HP、攻擊、防禦與治療量直接加成另計。', inline=False)
            embed.add_field(name='靈巧、速度與行動順序', value=
                '命中值：基礎 95，另加武器提供的命中；不再由靈巧提高。\n'
                '閃避率：隨靈巧線性成長，Lv.120 弓兵約為 35%，上限 35%。\n'
                '暴擊率：隨靈巧以越後期越緩慢的曲線成長，Lv.1 為 10%、Lv.120 弓兵約為 95%，上限 100%。\n'
                '暴擊傷害：裝甲步兵 150%、騎士 125%、弓兵 175%、僧侶 125%；民兵 150%。暴擊傷害由每名角色各自保存、可獨立變動。速度由職業與裝備決定；帶有【準備】詞條的增益優先結算，其餘行動速度高者先，同值隨機。', inline=False)
            embed.add_field(name='各職業攻擊公式', value=
                '裝甲步兵：力氣 ×3｜弓兵：力氣 + 靈巧 ×1.5\n'
                '騎士：力氣 + 生命力 ×1.25｜僧侶：力氣 + 信仰 ×1.25\n'
                '民兵：力氣 ×2；小數無條件捨去。', inline=False)
            embed.add_field(name='能力如何影響戰鬥', value=
                '實際命中機率＝自身命中值－目標迴避值，最低 10%、沒有上限；同階武器對同階怪物約為 95%，達 100% 即必中。\n'
                '基礎傷害＝攻擊 × 技能倍率－防禦 × 職業係數；騎士 0.45、裝甲步兵 0.40、其餘與魔物 0.35，再計算武器穩定度、個人暴擊傷害與減傷，最低 1。\n'
                '最大 HP 會提高騎士衝鋒與重整旗鼓的效果；防禦提高護衛加成，治療量影響包紮與僧侶治療技能。', inline=False)
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
                          'divination', 'tavern', 'tailor', 'crystals', 'profile', 'fishing', 'farming', 'provisions', 'help', 'give', 'use_items'):
                await navigate(self, interaction, action)
                return
            if action == 'close':
                await interaction.response.edit_message(content='安安大冒險已關閉。', embed=None, view=None)
                self.closed = True
                self.stop()
                return
            notice = None
            try:
                if action == 'job' and self.page == 'jobs' and value in JOBS:
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
