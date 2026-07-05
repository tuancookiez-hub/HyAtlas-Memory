"""
HY Memory Lite — Retrieval helper modules.

本包收敛 lite pipeline 读侧的通用工具，被 reader_legacy / reader_hybrid_tag /
reader_hybrid_v2 等共用。任何对召回/排序/弃权/演化链合成的改动都应在此实现，
避免 reader 实现类里散落重复代码。

模块分布:
- config:       读取环境变量、阈值/权重常量
- intent:       意图分类 + keyword 提取
- bm25:         BM25-lite 内存实现
- rrf:          Reciprocal Rank Fusion + 意图权重
- evolution:    演化链回溯 + evolved memory 合成（三个 reader 共享）
- tag_index:    per-user tag embedding 索引（write 侧惰性维护，reader2 读取）
- reconcile_retrieval: Reconcile hybrid 候选召回（向量池 + BM25）
"""
