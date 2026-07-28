import logging
import re
import base64
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
import sys

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evaluation.robust_json_comparator import RobustJSONComparator


# ============================================================
# CoT v2.3 新增：系统提示词模板
# ============================================================

SYSTEM_PROMPT_V2_3 = (
    "你是一个专业的文档信息提取专家。请仔细分析图片中的文档，提取指定的关键信息。\n"
    "\n"
    "请按照以下步骤进行推理：\n"
    "1. 首先观察图片的整体布局和文档类型\n"
    "2. 识别文档中的关键字段位置\n"
    "3. 逐个提取每个字段的值\n"
    "4. 验证提取结果的准确性\n"
    "5. 输出完整的 JSON 格式结果\n"
    "\n"
    "提取规则（必须严格遵守）：\n"
    "1. 照抄原文：图中已有字段值必须与图中文字完全一致，不得改写、不得补全、不得省略。\n"
    "   图中有的符号（如 ¥、®、™）保留，图中没有的不得添加；\n"
    "2. 标点风格：中文字段使用中文标点（全角），英文字段使用英文标点，\n"
    "   英文逗号后保留一个空格（如 \"LAU, LAI LI\"）；\n"
    "3. 金额字段：保留图中所示的货币符号与数字格式（如图中为\"¥5.83\"，输出\"¥5.83\"）；\n"
    "4. 字段在图中不存在时，结合已有信息进行推理，得到实际的结果；\n"
    "5. 纯数字长串（发票号码、校验码等）逐位核对后再输出。\n"
    "\n"
    "请使用中文进行推理思考，最终输出 JSON 格式的结果。"
)


# 
# CoT v2.3 新增：鲁棒文件写入器（实时写入 + 断点恢复）
# 

