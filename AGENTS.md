# Gate 量化项目智能体工作说明

本文件面向在此仓库工作的 AI 智能体和开发者。开始诊断或修改前必须完整阅读本文件，再按需阅读 `docs/SYSTEM_GUIDE_ZH.md`、`README.md` 和相关源码。不要根据上游项目、旧对话或界面截图猜测当前实现。

## 项目标识与边界

- 实际项目根目录：`C:\Users\quesi\Documents\ChatGPT\New project\gate量化`。
- `C:\Users\quesi\Documents\ChatGPT\New project\gate-quant` 是指向上述目录的 Windows Junction，不是第二套代码。
- Git 远端：`https://github.com/DiSTance-png/gate-.git`，主分支 `main`。
- 上游来源：`555cute/r20-quantum-trader`。本仓库复用其策略决策链，但当前项目是独立运行的 Gate 系统，交易执行层是 Gate Futures 原生 API。
- 本仓库的 `r20_backend`、`r20_gateway`、`scripts` 中保留了一些上游命名和兼容模块。清理遗留模块前必须先检查真实 import/call path，不能按文件名猜测是否仍在使用。

## 不可突破的交易约束

- 未经用户在当前对话明确授权，禁止启用 `GATE_LIVE_TRADING_ENABLED`，禁止提交 Gate Live 订单。
- 默认只允许只读检查和离线测试。Testnet 写回归也要先确认不会影响现有持仓和保护单。
- Live 必须同时满足：环境为 `live`、公开行情环境为 `live`、完整独立 Live Key/Secret、显式 Live 开关、风险档位允许执行、三项资金上限有效。
- Web 开启 Live 必须要求确认短语 `ENABLE GATE LIVE`，并在落盘前使用候选 Live 凭证完成私有只读探针。关闭 Live 不得隐式平仓。
- 同一时间只允许一个环境生效。切换环境前必须确认当前环境没有持仓和入场挂单。
- Testnet 一键停止接口的确认短语为 `STOP TESTNET AND FLATTEN`。顺序必须保持：先关闭新增交易、再撤入场挂单、再平仓并确认归零、最后清理系统保护单。人工或来源不明保护单只能保留并报告。
- Testnet 与 Live 凭证必须使用不同键名，不能复制或自动回退到另一环境。
- 网络超时后禁止盲目重复下单。只能先按既有客户端订单号 `text` 查单。
- 所有新增持仓必须有 Gate 原生止损和止盈覆盖。保护顺序固定为先止损、后止盈。
- 止损无法确认时，先撤未成交余量，再用 `reduce_only` 仅减本次新增仓位；不得误平加仓前已有仓位。
- 不得把普通反向计划单当作保护单。覆盖统计只认可 `reduce_only/is_reduce_only` 或 `close/is_close`。
- 存在未完成执行意图、保护缺口、孤立保护单、过期挂单、私有 API 错误或日亏损熔断时，新增风险必须 fail-closed。
- 自动交易、执行对账和 Web 手工写接口共用 `gate_quant/exchange_write_lock.py`。新增任何 Gate 私有写路径时必须接入同一写锁。

## 权威模块

- `gate_quant/client.py`：Gate API v4 签名、请求和 Gate Futures 原生端点。
- `gate_quant/config.py`：环境选择、凭证选择和 fail-closed 配置校验。
- `gate_quant/strategy_adapter.py`：把 Gate 数据适配到上游 R20 策略契约；不要另造平行策略链。
- `gate_quant/ai_worker.py`：15 分钟 AI 决策、持仓/挂单管理和入场入口。
- `gate_quant/execution_journal.py`：下单前 SQLite 执行意图台账。
- `gate_quant/execution_reconciler.py`：10 秒成交、持仓与保护单对账。
- `gate_quant/protection_lifecycle.py`：保护意图恢复、保护缺口和孤立保护单生命周期。
- `gate_quant/safety.py`、`gate_quant/risk.py`、`gate_quant/risk_profiles.py`：硬风控与风险档位。
- `gate_quant/web.py`：8081 Web、Gate 原生 API 路由和控制面挂载。
- `r20_gateway/worker.py`、`r20_gateway/scheduler.py`：任务调度和通知投递。这里的 Gateway 不是交易所 API 网关。
- `scripts/sync_gate_ledger.py`：Gate 原生平仓台账同步。
- `frontend/src`：Vue 监控页和管理后台。

## 进程和端口事实

- Gate Web 监听 `127.0.0.1:8081`。
- Linux VPS 标准目录为 `/opt/gate-quant`；Web 与 Gateway 分别由 `gate-quant-web.service`、`gate-quant-gateway.service` 管理，模板位于 `deploy/systemd/`。
- VPS Web 不直接监听公网地址。配置 VPN/HTTPS 前通过 SSH 本地转发访问，不能通过纯 HTTP 公网页面提交 API 凭证。
- Windows 默认由 Web lifespan 启动独立 SSH 隧道和 Gateway supervisor；Linux systemd 部署设置 `R20_GATEWAY_WORKER_ENABLED=false`，由独立 Gateway 服务持有调度所有权。
- Gateway 运行 `r20_gateway.worker`，负责定时任务与通知，不负责保持交易所长连接，也不是“有信号才启动”。
- Windows 虚拟环境启动器会表现为一对父子 `pythonw.exe` PID。Web 和 Gateway 各出现一对通常是正常现象，不能据此判断重复实例。
- 单实例依据是：8081 只有一个监听者、`.r20_gateway.lock` 只有一个持有者、调度日志只有一套周期。
- 禁止使用 `Stop-Process -Name python*`、`taskkill /IM python.exe` 等宽泛命令。需要重启时只处理命令行明确包含 `gate_quant.web:app` 或已确认属于本目录父子链的 `r20_gateway.worker`。
- `start.ps1` 在健康检查已通过时不会强制重启；它主要用于确保服务存在。

