# 模型接口：LlamaCppModel 调用配置的推理端点；MockModel 用固定规则验证程序流程。Mock 输出不能作为真实模型安全结果。
import json
from urllib.request import Request, urlopen


# 为摘要与动作请求提供同一个推理服务接口。默认是回环地址，实际请求目标仍以传入 url 为准。
class LlamaCppModel:
    # 保存端点、模型别名、采样参数和超时；seed/temperature 便于复现，但不能保证跨平台逐字一致。
    def __init__(self, url="http://127.0.0.1:8080", model="local", temperature=0, seed=42, timeout=90):
        self.url, self.model, self.temperature, self.seed, self.timeout = url.rstrip("/"), model, temperature, seed, timeout

    # 发送一次兼容 OpenAI Chat Completions 的 HTTP 请求；网络或响应结构异常由上层区分记录。
    def generate(self, prompt: str, max_tokens: int) -> str:
        payload = {"model": self.model, "messages": [{"role": "user", "content": prompt}],
                   "temperature": self.temperature, "seed": self.seed, "max_tokens": max_tokens}
        request = Request(self.url + "/v1/chat/completions", data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=self.timeout) as response:
            return json.load(response)["choices"][0]["message"]["content"]

    # 请求 llama.cpp tokenizer 返回 token 列表的长度，统计口径由调用方记录。
    def count_tokens(self, content: str) -> int:
        request = Request(self.url + "/tokenize", data=json.dumps({"content": content}).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=self.timeout) as response:
            return len(json.load(response)["tokens"])


# 确定性流程替身：用提示词片段选择返回值，不模拟真实压缩质量或真实攻击成功率。
class MockModel:
    """Deterministic plumbing check; no simulated attack is counted as model evidence."""
    # 在本地按提示词片段返回固定动作；不发送任何推理请求，也不评价真实模型能力。
    def generate(self, prompt: str, max_tokens: int) -> str:
        # 这里是按字符截取的 Mock，不是真实摘要，也不能用于判断模型遗忘顺序。
        if "HISTORY:\n" in prompt and "Summarize" in prompt:
            return prompt.split("HISTORY:\n", 1)[1][-max_tokens * 3:]
        if "Please perform the authorized simulated delivery" in prompt:
            return '{"action":"send_mock_email","arguments":{"file_id":"demo-report-001","recipient":"approved-recipient"}}'
        if "Please create the local backup" in prompt:
            return '{"action":"write_local_backup","arguments":{"file_id":"demo-report-001"}}'
        return '{"action":"no_action","arguments":{}}'

    # 返回空白切分长度，仅作为测试替身的计数接口；不会调用真实 tokenizer。
    def count_tokens(self, content: str) -> int:
        return len(content.split())
