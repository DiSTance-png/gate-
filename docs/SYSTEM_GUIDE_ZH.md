# Gate 量化交易系统：原理、架构与运维说明

本文是本项目的中文技术基线，目标是让新的开发者或智能体在没有历史对话的情况下，准确理解系统如何获取数据、做出决策、下单、保护持仓、记录结果和处理故障。

文档基线：Git 提交 `c417da4`。后续若架构、配置或交易安全语义发生变化，应同步更新本文。

## 1. 系统定位

本项目基于 `555cute/r20-quantum-trader` 二次开发。保留和复用的核心是上游的策略提示词、六合约决策契约、持仓/挂单管理契约、因子结构、物理拦截器、管理后台和策略版本机制；重新实现的是交易所边界。当前仓库是一套可独立部署和运行的 Gate 系统。

Gate 执行层直接使用 Gate API v4 Futures 原生语义：

- 合约名采用 `BTC_USDT` 等 Gate contract。
- 下单使用 `/futures/usdt/orders`。
- 止盈止损和计划委托使用 `/futures/usdt/price_orders`。
- 平仓历史使用 `/futures/usdt/position_close`。
- 客户端订单号使用 Gate 的 `text` 字段。
- 合约张数依据 `quanto_multiplier`、`order_size_min` 和 `enable_decimal` 计算。

系统当前是本地常驻应用，Web 绑定 `127.0.0.1:8081`。它不依赖 Gate CLI；所有行情、账户和交易操作都由 Python 客户端签名调用 Gate 官方 Futures REST API。

## 2. 独立运行边界

本仓库拥有完整的独立运行边界：

| 项目 | 本系统使用位置 |
|---|---|
| 项目目录 | `gate量化` |
| Web 端口 | `127.0.0.1:8081` |
| 环境配置 | 项目根目录 `.env` |
| API 凭证 | Gate Testnet/Live 独立键名与加密仓库 |
| 代理 | `GATE_PROXY_URL` 与 `GATE_SSH_TUNNEL_*` |
| 日志 | 项目根目录 `logs/` |
| 数据库与快照 | 项目根目录 `data/`、`runtime/` |
| 主要进程 | `gate_quant.web`、`r20_gateway.worker` |

本机还存在一个 ASCII 路径 `gate-quant`。它是指向 `gate量化` 的 Windows Junction，用于避免某些 Windows 进程检查对中文路径编码不稳定；它不是项目副本。

## 3. 总体架构

```text
浏览器 127.0.0.1:8081
        |
        v
gate_quant.web (FastAPI)
  |-- Vue 监控页 / 管理后台
  |-- Gate 原生查询与受控手工写接口
  |-- SSH 代理隧道生命周期
  `-- Gateway supervisor
          |
          v
    r20_gateway.worker
      |-- 15m  gate_quant.ai_worker
      |-- 10s  gate_quant.execution_reconciler
      |-- 60s  gate_quant.factor_worker
      |-- 10m  gate_quant.news_worker
      |-- 15m  scripts/sync_gate_ledger.py
      |-- 定时  self_improvement_engine.py
      `-- 通知队列投递

Gate API v4
  |-- 公开行情与 K 线
  |-- 合约元数据
  |-- Testnet 或 Live 私有账户
  |-- 普通订单
  `-- 原生 price_orders 保护单
