# 公开全市场研究候选发现实施计划

> **执行约束：** 按任务和复选框逐步实施，严格执行测试先行。当前工作区已有未提交改动，不得回滚或覆盖；未经用户明确要求，不提交、不推送代码。

**目标：** 当 MongoDB 不可用或 Mongo 动态候选快照属于允许降级的失败状态时，让 `holdings opportunities` 通过新浪全 A 股快照和腾讯批量行情生成不可执行的研究观察池。

**架构：** 新浪子进程返回命令级不可变全市场快照，同时供市场宽度和公开候选初筛复用；独立公开候选服务完成确定性排名，腾讯服务按 40 只分批复核最多 160 只；受限子进程并发完成全量技术初筛，再对最多 8 个技术通过者执行公司行动深检；CLI 编排层负责 Mongo 降级、90 秒截止时间及最终 research-only 安全清洗。

**技术栈：** Python 3.10+、Typer、PyMongo、Requests、AKShare、pytest、标准库 `subprocess` / `dataclasses` / `time.monotonic`。

**批准的设计：** `docs/design/2026-07-15-public-full-market-research-discovery.md`

---

## 文件边界

**新增文件**

- `app/services/public_candidate_discovery_service.py`：纯公开初筛、腾讯复核、确定性百分位和阶段统计；不读取 Mongo、不计算仓位。
- `app/services/opportunity_market_context.py`：命令级指数行情、基准交易日、公开快照缓存和 90 秒截止时间。
- `app/services/public_candidate_deep_check.py`：受限子进程入口，最多 50 秒执行技术日线、业绩和公司行动深检。
- `app/services/research_only_safety.py`：递归执行 research-only 输出安全不变量。
- `tests/test_public_candidate_discovery_service.py`：公开初筛和腾讯复核单元测试。
- `tests/test_opportunity_market_context.py`：命令级复用和截止时间测试。
- `tests/test_public_candidate_deep_check.py`：深检输入、行情复用和超时测试。
- `tests/test_research_only_safety.py`：递归安全不变量测试。

**修改文件**

- `app/services/public_market_breadth.py`：由“仅宽度”扩展为完整新浪快照提供方，同时保留现有宽度入口兼容性。
- `app/services/tencent_quote_service.py`：增加腾讯批量解析、批量抓取和公开研究时效校验。
- `app/services/holdings_cli.py`：注入命令级上下文、接入公开降级、复用腾讯行情、组装错误信封并把成功 Schema 升至 7。
- `tests/test_public_market_breadth.py`：覆盖预期总量、分交易所覆盖和完整行字段。
- `tests/test_tencent_quote_service.py`：覆盖批量响应和研究时效边界。
- `tests/test_cli_holdings.py`：覆盖公开降级、输出安全、错误语义、Schema 兼容和单次抓取。
- `docs/HERMES_HOLDINGS_CLI.md`：更新无候选代码时的公开研究模式和字段说明。

## Task 0：冻结当前基线

**文件：** 只读检查，不修改。

- [ ] **Step 1：确认工作区改动范围**

运行：

```bash
git status --short
```

预期：保留现有 `holdings_cli.py`、Hermes 文档、CLI 测试、公开宽度文件和设计文档改动；不得清理或回滚。

- [ ] **Step 2：运行当前相关测试作为基线**

运行：

```bash
.venv/bin/python -m pytest \
  tests/test_public_market_breadth.py \
  tests/test_tencent_quote_service.py \
  tests/test_candidate_discovery_service.py \
  tests/test_cli_holdings.py -q
```

预期：现有测试全部通过；若有失败，先记录并判断是否为本地既有失败，不能通过删除断言规避。

## Task 1：把新浪宽度结果升级为完整命令级快照

**文件：**

- 修改：`app/services/public_market_breadth.py`
- 测试：`tests/test_public_market_breadth.py`

- [ ] **Step 1：先写提供方预期数量和沪深京覆盖率失败测试**

新增测试至少覆盖：

```python
def test_snapshot_requires_total_and_each_exchange_coverage():
    result = _normalize_sina_snapshot(
        rows,
        benchmark_trade_date="2026-07-15",
        provider_anchor=anchor,
        provider_expected_counts={
            "total": 5527,
            "sh": 2307,
            "sz": 2893,
            "bj": 327,
        },
        now=now,
    )
    assert result["status"] == "public_snapshot_coverage_incomplete"
    assert result["exchange_coverage_ratio"]["bj"] < 0.95
```

同时增加预期数量接口不可用、北京差额为负、总量不足 500、某交易所缺失的失败测试。

- [ ] **Step 2：运行新测试，确认红灯**

运行：

```bash
.venv/bin/python -m pytest tests/test_public_market_breadth.py -q
```

预期：因 `_normalize_sina_snapshot` 尚不接收 `provider_expected_counts` 或缺少覆盖字段而失败。

- [ ] **Step 3：实现预期数量读取和代码交易所分类**

在 `public_market_breadth.py` 增加：

```python
SINA_COUNT_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeStockCount?node={node}"
)

def _load_sina_expected_counts() -> Dict[str, int]:
    total = _fetch_count("hs_a")
    sh = _fetch_count("sh_a")
    sz = _fetch_count("sz_a")
    bj = total - sh - sz
    if min(total, sh, sz, bj) <= 0:
        raise ValueError("invalid Sina expected counts")
    return {"total": total, "sh": sh, "sz": sz, "bj": bj}

def _exchange_for_code(code: str) -> Optional[str]:
    if re.fullmatch(r"6\d{5}", code):
        return "sh"
    if re.fullmatch(r"[03]\d{5}", code):
        return "sz"
    if code.startswith(("43", "83", "87", "88", "92")):
        return "bj"
    return None
```

数量解析必须接受带引号或空白的整数响应，但拒绝空值、非整数和负差额。

- [ ] **Step 4：补齐快照行和覆盖证明 DTO**

规范化行至少输出：

```python
{
    "code": code,
    "name": name,
    "exchange": exchange,
    "close": close,
    "pct_chg": pct_chg,
    "amount": amount,
    "trade_date": benchmark_date,
    "provider_time": provider_time,
}
```

成功 DTO 必须包含 `provider_expected_count`、
`provider_expected_exchange_counts`、`raw_row_count`、`unique_row_count`、
`exchange_counts`、`total_coverage_ratio`、`exchange_coverage_ratio`、
`duplicate_count` 和 `rows`。总量和每个交易所覆盖率都必须不低于 0.95。

- [ ] **Step 5：保留宽度兼容入口并确保一次 worker 调用**

新增主入口：

```python
def fetch_sina_public_market_snapshot(...):
    ...

def fetch_sina_public_market_breadth(...):
    return fetch_sina_public_market_snapshot(...)
```

worker 一次性读取锚点、预期数量和 AKShare 快照。`market-status` 继续使用旧函数名时不得产生第二次抓取。

- [ ] **Step 6：运行快照测试，确认绿灯**

运行：

```bash
.venv/bin/python -m pytest tests/test_public_market_breadth.py -q
```

预期：全部通过。

## Task 2：实现腾讯批量行情和研究时效门禁

**文件：**

- 修改：`app/services/tencent_quote_service.py`
- 测试：`tests/test_tencent_quote_service.py`

- [ ] **Step 1：先写批量解析失败测试**

测试一个响应中包含请求代码、额外代码和重复代码，断言解析结果保留原始行和
`provider_symbol`，不在服务层用字典吞掉重复项：

```python
rows = parse_tencent_quote_batch_payload(payload)
assert [row["code"] for row in rows] == ["600000", "000001", "600000"]
assert rows[0]["provider_symbol"] == "sh600000"
```

- [ ] **Step 2：先写公开研究时效边界测试**

覆盖盘中恰好滞后 300 秒、滞后 301 秒、未来 120 秒、未来 121 秒，以及收盘后
14:55、14:54:59、错交易日。公开研究时效函数不得改变现有
`assess_cn_quote_freshness` 的仓位执行语义。

- [ ] **Step 3：运行新测试，确认红灯**

运行：

```bash
.venv/bin/python -m pytest tests/test_tencent_quote_service.py -q
```

预期：批量函数和公开研究时效函数尚不存在而失败。

- [ ] **Step 4：实现批量解析和单次 HTTP 请求**

增加接口：

```python
def parse_tencent_quote_batch_payload(payload: str) -> List[Dict[str, Any]]:
    ...

def fetch_tencent_quotes_sync(
    codes: Iterable[str], *, timeout: float = 10.0
) -> Dict[str, Any]:
    ...
```

`fetch_tencent_quotes_sync` 对最多 40 个唯一代码构造一次
`https://qt.gtimg.cn/q=<symbol>,<symbol>` 请求，返回
`status/requested_codes/rows/error_type`，不把行折叠成代码映射。

- [ ] **Step 5：实现独立的研究时效函数**

增加：

```python
def assess_tencent_research_quote_freshness(
    quote: Dict[str, Any],
    *,
    benchmark_trade_date: str,
    now: datetime,
    max_age_seconds: int = 300,
    max_future_skew_seconds: int = 120,
) -> Dict[str, Any]:
    ...
```

盘中按命令时间判断 5 分钟滞后和 2 分钟未来偏差；收盘后要求同交易日、时间不早于
14:55 且不晚于当前时间 2 分钟。

- [ ] **Step 6：运行腾讯测试，确认绿灯**

运行：

