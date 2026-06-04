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


def _clean_price_label(value: str) -> str:
    return re.sub(r"[\s*`#：:]+", "", value or "")


def _parse_price_cell(value: str) -> Optional[Dict[str, Any]]:
    numbers = re.findall(r"\d+(?:\.\d+)?", value or "")
    if not numbers:
        return None
    values = [float(item) for item in numbers]
    return {
        "raw": (value or "").strip(),
        "price": round(values[0], 4),
        "low": round(min(values), 4),
        "high": round(max(values), 4),
    }


_PRICE_LEVEL_ALIASES = {
    "strong_support": ("强支撑位", "强支撑"),
    "secondary_support": ("次支撑位", "第二支撑位", "近支撑位"),
    "first_resistance": ("第一压力位", "第一阻力位", "一压力位", "一阻力位"),
    "second_resistance": ("第二压力位", "第二阻力位", "二压力位", "二阻力位"),
    "strong_resistance": ("强压力位", "强阻力位"),
    "breakout_buy": ("突破买入价", "突破买入位", "突破价"),
    "breakdown_sell": ("跌破卖出价", "跌破卖出位", "止损位", "止损价"),
}


def _extract_key_price_levels_from_text(text: str) -> Dict[str, Dict[str, Any]]:
    levels: Dict[str, Dict[str, Any]] = {}
    if not isinstance(text, str) or not text.strip():
        return levels

    in_price_zone = False
    for line in text.splitlines():
        if "关键价格区间" in line:
            in_price_zone = True
            continue
        if not in_price_zone or "|" not in line:
            continue

        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue

        label = _clean_price_label(cells[0])
        if not label or "价格类型" in label or set(label) <= {"-", " "}:
            continue

        parsed_price = _parse_price_cell(cells[1])
        if not parsed_price:
            continue

        for key, aliases in _PRICE_LEVEL_ALIASES.items():
            if any(alias in label for alias in aliases):
                parsed_price.update({
                    "label": label,
                    "description": cells[2].strip() if len(cells) > 2 else "",
                })
                levels.setdefault(key, parsed_price)
                break

    return levels


def _level_price(levels: Dict[str, Dict[str, Any]], key: str, prefer: str = "price") -> Optional[float]:
    level = levels.get(key)
    if not level:
        return None
    if prefer == "high":
        return _coerce_float(level.get("high"))
    if prefer == "low":
        return _coerce_float(level.get("low"))
    return _coerce_float(level.get("price"))


def extract_report_price_plan(reports: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract execution-oriented price references from report body tables."""
    if not isinstance(reports, dict) or not reports:
        return {}

    priority = [
        "market_report",
        "technical_report",
        "trader_investment_plan",
        "final_trade_decision",
        "risk_management_decision",
    ]
    ordered_keys = [key for key in priority if key in reports]
    ordered_keys.extend(key for key in reports.keys() if key not in ordered_keys)

    levels: Dict[str, Dict[str, Any]] = {}
    for key in ordered_keys:
        content = reports.get(key)
        parsed = _extract_key_price_levels_from_text(content) if isinstance(content, str) else {}
        for level_key, value in parsed.items():
            levels.setdefault(level_key, {**value, "module": key})

    if not levels:
        return {}

    stop_loss_price = (
        _level_price(levels, "breakdown_sell")
        or _level_price(levels, "strong_support", "low")
    )
    suggested_buy_price = _level_price(levels, "breakout_buy")
    suggested_sell_price = (
        _level_price(levels, "second_resistance")
        or _level_price(levels, "first_resistance", "high")
    )
    target_price = (
        _level_price(levels, "strong_resistance", "high")
        or _level_price(levels, "second_resistance", "high")
        or _level_price(levels, "first_resistance", "high")
    )

    return {
        "stop_loss_price": stop_loss_price,
        "suggested_buy_price": suggested_buy_price,
        "suggested_sell_price": suggested_sell_price,
        "target_price": target_price,
        "levels": levels,
    }


def parse_report_recommendation(
    recommendation: str,
    current_price: Optional[float] = None,
    decision: Optional[Dict[str, Any]] = None,
    reports: Optional[Dict[str, Any]] = None,
    price_plan: Optional[Dict[str, Any]] = None,
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
    report_price_plan = price_plan if isinstance(price_plan, dict) and price_plan else extract_report_price_plan(reports)

    if report_price_plan:
        target_price = report_price_plan.get("target_price") or target_price
        stop_loss_price = report_price_plan.get("stop_loss_price") or stop_loss_price
        suggested_buy_price = report_price_plan.get("suggested_buy_price")
        suggested_sell_price = report_price_plan.get("suggested_sell_price")
        source = "analysis_report_price_levels"
    else:
        suggested_buy_price = current if action == "buy" else None
        suggested_sell_price = current if action == "sell" else None
        source = "analysis_report_recommendation"

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
        "source": source,
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
    price_plan = extract_report_price_plan(reports)
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
            "price_plan": price_plan,
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


async def build_holding_report_advice(holding: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build holding advice from the latest saved analysis report only."""
    report_context = await _latest_report_context(str(holding.get("code", "")))
    report_meta = report_context.get("report") or {}
    report_recommendation = report_meta.get("recommendation") or ""
    report_decision = report_meta.get("decision") or {}
    report_price_plan = report_meta.get("price_plan") if isinstance(report_meta.get("price_plan"), dict) else {}
    if report_recommendation or report_decision:
        advice = parse_report_recommendation(
            report_recommendation,
            current_price=holding.get("current_price"),
            decision=report_decision,
            price_plan=report_price_plan,
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

    return None


async def build_holding_ai_advice(holding: Dict[str, Any]) -> Dict[str, Any]:
    from openai import AsyncOpenAI

    report_advice = await build_holding_report_advice(holding)
    if report_advice:
        return report_advice

    report_context = await _latest_report_context(str(holding.get("code", "")))
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
