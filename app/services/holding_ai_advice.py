"""AI powered holding advice using the configured LLM."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException, status


_OPENAI_COMPATIBLE_DEFAULTS = {
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "deepseek": "https://api.deepseek.com",
    "openrouter": "https://openrouter.ai/api/v1",
    "aihubmix": "https://aihubmix.com/v1",
    "openai": "https://api.openai.com/v1",
    "custom_openai": None,
}

_PROVIDER_ENV_KEYS = {
    "qwen": "DASHSCOPE_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "aihubmix": "AIHUBMIX_API_KEY",
    "openai": "OPENAI_API_KEY",
    "custom_openai": "CUSTOM_OPENAI_API_KEY",
}


def _coerce_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _normalize_action(value: Any) -> str:
    action = str(value or "hold").strip().lower()
    if action in {"buy", "sell", "hold"}:
        return action
    if "买" in action or "补" in action:
        return "buy"
    if "卖" in action or "减" in action:
        return "sell"
    return "hold"


def _json_loads_best_effort(raw: str) -> Optional[Dict[str, Any]]:
    text = (raw or "").strip()
    if not text:
        return None

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [text]
    if fenced:
        candidates.insert(0, fenced.group(1))

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.insert(0, text[start : end + 1])

    for candidate in candidates:
        try:
            loaded = json.loads(candidate)
            if isinstance(loaded, dict):
                return loaded
        except json.JSONDecodeError:
            continue
    return None


def parse_model_advice_response(raw: str) -> Dict[str, Any]:
    """Parse the LLM response into a stable advice shape."""
    parsed = _json_loads_best_effort(raw)
    if not parsed:
        return {
            "action": "hold",
            "confidence": 0.0,
            "suggested_buy_price": None,
            "suggested_sell_price": None,
            "target_price": None,
            "stop_loss_price": None,
            "position_suggestion": "继续观察",
            "reason": "模型返回格式无法解析，暂按持有观察处理。",
            "risks": [],
            "raw_response": raw,
            "is_reference_only": True,
        }

    risks = parsed.get("risks", [])
    if isinstance(risks, str):
        risks = [risks]
    if not isinstance(risks, list):
        risks = []

    return {
        "action": _normalize_action(parsed.get("action")),
        "confidence": _coerce_float(parsed.get("confidence")) or 0.0,
        "suggested_buy_price": _coerce_float(parsed.get("suggested_buy_price")),
        "suggested_sell_price": _coerce_float(parsed.get("suggested_sell_price")),
        "target_price": _coerce_float(parsed.get("target_price")),
        "stop_loss_price": _coerce_float(parsed.get("stop_loss_price")),
        "position_suggestion": str(parsed.get("position_suggestion") or "继续观察"),
        "reason": str(parsed.get("reason") or "模型未返回明确理由。"),
        "risks": [str(item) for item in risks if item],
        "raw_response": raw,
        "is_reference_only": True,
    }


def _extract_price(text: str, patterns: list[str]) -> Optional[float]:
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return _coerce_float(match.group(1))
    return None


def _extract_reason(text: str) -> str:
    match = re.search(r"(?:决策依据|理由|原因)[：:]\s*(.+)$", text, re.S)
    if match:
        return match.group(1).strip()
    return text.strip()


def parse_report_recommendation(
    recommendation: str,
    current_price: Optional[float] = None,
    decision: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Parse the saved report's investment recommendation into holding advice."""
    decision = decision or {}
    text = recommendation or ""
    action = _normalize_action(decision.get("action") or text)
    target_price = _coerce_float(decision.get("target_price")) or _extract_price(
        text,
        [
            r"目标价(?:格|位)?[：:\s]*([0-9]+(?:\.[0-9]+)?)",
            r"目标价格[：:\s]*([0-9]+(?:\.[0-9]+)?)",
            r"target\s*price[：:\s]*([0-9]+(?:\.[0-9]+)?)",
        ],
    )
    stop_loss_price = _extract_price(
        text,
        [
            r"止损(?:价|价格|位)?[：:\s]*([0-9]+(?:\.[0-9]+)?)",
            r"stop[-\s]*loss[：:\s]*([0-9]+(?:\.[0-9]+)?)",
        ],
    )
    current = _coerce_float(current_price)

    suggested_buy_price = current if action == "buy" else None
    suggested_sell_price = current if action == "sell" else None
    reason = _extract_reason(text)

    return {
        "action": action,
        "confidence": _coerce_float(decision.get("confidence")) or 0.0,
        "suggested_buy_price": suggested_buy_price,
        "suggested_sell_price": suggested_sell_price,
        "target_price": target_price,
        "stop_loss_price": stop_loss_price,
        "position_suggestion": text.split("。", 1)[0].strip() or "参考报告投资建议",
        "reason": reason,
        "risks": [],
        "raw_response": text,
        "source": "analysis_report_recommendation",
        "is_reference_only": True,
    }