## 配置与敏感数据

- `.env` 是本地运行配置，不得提交或在输出中打印。
- 新保存的 Gate Key/Secret 位于 `data/r20_secrets.enc`，密钥位于 `data/.r20_secret_key`，两者均不得提交或显示内容。
- `gate_quant/config.py` 优先使用加密仓库，再读取当前环境对应的 `.env` 变量。
- 面板配置采用“候选配置先完整校验，再保存”。不能改回先写文件后校验。
- `GATE_PROXY_URL` 和 `GATE_REQUIRE_PROXY` 是 Gate 网络路径的唯一代理配置来源。
- `GATE_SSH_TUNNEL_*` 只描述本机到用户 VPS 代理的隧道。公开文档只能写通用能力，不得提交私人主机名、IP、用户名或端口凭证。
- 所有 `data/` 运行快照、数据库、日志和凭证都应被 `.gitignore` 排除。提交前必须检查 staged 文件。

## 决策与执行顺序

1. Gateway 每 15 分钟启动 `gate_quant.ai_worker`。
2. Worker 获取六合约行情、K 线、因子、账户、持仓、普通挂单和保护单。
3. 先对交易所状态、日亏损和冷却进行安全巡检。
4. Gate 数据通过 `strategy_adapter` 进入上游 R20 提示词和拦截器。
5. AI 给出决策；系统再次执行确定性置信度、ADX、4H 方向、R:R、报价偏差和资金上限校验。
6. 下单前把客户端订单号、基准持仓、计划张数和 TP/SL 写入 `gate_execution.db`。
7. 使用 Gate 原生 GTC 限价单入场。
8. `execution_reconciler` 每 10 秒只按该客户端订单号查单，根据真实成交量和真实持仓补止损、再补止盈。
9. `sync_gate_ledger.py` 每 15 分钟读取 Gate `/position_close`，生成历史平仓台账。

不要把一次 AI 的 `BUY_LONG/SELL_SHORT` 文本等同于已经开仓。最终可能被策略拦截、报价偏差、保证金额度、安全门、最小张数或未成交限价单阻止。

## 正确诊断顺序

遇到“网页空白、没有决策、没有下单、Gateway offline、没有持仓”等问题时按以下顺序检查，不要先改代码：

1. `git status --short`，确认是否存在其他对话留下的未提交修改。
2. `GET http://127.0.0.1:8081/api/v1/health`，确认 Web、环境、代理和开关。
3. 检查 8081 监听 PID及其父子链，确认是否为 Gate Web。
4. `GET /api/all`。这是主页的规范化聚合数据；`/api/v1/dashboard` 只是原始私有账户快照，字段结构不同。
5. 查看 `logs/r20_gateway.log`、`logs/gate_trader.log`、`logs/gate_backend.log`。
6. 查看 `data/gate_execution_reconciler.json`、`data/gate_safety_status.json`、`data/gate_trader_heartbeat.json` 的时间和状态，但不要提交这些文件。
7. 检查 `data/gate_execution.db` 是否有活动状态；存在时安全门关闭属于预期，不要删除数据库来“修复”。
8. 区分公开行情错误、私有 API 错误、代理错误、AI 服务错误和策略拦截，不能把所有 502 都归因于 VPS。
9. 只有证据指向代码缺陷时才修改。

常见误判：

- Gateway offline 不等于 Gate API 不可用；Gateway 是本地调度/通知进程。
- Gate 下单不依赖外部交易 CLI，本系统直接签名调用官方 Futures REST API。
- AI 决策每 15 分钟运行，但成交对账每 10 秒运行；二者不能混为同一遥测。
- K 线来自公开行情环境，账户、持仓和交易永远来自 `GATE_ENVIRONMENT`。
- “没有仓位”可能是限价单未成交、已撤单、触发止损或最长持仓平仓，必须结合订单和 Gate 平仓台账判断。
- 页面显示旧内容时先检查实际 8081 进程和前端构建，不要复制出第二套项目。

## 修改和验证规则

- 修改前先读目标模块和测试，始终保持 Gate 原生字段和语义。
- 手工编辑使用小范围补丁，保留用户或其他对话的未提交修改。
- 正常最低验证：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q gate_quant r20_gateway r20_backend scripts
cd frontend
npm run build
cd ..
git diff --check
```

- 涉及交易执行时必须增加离线故障测试：超时查单、部分成交、进程重启、止损失败、止盈失败、并发写锁、环境隔离。
- 测试不得使用真实 Secret，不得主动发送 Live 请求。
- 重启后至少确认：健康接口正常、环境未改变、Live 关闭、Gateway 新调度存在、私有只读数据可用、原持仓和保护覆盖未变化。
- 提交前检查 staged 清单，确保没有 `.env`、`data/`、`logs/`、`*.db`、`*.enc`、密钥或运行快照。
- 修改架构、运行方式、配置字段或安全语义后，必须同步更新 `docs/SYSTEM_GUIDE_ZH.md`、本文件和 README 中对应段落。

## 当前验证基线

- 基线提交：`17dc9ee`。
- 离线测试：76 项通过。
- 已在 Gate Testnet 正常链验证行情、账户、持仓、挂单、成交、撤单、保护单、过期撤单、最长持仓平仓和原生平仓台账。
- 超时找回、部分成交递增保护、保护失败回滚和重启恢复已做离线故障注入，但尚未主动在 Testnet 制造这些异常。
- Gate Live 从未执行过交易，不能声称已经完成实盘回归。

完整原理、配置表、状态机和运维说明见 `docs/SYSTEM_GUIDE_ZH.md`。
