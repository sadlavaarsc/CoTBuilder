"""CoTBuilder — CoT 数据生产管线（重构版）。

模块依赖方向（单向）：
    cli → batch → generator → {client, matcher, extractor, writer}
    client → ratelimit
    matcher → extractor
"""