```bash
.venv/bin/python -m pytest tests/test_tencent_quote_service.py -q
```

预期：新增和原有单股、日线测试全部通过。

## Task 3：实现公开全市场初筛和确定性百分位

**文件：**

- 新增：`app/services/public_candidate_discovery_service.py`
- 新增：`tests/test_public_candidate_discovery_service.py`

- [ ] **Step 1：先写中位秩百分位测试**

```python
def test_midrank_percentiles_are_deterministic():
    assert midrank_percentiles([100.0]) == [1.0]
    assert midrank_percentiles([100.0, 200.0, 300.0]) == [0.0, 0.5, 1.0]
    assert midrank_percentiles([100.0, 100.0, 300.0]) == [0.25, 0.25, 1.0]
```

- [ ] **Step 2：先写沪深京、风险过滤和分桶配额测试**

测试必须证明：

- `600000`、`000001`、`300750`、`430047` 可参与；
- ST、退市、成交额低于 1 亿元、涨幅高于 3%、跌幅低于 -1.5% 被记录；
- 40 只上限按 30 只温和强势和 10 只可控回撤分配，不足时回填；
- 接近 5% 的股票不因涨幅高被排到首位；
- 排序 tie 依次使用分数、成交额、一手金额和代码。

- [ ] **Step 3：运行测试，确认红灯**

运行：

```bash
.venv/bin/python -m pytest tests/test_public_candidate_discovery_service.py -q
```

预期：模块或函数尚不存在而失败。

- [ ] **Step 4：实现纯函数初筛**

实现接口：

```python
def midrank_percentiles(values: Sequence[float]) -> List[float]:
    ...

def rank_public_candidate_universe(
    rows: Iterable[Dict[str, Any]],
    *,
    benchmark_trade_date: str,
    limit: int = 40,
) -> Dict[str, Any]:
    ...
```

`rank_public_candidate_universe` 不读取现金、Mongo 或行业字段。成交额百分位只在基础过滤后且涨跌幅位于 `[-1.5, 3.0]` 的集合内计算，输出
`definitions/rejection_counts/eligible_count/selected_bucket_counts`。

评分必须直接实现批准设计中的确定公式：

```python
public_score = 0.65 * amount_percentile + 0.35 * move_quality

if bucket == "strength":
    move_quality = (
        (pct_chg - 0.3) / (1.5 - 0.3)
        if pct_chg <= 1.5
        else (3.0 - pct_chg) / (3.0 - 1.5)
    )
else:
    move_quality = (
        (pct_chg - (-1.5)) / (-0.5 - (-1.5))
        if pct_chg <= -0.5
        else (0.3 - pct_chg) / (0.3 - (-0.5))
    )
move_quality = min(1.0, max(0.0, move_quality))
```

- [ ] **Step 5：运行公开初筛测试，确认绿灯**

运行：

```bash
.venv/bin/python -m pytest tests/test_public_candidate_discovery_service.py -q
```

预期：初筛和百分位测试通过。

## Task 4：实现腾讯复核、质量排名和阶段来源

**文件：**

- 修改：`app/services/public_candidate_discovery_service.py`
- 修改：`tests/test_public_candidate_discovery_service.py`

- [ ] **Step 1：先写响应集合有效性和动态覆盖门槛测试**

覆盖：额外代码、重复代码、交易所前缀错配、无效价格、错交易日、陈旧行情均不计成功数；最小成功数为：

```python
max(math.ceil(0.8 * requested_count), min(20, requested_count))
```

`requested_count == 0` 必须返回 `no_eligible_candidates` 和
`not_called_no_preselection`，且假 fetcher 未被调用。

- [ ] **Step 2：先写腾讯硬过滤和分数排序测试**

覆盖涨跌幅、换手率、振幅、流通市值、总市值、涨停距离和量比分档；成交额和总市值百分位只在通过全部硬过滤的集合中计算，tie 使用分数、成交额、振幅和代码。

- [ ] **Step 3：运行测试，确认红灯**

运行：

```bash
.venv/bin/python -m pytest tests/test_public_candidate_discovery_service.py -q
```

预期：腾讯复核接口尚不存在而失败。

- [ ] **Step 4：实现腾讯复核纯函数和编排函数**

实现：

```python
def verify_and_rank_tencent_candidates(
    definitions: Sequence[Dict[str, Any]],
    quote_rows: Sequence[Dict[str, Any]],
    *,
    benchmark_trade_date: str,
    now: datetime,
    limit: int = 8,
) -> Dict[str, Any]:
    ...

def discover_public_candidate_universe(
    snapshot: Dict[str, Any],
    *,
    fetch_quotes: Callable[[Iterable[str]], Dict[str, Any]],
    now: datetime,
) -> Dict[str, Any]:
    ...
```

腾讯评分按以下公式实现，不增加隐含权重：

```python
tencent_score = (
    0.30 * amount_percentile
    + 0.25 * move_quality
    + 0.15 * turnover_quality
    + 0.10 * volume_ratio_quality
    + 0.10 * amplitude_quality
    + 0.10 * market_cap_percentile
)
```

`turnover_quality` 在 0 至 0.5% 线性升至 1、0.5% 至 5% 为 1、5% 至 10% 线性降至 0；`amplitude_quality` 在 4% 以下为 1、4% 至 8% 线性降至 0；量比 `[0.8, 2.0]` 为 1，`[0.5, 0.8)` 或 `(2.0, 3.0]` 为 0.5，其他及缺失为 0。硬过滤严格使用设计中的 `[-1.5%, 3.0%]`、换手率 10%、振幅 8%、总市值 20 亿元、流通市值 10 亿元及距涨停价 0.5% 门槛。

成功结果必须返回已验证 `quote_map`，供技术深检直接复用；同时生成完整
`candidate_discovery` DTO 和动态 `source/stage_sources`。

- [ ] **Step 5：验证公开候选服务**

运行：

```bash
.venv/bin/python -m pytest tests/test_public_candidate_discovery_service.py -q
```

预期：全部通过。

## Task 5：实现命令级市场上下文和 90 秒截止时间

**文件：**

- 新增：`app/services/opportunity_market_context.py`
- 新增：`tests/test_opportunity_market_context.py`
- 修改：`app/services/holdings_cli.py` 中 `_build_a_share_market_gate` 和 `build_market_status_payload`

- [ ] **Step 1：先写同一命令只取一次指数和一次新浪快照测试**

使用计数 fake fetcher，先让市场宽度请求快照，再让公开候选请求快照，断言底层新浪 fetcher 只执行一次；四个指数使用一次腾讯批量请求。

- [ ] **Step 2：先写确定性截止时间测试**

注入假 `monotonic`，覆盖剩余预算计算、阶段预算截断和 90 秒后返回明确阶段超时，不真实等待。

测试固定阶段上限：Mongo 5 秒、腾讯市场上下文 10 秒、新浪 25 秒、腾讯候选复核 10 秒、技术深检 35 秒、编排和序列化 5 秒；每一阶段实际 timeout 均为该阶段上限与命令剩余时间的较小值。

- [ ] **Step 3：运行测试，确认红灯**

运行：

```bash
.venv/bin/python -m pytest tests/test_opportunity_market_context.py -q
```

- [ ] **Step 4：实现上下文对象**

```python
@dataclass
class OpportunityMarketContext:
    now: datetime
    started_at: float
    deadline_at: float
    index_quotes: List[Dict[str, Any]]
    benchmark_trade_date: Optional[str]
    public_snapshot: Optional[Dict[str, Any]] = None
    public_snapshot_loaded: bool = False

    def remaining_seconds(self, monotonic: Callable[[], float]) -> float:
        return max(0.0, self.deadline_at - monotonic())
```

提供 `build_opportunity_market_context` 和幂等 `ensure_public_snapshot`。指数日期缺失或沪深日期不一致时失败关闭；快照超时使用
`min(25, remaining_seconds)`。

命令截止时间必须在尝试 Mongo 连接之前创建。为 `opportunities` 增加专用的 5000ms Mongo 超时覆盖，将 `connectTimeoutMS`、`socketTimeoutMS` 和
`serverSelectionTimeoutMS` 限制为配置值与 5000ms 的较小值；其他 CLI 命令继续使用现有配置值。

- [ ] **Step 5：让市场门禁消费已注入上下文**

`_build_a_share_market_gate` 增加可选上下文参数；有上下文时不得再次请求指数或新浪。独立 `market-status` 命令仍创建自己的单命令上下文并保持 Schema 1。

- [ ] **Step 6：运行上下文和现有市场门禁测试**

运行：

```bash
.venv/bin/python -m pytest \
  tests/test_opportunity_market_context.py \
  tests/test_public_market_breadth.py \
  tests/test_cli_holdings.py -q
```

预期：新增复用测试与原有 `market-status` 回归测试通过。

## Task 6：复用腾讯行情并限制技术深检为 35 秒

**文件：**

- 新增：`app/services/public_candidate_deep_check.py`
- 新增：`tests/test_public_candidate_deep_check.py`
- 修改：`app/services/holdings_cli.py` 中 `_build_opportunity_candidates`
- 修改：`tests/test_cli_holdings.py`

- [ ] **Step 1：先写已验证行情禁止单股重取测试**

给 `_build_opportunity_candidates` 注入 `quote_snapshots={"600000": quote}`，把
`fetch_tencent_quote_sync` patch 为抛错，断言候选仍使用注入行情完成构建。

