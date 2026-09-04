"""Private adventure navigation, sharing one Discord message across pages."""
import asyncio

import discord

from core.rpg_character import CharacterError, ITEMS, JOBS, item_text


def add_back(view, row):
    button = discord.ui.Button(label='返回主選單', row=row)
    async def callback(interaction):
        await view.handle(interaction, 'home')
    button.callback = callback
    view.add_item(button)


async def navigate(view, interaction, page='home'):
    if page == 'equipment':
        from core.rpg_equipment_view import EquipmentView
        next_view = EquipmentView(view.cog, view.origin)
    elif page == 'skills':
        from core.rpg_skill_view import SkillView
        next_view = SkillView(view.cog, view.origin)
    elif page == 'shop':
        from core.rpg_shop_view import ShopView
        next_view = ShopView(view.cog, view.origin)
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
                ('背包', 'backpack'), ('商店', 'shop'), ('轉職', 'jobs'), ('說明', 'help'))):
                self.button(label, action, i // 3)
        elif self.page == 'jobs':
            from core.rpg_equipment_view import PanelSelect
            state = self.cog.characters.snapshot(self.guild_id, self.owner.id)
            self.add_item(PanelSelect('job', row=0, placeholder='選擇職業', options=[
                discord.SelectOption(label=job, value=job, default=job == self.selected_job) for job in JOBS]))
            self.button('確認轉職', 'change_job', 1, state['level'] < 10 or not self.selected_job)
        elif self.page == 'backpack':
            owned = self.cog.characters.inventory(self.guild_id, self.owner.id)
            self.pages = max(1, (len(owned) + 9) // 10)
            self.index = min(self.index, self.pages - 1)
            self.button('上一頁', 'previous', 0, self.index == 0)
            self.button('下一頁', 'next', 0, self.index == self.pages - 1)
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
            owned = chars.inventory(self.guild_id, self.owner.id)
            counts = chars.inventory_counts(self.guild_id, self.owner.id)
            equipped = set(chars.snapshot(self.guild_id, self.owner.id)['equipped'].values())
            from core.rpg_character import stage_level
            lines = []
            for key in owned[self.index * 10:(self.index + 1) * 10]:
                item = ITEMS[key]
                requirement = f'{item.job} Lv.{stage_level(item.stage, self.cog.settings)}' if item.job else '全職業通用'
                lines.append(f'**{item.name}** ×{counts[key]} {"【已裝備】" if key in equipped else ""}\n{requirement}｜{item_text(item)}')
            embed = discord.Embed(title=f'安安大冒險｜背包 {self.index + 1}/{self.pages}',
                                  description='\n\n'.join(lines) or '背包是空的。', color=0x8B5CF6)
        elif self.page == 'jobs':
            state = self.cog.characters.snapshot(self.guild_id, self.owner.id)
            embed = discord.Embed(title='安安大冒險｜轉職', description=
                f'目前：Lv.{state["level"]}・{state["title"]}\nLv.10 起可免費轉職。\n'
                '裝甲步兵：均衡近戰｜騎士：承受傷害\n弓兵：靈巧輸出｜僧侶：治療支援\n\n'
                '確認後保留經驗、金幣與背包，重新計算能力，卸下目前裝備並穿上早期武器與套裝；飾品需重新穿戴。早期補給自動發放，每件限一次。', color=0x8B5CF6)
        else:
            s = self.cog.settings
            embed = discord.Embed(title='安安大冒險｜冒險指南', description=
                f'文字至少 {s.text_min_chars} 個非空白字元，每 {s.text_cooldown_seconds} 秒 {s.text_xp} XP，跨頻道共用冷卻。\n'
                f'一般語音至少 {s.voice_min_members} 位未靜音、未拒聽真人，每完整分鐘 {s.voice_xp_per_minute} XP。AFK、舞台與 Bot 不計入；未滿分鐘及離線時間不補發，不偵測實際說話。\n\n'
                f'採 RuneScape 標準經驗曲線，上限 Lv.120。Lv.10 可轉職，Lv.{s.regular_level}／{s.veteran_level}／{s.elite_level} 晉升，飾品格 1／2／3／4 格。\n\n'
                '初始裝備木棒；空手無法造成傷害。武器／套裝增加戰鬥數值，飾品增加基礎能力，同名限穿一件。進階裝備從商店購買；魔像專屬武器僅由討伐掉落。\n\n'
                '在頻道點擊報名，五分鐘後自動討伐；開戰前可調整裝備和技能。勝利獲得經驗、金幣與機率專屬物品（可重複），稀有史萊姆群報酬較高但不掉飾品；實際獎勵依公告。\n\n'
                '討伐頻道動態難度：成功後 ×1.1，失敗或回合上限後 ×0.9，範圍 0.5–3 倍；取消不調整，下一場套用。勝利經驗與金幣隨動態難度增減，掉落率及失敗經驗不變。\n\n'
                '使用 /排行榜 查看排名，各功能由主選單開啟。' + ('\n目前暫停聊天與語音經驗。' if not s.enabled else ''), color=0x8B5CF6)
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
            if action in ('home', 'equipment', 'skills', 'backpack', 'shop', 'jobs', 'help'):
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
                elif action == 'previous' and self.page == 'backpack':
                    self.index = max(0, self.index - 1)
                elif action == 'next' and self.page == 'backpack':
                    self.index = min(self.pages - 1, self.index + 1)
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
