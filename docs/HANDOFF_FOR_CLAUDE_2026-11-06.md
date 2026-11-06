# Gate 量化系统交接记录

本文只整理当前对话上下文中的 Gate 量化交易系统内容，不包含远程 Codex Web 教程项目。面向接手维护的 Claude；先阅读仓库根目录 `AGENTS.md`、`README.md` 和 `docs/SYSTEM_GUIDE_ZH.md`，再按本记录核验当前运行状态。

## 项目与当前代码

- 本地仓库：`C:\Users\quesi\Documents\ChatGPT\New project\gate量化`
- GitHub：`https://github.com/DiSTance-png/gate-.git`，主分支 `main`
- VPS 部署目录：`/opt/gate-quant`
- systemd 服务：`gate-quant-web.service`、`gate-quant-gateway.service`
- 最近确认代码：`e743c53`，反向仓位管理策略感知；此前依次包含 `38cad5f`（反向 TP/SL 几何）、`9f373d3`（宽止损等额缩仓）和 `465fbc3`（受限反向执行模式）。
- 本地仓库最后检查时工作区干净，`main` 与 `origin/main` 同步。

不要在交接文档、提交或终端输出中记录 API Key、Secret、`.env`、加密密钥、运行数据库或日志内容。

## 用户提出的问题与策略决定

用户观察到波段仓位经常先出现未实现盈利，随后行情反弹触发止损；认为常规 2:1 盈亏比使止盈距离过远。之后用户提出把 AI 的回调方向反向执行，并反转 TP/SL 几何。最终确认的反向设置为：

- 仅对 AI 给出的 `retracement` 新仓候选反向；AI 原始方向保留为策略上下文。
- 反向候选按当前可执行买卖价构造立即入场假设；不反向既有仓位，也不把其他入场类型自动反转。
- `GATE_CONTRARIAN_TP_R=1.0`、`GATE_CONTRARIAN_SL_R=2.0`。这表示止盈距离 1R、止损距离 2R，盈利/风险比为 `0.5:1`，不是 2:1。
- 为保持单笔美元风险接近原计划，在 2R 止损下按 `1 / stop_loss_r` 缩放保证金，约为原计划的 50%。
- 确定性风控、风险档位、额度限制、执行台账、统一 Gate 写锁和 Gate 原生 TP/SL 保护仍然适用。反向只改变允许的候选方向和这组 R 参数，不应绕过硬风控。
- 反向模式由 `GATE_CONTRARIAN_MODE` 控制；`off` 默认关闭，`shadow` 仅记录候选，`testnet`/`live` 才进入对应环境的执行链。

## 仓位管理修复

用户担心反向开仓后，AI 只看到实际多空方向，可能沿用原策略方向管理并直接平仓。代码已增加策略感知：

- `gate_quant/ai_worker.py` 从 `gate_execution.db` 最近一次入场快照恢复活动仓位上下文。
- 上下文包含 `strategy_mode`、AI 原始方向、实际执行方向、计划 TP/SL 和反向 R 参数，并传给 AI 仓位管理提示。
- 反向仓位不能仅因实际方向与 4H/1H 趋势相反而被 `CLOSE_MARKET`。
- 反向平仓指令必须显式带 `strategy_mode=contrarian` 和具体失效理由；缺失时执行层安全忽略。
- AI 仍负责持仓管理；这项修复不是取消 AI 管仓或无条件持有到 TP/SL。
- 执行台账/成交快照缺失时，策略归属识别有不确定性；应优先只读核对，不能猜测后平仓。

相关实现主要在 `gate_quant/ai_worker.py`、`gate_quant/execution_journal.py`、`gate_quant/strategy_adapter.py`、`gate_quant/contrarian.py`，测试在 `tests/test_gate_offline.py` 和 `tests/test_contrarian.py`。

## 自进化状态与未闭环问题

用户此前报告自进化流程似乎未正常工作、候选配置无法应用，并希望现有样本能产生更好的策略。不要仅凭候选生成或页面显示宣称自进化已恢复。接手时需核验完整链路：

1. 真实平仓台账是否可读，样本是否正确匹配到下单前的 `entry_snapshot`。
2. 样本是否被标记为 `OBSERVED`、`PARTIAL`、`PRICE_ONLY` 或 `UNAVAILABLE`，过滤原因是否可见。
3. 周期任务是否实际运行，输出状态是 `NO_CHANGE`、`INSUFFICIENT_EVIDENCE`、`WAITING_REVIEW`、`LLM_ERROR`、`PARSE_ERROR`、`PUBLICATION_ERROR` 还是其他状态。
4. 候选是否能被管理员读取、审核和应用；若失败，定位接口响应、版本哈希冲突或配置落盘校验原因。
5. 应用后的策略版本/哈希是否更新，AI 决策是否加载到新版本。