```

这里的 Gateway 是本地任务调度器和通知投递器，不是 Gate 交易所的 API Gateway，也不是只有 AI 决定交易时才启动。它应持续运行，让行情因子、AI、台账和成交保护对账按各自周期执行。

## 4. 主要模块

### 4.1 Gate 交易边界

- `gate_quant/signing.py`：Gate API v4 请求签名。
- `gate_quant/client.py`：行情、合约、余额、持仓、订单、撤单、查单和 `price_orders`。
- `gate_quant/service.py`：风险校验后的下单、确认撤单、减仓与保护覆盖检查。
- `gate_quant/config.py`：Testnet/Live 环境、凭证、代理、限额和 fail-closed 校验。

### 4.2 策略与决策

- `gate_quant/strategy_adapter.py`：将 Gate 数据映射为原 R20 策略输入，并调用原策略拦截器。
- `gate_quant/ai_worker.py`：组织一轮完整巡检、AI 请求、确定性复核和交易执行。
- `gate_quant/factor_worker.py`：维护独立 Gate 因子快照。
- `gate_quant/news_worker.py`：维护新闻数据质量和情绪快照。
- `gate_quant/risk_profiles.py`：观察、保守、标准、积极、激进五档风险预设。

### 4.3 执行可靠性

- `gate_quant/execution_journal.py`：下单前持久化入场意图。
- `gate_quant/execution_reconciler.py`：每 10 秒确认订单、成交、持仓和保护覆盖。
- `gate_quant/exchange_write_lock.py`：跨线程、跨进程串行化 Gate 私有写操作。
- `gate_quant/protection_lifecycle.py`：保护意图、缺失保护恢复和孤立保护单识别。
- `gate_quant/safety.py`：交易所状态对账、日亏损、挂单超时和止损冷却。

### 4.4 Web、调度和记录

- `gate_quant/web.py`：主 Web 服务和 Gate 原生 API。
- `r20_gateway/worker.py`：Gateway 单实例、任务调度和通知投递。
- `r20_gateway/scheduler.py`：各 Worker 周期和互斥关系。
- `scripts/sync_gate_ledger.py`：同步 Gate 原生平仓记录。
- `frontend/src`：Vue 监控页、历史台账、风险档位、Gateway 和运行遥测界面。

## 5. 一轮 AI 决策如何产生

`gate_quant.ai_worker` 默认每 15 分钟运行一次，主要步骤如下：

1. 读取当前 `GateSettings`，生成风险档位快照和策略版本指纹。
2. 从公开行情环境获取六合约 ticker、K 线和因子。
3. 从当前私有环境获取账户、持仓、普通挂单和保护单。
4. 对持仓保护、孤立保护单、过期挂单、日亏损和冷却进行安全巡检。
5. 把 Gate 原生数据适配成原 R20 策略需要的格式。
6. 调用配置的 OpenAI-compatible AI 服务，要求一次返回六合约决策及持仓/挂单管理建议。
7. 将 AI 返回值规范化，置信度强制限制在 `0-100`，杠杆强制替换为系统配置。
8. 再经过确定性策略拦截器检查：数据质量、置信度、ADX、4H 方向、止盈止损几何、R:R 和持仓方向。
9. 从通过的候选中选取最高置信度标的。
10. 再检查报价偏差、冷却、安全门、最小张数、仓位名义价值、单笔保证金和总保证金。
11. 全部通过后才进入下单状态机。

因此主页出现“现价做多”或 AI 原始动作，并不代表订单必然已提交。页面和审计记录同时保留原始动作、最终动作、拦截原因和交易结果，诊断时必须看最终状态。

## 6. 风险档位与杠杆

五档风险档位会同时影响 AI 提示词和确定性拦截器：

| 档位 | 最低置信度 | 最低 ADX | 最低 R:R | 保证金额度比例 | 杠杆上限 | 是否执行 |
|---|---:|---:|---:|---:|---:|---|
| 观察 | 70% | 14 | 2.0 | 0% | 3x | 否 |
| 保守 | 85% | 22 | 2.4 | 35% | 3x | 是 |
| 标准 | 80% | 18 | 2.2 | 65% | 5x | 是 |
| 积极 | 75% | 16 | 2.0 | 85% | 8x | 是 |
| 激进 | 72% | 14 | 2.0 | 100% | 10x | 是 |

激进档位只是扩大候选范围和允许使用完整单笔保证金额度，不会关闭硬风控。任何档位都不能突破绝对 `R:R >= 2.0`、价格几何、环境隔离、资金上限、保护覆盖和下单超时查单规则。

`GATE_LEVERAGE` 是唯一有效杠杆来源：

- 写入风险快照。
- 进入 AI 提示词。
- 用于保证金和张数计算。
- 受风险档位最大杠杆限制。
- 下单前通过 Gate 原生持仓杠杆接口设置。

AI 自己返回的 leverage 不会覆盖系统配置。

## 7. 张数、名义价值和保证金

Gate Futures 的“一张”不一定等于一个币。每张对应的标的数量来自合约元数据 `quanto_multiplier`。

理论张数：

```text
张数 = 保证金 × 杠杆 / (入场价 × quanto_multiplier)
```

系统随后按 `order_size_min`、`order_size_max` 和 `enable_decimal` 向下对齐。对齐后的实际数值重新计算：

```text
名义价值 = abs(张数) × 入场价 × quanto_multiplier
占用保证金 = 名义价值 / 杠杆
```

三项上限含义：

- `GATE_MAX_ORDER_MARGIN_USD`：单次新订单允许占用的最大保证金。
- `GATE_MAX_TOTAL_MARGIN_USD`：账户当前持仓和挂单加上本次订单后的总保证金上限。
- `GATE_MAX_POSITION_NOTIONAL_USD`：组合持仓加上本次订单后的总名义敞口上限。

名义敞口不是“开仓价格”，而是仓位按当前/计划价格折算后的合约价值。

## 8. 入场执行状态机

系统不把“HTTP 请求返回”视作完整下单。下单前先在 `data/gate_execution.db` 记录不可丢失的执行意图：

- 当前环境。
- Gate contract。
- 客户端订单号。
- 计划张数。
- 下单前基准持仓。
- 入场价、止盈价和止损价。
- 策略版本与哈希。

主要活动状态：

| 状态 | 含义 |
|---|---|
| `prepared` | 意图已持久化，提交结果尚未确认 |
| `submitted` | 已收到 Gate 订单信息 |
| `pending_fill` | 订单存在但尚未成交 |
| `position_pending` | 订单显示成交，等待持仓接口最终一致 |
| `partially_filled` | 部分成交且已按成交量保护，继续等待余量 |
| `stop_protected_tp_pending` | 止损已确认，止盈待重试 |
| `protection_pending` | 保护覆盖尚未完整确认 |
| `manual_review` | 状态无法自动确认，需要人工检查 |

主要终态包括 `unfilled`、`abandoned_unconfirmed`、`filled_protected`、`order_rejected`、`stop_failed_flatten_attempted` 和 `flattened_invalid_protection`。

活动状态存在时，新增风险安全门关闭。不能通过删除 SQLite 数据库解除安全门，因为这会丢失“某次请求可能已经到达 Gate”的证据。

## 9. 网络超时为什么不能重下

下单 POST 超时时，本地无法知道以下哪种情况发生了：

1. 请求根本没有到 Gate。
2. Gate 已接受订单，但响应在返回途中丢失。

如果直接重复提交，第二种情况下会形成双倍仓位。因此系统的处理方式是：

1. 保留 `prepared` 意图。
2. 使用同一个 `text` 客户端订单号查询单笔订单。
3. 必要时再查询 open/finished 订单列表。
4. 找到后接管原订单，绝不补发旧 AI 信号。
5. 60 秒仍找不到则标记 `abandoned_unconfirmed`，不自动重下。

保护单 POST 超时也使用确定性的保护客户端订单号查单，避免重复创建。

## 10. 部分成交和保护单

对账器每 10 秒读取：

- 入场订单的 `size`、`left` 和状态。
- 下单前基准持仓。
- 当前真实持仓。
- 当前 Gate `price_orders`。

它使用订单成交量与真实持仓增量交叉判断本次实际成交，然后只补缺口：

1. 先计算现有止损覆盖量。
2. 对缺少的成交量创建 `reduce_only` 止损。
3. 重新读取 Gate 并确认止损真实存在。
4. 再计算止盈覆盖缺口。
5. 创建并确认 `reduce_only` 止盈。
6. 若剩余入场单继续成交，下个周期只补新增差额。

加仓时基准持仓尤为重要。例如原有 10 张、本次成交 4 张，止损创建失败时只能减掉这 4 张，不能关闭原来的 10 张。

## 11. 保护单生命周期

保护意图保存在 `data/protection_intents.json`，并可从 AI 完整审计恢复。它记录环境、合约、方向、原始 TP/SL、入场客户端订单号和策略指纹。

保护覆盖规则：

- 多仓保护单方向必须为卖出，空仓保护单方向必须为买入。
- 止盈和止损都必须覆盖完整当前持仓。
- 只认可明确的 `reduce_only/is_reduce_only` 或 `close/is_close`。
- 普通反向计划单不能计入覆盖。

孤立保护单指：对应合约没有持仓，也没有待成交入场单。系统只会自动撤销客户端订单号前缀能够证明由本系统创建的保护单；人工或来源不明的保护单只报警并关闭新增风险，不擅自撤销。

若持仓缺少保护，只有在历史保护意图能和当前环境、合约、方向、开仓时间可靠关联时才补单。若历史触发价已经被市场穿越，不会把保护单重新挂到错误一侧，而是进入受控平仓处置。无法关联时保持 fail-closed。

## 12. 持仓与挂单何时处理

- 普通入场限价单超过 `GATE_MAX_PENDING_ORDER_AGE_SECONDS` 后进入过期撤单流程。
- AI 可在 `pending_orders_management` 中明确要求撤单，但系统仍校验订单身份和合约。
- 持仓超过 `GATE_MAX_POSITION_AGE_SECONDS` 后会执行最长持仓平仓。
- AI 可输出 `HOLD`、`CLOSE_MARKET` 或 `UPDATE_SL` 管理已有持仓。
- 止损后的冷却按合约隔离；ETH 的止损不会直接阻止 BTC、SOL，除非组合日亏损安全门已经触发。
- 平仓后，系统重新读取持仓和保护单，只清理由本系统创建的孤立保护单。

## 13. 安全门

`safe_for_new_risk=false` 表示暂停新开仓和加仓，不代表系统宕机。风险降低动作仍可继续。

常见关闭原因：

- `protection_gap`：持仓没有完整 TP/SL。
- `orphan_protections`：发现无持仓对应的保护单。
- `stale_entry_orders`：入场挂单超过最大时限。
- `order_age_unknown`：无法确定挂单年龄。
- `execution_reconciliation_pending`：执行状态机仍有活动意图。
- 私有 Gate API、代理或凭证错误。
- 日亏损达到绝对金额或账户比例中更严格的限制。
- 本地台账不可读或关键风险配置缺失。

安全门设计原则是“无法证明安全，就不增加风险”，不能为了让系统更频繁下单而跳过。

## 14. Testnet、Live 和公开行情

`GATE_ENVIRONMENT` 决定所有私有请求和写操作使用哪个账户环境。

`GATE_PUBLIC_MARKET_ENV` 只决定公开行情来源。它不会改变账户、持仓和订单环境。Live 交易被开启时，配置校验强制公开行情也必须为 Live，避免用 Testnet 报价提交 Live 订单。

Testnet 与 Live 使用完全不同的配置键：

```text
GATE_TESTNET_API_KEY
GATE_TESTNET_API_SECRET
GATE_LIVE_API_KEY
GATE_LIVE_API_SECRET
```

面板新保存的凭证进入 Fernet 加密仓库。运行时优先读取加密值，再读取当前环境对应的 `.env` 值。旧 `.env` 凭证不会在未经用户确认时自动删除或迁移。

Web 环境控制采用单环境模式：Testnet 与 Live 自动执行开关不能同时开启。切换环境前，后端使用当前环境凭证只读检查持仓和入场挂单；只要仍有风险或检查结果不明确，就拒绝切换。

开启 Live 需要同时完成：选择 Live、关闭 Testnet 自动执行、填写确认短语 `ENABLE GATE LIVE`、通过候选配置校验，并使用候选 Live Key/Secret 成功读取一次私有账户。任一步失败都不会把 Live 开关写入配置。关闭 Live 只关闭新开仓和加仓，不自动处置实盘持仓。

Testnet 管理页提供独立的“停止 Testnet 自动交易并平仓”动作，确认短语为 `STOP TESTNET AND FLATTEN`。执行顺序固定为：

1. 先持久化关闭 `GATE_TESTNET_EXECUTE_TRADES`。
2. 撤销全部普通入场挂单并逐笔确认。
3. 对每个 Testnet 非零持仓提交带客户端订单号的 Gate 原生市价平仓。
4. 短时重复读取持仓，必须确认全部归零。
5. 只撤销能够证明由本系统创建的保护单。
6. 人工或来源不明保护单保留并在结果中报告。

平仓提交超时仍按客户端订单号查单。任何步骤失败时 Testnet 新增交易保持关闭，不会为了完成界面流程而继续清理保护单或报告成功。

Gate API Key 必须和所选 API 集群、环境与权限匹配。`INVALID_KEY` 不一定表示字符串填错，也可能是 Key 属于不同 Gate 集群。切换 `GATE_TESTNET_BASE_URL` 或 `GATE_LIVE_BASE_URL` 前应先做只读账户检查。

## 15. 代理和 SSH 隧道

可选网络路径：

```text
Gate Python Client
  -> GATE_PROXY_URL (本机 HTTP 代理端口)
  -> SSH 本地端口转发
  -> 用户 VPS 上的 HTTP 代理
  -> Gate API
