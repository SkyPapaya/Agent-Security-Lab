# Codex 原生会话适配层：只读日常配置，另起实验专用 app-server。
# 真实密钥仅供网关使用；子进程看到占位密钥，不能读取被挂载隐藏的日常 Codex 状态。
"""Private Codex app-server using the existing sub2api provider, without secrets in logs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import tomllib


# 统一表示配置、RPC 或原生回合失败；调用方应记为实验错误，而非信息遗忘。
class CodexError(RuntimeError):
    pass


# 读取已有配置和 API key，返回本次实验所需的模型、强度、线路和凭据来源。
# 显式参数只覆盖返回值，不改写用户配置；仅接受 API key，避免误用 ChatGPT OAuth 凭据。
def load_provider(config_path=None, auth_path=None, environ=None,
                  model_override=None, reasoning_effort_override=None,
                  service_tier_override=None):
    """Never send ChatGPT OAuth tokens to a third-party API provider."""
    environ = os.environ if environ is None else environ
    config_path = Path(config_path or "/home/haomin_wang/.codex/config.toml")
    auth_path = Path(auth_path or "/home/haomin_wang/.codex/auth.json")
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    provider = config.get("model_providers", {}).get("sub2api")
    if not provider or provider.get("wire_api") != "responses":
        raise CodexError("Expected an existing sub2api Responses provider in config.toml")
    auth = json.loads(auth_path.read_text()) if auth_path.exists() else {}
    key = auth.get("OPENAI_API_KEY")
    source = "auth_json_OPENAI_API_KEY"
    if not isinstance(key, str) or not key.strip():
        name = provider.get("env_key", "SUB2API_API_KEY")
        key = environ.get(name)
        source = "provider_env:" + name
    if not isinstance(key, str) or not key.strip():
        raise CodexError("No provider API key: auth.json has no OPENAI_API_KEY and the provider env_key is unset; ChatGPT OAuth tokens are not provider API keys")
    model = model_override or config.get("model", "gpt-6.1-sol")
    effort = reasoning_effort_override or config.get("model_reasoning_effort", "high")
    tier = service_tier_override or config.get("service_tier", "default")
    if not isinstance(model, str) or not model.strip():
        raise CodexError("model must be a non-empty string")
    if effort not in {"minimal", "low", "medium", "high", "xhigh"}:
        raise CodexError("reasoning effort must be one of minimal, low, medium, high, xhigh")
    if not isinstance(tier, str) or not tier.strip():
        raise CodexError("service tier must be a non-empty string")
    return {"model": model.strip(),
            "reasoning_effort": effort,
            "service_tier": tier.strip(),
            "base_url": provider["base_url"].rstrip("/"), "api_key": key.strip(),
            "credential_source": source, "config_path": str(config_path), "auth_path": str(auth_path)}


# 从 Responses 输出中提取用户可见消息文本；不把推理项或压缩项当作最终回答。
def response_text(response):
    return "\n".join(b.get("text", "") for item in response.get("output", [])
                     if item.get("type") == "message"
                     for b in item.get("content", []) if b.get("type") in ("output_text", "text"))


# 把实际请求中的可读文字投影为一段文本，供离线核对事实是否出现。
# 返回 text 和不透明状态标记；若含加密压缩内容，就不能用“文字里没找到”证明模型已遗忘。
def context_view(payload):
    """Inspect readable Responses input; encrypted compaction makes absence unknowable."""
    text = []
    opaque = False
    opaque_reasoning = False
    if isinstance(payload.get("instructions"), str):
        text.append(payload["instructions"])
    items = payload.get("input", [])
    if isinstance(items, str):
        items = [{"type": "message", "role": "user", "content": items}]
    for item in items:
        if not isinstance(item, dict):
            continue
        # 加密/不透明内容只作存在标记，不尝试推断其内部是否含某个安全事实。
        if item.get("type") == "compaction":
            opaque = True
        elif item.get("encrypted_content"):
            if item.get("type") == "reasoning":
                opaque_reasoning = True
            else:
                opaque = True
        content = item.get("content", [])
        if isinstance(content, str):
            text.append(content)
        elif isinstance(content, list):
            text.extend(b["text"] for b in content if isinstance(b, dict)
                        and b.get("type") in ("input_text", "output_text", "text") and isinstance(b.get("text"), str))
        if isinstance(item.get("text"), str):
            text.append(item["text"])
        if isinstance(item.get("summary"), list):
            text.extend(b["text"] for b in item["summary"] if isinstance(b, dict)
                        and b.get("type") == "summary_text" and isinstance(b.get("text"), str))
        if item.get("type") == "function_call_output":
            output = item.get("output")
            if isinstance(output, str):
                text.append(output)
            elif isinstance(output, list):
                text.extend(b["text"] for b in output if isinstance(b, dict) and isinstance(b.get("text"), str))
    return {"text": "\n".join(text), "opaque": opaque, "opaque_reasoning": opaque_reasoning,
            "observation": "opaque_state_present_absence_unobservable" if opaque else "readable_text_input"}


# 生成实验私有 TOML，设置压缩阈值，并关闭工具、记忆、插件相关能力和非必要流量。
# 只返回配置文本，由会话写入实验目录；不会覆盖日常 config.toml。
def private_config(provider, gateway_url, threshold, root):
    # Only an experiment-owned, mount-isolated config is written.
    scalar = {"model": provider["model"], "model_provider": "sub2api",
              "model_reasoning_effort": provider["reasoning_effort"],
              "service_tier": provider.get("service_tier", "default"),
              "approval_policy": "never", "sandbox_mode": "read-only",
              "model_auto_compact_token_limit": threshold, "project_doc_max_bytes": 0,
              "web_search": "disabled", "personality": "none",
              "log_dir": str(root / "logs"), "sqlite_home": str(root / "sqlite")}
    lines = [f"{k} = {json.dumps(v)}" for k, v in scalar.items()]
    lines += ["", "[model_providers.sub2api]", 'name = "Sub2API isolated lab recorder"',
              "base_url = " + json.dumps(gateway_url), 'wire_api = "responses"',
              'env_key = "SUB2API_API_KEY"', "requires_openai_auth = false",
              "request_max_retries = 0", "stream_max_retries = 0", "stream_idle_timeout_ms = 600000",
              "", "[features]", "shell_tool = false", "shell_snapshot = false",
              "multi_agent = false", "prevent_idle_sleep = false", "memory_tool = false",
              "", "[memories]", "use_memories = false", "generate_memories = false",
              "", "[analytics]", "enabled = false"]
    return "\n".join(lines) + "\n"


# 为实验子进程构造最小环境，避免继承代理、真实密钥或其他模型线路。
# 保留 HOME/CODEX_HOME 的原值，实际目录隔离由 bubblewrap 挂载完成。
def isolated_environment():
    # Preserve HOME/CODEX_HOME locations; namespace mounts isolate state instead.
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "CODEX_HOME"}
    env = {k: v for k, v in os.environ.items() if k in allowed}
    env["SUB2API_API_KEY"] = "experiment-loopback-placeholder"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    return env


# 一条轨迹对应一个独立进程和临时线程；RPC 回包与异步原生事件分别保存。
# 此类只让模型接收文字和压缩历史，不允许它获得宿主工具权限。
class CodexSession:
    """One independent app-server process and ephemeral in-memory thread."""
    # 创建私有目录、配置和二进制引用，再在 bubblewrap 中启动 app-server。
    # 完成初始化后保存版本、二进制哈希及线程信息；缺少隔离组件时直接失败，不回退到日常环境。
    def __init__(self, root, provider, gateway_url, threshold=24000, timeout=600):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.provider, self.timeout = provider, timeout
        self.events = []
        # Condition 用于后台读取线程通知 RPC 等待者；responses 按请求 id 保存，events 保留异步通知。
        self._condition = threading.Condition()
        self._responses = {}
        self._next = 1
        self.thread_id = None
        self.proc = None
        binary = shutil.which("codex")
        bwrap = shutil.which("bwrap")
        if not binary or not bwrap:
            raise CodexError("This isolated launcher requires installed codex and bubblewrap; no global-config fallback is used")
        source = Path(binary).resolve()
        # Hardlink an already installed binary out of the directory hidden by the mount.
        self.binary = self.root / "codex-bin"
        if not self.binary.exists():
            try:
                os.link(source, self.binary)
            except OSError:
                shutil.copy2(source, self.binary)
        # 保留正常目录路径，只在子进程挂载命名空间中遮盖它，宿主上的日常文件不会被替换。
        self.normal_codex_dir = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve()
        private = self.root / "private_codex"
        private.mkdir(exist_ok=True)
        (private / "config.toml").write_text(private_config(provider, gateway_url, threshold, self.root))
        (self.root / "work").mkdir(exist_ok=True)
        # 宿主文件系统只读挂载；只允许实验根目录写入，并把日常 Codex 目录替换成私有目录。
        # 这不是完全网络隔离：模型推理仍通过回环网关转发到用户配置的服务。
        args = [bwrap, "--ro-bind", "/", "/", "--bind", str(self.root), str(self.root),
                "--bind", str(private), str(self.normal_codex_dir), "--proc", "/proc", "--dev", "/dev",
                "--ro-bind", str(self.binary), str(self.binary),
                "--tmpfs", "/tmp", "--unshare-pid", "--die-with-parent", "--chdir", str(self.root / "work"),
                "--", str(self.binary), "--no-daemon", "app-server", "--listen", "stdio://"]
        self.stderr = (self.root / "stderr.log").open("a", encoding="utf-8")
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.stderr, text=True, bufsize=1, env=isolated_environment())
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self.call("initialize", {"clientInfo": {"name": "compaction-security-lab", "version": "1.0"},
                                 "capabilities": {"experimentalApi": True}})
        self._write({"method": "initialized"})
        response = self.call("thread/start", {"model": provider["model"], "modelProvider": "sub2api",
                    "ephemeral": True, "cwd": str(self.root / "work"), "sandbox": "read-only",
                    "approvalPolicy": "never", "approvalsReviewer": "user", "experimentalRawEvents": True,
                    "environments": [], "dynamicTools": [], "selectedCapabilityRoots": []})
        # 任何意外载入的指令文件都可能污染条件，因此发现后关闭会话而不是继续实验。
        if response.get("instructionSources"):
            self.close()
            raise CodexError("Unexpected instruction files in isolated thread")
        self.thread_id = response["thread"]["id"]
        manifest = {"framework": subprocess.check_output([binary, "--version"], text=True).strip(),
                    "binary_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "thread_id": self.thread_id, "ephemeral": True,
                    "isolation": "bubblewrap_private_codex_mount_readonly_host_no_daemon",
                    "source_config_edited": False, "source_auth_copied": False,
                    "model": provider["model"], "credential_source": provider["credential_source"]}
        (self.root / "session_manifest.json").write_text(json.dumps(manifest, indent=2))

    # 向 stdio RPC 写入一条完整 JSON 行；子进程退出时立即报错，避免继续写入失效管道。
    def _write(self, obj):
        if self.proc.poll() is not None:
            raise CodexError("Isolated Codex app-server exited; inspect stderr.log")
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    # 后台读取 RPC 输出并逐条保存：带 id 的回包交给等待者，通知事件加入事件列表。
    # 任何服务端发起的工具或审批 RPC 都返回拒绝，防止模型越过实验工具执行层。
    def _read(self):
        for line in self.proc.stdout:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self._condition:
                with (self.root / "appserver.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
                if "id" in item and "method" not in item:
                    self._responses[item["id"]] = item
                else:
                    self.events.append(item)
                    # Defense in depth: no model-originated host capabilities are granted.
                    if "id" in item and "method" in item:
                        self._write({"id": item["id"], "error": {"code": -32601, "message": "Experiment disables tool and approval requests"}})
                self._condition.notify_all()
        with self._condition:
            self._condition.notify_all()

    # 分配请求编号并等待对应 RPC 回包；Condition 允许读取线程唤醒当前调用。
    # 进程退出、超时和 RPC 错误都显式失败，不把缺失回包解释成正常完成。
    def call(self, method, params):
        n = self._next
        self._next += 1
        self._write({"id": n, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        with self._condition:
            while n not in self._responses:
                if self.proc.poll() is not None:
                    raise CodexError("Isolated app-server exited before RPC response; inspect stderr.log")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.proc.terminate()
                    raise CodexError("Codex RPC timeout: " + method)
                self._condition.wait(min(remaining, 1))
            response = self._responses.pop(n)
        if "error" in response:
            self.proc.terminate()
            raise CodexError("Codex RPC rejected " + method + ": " + str(response["error"].get("message", ""))[:250])
        return response.get("result", {})

    # 从指定事件位置起等待本次 turn/completed，可按 turn_id 排除其他回合。
    # 必须原生状态为 completed 才返回；错误、进程退出及超时分别抛出异常。
    def _wait_turn(self, start, turn_id=None):
        deadline = time.monotonic() + self.timeout
        with self._condition:
            while True:
                for event in self.events[start:]:
                    if event.get("method") != "turn/completed":
                        continue
                    turn = event.get("params", {}).get("turn", {})
                    if turn_id and turn.get("id") != turn_id:
                        continue
                    if turn.get("status") != "completed":
                        raise CodexError("Codex turn failed: " + json.dumps(turn.get("error"))[:250])
                    return turn
                if self.proc.poll() is not None:
                    raise CodexError("Codex exited during turn")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexError("Codex turn timed out")
                self._condition.wait(min(remaining, 1))

    # 发起一个文字回合并等待结束，返回本回合结果和新增原生事件，供网关记录关联。
    def send(self, text):
        start = len(self.events)
        response = self.call("turn/start", {"threadId": self.thread_id,
                    "input": [{"type": "text", "text": text}],
                    "disabledPluginIds": [],
                    "effort": self.provider["reasoning_effort"]})
        turn = self._wait_turn(start, response.get("turn", {}).get("id"))
        return {"turn": turn, "events": self.events[start:]}

    # 显式请求框架执行原生压缩；这是人工触发诊断条件，不应与自动压缩混算。
    def compact(self):
        start = len(self.events)
        self.call("thread/compact/start", {"threadId": self.thread_id})
        turn = self._wait_turn(start)
        return {"turn": turn, "events": self.events[start:]}

    # 将预先构造的消息项放入线程，用于比较 user/assistant/tool 来源。
    # 这是人工角色放置，并不表示模型真实调用了这些工具。
    def inject(self, items):
        """Explicitly labelled role-placement diagnostic; no tools are executed."""
        return self.call("thread/inject_items", {"threadId": self.thread_id, "items": items})

    # 只统计已完成的 contextCompaction 原生事件，避免把开始事件或普通回合算作压缩。
    def compactions(self):
        return [e for e in self.events if e.get("method") == "item/completed"
                and e.get("params", {}).get("item", {}).get("type") == "contextCompaction"]

    # 释放本轨迹进程与日志句柄；先正常终止，超过等待时间后再强制结束。
    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if hasattr(self, "stderr"):
            self.stderr.close()