- [ ] **Step 2：先写受限 worker 超时测试**

把 worker 命令替换为睡眠进程，以 0.05 秒 timeout 运行，断言进程被终止并返回：

```python
{
    "status": "technical_deep_check_timeout",
    "candidates": [],
}
```

- [ ] **Step 3：运行新测试，确认红灯**

运行：

```bash
.venv/bin/python -m pytest \
  tests/test_public_candidate_deep_check.py \
  tests/test_cli_holdings.py -q
```

- [ ] **Step 4：增加行情映射参数**

把函数签名扩展为：

```python
def _build_opportunity_candidates(
    definitions: List[Dict[str, Any]],
    *,
    cash: Optional[float],
    buy_lot_size: int,
    holding_themes: set,
    allow_reference_price_plan: bool = False,
    quote_snapshots: Optional[Mapping[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    ...
```

映射存在相应代码时禁止调用 `fetch_tencent_quote_sync`。Mongo 和手工路径不传映射，保持现有行为。

- [ ] **Step 5：实现 JSON worker 和父进程超时**

`public_candidate_deep_check.py` 通过 stdin 接收最多 8 个定义及腾讯映射，worker 内部延迟导入 `_build_opportunity_candidates`，父进程以
`min(35, command_remaining)` 为 timeout。超时后公开候选降级为腾讯已验证的次级观察项，标记
`plan_status=technical_deep_check_timeout`，仍为 0 股。

- [ ] **Step 6：运行深检测试，确认绿灯**

运行：

```bash
.venv/bin/python -m pytest \
  tests/test_public_candidate_deep_check.py \
  tests/test_cli_holdings.py -q
```

## Task 7：实现 research-only 递归安全不变量和公开响应契约

**文件：**

- 新增：`app/services/research_only_safety.py`
- 新增：`tests/test_research_only_safety.py`
- 修改：`app/services/holdings_cli.py`
- 修改：`tests/test_cli_holdings.py`

- [ ] **Step 1：先写恶意嵌套 payload 安全测试**

构造多层 payload，包含 `actionable=True`、`reference_actionable=True`、
`new_position_allowed=True`、非零建议手数/数量/新增敞口，断言清洗后：

```python
assert_all_research_only(safe_payload)
```

测试同时确认行情 `volume`、历史成交量和价格不会被误清零。

- [ ] **Step 2：运行测试，确认红灯**

运行：

```bash
.venv/bin/python -m pytest tests/test_research_only_safety.py -q
```

- [ ] **Step 3：实现显式键集合递归清洗**

```python
FALSE_KEYS = {"actionable", "reference_actionable", "new_position_allowed"}
ZERO_KEYS = {
    "suggested_lots",
    "suggested_quantity",
    "new_position_lots",
    "new_position_quantity",
    "max_new_exposure_amount",
    "max_new_exposure_pct",
    "external_new_exposure_amount",
    "market_adjusted_new_exposure_cap",
}

def enforce_research_only_safety(value: Any) -> Any:
    ...
```

只按明确交易语义键清洗，不能使用模糊的 `*quantity*` 规则破坏行情成交量。

- [ ] **Step 4：实现公开研究响应组装**

在 `holdings_cli.py` 增加
`build_public_research_opportunities_payload(...)`，输出完整
`candidate_discovery` DTO、账户不可用状态、`observe/false/0` 决策、候选和免责声明，最后统一调用安全清洗函数。

- [ ] **Step 5：覆盖成功、无候选和技术超时响应**

测试 `status=ok` 和 `status=no_eligible_candidates` 使用同一覆盖 DTO；无预选时
`source` 不包含腾讯且阶段状态为 `not_called_no_preselection`。

- [ ] **Step 6：运行安全和 payload 测试**

运行：

```bash
.venv/bin/python -m pytest \
  tests/test_research_only_safety.py \
  tests/test_cli_holdings.py -q
```

## Task 8：接入 CLI 降级、错误信封和 Schema 7

**文件：**

- 修改：`app/services/holdings_cli.py`
- 修改：`tests/test_cli_holdings.py`
- 参考：`tests/test_holdings_cli_entrypoint.py`

- [ ] **Step 1：先写 Mongo 不可用且无手工候选的成功降级测试**

把 `_get_database` patch 为连接失败，公开发现 patch 为成功，断言命令退出 0、
`data.mode=research_only`、来源为 `public_full_market`，且没有账户推断。

- [ ] **Step 2：先写允许与禁止的 Mongo 部分降级矩阵**

参数化断言只有以下状态触发公开模式：

```python
{
    "candidate_discovery_unavailable",
    "quote_universe_empty",
    "stale_quote_universe",
    "quote_universe_too_small",
}
```

`no_eligible_candidates`、`cash_unavailable`、
`benchmark_calendar_unavailable` 保留 Mongo 路径结果，不调用公开发现。

- [ ] **Step 3：先写数据源失败和真实无候选的退出码测试**

- 公开源失败：stderr JSON、`ok=false`、
  `error.code=candidate_discovery_unavailable`、单一 `details.stage`、退出码 4；
- 完整扫描后无候选：stdout JSON、`ok=true`、
  `status=no_eligible_candidates`、退出码 0。

- [ ] **Step 4：先写 Schema 兼容测试**

Mongo、手工候选、公开研究三种 `opportunities` 成功响应都断言
`meta.schema_version == 7`；`market-status` 仍为 1。

- [ ] **Step 5：运行 CLI 测试，确认红灯**

运行：

```bash
.venv/bin/python -m pytest tests/test_cli_holdings.py -q
```

- [ ] **Step 6：扩展 CLIError 结构化 details**

```python
class CLIError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "cli_error",
        exit_code: int = 2,
        details: Optional[Dict[str, Any]] = None,
    ):
        ...
```

所有 CLI 捕获点仅在 `details` 非空时加入错误信封，保持旧错误 JSON 兼容。

- [ ] **Step 7：接入公开发现和降级矩阵**

重构 `opportunities_command` 和 `build_opportunities_payload`，确保：

- 手工 `--candidate-code` 优先且语义不变；
- Mongo 正常时先走动态候选；
- 只有允许状态或数据库访问异常进入公开研究；
- 一旦来源是 `public_full_market`，即使账户曾读取成功也对整棵树执行 research-only 安全清洗；
- 使用同一个命令上下文和公开快照；
- 在打开 Mongo 前启动 90 秒截止时间，并只对 `opportunities` 使用 5 秒 Mongo 超时覆盖；
- 超过 90 秒按当前阶段返回失败，不留下子进程。

- [ ] **Step 8：统一 opportunities Schema 为 7**

只修改 `build_opportunities_payload`、
`build_research_only_opportunities_payload` 和公开研究 builder 的成功 Schema；不要改持仓、汇总、交易流水或 `market-status` 的版本。

- [ ] **Step 9：运行 CLI 回归测试，确认绿灯**

运行：

```bash
.venv/bin/python -m pytest tests/test_cli_holdings.py tests/test_holdings_cli_entrypoint.py -q
```

预期：公开模式新测试和原有 Mongo/手工/入口测试全部通过。

## Task 9：更新 Hermes 文档并完成全量验证

**文件：**

- 修改：`docs/HERMES_HOLDINGS_CLI.md`
- 修改：`tests/test_cli_holdings.py`（仅在文档契约需要快照断言时）

- [ ] **Step 1：更新 Hermes 调用流程**

明确说明 Mongo 不可用且未传代码时，以下命令会自动进行公开全市场研究筛选：

```bash
.venv/bin/holdings opportunities --external-risk-level red --pretty
```

Hermes 必须检查 `ok`、进程退出码、`candidate_discovery.status`、覆盖率、阶段来源和全部 0 股安全字段；不得把公开观察池描述成买入建议。

- [ ] **Step 2：更新旧边界描述**

删除“Mongo 不可用且未传 `--candidate-code` 必然返回 database_error”的旧说明，补充：

- `no_eligible_candidates` 是成功扫描；
- `candidate_discovery_unavailable` 是数据源失败；
- 公开模式不读取或推断本金、现金、持仓；
- Schema 7 仅适用于 `opportunities` 成功响应。

- [ ] **Step 3：运行所有相关单元测试**

运行：

```bash
.venv/bin/python -m pytest \
  tests/test_public_market_breadth.py \
  tests/test_tencent_quote_service.py \
  tests/test_candidate_discovery_service.py \
  tests/test_public_candidate_discovery_service.py \
  tests/test_opportunity_market_context.py \
  tests/test_public_candidate_deep_check.py \
  tests/test_research_only_safety.py \
  tests/test_cli_holdings.py \
  tests/test_holdings_cli_entrypoint.py -q
```

预期：全部通过，0 failed。

- [ ] **Step 4：执行语法和补丁检查**

运行：

```bash
.venv/bin/python -m compileall app/services tests
git diff --check
```

预期：两条命令退出码均为 0。

- [ ] **Step 5：在网络可用时执行真实只读冒烟测试**

运行：

```bash
/usr/bin/time -p .venv/bin/holdings opportunities --external-risk-level red --pretty
```

验收记录：总耗时不超过 90 秒、沪深京实际与预期数量、腾讯请求/验证数、技术深检数、最终候选数、退出码，以及递归 0 股不变量。Mongo 或 Docker 仍不可用不影响 A 方案测试，但不得擅自启动或重建 Docker。

- [ ] **Step 6：最终检查工作区且等待用户决定是否提交**

运行：

