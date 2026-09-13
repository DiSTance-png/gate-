<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useApi } from '../../composables/useApi'
import { Terminal, RefreshCw } from 'lucide-vue-next'

const { api } = useApi()
const loading = ref(true)
const logs = ref<string[]>([])
const decisionCycles = ref<any[]>([])
const activeLogTab = ref<'trader' | 'backend' | 'scheduler'>('trader')
const logContent = ref<string>('')
const logLoading = ref(false)
const historyLoading = ref(false)
const historyQuery = ref('')
const historyPage = ref(1)
const historyPages = ref(1)
const decisionTotal = ref(0)
const tradeHistory = ref<any[]>([])
const tradeTotal = ref(0)

const actionText: Record<string, string> = {
  BUY_LONG: '做多',
  SELL_SHORT: '做空',
  WAIT: '观望',
}

const structureText: Record<string, string> = {
  BULL: '偏多',
  BEAR: '偏空',
  CHOP: '震荡',
}

const macroText: Record<string, string> = {
  BULL: '大周期偏多',
  BEAR: '大周期偏空',
  RANGE: '大周期震荡',
}

function pct(value: unknown, digits = 2) {
  const number = Number(value)
  return Number.isFinite(number) ? `${number.toFixed(digits)}%` : '--'
}

function cycleTime(value: unknown) {
  const timestamp = Number(value)
  return timestamp > 0 ? new Date(timestamp).toLocaleString('zh-CN', { hour12: false }) : '时间未记录'
}

function cycleTradeText(trade: any) {
  const labels: Record<string, string> = {
    not_submitted: '未下单',
    submitted_testnet: '已提交测试网订单',
    blocked_by_strategy_interceptor: '被策略拦截',
    protection_failed_flatten_attempted: '保护单失败，已尝试平仓',
  }
  return labels[trade?.status] || trade?.status || '未下单'
}

function formatTraderLog(raw: string) {
  const lines = raw.split(/\r?\n/)
  const chineseSummary: string[] = []
  for (let index = 0; index < lines.length; index += 1) {
    if (lines[index].includes('Gate Quantum Trader') && (lines[index].includes('巡检完成') || lines[index].includes('巡检异常'))) {
      chineseSummary.push(lines[index])
      if (lines[index + 1]?.startsWith('AI决策')) chineseSummary.push(lines[index + 1])
    }
  }
  if (chineseSummary.length) return chineseSummary.slice(-12).join('\n')

  const headerPattern = /^\[\d{4}-\d{2}-\d{2} .*?\]\s+job=trader\s+return_code=\d+/gm
  const headers = [...raw.matchAll(headerPattern)]
  if (!headers.length) return '原始巡检明细不完整，已隐藏。请等待下一轮巡检。'
  const blocks = headers.map((header, index) => {
    const start = header.index || 0
    const end = headers[index + 1]?.index ?? raw.length
    return raw.slice(start, end)
  })

  return blocks.map((block) => {
    const lines = block.trim().split(/\r?\n/)
    const header = lines.shift() || ''
    const jsonText = lines.join('\n').trim()
    const match = header.match(/^\[(.*?)\].*?return_code=(\d+)/)
    const timestamp = match?.[1] || '--'
    const returnCode = match?.[2] || '--'
    let payload: any
    try {
      payload = JSON.parse(jsonText)
    } catch {
      return [
        `【交易巡检】${timestamp}`,
        `巡检状态：${returnCode === '0' ? '正常完成' : `异常结束（返回码 ${returnCode}）`}`,
        '巡检明细过长或日志片段不完整，原始字符已隐藏。',
      ].join('\n')
    }

    const decisions = Object.entries(payload.decisions || {}) as [string, any][]
    const rows = decisions.map(([symbol, envelope]) => {
      const decision = envelope?.decision || {}
      const indicators = envelope?.indicators || {}
      const probability = indicators.calculus?.probability_theory || {}
      const action = actionText[decision.action] || decision.action || '未知'
      const reason = decision.rejection_reason || decision.summary_reason || '未提供原因'
      return [
        `${symbol.replace('_USDT', '/USDT')}: ${action}，信心度 ${pct(decision.confidence, 0)}`,
        `  结构：${macroText[indicators.macro_4h] || indicators.macro_4h || '--'} / ${structureText[indicators.structure_1h] || indicators.structure_1h || '--'}；RSI(1小时) ${indicators.rsi_1h ?? '--'}；ADX ${indicators.adx_1h ?? '--'}`,
        `  概率：延续 ${pct(probability.continuation_prob_pct)}，击穿 ${pct(probability.breakdown_prob_pct)}；VaR ${pct(probability.var_95_pct)}，CVaR ${pct(probability.cvar_95_pct)}`,
        `  说明：${reason}`,
      ].join('\n')
    })

    const trade = payload.trade || {}
    const tradeStatus: Record<string, string> = {
      not_submitted: '未下单',
      submitted_testnet: '已提交测试网订单',
      blocked_by_strategy_interceptor: '被策略拦截',
      protection_failed_flatten_attempted: '保护单失败，已尝试平仓',
    }
    const tradeLine = `执行结果：${tradeStatus[trade.status] || trade.status || '未说明'}${trade.reason ? `（${trade.reason}）` : ''}`
    return [`【交易巡检】${timestamp}`, `巡检状态：${returnCode === '0' ? '正常完成' : `异常结束（返回码 ${returnCode}）`}`, tradeLine, '', rows.join('\n\n') || '本轮没有可展示的合约决策'].join('\n')
  }).join('\n\n────────────────────────\n\n')
}

