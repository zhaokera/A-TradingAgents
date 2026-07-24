# Codex Governed Decision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a complete vertical decision workflow in which the software produces an auditable research packet and enforces hard constraints, Codex submits the final structured proposal, and the user explicitly confirms a validated proposal.

**Architecture:** Keep `DailyDecisionService` as the deterministic software baseline, then add four focused services around it: research-packet extraction, proposal persistence, deterministic validation, and user confirmation. Expose the workflow through authenticated FastAPI routes, `agentctl`, and a Vue decision workspace while preserving existing decision history and shadow-trade infrastructure.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic v2, Motor/MongoDB, Typer, pytest/pytest-asyncio, Vue 3, TypeScript, Element Plus, Axios.

---

## Scope and sequencing

The backend domain, API/CLI, and frontend are one sequential vertical feature rather than independent products:

1. Contracts establish the only valid proposal shape.
2. Research packets provide the proposal facts and risk envelope.
3. The validator consumes those two contracts.
4. Persistence and API/CLI make the workflow usable by Codex.
5. The frontend only consumes the completed authenticated API.

The current worktree contains prerequisite decision/CLI changes that are not all committed. Before each commit, inspect `git diff --cached --name-only`; never stage unrelated paths or pre-existing hunks. If a clean scoped commit cannot be produced without absorbing unrelated user changes, leave the implementation uncommitted and report that explicitly.

## File map

### New backend files

- `app/models/decision.py`: Pydantic request and proposal contracts.
- `app/services/decision_workflow_errors.py`: shared structured workflow exception.
- `app/services/decision_research_service.py`: research-packet extraction, hard/soft taxonomy, hard risk envelope, persistence.
- `app/services/decision_validation_service.py`: deterministic proposal validation and validation persistence.
- `app/services/decision_proposal_service.py`: proposal normalization, idempotent persistence, lookup, and initial validation.
- `app/services/decision_confirmation_service.py`: explicit accept/reject events and final workspace assembly.

### New tests

- `tests/test_decision_models.py`
- `tests/test_decision_research_service.py`
- `tests/test_decision_validation_service.py`
- `tests/test_decision_proposal_service.py`

### Existing backend files

- `app/services/daily_decision_service.py:654-1009`: add baseline-authority metadata without changing deterministic buckets.
- `app/core/config.py:Settings`: add rollout and decision-safety settings.
- `app/core/database.py:create_database_indexes`: add append-only workflow indexes.
- `app/routers/decision.py`: add research, baseline, proposal, validation, final, and confirmation routes.

### Existing CLI and documentation

- `cli/agent.py:175-253, 673-780`: add workflow commands and baseline semantics.
- `tests/test_agent_cli.py`: add authenticated command-contract tests.
- `docs/cli/AGENTCTL.md`: document the new command flow.
- `docs/cli/CODEX_DECISION_PROMPT.md`: make Codex the final soft-decision authority.

### Frontend

- `frontend/src/api/decision.ts`: shared workflow types and API methods.
- `frontend/src/views/Decision/index.vue`: decision workspace.
- `frontend/src/router/index.ts`: authenticated `/decision` route.
- `frontend/src/components/Layout/SidebarMenu.vue`: decision-workspace navigation.

## Task 1: Define strict Codex proposal contracts

**Files:**

- Create: `app/models/decision.py`
- Create: `tests/test_decision_models.py`

- [ ] **Step 1: Write failing model tests**

Cover:

```python
def test_actionable_selection_requires_complete_price_plan():
    with pytest.raises(ValidationError):
        CodexDecisionProposalInput.model_validate({
            "research_packet_id": "rp_1",
            "decision_scope": {"max_new_positions": 2, "primary_position_count": 1},
            "selections": [{
                "symbol": "600406",
                "action": "condition_order",
                "position_role": "primary",
                "requested_quantity": 300,
                "thesis": "结构完整但缺少价格计划",
                "evidence_refs": ["600406:quote"],
            }],
            "portfolio_rationale": "选择一只主仓候选",
        })


def test_empty_selection_requires_no_action_reason():
    with pytest.raises(ValidationError):
        CodexDecisionProposalInput.model_validate({
            "research_packet_id": "rp_1",
            "decision_scope": {"max_new_positions": 2, "primary_position_count": 1},
            "selections": [],
            "portfolio_rationale": "当前不配置仓位",
        })


def test_wait_selection_cannot_carry_executable_quantity():
    with pytest.raises(ValidationError):
        CodexSelection.model_validate({
            "symbol": "600406",
            "action": "wait",
            "position_role": "none",
            "requested_quantity": 100,
            "thesis": "等待新的价格信号",
            "evidence_refs": ["600406:plan:short"],
        })
```

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```bash
python -m pytest tests/test_decision_models.py -q
```

Expected: collection/import failure because `app.models.decision` does not exist.

- [ ] **Step 3: Implement the Pydantic models**

Create:

```python
class DecisionAction(str, Enum):
    BUY_NOW = "buy_now"
    CONDITION_ORDER = "condition_order"
    WAIT = "wait"
    AVOID = "avoid"


class CodexDecisionOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    warning_code: str = Field(min_length=1)
    reason: str = Field(min_length=4)
    risk_adjustment: str = Field(min_length=1)


class CodexSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    action: DecisionAction
    position_role: Literal["primary", "secondary", "none"]
    requested_quantity: Optional[int] = Field(default=None, ge=0)
    entry_strategy: Optional[Literal["pullback", "breakout", "reference"]] = None
    trigger_price: Optional[Decimal] = Field(default=None, gt=0)
    stop_price: Optional[Decimal] = Field(default=None, gt=0)
    target_price: Optional[Decimal] = Field(default=None, gt=0)
    expires_at: Optional[datetime] = None
    confidence: float = Field(default=0.5, ge=0, le=1)
    thesis: str = Field(min_length=4)
    evidence_refs: list[str] = Field(min_length=1)
    overrides: list[CodexDecisionOverride] = Field(default_factory=list)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        digits = re.sub(r"\D", "", value)
        if len(digits) != 6:
            raise ValueError("symbol must contain six digits")
        return digits

    @model_validator(mode="after")
    def validate_action_shape(self):
        actionable = self.action in {
            DecisionAction.BUY_NOW,
            DecisionAction.CONDITION_ORDER,
        }
        if actionable:
            required = (
                self.requested_quantity,
                self.entry_strategy,
                self.trigger_price,
                self.stop_price,
                self.target_price,
                self.expires_at,
            )
            if any(value is None for value in required) or not self.requested_quantity:
                raise ValueError("actionable selection requires quantity and price plan")
            if not self.stop_price < self.trigger_price < self.target_price:
                raise ValueError("expected stop_price < trigger_price < target_price")
        elif self.requested_quantity not in (None, 0):
            raise ValueError("wait/avoid selection cannot carry executable quantity")
        return self
```

Add `DecisionScope`, `CodexDecisionProposalInput`, and `DecisionConfirmationInput`.
Forbid extra fields. Ensure the proposal rejects more actionable selections than
`max_new_positions`, more primary selections than `primary_position_count`, and an
empty selection without `no_action_reason`.

- [ ] **Step 4: Run tests and confirm GREEN**

Run:

```bash
python -m pytest tests/test_decision_models.py -q
```

Expected: all contract tests pass.

- [ ] **Step 5: Commit the isolated new files if safe**

```bash
git add app/models/decision.py tests/test_decision_models.py
git diff --cached --check
git commit -m "feat: define governed Codex proposal contracts"
```

## Task 2: Build an auditable ResearchPacket

**Files:**

- Create: `app/services/decision_workflow_errors.py`
- Create: `app/services/decision_research_service.py`
- Create: `tests/test_decision_research_service.py`
- Modify: `app/services/daily_decision_service.py:946-1009`

- [ ] **Step 1: Write failing research-service tests**

Use a fake baseline service and fake async collection. Cover:

```python
@pytest.mark.asyncio
async def test_market_red_is_soft_and_keeps_nonzero_hard_envelope():
    baseline = baseline_packet(
        bucket="avoid",
        reason_codes=["market_red"],
        available_cash=10_685.41,
        total_assets=10_685.41,
    )
    packet = await service_for(baseline).today("owner-1", refresh=False)
    candidate = packet["candidates"][0]

    assert [item["code"] for item in candidate["soft_warnings"]] == ["market_red"]
    assert candidate["hard_constraints"] == []
    assert candidate["risk_envelope"]["max_allowed_quantity"] >= 100


@pytest.mark.asyncio
async def test_account_blocked_remains_a_hard_constraint():
    packet = await service_for(
        baseline_packet(bucket="wait", reason_codes=["account_blocked"])
    ).today("owner-1", refresh=False)
    assert packet["candidates"][0]["hard_constraints"][0]["code"] == "account_blocked"


@pytest.mark.asyncio
async def test_research_packet_is_idempotent_for_one_baseline_snapshot():
    service = service_for(baseline_packet())
    left = await service.today("owner-1", refresh=False)
    right = await service.today("owner-1", refresh=False)
    assert left["research_packet_id"] == right["research_packet_id"]
```

Also assert:

- evidence IDs are stable and unique;
- an unknown baseline reason becomes `unclassified_gate` hard failure;
- a different user cannot load the packet;
- the source baseline is labeled `authority=software_baseline`.

- [ ] **Step 2: Run the tests and confirm RED**

```bash
python -m pytest tests/test_decision_research_service.py -q
```

Expected: import failure for the new service.

- [ ] **Step 3: Add the shared workflow exception**

Implement `DecisionWorkflowError` with:

```python
class DecisionWorkflowError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
```

- [ ] **Step 4: Implement reason classification**

In `decision_research_service.py`, define explicit sets:

```python
SOFT_WARNING_CODES = frozenset({
    "market_red",
    "objective_mismatch",
    "profile_incomplete",
    "external_risk_unverified",
})

STATUS_REASON_CODES = frozenset({
    "live_price_condition_met",
    "valid_allocated_plan",
    "live_quote_recheck_required",
    "entry_condition_not_met",
})

ACTION_SCOPED_HARD_CODES = {
    "calendar_unknown": ("buy_now",),
}
```

All existing account, plan validity, tradability, affordability, valuation,
taxonomy, concentration, correlation, and loss-budget reason codes are hard.
Unknown reasons are exposed as `unclassified_gate` with the original code in
details; they never become silently overrideable.

- [ ] **Step 5: Implement the market-independent hard risk envelope**

Derive hard limits from the baseline packet:

```python
hard_cap = policy.get("green_new_exposure_cap_pct", 60.0)
if settings.MARKET_RED_BLOCKS_NEW_POSITIONS and market_is_red:
    hard_cap = 0.0
available_exposure = max(
    0.0,
    min(
        hard_cap - account["current_exposure_pct"],
        account["available_cash"] / account["total_assets"] * 100,
    ),
)
```

For candidate sizing, call `calculate_candidate_position_sizing()` with:

- `preferred_single_symbol_pct` set to the explicit hard single-symbol cap;
- `available_new_exposure_pct` from the hard envelope rather than the software
  baseline's red-market budget;
- the candidate's short-plan entry and stop;
- existing symbol market value from `portfolio_impact.symbol_exposure`.

Store `max_allowed_quantity`, `lot_size`, hard capital/loss limits, and the
calculation basis in each candidate.

- [ ] **Step 6: Implement packet extraction and persistence**

Flatten all baseline buckets into one ordered `candidates` list. Add:

- `software_baseline_action`;
- `software_reason_codes`;
- `hard_constraints`;
- `soft_warnings`;
- stable evidence records for quote, short plan, allocation, and field-level
  profile evidence;
- `decision_objective`;
- `hard_risk_policy`;
- `source_baseline_id`;
- schema and policy versions.

Persist append-only packets in `decision_research_packets`. Use a deterministic
content hash and unique `(user_id, source_baseline_id)` lookup so repeated reads
return the same packet.

- [ ] **Step 7: Mark the existing packet as baseline**

In `DailyDecisionService._compose_packet()`, add:

```python
"authority": "software_baseline",
"is_final_decision": False,
```

Do not change any current bucket precedence or reason-code behavior.

- [ ] **Step 8: Run focused tests**

```bash
python -m pytest \
  tests/test_decision_research_service.py \
  tests/test_daily_decision_service.py -q
```

Expected: all tests pass and existing market-red baseline tests remain valid.

- [ ] **Step 9: Commit only safe paths/hunks**

New files may be committed directly. Stage the two-line baseline metadata change
only if it can be isolated from pre-existing edits in
`daily_decision_service.py`.

## Task 3: Implement deterministic proposal validation

**Files:**

- Create: `app/services/decision_validation_service.py`
- Create: `tests/test_decision_validation_service.py`

- [ ] **Step 1: Write failing validator tests**

Cover:

```python
@pytest.mark.asyncio
async def test_market_red_override_can_validate():
    result = await validator.validate_document(
        "owner-1",
        proposal_with_market_red_override(),
        red_market_research_packet(),
        now=NOW,
    )
    assert result["status"] == "valid"
    assert result["accepted_overrides"][0]["warning_code"] == "market_red"


@pytest.mark.asyncio
async def test_market_red_without_declared_override_is_invalid():
    result = await validator.validate_document(
        "owner-1",
        proposal_without_overrides(),
        red_market_research_packet(),
        now=NOW,
    )
    assert result["status"] == "invalid"
    assert result["hard_failures"][0]["code"] == "soft_warning_override_missing"


@pytest.mark.parametrize(
    ("quantity", "failure"),
    [(150, "invalid_board_lot"), (1000, "requested_quantity_exceeds_hard_limit")],
)
@pytest.mark.asyncio
async def test_quantity_is_rejected_not_rewritten(quantity, failure):
    result = await validator.validate_document(
        "owner-1",
        proposal(quantity=quantity),
        research_packet(max_allowed_quantity=300),
        now=NOW,
    )
    assert failure in {item["code"] for item in result["hard_failures"]}
    assert result["proposal_quantity"] == quantity
```

Also cover:

- cash, aggregate exposure, per-position loss and total loss;
- `stop < trigger < target`;
- missing/foreign evidence refs;
- `buy_now` outside live session;
- stale/non-Tencent `buy_now` quote;
- pullback/breakout current-price condition;
- expired plan;
- condition order succeeds off-session but requires trigger-time revalidation;
- two selected candidates share capital/loss ledgers;
- industry/theme/correlation hard caps;
- empty/`wait` decision can be valid without executable calculations.

- [ ] **Step 2: Run the tests and confirm RED**

```bash
python -m pytest tests/test_decision_validation_service.py -q
```

Expected: import failure for the validator.

- [ ] **Step 3: Implement pure validation first**

`validate_document()` must not mutate the proposal. It should:

1. Verify user, packet ID, candidate identity, evidence refs, and declared
   overrides.
2. Apply action-scoped and global hard constraints.
3. Enforce A-share board lot and two-decimal tick rules.
4. Recalculate cost, position weight, and planned loss using `Decimal` and
   `ROUND_HALF_UP`.
5. Maintain shared remaining cash, exposure, and loss ledgers.
6. Maintain taxonomy ledgers and use the existing taxonomy fallback
   (`same industry=1.00`, `same theme=0.85`, otherwise `0.50`) when empirical
   correlation is unavailable.
7. Validate live quote/session semantics for `buy_now`.
8. Return structured failures without dropping selections or changing quantity.

- [ ] **Step 4: Add append-only validation persistence**

`validate(proposal_id, user_id, refresh_quote=False)` loads the proposal and
research packet. When `refresh_quote=True`, build the newest research packet:

- same material hash: validate against the refreshed time-sensitive data;
- different material hash: persist `stale_revalidation_required` with
  `research_packet_stale`;
- missing data or provider failure: persist `invalid`.

Every result contains `validation_id`, `validated_at`, `valid_until`,
`validator_version`, and `trigger_time_revalidation_required`.

- [ ] **Step 5: Run focused tests**