```bash
git status --short
```

预期：只包含本功能和此前保留的相关改动。不要自动执行 `git add`、`git commit`、`git push` 或 Docker 重启。

## Task 10：补全真实公开候选的入选依据

**文件：**

- 修改：`app/services/holdings_cli.py`
- 修改：`tests/test_cli_holdings.py`
- 修改：`docs/HERMES_HOLDINGS_CLI.md`

- [x] **Step 1：把公开发现测试夹具改为真实扁平字段**

删除夹具中人工预置的 `priority/discovery`，加入公开排名和腾讯复核实际产生的分数、
百分位与质量字段。

- [x] **Step 2：先写正常深检和超时降级的失败测试**

断言两条路径都按最终候选顺序返回非空 `priority/discovery`，并准确保留公开与腾讯
两阶段证据；无候选仍返回空数组。运行聚焦测试并确认因字段为 `None` 而失败。

- [x] **Step 3：实现规范化适配器**

在一致性校验后按定义顺序生成公开研究证据。正常深检结果按代码绑定证据，超时路径
复用同一份规范化定义。不要从技术价格计划反推发现证据，不修改账户隔离或可执行性。
只从白名单字段构造嵌套证据；必需排名证据缺失、非有限或越界时必须 fail closed，
不得透传上游未知 `discovery` 字段。公开候选、超时行情和候选发现元数据也必须按
严格 DTO 白名单重建；腾讯时间须与已验证行情及基准交易日一致，显式可空证据键必须
存在。腾讯时间须精确到秒并带 `Z` 或明确 UTC 偏移；仅日期、缺秒或缺时区时失败关闭。
覆盖元数据还须验证必需字段、数值类型/有限性和计数关系；正常深检只有同时
返回有效腾讯行情、成交量和技术计划时才能标记为完成，超时定义字段也不得透传嵌套
对象。覆盖率必须由计数重算，腾讯请求数与最小验证数必须符合发现服务公式；深检行情
及扁平腾讯证据必须重新绑定原始已验证行情的代码、交易时间、价格、成交额和成交量。

- [x] **Step 4：运行聚焦测试并确认绿灯**

运行：

```bash
.venv/bin/python -m pytest \
  tests/test_cli_holdings.py \
  -k 'public_research and (discovery or timeout or safety)' -q
```

- [x] **Step 5：更新 Hermes 字段说明并运行完整回归**

说明 `priority/discovery` 仅表示观察池排名依据。随后运行 Task 9 的相关测试矩阵、
`compileall`、`git diff --check` 和一次真实只读 `opportunities` 冒烟，不提交或重启 Docker。

## Task 11：账户手工候选保留休市技术参考计划

**文件：**

- 修改：`app/services/holdings_cli.py`
- 修改：`tests/test_cli_holdings.py`
- 修改：`docs/HERMES_HOLDINGS_CLI.md`

- [x] **Step 1：先写账户模式休市参考计划失败测试**

数据库和账户可用、用户明确传入 `--candidate-code`、腾讯行情处于休市状态时，断言候选
仍返回日线计算的入场、止损和目标参考价，但 `actionable=false`、建议手数为 0，并保留
行情时效、市场和外部风险门禁。实现前测试应因缺少 `reference_actionable` 而失败。

- [x] **Step 2：仅为手工候选开启账户感知参考计划**

账户模式调用 `_build_opportunity_candidates` 时，仅在存在显式 `candidate_codes` 时设置
`allow_reference_price_plan=True`。休市参考计划根据账户数据是否可用生成阻断项：账户已
读取成功时仅写 `quote_freshness`；无账户 research-only 路径继续同时写
`account_data_unavailable`。动态候选行为不变。计划降级为休市参考时删除
`fee_aware_trade` 中的入场、止损和目标模拟订单，只保留风险额、收益额和费后盈亏比。

- [x] **Step 3：验证安全门禁和真实 CLI**

聚焦测试和完整 `tests/test_cli_holdings.py` 通过后，以真实账户和 `external-risk=red`
复核手工候选。输出必须同时包含账户资金、一手可达性、不可执行技术参考和 0 股结论。
测试递归确认参考计划中不再包含任何带非零数量的模拟订单。

## Task 12：独立市场门禁恢复一次新浪快照超时

**文件：**

- 修改：`app/services/opportunity_market_context.py`
- 修改：`app/services/holdings_cli.py`
- 修改：`tests/test_opportunity_market_context.py`
- 修改：`tests/test_cli_holdings.py`
- 修改：`docs/HERMES_HOLDINGS_CLI.md`

- [x] **Step 1：先写精确超时恢复失败测试**

模拟第一次返回 `public_breadth_timeout`、第二次返回完整公开宽度。实现前分别因上下文
缺少单次恢复方法和 market-status builder 缺少显式开关而失败。

- [x] **Step 2：实现上下文级单次恢复**

只允许已缓存的 `public_breadth_timeout` 清空一次并重新抓取；缓存第二次结果，写入
`attempt_count=2` 和 `retried_after_status`。成功后市场宽度透出对应审计字段；其他失败
状态不触发该方法。

- [x] **Step 3：仅在独立命令启用**

`build_market_status_payload` 默认不开启重试，`market-status` 命令显式启用。
`opportunities`、公开发现、单次 25 秒上限、90 秒总预算和 research-only 安全边界均不变。

- [x] **Step 4：完成相关回归、真实只读冒烟和最终检查**

## Task 13：为技术深检保留一手金额多样性

**文件：**

- 修改：`app/services/public_candidate_discovery_service.py`
- 修改：`app/services/holdings_cli.py`
- 修改：`tests/test_public_candidate_discovery_service.py`
- 修改：`tests/test_cli_holdings.py`
- 修改：`docs/design/2026-07-15-public-full-market-research-discovery.md`
- 修改：`docs/HERMES_HOLDINGS_CLI.md`

- [x] **Step 1：用真实排名分布确认问题并先写失败测试**

2026-07-17 的 34 只腾讯硬过滤样本中有 19 只一手金额不高于当前账户 20% 单票上限，
但纯质量前 8 只中只有 1 只满足；第 9 名以后存在 18 只低金额候选。单元测试构造前 8
只均为高一手金额、后续候选为低一手金额的排名总体，确认旧实现会让高金额股票占满
深检名额。

- [x] **Step 2：实现 5 个质量核心位和 3 个金额多样性位**

候选超过 8 只时先保留 5 个腾讯质量核心位，再从腾讯一手金额较低的半区按原质量顺序
补足 3 个名额；不足时按原质量顺序补位。选择结果继续按原质量名次输出，不新增网络
请求，不读取账户信息，不修改技术、市场、外部风险和 0 股门禁。

- [x] **Step 3：保留并校验可解释证据**

定义和最终 `discovery.tencent` 白名单保留 `tencent_one_lot_amount`、
`tencent_quality_rank`、`selection_lane`。一手金额必须与腾讯价格及 100 股一手严格绑定，
质量名次必须为正整数，通道必须属于 `quality_core`、`one_lot_diversity`、`quality_fill`；
部分字段、金额错配或未知通道均 fail closed。

- [x] **Step 4：完成完整回归和真实公开扫描**

运行公开候选、CLI、市场上下文、深检和安全清洗相关矩阵，随后执行 `compileall`、
`git diff --check` 及真实只读 `opportunities`。确认最终仍是 research-only、0 股，且新
候选包含分层前质量名次和入选通道。不要提交、推送或重启 Docker。

验收记录：九个相关测试文件共 `652 passed`；真实命令首轮成功，耗时约 22 秒，公开
排名总体 34 只、最终 8 只、技术深检 8 只。最终候选包含 5 个 `quality_core` 和 3 个
`one_lot_diversity`，递归可执行字段全部为 false、仓位字段全部为 0。`compileall` 与
`git diff --check` 均通过。

## Task 14：公开降级保留已验证账户上下文

**文件：**

- 修改：`app/services/holdings_cli.py`
- 修改：`tests/test_cli_holdings.py`
- 修改：`docs/design/2026-07-15-public-full-market-research-discovery.md`
- 修改：`docs/HERMES_HOLDINGS_CLI.md`

- [x] **Step 1：先证明账户上下文在公开降级时丢失**

真实 admin 账户已配置总资产和现金 `10685.41`、当前空仓、近期存在 `000977` 以
`70.4` 卖出的记录；旧命令在 Mongo 动态候选池触发公开降级后却返回
`mode=research_only`，账户三个金额均为 null。先写纯构建器和命令路由失败测试。

- [x] **Step 2：实现不可执行的账户适配叠加层**

仅在 Mongo、用户和账户已经成功解析时复用上一阶段 payload，不新增数据库或行情请求。
输出升级为 `account_context_research_only` / Schema 8，保留账户、持仓摘要和近期交易；
逐只计算腾讯现价一手金额、现金/权益占比、已有同代码市值与 20% 单票上限。Mongo 或
账户不可用时继续使用原 Schema 7 纯公开模式。

- [x] **Step 3：持仓估值和研究边界失败关闭**

已有同代码持仓只有 `valuation_actionable=true` 且市值完整时才参与单票上限，否则返回
`account_fit_data_incomplete`。所有候选的 `blocking_reasons` 固定包含
`public_research_only`，`passes_account_size_checks` 仅表达金额可达；递归交易布尔值仍为
false、建议数量仍为 0。

- [x] **Step 4：完成完整回归和真实 CLI 验收**

