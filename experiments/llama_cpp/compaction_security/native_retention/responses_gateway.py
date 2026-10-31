# Codex/Responses 实验网关：接住本地请求，原样记录正文，再转发给指定 HTTPS 推理线路。
# 网关本身不执行工具；若上游返回工具调用项，会保存原始证据但阻止它进入 Codex。
# 请求数量和 token 上限是停止保护，不是账单承诺；密钥不写入请求日志。
"""Transparent, local Responses recorder with independent tool execution denial.

Requests go only to the constructor's HTTPS provider.  Credentials live in
memory and are injected as a bearer header; headers are never recorded.  The
recorder leaves successful JSON/SSE bodies and opaque compaction items intact,
except when redacting an accidentally echoed credential.  Each run needs a new
recording directory; existing ledgers are never overwritten or silently reset.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ALLOWED_PATHS = frozenset({"/v1/responses", "/v1/responses/compact"})
ALLOWED_OUTPUT_TYPES = frozenset({"message", "reasoning", "compaction"})
DEFAULT_OUTPUT_RESERVATION = 32768
DEFAULT_USER_AGENT = "codex_cli_rs/0.165.0"


# 统一网关异常，只保留经过控制的错误信息，避免异常字符串泄露凭据。
class ResponsesGatewayError(RuntimeError):
    """Audited error; messages contain neither credentials nor raw exceptions."""


# 单独表示请求/token 停止保护已触发，供编排器停止轨迹而非误计为模型失败。
class ResponsesBudgetExceeded(ResponsesGatewayError):
    pass


# Familiar names for callers that share generic error handling with the old lab.
GatewayError = ResponsesGatewayError
BudgetExceeded = ResponsesBudgetExceeded


# 先写临时文件再替换，减少监控端读到半份 meta.json 的机会。
def _json_write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


# 解析 JSON 或 SSE 正文，得到可审计的对象列表；不会改写原始响应文件。
# SSE 以空行分隔事件，同一事件的多行 data 合并后解析，非 JSON 段直接跳过。
def _values(body: bytes, content_type: str) -> list[dict[str, Any]]:
    text = body.decode("utf-8", errors="replace")
    if "event-stream" not in content_type and not text.lstrip().startswith(("event:", "data:")):
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            return []
        return [value] if isinstance(value, dict) else []
    result, pieces = [], []

    # 收集当前 SSE 事件中的完整 JSON 对象；残缺或非对象内容不伪造成有效响应。
    def accept():
        if not pieces:
            return
        try:
            value = json.loads("\n".join(pieces))
        except (ValueError, TypeError):
            return
        if isinstance(value, dict):
            result.append(value)

    for line in text.splitlines() + [""]:
        if not line:
            accept()
            pieces = []
        elif line.startswith("data:"):
            pieces.append(line[5:].lstrip(" "))
    return result


# 优先提取结束事件中的 response，否则接受直接返回的 Response 对象。
# 没有完整对象时返回空字典，调用层据此记录协议错误。
def _response_from_values(values: list[dict[str, Any]]) -> dict[str, Any]:
    terminal = None
    direct = None
    for value in values:
        if value.get("type") in {"response.completed", "response.incomplete", "response.failed", "response.done"}:
            if isinstance(value.get("response"), dict):
                terminal = value["response"]
        elif "output" in value or value.get("object") in {"response", "response.compaction"}:
            direct = value
    if terminal is not None:
        return terminal
    if direct is not None:
        return direct
    return {}


# 根据记录元数据读取原始响应文件，从 JSON/SSE 中还原完整 Response。
# 这是离线审计入口，能读取被网关拦住的上游输出；不等于输出曾被交给原生客户端。
def response_object(meta: dict[str, Any]) -> dict[str, Any]:
    """Load the provider response object from an audited JSON or SSE artifact.

    This audit helper can also inspect an output that the executor blocked;
    ``post()`` and the HTTP gateway never return that blocked output to Codex.
    """
    path = meta.get("response_path", meta.get("response_file"))
    if not path:
        raise ResponsesGatewayError("Recorded response artifact is unavailable")
    try:
        body = Path(path).read_bytes()
    except OSError:
        raise ResponsesGatewayError("Recorded response artifact could not be read") from None
    value = _response_from_values(_values(body, str(meta.get("response_content_type", "application/json"))))
    if not value:
        raise ResponsesGatewayError("No complete Responses object in recorded response")
    return value


# 只接收非负整数用量并排除 bool，避免不规范用量进入累计统计。
# 缓存细分仍单独保存，input_tokens 已含缓存部分，不能重复相加。
def _usage(response: dict[str, Any]) -> dict[str, Any]:
    value = response.get("usage")
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        count = value.get(key)
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            result[key] = count
    for key in ("input_tokens_details", "output_tokens_details"):
        detail = value.get(key)
        if isinstance(detail, dict):
            result[key] = {k: n for k, n in detail.items()
                           if isinstance(n, int) and not isinstance(n, bool) and n >= 0}
    return result


# 检查整段响应中的所有输出项，包括流中出现但最终响应省略的工具调用。
# 只放行普通文本、推理和压缩项，其他类型作为待阻断动作记录。
def _blocked_types(values: list[dict[str, Any]]) -> list[str]:
    """Reject action items before buffered responses reach the native client."""
    output_items = []
    for value in values:
        if isinstance(value.get("output"), list):
            output_items.extend(value["output"])
        response = value.get("response")
        if isinstance(response, dict) and isinstance(response.get("output"), list):
            output_items.extend(response["output"])
        if value.get("type") in {"response.output_item.added", "response.output_item.done"}:
            output_items.append(value.get("item"))
    blocked = set()
    for item in output_items:
        if not isinstance(item, dict):
            blocked.add("invalid_output_item")
            continue
        kind = item.get("type")
        if kind not in ALLOWED_OUTPUT_TYPES:
            blocked.add(str(kind or "missing_output_type"))
            continue
        if kind == "message":
            content = item.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") not in {"output_text", "refusal"}:
                        blocked.add(str(block.get("type") or "missing_content_type"))
    return sorted(blocked)


# 禁止上游重定向，避免携带 Authorization 的请求被转到其他位置。
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # 返回 None 让 urllib 不生成重定向请求；原始 HTTP 状态仍可记录。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must not carry authorization to another host or endpoint.
        return None


# 同时服务原生 Codex 请求与独立探针请求，共享审计账本和停止保护。
# 线程锁覆盖编号、在途预留和累计用量，防止并发请求绕过额度检查。
class ResponsesGateway:
    # 校验无凭据的 HTTPS 地址和正整数上限，创建本批记录器。
    # 拒绝复用已有账本目录，避免覆盖旧证据；测试可注入 opener，不必调用真实服务。
    def __init__(self, root: Path, base_url: str, api_key: str,
                 max_requests: int = 200, max_input_tokens: int = 2_000_000,
                 max_output_tokens: int = 200_000, *, opener: Any = None):
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("A provider credential is required")
        if not isinstance(base_url, str) or api_key in base_url:
            raise ValueError("A credential-free provider base URL is required")
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password \
                or parsed.query or parsed.fragment:
            raise ValueError("Provider base URL must be HTTPS without user info, query or fragment")
        for value in (max_requests, max_input_tokens, max_output_tokens):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("Request and token guards must be positive integers")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if (self.root / "requests.jsonl").exists() or any(self.root.glob("[0-9]*/meta.json")):
            raise ResponsesGatewayError("Recording directory already contains a run; use a new directory")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._secret = api_key.encode("utf-8")
        self.max_requests = max_requests
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.label = "native"
        # 显式禁用环境代理并拒绝重定向；不会改动调用者的系统代理配置。
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._next_idx = 1
        # 已结算用量与在途预留分开保存，避免多个并发请求同时看见尚未扣除的额度。
        self._input = self._output = self._reserved_input = self._reserved_output = 0
        self._guard_exhausted = False
        self._guard_reason = None
        self._server = None
        self._thread = None

    response_object = staticmethod(response_object)

    # 返回按请求编号排序的深拷贝，调用者筛选事件不会修改网关内部账本。
    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(sorted(self._events, key=lambda e: e["idx"]))

    # 提供已用量、在途预留和保护状态的快照，供进度条和停止逻辑读取。
    def accounting(self) -> dict[str, Any]:
        with self._lock:
            return {"requests": self._next_idx - 1, "requests_started": self._next_idx - 1,
                    "max_requests": self.max_requests,
                    "total_input_tokens": self._input, "input_tokens": self._input,
                    "output_tokens": self._output,
                    "reserved_input_tokens": self._reserved_input,
                    "reserved_output_tokens": self._reserved_output,
                    "max_input_tokens": self.max_input_tokens,
                    "max_output_tokens": self.max_output_tokens,
                    "guard_exhausted": self._guard_exhausted, "guard_reason": self._guard_reason,
                    "accounting_note": "Responses input_tokens includes cached tokens; no cache double count"}

    # 在请求外发前校验 JSON、检查正文/标签是否误含密钥，并预留输入与输出额度。
    # 预留成功后才分配目录和编号；一旦触发上限，保护保持生效，后续请求不能再次尝试。
    def _reserve(self, raw: bytes, label: str, path: str):
        if self._secret in raw or self._api_key in label:
            raise ResponsesGatewayError("Credential unexpectedly present in request data; not recorded")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ResponsesGatewayError("Request must be valid UTF-8 JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("input"), (list, str)):
            raise ResponsesGatewayError("Request must contain a Responses input list or string")
        maximum = payload.get("max_output_tokens", DEFAULT_OUTPUT_RESERVATION)
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 1_000_000:
            raise ResponsesGatewayError("max_output_tokens must be an integer from 1 to 1000000")
        # 在途输入按 UTF-8 正文长度作保守预留，并非精确 tokenizer 计数；完成后用上游 usage 结算。
        reserved_input = len(raw)
        with self._lock:
            reason = self._guard_reason
            if not self._guard_exhausted:
                if self._next_idx > self.max_requests:
                    reason = "request_count"
                elif self._input + self._reserved_input + reserved_input > self.max_input_tokens:
                    reason = "input_tokens"
                elif self._output + self._reserved_output + maximum > self.max_output_tokens:
                    reason = "output_tokens"
                if reason:
                    self._guard_exhausted, self._guard_reason = True, reason
            if self._guard_exhausted:
                raise ResponsesBudgetExceeded("Sticky experiment token/request guard reached")
            idx = self._next_idx
            self._next_idx += 1
            self._reserved_input += reserved_input
            self._reserved_output += maximum
            folder = self.root / f"{idx:06d}"
            folder.mkdir()
            request_path = folder / "request.json"
            request_path.write_bytes(raw)
            event = {"idx": idx, "label": label, "path": path, "status": "in_flight",
                     "started_at_unix": time.time(), "requested_model": payload.get("model"),
                     "request_bytes": len(raw), "request_sha256": hashlib.sha256(raw).hexdigest(),
                     "reserved_input_tokens": reserved_input, "reserved_output_tokens": maximum,
                     "request_path": str(request_path), "request_file": str(request_path)}
            _json_write(folder / "meta.json", event)
            return folder, event

    # 完成一个请求的预留、固定线路转发、脱敏、协议校验、落盘及用量结算。
    # 上游正文作为证据保存；发给客户端的正文可能因工具调用或协议错误而替换成拒绝信息。
    def _forward(self, raw: bytes, label: str, path: str, timeout: float = 600,
                 incoming_headers: Any = None):
        if path not in ALLOWED_PATHS:
            raise ResponsesGatewayError("Unsupported local Responses path")
        folder, event = self._reserve(raw, label, path)
        endpoint = self.base_url + ("/responses/compact" if path.endswith("/compact") else "/responses")
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self._api_key,
                   "Accept": "application/json, text/event-stream", "User-Agent": DEFAULT_USER_AGENT}
        # SDK-generated protocol beta headers can be necessary; host/auth/session
        # identity headers and arbitrary caller headers are never copied or logged.
        if incoming_headers:
            for name in ("User-Agent", "OpenAI-Beta", "Originator", "Version",
                         "X-Codex-Beta-Features", "X-OpenAI-Client-User-Agent"):
                value = incoming_headers.get(name)
                if value and self._api_key not in value:
                    headers[name] = value
        request = urllib.request.Request(endpoint, data=raw, headers=headers, method="POST")
        status, request_id, content_type = 502, None, "application/json"
        body = b'{"error":{"type":"gateway_transport_error","message":"Provider request failed"}}'
        try:
            try:
                response = self._opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status = response.status
                content_type = response.headers.get("content-type", "application/json")
                request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                body = response.read()
        except Exception as exc:
            event["transport_error_type"] = type(exc).__name__
        if self._secret in body:
            body = body.replace(self._secret, b"[REDACTED]")
            event["response_redacted"] = True
        if request_id and self._api_key in request_id:
            request_id = "[REDACTED]"
        values = _values(body, content_type)
        obj = _response_from_values(values)
        usage = _usage(obj)
        # 同时扫描增量事件与最终输出；完整保存上游证据后，才决定客户端能否看到该响应。
        blocked = _blocked_types(values) if 200 <= status < 300 else []
        upstream_status = status
        sent_body, sent_type = body, content_type
        # 阻断返回值与原始响应分开：日志里的 tool_blocked 表示网关拒绝传递动作，不表示工具执行过。
        if blocked:
            event["tool_blocked"] = True
            event["blocked_output_types"] = blocked
            status, sent_type = 400, "application/json"
            sent_body = b'{"error":{"type":"tool_execution_blocked","message":"Experiment executor forbids action outputs"}}'
        elif 200 <= status < 300 and not obj:
            event["response_parse_error"] = True
            status, sent_type = 502, "application/json"
            sent_body = b'{"error":{"type":"gateway_protocol_error","message":"Provider did not return a complete Responses object"}}'
        event.update({"status": status, "upstream_status": upstream_status,
                      "completed_at_unix": time.time(), "provider_request_id": request_id,
                      "usage": usage, "returned_model": obj.get("model"),
                      "response_id": obj.get("id"), "response_status": obj.get("status"),
                      "response_content_type": content_type,
                      "response_sha256": hashlib.sha256(body).hexdigest()})
        suffix = "sse" if "event-stream" in content_type or body.lstrip().startswith((b"event:", b"data:")) else "json"
        response_path = folder / ("response." + suffix)
        response_path.write_bytes(body)
        event["response_path"] = event["response_file"] = str(response_path)
        # Responses input_tokens already includes the cached-token subtotal.
        # 缺失 usage 时保留预留估计并标记 imputed，不把无法统计的调用记为零消耗。
        accounted_input = usage.get("input_tokens", event["reserved_input_tokens"])
        accounted_output = usage.get("output_tokens", event["reserved_output_tokens"])
        event["accounted_input_tokens"] = accounted_input
        event["accounted_output_tokens"] = accounted_output
        event["input_imputed_from_reservation"] = "input_tokens" not in usage
        event["output_imputed_from_reservation"] = "output_tokens" not in usage
        with self._lock:
            self._reserved_input -= event["reserved_input_tokens"]
            self._reserved_output -= event["reserved_output_tokens"]
            self._input += accounted_input
            self._output += accounted_output
            _json_write(folder / "meta.json", event)
            with (self.root / "requests.jsonl").open("a", encoding="utf-8") as ledger:
                ledger.write(json.dumps(event, ensure_ascii=False) + "\n")
                ledger.flush()
            self._events.append(copy.deepcopy(event))
        return status, sent_type, sent_body, event

    # 独立探针直接调用的入口：序列化请求并转发，成功时返回完整 Response。
    # 不会把探针回复加入原生线程，因此不会用测试答案反向强化模型记忆。
    def post(self, payload: dict[str, Any], label: str, compact: bool = False,
             timeout: float = 600) -> dict[str, Any]:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        path = "/v1/responses/compact" if compact else "/v1/responses"
        status, _, _, event = self._forward(raw, label, path, timeout)
        if not 200 <= status < 300:
            raise ResponsesGatewayError(f"Provider/gateway returned HTTP {status}; inspect recorded response")
        return response_object(event)

    # 只监听 127.0.0.1 的临时端口，将本地地址交给实验专用 Codex 配置。
    # 重复调用返回现有地址，不改动系统代理或日常模型线路。
    def start(self) -> str:
        if self._server is not None:
            host, port = self._server.server_address
            return f"http://{host}:{port}/v1"
        owner = self

        # 本地 HTTP 适配器；只开放固定 POST 路径，不提供通用代理或文件接口。
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            # 关闭默认 HTTP 日志，避免把请求细节写到终端或其他非审计日志。
            def log_message(self, _format, *_args):
                pass

            # 返回完整响应后关闭连接；客户端中断连接时不让 BrokenPipe 影响网关清理。
            def _send(self, status, content_type, body):
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    self.close_connection = True

            # 校验路径与正文大小，读取实验标签后交给统一转发流程。
            # 额度和请求错误只返回固定错误体，真实凭据与异常细节不会回显给客户端。
            def do_POST(self):
                if self.path not in ALLOWED_PATHS:
                    self._send(404, "application/json", b'{"error":{"message":"Unsupported gateway path"}}')
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 32_000_000:
                        raise ResponsesGatewayError("Invalid request size")
                    raw = self.rfile.read(length)
                    label = self.headers.get("X-Lab-Label", owner.label)[:256]
                    status, kind, body, _ = owner._forward(raw, label, self.path, incoming_headers=self.headers)
                except ResponsesBudgetExceeded:
                    status, kind, body = 400, "application/json", b'{"error":{"type":"experiment_guard_reached","message":"Experiment request/token guard reached"}}'
                except (ResponsesGatewayError, ValueError):
                    status, kind, body = 400, "application/json", b'{"error":{"type":"invalid_gateway_request","message":"Invalid experiment request"}}'
                self._send(status, kind, body)

            # 拒绝所有 GET 请求，避免网关被当成浏览或资源读取入口。
            def do_GET(self):
                self._send(404, "application/json", b'{"error":{"message":"Unsupported gateway path"}}')

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True,
                                        name="codex-responses-recording-gateway")
        self._thread.start()
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    # 停止监听并回收服务线程；不会删除已保存的请求、响应和账本。
    def close(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
