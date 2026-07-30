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
        request_timeout: 单次 HTTP 请求总超时（秒）。默认 120 = 2026-07-30
            生产实测推荐值。推理：官方采样档修复死循环后，合法响应实测
            分布 4–31s（p50≈10s），120s 留 ~4× 余量；超过 120s 的请求
            几乎必然已死于 thinking 耗尽（LENGTH_TRUNCATED 实测
            230–316s）或正走向网关墙（504@360s）——提前掐断释放并发槽、
            早点重试（官方档下重试有方差、有真实胜率）。
            **有意放弃了「超时必须大于网关墙 360s」的旧结论**（旧推导见
            doc/investigation-01 追加三，推翻记录见追加五）：代价是
            >120s 的慢失败不再保留 LENGTH_TRUNCATED / GATEWAY_ERROR
            精细分类，统一归 NETWORK_ERROR(timeout) 烧网络账重试
            （日志 `elapsed≈120` 可辨）；换来的是单样本死时间上限从
            ~316s 降到 ~120s，并发槽周转率显著提升。
        connect_timeout: 建立连接的超时（秒）。默认 30 = 生产实测推荐值，
            弱网/代理环境下不冤杀慢握手；真连接故障仍在半分钟内快速
            失败，不陪跑 request_timeout。
        gateway_max_attempts: 网关类错误（502/503/504）单样本重试上限。
            504 实测多为网关 360s 阈值截断（确定性长尾），满额 network_life
            重试 = 单样本最多 30 分钟纯浪费，故单独封顶（默认 2）；仍消耗
            network_life，两约束同时生效。注：request_timeout=120 下，
            360s 慢 504 会先被掐成 NETWORK_ERROR(timeout)，此分类主要
            覆盖快速返回的 502/503/504。
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
    request_timeout: float = 120.0
    connect_timeout: float = 30.0
    gateway_max_attempts: int = 2
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
