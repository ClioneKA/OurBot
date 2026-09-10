"""Original request lines, informed by config/persona.txt and official profiles.

Source: https://prtimes.jp/main/html/rd/p/000000007.000066682.html
Anan writes in a sketchbook using wagahai; Hanna affects proud lady-like speech.
These are original tavern adaptations, not quotations from the game.
"""
import random


COMMISSION_LINES = {
    'annan': (
        '「吾輩正在構思巨作。{target}太吵，討伐的事就交給汝了。……別逞強。」',
        '「今日的敵役決定是{target}。汝去討伐，吾輩留守。此乃合理分工。」',
        '「吾輩今日已走到酒館，實屬壯舉。討伐{target}……就拜託汝了。」',
        '「{target}擋住了吾輩取材的路。請汝處理。才不是因為吾輩害怕。」',
        '「討伐{target}，交予汝。若敵不過便撤退，吾輩不缺悲劇結局。」',
        '「{target}就交給勇者。吾輩負責記錄汝的戰果。……記得回來讓吾輩寫完。」',
    ),
    'hanna': (
        '「本小姐今日需要{target} ×{quantity}。既然你有空，就勞煩你替我備齊吧。品相可得仔細挑選喔。」',
        '「哎呀，還缺{target} ×{quantity}呢。體面的餐桌怎能馬虎？這件差事，本小姐就交給你了。」',
        '「請替本小姐帶來{target} ×{quantity}。不必多買，恰到好處才叫講究，可不是捨不得花錢喔！」',
        '「本小姐正忙著裁縫，採買就勞煩你了。{target} ×{quantity}，請妥善裝好，可別壓壞了。」',
        '「呵呵，能替本小姐挑選食材，你的眼光想必不差。就帶{target} ×{quantity}來，讓我瞧瞧吧。」',
        '「{target} ×{quantity}，清單都寫在這裡了。……若有剩下的也別糟蹋。珍惜食材，本就是應有的修養。」',
    ),
}


def daily_dialogue(guild, day, npc):
    # A separate seed leaves monster, ingredient and quantity rolls unchanged.
    return random.Random(f'tavern-dialogue:{guild}:{day}:{npc}').choice(COMMISSION_LINES[npc])