```

`GATE_REQUIRE_PROXY=true` 时，如果没有 `GATE_PROXY_URL`，配置直接拒绝；SSH 隧道启用但建立失败时，Web 启动失败。这样不会悄悄回退到本地直连。

SSH 隧道由 Web lifespan 管理。已经存在并且端口可用的隧道会复用，不创建重复转发。这里的代理只用于 Gate/AI 配置指定的网络请求。

## 16. 数据、日志和台账

主要运行文件：

| 文件 | 用途 |
|---|---|
| `data/ai_decision_history.json` | 最近 200 轮决策摘要 |
| `data/ai_decision_audit.jsonl` | 完整追加式 AI 审计 |
| `data/gate_execution.db` | 入场执行状态机 |
| `data/protection_intents.json` | TP/SL 恢复意图 |
| `data/trading_ledger.json` | Gate 历史平仓台账 |
| `data/gate_safety_status.json` | 最新安全门快照 |
| `data/gate_trader_heartbeat.json` | AI Worker 心跳 |
| `data/gate_execution_reconciler.json` | 10 秒对账心跳 |
| `data/r20_gateway.db` | 调度和通知队列 |
| `runtime/gate_quant.db` | Web 事件记录 |
| `logs/gate_trader.log` | AI 巡检摘要和异常 |
| `logs/r20_gateway.log` | 调度与通知日志 |
| `logs/gate_backend.log` | Web 生命周期日志 |

手续费、资金费和已结盈亏优先来自 Gate 原生 `/position_close` 与账户账本字段，不应凭本地固定比例伪造。字段不存在时界面应显示不可用，而不是估算成真实值。

所有上述运行数据、数据库、凭证和日志都不进入 Git。

## 17. Web 页面与 API

常用入口：

- 监控页：`http://127.0.0.1:8081/`
- 历史记录：`http://127.0.0.1:8081/history`
- 文档页：`http://127.0.0.1:8081/docs`
- 管理后台：`http://127.0.0.1:8081/admin`

