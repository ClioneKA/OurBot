import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple


_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
_LATIN_TERM_RE = re.compile(r"[a-z0-9][a-z0-9._/+%-]*", re.IGNORECASE)
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_QUESTION_HINT_RE = re.compile(
    r"(?:怎麼|怎么|如何|哪裡|哪里|什麼|什么|多少|為什麼|为什么|能否|可以|"
    r"規則|规则|介紹|介绍|說明|说明|教學|教学|攻略|用途|效果|取得|獲得|获得|"
    r"解鎖|解锁|升級|升级|推薦|推荐|要|嗎|吗|呢|？|\?)",
    re.IGNORECASE,
)
_EXPLICIT_GAME_RE = re.compile(r"(?:安安大冒險|安安大冒险|大冒險|大冒险|/冒險|/冒险)")
_ITEM_SOURCE_HINT_RE = re.compile(
    r"(?:來源|来源|出處|出处|掉落|誰掉|谁掉|哪(?:裡|里)(?:拿|買|买|出|掉|取得|獲得|获得)|"
    r"怎麼(?:拿|取得|獲得|获得)|怎么(?:拿|取得|获得)|如何(?:拿|取得|獲得|获得)|"
    r"從哪(?:裡|里)(?:拿|取得|獲得|获得)|哪(?:隻|只).{0,8}掉)",
    re.IGNORECASE,
)
_ITEM_INFO_HINT_RE = re.compile(
    r"(?:是什麼|是什么|有什麼用|有什么用|能做什麼|能做什么|作用|用途|效果)",
    re.IGNORECASE,
)
_GAME_TERMS = (
    "冒險", "冒险", "角色", "職業", "职业", "轉職", "转职", "民兵", "裝甲步兵",
    "弓兵", "騎士", "骑士", "僧侶", "僧侣", "能力值", "裝備", "装备", "武器",
    "套裝", "套装", "飾品", "饰品", "背包", "倉庫", "仓库", "討伐", "讨伐",
    "魔物", "怪物", "總力戰", "总力战", "繪境", "绘境", "迷宮", "迷宫", "魔女",
    "釣魚", "钓鱼", "農耕", "农耕", "煉金", "炼金", "人偶", "遠征", "远征",
    "酒館", "酒馆", "料理", "占卜", "裁縫", "裁缝", "刺繡", "刺绣", "染色",
    "技能", "被動", "被动", "冷卻", "冷却", "經驗", "经验", "金幣", "金币",
    "等級", "等级", "生命力", "力氣", "力气", "耐力", "靈巧", "灵巧", "信仰",
    "血翼", "史萊姆", "史莱姆", "哥布林", "鐵殼魔像", "铁壳魔像", "妖樹", "妖树",
)
_IGNORED_TERMS = {
    "安安", "請問", "请问", "一下", "告訴", "告诉", "我想", "想問", "想问",
    "怎麼", "怎么", "如何", "什麼", "什么", "可以", "多少", "為什", "为什么",
}


@dataclass(frozen=True)
class KnowledgePassage:
    heading: str
    text: str


def _terms(text: str) -> set[str]:
    normalized = text.lower()
    result = set(_LATIN_TERM_RE.findall(normalized))
    for run in _CJK_RUN_RE.findall(normalized):
        if 2 <= len(run) <= 8:
            result.add(run)
        for size in (2, 3):
            result.update(run[index:index + size] for index in range(len(run) - size + 1))
    return result - _IGNORED_TERMS


def _chunk_section(heading: str, lines: Sequence[str], max_chars: int) -> Iterable[KnowledgePassage]:
    paragraphs = "\n".join(lines).strip().split("\n\n")
    current: List[str] = []
    current_size = 0
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if current and current_size + len(paragraph) + 2 > max_chars:
            yield KnowledgePassage(heading, "\n\n".join(current))
            current = []
            current_size = 0
        if len(paragraph) <= max_chars:
            current.append(paragraph)
            current_size += len(paragraph) + 2
            continue
        for start in range(0, len(paragraph), max_chars):
            if current:
                yield KnowledgePassage(heading, "\n\n".join(current))
                current = []
                current_size = 0
            yield KnowledgePassage(heading, paragraph[start:start + max_chars])
    if current:
        yield KnowledgePassage(heading, "\n\n".join(current))


