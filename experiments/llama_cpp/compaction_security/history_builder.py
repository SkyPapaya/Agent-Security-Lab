# 从预先编写的虚构场景生成对话历史。可信权限读取 security_state，低信任材料只进入历史文本，两条数据通路不能混用。
import json
from pathlib import Path
from .schemas import SecurityState


# 读取 UTF-8 场景 JSON，并核对场景编号与文件名，帮助避免把不同实验材料混用。
def load_scenario(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["id"] == path.stem
    return data


# 只从受信的场景配置建立权限，不能让 history 中的文档自称授权后修改这些字段。
def trusted_state(scenario: dict) -> SecurityState:
    # Only trusted scenario configuration contributes to executable authority.
    return SecurityState(**scenario["security_state"])


# 按固定顺序放入用户原始任务、已有历史及普通检查记录；填充内容用于增加长度，不授予新权限。
def build_history(scenario: dict) -> list[dict]:
    history = [{"role": "user", "content": scenario["initial_user"]}]
    history += [{"role": row["role"], "content": row["content"],
                 "source_trust": row.get("source_trust", "trusted")}
                for row in scenario["history"]]
    for i in range(scenario.get("routine_note_count", 0)):
        history.append({"role": "assistant", "source_trust": "trusted",
                        "content": f"Routine local quality check {i+1}: synthetic report columns and local backup metadata reviewed."})
    return history


# 把角色与来源可信度一起写进模型可见文本；保留这些标签便于比较压缩前后的来源信息。
def render_history(history: list[dict]) -> str:
    return "\n".join(f"[{m['role']} source_trust={m.get('source_trust', 'trusted')}] {m['content']}" for m in history)
