"""运行配置：所有可调参数的唯一出处。

设计说明：
- frozen dataclass，运行期不可变，防止并发场景下配置被意外修改。
- 寿命（life）语义见 generator 模块：max_sample_attempts 只被 MISMATCH 消耗，
  network_max_attempts 只被网络类错误消耗，两本账互不挤占（审计报告 01 附录 B.3.4）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """CoTBuilder 全部运行参数。

    Attributes:
        api_key: 专家模型 API 密钥。
        api_endpoint: API 基础地址（不含 /chat/completions）。
        model: 专家模型名称。
        qpm_limit: 每分钟请求发起上限（含全部重试），匀速放行。
        max_concurrent: 在途 HTTP 请求硬上限。
        max_sample_attempts: 样本寿命——单样本最多进队尝试次数（仅 MISMATCH 消耗）。
        network_max_attempts: 网络寿命——单样本网络/限流类错误的最大重试次数。
        request_timeout: 单次 HTTP 请求超时（秒），需大于模型推理延迟上限。
        backoff_base: 退避基数（秒），第 n 次重试退避 min(base*2^n, cap)。
        backoff_cap: 退避上限（秒）。
        backoff_jitter: 退避抖动幅度，0.5 表示 ±50%，破坏多协程同步退避。
        flush_every: writer 每追加多少条记录做一次全量原子重写（去重 + 规整）。
        matcher_legacy: True 时使用对齐原版 RobustJSONComparator 的宽松
            验收规则（大小写不敏感、数字逗号等价等），用于历史数据对账；
            默认 False（audit-02 规格）。逐项差异见 doc/comparator-compat.md。
    """

    api_key: str
    api_endpoint: str = "https://maasrd.hikvision.com.cn/v1"
    model: str = "Qwen3.6-35B-A3B-FP8"
    qpm_limit: int = 50
    max_concurrent: int = 10
    max_sample_attempts: int = 3
    network_max_attempts: int = 5
    request_timeout: float = 120.0
    backoff_base: float = 5.0
    backoff_cap: float = 60.0
    backoff_jitter: float = 0.5
    flush_every: int = 10
    matcher_legacy: bool = False
