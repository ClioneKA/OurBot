"""Private controls for choosing the item shown on a public adventurer card."""
import asyncio

import discord

from core.rpg_character import CharacterError, inventory_entry_label, item_text
from core.rpg_menu import add_back, navigate


PAGE_SIZE = 24


class ShowcaseSelect(discord.ui.Select):
    def __init__(self, view, owned):
        current = view.cog.characters.showcase_reference(view.guild_id, view.owner.id)
        equipped_ids = set(view.cog.characters.snapshot(
            view.guild_id, view.owner.id)['equipped_instances'].values())
        options = [discord.SelectOption(
            label=inventory_entry_label(entry, equipped_ids),
            value=entry.reference,
            description=f'{entry.item.category}｜{item_text(entry.item)}'[:100],
            default=entry.reference == current) for entry in owned]
        super().__init__(placeholder='選擇要公開展示的物品', options=options, row=0)
        self.profile_view = view

    async def callback(self, interaction):
        await self.profile_view.handle(interaction, 'showcase', self.values[0])


class ProfileView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.index = 0
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    def button(self, label, action, row=1, disabled=False):
        button = discord.ui.Button(label=label, row=row, disabled=disabled)
        async def callback(interaction):
            await self.handle(interaction, action)
        button.callback = callback
        self.add_item(button)

    def rebuild(self):
        self.clear_items()
        owned = self.cog.characters.inventory_entries(self.guild_id, self.owner.id)
        self.pages = max(1, (len(owned) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.index = min(self.index, self.pages - 1)
        page = owned[self.index * PAGE_SIZE:(self.index + 1) * PAGE_SIZE]
        if page:
            self.add_item(ShowcaseSelect(self, page))
        self.button('上一頁', 'previous', disabled=self.index == 0)
        self.button('下一頁', 'next', disabled=self.index == self.pages - 1)
        self.button('取消展示', 'clear')
        add_back(self, 2)
        self.button('關閉', 'close', 2)

    def embed(self, notice=None):
        embed = self.cog.adventurer_embed(self.guild_id, self.owner)
        embed.title = '安安大冒險｜展示名片設定'
        embed.description = (embed.description or '') + '\n\n從背包選擇一件物品，公開名片便會展示它。'
        if self.pages > 1:
            embed.description += f'\n物品頁次：{self.index + 1}/{self.pages}'
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='設定只影響名片展示，不會裝備、消耗或鎖定物品。')
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請從自己的冒險面板設定展示名片。', ephemeral=True)
            return False
        return True

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('面板已關閉，請重新使用 /冒險。', ephemeral=True)
                return
            if action == 'home':
                await navigate(self, interaction, 'home')
                return
            if action == 'close':
                await interaction.response.edit_message(content='展示名片設定已關閉。', embed=None, view=None)
                self.closed = True
                self.stop()
                return
            notice = None
            try:
                if action == 'showcase':
                    item = self.cog.characters.item_for_reference(self.guild_id, self.owner.id, value)
                    self.cog.characters.set_showcase(self.guild_id, self.owner.id, value)
                    notice = f'現在展示：{item.name}'
                elif action == 'clear':
                    self.cog.characters.set_showcase(self.guild_id, self.owner.id, None)
                    notice = '已取消展示物品。'
                elif action == 'previous':
                    self.index = max(0, self.index - 1)
                elif action == 'next':
                    self.index = min(self.pages - 1, self.index + 1)
            except CharacterError as exc:
                notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(content=None, embed=self.embed(notice), view=self,
                                                    allowed_mentions=discord.AllowedMentions.none())

    async def on_timeout(self):
        self.closed = True
        self.stop()
        try:
            await self.origin.edit_original_response(view=None)
        except discord.HTTPException:
            pass
