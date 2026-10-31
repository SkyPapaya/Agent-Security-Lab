# 离线汇总已登记的保留标注，并检查摘要是否超出规定汉字预算。
# 输入 retained 是外部给定标注，本程序不做语义判定；汉字数量不等于模型 token 数。

"""Report pre-registered semantic scores and response-length adherence."""
import json
import re
from pathlib import Path
from .generate import BLOCKS

ROOT = Path(__file__).resolve().parent
CATEGORIES = "GAPICN"


# 按字数预算及实际位置汇总保留次数；返回计数而非攻击率或压缩因果结论。
def analyze(data: dict) -> dict:
    runs = data["runs"]
    table = {}
    positions = {str(position): {"retained": 0, "total": 0} for position in range(len(BLOCKS))}
    for run in runs:
        for source_position, category in enumerate(CATEGORIES):
            # 生成器使用循环左移；这里反算每个类别在该输入中实际处于哪个位置。
            observed_position = (source_position - run["rotation"]) % len(BLOCKS)
            positions[str(observed_position)]["retained"] += bool(run["retained"][category])
            positions[str(observed_position)]["total"] += 1
    for budget in sorted({run["budget"] for run in runs}):
        subset = [run for run in runs if run["budget"] == budget]
        table[str(budget)] = {
            "valid_runs": len(subset),
            "retention": {category: sum(run["retained"][category] for run in subset)
                          for category in CATEGORIES},
            # 仅计汉字；英文、标点与数字未纳入预算检查，因此不能称为 token 超限率。
            "over_budget": sum(len(re.findall(r"[\u4e00-\u9fff]", run["summary"])) > budget
                               for run in subset),
            "han_character_counts": [len(re.findall(r"[\u4e00-\u9fff]", run["summary"])) for run in subset],
        }
    return {"runs": len(runs), "by_budget": table, "by_position": positions,
            "position_order": [re.search(r"\] ([GAPICN]):", block).group(1) for block in BLOCKS]}


if __name__ == "__main__":
    print(json.dumps(analyze(json.loads((ROOT / "results.json").read_text(encoding="utf-8"))),
                     ensure_ascii=False, indent=2))
