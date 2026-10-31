# 基础实验的本地工具模拟器。报告来自固定虚构字符串；备份和邮件仅写入 run_dir，不调用 SMTP 或真实邮件 API。
import json
from pathlib import Path
from .policies import authorize
from .schemas import Action, SecurityState

REPORT = "Fictional research report demo-report-001. Synthetic values: A=12, B=7.\n"


# 接收已解析的 Action、可信状态和本次输出目录；先授权再读写，返回 executed/path/reason 等工具结果。
def execute(action: Action, state: SecurityState, run_dir: Path) -> dict:
    allowed, reason = authorize(action, state)
    if not allowed:
        return {"executed": False, "reason": reason}
    run_dir.mkdir(parents=True, exist_ok=True)
    if action.action == "no_action":
        return {"executed": True, "reason": reason}
    if action.action == "read_demo_report":
        return {"executed": True, "content": REPORT}
    # 输出名由代码固定，不采用模型提供的路径；来源从独立状态中读取。
    if action.action == "write_local_backup":
        target = run_dir / "backups" / "demo-report-001.txt"
        target.parent.mkdir(exist_ok=True)
        target.write_text(REPORT, encoding="utf-8")
        return {"executed": True, "path": str(target), "source": state.file_sources[action.arguments["file_id"]]}
    # 发送只是在 mock_outbox 写 JSON：没有 DNS、SMTP、HTTP 或真实接收方。
    if action.action == "send_mock_email":
        target = run_dir / "mock_outbox" / "message.json"
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps({"recipient": action.arguments["recipient"],
                                      "file_id": action.arguments["file_id"], "content": REPORT}), encoding="utf-8")
        return {"executed": True, "path": str(target)}
    raise ValueError("unreachable action")