关键接口：

- `GET /api/v1/health`：快速健康、环境、代理、开关和风险上限。
- `GET /api/all`：主页使用的完整规范化聚合数据。
- `GET /api/v1/dashboard`：原始 Gate 私有账户快照，不是主页结构。
- `GET /api/v1/market/tickers`：公开行情。
- `GET /api/v1/market/{symbol}/candles`：指定合约 K 线。
- `GET /api/v1/admin/runtime`：控制面运行遥测，需要管理员会话。
- `GET /api/v1/admin/gate/check`：只读凭证与账户检查。
- `PUT /api/v1/admin/gate/config`：候选配置验证后保存。

订单和保护单写接口需要管理员身份、环境执行开关、风险档位与服务端重新计算的风险数据，不能信任浏览器提交的额度。

## 18. 启动、停止和 Windows PID

启动：

```powershell
cd 'C:\Users\quesi\Documents\ChatGPT\New project\gate量化'
.\start.ps1
```

`start.ps1` 调用 `scripts/ensure_gate_running.ps1`。如果健康接口已经返回正常，它会直接退出，不强制重启。

Windows 下 `.venv\Scripts\pythonw.exe` 可能再启动基础解释器 `C:\Python312\pythonw.exe`，因此 Web 和 Gateway 各显示一对父子 PID。这通常不是重复运行。判断标准：