async function loadDecisions() {
  loading.value = true
  try {
    const res = await api('/api/v1/admin/runtime')
    logs.value = res.recent_logs || []
    await loadHistory()
    await fetchLogStream('trader')
  } catch (e: any) {
    console.error(e)
  } finally {
    loading.value = false
  }
}

async function loadHistory() {
  historyLoading.value = true
  try {
    const res = await api(`/api/v1/admin/history?page=${historyPage.value}&page_size=10&query=${encodeURIComponent(historyQuery.value)}`)
    decisionCycles.value = res.decision_history || []
    historyPages.value = res.decision_pages || 1
    decisionTotal.value = res.decision_total || 0
    tradeHistory.value = res.trade_history?.items || []
    tradeTotal.value = res.trade_history?.total || 0
  } finally {
    historyLoading.value = false
  }
}

function searchHistory() {
  historyPage.value = 1
  loadHistory()
}

async function fetchLogStream(type: 'trader' | 'backend' | 'scheduler') {
  activeLogTab.value = type
  logLoading.value = true
  try {
    const res = await api(`/api/v1/admin/logs?source=${type}&lines=100`)
    const content = res.content || res.lines?.join('\n') || '无实时日志'
    logContent.value = type === 'trader' ? formatTraderLog(content) : content
  } catch (e: any) {
    logContent.value = `获取日志失败: ${e.message}`
  } finally {
    logLoading.value = false
  }
}

onMounted(() => {
  loadDecisions()
})
</script>