```bash
python -m pytest \
  tests/test_decision_validation_service.py \
  tests/test_investment_policy.py \
  tests/test_daily_decision_service.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit new validator files if safe**

```bash
git add app/services/decision_validation_service.py tests/test_decision_validation_service.py
git diff --cached --check
git commit -m "feat: validate Codex decisions against hard limits"
```

## Task 4: Persist proposals, confirmations, and indexes

**Files:**

- Create: `app/services/decision_proposal_service.py`
- Create: `app/services/decision_confirmation_service.py`
- Create: `tests/test_decision_proposal_service.py`
- Modify: `app/core/config.py:Settings`
- Modify: `app/core/database.py:create_database_indexes`
- Modify: `tests/test_decision_scheduler.py`

- [ ] **Step 1: Write failing proposal/confirmation tests**

Cover:

- proposal normalization and content-hash idempotency;
- initial validation is returned from `submit()`;
- invalid proposals are still auditable but never confirmable;
- cross-user packet or proposal access returns not found;
- accepting an expired or invalid validation fails;
- rejecting a proposal is always allowed for its owner;
- final workspace contains research, baseline, proposal, latest validation,
  confirmation, authority mode, and disclaimer.

- [ ] **Step 2: Run tests and confirm RED**

```bash
python -m pytest tests/test_decision_proposal_service.py -q
```

- [ ] **Step 3: Implement proposal persistence**

`DecisionProposalService.submit()`:

1. loads the referenced research packet for the authenticated user;
2. normalizes `proposal.model_dump(mode="json")`;
3. computes SHA-256 over user, research packet, schema, and canonical payload;
4. inserts one immutable document or reuses the existing matching hash;
5. invokes the validator and returns `{proposal, validation}`.

The service never calls an LLM.

- [ ] **Step 4: Implement explicit confirmation**

`DecisionConfirmationService.confirm()`:

- requires an owned proposal and validation;
- accepts `accepted`/`rejected`;
- requires `status=valid` and unexpired `valid_until` for acceptance;
- appends a confirmation event;
- never places an order.

`workspace()` returns a composite read model. In
`codex_validated` mode a valid Codex proposal is the primary decision; otherwise
the software baseline is display-only with `is_final_decision=false`.

- [ ] **Step 5: Add settings**

Add validated settings:

```python
DECISION_AUTHORITY_MODE: str = Field(
    default="software_baseline",
    pattern="^(software_baseline|codex_shadow|codex_validated)$",
)
MARKET_RED_BLOCKS_NEW_POSITIONS: bool = False
CODEX_DECISION_MAX_NEW_POSITIONS: int = Field(default=2, ge=0, le=10)
CODEX_DECISION_PRIMARY_POSITION_COUNT: int = Field(default=1, ge=0, le=3)
CODEX_DECISION_VALIDATION_TTL_SECONDS: int = Field(default=60, ge=10, le=300)
```

- [ ] **Step 6: Add Mongo indexes**

Add:

- `decision_research_packets`: unique packet ID, unique
  `(user_id, source_baseline_id)`, history;
- `codex_decision_proposals`: unique proposal ID, unique
  `(user_id, proposal_hash)`, latest;
- `decision_validations`: unique validation ID and proposal history;
- `decision_confirmations`: unique confirmation ID and proposal history.

Extend `tests/test_decision_scheduler.py` to assert names and uniqueness.

- [ ] **Step 7: Run focused tests**

```bash
python -m pytest \
  tests/test_decision_proposal_service.py \
  tests/test_decision_scheduler.py \
  tests/config/test_settings.py -q