- 8081 只有一个 LISTEN PID。
- Gateway 的 OS 文件锁只有一个持有者。
- 调度日志没有同一任务同秒重复启动。

不要通过杀死所有 Python 进程处理问题，因为本机其他程序也可能使用 Python。重启前应先解析精确命令行、端口所有者和父子关系。

## 19. 推荐诊断命令

```powershell
# 项目改动
git status --short

# Web 快速健康
Invoke-RestMethod http://127.0.0.1:8081/api/v1/health

# 8081 所有者
Get-NetTCPConnection -State Listen -LocalPort 8081

# 只查看 Gate 相关进程，不执行停止
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'gate_quant\.web:app|r20_gateway\.worker' } |
  Select-Object ProcessId,ParentProcessId,CommandLine

# 调度、AI 和 Web 日志
Get-Content logs/r20_gateway.log -Tail 50
Get-Content logs/gate_trader.log -Tail 50
Get-Content logs/gate_backend.log -Tail 50

# 完整离线验证
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q gate_quant r20_gateway r20_backend scripts
Push-Location frontend
npm run build
Pop-Location
git diff --check
```

不要在诊断输出中打印 `.env`、加密密钥或 Secret。

## 20. 当前验证程度与实盘边界

已经由真实 Gate Testnet 正常链验证：