class RPGKnowledgeBase:
    """Small local retriever for the player-facing RPG reference."""

    def __init__(
        self,
        passages: Sequence[KnowledgePassage],
        item_passages: Mapping[str, KnowledgePassage] | None = None,
    ):
        self.passages = tuple(passages)
        self.item_passages = dict(item_passages or {})
        self._passage_terms = tuple(_terms(f"{item.heading}\n{item.text}") for item in passages)
        document_frequency = Counter(
            term for passage_terms in self._passage_terms for term in passage_terms
        )
        count = max(1, len(self.passages))
        self._idf = {
            term: math.log((count + 1) / (frequency + 1)) + 1
            for term, frequency in document_frequency.items()
        }

    @classmethod
    def from_markdown(cls, path: Path, max_chunk_chars: int = 2400) -> "RPGKnowledgeBase":
        text = path.read_text(encoding="utf-8")
        passages: List[KnowledgePassage] = []
        heading_stack: List[Tuple[int, str]] = []
        current_heading = path.stem
        current_lines: List[str] = []

        def flush() -> None:
            if current_lines:
                passages.extend(_chunk_section(current_heading, current_lines, max_chunk_chars))

        for line in text.splitlines():
            match = _HEADING_RE.match(line)
            if not match:
                current_lines.append(line)
                continue
            flush()
            current_lines = []
            level = len(match.group(1))
            title = match.group(2)
            heading_stack = [item for item in heading_stack if item[0] < level]
            heading_stack.append((level, title))
            current_heading = " > ".join(item[1] for item in heading_stack)
        flush()
        return cls(passages)

    @classmethod
    def from_project(cls, project_root: Path) -> "RPGKnowledgeBase":
        knowledge = cls.from_markdown(project_root / "config" / "RPG.md")
        knowledge.item_passages = _build_item_passages()
        return knowledge

    def _matching_items(self, query: str) -> Tuple[KnowledgePassage, ...]:
        matches = [
            (name, passage) for name, passage in self.item_passages.items()
            if name in query
        ]
        matches.sort(key=lambda item: (-len(item[0]), item[0]))
        return tuple(passage for _, passage in matches[:3])

    @staticmethod
    def is_game_question(query: str) -> bool:
        if _EXPLICIT_GAME_RE.search(query):
            return True
        if _ITEM_SOURCE_HINT_RE.search(query):
            return True
        if _ITEM_INFO_HINT_RE.search(query):
            return True
        return _QUESTION_HINT_RE.search(query) is not None and any(
            term in query for term in _GAME_TERMS
        )

    def search(
        self, query: str, *, limit: int = 3, max_total_chars: int = 6000
    ) -> Tuple[KnowledgePassage, ...]:
        item_matches = self._matching_items(query)
        if item_matches:
            remaining = max(0, limit - len(item_matches))
            general = self._search_rules(query, limit=remaining, max_total_chars=max_total_chars)
            seen = {(item.heading, item.text) for item in item_matches}
            matched_names = tuple(
                name for name in self.item_passages if name in query
            )
            return item_matches + tuple(
                item for item in general
                if (item.heading, item.text) not in seen
                and any(name in f"{item.heading}\n{item.text}" for name in matched_names)
            )[:remaining]
        return self._search_rules(query, limit=limit, max_total_chars=max_total_chars)

    def _search_rules(
        self, query: str, *, limit: int, max_total_chars: int
    ) -> Tuple[KnowledgePassage, ...]:
        if limit <= 0:
            return ()
        if not self.passages or not self.is_game_question(query):
            return ()
        explicit_game_question = _EXPLICIT_GAME_RE.search(query) is not None
        focused_query = _EXPLICIT_GAME_RE.sub("", query)
        focused_query = _ITEM_SOURCE_HINT_RE.sub("", focused_query)
        focused_query = _ITEM_INFO_HINT_RE.sub("", focused_query)
        query_terms = _terms(focused_query)
        domain_terms = {
            term.lower() for term in _GAME_TERMS if term.lower() in focused_query.lower()
        }
        if not query_terms and explicit_game_question:
            return (self.passages[0],)
        scored = []
        for index, (passage, passage_terms) in enumerate(
            zip(self.passages, self._passage_terms)
        ):
            shared = query_terms & passage_terms
            if not shared:
                continue
            heading_terms = _terms(passage.heading)
            score = sum(
                self._idf.get(term, 1.0) * (4 if term in heading_terms else 1)
                for term in shared
            )
            for term in domain_terms:
                if term in passage.heading.lower():
                    score += 80
                elif term in passage.text.lower():
                    score += 20
            scored.append((score, index, passage))
        if not scored:
            return (self.passages[0],) if explicit_game_question else ()

        chosen: List[KnowledgePassage] = []
        total_chars = 0
        for _, _, passage in sorted(scored, key=lambda item: (-item[0], item[1])):
            passage_size = len(passage.heading) + len(passage.text)
            if chosen and total_chars + passage_size > max_total_chars:
                continue
            chosen.append(passage)
            total_chars += passage_size
            if len(chosen) >= limit:
                break
        return tuple(chosen)

    def context(self, query: str) -> str:
        passages = self.search(query)
        if not passages:
            return ""
        return "\n\n".join(
            f"### {passage.heading}\n{passage.text}" for passage in passages
        )