运行 CLI 全量及九模块相关矩阵、`compileall`、`git diff --check`。真实命令必须保留
`10685.41`、空仓和浪潮信息卖出记录，并为 8 只候选给出账户适配；不得提交、推送或
重启 Docker。

验收记录：`tests/test_cli_holdings.py` 为 `283 passed`，九模块相关矩阵为
`656 passed`；`compileall` 和 `git diff --check` 通过。真实命令返回 Schema 8，保留
本金/现金 `10685.41`、空仓、浪潮信息 `70.4` 卖出记录，并为 8 只候选输出账户适配。
全部 `blocking_reasons` 包含 `public_research_only`，未产生任何非零建议数量。

## Task 15：公开账户适配复用近期卖出冷静期

**文件：**

- 修改：`app/services/holdings_cli.py`
- 修改：`tests/test_cli_holdings.py`
- 修改：`docs/design/2026-07-15-public-full-market-research-discovery.md`
- 修改：`docs/HERMES_HOLDINGS_CLI.md`

- [x] **Step 1：先写同代码重新入池失败测试**

构造周五卖出、周日运行且同代码进入公开候选的场景。旧 Schema 8 只保留 last_trade，
构建器不接受 as_of/交易日参数，也不输出 `recent_sale_policy` 或
`recent_sale_cooldown`，测试按预期失败。

- [x] **Step 2：复用现有两交易日冷静期策略**

账户叠加内部保留 Mongo 阶段已经读取的最近交易，以命令级 as_of 和基准交易日调用
`_build_recent_sale_policy`，重新匹配公开候选。命中候选增加
`recent_sale_cooldown`，顶层返回策略摘要；不新增数据库或行情请求。

- [x] **Step 3：完成聚焦测试和 Hermes 契约**

覆盖新鲜卖出匹配、持仓估值失败关闭、账户适配、数据库不可用和公开降级状态矩阵。
Hermes 必须交叉检查 `recent_sale_policy.matched_candidate_codes` 与候选阻断原因。

- [x] **Step 4：完成完整回归和真实命令复核**

真实账户中的 `000977` 当前不在公开 8 只，且数据库卖出日期已超过冷静期，因此不得
伪造命中；真实输出应保持策略 `expired`。随后运行完整相关矩阵、`compileall` 和
`git diff --check`。

验收记录：九模块矩阵 `657 passed`，`compileall` 和 `git diff --check` 通过。真实 admin
输出的浪潮信息卖出日期为 `2026-07-07`，截至当前已开始 8 个工作日，策略正确返回
`expired`、匹配候选为空；当前公开 8 只均未误加 `recent_sale_cooldown`。

## Task 16：深回撤候选增加趋势修复门禁

**文件：**

- 修改：`app/services/holding_price_guardrails.py`
- 修改：`app/services/holdings_cli.py`
- 修改：`tests/test_holding_price_guardrails.py`
- 修改：`tests/test_cli_holdings.py`
- 修改：`docs/design/2026-07-15-public-full-market-research-discovery.md`
- 修改：`docs/HERMES_HOLDINGS_CLI.md`

- [x] **Step 1：用真实急跌样本确认问题并先写失败测试**

`000519` 在 2026-07-17 收于 `13.63`，低于 MA5/MA10/MA20/MA60，距 20 日高点回撤
`40.25%`，距技术入场价 `14.76` 仍有 `8.29%`；旧计划虽保留突破条件，却仍把
`reference_actionable` 标成 true。先写趋势上下文、公开清洗、账户适配和仓位门禁测试。

- [x] **Step 2：输出确定性趋势上下文并失败关闭**

当回撤至少 20% 且满足 `现价 < MA5 < MA10 < MA20` 时，输出
`trend_context.state=recovery_required` 和 `trend_recovery_required` 风险标记。费后盈亏比、
止损、入场和目标参考继续保留，但可执行和参考可执行状态均为 false，完整账户模式和
Schema 8 账户适配都把该条件作为独立阻断。

- [x] **Step 3：保持公开主筛选窗口和正常候选行为不变**

不放宽 `[-1.5%, 3.0%]`，不增加网络请求，不修改持仓前端。正常趋势候选继续使用原价格
计划和费后盈亏比规则；公开结果继续 research-only 和 0 股。

- [x] **Step 4：完成相关回归和真实命令复核**

聚焦测试 4 项通过，五模块回归 `496 passed`、其余四模块 `144 passed`。真实
`opportunities` 返回 Schema 8、账户 `10685.41`、空仓、公开全市场 `5521` 只、深检 8
只且全部 0 股；真实 `000519` 复现为 `trend_recovery_required`，费后盈亏比 `2.2633`
仍保留，但 `actionable/reference_actionable=false`。

## Task 17：扩大技术覆盖并改为两段式漏斗

**文件：**

- 修改：`app/services/tencent_quote_service.py`
- 修改：`app/services/public_candidate_discovery_service.py`
- 修改：`app/services/public_candidate_deep_check.py`
- 修改：`app/services/opportunity_market_context.py`
- 修改：`app/services/holdings_cli.py`
- 修改：对应测试与 Hermes/设计文档

- [x] **Step 1：用 160 只真实候选审计确认前 8 截断漏检**

2026-07-17 的前 40 只腾讯硬门禁候选全部技术检查后无通过者，但公开排名 81 至 160
中出现 `688599`、`300113`、`002165` 三个技术通过样本。把 160 只重新按腾讯质量只取
前 8，或每 40 只各取 8，仍会漏掉这三只，证明“先按流动性截断、再看技术结构”的顺序
存在系统性漏检。

- [x] **Step 2：增加 160 只分批腾讯复核和并发纯技术初筛**

公开预选上限提升至 160；实时行情在一个 10 秒总截止时间内按 40 只分批获取，任一批
失败时丢弃全部部分结果。技术子进程使用 4 个线程检查所有腾讯硬门禁候选的前复权日线、
趋势修复和费后净收益风险比，任何真实日线抓取错误都失败关闭。

- [x] **Step 3：仅对技术通过者执行完整候选构建并输出审计计数**

技术通过者按净收益风险比、腾讯分数、一手金额和代码稳定排序，最多 8 只复用已计算
技术计划，再读取公司行动。输出增加 `technical_screened_count`、
`technical_passed_count`、`technical_selected_count`、`technical_screen_status_counts`，并
区分 `stage_sources.technical_screen` 与 `technical_deep_check`。新漏斗超时不返回半成品
候选。

- [x] **Step 4：完整回归和真实 CLI 验收**

相关 CLI、价格计划、腾讯行情、市场上下文、公开发现和安全门禁矩阵共 `638 passed`。
最终真实 `holdings opportunities --external-risk-level red` 在 `58.52s` 内返回 Schema 8：
公开全市场 `5521` 只，预选和腾讯请求/验证均为 `160`，腾讯硬门禁与技术初筛均为
`134`，技术通过、送入完整深检和最终候选均为 `3`。三个候选全部保持 `observe`、
`suggested_quantity=0`；账户覆盖后不再残留错误的 `account_data_unavailable`，红色市场门禁、
外部风险和 `public_research_only` 阻断仍完整保留。验收期间一次新浪 worker 在 25 秒边界
失败关闭，精确单次复测恢复后完整命令成功；未放宽数据完整性或安全门禁。语法编译和差异
检查通过，不提交、不推送、不重启 Docker。

## Task 18：技术候选增加最近报告期业绩预告门禁

**文件：**

- 新增：`app/services/public_candidate_earnings_risk.py`
- 修改：`app/services/public_candidate_deep_check.py`
- 修改：`app/services/public_candidate_discovery_service.py`
- 修改：`app/services/holdings_cli.py`
- 新增：`tests/test_public_candidate_earnings_risk.py`
- 修改：对应公开深检、CLI、设计与 Hermes 文档

- [x] **Step 1：核对三只技术候选并确认基本面漏检**

2026-07-17 的技术漏斗筛出 `688599`、`002165`、`300113`，但东方财富业绩预告数据明确
显示前两只 2026 年半年度归母净利润和扣非净利润均预亏；旧流程只检查技术结构和公司
行动，因此三只都会进入最终观察池。`002165` 的预告原因还明确提到受国际形势影响，主料
环氧丙烷采购成本同比上涨超过 23%，说明国际风险与公司盈利风险存在直接传导。

- [x] **Step 2：实现一次性业绩预告门禁并失败关闭**

根据基准交易日确定最近已完成报告期，一次读取最多 8 只技术幸存者对应的业绩预告。
归母或扣非净利润预测值为负、预告类型为首亏/续亏/增亏/减亏，或文字明确亏损时标记
`loss_forecast` 并剔除；无预告标记 `no_forecast`，只表示证据缺失，不表示盈利。提供方、
DTO、报告期或计数关系异常时在 `earnings_forecast_review` 阶段失败关闭。

- [x] **Step 3：输出逐只审计证据并保持 research-only 边界**

`candidate_discovery` 增加业绩筛选数、阻断数、放行数、报告期、状态统计和逐只证据，
`stage_sources` 增加独立业绩预告阶段。公司行动构建只接收业绩幸存者，公开结果仍全部
`observe`、不可执行且数量为 0；Schema 7/8 和旧深检载荷兼容语义不变。

- [x] **Step 4：完成相关回归、真实 CLI 验收和最终检查**