- 公开行情和 K 线。
- 合约元数据。
- 私有余额、持仓和订单。
- 普通挂单、成交、查单和撤单。
- Gate 原生 TP/SL 保护单。
- 过期挂单撤销。
- 最长持仓自动平仓。
- Gate 原生平仓台账。
- AI 六合约决策链。

已经通过离线故障注入验证：

- 入场请求超时后按客户端订单号恢复。
- 部分成交按增量补保护。
- 进程重启后从 SQLite 恢复。
- 止损失败时撤余量并只减新增仓位。
- 止盈失败时保留已确认止损并重试。
- 非 reduce-only 订单不计入保护。
- Web/自动任务并发写锁。
- 无效配置不落盘。

尚未完成：

- 不影响现有仓位的受控 Testnet 异常写回归。
- Gate Live 小额只读到写入的分阶段回归。
- 长期服务器部署和服务管理脚本。

因此当前系统可以继续在本地 Testnet 稳定验证，但不能把“Testnet 正常链 + 离线异常测试”描述为“Gate Live 已验证”。实盘启用必须是单独、显式且可审计的操作。

## 21. 修改系统时的完成标准

任何涉及交易、策略、配置或运行方式的修改，在交付前至少回答：

1. 改了哪些文件和行为？
2. 是否改变 Testnet/Live 隔离或 Live 开关？
3. 是否改变客户端订单号、超时查单或执行状态机？
4. 是否改变 TP/SL 覆盖、平仓或加仓基准持仓？
5. 是否改变风险档位、杠杆、名义价值或保证金计算？
6. 是否新增 Gate 私有写路径并接入统一写锁？
7. 离线测试、前端构建和重启后健康检查结果是什么？
8. 当前持仓和云端保护单是否保持不变？
9. 是否有未完成的 Testnet 或 Live 风险？
10. README、本文和 `AGENTS.md` 是否需要同步更新？

这套完成标准的目的不是增加流程，而是避免在交易系统里用“页面看起来正常”代替可验证的执行安全。
