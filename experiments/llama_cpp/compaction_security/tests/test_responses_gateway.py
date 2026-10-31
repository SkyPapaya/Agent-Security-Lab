# 测试 Responses 记录网关的原文保留、工具隔离、凭据脱敏和请求上限。模型响应由伪造上游提供，不访问供应商。
# 阅读顺序：先看每个测试上方的条件和目的，再对照实际断言；固定回复只用于离线单测。
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from experiments.llama_cpp.compaction_security.native_retention.responses_gateway import (
    ResponsesGateway, ResponsesGatewayError, ResponsesBudgetExceeded, response_object,
)


SECRET = "fictional-unit-test-sub2api-credential"
BASE = "https://provider.example.invalid/v1"
PAYLOAD = {"model": "synthetic-model", "input": [{"role": "user", "content": "虚构测试"}],
           "max_output_tokens": 64}


# 用内存字节流模拟上游响应的状态和响应头，测试不会通过它访问远端。
class Response(io.BytesIO):
    # 初始化可读取的响应字节、状态码和头信息，让网关按真实接口解析伪响应。
    def __init__(self, body, content_type="application/json", status=200):
        super().__init__(body)
        self.status = status
        self.headers = {"content-type": content_type, "x-request-id": "synthetic-provider-request"}


# 构造 completed Responses 对象和固定用量，便于检查网关是否原样保留并正确统计。
def response_data(output=None):
    return {"id": "resp_synthetic", "object": "response", "model": "synthetic-model",
            "status": "completed", "output": output if output is not None else [
                {"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
            "usage": {"input_tokens": 100, "output_tokens": 10,
                      "input_tokens_details": {"cached_tokens": 80}, "total_tokens": 110}}


# 记录收到的请求并返回固定响应，替代真实网络传输。
class Opener:
    # 初始化本用例替身的记录容器和可选响应，不建立真实连接。
    def __init__(self, responder=None):
        self.requests = []
        self.responder = responder

    # 记录请求及可选超时参数后返回固定字节响应，网络行为完全由替身控制。
    def open(self, request, timeout):
        self.requests.append(request)
        return self.responder() if self.responder else Response(json.dumps(response_data()).encode())


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class ResponsesGatewayTests(unittest.TestCase):
    # 每个用例建立自己的临时材料和目录，避免前一个用例状态或已有实验记录影响断言。
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    # 清理本用例创建的临时目录，保留仓库原有实验和日志。
    def tearDown(self):
        self.temp.cleanup()

    # 创建使用虚构凭据和伪上游的 Responses 网关，测试参数可覆盖预算限制。
    def gateway(self, opener=None, **kwargs):
        return ResponsesGateway(self.root, BASE, SECRET, opener=opener or Opener(), **kwargs)

    # 核对 Responses 请求原文、标签、模型和 token 用量，并确认只有转发认证头持有虚构密钥。
    def test_json_auth_labels_request_body_and_usage_preserved(self):
        opener = Opener()
        gateway = self.gateway(opener)
        result = gateway.post(PAYLOAD, "trajectory/probe")
        self.assertEqual(result, response_data())
        event = gateway.events()[0]
        self.assertEqual(event["label"], "trajectory/probe")
        self.assertEqual(event["provider_request_id"], "synthetic-provider-request")
        self.assertEqual(event["returned_model"], "synthetic-model")
        self.assertEqual(json.loads(Path(event["request_path"]).read_bytes()), PAYLOAD)
        self.assertEqual(opener.requests[0].full_url, BASE + "/responses")
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer " + SECRET)
        self.assertEqual(opener.requests[0].get_header("User-agent"), "codex_cli_rs/0.165.0")
        self.assertEqual(gateway.accounting()["total_input_tokens"], 100)
        self.assertEqual(gateway.accounting()["output_tokens"], 10)
        self.assertEqual(response_object(event), result)
        self.assertEqual(gateway.response_object(event), result)
        self.assertEqual(gateway.accounting()["reserved_input_tokens"], 0)
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes())

    # compact 返回的加密状态与原始流逐字保存；只统计已返回用量，不推断其内部内容。
    def test_sse_complete_response_and_opaque_compaction_are_unchanged(self):
        opaque = "synthetic-opaque-state=="
        obj = response_data([{ "type": "compaction", "encrypted_content": opaque}])
        body = ('event: response.created\ndata: {"type":"response.created","response":{"id":"r"}}\n\n'
                'event: response.completed\ndata: ' + json.dumps({"type": "response.completed", "response": obj})
                + '\n\ndata: [DONE]\n\n').encode()
        gateway = self.gateway(Opener(lambda: Response(body, "text/event-stream")))
        result = gateway.post({**PAYLOAD, "stream": True}, "native/round1", compact=True)
        self.assertEqual(result["output"][0]["encrypted_content"], opaque)
        event = gateway.events()[0]
        self.assertEqual(Path(event["response_path"]).read_bytes(), body)
        self.assertEqual(event["path"], "/v1/responses/compact")
        self.assertEqual(event["usage"]["input_tokens_details"]["cached_tokens"], 80)
        self.assertEqual(gateway.accounting()["input_tokens"], 100)

    # 上游即使 HTTP 200，只要输出宿主工具动作也必须阻断，同时保留原响应作为诊断证据。
    def test_function_and_custom_tools_blocked_but_provider_artifact_kept(self):
        for kind in ("function_call", "custom_tool_call", "computer_call", "shell_call", "future_action"):
            with self.subTest(kind=kind):
                folder = self.root / kind
                original = response_data([{"type": kind, "name": "forbidden", "arguments": "{}"}])
                gateway = ResponsesGateway(folder, BASE, SECRET, opener=Opener(
                    lambda: Response(json.dumps(original).encode())))
                with self.assertRaisesRegex(ResponsesGatewayError, "HTTP 400"):
                    gateway.post(PAYLOAD, "blocked")
                event = gateway.events()[0]
                self.assertTrue(event["tool_blocked"])
                self.assertEqual(event["upstream_status"], 200)
                self.assertEqual(event["status"], 400)
                self.assertEqual(response_object(event), original)

    # 危险工具项可能只在早期流事件出现；不能仅检查最终 output 而漏过它。
    def test_sse_early_tool_item_is_blocked_even_if_final_output_omits_it(self):
        data = [{"type": "response.output_item.added", "item": {"type": "function_call", "name": "Bash"}},
                {"type": "response.completed", "response": response_data()}]
        body = "".join("data: " + json.dumps(item) + "\n\n" for item in data).encode()
        gateway = self.gateway(Opener(lambda: Response(body, "text/event-stream")))
        with self.assertRaises(ResponsesGatewayError):
            gateway.post({**PAYLOAD, "stream": True}, "tool-stream")
        self.assertEqual(gateway.events()[0]["blocked_output_types"], ["function_call"])

    # 真实回环入口应替换陈旧 key、保留允许的头，并拒绝查询跳转、其他路由和路径穿越。
    def test_http_label_auth_override_and_path_allowlist(self):
        opener = Opener()
        gateway = self.gateway(opener)
        gateway.label = "set-by-runner/turn1"
        base = gateway.start()
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            req = urllib.request.Request(base + "/responses", data=json.dumps(PAYLOAD).encode(),
                                         headers={"Authorization": "Bearer stale-test-key", "OpenAI-Beta": "responses-test",
                                                  "User-Agent": "synthetic-native-client/1.2"})
            with local.open(req) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(gateway.events()[0]["label"], gateway.label)
            self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer " + SECRET)
            self.assertIsNone(opener.requests[0].get_header("X-lab-label"))
            self.assertEqual(opener.requests[0].get_header("Openai-beta"), "responses-test")
            self.assertEqual(opener.requests[0].get_header("User-agent"), "synthetic-native-client/1.2")
            for suffix in ("/responses?target=evil", "/responses/other", "/../responses"):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    local.open(urllib.request.Request(base + suffix, data=b"{}"))
                self.assertEqual(caught.exception.code, 404)
            self.assertEqual(len(opener.requests), 1)
        finally:
            gateway.close()

    # 分别触发输入、输出和请求数上限，保护触发后后续小请求也不得继续。
    def test_input_output_and_request_guards_are_sticky(self):
        cases = [({"max_input_tokens": 1}, "input_tokens"),
                 ({"max_output_tokens": 1}, "output_tokens")]
        for index, (limits, reason) in enumerate(cases):
            with self.subTest(reason=reason):
                opener = Opener()
                gateway = ResponsesGateway(self.root / str(index), BASE, SECRET, opener=opener, **limits)
                with self.assertRaises(ResponsesBudgetExceeded):
                    gateway.post(PAYLOAD, "guard")
                self.assertEqual(gateway.accounting()["guard_reason"], reason)
                self.assertTrue(gateway.accounting()["guard_exhausted"])
                with self.assertRaises(ResponsesBudgetExceeded):
                    gateway.post({"model": "x", "input": "", "max_output_tokens": 1}, "retry")
                self.assertEqual(opener.requests, [])
        gateway = self.gateway(max_requests=1)
        gateway.post(PAYLOAD, "first")
        with self.assertRaises(ResponsesBudgetExceeded):
            gateway.post(PAYLOAD, "second")
        self.assertEqual(gateway.accounting()["guard_reason"], "request_count")

    # 未声明输出上限时也需要默认预留；超过配置限额应由本地 HTTP 入口明确拒绝。
    def test_default_output_reservation_and_http_guard_returns_400(self):
        gateway = self.gateway(max_output_tokens=100)
        base = gateway.start()
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            req = urllib.request.Request(base + "/responses", data=b'{"model":"x","input":"test"}')
            with self.assertRaises(urllib.error.HTTPError) as caught:
                local.open(req)
            self.assertEqual(caught.exception.code, 400)
            self.assertTrue(gateway.accounting()["guard_exhausted"])
        finally:
            gateway.close()

    # 两请求并发时必须把尚未返回的输入预留计入上限，防止仅统计已完成用量而超支。
    def test_concurrent_reservations_cannot_exceed_input_guard(self):
        entered, release = threading.Event(), threading.Event()

        # 让首请求暂时挂起以保持预算预留，人工构造并发竞争条件。
        class BlockedOpener(Opener):
            # 通知主测试首请求已进入，等待释放信号后才返回，使第二请求能够撞上尚未结算的预留。
            def open(self, request, timeout):
                entered.set()
                release.wait(2)
                return super().open(request, timeout)

        # One request has about 120 UTF-8 bytes; two reservations exceed 200.
        gateway = self.gateway(BlockedOpener(), max_input_tokens=200)
        failures = []

        # 在线程中发起一个伪上游请求并收集异常，便于主线程判断预算竞争是否正确处理。
        def send():
            try:
                gateway.post(PAYLOAD, "first")
            except Exception as exc:
                failures.append(exc)

        # 事件同步保证首请求仍占用输入预留，此时第二请求应被本地上限拦住。
        worker = threading.Thread(target=send)
        worker.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(ResponsesBudgetExceeded):
            gateway.post(PAYLOAD, "second")
        release.set()
        worker.join(2)
        self.assertEqual(failures, [])
        self.assertEqual(gateway.accounting()["reserved_input_tokens"], 0)
        with self.assertRaises(ResponsesBudgetExceeded):
            gateway.post(PAYLOAD, "after-release")

    # 供应商回显 key 或异常消息带 key 时都要脱敏，且日志保留发生脱敏/传输失败的标记。
    def test_echoed_credential_is_redacted_and_transport_errors_do_not_leak(self):
        original = response_data([{ "type": "message", "content": [{"type": "output_text", "text": SECRET}]}])
        gateway = self.gateway(Opener(lambda: Response(json.dumps(original).encode())))
        result = gateway.post(PAYLOAD, "redact")
        self.assertEqual(result["output"][0]["content"][0]["text"], "[REDACTED]")
        self.assertTrue(gateway.events()[0]["response_redacted"])
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes())

        # 故意抛出含虚构密钥的异常，验证错误日志不会泄露认证内容。
        class BrokenOpener:
            # 模拟传输异常并把虚构密钥放入异常文本，检验网关是否安全处理错误。
            def open(self, *_args, **_kwargs):
                raise OSError(SECRET)

        other = ResponsesGateway(self.root / "broken", BASE, SECRET, opener=BrokenOpener())
        with self.assertRaisesRegex(ResponsesGatewayError, "HTTP 502"):
            other.post(PAYLOAD, "transport-failure")
        self.assertEqual(other.events()[0]["transport_error_type"], "OSError")

    # 拒绝含凭据正文、非法 schema、不安全供应商地址和非空旧目录，防止泄密与覆盖原实验。
    def test_bad_credentials_payload_and_existing_directory_are_rejected(self):
        gateway = self.gateway()
        for payload in ({**PAYLOAD, "input": SECRET}, {**PAYLOAD, "input": {}},
                        {**PAYLOAD, "max_output_tokens": True}):
            with self.assertRaises(ResponsesGatewayError):
                gateway.post(payload, "bad")
        self.assertEqual(gateway.events(), [])
        gateway.post(PAYLOAD, "recorded")
        with self.assertRaises(ResponsesGatewayError):
            self.gateway()
        for base in ("http://provider.example.invalid/v1", "https://name:password@provider.invalid/v1",
                     "https://provider.invalid/v1?redirect=other"):
            with self.assertRaises(ValueError):
                ResponsesGateway(self.root / "unused", base, SECRET)

    # 流只有 created 没有 completed 时记录解析失败，不能自动补成成功模型响应。
    def test_incomplete_sse_not_invented_as_complete_response(self):
        body = b'data: {"type":"response.created","response":{"id":"pending"}}\n\n'
        gateway = self.gateway(Opener(lambda: Response(body, "text/event-stream")))
        with self.assertRaisesRegex(ResponsesGatewayError, "HTTP 502"):
            gateway.post({**PAYLOAD, "stream": True}, "incomplete")
        self.assertTrue(gateway.events()[0]["response_parse_error"])


if __name__ == "__main__":
    unittest.main()
