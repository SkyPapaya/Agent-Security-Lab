# 测试兼容 Messages 协议的记录网关。上游是本地伪响应；部分测试仅启动回环 HTTP 服务，验证转发、脱敏和预算保护。
# 阅读顺序：先看每个测试上方的条件和目的，再对照实际断言；固定回复只用于离线单测。
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from experiments.llama_cpp.compaction_security.native_retention.gateway import (
    BudgetExceeded, GatewayError, RecordingGateway, UPSTREAM, response_metadata,
)


PAYLOAD = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "测试"}],
           "max_tokens": 64}
SECRET = "fictional-unit-test-credential"


# 用内存字节流模拟上游响应的状态和响应头，测试不会通过它访问远端。
class Response(io.BytesIO):
    # 初始化可读取的响应字节、状态码和头信息，让网关按真实接口解析伪响应。
    def __init__(self, body, content_type="application/json", status=200):
        super().__init__(body)
        self.status = status
        self.headers = {"content-type": content_type, "request-id": "fictional-request-1"}


# 记录收到的请求并返回固定响应，替代真实网络传输。
class Opener:
    # 初始化本用例替身的记录容器和可选响应，不建立真实连接。
    def __init__(self, response=None):
        self.requests = []
        self.response = response

    # 记录请求及可选超时参数后返回固定字节响应，网络行为完全由替身控制。
    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.response is not None:
            return self.response()
        return Response(json.dumps({"model": "deepseek-flash", "content": [{"type": "text", "text": "OK"}],
                                    "usage": {"input_tokens": 32, "output_tokens": 4}}).encode())


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class GatewayTests(unittest.TestCase):
    # 每个用例建立自己的临时材料和目录，避免前一个用例状态或已有实验记录影响断言。
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    # 清理本用例创建的临时目录，保留仓库原有实验和日志。
    def tearDown(self):
        self.temp.cleanup()

    # 离线发出带标签的探针，核对输入、返回模型和用量均被记录，但虚构密钥不能进入任何日志。
    def test_detached_request_audited_secret_not_logged(self):
        opener = Opener()
        gateway = RecordingGateway(self.root, SECRET, opener=opener)
        result = gateway.post(PAYLOAD, "trajectory-1/probe-0")
        self.assertEqual(result["model"], "deepseek-flash")
        event = gateway.events()[0]
        self.assertEqual(event["label"], "trajectory-1/probe-0")
        self.assertEqual(event["usage"], {"input_tokens": 32, "output_tokens": 4})
        self.assertEqual(event["returned_model"], "deepseek-flash")
        self.assertFalse(event["cost_imputed_from_reservation"])
        self.assertEqual(json.loads(Path(event["request_path"]).read_bytes()), PAYLOAD)
        request = opener.requests[0][0]
        self.assertEqual(request.full_url, UPSTREAM)
        self.assertEqual(request.get_header("X-api-key"), SECRET)
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes())

    # 流中输出 token 是累计值，不能逐事件相加；原始 SSE 字节也必须不变，便于复核。
    def test_sse_usage_is_cumulative_and_body_unchanged(self):
        body = (b'event: message_start\ndata: {"type":"message_start","message":{"model":"deepseek-flash",'
                b'"usage":{"input_tokens":100,"output_tokens":1,"cache_read_input_tokens":40}}}\n\n'
                b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":15}}\n\n'
                b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
        opener = Opener(lambda: Response(body, "text/event-stream; charset=utf-8"))
        gateway = RecordingGateway(self.root, SECRET, opener=opener)
        status, content_type, received = gateway._forward(json.dumps(PAYLOAD).encode(), "native", "/v1/messages", 10)
        self.assertEqual(status, 200)
        self.assertEqual(received, body)
        self.assertIn("event-stream", content_type)
        event = gateway.events()[0]
        self.assertEqual(event["usage"]["output_tokens"], 15)
        self.assertEqual(event["usage"]["cache_read_input_tokens"], 40)
        self.assertAlmostEqual(event["estimated_usd"], 140 * .3e-6 + 15 * 1.2e-6)
        self.assertEqual(Path(event["response_path"]).read_bytes(), body)

    # 回环网关只转发白名单路径，覆盖陈旧认证、保留允许的协议头并移除实验标签头。
    def test_http_gateway_label_protocol_and_path_allowlist(self):
        opener = Opener()
        gateway = RecordingGateway(self.root, SECRET, opener=opener)
        base = gateway.start()
        local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            request = urllib.request.Request(base + "/v1/messages?beta=true", data=json.dumps(PAYLOAD).encode(),
                                             headers={"Authorization": "Bearer fictional-stale-key",
                                                      "X-api-key": "fictional-stale-key",
                                                      "Anthropic-beta": "fictional-feature",
                                                      "X-Lab-Label": "history_01/turn_004"})
            with local_opener.open(request) as response:
                self.assertEqual(response.status, 200)
            event = gateway.events()[0]
            self.assertEqual(event["label"], "history_01/turn_004")
            forwarded = opener.requests[0][0]
            self.assertEqual(forwarded.full_url, UPSTREAM + "?beta=true")
            self.assertIsNone(forwarded.get_header("Authorization"))
            self.assertIsNone(forwarded.get_header("X-lab-label"))
            self.assertEqual(forwarded.get_header("X-api-key"), SECRET)
            self.assertEqual(forwarded.get_header("Anthropic-beta"), "fictional-feature")
            bad = urllib.request.Request(base + "/v1/messages?redirect=https://example.invalid", data=b"{}")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                local_opener.open(bad)
            self.assertEqual(caught.exception.code, 404)
            self.assertEqual(len(opener.requests), 1)
        finally:
            gateway.close()

    # 让首请求阻塞以保留预算，再发第二请求验证原子预留；预算一旦触发，释放预留后也不自动恢复。
    def test_concurrent_reservations_cannot_overspend(self):
        entered, release = threading.Event(), threading.Event()

        # 让首请求暂时挂起以保持预算预留，人工构造并发竞争条件。
        class BlockingOpener(Opener):
            # 通知主测试首请求已进入，等待释放信号后才返回，使第二请求能够撞上尚未结算的预留。
            def open(inner, request, timeout):
                entered.set()
                release.wait(2)
                return super().open(request, timeout)

        # Each request reserves >$0.00010; budget permits one but not two.
        gateway = RecordingGateway(self.root, SECRET, budget_usd=.00015, opener=BlockingOpener())
        errors = []

        # 在线程中发起一个伪上游请求并收集异常，便于主线程判断预算竞争是否正确处理。
        def send():
            try:
                gateway.post(PAYLOAD, "first")
            except Exception as error:
                errors.append(error)

        # 先确保首请求已经占用预留，再发第二请求；不能依赖不稳定的线程启动先后。
        worker = threading.Thread(target=send)
        worker.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(BudgetExceeded):
            gateway.post(PAYLOAD, "second")
        self.assertTrue(gateway.accounting()["guard_exhausted"])
        self.assertEqual(gateway.accounting()["guard_reason"], "cost")
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(gateway.events()), 1)
        self.assertAlmostEqual(gateway.accounting()["reserved_usd"], 0)
        # Completion releases its reservation, but the stop guard stays sticky.
        with self.assertRaises(BudgetExceeded):
            gateway.post({**PAYLOAD, "max_tokens": 1}, "after-release")

    # 请求数到上限后停止，重新打开账本仍要继承已用量与保护状态，不能靠重启绕过。
    def test_count_guard_and_resumed_cost_ledger(self):
        gateway = RecordingGateway(self.root, SECRET, max_requests=1, opener=Opener())
        gateway.post(PAYLOAD, "first")
        with self.assertRaises(BudgetExceeded):
            gateway.post(PAYLOAD, "second")
        self.assertTrue(gateway.accounting()["guard_exhausted"])
        self.assertEqual(gateway.accounting()["guard_reason"], "request_count")
        resumed = RecordingGateway(self.root, SECRET, max_requests=1, opener=Opener())
        self.assertEqual(resumed.accounting()["estimated_usd"], gateway.accounting()["estimated_usd"])
        self.assertEqual(resumed.accounting()["requests_started"], gateway.accounting()["requests_started"])
        self.assertEqual(resumed.events(), gateway.events())
        with self.assertRaises(BudgetExceeded):
            resumed.post(PAYLOAD, "resumed")

    # 通过回环 HTTP 入口触发预算拒绝，检查全局停止标志可见且没有请求到达伪上游。
    def test_native_http_budget_rejection_sets_visible_global_guard(self):
        opener = Opener()
        gateway = RecordingGateway(self.root, SECRET, budget_usd=.000001, opener=opener)
        self.assertFalse(gateway.accounting()["guard_exhausted"])
        self.assertIsNone(gateway.accounting()["guard_reason"])
        base = gateway.start()
        local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            request = urllib.request.Request(base + "/v1/messages", data=json.dumps(PAYLOAD).encode())
            with self.assertRaises(urllib.error.HTTPError) as caught:
                local_opener.open(request)
            self.assertEqual(caught.exception.code, 429)
            self.assertTrue(gateway.accounting()["guard_exhausted"])
            self.assertEqual(gateway.accounting()["guard_reason"], "cost")
            self.assertEqual(opener.requests, [])
            self.assertEqual(gateway.events(), [])
        finally:
            gateway.close()

    # 伪上游抛出含密钥的异常，日志只保留安全的错误类型，并以预留量记录未知成本。
    def test_transport_failure_audited_without_exception_secrets(self):
        # 故意抛出含虚构密钥的异常，验证错误日志不会泄露认证内容。
        class FailedOpener:
            # 模拟传输异常并把虚构密钥放入异常文本，检验网关是否安全处理错误。
            def open(self, request, timeout):
                raise OSError("sensitive credential " + SECRET)

        gateway = RecordingGateway(self.root, SECRET, opener=FailedOpener())
        with self.assertRaises(GatewayError):
            gateway.post(PAYLOAD, "failure")
        event = gateway.events()[0]
        self.assertEqual(event["status"], 502)
        self.assertTrue(event["cost_imputed_from_reservation"])
        self.assertEqual(event["estimated_usd"], event["reserved_usd"])
        self.assertEqual(event["transport_error_type"], "OSError")
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes())

    # 请求正文意外包含凭据或输出限额非法时，必须在转发和日志落盘前拒绝。
    def test_bad_payload_and_credential_are_never_dispatched(self):
        opener = Opener()
        gateway = RecordingGateway(self.root, SECRET, opener=opener)
        with self.assertRaises(GatewayError):
            gateway.post({**PAYLOAD, "messages": [{"role": "user", "content": SECRET}]}, "bad")
        with self.assertRaises(GatewayError):
            gateway.post({**PAYLOAD, "max_tokens": 1_000_001}, "bad")
        self.assertEqual(opener.requests, [])
        self.assertEqual(list(self.root.iterdir()), [])

    # 上游 HTTP 错误保留真实状态，无效 JSON 不能构造成正常模型回复。
    def test_http_error_and_invalid_json_separate_from_model_success(self):
        # 构造供应商 HTTP 错误响应，检查它不会被当成模型成功。
        class ErrorOpener:
            # 抛出带固定错误体的 HTTPError，验证真实状态码和失败类型可追溯。
            def open(self, request, timeout):
                raise urllib.error.HTTPError(UPSTREAM, 422, "invalid input", {"content-type": "application/json"},
                                             io.BytesIO(b'{"error":{"type":"invalid_request_error"}}'))

        gateway = RecordingGateway(self.root, SECRET, opener=ErrorOpener())
        with self.assertRaisesRegex(GatewayError, "HTTP 422"):
            gateway.post(PAYLOAD, "provider-failed")
        self.assertEqual(gateway.events()[0]["status"], 422)
        self.assertEqual(response_metadata(b"not JSON", "application/json"), ({}, None))


if __name__ == "__main__":
    unittest.main()