运行 CLI 全量与公开候选相关矩阵，随后执行真实
`holdings opportunities --external-risk-level red`、`compileall` 和 `git diff --check`。
确认两只明确预亏股票被审计剔除，只保留业绩门禁幸存者且仍为 0 股。不要提交、推送或
重启 Docker。

验收记录：`tests/test_cli_holdings.py` 为 `290 passed`，九模块相关矩阵为
`655 passed`。真实命令在 `56.78s` 内返回 Schema 8：公开全市场 `5521` 只、腾讯复核
`160` 只、技术初筛 `134` 只、技术通过 `3` 只；业绩预告核对阻断 `688599` 和
`002165`，仅 `300113` 进入公司行动深检。最终候选仍为 `observe`、0 股，并受红色市场、
红色外部风险和 `public_research_only` 阻断。递归安全检查、`compileall` 与
`git diff --check` 均通过；未提交、推送或重启 Docker。

## Task 19：补齐最近实际业绩门禁和腾讯估值证据

**文件：**

- 修改：`app/services/public_candidate_earnings_risk.py`
- 修改：`app/services/public_candidate_deep_check.py`
- 修改：`app/services/public_candidate_discovery_service.py`
- 修改：`app/services/holdings_cli.py`
- 修改：对应公开业绩、深检、发现、CLI、设计与 Hermes 文档测试

- [x] **Step 1：确认无预告不等于基本面通过**

旧门禁会让 `status=no_forecast` 的股票直接进入公司行动深检，只能证明该报告期没有找到
预告，不能证明公司最近一期已经盈利。真实核对 `300113` 时确认 2026 年半年度尚无预告，
但 2026 年一季报已有可用实际业绩，因此需要把最近法定披露期实际结果作为独立强制证据。

- [x] **Step 2：实现预告与实际业绩双门禁并失败关闭**

在原有 `stock_yjyg_em` 预告批量查询之外，增加 `stock_yjbb_em` 最近法定披露期批量查询。
只有归母净利润为正且公告日不晚于基准交易日时标记 `positive_profit`；实际亏损、零利润、
缺行、缺利润、只有未来公告或提供方失败均阻断。营收同比大幅下降、利润同比下降和每股
经营现金流为负保留为审计风险标记，不单独生成买入结论。

- [x] **Step 3：统一严格 DTO 校验并保留腾讯估值**

父进程与受限 worker 复用同一个业绩元数据校验器，严格核对两个数据源、两个报告期、
状态与利润符号、公告日期、风险标记、顺序、计数和阻断并集。CLI 输出新增
`earnings_actual_report_period`、`earnings_actual_status_counts` 和逐只 `latest_actual`；
同时从同一腾讯行情快照保留 `pe_ratio`、`pb_ratio`、`circ_mv`、`total_mv`，并验证中间层
没有改写这些证据。

- [x] **Step 4：完成真实数据、相关回归和完整 CLI 验收**

真实批量业绩调用在 `7.20s` 内确认：`688599` 和 `002165` 的 2026 年一季报归母净利润
均为负且 2026 半年度明确预亏；`300113` 一季报归母净利润 `80491519.51`、同比
`+9.56%`，但营收同比 `-50.77%`，因此带 `severe_revenue_contraction` 风险标记继续观察。
九模块相关矩阵共 `671 passed`。真实 `holdings opportunities --external-risk-level red`
在 `58.90s` 内返回 Schema 8：全市场 `5521` 只、腾讯验证 `160` 只、技术初筛 `134` 只、
技术通过 `3` 只，业绩双门禁阻断 `2` 只，只保留 `300113`；其腾讯快照为 `17.01` 元、
PE `27.0`、PB `4.7`、总市值约 `114.68` 亿元。账户本金/现金 `10685.41`、当前空仓，
市场和外部风险均为红色，候选仍为 `observe`、0 股。递归不可执行/零数量检查、
`compileall` 和 `git diff --check` 全部通过；未提交、推送或重启 Docker。

## Task 20：保留最接近技术门槛的淘汰样本

**文件：**

- 修改：`app/services/public_candidate_deep_check.py`
- 修改：`app/services/public_candidate_discovery_service.py`
- 修改：`app/services/holdings_cli.py`
- 修改：公开深检、发现、CLI 测试及设计/Hermes 文档

- [x] **Step 1：确认技术淘汰聚合造成解释信息丢失**

公开扫描只输出 `technical_screen_status_counts`。大量股票被聚合为
`net_rr_below_1_5` 后，Hermes 无法区分风险收益比接近 1.5 的股票和远低于门槛的股票，
也无法解释严格漏斗为何只剩少量候选。该缺口不应通过放宽门槛解决。

- [x] **Step 2：增加严格不可执行的最近门槛样本**

受限 worker 从 `net_rr_below_1_5` 结果中按风险收益比降序、腾讯分数降序、一手金额升序、
代码升序保留最多 5 只。输出仅含代码、名称、风险收益比、距 1.5 门槛差值和腾讯分数，
并固定 `earnings_review_status=not_reviewed`、`actionable=false`、
`is_reference_only=true`。它们不进入业绩复核、公司行动深检或最终候选。

- [x] **Step 3：统一父进程/CLI 严格校验并更新 Hermes 契约**

技术初筛元数据校验由 worker 父进程与 CLI 共享，重算样本数、门槛差值、腾讯分数和稳定
顺序，验证代码不与技术通过者重叠；任何可执行值、未知字段、计数或排序不一致均以
`InvalidTechnicalScreenMetadata` 失败关闭。CLI 输出增加
`technical_closest_rejection_count`、`technical_closest_rejections`，并在实际存在时把
`technical_closest_rejections` 加入 `context.available_data`。Hermes 必须把它们称为
“最接近技术门槛的淘汰样本”，不得称为候选或基本面已通过。

- [x] **Step 4：完成真实研究、完整回归和安全验收**

九模块相关矩阵共 `674 passed`。最终真实
`holdings opportunities --external-risk-level red` 在 `56.27s` 内返回 Schema 8：公开
全市场 `5521` 只、腾讯验证 `160` 只、技术初筛 `134` 只、技术通过 `3` 只，最终仍只保留
`300113` 为 0 股观察项。最近技术淘汰样本为 `002318`、`300803`、`000100`、`301291`、
`000777`；其中 `002318` 风险收益比 `1.381` 最接近门槛，其余仅 `1.0174-1.0653`。
额外真实业绩核对显示 `000777` 最近一季亏损；`002318` 半年归母净利润预计同比下降
`50%-55%`；`000100` 半年归母净利润预计同比增长 `96%-108%`，但技术风险收益比仅
`1.0607`。市场门禁和外部风险均为红色，递归不可执行/零数量检查无违规；`compileall`
与 `git diff --check` 通过。未提交、推送或重启 Docker。

## Task 21：为 Hermes 增加独立公开业绩核对命令

**文件：**

- 修改：`app/services/holdings_cli.py`
- 修改：`tests/test_cli_holdings.py`
- 修改：设计、实施和 Hermes 文档

- [x] **Step 1：确认最接近技术门槛样本缺少公开复核入口**

`technical_closest_rejections` 有意固定为 `earnings_review_status=not_reviewed`，但原 CLI 没有
独立批量业绩命令；Hermes 若继续核对，只能绕过 CLI 调用 Python 内部服务，无法获得稳定
JSON 契约和统一失败语义。

- [x] **Step 2：实现不依赖登录和 Mongo 的 `holdings earnings`**

命令去重后最多接收 8 只 A 股，先用腾讯主要指数确定基准交易日，再复用公开业绩预告、
最新法定披露期实际业绩及严格元数据校验。无效输入在任何网络调用前失败；提供方和 DTO
异常输出结构化阶段错误。成功响应固定为 Schema 1、`public_research_only`、不可执行和
0 股，业绩放行不能升级为候选。

- [x] **Step 3：补充命令边界测试和 Hermes 调用规则**

覆盖重复代码去重、最多 8 只、输入预检、禁止 Mongo、提供方失败细节、伪造结果拒绝和
递归 research-only 安全。Hermes 在全市场扫描后可批量复核最近技术淘汰样本，但必须继续
称其为技术淘汰样本。

- [x] **Step 4：完成完整回归和真实五股命令验收**

运行相关九模块矩阵、`compileall`、`git diff --check`，再用真实腾讯基准日核对
`002318`、`300803`、`000100`、`301291`、`000777`。确认输出不触达 Mongo、不包含账户
字段，且整棵响应不可执行、数量为 0。

验收记录：九模块矩阵 `681 passed`，`compileall` 和 `git diff --check` 通过。真实命令在
`5.60s` 内返回 Schema 1，以腾讯 `2026-07-17` 为基准日，核对 2026 半年度预告和 2026
一季报实际业绩；`000777` 因实际亏损被阻断，其余四只只标记为业绩门禁未阻断，未升级
为候选。响应内账户/数据库对象、可执行布尔违规和非零建议数量均为 0；未提交、推送或
重启 Docker。

## Task 22：为独立市场门禁补充交易时段证据

**文件：**

- 修改：`app/services/holdings_cli.py`
- 修改：`tests/test_cli_holdings.py`
- 修改：设计、实施和 Hermes 文档

- [x] **Step 1：复现盘前结果缺少时段语义**

2026-07-20 盘前运行 `market-status` 时，腾讯和新浪正确返回 2026-07-17 最近交易日，
但响应只有基准交易日，没有直接输出当前为 `pre_open` 和下一刷新时间，Hermes 容易把
最近交易日门禁误写成 7 月 20 日实时盘面。

