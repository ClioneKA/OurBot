"""Public final-door vote after the last rest point."""
import discord

from core.rpg_character import CharacterError


class FinalVoteView(discord.ui.View):
    def __init__(self, service, room_id):
        super().__init__(timeout=None)
        self.service, self.room_id = service, room_id

    async def vote(self, interaction, choice):
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.service.lock(self.room_id):
                room = self.service.repo.vote_final(self.room_id, interaction.user.id, choice)
                await self.service._refresh(room)
            await interaction.followup.send('已登記去留選擇；截止前可修改。', ephemeral=True)
        except CharacterError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    @discord.ui.button(label='挑戰尾王（失敗掉落減半）', style=discord.ButtonStyle.danger,
                       custom_id='painted_maze:final:enter')
    async def enter(self, interaction, button):
        await self.vote(interaction, 'enter')

    @discord.ui.button(label='撤退並保留全部掉落', style=discord.ButtonStyle.success,
                       custom_id='painted_maze:final:retreat')
    async def retreat(self, interaction, button):
        await self.vote(interaction, 'retreat')