```

- [ ] **Step 8: Commit only safely isolatable changes**

Do not stage unrelated pre-existing changes in `config.py` or `database.py`.

## Task 5: Expose authenticated decision-workflow APIs

**Files:**

- Modify: `app/routers/decision.py`
- Modify: `tests/test_decision_router.py`

- [ ] **Step 1: Extend router tests first**

Add authenticated-owner assertions for:

```text
GET  /api/decision/research/today
GET  /api/decision/baseline/today
POST /api/decision/proposals
POST /api/decision/proposals/{id}/validate
GET  /api/decision/final/today
POST /api/decision/proposals/{id}/confirm
```

Verify:

- query/body `user_id` cannot override `current_user["id"]`;
- workflow errors preserve `code`, `message`, and `details`;
- not-found is 404, invalid proposal state is 409, persistence outage is 503;
- Pydantic schema failures are 422.

- [ ] **Step 2: Run router tests and confirm RED**

```bash
python -m pytest tests/test_decision_router.py -q
```

- [ ] **Step 3: Implement routes**

Use the existing `ok()` response shape. Catch `DecisionWorkflowError` once in a
small private helper so every route returns:

```json
{
  "detail": {
    "code": "research_packet_stale",
    "message": "...",
    "details": {}
  }
}
```

`POST /proposals` performs initial validation. `POST /validate` accepts a
`refresh_quote` query flag. Confirmation receives `DecisionConfirmationInput`.

- [ ] **Step 4: Run router and service tests**

```bash
python -m pytest \
  tests/test_decision_router.py \
  tests/test_decision_models.py \
  tests/test_decision_research_service.py \
  tests/test_decision_validation_service.py \
  tests/test_decision_proposal_service.py -q
```

- [ ] **Step 5: Commit the router change only if it can be isolated**

## Task 6: Add the agentctl decision loop

**Files:**

- Modify: `cli/agent.py:175-253, 673-780`
- Modify: `tests/test_agent_cli.py`
- Modify: `docs/cli/AGENTCTL.md`
- Modify: `docs/cli/CODEX_DECISION_PROMPT.md`

- [ ] **Step 1: Write failing CLI contract tests**

Assert exact paths, methods, and payloads for:

```bash
agentctl decision research --no-refresh
agentctl decision baseline --no-refresh
agentctl decision propose --payload-json '{...}'
agentctl decision validate --proposal-id cp_1 --refresh-quote
agentctl decision final
agentctl decision confirm --proposal-id cp_1 --validation-id dv_1 --accept --confirm
```

Also assert:

- confirmation without `--confirm` fails before the API call;
- `--accept` and `--reject` are mutually exclusive;
- `decision today --view summary` includes `authority` and
  `is_final_decision`;
- invalid JSON preserves the existing structured CLI error contract.

- [ ] **Step 2: Run tests and confirm RED**

```bash
python -m pytest tests/test_agent_cli.py -q
```

- [ ] **Step 3: Implement commands**

Use `_call_api()` for simple commands. Proposal and confirmation must parse JSON
or build a compact payload, then use the authenticated `AgentApiClient`.

Do not add database access or local JWT signing.

- [ ] **Step 4: Update Codex instructions**

The prompt flow becomes:

1. `auth status`
2. `doctor`
3. `decision research --refresh`
4. optional `decision baseline`
5. evidence drill-down
6. create strict proposal JSON
7. `decision propose`
8. if invalid, revise once from exact hard-failure codes
9. `decision final`

Replace “不得升级 wait/avoid” with:

> 不得违反 hard_constraints；可以覆盖 soft_warnings，但每项必须提供
> evidence_refs、覆盖理由和风险调整。软件基线只用于对照，不是最终权限。

Retain the rule that Codex never runs `decision confirm ... --confirm`.

- [ ] **Step 5: Run CLI tests**

```bash
python -m pytest \
  tests/test_agent_cli.py \
  tests/test_agent_client.py \
  tests/test_auth_cli_session.py -q
