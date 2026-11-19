"""抽取层：把一段文本变成 实体 / 关系 / 记忆。

两个实现，同一接口：
  * LlmExtractor  —— 真实模型，严格 JSON 输出 + 与 schema 对齐 + 置信度过滤；
  * RuleExtractor —— 零依赖规则版，保证离线可用，也是 LLM 失败时的兜底。

规则版不是摆设：它用"类型后缀词表 + 动词/介词关系模式 + 角色标签"做经典信息抽取，
在两个实体之间只隔着一个关系关键词时判定成立。准确度不如 LLM，但可解释、可调试。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .ontology import ENTITY_KINDS
from .textutil import (
    CJK,
    normalize_name,
    normalize_text,
    split_sentences,
)
from .util import clamp, truncate

# ---------------------------------------------------------------- 数据结构


@dataclass
class ExtractedEntity:
    name: str
    type_name: str = "概念"
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    confidence: float = 0.7


@dataclass
class ExtractedRelation:
    src: str
    src_type: str
    rel_type: str
    dst: str
    dst_type: str
    evidence: str = ""
    confidence: float = 0.7


@dataclass
class ExtractedMemory:
    kind: str
    content: str
    subject: str = ""
    predicate: str = ""
    object_text: str = ""
    importance: float = 0.5
    confidence: float = 0.7


@dataclass
class Extraction:
    entities: list[ExtractedEntity] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)
    memories: list[ExtractedMemory] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.entities or self.relations or self.memories)

    def summary(self) -> str:
        return (
            f"实体 {len(self.entities)} / 关系 {len(self.relations)}"
            f" / 记忆 {len(self.memories)}"
        )


class Extractor:
    def extract(
        self, text: str, *, title: str = "", known_entities: Sequence[str] = ()
    ) -> Extraction:
        raise NotImplementedError


# ---------------------------------------------------------------- 共享词表

SUFFIX_TYPES: dict[str, str] = {
    "项目": "项目", "系统": "系统", "服务": "系统", "平台": "系统",
    "数据库": "数据", "模块": "系统", "接口": "系统", "组件": "系统",
    "网关": "系统", "集群": "系统", "中台": "系统", "引擎": "系统", "队列": "系统",
    "中心": "系统", "链路": "系统", "管道": "系统", "体系": "概念", "机制": "流程",
    "团队": "组织", "部门": "组织", "公司": "组织", "小组": "组织",
    "流程": "流程", "规范": "文档", "文档": "文档", "报告": "文档",
    "手册": "文档", "方案": "文档", "指标": "数据", "数据集": "数据",
    "决策": "决策", "会议": "事件", "事故": "事件", "里程碑": "事件",
    "模型": "概念", "协议": "概念", "标准": "概念",
}

# 规则版用的小技术词表：文档里出现这些词时视为"系统"实体
TECH_GAZETTEER = {
    "postgresql": "PostgreSQL", "mysql": "MySQL", "tidb": "TiDB",
    "redis": "Redis", "kafka": "Kafka", "elasticsearch": "Elasticsearch",
    "kubernetes": "Kubernetes", "docker": "Docker", "clickhouse": "ClickHouse",
    "minio": "MinIO", "nginx": "Nginx", "rabbitmq": "RabbitMQ",
    "tensorflow": "TensorFlow", "pytorch": "PyTorch", "spark": "Spark",
}

STOP_ENTITY_WORDS = {"这个", "那个", "我们", "你们", "他们", "自己", "大家", "目前", "之后", "以前"}

# "X 负责" 这类模式里常见的非人名词
FALSE_PERSON_PREFIX = {
    "项目", "团队", "部门", "系统", "平台", "服务", "本次", "目前", "后续",
    "每个", "这个", "那个", "他们", "我们", "你们", "自己", "有人", "专人",
    "产品", "技术", "业务", "研发", "测试", "运营", "数据", "设计", "需求",
    "质量", "安全", "运维", "架构", "最终", "整体", "相关", "负责",
}

# 常见姓氏：用于"X 负责 / 由 X 主导"这类弱模式里筛人名，避免把"产品""技术"当人
COMMON_SURNAMES = set(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田"
    "董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白"
    "邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
)

# 关系词前面的动词/介词：命中即从该词之后截断，得到名词短语
VERB_PREFIXES = (
    "使用了", "使用", "依赖于", "依赖", "基于", "调用了", "调用", "采用了", "采用",
    "通过", "属于", "负责", "包含了", "包含", "包括了", "包括", "替代了", "替代",
    "迁移到", "迁移至", "升级到", "升级为", "提供了", "提供", "产出了", "产出",
    "评估了", "评估", "放弃了", "放弃", "决定了", "决定", "改用了", "改用",
    "改为", "选择了", "选择", "完成了", "完成", "进入了", "进入", "存放了", "存放",
    "沉淀成", "沉淀", "安排了", "安排", "建立了", "建立", "建设", "实现了", "实现",
    "推进了", "推进", "支持", "以及", "和", "与", "及", "从", "将", "把", "由",
    "在", "对", "向", "给", "我们的", "我们", "该", "这", "那", "新",
)
# 名词短语内部的分界字符：左起最后一个之后的内容才算候选名
PHRASE_BREAKS = "的、和与及把被将是了对从向给并或而但有"
# 只用于"在句中任意位置出现即截断"的强动词，避免单字介词误伤名字
MID_VERBS = (
    "评估", "放弃", "决定", "改用", "改为", "选择", "使用", "依赖", "调用",
    "采用", "包含", "包括", "替代", "取代", "迁移", "升级", "完成", "进入",
    "存放", "沉淀", "安排", "建立", "建设", "实现", "推进", "支持", "负责",
    "主导", "牵头", "提供", "产出", "属于",
)
QUANTIFIERS = (
    "一个", "两个", "三个", "四个", "多个", "几个", "这个", "那个", "每个",
    "所有", "各个", "本次", "本", "该",
)
ACRONYM_SKIP = {"OK", "TODO", "NOTE", "FIXME", "NA", "II", "III", "IV", "AI", "UI", "ID"}
CANDIDATE_DELIMS = set("。，、；：！？（）()《》〈〉【】[]{}<>\"'“”‘’ ›» \n\t\r…—～~|/\\+-=*&^%$#@!?.,;:")

# 关系关键词 → (关系类型, 方向)。forward: A --rel--> B；reverse: B --rel--> A
# 只保留有明确语义的关系词；"是/为/和/与" 这类共现会被误判，不建边。
REL_PATTERNS: list[tuple[str, str, str]] = [
    ("的负责人是", "负责", "reverse"),
    ("负责人为", "负责", "reverse"),
    ("负责人是", "负责", "reverse"),
    ("负责", "负责", "forward"),
    ("主导", "负责", "forward"),
    ("牵头", "负责", "forward"),
    ("依赖于", "依赖", "forward"),
    ("依赖", "依赖", "forward"),
    ("depends on", "依赖", "forward"),
    ("基于", "使用", "forward"),
    ("使用", "使用", "forward"),
    ("采用", "使用", "forward"),
    ("调用", "使用", "forward"),
    ("uses", "使用", "forward"),
    ("属于", "属于", "forward"),
    ("隶属于", "属于", "forward"),
    ("包含", "包含", "forward"),
    ("包括", "包含", "forward"),
    ("替代了", "替代", "forward"),
    ("取代了", "替代", "forward"),
    ("替代", "替代", "forward"),
    ("迁移到", "迁移到", "forward"),
    ("迁移至", "迁移到", "forward"),
    ("升级为", "迁移到", "forward"),
    ("产出", "产出", "forward"),
    ("输出", "产出", "forward"),
    ("参与", "参与", "forward"),
    ("针对", "针对", "forward"),
    ("关于", "针对", "forward"),
]

# 角色标签 → 人物
ROLE_LABEL_RE = re.compile(
    rf"(?:负责人|owner|Owner|OWNER|主导人|牵头人|提出人|作者|撰写人)\s*[:：]\s*"
    rf"([{CJK}]{{2,4}}|[A-Za-z][A-Za-z.\-]{{0,20}})"
)
PAREN_ROLE_RE = re.compile(rf"([{CJK}]{{2,4}})\s*[（(]([^）)]{{1,16}})[）)]")
SELF_RE = re.compile(r"(?:我|本人|笔者)\s*(?:更|比较|特别)?\s*(喜欢|偏好|倾向于|习惯|讨厌|不喜欢|擅长|在做|负责)")
MEMORY_RULES: list[tuple[re.Pattern, str, str, float]] = [
    (re.compile(r"(?P<all>(?:我|本人)\s*(?:更|比较|特别)?\s*(?:喜欢|偏好|倾向于|习惯|讨厌|不喜欢)[^。！？\n]{1,60}[。！？]?)"), "preference", "偏好", 0.8),
    (re.compile(r"(?P<all>记住[：:\s]*[^。！？\n]{2,120}[。！？]?)"), "fact", "约定", 0.75),
    (re.compile(r"(?P<all>(?:我|本人)\s*(?:是|在|从事)[^。！？\n]{2,60}[。！？]?)"), "fact", "身份", 0.6),
    (re.compile(r"(?P<all>\bI\s+(?:prefer|like|always|never|work)[^.!?\n]{2,120}[.!?]?)", re.I), "preference", "偏好", 0.8),
]


def _trim_candidate(raw: str) -> str:
    """把 "使用统一登录" 修剪成 "统一登录"。

    中文没有分词器，这里用"动词前缀 + 名词短语分界 + 量词"三层剥离，
    把长句里紧贴后缀的修饰语去掉，尽量还原名词短语。
    """
    cand = raw.strip()
    if not cand:
        return ""
    # 1) 从最后一个短语分界字符之后取（"…的把团队" → "团队"）
    cut = max((cand.rfind(ch) for ch in PHRASE_BREAKS), default=-1)
    if cut >= 0:
        cand = cand[cut + 1 :]
        if not cand:
            return ""
    # 2a) 从最后一个句中断动词之后取（"团队曾评估自建消息" → "自建消息"）
    cut = 0
    for verb in MID_VERBS:
        idx = cand.rfind(verb)
        if idx >= 0 and idx + len(verb) > cut:
            cut = idx + len(verb)
    if 0 < cut < len(cand) - 1:
        cand = cand[cut:]
    # 2b) 剥掉动词 / 介词前缀
    changed = True
    while changed and cand:
        changed = False
        for prefix in sorted(VERB_PREFIXES, key=len, reverse=True):
            if cand.startswith(prefix) and len(cand) > len(prefix) + 1:
                cand = cand[len(prefix):]
                changed = True
                break
    # 3) 剥掉量词 / 指示词
    changed = True
    while changed and cand:
        changed = False
        for q in sorted(QUANTIFIERS, key=len, reverse=True):
            if cand.startswith(q) and len(cand) > len(q) + 1:
                cand = cand[len(q):]
                changed = True
                break
    # 4) 去掉尾部修饰
    for suffix in ("的", "着", "过", "等", "这些", "那些"):
        if cand.endswith(suffix) and len(cand) - len(suffix) >= 2:
            cand = cand[: -len(suffix)]
            break
    cand = cand.strip("　 \t-—·")
    if any(ch in cand for ch in "的是了和与及对把被将为从") or any(ch.isdigit() for ch in cand):
        return ""
    # 5) 名词短语一般不超过 8 个汉字，取最靠近后缀的部分
    if len(cand) > 8:
        cand = cand[-8:]
    return cand


def _left_context(text: str, end: int, width: int = 12) -> str:
    """取实体后缀左侧的候选片段，从最近的词边界开始。"""
    start = max(0, end - width)
    chunk = text[start:end]  # 保留换行，换行本身就是最强的边界
    for i in range(len(chunk) - 1, -1, -1):
        if chunk[i] in CANDIDATE_DELIMS:
            return chunk[i + 1 :].strip()
    return chunk.strip()


def _looks_like_person(name: str) -> bool:
    if not name or len(name) < 2:
        return False
    if name in FALSE_PERSON_PREFIX:
        return False
    return name[0] in COMMON_SURNAMES



# ---------------------------------------------------------------- 规则抽取器


class RuleExtractor(Extractor):
    def extract(
        self, text: str, *, title: str = "", known_entities: Sequence[str] = ()
    ) -> Extraction:
        text = normalize_text(text)
        out = Extraction()
        mentions: dict[str, ExtractedEntity] = {}

        def add_entity(name: str, type_name: str, *, description: str = "", conf: float = 0.7) -> None:
            name = name.strip()
            if len(name) < 2 or name in STOP_ENTITY_WORDS:
                return
            norm = normalize_name(name)
            if not norm:
                return
            if norm in mentions:
                mentions[norm].confidence = max(mentions[norm].confidence, conf)
                if description and not mentions[norm].description:
                    mentions[norm].description = description
                return
            mentions[norm] = ExtractedEntity(
                name=name, type_name=type_name, description=description, confidence=conf
            )

        # 1) 类型后缀词表
        for suffix, type_name in SUFFIX_TYPES.items():
            for m in re.finditer(re.escape(suffix), text):
                before = _left_context(text, m.start())
                cand = _trim_candidate(before)
                if len(cand) < 2:
                    continue
                add_entity(cand + suffix, type_name)

        # 2) 书名号 / 引号里的专名
        for m in re.finditer(r"《([^》\n]{2,30})》", text):
            add_entity(m.group(1), "文档", conf=0.75)

        # 3) 技术词表
        lowered = text.lower()
        for key, display in TECH_GAZETTEER.items():
            if re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", lowered):
                add_entity(display, "系统", conf=0.8)

        # 3.5) 英文缩写（JWT / ADR / API 之类），至少出现两次才算实体
        for token in sorted(set(re.findall(r"(?<![A-Za-z])([A-Z]{2,6})(?![A-Za-z])", text))):
            if token in ACRONYM_SKIP:
                continue
            if text.count(token) >= 2:
                add_entity(token, "概念", conf=0.6)

        # 4) 人物：显式角色标签 + 括号里的角色说明
        for m in ROLE_LABEL_RE.finditer(text):
            add_entity(m.group(1), "人物", conf=0.85)
        for m in PAREN_ROLE_RE.finditer(text):
            name, role = m.group(1), m.group(2)
            if any(k in role for k in ("工程", "架构", "经理", "总监", "负责", "开发", "设计", "产品", "测试", "运营", "数据", "算法")):
                add_entity(name, "人物", description=role, conf=0.75)
        for m in re.finditer(r"@([A-Za-z][A-Za-z0-9_\-]{1,19})", text):
            add_entity("@" + m.group(1), "人物", conf=0.6)
        # "林晓负责订单中心" / "由林晓主导"：无角色标签时的兜底
        for m in re.finditer(
            rf"([{CJK}]{{2,3}})(?:负责|主导|牵头|提出|拍板|决定)", text
        ):
            cand = m.group(1)
            if _looks_like_person(cand):
                add_entity(cand, "人物", conf=0.6)
        for m in re.finditer(
            rf"(?:由|请|让)([{CJK}]{{2,3}})(?:负责|主导|牵头|来)", text
        ):
            cand = m.group(1)
            if _looks_like_person(cand):
                add_entity(cand, "人物", conf=0.7)

        # 5) 已知识别名（跨文档指代）
        for known in known_entities:
            if known and known in text and normalize_name(known) not in mentions:
                add_entity(known, "概念", conf=0.5)

        # 6) 关系：同一句内、两个实体之间只隔一个关系词
        name_to_norm = {e.name: norm for norm, e in mentions.items()}
        for sent in split_sentences(text):
            spans = []
            for norm, ent in mentions.items():
                for name in [ent.name, *ent.aliases]:
                    for m in re.finditer(re.escape(name), sent):
                        spans.append((m.start(), m.end(), norm))
            spans.sort(key=lambda s: (s[0], -s[1]))
            # 去掉被更长 span 覆盖的
            pruned: list[tuple[int, int, str]] = []
            for span in spans:
                if any(not (span[1] <= s[0] or span[0] >= s[1]) for s in pruned):
                    continue
                pruned.append(span)
            for i, a in enumerate(pruned):
                for b in pruned[i + 1 :]:
                    if a[2] == b[2]:
                        continue
                    gap = sent[a[1] : b[0]]
                    if not gap or len(gap) > 20:
                        continue
                    if "，" in gap or "," in gap or "；" in gap or ";" in gap:
                        continue  # 跨小句的关系主语可能已经换了
                    # 中间夹着第三个实体时通常不是 A 与 B 的关系；
                    # 但 "X 从 A 迁移到 B" 与 "负责 A 与 B" 是合法句式，允许穿过
                    conjunctive = any(ch in gap for ch in "与和及、")
                    if ("从" not in gap and "由" not in gap and not conjunctive) and any(
                        a[1] <= m[0] and m[1] <= b[0]
                        for m in pruned
                        if m is not a and m is not b and m[2] != a[2] and m[2] != b[2]
                    ):
                        continue
                    for kw, rel, direction in REL_PATTERNS:
                        if kw in gap:
                            ea, eb = mentions[a[2]], mentions[b[2]]
                            if direction == "reverse":
                                src, src_t, dst, dst_t = eb, eb.type_name, ea, ea.type_name
                            else:
                                src, src_t, dst, dst_t = ea, ea.type_name, eb, eb.type_name
                            out.relations.append(
                                ExtractedRelation(
                                    src=src.name, src_type=src_t, rel_type=rel,
                                    dst=dst.name, dst_type=dst_t,
                                    evidence=truncate(sent, 80), confidence=0.6,
                                )
                            )
                            break

        # 7) 记忆规则
        for pattern, kind, predicate, importance in MEMORY_RULES:
            for m in pattern.finditer(text):
                sentence = m.group("all").strip()
                if not sentence:
                    continue
                out.memories.append(
                    ExtractedMemory(
                        kind=kind, content=sentence,
                        subject="我", predicate=predicate,
                        object_text=_strip_self(sentence, predicate),
                        importance=importance, confidence=0.6,
                    )
                )

        out.entities = list(mentions.values())
        out.relations = _dedupe_relations(out.relations)
        out.memories = _dedupe_memories(out.memories)
        return out


def _strip_self(sentence: str, predicate: str) -> str:
    s = re.sub(r"^(?:我|本人)\s*(?:更|比较|特别)?\s*", "", sentence)
    s = re.sub(rf"^(?:{re.escape(predicate)}|喜欢|偏好|倾向于|习惯|讨厌|不喜欢)", "", s)
    s = re.sub(r"[。！？]$", "", s).strip()
    return s or sentence


def _dedupe_relations(rels: Iterable[ExtractedRelation]) -> list[ExtractedRelation]:
    seen: dict[tuple, ExtractedRelation] = {}
    for r in rels:
        key = (r.rel_type, normalize_name(r.src), normalize_name(r.dst))
        if key in seen:
            seen[key].confidence = max(seen[key].confidence, r.confidence)
            continue
        seen[key] = r
    return list(seen.values())


def _dedupe_memories(mems: Iterable[ExtractedMemory]) -> list[ExtractedMemory]:
    seen: dict[str, ExtractedMemory] = {}
    for m in mems:
        key = normalize_name(m.content)
        if key and key not in seen:
            seen[key] = m
    return list(seen.values())


# ---------------------------------------------------------------- LLM 抽取器

SYSTEM_PROMPT = """你是知识图谱信息抽取器。把输入文本转成严格的 JSON，不要输出任何解释。

