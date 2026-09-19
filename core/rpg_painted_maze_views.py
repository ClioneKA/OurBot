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
                complete = all(str(uid) in room['final_vote']['votes'] for uid in room['members'])
                if complete:
                    room = self.service.repo.resolve_final_vote(self.room_id)
                    if room['status'] == 'retreated':
                        room = self.service.recover_rewards(room['id'])
                        self.service.cog.divinations.clear_raid(room['id'])
                await self.service._refresh(room)
                if room['status'] == 'retreated':
                    await self.service._archive(room)
            await interaction.followup.send(
                ('全員已投票，已進入尾王戰前休息點。'
                 if complete and room['final_vote']['result'] == 'enter' else
                 '全員已投票，隊伍已撤退。'
                 if complete else
                 '已登記去留選擇；全員投完後立即結算，截止前可修改。'),
                ephemeral=True)
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
