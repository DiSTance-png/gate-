# Gate 量化交易系统

本项目基于 [555cute/r20-quantum-trader](https://github.com/555cute/r20-quantum-trader) 二次开发，保留原项目的 MIT License，并将交易执行层改造为 Gate Futures 原生 API。当前仓库是一套可独立部署和运行的 Gate 系统，拥有自己的配置、凭证、代理、日志和数据库。

本项目不是原作者仓库的直接替换版本；策略决策链尽量复用原作，交易所字段、签名、订单、保护单和环境开关均按 Gate 官方接口单独实现。

完整的系统原理、模块关系、执行状态机、安全门、诊断方法和实盘边界见 [中文系统说明](docs/SYSTEM_GUIDE_ZH.md)。在本仓库工作的开发者或 AI 智能体还必须先阅读 [AGENTS.md](AGENTS.md)，避免误改运行数据或绕过 Gate 交易安全约束。

## 启动与配置

```powershell
cd 'C:\Users\quesi\Documents\ChatGPT\New project\gate量化'
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item .env.example .env
pytest -q
./start.ps1
```

启动后访问 `http://127.0.0.1:8081/`，后台入口为 `http://127.0.0.1:8081/admin`。`start.ps1` 固定使用本项目 `.venv`、启动脚本和端口。

默认 `GATE_ENVIRONMENT=testnet`。Testnet 使用 `GATE_TESTNET_API_KEY/SECRET`，Live 使用完全独立的 `GATE_LIVE_API_KEY/SECRET`。`GATE_TESTNET_EXECUTE_TRADES` 只控制模拟盘自动执行，绝不能解锁 Live；Live 必须同时满足 `GATE_ENVIRONMENT=live`、`GATE_LIVE_TRADING_ENABLED=true`、完整 Live 凭证以及 `GATE_PUBLIC_MARKET_ENV=live`。Live 环境允许在交易开关关闭时做只读连接检查。`GATE_LEVERAGE` 是执行层唯一杠杆来源，同时进入 AI 约束、保证金/张数计算，并在下单前通过 Gate 原生持仓杠杆接口设置。未配置任一限额时下单会拒绝（fail-closed）。代理仅读取 `GATE_PROXY_URL`。

显式入场意图默认关闭，必须先完成 Gate Testnet 验证后再启用：

```dotenv
GATE_ENTRY_INTENT_ENABLED=false
GATE_MAX_ENTRY_SLIPPAGE_PCT=0.003
GATE_BREAKOUT_EXPIRATION_SECONDS=840
```

启用后 AI 策略主体不变，只在每个非观望决策中增加 `entry_intent=immediate|retracement|breakout`。滑点和有效期是执行层硬参数，AI 无权覆盖。

`GATE_TESTNET_BASE_URL` 和 `GATE_LIVE_BASE_URL` 可按 Gate 账户集群覆盖，必须是完整的 `/api/v4` 地址。当前默认 Testnet 地址已用本账户验证；Gate 官方 SDK 当前列出的 `fx-api-testnet.gateio.ws` 属于另一集群，切换前必须用同一凭证做只读检测。

## Linux VPS 部署

服务器建议把仓库放在 `/opt/gate-quant`，使用 Python 3.12、Node.js 20 LTS，并分别运行两个 systemd 服务：

- `gate-quant-web.service`：FastAPI Web，仅监听 `127.0.0.1:8081`。
- `gate-quant-gateway.service`：独立运行调度器、AI Worker 和成交保护对账。

模板位于 `deploy/systemd/` 和 `deploy/env.server.example`。首次部署应复制服务器环境模板为 `.env`，设置强 `GATE_ADMIN_PASSWORD`，并保持 `GATE_TESTNET_EXECUTE_TRADES=false`、`GATE_LIVE_TRADING_ENABLED=false`。API Key/Secret 可以在后台“Gate 账户与合约池”中填写，保存后进入服务器本地加密仓库，不需要以明文写入 `.env`。

在配置 HTTPS 或 VPN 前，不要把 8081 直接开放到公网。电脑可通过 SSH 本地转发访问：

```powershell
ssh -N -L 28081:127.0.0.1:8081 your-server-alias
```

然后打开 `http://127.0.0.1:28081/`。手机使用蜂窝网络时无法访问电脑的 `127.0.0.1` 隧道；应先配置 Tailscale/WireGuard，或配置域名、有效 HTTPS 证书和额外访问控制，再从手机填写敏感配置。纯 HTTP 公网页面不得用于提交 API Key/Secret。

## AI 风险档位与杠杆

后台“Gate 账户与合约池”可选择 `GATE_RISK_PROFILE`、修改 `GATE_LEVERAGE` 和单轮最大开仓数量。保守、标准、积极档每轮最多 1 单；激进档可选 1 或 2 单；观察模式为 0。保存后，后端生成带版本和哈希的不可变风险快照；同一份快照同时进入 AI system/user prompt、置信度/ADX/R:R 拦截器、有效保证金额度、下单审计和历史记录。AI 输出的 leverage 不能覆盖系统配置；真正下单前仍调用 Gate 原生持仓杠杆接口。多候选按置信度串行执行，每一笔前重新读取账户和持仓；单个币种等待报价确认或被拒绝不会遮挡后续合格币种，只有订单状态不明确才停止整轮。

| 档位 | 置信度 | 1H ADX | 执行 R:R | 有效保证金额度 | 杠杆上限 |
|---|---:|---:|---:|---:|---:|
| 观察 | 70% | 14 | 2.0 | 0%（禁止下单） | 3x |
| 保守 | 85% | 22 | 2.4 | 35% | 3x |
| 标准 | 80% | 18 | 2.2 | 65% | 5x |
| 积极 | 75% | 16 | 2.0 | 85% | 8x |
| 激进 | 72% | 14 | 2.0 | 100% | 10x |

档位只能在预定义集合中选择，前端不能上传任意阈值。任何档位都不能降低真实价格几何、绝对 `R:R >= 2.0`、4H 方向、单笔/总保证金与仓位上限、双保护覆盖、Live 显式开关和网络超时按客户端订单号查单等 P0 条件。杠杆超过当前档位上限时配置保存会失败，而不是静默截断。

支持使用独立 VPS 作为 Gate API 的网络转发节点：本机通过 SSH 本地端口转发到 VPS 上的 HTTP 代理，再由 Gate 请求经该代理出网。SSH 主机别名、监听端口和远端代理端口都只在本地 `.env` 配置，不写入公开仓库。`GATE_REQUIRE_PROXY=true` 禁止 Gate 请求回退本地直连；隧道创建失败时 Gate 服务启动失败。

`GATE_PUBLIC_MARKET_ENV` 只控制公开行情来源，默认是 `live`（因为 Gate Testnet 公共行情域名可能不可用）；余额、持仓、订单和所有写操作始终使用 `GATE_ENVIRONMENT`，不会因行情来源切换环境。

客户端覆盖行情、合约元数据、账户余额、持仓、挂单、下单、撤单、订单查询，以及 Gate 原生 `/price_orders` 计划委托/止盈止损保护单。下单网络超时会先按 `text` 客户端订单号查单，绝不自动重复提交。

策略层遵循“原 R20 决策链复用、Gate 交易边界适配”原则：`gate_quant/strategy_adapter.py` 调用原 `scripts/ai_brain_trader.py` 的完整系统提示词、六标的决策契约、持仓/挂单管理契约和 `validate_and_filter_decision` 拦截校验。Gate Worker 只负责将 Gate 原生行情/账户字段映射为策略包，并将通过校验的结果映射回 Gate `orders` / `price_orders`；不会复制或另起一套策略规则。

后台调度的 trader、factor 和 news 入口均为 Gate 专用模块（`gate_quant.ai_worker`、`gate_quant.factor_worker`、`gate_quant.news_worker`），交易执行不依赖外部交易 CLI。当前没有配置可验证的 Gate 新闻源时，新闻快照会明确标记 `data_quality=unavailable`，策略不得把缺失新闻误判为中性利好。

后台“Gate 账户与合约池”页面可维护独立环境、两套凭证、VPS 代理和三项风险上限。Gate Key/Secret 由面板保存到项目独立的 Fernet 加密仓库，普通配置、日志和 Git 仓库不会写入新凭证明文；Testnet 与 Live 使用不同键名和加密条目。订单写接口要求管理员会话；服务端自行读取 Gate 账户、持仓、合约乘数与行情计算风险额度，客户端不能自行声明额度绕过限制。

面板保存配置时会先在内存中构造并校验完整候选配置，验证失败不会写入 `.env` 或加密凭证仓库。自动交易、成交对账和 Web 手工下单/撤单/保护单共用独立的跨进程 Gate 写锁；并发请求拿不到锁时会明确返回“未发送到交易所”，不会绕过对账并重复写单。

系统同一时间只允许一个交易环境生效。切换环境前会只读检查当前环境的持仓和入场挂单；仍有风险时拒绝切换。Web 开启 Live 必须输入 `ENABLE GATE LIVE`，并在保存前使用候选 Live 凭证完成私有账户只读检测。关闭 Live 只阻止新增风险，不自动平仓。Testnet 提供独立的“停止自动交易并平仓”按钮，必须输入 `STOP TESTNET AND FLATTEN`：系统先关闭 Testnet 新增交易，再撤入场挂单、逐合约平仓、重复确认持仓归零，最后只清理由本系统创建的保护单；任何步骤无法确认时保持 Testnet 关闭并返回失败。

显式入场意图启用后，`immediate` 使用带滑点上限的 IOC 可成交限价，`retracement` 使用 GTC 回调/反弹限价，`breakout` 使用 Gate 原生 `/price_orders` 上破/下破计划单。意图与买一/卖一关系矛盾时转为观望，不根据价格位置猜测。当前 Gate Testnet 要求原生 `trigger.expiration` 为 86400 秒的整数倍，因此交易所字段固定使用 86400 秒，10 秒对账器按本地 `GATE_BREAKOUT_EXPIRATION_SECONDS=840` 主动撤销并确认，确保计划不跨下一轮 AI。下一轮变为观望或反向信号时同样先确认撤单。

下单张数按 `floor(margin_usdt × GATE_LEVERAGE ÷ (entry_price × quanto_multiplier))` 计算，并按 Gate 合约元数据的 `enable_decimal` 与 `order_size_min` 向下对齐。Gate 官方 `price_orders` 初始张数模型为整数，因此突破计划会再次向下取整，绝不向上放大仓位；取整后不足一张则拒绝。舍入后重新计算实际名义价值和保证金，再接受组合名义价值、总保证金和单笔保证金三重上限校验。

每轮最近 200 条摘要保存在 `data/ai_decision_history.json`，完整追加审计写入 `data/ai_decision_audit.jsonl`。每条记录包含环境、原始/最终动作、拦截原因、交易结果和当轮风险快照，Testnet 与 Live 可明确区分。

后台“异常与未闭环”每 60 秒执行独立只读审计，不再只检查 Trader 返回码。它会继续核对六合约决策完整性、持仓管理结论、Gateway/Trader/对账器心跳、执行台账错误、成交与保护数量、实时 TP/SL 覆盖、过期与孤立订单，以及安全门快照和实时 Gate 状态是否一致。异常按环境保存首次发现、最后出现、累计次数和恢复时间到 `data/gate_anomaly_history.json`；检测器不会自动重跑 AI、补单、撤单或平仓。

入场执行另有 `data/gate_execution.db` 持久化状态机。系统在调用 Gate 下单前先保存客户端订单号、环境、原持仓、计划张数、入场意图、滑点上限和 TP/SL 百分比距离；10 秒对账任务只按已有客户端订单号恢复，绝不根据旧 AI 信号重新提交入场单。突破触发后使用 Gate 返回的 `trade_id` 找到真实订单，以真实平均成交价平移 TP/SL 距离并校验价格几何。保护单按真实成交量和持仓逐步补齐，顺序固定为先止损、后止盈；保护失败只减本次新增仓位，不误伤旧仓。

持仓生命周期巡查发现某一新增仓位切片缺少止损且保存的止损价已经被穿过时，只用 Gate `reduce_only` 市价单减掉该未保护数量，不再整仓平仓；同一轮同时跳过该合约其他保护修复，避免先创建随后变成多余保护单的 TP。该减仓会以“保护缺口减仓”写入中文巡检摘要。

Gate 双向持仓查询会同时返回 `dual_long` 与 `dual_short` 两行；执行对账器必须按原订单方向选择对应行，不能把排在前面的零仓位当成成交缺失。对账恢复始终沿用原客户端订单号，不会因方向匹配失败重发入场单。

Gate 公共行情 GET 遇到超时、连接异常或 HTTP 500/502/503/504 时最多进行两次短延迟重试，减少偶发 Gate 故障导致的整轮决策缺失。私有请求及任何下单、撤单、平仓请求不使用该重试；订单写入超时仍必须先按客户端订单号查单。

AI 的 `UPDATE_SL` 使用 Gate 单止损约束下的受控替换：只接受方向正确且比旧止损更严格的价格，先确认撤销唯一旧止损，再创建并确认新止损；若新建失败则立即恢复旧价格。止盈单不参与替换，恢复不明确时按 fail-closed 记录异常。

只要执行台账存在待确认、部分成交、已触发待对账或人工复核状态，新增风险安全门就保持关闭。唯一例外是已确认仍在 Gate 等待触发的 `awaiting_trigger`：它保留风险预算但只占对应合约，不阻止其他合约使用剩余额度。一旦触发状态不明确，立即恢复组合级 fail-closed。

P0/P1 安全层还会记录 Gate 对账、日亏损熔断、挂单生命周期、分阶段最长持仓、按合约隔离的止损冷却和进程心跳。持仓默认在 16 小时进入 AI 复核，由浮盈、1H/4H 趋势、结构失效和反向信号决定继续持有、移动止损或退出；达到 36 小时绝对上限才由执行层强制平仓。Gate 双向持仓按 `dual_long/dual_short` 使用原生 `size=0, reduce_only=true, close=false, auto_size=close_long/close_short`，单向持仓使用 `size=0, close=true`；平仓超时同样先按客户端订单号查单，不会盲目重复提交。某一合约止损后只暂停该合约，组合达到日亏损上限时才暂停全部合约。运行时文件默认被 `.gitignore` 排除，不应提交到公开仓库。

保护单生命周期使用独立的本地 `data/protection_intents.json` 保存环境、合约、方向、原始止盈/止损价、客户端订单号和策略指纹，并可从完整决策审计迁移已有记录。巡检只会自动撤销由本系统 `t-gate-*` 创建且已确认没有持仓或待成交入场单的孤立保护单；人工保护单只报警、不自动撤销。持仓缺少覆盖时，仅在历史意图与当前持仓环境、合约、方向和开仓时间可关联时按原价补足缺口，并在 Gate 查单确认。若原始触发条件已经成立，则不在错误一侧重新挂单，而是通过带客户端订单号的 Gate 原生平仓请求处置；来源、行情或关联无法确认时继续 fail-closed。所有规则均受 Testnet/Live 各自的显式交易开关约束。

## P2/P3 策略版本与运行监控

每轮 AI 决策、交易结果和历史记录都绑定 `policy_version` 与 `policy_hash`。版本指纹覆盖提示词、自进化心法、物理拦截器、模型委员会、Gate 风险档位/杠杆/资金上限/生命周期参数和标的池；归档与回滚明确排除 API Key、Secret、代理、`GATE_ENVIRONMENT` 和 Live 开关。

自动复盘默认只写入 `data/evolution_candidates/` 候选，不会直接覆盖当前稳定心法或标的倍率。超级管理员可在“自进化配置”审核应用或拒绝候选。收益快照基于真实平仓台账统计净盈亏、手续费、最大回撤和分标的表现；台账没有资金费或滑点字段时显示“不可用”，不会估算或伪造。

策略自动回滚默认关闭，只允许 Testnet：

```dotenv
GATE_AUTO_ROLLBACK_ENABLED=false
GATE_AUTO_ROLLBACK_POLICY_HASH=
GATE_AUTO_ROLLBACK_MIN_TRADES=20
GATE_AUTO_ROLLBACK_MIN_PROFIT_FACTOR=0.8
GATE_AUTO_ROLLBACK_MAX_DRAWDOWN_USD=100
```

必须先在策略版本页归档一个已验证版本，再把它的哈希填入目标字段。即使误将自动回滚开关带入 Live，执行器也会返回 `live_forbidden`，不会自动回滚。管理总览显示新增风险安全门、Gateway 真实 PID/进程锁和 Trader 心跳；主页挂单表显示已挂时长、剩余时间及正常/即将过期/等待撤单状态。

## Gate Testnet 验证

已通过 Gate Testnet 验证：行情、合约元数据、账户余额、持仓、普通挂单、原生保护单、实际成交、过期挂单撤销、旧版最长持仓平仓和 Gate 原生平仓台账均有运行记录；AI worker 已使用原 R20 提示词链生成六合约决策。2026-09-17 又完成突破计划单创建、客户端订单号查询、显式撤销、本地 840 秒到期撤销、`trade_id` 对账、BTC 多空最小张数触发成交和真实均价 TP/SL 重算，以及双向持仓最小 BTC 多仓的原生 `auto_size=close_long` 平仓验证。新的“16 小时复核 + 36 小时绝对上限”已完成离线故障测试，尚未等待真实时长做 Testnet 回归。验证结束后账户持仓、普通挂单、计划单和保护单均确认归零。全新部署仍默认关闭所有自动交易和显式入场意图。

## 当前风险

Testnet 正常链已经覆盖突破计划单，但不等于 Live 已验证。当前账户集群实测要求 `trigger.expiration` 为 86400 秒的整数倍，系统通过本地 840 秒主动撤单满足策略生命周期；Gate API 版本、账户持仓模式或合约规则变化后必须重新执行 Testnet 回归。该功能尚未获准用于 Live。
