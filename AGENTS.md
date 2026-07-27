# AGENTS.md

本文件适用于整个仓库。后续 agent 进入项目后先读这里，再读相关目录下的源码和文档。

## 项目定位

A-TradingAgents 是当前持续开发的多智能体股票分析与 Agent 管理平台，远端仓库为
`zhaokera/A-TradingAgents`。本地目录暂时仍沿用 `TradingAgents-CN`，同时保留并参考
TradingAgents-CN 的既有实现与版权说明；目录名和参考项目名都不代表当前产品名称。
当前仓库不是单一 Python 包，而是三块并行系统：

- `tradingagents/`: 核心多智能体库，基于 LangGraph/LangChain 组织分析师、研究员、交易员和风险讨论流程。
- `app/`: FastAPI 后端，负责认证、任务、报告、配置、数据同步、缓存、SSE/WebSocket、定时任务和数据库访问。
- `frontend/`: Vue 3 + Vite + Element Plus 前端，负责 Web 控制台和分析交互。

根目录 `main.py` 是统一 JSON CLI 的兼容入口，不是后端启动入口。正式自动化入口为
`agentctl`、`tradingagents` 或 `python -m cli.agent`；旧交互界面源码仍可通过
`python -m cli.main` 在完整开发环境中手动运行，但不再安装为产品命令。
统一 CLI 必须通过 `/api/auth/login` 使用账号密码建立会话，默认访问会话至少 7 天；不得
恢复本地读取 JWT 密钥、自行签发令牌或按 `user_id` 绕过认证的实现。

## 版权与边界

- README 声明 `app/` 和 `frontend/` 属于专有部分；不要删除或弱化已有版权/授权说明。
- 不要提交 `.env`、API Key、Token、密码、运行日志、数据库导出、缓存、分析结果或用户数据。
- `.gitignore` 已排除 `data/`、`logs/`、`results/`、`frontend/node_modules/`、`frontend/dist/` 等运行产物。新增产物默认也应保持不入库。
- 当前分支可能有用户未提交改动。修改前看 `git status --short`，只改任务相关文件，不回滚他人改动。

## 架构入口

核心分析链路：

- `tradingagents/graph/trading_graph.py`: `TradingAgentsGraph` 主编排类，创建 LLM、工具、记忆、传播器、反思器和信号处理器。
- `tradingagents/graph/setup.py`: 构建 LangGraph 节点和边，串起 analyst -> bull/bear research -> research manager -> trader -> risk debate -> risk judge。
- `tradingagents/agents/`: 各类 agent 的 prompt/节点工厂。状态定义在 `tradingagents/agents/utils/agent_states.py`。
- `tradingagents/dataflows/`: 行情、基本面、新闻、技术指标、缓存和多数据源降级逻辑。
- `tradingagents/llm_clients/`: provider 规范化和 OpenAI-compatible/Google/Anthropic 客户端封装。

Web 后端链路：

- `app/main.py`: FastAPI 应用、生命周期、路由注册、中间件、调度器启动。
- `app/core/config.py`: Pydantic Settings 与 Mongo/Redis/JWT/数据同步等环境配置。
- `app/core/database.py`: Motor/PyMongo/Redis 连接与索引初始化。
- `app/core/config_bridge.py`: 将 Web/DB 配置桥接到环境变量，供 `tradingagents/` 核心库读取。
- `app/routers/`: API 层。新增业务接口先看同类 router 的响应格式和认证方式。
- `app/services/`: 业务逻辑层。长耗时分析集中在 `simple_analysis_service.py`、队列/进度/报告/同步相关服务中。

前端链路：

- `frontend/src/main.ts`: Vue 应用初始化、Element Plus、Pinia、路由和全局错误处理。
- `frontend/src/router/index.ts`: 页面路由、权限 meta、菜单相关 meta。
- `frontend/src/api/request.ts`: Axios 实例、认证头、统一错误处理、请求 ID、API 兼容守卫。
- `frontend/src/api/`: 按业务拆分的 API 封装。
- `frontend/src/views/`, `frontend/src/components/`, `frontend/src/stores/`: 页面、组件、Pinia 状态。

## 配置和数据源规则

- Python 依赖以 `pyproject.toml` 为准；`requirements.txt` 文件头已说明弃用。
- 前端依赖以 `frontend/yarn.lock` 为准。不要生成或提交 `frontend/package-lock.json`。
- 后端启动会初始化 MongoDB、Redis、配置桥接和 APScheduler。没有 MongoDB/Redis 时，很多端到端路径会失败。
- 配置优先级需要看具体模块：`ConfigProvider` 对系统设置使用 ENV > DB；`config_bridge.py` 对部分数据源密钥明确允许 DB 覆盖 `.env`。改配置逻辑前先读相关函数，不要凭直觉统一改优先级。
- LLM provider 名称必须走 `tradingagents/llm_clients/provider_keys.py` 的规范化逻辑。`dashscope`/`alibaba` 归一到 `qwen`，`zhipu` 归一到 `glm`。
- 数据源优先级优先读数据库配置，失败后回退默认链路。A 股常见链路涉及 MongoDB 缓存、AKShare、Tushare、BaoStock；港股/美股多为按需获取加缓存。
- 单元测试不要直接依赖真实行情、真实 LLM 或真实外部 API；需要时 mock 数据源、Mongo/Redis、LLM 客户端。