class RobustFileWriter:
    """
    简单的实时写入器：每处理一个样本立即写入，不做批量优化。
    因为 latency 瓶颈在 API 调用，IO 不是瓶颈。
    """

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        # 确保输出目录存在
        os.makedirs(output_dir, exist_ok=True)
        self.success_file = os.path.join(output_dir, "success_samples.json")
        self.failed_file = os.path.join(output_dir, "failed_samples.json")
        self.checkpoint_file = os.path.join(output_dir, "checkpoint.json")
        self.processed_ids: set = set()
        self._load_checkpoint()

    def _load_checkpoint(self):
        """加载检查点，用于断点恢复"""
        if os.path.exists(self.checkpoint_file):
            try:
                with open(self.checkpoint_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.processed_ids = set(data.get("processed_ids", []))
            except (json.JSONDecodeError, IOError):
                self.processed_ids = set()

    def _read_json_file(self, filepath: str) -> list:
        """读取 JSON 文件"""
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return []
        return []


    def save_result(self, result: Dict[str, Any]):
        """
        立即保存单个样本结果（简单粗暴，不做批量优化）
        
        流程：
        1. 读取现有成功/失败列表
        2. 去重（移除已处理的同 ID 样本）
        3. 追加新结果
        4. 立即写入磁盘
        5. 更新检查点
        """
        status = result["status"]
        target_file = self.success_file if status == "success" else self.failed_file

        existing = self._read_json_file(target_file)

        # 去重
        existing = [r for r in existing if r["sample_id"] != result["sample_id"]]
        existing.append(result)

        # 立即写入
        with open(target_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)

        # 更新检查点
        self.processed_ids.add(result["sample_id"])
        self._save_checkpoint()

    def _save_checkpoint(self):
        """保存检查点"""
        checkpoint = {
            "timestamp": time.time(),
            "processed_ids": list(self.processed_ids),
        }
        with open(self.checkpoint_file, "w", encoding="utf-8") as f:
            json.dump(checkpoint, f, ensure_ascii=False, indent=2)

    def is_processed(self, sample_id: str) -> bool:
        """检查样本是否已处理"""
        return sample_id in self.processed_ids


# ============================================================
# CoT v2.3 新增：鲁棒匹配器（三级匹配标准，用于评估分析）
# ============================================================

class RobustMatcher:
    """鲁棒匹配器：用于评估分析，不影响主流程"""

    def __init__(self):
        # 常见符号差异映射
        self.punctuation_map = {
            "(": "（",
            ")": "）",
            ",": "，",  # 英文逗号 -> 中文逗号
        }

    def normalize(self, text: str) -> str:
        """归一化处理：统一标点、空格、货币符号"""
        # 统一标点
        for eng, chn in self.punctuation_map.items():
            text = text.replace(eng, chn)

        # 统一空格
        text = text.replace(" ", "")

        # 移除货币符号
        text = text.replace("¥", "")

        return text.strip()

    def classify_match(self, predicted: Dict, ground_truth: Dict) -> Dict:
        """
        三级匹配分类：
        - STRICT_MATCH: 完全一致
        - NORMALIZED_MATCH: 归一化后一致（格式问题，非推理错误）
        - MISMATCH: 不匹配
        """
        # 严格匹配
        strict = predicted == ground_truth

        # 归一化匹配
        normalized_pred = {k: self.normalize(str(v)) for k, v in predicted.items()}
        normalized_gt = {k: self.normalize(str(v)) for k, v in ground_truth.items()}
        normalized = normalized_pred == normalized_gt

        # 分类
        if strict:
            match_level = "STRICT_MATCH"
        elif normalized:
            match_level = "NORMALIZED_MATCH"
        else:
            match_level = "MISMATCH"

        return {
            "match_level": match_level,
            "strict_match": strict,
            "normalized_match": normalized,
            "match_score": 1.0 if strict else (0.8 if normalized else 0.0),
        }


class CoTGenerator:
    """CoT 数据生成器 v2"""

    def __init__(self, 
                 api_key: str,
                 api_endpoint: str = "https://maasrd.hikvision.com.cn/v1",
                 model: str = "Qwen3.6-35B-A3B-FP8",
                 max_retries: int = 3,
                 qpm_limit: int = 50,
                 max_concurrent: int = 10):
        """
        初始化 CoT 生成器。

        Args:
            api_key: API 密钥
            api_endpoint: API 端点
            model: 专家模型名称
            max_retries: 最大重试次数（仅用于 MISMATCH）
            qpm_limit: QPM 限制
            max_concurrent: 最大并发数
        """
        self.api_key = api_key
        self.api_endpoint = api_endpoint
        self.model = model
        self.max_retries = max_retries
        self.qpm_limit = qpm_limit
        self.max_concurrent = max_concurrent
        
        # 初始化比较器
        self.comparator = RobustJSONComparator()
        
        # 设置日志
        self._setup_logging()
        
        # QPM 控制
        self.qpm_semaphore = asyncio.Semaphore(max_concurrent)
        self.request_timestamps = deque()  # 存储最近60秒内的请求时间戳
        self.qpm_window = 60  # QPM 窗口大小，单位：秒
        self.qpm_limit = qpm_limit
        
        # CoT v2.3 新增：鲁棒文件写入器和匹配器实例
        # 这些将在 generate_batch 中初始化，以确保 output_dir 已设置
        self.robust_file_writer = None
        self.robust_matcher = RobustMatcher()
    def _setup_logging(self):
        """设置日志配置"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(sys.stdout),
                logging.FileHandler('cot_generator_v2.log', encoding='utf-8')
            ]
        )
        self.logger = logging.getLogger(__name__)

    async def _rate_limit(self):
        """基于滑动窗口的 QPM 限流控制 — 精确计算等待时间"""
        async with self.qpm_semaphore:
            now = time.time()
            
            # 清理过期的时间戳（超过60秒前的请求）
            while self.request_timestamps and now - self.request_timestamps[0] >= self.qpm_window:
                self.request_timestamps.popleft()
            
            # 计算当前 QPM（过去60秒内的请求数）
            current_qpm = len(self.request_timestamps)
            
            # 如果当前 QPM 已经达到或超过限制，需要暂停
            if current_qpm >= self.qpm_limit:
                # 精确计算等待时间：等待最早进入窗口的请求滑出
                # 需要释放的空间数 = current_qpm - qpm_limit + 1（为当前请求留出位置）
                slots_to_free = current_qpm - self.qpm_limit + 1
                
                # 取第 slots_to_free 个最早的时间戳（索引 slots_to_free - 1）
                # 它滑出窗口后，就有足够的空间了
                earliest_needed = self.request_timestamps[slots_to_free - 1]
                
                # 计算它何时滑出窗口
                wait_time = (earliest_needed + self.qpm_window) - now
                wait_time = max(0.1, wait_time)  # 最小等待 0.1 秒，避免负数
                
                self.logger.info(
                    f"QPM limit reached ({current_qpm}/{self.qpm_limit}), "
                    f"need to free {slots_to_free} slot(s), waiting {wait_time:.2f}s"
                )
                
                # 等待后循环检查，确保窗口内有足够空间
                while wait_time > 0:
                    await asyncio.sleep(min(wait_time, 1.0))  # 分片等待，每次最多 1 秒
                    now = time.time()
                    
                    # 重新清理过期时间戳
                    while self.request_timestamps and now - self.request_timestamps[0] >= self.qpm_window:
                        self.request_timestamps.popleft()
                    
                    current_qpm = len(self.request_timestamps)
                    
                    if current_qpm < self.qpm_limit:
                        break  # 有空间了，退出循环
                    
                    # 仍然超限，重新计算等待时间
                    slots_to_free = current_qpm - self.qpm_limit + 1
                    earliest_needed = self.request_timestamps[slots_to_free - 1]
                    wait_time = (earliest_needed + self.qpm_window) - now
                    wait_time = max(0.1, wait_time)
            
            # 记录本次请求的时间戳
            self.request_timestamps.append(time.time())
            
            # 记录调试信息
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"Request processed, current QPM: {current_qpm}/{self.qpm_limit}")
    async def _network_retry_call(self, messages: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], str]:  # 新增返回值类型
        """
        网络问题指数退避重试（不消耗重试次数）。

        Args:
            messages: 消息列表（多模态格式）

        Returns:
            Tuple[Optional[Dict], str]: (完整API响应或 None, 错误类型)
        """
        # 网络相关错误类型
        NETWORK_ERRORS = ['NETWORK_ERROR', 'RATE_LIMITED', 'API_ERROR', 'EMPTY_RESPONSE']
        
        for attempt in range(5):  # 最多5次指数退避重试
            try:
                await self._rate_limit()
                
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.api_endpoint}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "model": self.model,
                            "messages": messages,
                            "max_tokens": 65536,
                            "temperature": 0.1,
                            "top_p": 0.9,
                            "chat_template_kwargs": {"enable_thinking": True}
                        },
                        timeout=aiohttp.ClientTimeout(total=120)
                    ) as response:
                        
                        if response.status == 200:
                            result = await response.json()
                            return result, None  # 成功，无错误
                        elif response.status == 403:
                            # QPM 限流错误
                            error_text = await response.text()
                            if "qpm" in error_text.lower() or "频率" in error_text:
                                wait_time = min(5 * (2 ** attempt), 60)  # 指数退避，最多60秒
                                self.logger.warning(f"QPM rate limited, waiting {wait_time}s (attempt {attempt + 1})")
                                await asyncio.sleep(wait_time)
                                continue  # 继续循环，不消耗 max_retries
                            else:
                                self.logger.error(f"API request failed (403): {error_text}")
                                return None, 'API_ERROR'
                        else:
                            error_text = await response.text()
                            self.logger.error(f"API request failed: {response.status} - {error_text}")
                            return None, 'API_ERROR'
            
            except Exception as e:
                self.logger.error(f"Error calling expert model: {e}")
                if attempt < 4:  # 不是最后一次尝试
                    wait_time = min(5 * (2 ** attempt), 60)
                    self.logger.warning(f"Network error, retrying in {wait_time}s (attempt {attempt + 1})")
                    await asyncio.sleep(wait_time)
                else:
                    return None, 'NETWORK_ERROR'
        return None, 'NETWORK_ERROR'  # 所有重试失败

    def _extract_json_from_response(self, response: str) -> Optional[Dict[str, Any]]:
        """
        从模型响应中提取 JSON 结果。

        Args:
            response: 模型响应文本

        Returns:
            Optional[Dict]: 提取的 JSON 字典或 None
        """
        if not response:
            return None
        
        # 尝试直接解析 JSON
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass
        
        # 尝试从代码块中提取
        import re
        json_pattern = r'```(?:json)?\s*(\{.*?\})\s*```'
        match = re.search(json_pattern, response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        
        # 尝试从文本中提取第一个 JSON 对象
        brace_match = re.search(r'\{.*?\}', response, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group())
            except json.JSONDecodeError:
                pass
        
        return None

    def _encode_image_to_base64(self, image_path: str) -> Optional[str]:
        """
        将图片文件编码为 base64 URL。

        Args:
            image_path: 图片文件路径

        Returns:
            Optional[str]: base64 data URL 或 None（失败时）
        """
        try:
            with open(image_path, 'rb') as f:
                image_data = f.read()
            b64 = base64.b64encode(image_data).decode('utf-8')
            image_url = f"data:image/jpeg;base64,{b64}"
            self.logger.info(f"Encoded image {image_path} to base64 ({len(b64)} chars)")
            return image_url
        except Exception as e:
            self.logger.error(f"Failed to encode image {image_path}: {e}")
            return None


    def _build_messages(self, sample: Dict[str, Any], cot_prompt: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        构建模型输入消息（多模态格式）。

        Args:
            sample: 样本数据
            cot_prompt: 自定义 CoT 提示词

        Returns:
            List[Dict]: 消息列表，content 为数组格式（image_url + text）
        """
        if cot_prompt:
            system_message = cot_prompt
        else:
            # CoT v2.3: 使用新版系统提示词（推理框架 + 字段约束）
            system_message = SYSTEM_PROMPT_V2_3

        # 优先读取 messages 字段（新格式），没有则回退到 conversations（旧格式）
        if "messages" in sample and sample["messages"]:
            user_message = sample["messages"][0].get("content", "")
        else:
            user_message = sample.get("conversations", [{}])[0].get("value", "")
        
        # 从 prompt 中移除 <image> 占位符（如果存在）
        prompt_text = user_message.replace("<image>", "").strip()
        if not prompt_text:
            prompt_text = user_message.strip()
        
        # 构造多模态消息
        messages = [
            {"role": "system", "content": system_message}
        ]
        
        user_content = []
        
        # 如果有图片，添加图片部分
        if "images" in sample and sample["images"]:
            image_path = sample["images"][0]
            if image_path and isinstance(image_path, str) and image_path.strip():
                image_url = self._encode_image_to_base64(image_path)
                if image_url:
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": image_url}
                    })
        
        # 添加文本部分
        user_content.append({
            "type": "text",
            "text": prompt_text
        })
        
        messages.append({
            "role": "user",
            "content": user_content
        })
        
        return messages
    async def generate_cot_for_sample(self, 
                                    sample: Dict[str, Any], 
                                    sample_id: str,
                                    cot_prompt: Optional[str] = None) -> Dict[str, Any]:
        """
        为单个样本生成 CoT 数据。

        Args:
            sample: 样本数据
            sample_id: 样本 ID
            cot_prompt: 自定义 CoT 提示词

        Returns:
            Dict: 生成结果
        """
        self.logger.info(f"Processing sample {sample_id}")
        
        # 获取 Ground Truth（兼容 messages 和 conversations 格式）
        if "messages" in sample and len(sample.get("messages", [])) > 1:
            ground_truth_text = sample["messages"][1].get("content", "")
        else:
            ground_truth_text = sample.get("conversations", [{}])[1].get("value", "")
        ground_truth = self.comparator._parse_json(ground_truth_text)
        
        if not ground_truth:
            self.logger.error(f"Failed to parse ground truth for sample {sample_id}")
            return {
                "sample_id": sample_id,
                "status": "failed",
                "error": "Invalid ground truth format",
                "error_type": "JSON_PARSE_ERROR"  # 新增错误类型
            }
        # 构建消息
        messages = self._build_messages(sample, cot_prompt)
        
        # 先进行网络重试（不消耗 max_retries）
        api_response, network_error = await self._network_retry_call(messages)
        
        if network_error:
            # 网络问题重试失败
            return {
                "sample_id": sample_id,
                "status": "failed",
                "error": f"Network error: {network_error}",
                "error_type": network_error
            }
        
        # 从完整响应中提取 content
        response_content = api_response.get("choices", [{}])[0].get("message", {}).get("content", "")
        
        if not response_content:
            return {
                "sample_id": sample_id,
                "status": "failed",
                "error": "Empty response content",
                "error_type": "EMPTY_RESPONSE",
                "full_api_response": api_response
            }
        
        # 提取 JSON 结果
        predicted_json = self._extract_json_from_response(response_content)
        
        if not predicted_json:
            return {
                "sample_id": sample_id,
                "status": "failed",
                "error": "Failed to extract JSON from response",
                "error_type": "JSON_PARSE_ERROR",
                "cot_response": response_content,
                "full_api_response": api_response
            }
        # 验证结果
        comparison_result = self.comparator.compare(predicted_json, ground_truth)
        
        if comparison_result["is_match"]:
            self.logger.info(f"Sample {sample_id}: MATCH")
            result = {
                "sample_id": sample_id,
                "status": "success",
                "attempts": 1,  # 网络重试不计入
                "original_sample": sample,
                "cot_response": response_content,
                "full_api_response": api_response,
                "predicted_json": predicted_json,
                "ground_truth": ground_truth,
                "comparison_result": comparison_result,
            }
            # CoT v2.3: 添加鲁棒匹配分析
            result["robust_match"] = self._get_robust_match_analysis(result)
            return result

        # MISMATCH，启动三并发抽卡
        self.logger.info(f"Sample {sample_id}: MISMATCH, starting 3x concurrent retries")
        
        # 创建三个并发任务
        tasks = [
            self._network_retry_call(messages) for _ in range(3)
        ]
        results = await asyncio.gather(*tasks)
        # 从三次结果中选择匹配度最高的
        best_result = None
        best_score = -1.0
        
        for api_response, error in results:
            if error:
                continue  # 跳过失败的
            
            content = api_response.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not content:
                continue
            
            json_obj = self._extract_json_from_response(content)
            if not json_obj:
                continue
            
            comp_result = self.comparator.compare(json_obj, ground_truth)
            if comp_result["match_score"] > best_score:
                best_score = comp_result["match_score"]
                best_result = {
                    "api_response": api_response,
                    "content": content,
                    "json": json_obj,
                    "comparison": comp_result
                }


        if best_result and best_result["comparison"]["is_match"]:
            self.logger.info(f"Sample {sample_id}: MATCH from 3x concurrent")
            result = {
                "sample_id": sample_id,
                "status": "success",
                "attempts": 1,  # 并发视为一次尝试
                "original_sample": sample,
                "cot_response": best_result["content"],
                "full_api_response": best_result["api_response"],
                "predicted_json": best_result["json"],
                "ground_truth": ground_truth,
                "comparison_result": best_result["comparison"],
            }
            # CoT v2.3: 添加鲁棒匹配分析
            result["robust_match"] = self._get_robust_match_analysis(result)
            return result
        
        # 三次均未成功，返回最佳结果
        if best_result:
            self.logger.info(f"Sample {sample_id}: Best MISMATCH score: {best_score:.2%}")
            result = {
                "sample_id": sample_id,
                "status": "failed",
                "attempts": 1,
                "original_sample": sample,
                "cot_response": best_result["content"],
                "full_api_response": best_result["api_response"],
                "predicted_json": best_result["json"],
                "ground_truth": ground_truth,
                "comparison_result": best_result["comparison"],
                "error": f"Failed to match after 3x concurrent, best score: {best_score:.2%}",
                "error_type": "MISMATCH",
            }
            # CoT v2.3: 添加鲁棒匹配分析
            result["robust_match"] = self._get_robust_match_analysis(result)
            return result
        # 三次都失败
        result = {
            "sample_id": sample_id,
            "status": "failed",
            "attempts": 1,
            "original_sample": sample,
            "error": "All 3 concurrent attempts failed",
            "error_type": "NETWORK_ERROR"  # 或者更具体的错误
        }
        # CoT v2.3: 添加鲁棒匹配分析（即使失败也提供分析）
        result["robust_match"] = self._get_robust_match_analysis(result)
        return result
    def _get_robust_match_analysis(self, result: Dict[str, Any]) -> Dict:
        """
        CoT v2.3: 计算鲁棒匹配分析结果。
        仅用于评估报告，不改变主流程。
        """
        if self.robust_matcher is None:
            return {}
        
        predicted = result.get("predicted_json")
        ground_truth = result.get("ground_truth")
        if predicted and ground_truth:
            return self.robust_matcher.classify_match(predicted, ground_truth)
        return {}

    async def generate_batch(self,
                           samples: List[Dict[str, Any]],
                           output_dir: str,
                           cot_prompt: Optional[str] = None,
                           progress_callback=None) -> Dict[str, Any]:
        """
        批量生成 CoT 数据。

        Args:
            samples: 样本列表
            output_dir: 输出目录（v2.3 新增，用于实时写入）
            cot_prompt: 自定义 CoT 提示词
            progress_callback: 进度回调函数

        Returns:
            Dict: 批量生成结果
        """
        self.logger.info(f"Starting batch generation for {len(samples)} samples")
        
        # CoT v2.3: 初始化鲁棒文件写入器（实时写入 + 断点恢复）
        self.robust_file_writer = RobustFileWriter(output_dir)
        
        tasks = []
        results = []
        skipped_count = 0
        
        # 创建异步任务（v2.3: 跳过已处理的样本）
        for i, sample in enumerate(samples):
            sample_id = sample.get("id", f"sample_{i}")
            
            # CoT v2.3: 检查是否已处理，跳过已完成的样本
            if self.robust_file_writer.is_processed(sample_id):
                self.logger.info(f"Skipping already processed sample: {sample_id}")
                skipped_count += 1
                continue
            
            task = self.generate_cot_for_sample(sample, sample_id, cot_prompt)
            tasks.append(task)
        
        self.logger.info(f"Starting generation for {len(tasks)} samples (skipped {skipped_count} already processed)")
        
        # 并发执行
        completed = 0
        for future in asyncio.as_completed(tasks):
            result = await future
            results.append(result)
            completed += 1
            
            # CoT v2.3: 实时写入磁盘（每处理一个样本立即保存）
            self.robust_file_writer.save_result(result)
            
            if progress_callback:
                progress_callback(completed + skipped_count, len(samples))
            
            self.logger.info(f"Progress: {completed + skipped_count}/{len(samples)} samples processed (skipped: {skipped_count})")
        
        # 统计结果
        success_count = sum(1 for r in results if r["status"] == "success")
        failed_count = len(results) - success_count
        return {
            "total_samples": len(samples),
            "success_count": success_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "success_rate": success_count / len(samples) if len(samples) > 0 else 0,
            "results": results
        }

    def save_results(self, batch_result: Dict[str, Any], output_dir: str):
        """
        保存生成结果。

        Args:
            batch_result: 批量生成结果
            output_dir: 输出目录
        """
        os.makedirs(output_dir, exist_ok=True)
        
        # 保存成功样本
        success_samples = [r for r in batch_result["results"] if r["status"] == "success"]
        if success_samples:
            success_file = os.path.join(output_dir, "success_samples.json")
            with open(success_file, 'w', encoding='utf-8') as f:
                json.dump(success_samples, f, ensure_ascii=False, indent=2)
            self.logger.info(f"Saved {len(success_samples)} success samples to {success_file}")
        
        # 保存失败样本
        failed_samples = [r for r in batch_result["results"] if r["status"] == "failed"]
        if failed_samples:
            failed_file = os.path.join(output_dir, "failed_samples.json")
            with open(failed_file, 'w', encoding='utf-8') as f:
                json.dump(failed_samples, f, ensure_ascii=False, indent=2)
            self.logger.info(f"Saved {len(failed_samples)} failed samples to {failed_file}")
        # 保存摘要报告
        summary = {
            "timestamp": time.time(),
            "total_samples": batch_result["total_samples"],
            "success_count": batch_result["success_count"],
            "failed_count": batch_result["failed_count"],
            "success_rate": batch_result["success_rate"],
            "config": {
                "model": self.model,
                "max_retries": self.max_retries,
                "qpm_limit": self.qpm_limit,
                "max_concurrent": self.max_concurrent
            }
        }
        
        summary_file = os.path.join(output_dir, "summary.json")
        with open(summary_file, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        
        self.logger.info(f"Saved summary to {summary_file}")
def load_samples(input_file: str, num_samples: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    加载样本数据。

    Args:
        input_file: 输入文件路径
        num_samples: 采样数量

    Returns:
        List[Dict]: 样本列表
    """
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    if num_samples and num_samples < len(data):
        import random
        random.seed(42)  # 固定随机种子保证可复现
        return random.sample(data, num_samples)
    
    return data


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="CoT Data Generator v2")
    parser.add_argument("--input", required=True, help="Input JSON file path")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--num-samples", type=int, default=None, help="Number of samples to process")
    parser.add_argument("--api-key", required=True, help="API key for expert model")
    parser.add_argument("--model", default="Qwen3.6-35B-A3B-FP8", help="Expert model name")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retry attempts (for MISMATCH, not network)")
    parser.add_argument("--max-concurrent", type=int, default=10, help="Max concurrent requests")
    parser.add_argument("--qpm-limit", type=int, default=50, help="QPM limit")
    
    args = parser.parse_args()
    
    # 加载样本
    samples = load_samples(args.input, args.num_samples)
    print(f"Loaded {len(samples)} samples from {args.input}")


    # 创建生成器
    generator = CoTGenerator(
        api_key=args.api_key,
        model=args.model,
        max_retries=args.max_retries,
        qpm_limit=args.qpm_limit,
        max_concurrent=args.max_concurrent
    )
    
    # 进度回调
    def progress_callback(completed, total):
        print(f"Progress: {completed}/{total} ({completed/total*100:.1f}%)")
    
    # 运行批量生成
    async def run_generation():
        return await generator.generate_batch(samples, args.output, progress_callback=progress_callback)
    
    # 执行异步任务
    loop = asyncio.get_event_loop()
    batch_result = loop.run_until_complete(run_generation())
    
    # v2.3: save_results is deprecated, results are saved in real-time by RobustFileWriter
    # generator.save_results(batch_result, args.output)
    # 打印摘要
    print("\n" + "="*50)
    print("CoT Generation Summary (v2)")
    print("="*50)
    print(f"Total samples: {batch_result['total_samples']}")
    print(f"Success: {batch_result['success_count']}")
    print(f"Failed: {batch_result['failed_count']}")
    print(f"Success rate: {batch_result['success_rate']:.2%}")
    print("="*50)


if __name__ == "__main__":
    main()