```

- [ ] **Step 6: Run a local CLI contract smoke test**

```bash
.venv/bin/agentctl --pretty auth status
.venv/bin/agentctl --pretty doctor
.venv/bin/agentctl --pretty decision research --no-refresh
.venv/bin/agentctl --pretty decision baseline --no-refresh
.venv/bin/agentctl --pretty decision final
```

Expected: authenticated JSON responses; no direct database access.

## Task 7: Add the Vue decision workspace

**Files:**

- Create: `frontend/src/api/decision.ts`
- Create: `frontend/src/views/Decision/index.vue`
- Modify: `frontend/src/router/index.ts`
- Modify: `frontend/src/components/Layout/SidebarMenu.vue`

- [ ] **Step 1: Create typed API contracts**

Define types for:

- `DecisionResearchPacket`
- `DecisionCandidate`
- `SoftwareBaseline`
- `CodexDecisionProposal`
- `DecisionValidation`
- `DecisionConfirmation`
- `DecisionWorkspace`

Expose:

```typescript
getResearch(refresh = true)
getBaseline(refresh = false)
getFinal()
revalidate(proposalId, refreshQuote = true)
confirm(proposalId, payload)
```

- [ ] **Step 2: Build the workspace page**

The page must show:

1. authority mode and whether a final Codex proposal exists;
2. account, market phase, research timestamp, and data-quality state;
3. candidates with software baseline action, hard constraints, soft warnings,
   max allowed quantity, quote, entry, stop, target, and evidence count;
4. Codex selections with primary/secondary role and declared overrides;
5. validation status, recalculated cost/loss, hard failures, and expiration;
6. explicit accept/reject buttons only when the validation is current and valid;
7. permanent research-only disclaimer.

Do not add a free-form LLM chat or API-key input to this page.

- [ ] **Step 3: Add route and menu**

Add authenticated `/decision` with `fluidContent: true` and a “决策工作台” menu
entry near “股票筛选”.

- [ ] **Step 4: Run frontend verification**

```bash
cd frontend
yarn type-check
yarn build
```

Expected: both succeed without generating `package-lock.json`.

## Task 8: Complete rollout mode, integration tests, and Docker smoke

**Files:**

- Modify as required by failures in task-scoped files only.
- Do not edit unrelated dirty files to make broad tests green.

- [ ] **Step 1: Run syntax/import checks**

```bash
python -m compileall \
  app/models/decision.py \
  app/services/decision_research_service.py \
  app/services/decision_validation_service.py \
  app/services/decision_proposal_service.py \
  app/services/decision_confirmation_service.py \
  app/routers/decision.py \
  cli/agent.py
```

- [ ] **Step 2: Run the complete decision slice**

```bash
python -m pytest \
  tests/test_decision_models.py \
  tests/test_decision_research_service.py \
  tests/test_decision_validation_service.py \
  tests/test_decision_proposal_service.py \
  tests/test_daily_decision_service.py \
  tests/test_decision_router.py \
  tests/test_decision_scheduler.py \
  tests/test_agent_cli.py \
  tests/test_agent_client.py \
  tests/test_auth_cli_session.py -q
```

- [ ] **Step 3: Run broader regression tests**

```bash
python -m pytest \
  tests/test_investment_policy.py \
  tests/test_portfolio_diversification_service.py \
  tests/test_decision_tracking_service.py \
  tests/test_decision_review_service.py \
  tests/test_product_optimization_contracts.py -q
```

- [ ] **Step 4: Enable validated mode only for the local installation**

Set the ignored local `.env` value:

```text
DECISION_AUTHORITY_MODE=codex_validated
```

Do not commit `.env`.

- [ ] **Step 5: Rebuild and restart the backend/frontend containers**

```bash
docker-compose build backend frontend
docker-compose up -d --force-recreate backend frontend
docker-compose ps
```

Expected: backend and frontend become healthy.

- [ ] **Step 6: Run authenticated end-to-end smoke**

Use `agentctl`:

1. get a research packet;
2. submit an empty/`wait` proposal and verify it validates;
3. submit a deliberately invalid lot and verify the exact hard failure;
4. read `decision final`;
5. confirm only through explicit user-facing confirmation, not from the Codex
   automation path.

- [ ] **Step 7: Verify the browser**

Open `http://localhost:3000/decision` and verify:

- no backend connection banner;
- research and baseline render;
- Codex and validation state are clearly separated;
- confirmation is disabled for invalid/expired proposals;
- the disclaimer is visible.

- [ ] **Step 8: Final worktree audit**

```bash
git status --short
git diff --check
git diff --name-only
```

Report:

- files changed for this implementation;
- tests and builds run;
- any failures caused by pre-existing dirty changes;
- local-only configuration changes;
- no broker orders were placed.