## 常用命令

后端/核心库安装：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .
```

启动后端：

```bash
python -m app
```

或：

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

前端开发：

```bash
cd frontend
yarn install --frozen-lockfile
yarn dev --host 0.0.0.0 --port 3000
```

Docker：

```bash
docker-compose up -d
docker-compose logs -f backend
docker-compose down
```

Python 测试：

```bash
python -m pytest
python -m pytest tests/test_holding_analysis.py tests/test_holding_ai_advice.py tests/test_portfolio_target_analysis.py
python -m pytest -m integration
```

语法/导入快速检查：

```bash
python -m compileall app tradingagents tests
python scripts/syntax_checker.py
```

前端验证：

```bash
cd frontend
yarn type-check
yarn build
```

`frontend/package.json` 里的 `yarn lint` 会带 `--fix`，运行前确认可以接受自动格式改动。

## 测试注意事项

- 根目录 `pytest.ini` 默认只收集 `tests/`，默认跳过 `integration` 标记，并排除 `test_server_config`、`test_stock_codes`。
- 优先跑与改动直接相关的测试，再按风险补充 `python -m pytest`。
- 涉及前端类型、路由、API 封装时至少跑 `yarn type-check`；改构建配置或依赖时跑 `yarn build`。
- 涉及 Dockerfile、nginx 或 compose 时，至少做对应服务的 build 或容器启动验证。
- 不要为了让测试通过删除断言、放宽错误处理或跳过真实失败；先定位原因。

## 编码约定

Python：

- 保持 Python 3.10+ 兼容。
- FastAPI handler 使用 async/Motor；只有在同步核心库桥接、线程池或启动配置场景中才使用 PyMongo 同步客户端。
- 新 router 优先复用 `app.core.response.ok` 或同模块既有响应形态，避免同一模块返回结构漂移。
- 需要当前用户的接口使用 `Depends(get_current_user)`，并用 `current_user["id"]` 做数据隔离。
- 长耗时分析不要阻塞请求线程；参考 `simple_analysis_service.py` 的任务记录、后台执行、进度和报告落库链路。
- 日志使用模块 logger。不要新增裸 `print`，除非是在一次性脚本或已有调试脚本中。

前端：

- API 请求走 `frontend/src/api/request.ts` 导出的实例和 `ApiResponse` 约定，不要绕过认证/错误处理。
- 新页面加路由时同步考虑 `meta.title`、`requiresAuth`、菜单显示、布局 `fluidContent` 等已有约定。
- Element Plus、Pinia、Vue Router 是既有基础设施；不要引入新 UI/状态库，除非任务明确要求。
- 修改 API 字段时同步更新 `frontend/src/types/`、相关 `api/` 封装和后端 Pydantic/Mongo 文档字段。

数据/报告：

- Mongo 集合命名和字段存在历史兼容逻辑。改 `analysis_tasks`、`analysis_reports`、`system_configs`、`llm_providers`、`stock_basic_info`、`market_quotes` 等集合时，先查调用点。
- 报告导出依赖 markdown、pypandoc、python-docx、pdfkit/wkhtmltopdf；Dockerfile 已安装 pandoc/wkhtmltopdf 和中文字体。
- 所有投资/持仓输出都必须保持“参考/研究用途，不构成投资建议或交易指令”的语义。

## 工作流约束

- 不要直接在 `main` 上做合并、强推或发布。`docs/development/DEVELOPMENT_WORKFLOW.md` 明确要求功能分支、测试和用户确认。
- 本仓库没有常规 CI 测试 workflow；`.github/workflows/` 主要是 Docker 发布和上游同步检查。本地验证不能省。
- 上游同步是人工选择性吸收，不要直接大范围 merge upstream。先看 `docs/maintenance/upstream-sync.md` 和人工吸收清单。
- 如果文档和代码冲突，以当前代码为准，并在变更说明里指出旧文档可能滞后。

## 快速定位建议

- 分析任务问题：先看 `app/routers/analysis.py`、`app/services/simple_analysis_service.py`、`app/services/memory_state_manager.py`、`app/services/redis_progress_tracker.py`。
- LLM/provider 问题：先看 `app/services/simple_analysis_service.py` 的配置创建、`tradingagents/graph/trading_graph.py`、`tradingagents/llm_clients/`。
- 数据源问题：先看 `tradingagents/dataflows/data_source_manager.py`、`tradingagents/dataflows/interface.py`、`app/services/*sync*`。
- 前端 API/认证问题：先看 `frontend/src/api/request.ts`、`frontend/src/stores/auth.ts`、`app/routers/auth_db.py`。
- 持仓功能：先看 `app/routers/holdings.py`、`app/services/holding_analysis.py`、`app/services/holding_ai_advice.py`、`app/services/portfolio_target_analysis.py`、`frontend/src/views/Holdings/`、`frontend/src/api/holdings.ts`。