def _build_item_passages() -> Dict[str, KnowledgePassage]:
    """Build item facts from the same runtime tables used by the game."""
    # These imports are intentionally lazy. Several feature modules register
    # their item definitions when imported, and AI startup should see the full
    # catalog regardless of cog load order.
    from core import rpg_witch_rest  # noqa: F401
    from core.rpg_character import ITEMS, LIFESTYLE_ACCESSORY_PRICE, LIFESTYLE_ACCESSORY_RECIPES
    from core.rpg_farming import PLANTS, STAR_FIBER_CHANCES, STAR_FIBER_ID
    from core.rpg_fishing import GLIMMER_PEARL_CHANCE_PER_BASE_CATCH, GLIMMER_PEARL_ID, RECIPES, SPOTS
    from core.rpg_raid_store import CHANCE_DROPS, DROP_TABLES, FIXED_DROPS

    sources: Dict[str, List[str]] = {item_id: [] for item_id in ITEMS}
    for monster, item_ids in DROP_TABLES.items():
        for item_id in item_ids:
            sources.setdefault(item_id, []).append(f"討伐「{monster}」的裝備掉落池")
    for monster, item_id in FIXED_DROPS.items():
        sources.setdefault(item_id, []).append(f"擊敗「{monster}」後由全隊隨機一人取得")
    for monster, rule in CHANCE_DROPS.items():
        chance = float(rule.get("chance", 0)) * 100
        for item_id in rule.get("pool", ()):
            sources.setdefault(item_id, []).append(
                f"擊敗「{monster}」時，每次抽選有 {chance:g}% 機率取得"
            )

    for spot in SPOTS.values():
        for item_id, weight in spot.loot:
            sources.setdefault(item_id, []).append(
                f"在「{spot.name}」釣魚的一般掉落（基礎權重 {weight:g}%）"
            )
    for rod_id, materials in RECIPES.items():
        material_names = "、".join(ITEMS[item_id].name for item_id in materials)
        sources.setdefault(rod_id, []).append(f"使用 {material_names} 製作")
    sources.setdefault(GLIMMER_PEARL_ID, []).append(
        f"釣魚 Lv.60 後在「魔女島海灣」收竿，每次基礎捕獲有 "
        f"{GLIMMER_PEARL_CHANCE_PER_BASE_CATCH * 100:g}% 機率額外取得"
    )

    for plant in PLANTS.values():
        sources.setdefault(plant.item_id, []).append(
            f"農耕 Lv.{plant.level} 解鎖種植「{plant.name}」，成熟後收成"
        )
    fiber_chances = sorted(set(int(chance * 100) for chance in STAR_FIBER_CHANCES.values()))
    sources.setdefault(STAR_FIBER_ID, []).append(
        "農耕 Lv.60 後收成指定高階作物時額外取得；依作物時長機率為 "
        + "／".join(f"{chance}%" for chance in fiber_chances)
    )

    for _, (product_id, material_id, amount) in LIFESTYLE_ACCESSORY_RECIPES.items():
        sources.setdefault(product_id, []).append(
            f"在漢娜的裁縫所使用 {ITEMS[material_id].name} ×{amount} 與 "
            f"{LIFESTYLE_ACCESSORY_PRICE:,} 金幣製作"
        )

    witch_rewards = {
        "witch_rest:fragment": "魔女安息儀式勝利後的隨機個人獎勵",
        "witch_rest:dust": "魔女安息儀式勝利後的隨機個人獎勵",
        "witch_rest:crystal_shard": "魔女化 100% 以上的魔女安息儀式勝利獎勵池",
    }
    for witch_id, witch in rpg_witch_rest.WITCHES.items():
        witch_rewards[f"witch_rest:{witch_id}:core"] = (
            f"挑戰「{witch.name}」的魔女安息儀式勝利後隨機取得"
        )
    for item_id, source in witch_rewards.items():
        sources.setdefault(item_id, []).append(source)

    passages: Dict[str, KnowledgePassage] = {}
    for item_id, item in ITEMS.items():
        facts = [f"名稱：{item.name}", f"分類：{item.category or item.slot or '其他'}"]
        if item.job:
            facts.append(f"限定職業：{item.job}")
        if item.required_level:
            facts.append(f"需求等級：Lv.{item.required_level}")
        if item.price:
            facts.append(f"商店價格：{item.price:,} 金幣")
            sources.setdefault(item_id, []).append("從金幣商店購買")
        if item.description:
            facts.append(f"說明：{item.description}")
        item_sources = list(dict.fromkeys(sources.get(item_id, ())))
        facts.append("取得方式：" + ("；".join(item_sources) if item_sources else "目前資料未列出"))
        passages[item.name] = KnowledgePassage(
            f"道具資料 > {item.name}", "\n".join(facts)
        )
    return passages
