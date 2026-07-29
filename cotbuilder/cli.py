"""命令行入口：python -m cotbuilder.cli --input ... --output ...

只负责参数解析、日志配置与 asyncio.run，不含任何业务逻辑。
（老代码在构造函数里 basicConfig，import 即污染全局日志——已修正。）
"""

import argparse
import asyncio
import json
import logging
import random
import sys
from typing import Any, Dict, List, Optional

from .batch import BatchRunner
from .config import Config


def load_samples(input_file: str,
                 num_samples: Optional[int] = None) -> List[Dict[str, Any]]:
    """加载样本数据；num_samples 采样时固定种子保证可复现（老代码语义）。"""
    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    if num_samples and num_samples < len(data):
        random.seed(42)
        return random.sample(data, num_samples)
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="CoT Data Generator (重构版)")
    parser.add_argument("--input", required=True, help="输入样本 JSON 文件")
    parser.add_argument("--output", required=True, help="输出目录")
    parser.add_argument("--num-samples", type=int, default=None,
                        help="采样数量（默认全部）")
    parser.add_argument("--api-key", required=True, help="专家模型 API 密钥")
    parser.add_argument("--api-endpoint",
                        default="https://maasrd.hikvision.com.cn/v1",
                        help="API 基础地址")
    parser.add_argument("--model", default="Qwen3.6-35B-A3B-FP8",
                        help="专家模型名称")
    parser.add_argument("--qpm-limit", type=int, default=50,
                        help="每分钟请求发起上限（含全部重试，匀速放行）")
    parser.add_argument("--max-concurrent", type=int, default=10,
                        help="在途 HTTP 请求硬上限")
    parser.add_argument("--max-sample-attempts", type=int, default=3,
                        help="样本寿命：MISMATCH 最大重试次数")
    parser.add_argument("--network-max-attempts", type=int, default=5,
                        help="网络寿命：网络/限流错误最大重试次数")
    parser.add_argument("--request-timeout", type=float, default=600.0,
                        help="单次请求总超时（秒）。思考型模型推理可超过 "
                             "2 分钟，默认 600s；过短会把正常慢推理掐成 "
                             "NETWORK_ERROR")
    parser.add_argument("--connect-timeout", type=float, default=15.0,
                        help="建立连接超时（秒），真网络故障快速失败")
    parser.add_argument("--max-tokens", type=int, default=32768,
                        help="输出 token 上限。32768 是服务端硬上限，"
                             "调大无效（被静默钳制）")
    parser.add_argument("--temperature", type=float, default=0.6,
                        help="采样温度，默认官方思考·精确档 0.6")
    parser.add_argument("--top-p", type=float, default=0.95,
                        help=" nucleus sampling，默认官方档 0.95")
    parser.add_argument("--top-k", type=int, default=20,
                        help="top-k 采样，默认官方档 20")
    parser.add_argument("--presence-penalty", type=float, default=0.0,
                        help="存在惩罚（抗重复）。思考·通用档官方建议 1.5，"
                             "死循环频发时可试")
    parser.add_argument("--no-thinking", action="store_false",
                        dest="enable_thinking",
                        help="关闭思考模式（enable_thinking=false）")
    parser.add_argument("--legacy-matcher", action="store_true",
                        help="使用对齐原版 RobustJSONComparator 的宽松验收"
                             "规则（历史数据对账用，默认 audit-02 规格）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("cot_generator.log", encoding="utf-8"),
        ],
    )

    samples = load_samples(args.input, args.num_samples)
    print(f"Loaded {len(samples)} samples from {args.input}")

    config = Config(
        api_key=args.api_key,
        api_endpoint=args.api_endpoint,
        model=args.model,
        qpm_limit=args.qpm_limit,
        max_concurrent=args.max_concurrent,
        max_sample_attempts=args.max_sample_attempts,
        network_max_attempts=args.network_max_attempts,
        request_timeout=args.request_timeout,
        connect_timeout=args.connect_timeout,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
        top_k=args.top_k,
        presence_penalty=args.presence_penalty,
        enable_thinking=args.enable_thinking,
        matcher_legacy=args.legacy_matcher,
    )

    def progress(completed: int, total: int) -> None:
        print(f"Progress: {completed}/{total} ({completed / total * 100:.1f}%)")

    runner = BatchRunner(config)
    result = asyncio.run(
        runner.run(samples, args.output, progress_callback=progress))

    print("\n" + "=" * 50)
    print("CoT Generation Summary")
    print("=" * 50)
    print(f"Total samples: {result['total_samples']}")
    print(f"Success: {result['success_count']}")
    print(f"Failed: {result['failed_count']}")
    print(f"Skipped: {result['skipped_count']}")
    print(f"Success rate: {result['success_rate']:.2%}")
    print("=" * 50)


if __name__ == "__main__":
    main()
