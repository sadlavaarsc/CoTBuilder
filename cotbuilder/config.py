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
        request_timeout: 单次 HTTP 请求总超时（秒），需大于模型推理延迟上限。
            思考型模型（enable_thinking=true）推理可超过 2 分钟，默认 600s
            （实测 120s 会把正常慢推理掐成 NETWORK_ERROR，见 design.md §7）。
        connect_timeout: 建立连接的超时（秒）。连接失败快速失败，与慢推理
            分离——真网络故障几秒内就能判死，无需陪跑 600s。
        backoff_base: 退避基数（秒），第 n 次重试退避 min(base*2^n, cap)。
        backoff_cap: 退避上限（秒）。
        backoff_jitter: 退避抖动幅度，0.5 表示 ±50%，破坏多协程同步退避。
        flush_every: writer 每追加多少条记录做一次全量原子重写（去重 + 规整）。
        max_tokens: 单次请求输出 token 上限。32768 是 Qwen3.6-35B 服务端
            输出硬上限（实测发 65536 被静默钳制），调大无效；调小会让
            thinking 更早耗尽预算导致 content=null（见
            doc/investigation-01-e2e-diagnosis.md §2.2）。
        temperature / top_p / top_k / presence_penalty: 采样参数。
            默认 = Qwen3.6-35B 官方「思考模式·精确任务」档
            （0.6 / 0.95 / 20 / 0）。原 temp=0.1 严重偏离官方建议，
            是 thinking 死循环（43% EMPTY）的疑似诱因，并让重试近乎
            确定性（同上报告，追加记录 2026-07-29）。
        enable_thinking: 是否开启思考模式（chat_template_kwargs）。
        metrics_interval: 性能追踪滑动桶宽度（秒），有效 QPM 曲线的采样粒度。
        progress_log_interval: 控制台进度行输出间隔（秒），0 = 关闭。
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
    request_timeout: float = 600.0
    connect_timeout: float = 15.0
    backoff_base: float = 5.0
    backoff_cap: float = 60.0
    backoff_jitter: float = 0.5
    flush_every: int = 10
    max_tokens: int = 32768
    temperature: float = 0.6
    top_p: float = 0.95
    top_k: int = 20
    presence_penalty: float = 0.0
    enable_thinking: bool = True
    metrics_interval: float = 10.0
    progress_log_interval: float = 30.0
    matcher_legacy: bool = False