只允许输出这个结构：
{
  "entities": [{"name": "", "type": "", "aliases": [], "description": "", "confidence": 0.0}],
  "relations": [{"src": "", "rel_type": "", "dst": "", "evidence": "", "confidence": 0.0}],
  "memories": [{"kind": "fact|preference|episode|task", "subject": "", "predicate": "", "object": "", "content": "", "importance": 0.0}]
}

硬性规则：
1. 只抽取文本里明确写出的信息；不确定就不要写。宁缺毋滥。
2. entities.name 用原文写法；同一实体的不同写法放进 aliases。
3. relations.src / dst 必须是 entities 里出现过的 name；rel_type 优先用给定关系类型，确实不合适再自拟（2-6 个汉字）。
4. relations.evidence 必须是原文中的连续片段，不超过 60 字。
5. confidence 反映证据强度，0.5~0.95；没有直接证据的关系不要输出。
6. memories 只记录"关于使用者或团队的长期事实/偏好/约定"，文档里的通用知识不要记成 memory。
7. 实体数量控制在 25 个以内，优先抽核心实体。
"""


class LlmExtractor(Extractor):
    def __init__(self, provider, config: dict) -> None:
        self.provider = provider
        self.cfg = (config or {}).get("extraction", {}) or {}
        self.min_conf = float(self.cfg.get("min_confidence", 0.4))
        self.entity_types = list(ENTITY_KINDS)

    def extract(
        self, text: str, *, title: str = "", known_entities: Sequence[str] = ()
    ) -> Extraction:
        schema_hint = (
            f"可用实体类型：{'、'.join(self.entity_types)}\n"
            f"可用关系类型：依赖、使用、负责、参与、属于、包含、替代、迁移到、产出、针对、相关"
        )
        known_hint = (
            "已知实体（可复用，避免重复造节点）：" + "、".join(list(known_entities)[:40])
            if known_entities
            else "已知实体：无"
        )
        user = f"{schema_hint}\n{known_hint}\n\n标题：{title or '（无）'}\n\n正文：\n{text}"
        raw = self.provider.chat.complete(SYSTEM_PROMPT, user, json_mode=True, max_tokens=2000)
        return self.parse(raw, title=title)

    def parse(self, raw: str, *, title: str = "") -> Extraction:
        out = Extraction()
        obj = _parse_json_object(raw)
        if obj is None:
            out.notes.append("LLM 输出无法解析为 JSON，已跳过本次抽取")
            return out
        for item in _as_list(obj.get("entities")):
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            conf = _conf(item.get("confidence"), 0.7)
            if conf < self.min_conf:
                continue
            out.entities.append(
                ExtractedEntity(
                    name=name,
                    type_name=str(item.get("type") or "概念").strip() or "概念",
                    aliases=[str(a).strip() for a in _as_list(item.get("aliases")) if str(a).strip()],
                    description=truncate(str(item.get("description") or ""), 200),
                    confidence=conf,
                )
            )
        names = {e.name for e in out.entities}
        type_of = {e.name: e.type_name for e in out.entities}
        for item in _as_list(obj.get("relations")):
            src = str(item.get("src") or "").strip()
            dst = str(item.get("dst") or "").strip()
            rel = str(item.get("rel_type") or "").strip()
            if not src or not dst or not rel or src == dst:
                continue
            if names and (src not in names or dst not in names):
                continue  # 端点必须先在 entities 里声明，避免悬空边
            conf = _conf(item.get("confidence"), 0.6)
            if conf < self.min_conf:
                continue
            out.relations.append(
                ExtractedRelation(
                    src=src, src_type=type_of.get(src, "概念"), rel_type=rel,
                    dst=dst, dst_type=type_of.get(dst, "概念"),
                    evidence=truncate(str(item.get("evidence") or ""), 120),
                    confidence=conf,
                )
            )
        for item in _as_list(obj.get("memories")):
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            kind = str(item.get("kind") or "fact").strip()
            if kind not in ("fact", "preference", "episode", "task"):
                kind = "fact"
            out.memories.append(
                ExtractedMemory(
                    kind=kind, content=content,
                    subject=str(item.get("subject") or "").strip(),
                    predicate=str(item.get("predicate") or "").strip(),
                    object_text=str(item.get("object") or "").strip() or content,
                    importance=clamp(_conf(item.get("importance"), 0.5), 0.0, 1.0),
                    confidence=_conf(item.get("confidence"), 0.6),
                )
            )
        out.relations = _dedupe_relations(out.relations)
        out.memories = _dedupe_memories(out.memories)
        return out


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict) or isinstance(v, str)]
    return []


def _conf(value: Any, default: float) -> float:
    try:
        return clamp(float(value), 0.0, 1.0)
    except (TypeError, ValueError):
        return default


def _parse_json_object(raw: str) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = text[start : end + 1]
    for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
        try:
            obj = json.loads(attempt)
        except ValueError:
            continue
        return obj if isinstance(obj, dict) else None
    return None


def build_extractor(config: dict, provider) -> Extractor:
    if provider is not None and not provider.is_mock:
        return LlmExtractor(provider, config)
    return RuleExtractor()