<template>
  <div class="space-y-4 max-w-[2048px] mx-auto">
    <div class="flex items-center justify-between">
      <p class="text-xs font-mono" style="color: var(--text-muted);">核对 AI 宏观基调与逐币动作，并审查交易、后台与任务调度三路实时日志流。</p>
      <span
        class="text-[10px] font-mono px-2 py-1 rounded border font-bold"
        style="background-color: var(--color-brand-bg); color: var(--color-brand); border-color: var(--color-brand-border);"
      >
        日常运行 · 决策与审计
      </span>
    </div>

    <section class="border rounded-lg overflow-hidden" style="background-color: var(--bg-card); border-color: var(--border-subtle);">
      <div class="px-4 py-3 border-b" style="border-color: var(--border-subtle);">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 class="text-xs font-black" style="color: var(--text-main);">完整 AI 决策历史</h2>
            <p class="text-[11px] mt-1" style="color: var(--text-muted);">共 {{ decisionTotal }} 轮，按 15 分钟巡测持续保存。</p>
          </div>
          <div class="flex items-center gap-1.5">
            <input v-model="historyQuery" @keyup.enter="searchHistory" class="input text-[11px] w-36" placeholder="搜索合约/原因" />
            <button @click="searchHistory" class="btn-admin-secondary text-[11px]">查询</button>
          </div>
        </div>
      </div>
      <div v-if="decisionCycles.length" class="divide-y" style="border-color: var(--border-subtle);">
        <div v-for="(cycle, cycleIndex) in decisionCycles" :key="cycle.generated_at_ms || cycleIndex" class="px-4 py-3">
          <div class="flex flex-wrap items-center justify-between gap-2 mb-2">
            <strong class="text-xs" style="color: var(--text-main);">第 {{ cycleIndex + 1 }} 轮 · {{ cycleTime(cycle.generated_at_ms) }}</strong>
            <span class="text-[11px] font-bold" style="color: var(--text-muted);">执行：{{ cycleTradeText(cycle.trade) }}</span>
          </div>
          <div class="overflow-x-auto">
            <table class="w-full text-xs">
              <thead>
                <tr class="text-left" style="color: var(--text-muted);">
                  <th class="py-1.5 pr-3 font-medium">合约</th>
                  <th class="py-1.5 pr-3 font-medium">AI 决策</th>
                  <th class="py-1.5 pr-3 font-medium">信心度</th>
                  <th class="py-1.5 font-medium">决策原因</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(decision, symbol) in cycle.decisions" :key="String(symbol)" class="border-t" style="border-color: var(--border-subtle); color: var(--text-main);">
                  <td class="py-2 pr-3 font-mono whitespace-nowrap">{{ String(symbol).replace('_USDT', '/USDT') }}</td>
                  <td class="py-2 pr-3 font-bold whitespace-nowrap">{{ actionText[decision.action] || decision.action || '未知' }}</td>
                  <td class="py-2 pr-3 whitespace-nowrap">{{ pct(decision.confidence, 0) }}</td>
                  <td class="py-2 min-w-[280px]">{{ decision.rejection_reason || decision.summary_reason || '未提供原因' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
      <div v-else class="px-4 py-6 text-xs text-center" style="color: var(--text-muted);">暂无 AI 决策记录</div>
      <div class="px-4 py-2 border-t flex items-center justify-between text-[11px]" style="border-color: var(--border-subtle); color: var(--text-muted);">
        <span>第 {{ historyPage }} / {{ historyPages }} 页</span>
        <div class="flex gap-1">
          <button @click="historyPage = Math.max(1, historyPage - 1); loadHistory()" :disabled="historyPage <= 1 || historyLoading" class="btn-admin-secondary text-[11px]">上一页</button>
          <button @click="historyPage = Math.min(historyPages, historyPage + 1); loadHistory()" :disabled="historyPage >= historyPages || historyLoading" class="btn-admin-secondary text-[11px]">下一页</button>
        </div>
      </div>
    </section>

    <section class="border rounded-lg overflow-hidden" style="background-color: var(--bg-card); border-color: var(--border-subtle);">
      <div class="px-4 py-3 border-b" style="border-color: var(--border-subtle);">
        <h2 class="text-xs font-black" style="color: var(--text-main);">Gate 交易与账户流水历史</h2>
        <p class="text-[11px] mt-1" style="color: var(--text-muted);">已完成订单、当前订单和 Gate Futures account_book 按原生编号去重保存，共 {{ tradeTotal }} 条。</p>
      </div>
      <div v-if="tradeHistory.length" class="overflow-x-auto">
        <table class="w-full text-xs">
          <thead><tr class="text-left" style="color: var(--text-muted);"><th class="px-4 py-2">时间</th><th class="px-4 py-2">类型</th><th class="px-4 py-2">合约</th><th class="px-4 py-2">状态</th><th class="px-4 py-2">Gate 编号</th></tr></thead>
          <tbody><tr v-for="row in tradeHistory" :key="row.record_type + row.external_id" class="border-t" style="border-color: var(--border-subtle); color: var(--text-main);"><td class="px-4 py-2 whitespace-nowrap">{{ cycleTime(row.occurred_at) }}</td><td class="px-4 py-2">{{ row.record_type === 'account_book' ? '账户流水' : '订单' }}</td><td class="px-4 py-2">{{ row.contract || '--' }}</td><td class="px-4 py-2">{{ row.status || '--' }}</td><td class="px-4 py-2 font-mono">{{ row.external_id }}</td></tr></tbody>
        </table>
      </div>
      <div v-else class="px-4 py-6 text-xs text-center" style="color: var(--text-muted);">暂无 Gate 交易流水；系统会在下一次同步时自动补录。</div>
    </section>

    <!-- 3-Way Log Streams -->
    <div class="rounded-xl border p-4 sm:p-5 shadow-xs transition-colors" style="background-color: var(--bg-card); border-color: var(--border-subtle);">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pb-3 mb-3 border-b" style="border-color: var(--border-subtle);">
        <div class="flex items-center space-x-2">
          <Terminal class="w-4 h-4 text-purple-400" />
          <h2 class="text-xs font-black font-mono uppercase tracking-wide" style="color: var(--text-main);">系统实时日志流</h2>
        </div>
        <!-- Log Selector Tabs -->
        <div class="flex flex-wrap gap-1 p-1 rounded-lg border" style="background-color: var(--bg-card-subtle); border-color: var(--border-subtle);">
          <button
            @click="fetchLogStream('trader')"
            class="px-2.5 py-1 rounded text-xs font-mono font-bold cursor-pointer transition-colors"
            :style="activeLogTab === 'trader' ? { backgroundColor: 'var(--text-main)', color: 'var(--bg-card)' } : { color: 'var(--text-muted)' }"
          >
            交易巡检
          </button>
          <button
            @click="fetchLogStream('backend')"
            class="px-2.5 py-1 rounded text-xs font-mono font-bold cursor-pointer transition-colors"
            :style="activeLogTab === 'backend' ? { backgroundColor: 'var(--text-main)', color: 'var(--bg-card)' } : { color: 'var(--text-muted)' }"
          >
            控制面服务
          </button>
          <button
            @click="fetchLogStream('scheduler')"
            class="px-2.5 py-1 rounded text-xs font-mono font-bold cursor-pointer transition-colors"
            :style="activeLogTab === 'scheduler' ? { backgroundColor: 'var(--text-main)', color: 'var(--bg-card)' } : { color: 'var(--text-muted)' }"
          >
            任务调度器
          </button>
        </div>
      </div>

      <div class="relative">
        <div v-if="logLoading" class="absolute inset-0 bg-black/40 backdrop-blur-xs flex items-center justify-center text-xs font-mono" style="color: var(--color-brand);">
          <RefreshCw class="w-4 h-4 animate-spin mr-1.5" />
          <span>正在拉取最新日志流...</span>
        </div>
        <pre class="border rounded-lg p-3 text-xs font-mono max-h-[520px] overflow-y-auto whitespace-pre-wrap leading-relaxed select-text" style="background-color: var(--bg-card-subtle); border-color: var(--border-subtle); color: var(--text-main);">{{ logContent }}</pre>
      </div>
    </div>
  </div>
</template>
