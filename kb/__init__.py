"""kb —— RAG memory + ontology 知识库。

分层：
    textutil   文本规范化 / 分块 / 轻量向量化 token
    db         SQLite schema 与连接
    providers  可插拔 LLM / Embedding 后端（mock 离线版 / ARK / OpenAI 兼容）
    ontology   类型化知识图谱：类型、实体消解、多跳关系
    memory     长期记忆：去重、冲突替代、时间衰减
    extraction 从文本抽取实体 / 关系 / 记忆
    ingest     摄取管线
    retrieval  混合检索（向量 + 全文 + 图谱 + RRF）与答案生成
    cli        命令行入口

设计文档见 DESIGN.md，快速上手见 README.md。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