当前系统约束是：自动复盘只生成候选，需管理员审核；持久化策略规则至少需要 20 个完整 `OBSERVED` 样本、至少两个独立周窗口、扣除手续费后仍有支持收益并保留反例。证据不足时只能建议 `COLLECT_EVIDENCE`，不得强行发布长期规则。结构化 `structured_trading_memory.json` 是唯一权威；baseline 不可修改或删除；记忆候选不能直接改资产倍数或其他交易参数。细节见 `AGENTS.md` 与 `docs/SYSTEM_GUIDE_ZH.md` 的自进化部分。

因此，“样本够不够”应以合格证据数和覆盖窗口判断，不能只看平仓总笔数。当前对话上下文没有证明用户报告的自进化应用故障已被端到端复测解决，接手后应将其作为待核验项。

## 最近部署和测试记录

按本对话前序工作记录：

- 变更已推送 GitHub，并同步到 VPS `/opt/gate-quant`。
- `gate-quant-web.service` 与 `gate-quant-gateway.service` 重启后均为 `active`。
- 本地与 VPS 离线测试各 `275 passed`；Python 编译检查、前端 `npm run build`、`git diff --check` 均通过。
- 当时 VPS 配置报告为 `GATE_ENVIRONMENT=live`、`GATE_LIVE_TRADING_ENABLED=true`、`GATE_CONTRARIAN_MODE=live`、TP/SL `1.0/2.0`。
- 最近一次部署后核验快照曾看到 SOL、BTC、XRP 反向空仓，Gate 原生保护覆盖率 100%，安全门允许新风险；该快照是部署后当时的状态，不代表当前仍持有这些仓位或当前安全门状态。
- 部署后紧接着的决策快照仍是重启前的 01:00 记录；当时尚待下一轮 15 分钟 AI 周期确认新决策带有反向仓位上下文。

以上都是历史记录。Claude 接手必须先对本地 Git、GitHub、VPS 服务、健康接口、当前持仓、保护覆盖率、安全门和最新 AI 决策做只读核验，不要依据此处旧仓位快照下单、平仓、撤保护单或调整安全门。

## 接手优先级

1. **只读确认实盘现状**：检查 `git status`/提交、服务状态、`/api/v1/health`、`/api/all`、`safety_status.reconciliation`、当前持仓与每仓 TP/SL 覆盖、挂单、执行台账活动意图、AI 心跳和决策时间。
2. **检查反向仓位管理**：查看最新 `position_management` 是否带 `strategy_mode=contrarian`、原始/实际方向和保护信息；若发生反向平仓，确认是否有明确失效理由，不能把单纯逆趋势当作失败证据。
3. **检查自进化应用故障**：沿“平仓记录 → 入场快照 → 样本分类 → 候选生成 → 人工审核 → 应用 → 新版本加载”逐段找证据，提供明确失败点和可复现依据。
4. **策略效果评价**：分别统计反向入场的已平仓样本、手续费后净盈亏、最大不利/有利波动（若数据可得）、持仓时间和止盈/止损原因。样本有限时明确不确定性，不因短期结果自动扩大仓位或放松安全门。

## 实盘与安全边界

- 用户此前明确确认过本策略按 `TP=1R / SL=2R` 实盘执行，并授权部署该版本；这不等于授权手工下单、平仓、撤销保护单、重置日亏损安全门或更改 Live 凭证。
- 不绕过 `gate_quant/exchange_write_lock.py`、执行台账、对账器、Gate 原生 TP/SL 或 fail-closed 安全门。
- 不因代码更新误平现有仓位；保留并核验 Gate 原生止盈止损。
- 不盲目重复超时订单；先按已有客户端订单号查单。
- 安全门关闭时先查原因，不手工重置。
- 修改策略、风险参数或 Live 配置前，先做本地测试；通过后再按任务授权推送 GitHub 和更新 VPS。策略改动需让用户审核后再实施。
- 不承诺反向策略能盈利。当前部署样本需要继续观察，评价必须基于已闭合、可归因、扣费后的样本。

## 常用验证

按仓库现行规则执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q gate_quant r20_gateway r20_backend scripts
Push-Location frontend
npm run build
Pop-Location
git diff --check
```

任何读日志或诊断输出都要避免暴露 `.env`、密钥、Token、用户 IP 和运行数据库内容。完整系统边界、诊断顺序和 Live 规则以当前 `AGENTS.md` 与 `docs/SYSTEM_GUIDE_ZH.md` 为准。