- [x] **Step 2：复用命令时钟输出 `market_session`**

`build_market_status_payload` 使用同一个 `OpportunityMarketContext.now` 调用既有
`_market_session_context`，在 Schema 1 中增加上海本地时间、时段、连续交易状态、行情
陈旧风险和下一刷新时间。市场门禁和数据完整性算法保持不变。

- [x] **Step 3：增加确定性盘前测试并更新 Hermes 契约**

固定 2026-07-20 01:07，验证 `session=pre_open`、`quote_stale_risk=true`、下一刷新为
09:30，同时保留 2026-07-17 基准交易日。Hermes 必须把非连续交易时段结果称为最近可用
交易日基线。公开及手工 research-only 的 opportunities 必须在
`data.market_status.market_session` 保留同一证据，账户叠加不能删除它。

- [x] **Step 4：完成回归和真实盘前命令验收**

运行 CLI 及九模块矩阵、`compileall`、`git diff --check`，再真实执行一次
`holdings market-status --pretty`，确认命令时段与腾讯基准交易日没有混淆。

验收记录：九模块矩阵 `682 passed`。真实命令在 2026-07-20 01:14 上海时间返回
Schema 1、`session=pre_open`、`quote_stale_risk=true`、下一刷新时间 09:30，同时明确
腾讯基准交易日仍为 2026-07-17；最近交易日红色门禁和盘前状态没有混淆。`compileall`
和 `git diff --check` 通过；未提交、推送或重启 Docker。

## Task 23：为候选增加独立近期公告核查命令

**文件：**

- 新增：`app/services/public_candidate_notice_review.py`
- 修改：`app/services/holdings_cli.py`
- 新增：`tests/test_public_candidate_notice_review.py`
- 修改：CLI 测试及设计、实施和 Hermes 文档

- [x] **Step 1：确认周末公告是公开研究缺口**

原公开漏斗只检查业绩和公司行动，没有覆盖最近交易日收盘后及周末发布的公司公告。
真实按日查询 2026-07-17 至 2026-07-20 后确认：`000100` 有回购完成及重大资产重组材料，
`002318` 有增持完成公告，`300803` 有子公司半年度未经审计财务报表；只依赖周五行情会
漏掉这些需要人工阅读的证据。

- [x] **Step 2：实现 7 个自然日批量公告服务和严格 DTO**

服务按上海本地截止日一次读取 7 个自然日的东方财富全市场公告，只过滤最多 8 个请求
代码，按 URL 去重并为每只保留最新 20 条。标题标签只用于人工核查，不做情绪或买卖
判断。任一天提供方失败、字段/日期/URL 异常，或成功 DTO 的顺序、计数、标签和截断关系
不一致时整次失败关闭，不暴露部分结果。

- [x] **Step 3：接入无登录、无 Mongo 的 `holdings notices` 并更新 Hermes**

命令先校验代码，再建立一次腾讯市场上下文；成功固定为 Schema 1、
`public_research_only`、不可执行和 0 股，并同时输出腾讯基准交易日与命令市场时段。
Hermes 必须对最终观察项和技术淘汰样本去重分批核查，且不得根据标签升级或剔除候选。

- [x] **Step 4：完成完整回归和真实六股验收**

运行公告服务、CLI 及公开研究相关矩阵、`compileall`、`git diff --check`，再真实核对
`300113`、`000100`、`002318`、`300803`、`301291`、`000777`。记录公告窗口、腾讯基准
交易日、命中代码、截断状态和整棵响应的 0 股安全不变量；不提交、不推送、不重启 Docker。

验收记录：十模块相关矩阵 `707 passed`，`compileall` 和 `git diff --check` 通过。真实命令
在 2026-07-20 盘前返回 Schema 1、`public_research_only`，腾讯基准交易日为
`2026-07-17`，公告窗口为 `2026-07-14` 至 `2026-07-20`。六只中 `000100`、`002318`、
`300803` 有公告；总计 28 条、返回 24 条，其中 `000100` 为 24 条并截断返回最新 20 条。
`300113`、`301291`、`000777` 在窗口内无记录。响应固定 `observe`、不可执行、建议手数和
数量均为 0；未提交、推送或重启 Docker。

## Task 24：稳定新浪全市场快照并暴露覆盖证据

**文件：**

- 修改：`app/services/public_market_breadth.py`
- 修改：`app/services/public_candidate_discovery_service.py`
- 修改：`app/services/holdings_cli.py`
- 修改：对应公开宽度、发现、CLI、设计和 Hermes 文档测试

- [x] **Step 1：复现串行分页超过 25 秒**

2026-07-20 02:05 盘前真实运行 `market-status`，新浪 worker 连续两次达到 25 秒上限，
只能返回 `indices_only`。源码确认 AKShare `stock_zh_a_spot()` 会串行读取约 56 至 70 个
分页，单次延迟波动足以触发命令超时，立即整次重试还会增加提供方限流风险。

- [x] **Step 2：改为有界并发分页且保持失败关闭**

直接复用同一个新浪 Market Center 端点，先读取总数，再由最多 8 个 worker 按每页 100 只
并发抓取。每页 HTTP、JSON 和精确行数均须有效，最后按页号恢复稳定顺序；任一缺页整批
失败。真实原型返回 `5527/5527` 个原始代码，耗时 `5.13s`，没有改变后续日期、时效、
沪深京覆盖和唯一代码门禁。

- [x] **Step 3：修正开盘时间戳边界并传播覆盖审计**

新浪行只有时间没有日期。锚点切到当日后，个别未刷新行可能仍显示前日 `15:00`；旧聚合
会把单行误认为当天未来时间并拖垮整个快照。新逻辑只排除晚于命令时间 2 分钟以上的行，
记录 `excluded_future_time_count`，再用 95% 覆盖门禁决定整批是否可用；收盘后合法延迟
结算时间仍保留。`market-status` 同时传播实际/预期总量、沪深京数量和覆盖率。

- [x] **Step 4：完成完整回归与真实端到端验收**

运行相关十模块矩阵、`compileall` 和 `git diff --check`；真实运行 `market-status` 和盘前
`opportunities --external-risk-level red`，记录耗时、覆盖、漏斗和 research-only 安全字段。
开盘后仍必须重新获取 2026-07-20 当日快照，盘前验收不能替代当天研究。

验收记录：十模块相关矩阵 `712 passed`，`compileall` 和 `git diff --check` 通过。真实
`market-status` 在 `5.82s` 内返回 `indices_and_public_breadth`：唯一股票 `5521`、提供方
预期 `5527`，总覆盖 `99.89%`，沪深京覆盖分别约 `99.91%`、`99.90%`、`99.70%`，无需
超时重试。真实盘前 `opportunities --external-risk-level red` 在 `49.07s` 内返回 Schema 8：
腾讯复核 `160` 只、技术初筛 `134` 只、技术通过 `3` 只、业绩阻断 `2` 只，仍仅保留
`300113` 为不可执行 0 股观察项。账户本金/现金 `10685.41`、当前空仓；市场与外部风险
均为红色。未提交、推送或重启 Docker。

## Task 25：阻断严重业绩恶化并统一手工候选业绩门禁

**文件：**

- 修改：`app/services/public_candidate_earnings_risk.py`
- 修改：`app/services/public_candidate_deep_check.py`
- 修改：`app/services/holdings_cli.py`
- 修改：业绩服务、CLI、设计、实施和 Hermes 文档测试

- [x] **Step 1：修复正利润但基本盘严重收缩仍被放行的问题**

盘前真实结果中 `300113` 最近一季营收同比下降 `50.77%`，却因归母净利润仍为正而通过旧
门禁。新规则在原有预告亏损、实际亏损和实际缺失之外，增加 -30% 严重恶化阈值：任何
相关预告同比变动、实际营收同比或实际净利润同比不高于该值，都阻断新仓。温和利润下降
和负经营现金流继续保留为警告，不把正利润本身解释为通过全部条件。

- [x] **Step 2：让显式手工候选也经过同一业绩证据核对**

`opportunities --candidate-code` 去重后最多接受 8 只，在有账户和无账户路径都输出整批
`earnings_review` 及逐只 `earnings_gate`。业绩硬门禁传播为 `earnings_risk_gate`，提供方或
基准日期不可用传播为 `earnings_review_unavailable`；仓位计划均保持 0。命令级只计算
一次业绩复核，并在 Mongo 懒读取失败后的 research-only 降级中复用，避免重复外部请求。

- [x] **Step 3：补齐失败关闭、数量和单次复用回归**

覆盖非亏损预告同比下降 `52%`、实际营收同比下降 `50.77%`、温和利润下降 `19.8%`、
业绩提供方不可用、超过 8 只、阻断后 0 手/0 股，以及完整构建降级后业绩接口只调用一次。
定向命令级回归 `16 passed`；业绩与深检定向回归 `58 passed`，完整 CLI 文件回归在最终
验收中重新执行。

- [x] **Step 4：完成完整回归与 7 月 20 日开盘后真实验收**

运行公开研究相关十模块矩阵、`compileall` 和 `git diff --check`；在连续交易开始并稳定后
重新读取当日腾讯指数、新浪全市场覆盖、账户和公开全市场漏斗。不得继续引用 7 月 17 日
盘前基线作为 7 月 20 日买入结论。

