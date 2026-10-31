# 旧 Claude Code/DeepSeek 实验网关，保留给历史实验复现与测试使用。
# 只将固定 Anthropic messages 请求转发到代码中的 DeepSeek 端点；与 Codex 的 Responses 网关分开。
# 这里的美元统计是按固定价格作出的保守估计，不能用于当前 sub2api 账单。
"""Loopback-only, auditable forwarding to the official DeepSeek Anthropic API.

The gateway changes neither native request bodies nor successful response bodies.
It stores no request headers.  A test opener may be injected; production uses an
opener with environment proxies disabled, without changing the parent environment.
Costs are conservative estimates, not provider billing statements.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


UPSTREAM = "https://api.deepseek.com/anthropic/v1/messages"
INPUT_USD_PER_TOKEN = 0.3 / 1_000_000
OUTPUT_USD_PER_TOKEN = 1.2 / 1_000_000
ALLOWED_PATHS = {"/v1/messages", "/v1/messages?beta=true"}


# 已控制错误内容的网关异常；调用层不能把它当作安全拒绝或遗忘。
class GatewayError(RuntimeError):
    """An audited gateway failure; exception messages never contain credentials."""


# 表示预算或请求数停止保护触发，避免与普通服务错误混淆。
class BudgetExceeded(GatewayError):
    pass


# 读取 JSON/SSE 响应的模型名称与用量；SSE 中后到的累计字段覆盖先到的值。
# 返回 usage 和 model，不把逐事件累计值相加，以免重复计算。
def response_metadata(body: bytes, content_type: str) -> tuple[dict[str, Any], str | None]:
    """Merge cumulative Anthropic usage fields from JSON or SSE responses."""
    usage: dict[str, Any] = {}
    model = None
    if "event-stream" in content_type:
        values = []
        for line in body.decode("utf-8", errors="replace").splitlines():
            if line.startswith("data:"):
                try:
                    value = json.loads(line[5:].strip())
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict):
                    values.append(value)
    else:
        try:
            value = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            value = {}
        values = [value] if isinstance(value, dict) else []
    for value in values:
        message = value.get("message", {})
        if not isinstance(message, dict):
            message = {}
        model = value.get("model") or message.get("model") or model
        for candidate in (message.get("usage"), value.get("usage")):
            if isinstance(candidate, dict):
                for key, count in candidate.items():
                    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                        usage[key] = count
    return usage, model


# 按全部输入都未命中缓存估计费用；缺少输入或输出用量时返回 None。
# 该估计仅用于停止保护，不表示服务商真实计费。
def usage_cost(usage: dict[str, Any]) -> float | None:
    """Upper estimate treating all reported input, including cached input, as miss."""
    if "input_tokens" not in usage or "output_tokens" not in usage:
        return None
    total_input = sum(usage.get(key, 0) for key in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"
    ))
    return total_input * INPUT_USD_PER_TOKEN + usage["output_tokens"] * OUTPUT_USD_PER_TOKEN


# 原子替换元数据文件，便于进度查询与并发写入时读取完整内容。
def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


# 旧 messages 协议的回环网关，集中保留请求/响应、请求编号和预算账本。
class RecordingGateway:
    # 创建记录目录和线程锁，初始化已花费与在途预留。
    # 恢复旧目录时，将未完成请求也按预留金额计入，防止中断后漏算开销。
    def __init__(self, root: Path, api_key: str, budget_usd: float = 50,
                 max_requests: int = 5000, *, opener: Any = None):
        if not api_key or budget_usd <= 0 or max_requests < 1:
            raise ValueError("A credential, positive budget, and positive request limit are required")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._api_key = api_key
        self._secret_bytes = api_key.encode("utf-8")
        self.budget_usd = float(budget_usd)
        self.max_requests = max_requests
        # 此 opener 忽略环境代理，只影响实验请求；与系统网络配置和父进程无关。
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._reserved_usd = 0.0
        self._spent_usd = 0.0
        self._next_idx = 1
        self._guard_exhausted = False
        self._guard_reason: str | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # Restore both completed and interrupted reservations conservatively.
        for path in sorted(self.root.glob("[0-9]*/meta.json")):
            event = json.loads(path.read_text(encoding="utf-8"))
            self._events.append(event)
            self._next_idx = max(self._next_idx, event["idx"] + 1)
            self._spent_usd += event.get("estimated_usd", event.get("reserved_usd", 0))

    # 返回事件深拷贝并按请求编号排序，供轨迹匹配真实请求。
    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(sorted(self._events, key=lambda event: event["idx"]))

    # 返回预算、请求数量与保护状态快照；并发访问受同一锁保护。
    def accounting(self) -> dict[str, Any]:
        with self._lock:
            return {"estimated_usd": self._spent_usd, "reserved_usd": self._reserved_usd,
                    "budget_usd": self.budget_usd, "requests_started": self._next_idx - 1,
                    "max_requests": self.max_requests,
                    "guard_exhausted": self._guard_exhausted,
                    "guard_reason": self._guard_reason}

    # 检查请求格式和密钥泄露，再按正文长度及 max_tokens 预留费用。
    # 不截断材料来适应窗口；超长请求应保留为失败，不能悄悄改变实验输入。
    def _reserve(self, raw: bytes, label: str, path: str) -> tuple[Path, dict[str, Any]]:
        if self._secret_bytes in raw or self._api_key in label:
            raise GatewayError("Credential unexpectedly present in payload or label; request not recorded")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise GatewayError("Request must be valid UTF-8 JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            raise GatewayError("Request must contain a messages array")
        maximum = payload.get("max_tokens")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 1_000_000:
            raise GatewayError("max_tokens must be an integer between 1 and 1000000")
        # UTF-8 bytes exceed token count for this text-only experiment.  We do not
        # truncate payloads: over-context provider errors are retained as failures.
        # 按正文字节数和请求输出上限提前预留；并发请求要把其他在途预留一并计算。
        reserved = len(raw) * INPUT_USD_PER_TOKEN + maximum * OUTPUT_USD_PER_TOKEN
        with self._lock:
            if self._guard_exhausted:
                raise BudgetExceeded("Gateway sticky experiment guard already reached")
            if self._next_idx > self.max_requests:
                self._guard_exhausted = True
                self._guard_reason = "request_count"
                raise BudgetExceeded("Gateway request-count guard reached")
            if self._spent_usd + self._reserved_usd + reserved > self.budget_usd:
                self._guard_exhausted = True
                self._guard_reason = "cost"
                raise BudgetExceeded("Gateway conservative cost guard reached")
            idx = self._next_idx
            self._next_idx += 1
            self._reserved_usd += reserved
            folder = self.root / f"{idx:06d}"
            folder.mkdir()
            (folder / "request.json").write_bytes(raw)
            event = {"idx": idx, "label": label, "path": path, "status": "in_flight",
                     "started_at_unix": time.time(), "requested_model": payload.get("model"),
                     "request_sha256": hashlib.sha256(raw).hexdigest(),
                     "request_bytes": len(raw), "reserved_usd": reserved,
                     "cost_basis": "all_input_cache_miss_upper_bound",
                     "request_file": str(folder / "request.json"),
                     "request_path": str(folder / "request.json")}
            _write_json(folder / "meta.json", event)
            return folder, event

    # 转发到固定 DeepSeek 端点并记录脱敏后的原始正文、状态及哈希。
    # 只有白名单协议头会被保留，客户端的认证头不会传到上游或写入日志。
    def _forward(self, raw: bytes, label: str, path: str, timeout: float,
                 incoming_headers: Any = None) -> tuple[int, str, bytes]:
        if path not in ALLOWED_PATHS:
            raise GatewayError("Unsupported gateway path")
        folder, event = self._reserve(raw, label, path)
        headers = {"content-type": "application/json", "x-api-key": self._api_key,
                   "anthropic-version": "2023-06-01", "accept": "application/json, text/event-stream"}
        # Only protocol fields cross this boundary; native Authorization and
        # x-api-key values are deliberately ignored rather than copied or logged.
        if incoming_headers:
            for name in ("anthropic-version", "anthropic-beta"):
                value = incoming_headers.get(name)
                if value and self._api_key not in value:
                    headers[name] = value
        query = "?beta=true" if path.endswith("?beta=true") else ""
        request = urllib.request.Request(UPSTREAM + query, data=raw, headers=headers, method="POST")
        status = 502
        content_type = "application/json"
        body = b'{"error":{"type":"gateway_error","message":"Upstream request failed"}}'
        request_id = None
        try:
            try:
                response = self._opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = response.status
                content_type = response.headers.get("content-type", "application/json")
                request_id = response.headers.get("request-id") or response.headers.get("x-request-id")
                body = response.read()
        except Exception as error:
            # Do not stringify exceptions: urllib exceptions can embed headers,
            # URLs, or credential-bearing upstream error objects.
            event["transport_error_type"] = type(error).__name__
        if self._secret_bytes in body:
            body = body.replace(self._secret_bytes, b"[REDACTED]")
            event["response_redacted"] = True
        if request_id and self._api_key in request_id:
            request_id = "[REDACTED]"
        # 即便 HTTP 失败也保留响应和费用估计；缺失 usage 不能算免费请求。
        usage, model = response_metadata(body, content_type)
        cost = usage_cost(usage)
        event.update({"status": status, "completed_at_unix": time.time(), "usage": usage,
                      "returned_model": model, "provider_request_id": request_id,
                      "response_content_type": content_type,
                      "estimated_usd": cost if cost is not None else event["reserved_usd"],
                      "cost_imputed_from_reservation": cost is None,
                      "response_sha256": hashlib.sha256(body).hexdigest()})
        suffix = "sse" if "event-stream" in content_type else "json"
        response_path = folder / f"response.{suffix}"
        response_path.write_bytes(body)
        event["response_file"] = str(response_path)
        event["response_path"] = str(response_path)
        with self._lock:
            self._reserved_usd -= event["reserved_usd"]
            self._spent_usd += event["estimated_usd"]
            _write_json(folder / "meta.json", event)
            with (self.root / "requests.jsonl").open("a", encoding="utf-8") as ledger:
                ledger.write(json.dumps(event, ensure_ascii=False) + "\n")
                ledger.flush()
            self._events.append(copy.deepcopy(event))
        return status, content_type, body

    # 给独立探针发送非流式 JSON 请求；输出不回灌 Claude Code 主会话。
    # HTTP 错误或非 JSON 正文显式报错，不能当作探针答案。
    def post(self, payload: dict[str, Any], label: str, timeout: float = 600) -> dict[str, Any]:
        """Record a detached request and return its JSON response, never a parent turn."""
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        status, _content_type, body = self._forward(raw, label, "/v1/messages", timeout)
        if status < 200 or status >= 300:
            raise GatewayError(f"Upstream returned HTTP {status}; inspect recorded response")
        try:
            value = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise GatewayError("Detached request did not return valid JSON; inspect recorded response") from None
        if not isinstance(value, dict):
            raise GatewayError("Detached response is not a JSON object")
        return value

    # 在回环临时端口启动网关，返回实验专用 base URL。
    # 该启动方式只改变子进程环境，不修改全局网络或服务配置。
    def start(self) -> str:
        """Return an ephemeral loopback base URL; no global configuration changes."""
        if self._server is not None:
            host, port = self._server.server_address
            return f"http://{host}:{port}"
        owner = self

        # 最小 HTTP 适配层，只接受固定 messages 路径。
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            # 禁用默认访问日志，审计信息统一写入实验目录。
            def log_message(self, _format: str, *args: Any) -> None:
                pass

            # 写出固定长度响应并关闭连接；容忍调用方已断开连接。
            def _send(self, status: int, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True

            # 检查路径和正文大小后转发；预算用 429、请求错误用 400 单独表示。
            # 此处没有通用工具或任意 URL 转发接口。
            def do_POST(self) -> None:
                if self.path not in ALLOWED_PATHS:
                    self._send(404, "application/json", b'{"error":"unsupported gateway path"}')
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 32_000_000:
                        raise GatewayError("Invalid request size")
                    raw = self.rfile.read(length)
                    label = self.headers.get("X-Lab-Label", "native")[:256]
                    status, content_type, body = owner._forward(
                        raw, label, self.path, timeout=600, incoming_headers=self.headers)
                except BudgetExceeded:
                    status, content_type, body = 429, "application/json", b'{"error":"experiment budget guard reached"}'
                except GatewayError:
                    status, content_type, body = 400, "application/json", b'{"error":"invalid gateway request"}'
                self._send(status, content_type, body)

            # 拒绝 GET，不提供额外网页或文件读取能力。
            def do_GET(self) -> None:
                self._send(404, "application/json", b'{"error":"unsupported gateway path"}')

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True,
                                        name="native-retention-gateway")
        self._thread.start()
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    # 关闭监听和线程，保留全部实验产物以便离线核验。
    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