def _provider_value(provider: Any) -> str:
    if provider is None:
        return ""
    if hasattr(provider, "value"):
        provider = provider.value
    return str(provider).strip().lower()


def _normalize_api_key(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    if not value or value in {"your-api-key", "your-openai-api-key", "your-qwen-api-key"}:
        return None

    match = re.fullmatch(r"os\.getenv\([\"']([^\"']+)[\"']\)", value)
    if match:
        inner = match.group(1)
        return os.environ.get(inner) or inner

    if re.fullmatch(r"[A-Z][A-Z0-9_]+", value):
        return os.environ.get(value) or value
    return value


async def _select_llm_config() -> Dict[str, Any]:
    from app.core.database import get_mongo_db
    from app.services.config_service import config_service

    config = await config_service.get_system_config()
    if not config or not config.llm_configs:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="未配置可用的大模型")

    settings = config.system_settings or {}
    preferred_names = [
        settings.get("deep_analysis_model"),
        settings.get("quick_analysis_model"),
        config.default_llm,
    ]

    enabled = [item for item in config.llm_configs if getattr(item, "enabled", True)]
    candidates = enabled or config.llm_configs

    selected = None
    for name in preferred_names:
        if not name:
            continue
        selected = next((item for item in candidates if item.model_name == name), None)
        if selected:
            break
    if not selected:
        selected = candidates[0]

    provider = _provider_value(selected.provider)
    api_base = selected.api_base or getattr(selected, "custom_endpoint", None) or _OPENAI_COMPATIBLE_DEFAULTS.get(provider)
    api_key = _normalize_api_key(selected.api_key)

    if not api_key:
        db = get_mongo_db()
        provider_doc = await db["llm_providers"].find_one({"name": provider}, {"api_key": 1, "default_base_url": 1})
        if provider_doc:
            api_key = _normalize_api_key(provider_doc.get("api_key"))
            api_base = api_base or provider_doc.get("default_base_url")

    if not api_key:
        env_key = _PROVIDER_ENV_KEYS.get(provider)
        api_key = os.environ.get(env_key) if env_key else None

    if not api_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"模型 {selected.model_name} 未配置 API Key")
    if not api_base:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"模型 {selected.model_name} 未配置 API Base")

    return {
        "provider": provider,
        "model_name": selected.model_name,
        "api_base": api_base,
        "api_key": api_key,
        "temperature": float(selected.temperature or 0.3),
        "max_tokens": int(selected.max_tokens or 1800),
        "timeout": int(selected.timeout or 180),
        "retry_times": int(selected.retry_times or 2),
    }


