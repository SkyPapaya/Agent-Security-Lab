# 基础版外置摘要接口。full 不走这里；llm/preserving 由同一模型生成摘要。本文件不模拟 Codex 原生自动压缩，也不预设遗漏内容。
from .config import SUMMARY_PROMPT
from .history_builder import render_history
from .model_adapter import LlamaCppModel


# 按空白切分的可重复长度代理。中文和真实 tokenizer 差异可能很大，不能把它称为精确 token 数。
def token_count(text: str) -> int:
    # Reproducible whitespace proxy; not a model tokenizer count.
    return len(text.split())


# 真实 llama.cpp 优先请求 /tokenize；失败才回退到代理，并同时返回计数方法，供日志注明。
def model_token_count(model, content: str) -> tuple[int, str]:
    try:
        if isinstance(model, LlamaCppModel):
            return model.count_tokens(content), "llama.cpp /tokenize"
    except Exception:
        return token_count(content), "whitespace_proxy (tokenize unavailable)"
    return token_count(content), "whitespace_proxy"


# 拼接固定提示词和完整历史，保留模型原始摘要及完整提示词；空摘要作为压缩失败，而不是安全成功。
def compact(history: list[dict], model, max_tokens: int):
    original = render_history(history)
    prompt = SUMMARY_PROMPT + original
    summary = model.generate(prompt, max_tokens)
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("empty compaction")
    before, before_method = model_token_count(model, original)
    after, after_method = model_token_count(model, summary)
    method = before_method if before_method == after_method else f"before: {before_method}; after: {after_method}"
    return {"prompt": prompt, "summary": summary, "before_tokens": before,
            "after_tokens": after, "token_count_method": method}