验收记录：十一模块相关矩阵 `728 passed`。2026-07-20 09:40 真实公开扫描以当日腾讯指数
为基准，新浪唯一股票 `5523/5527`，总覆盖 `99.93%`，沪深京均高于 `99.69%`；腾讯复核
`160` 只，日线技术初筛 `152` 只，但技术通过和最终候选均为 `0`。最近技术淘汰样本为
`601688`、`300059`、`300065`、`300085`、`000100`；独立业绩核对进一步阻断营收和利润
严重收缩的 `300065` 及实际亏损的 `300085`，另外三只只表示业绩门禁未阻断，仍未通过
费后风险收益比 1.5。手工三股真实命令在 `44s` 内返回同日新鲜腾讯行情、逐只业绩证据和
0 股 research-only 结果。Docker daemon 未运行，`summary/list` 明确返回数据库错误，因而
本轮未沿用旧本金或空仓状态。`compileall` 和 `git diff --check` 通过；未提交、推送或重启
Docker。

## Task 26：解释强势盘面的尾部风险并恢复瞬时公司行动失败

**文件：**

- 修改：`app/services/a_share_market_regime.py`
- 修改：`app/services/corporate_action_service.py`
- 修改：市场门禁、公司行动、设计和 Hermes 文档测试

- [x] **Step 1：区分多数下跌与少数深跌尾部风险**

7 月 20 日 09:46 真实快照有 `4269` 只上涨、`1154` 只下跌，但因 `29` 只近跌停股票达到
既有绝对数量阈值而返回黄色。风险预算减半本身保持不变，不为一次强势开盘放宽门槛；
新增 `risk_triggers` 和 `limit_down_like_ratio_pct`，仅由深跌/近跌停尾部触发时使用准确
文案，不再把结果笼统描述成多数股票下跌。

- [x] **Step 2：对公司行动提供方的快速解析异常重试一次**

手工三股核对中 `601688` 和 `000100` 分别瞬时返回 `KeyError('records')` 和 JSON 解析错误，
随后直接复查均成功。公司行动服务仅对 `KeyError`/`ValueError` 这类快速结构异常重试
一次；网络超时和其他运行时异常不重试，避免最多 8 只候选耗尽 90 秒命令预算。真实复查
两只股票均返回 `no_upcoming_corporate_action`，但不改变技术门禁和公告人工核查结论。

- [x] **Step 3：完成回归与静态检查**

新增强势但尾部风险样本和瞬时解析失败后恢复样本。包含市场、公司行动、公开研究、CLI
和安全门禁的十二模块矩阵 `737 passed`；最终 `compileall` 和 `git diff --check` 在 10:30
复扫后统一执行。

- [x] **Step 4：10:30 复扫并验证实时解释字段**

重新运行 `market-status` 和公开全市场 `opportunities`，确认当日覆盖、指数、涨跌家数、
`risk_triggers`、近跌停比例和候选漏斗随盘面更新；不得用 09:40 单点结果代替上午结论。

验收记录：10:30 快照唯一股票 `5523/5527`，上证 `+0.65%`、深证 `-0.54%`、创业板
`+0.08%`、科创 50 `-1.40%`；上涨 `2883`、下跌 `2511`，深跌 `223`、近跌停 `91`，
近跌停绝对数量触发红色宽度门禁，新仓敞口为 0。随后完整扫描在技术阶段达到 50 秒预算，
暴露出两个实现问题：4 个 worker 无法稳定完成约 130 只日线筛选，外层还把专用超时错误
错误包装成了普通候选发现失败。该问题转入 Task 27 修复，不能把这次失败解释为 0 只候选。

## Task 27：修复技术漏斗超时并扩展持续公告风险回看

**文件：**

- 修改：`app/services/public_candidate_deep_check.py`
- 修改：`app/services/public_candidate_notice_review.py`
- 修改：`app/services/holdings_cli.py`
- 修改：技术深检、公告、CLI、设计、实施和 Hermes 文档测试

- [x] **Step 1：复现并定位技术阶段超时语义丢失**

10:30 完整公开扫描完成新浪快照、腾讯 160 只复核和 129 至 135 只预筛后，在固定 50 秒
技术阶段预算终止。单独逐股调试没有出现日线抓取错误，但以 4 个 worker 执行完整漏斗会稳定
返回 `technical_deep_check_timeout`；外层随后将它重写为 `candidate_discovery_unavailable`，既
隐藏根因，也容易被误读为普通无候选。

- [x] **Step 2：提升吞吐、保留专用错误并隔离单票瞬时失败**

技术日线并发上限由 4 调整为 6，50 秒阶段预算、每个底层 HTTP 超时以及任一真实
`fetch_error` 的失败记录均保持不变。CLI 编排层原样传播
`technical_deep_check_timeout` 和 `stage=technical_deep_check`，不再包装为普通发现失败。
新增测试确认最多只同时执行 6 个日线请求，并确认专用错误能够穿透完整公开工作流。午后
真实复扫进一步证明单只日线请求失败会不稳定地拖垮 130 只以上的整批结果，因此改为剔除
少量失败股票；有效日线覆盖率必须至少 90%，低于阈值仍整批失败关闭。

- [x] **Step 3：为最终候选增加最多 90 天的代码级公告回看**

默认 7 天公告命令和按日全市场接口保持兼容；新增 `--lookback-days 1..90`，超过默认窗口
时按代码和日期区间调用东方财富个股公告接口，每只只请求一次。新增
`sanctions_or_trade_restrictions` 标签，覆盖制裁、SDN/实体清单、出口管制、贸易限制和
禁运标题；标签只要求人工读原文，不自动给出情绪或交易结论。最终候选必须回看 90 天，
技术淘汰样本仍可使用默认 7 天快速核对。

- [x] **Step 4：完成完整回归和真实风险验收**

午后公开全市场扫描覆盖 `5523/5527` 只股票，腾讯复核 160 只，132 只进入日线技术筛选，
38 只技术通过、8 只进入业绩复核、5 只最终通过。90 天公告源使用
`akshare.eastmoney.stock_individual_notice_report`；最终主组合未发现制裁、减持或权益融资
阻断，陕西煤业 20.5 亿元融资租赁为子公司设备采购关联交易，不是股权稀释。外部风险红色
在常规模式仍保持 0 股；只有用户显式截止日目标模式才按软约束处理。

## Task 28：支持显式截止日仓位目标并修复回踩价格层级

**文件：**

- 修改：`app/services/holding_price_guardrails.py`
- 修改：`app/services/holding_risk_sizing.py`
- 修改：`app/services/holdings_cli.py`
- 修改：价格计划、风险定额、CLI、设计、实施和 Hermes 文档测试

- [x] **Step 1：定位 60% 截止目标长期无法完成的目标冲突**

旧现金计划把外部风险红色、市场门禁红色、50% 首批上限、20% 单票上限和 0.75% 组合
损失预算同时设为硬约束。即使存在业绩和技术均可研究的候选，外部风险红色也会先把
`remaining_new_exposure` 固定为 0；这是目标优先级冲突，不是全市场没有股票。

- [x] **Step 2：增加费用后回踩路径并修复同一压力位重复使用**

突破计划保持兼容；突破费后收益风险比不足时，再评估距现价不超过 3% 的最近支撑回踩，
并继续要求费用后净收益风险比不低于 1.5。修复先四舍五入减仓位、再把同一未舍入压力位
误当作更高目标的精度错误；减仓位和目标位现在必须来自严格不同的原始压力层级。

- [x] **Step 3：实现显式截止日目标模式和账户失败关闭**

`opportunities` 新增配对参数 `--target-exposure-pct` 与 `--deployment-deadline`，并强制用户
显式列出候选。目标模式允许最多 5 个百分点的整手上浮，单票上限 25%，组合损失预算
3.5%；60% 仓位配合约 5% 技术止损时，原 3% 上限在计入费用和滑点后会形成数学冲突。
外部和市场门禁改为限价/分批提示，其余账户、行情、业绩、公司行动、价格计划、
趋势、追涨、大分歧和冷静期仍为硬阻断。Mongo 不可用时只返回
`account_data_unavailable` 和 0 股，不沿用历史本金。

- [x] **Step 4：完成 7 月 20 日午后复扫和 7 月 21 日 60% 计划验收**

午后真实复扫暴露出单票腾讯日线请求失败会中止 130-142 只整批技术漏斗；同一流程重试
曾成功返回 6 只候选，证明是单点瞬时失败而不是全市场无候选。技术漏斗现改为剔除少量
失败股票，并要求有效日线覆盖率至少 90%；低于阈值仍整批失败关闭。

13:23 腾讯连续交易报价下最终主组合固定为 `601688` 1 手、`600104` 2 手、`601225` 1 手；
限价参考分别为 `20.37`、`10.55`、`24.63`。按最后一次已核实账户样本 `10685.41` 估算，
含买入费用和滑点成本 `6628.37`、仓位约 `62.03%`，组合技术止损风险约 `2.82%`。13:23
更优实时价 `20.26`、`10.55`、`24.53` 下估算成本 `6607.36`、仓位约 `61.84%`、止损风险
约 `2.63%`。固定替补顺序为 `300059`、`600547`；不再追买已高于技术回踩价的 `600028`。
Docker daemon 当前未运行，所以这些百分比明确基于最后一次已核实本金，不冒充当前数据库
快照。最终公开研究十三模块矩阵 `767 passed`，`compileall` 与 `git diff --check` 通过。