def _clip_text(value: Any, limit: int = 5000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[内容已截断]"


async def _latest_report_context(code: str) -> Dict[str, Any]:
    from app.core.database import get_mongo_db

    db = get_mongo_db()
    doc = await db["analysis_reports"].find_one(
        {"stock_symbol": code},
        sort=[("created_at", -1)],
    )
    if not doc:
        return {"report": None, "text": "暂无历史分析报告。"}

    parts = []
    if doc.get("summary"):
        parts.append("摘要:\n" + _clip_text(doc.get("summary"), 1800))
    if doc.get("recommendation"):
        parts.append("已有建议:\n" + _clip_text(doc.get("recommendation"), 1200))
    if doc.get("decision"):
        parts.append("已有决策字段:\n" + _clip_text(doc.get("decision"), 1200))

    reports = doc.get("reports") or {}
    if reports:
        for name, content in list(reports.items())[:5]:
            parts.append(f"{name}:\n{_clip_text(content, 1600)}")

    return {
        "report": {
            "id": str(doc.get("_id")),
            "analysis_id": doc.get("analysis_id"),
            "task_id": doc.get("task_id"),
            "analysis_date": doc.get("analysis_date"),
            "model_info": doc.get("model_info"),
            "recommendation": doc.get("recommendation") or "",
            "decision": doc.get("decision") if isinstance(doc.get("decision"), dict) else {},
        },
        "text": "\n\n".join(parts) if parts else "最近报告没有可用正文。",
    }


def _build_prompt(holding: Dict[str, Any], report_context: Dict[str, Any]) -> str:
    payload = {
        "holding": {
            "code": holding.get("code"),
            "name": holding.get("name"),
            "market": holding.get("market"),
            "quantity": holding.get("quantity"),
            "cost_price": holding.get("cost_price"),
            "current_price": holding.get("current_price"),
            "target_monthly_return_pct": holding.get("target_monthly_return_pct"),
            "stop_loss_pct": holding.get("stop_loss_pct"),
            "strategy": holding.get("strategy"),
            "notes": holding.get("notes"),
        },
        "rule_based_analysis": holding.get("analysis"),
        "latest_report_meta": report_context.get("report"),
        "latest_report_text": report_context.get("text"),
    }
    return (
        "你是股票持仓管理助手。请基于用户持仓、当前价格、规则型目标进度、历史股票分析报告，"
        "给出仅用于学习研究和仓位管理参考的价格建议。不要承诺收益，不要输出交易指令。\n"
        "必须只返回 JSON，不要 Markdown，不要额外解释。字段固定如下：\n"
        "{\n"
        '  "action": "buy|sell|hold",\n'
        '  "confidence": 0.0,\n'
        '  "suggested_buy_price": null,\n'
        '  "suggested_sell_price": null,\n'
        '  "target_price": null,\n'
        '  "stop_loss_price": null,\n'
        '  "position_suggestion": "例如：持有观察/减仓30%/回落到xx附近再考虑",\n'
        '  "reason": "简明理由",\n'
        '  "risks": ["风险1", "风险2"]\n'
        "}\n\n"
        "输入数据：\n"
        f"{json.dumps(payload, ensure_ascii=False, default=str)}"
    )


async def build_holding_ai_advice(holding: Dict[str, Any]) -> Dict[str, Any]:
    from openai import AsyncOpenAI

    report_context = await _latest_report_context(str(holding.get("code", "")))
    report_meta = report_context.get("report") or {}
    report_recommendation = report_meta.get("recommendation") or ""
    report_decision = report_meta.get("decision") or {}
    if report_recommendation or report_decision:
        advice = parse_report_recommendation(
            report_recommendation,
            current_price=holding.get("current_price"),
            decision=report_decision,
        )
        model_info = str(report_meta.get("model_info") or "analysis_report")
        advice.update(
            {
                "model_name": model_info,
                "provider": "analysis_report",
                "based_on_report": {k: v for k, v in report_meta.items() if k not in {"recommendation", "decision"}},
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return advice

    llm_config = await _select_llm_config()
    prompt = _build_prompt(holding, report_context)

    client = AsyncOpenAI(
        api_key=llm_config["api_key"],
        base_url=llm_config["api_base"],
        timeout=llm_config["timeout"],
        max_retries=llm_config["retry_times"],
    )
    response = await client.chat.completions.create(
        model=llm_config["model_name"],
        messages=[
            {"role": "system", "content": "你只输出合法 JSON。"},
            {"role": "user", "content": prompt},
        ],
        temperature=llm_config["temperature"],
        max_tokens=min(llm_config["max_tokens"], 1800),
    )
    raw = response.choices[0].message.content or ""
    advice = parse_model_advice_response(raw)
    advice.update(
        {
            "model_name": llm_config["model_name"],
            "provider": llm_config["provider"],
            "based_on_report": report_context.get("report"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return advice
