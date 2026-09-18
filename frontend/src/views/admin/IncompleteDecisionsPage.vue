<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { AlertTriangle, CheckCircle2, RefreshCw, ShieldAlert } from 'lucide-vue-next'
import { useApi } from '../../composables/useApi'

const { api } = useApi()
const loading = ref(false)
const error = ref('')
const items = ref<any[]>([])
const history = ref<any[]>([])
const events = ref<any[]>([])
const summary = ref<Record<string, number>>({})
const checkedAt = ref(0)
const activeTab = ref<'all' | 'active' | 'resolved'>('all')
const category = ref('all')

const severityLabel: Record<string, string> = { critical: '紧急', error: '错误', warning: '警告', info: '提示' }
const categoryLabel: Record<string, string> = {
  decision: 'AI 决策', execution: '订单执行', protection: '保护单', position: '持仓管理',
  runtime: '运行状态', data_quality: '数据质量', state_consistency: '状态一致性',
}
const severityClass: Record<string, string> = {
  critical: 'text-red-300 border-red-500/50 bg-red-500/10',
  error: 'text-orange-300 border-orange-500/50 bg-orange-500/10',
  warning: 'text-amber-300 border-amber-500/50 bg-amber-500/10',
  info: 'text-sky-300 border-sky-500/50 bg-sky-500/10',
}

const sourceRows = computed(() => {
  if (activeTab.value === 'all') return events.value
  return activeTab.value === 'active' ? items.value : history.value.filter(row => row.status === 'resolved')
})
const filtered = computed(() => category.value === 'all' ? sourceRows.value : sourceRows.value.filter(row => row.category === category.value))
const categories = computed(() => [...new Set(sourceRows.value.map(row => row.category).filter(Boolean))])

function formatTime(value: number) {
  if (!value) return '--'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const result = await api('/api/v1/admin/incomplete-decisions?limit=500')
    items.value = result.items || []
    history.value = result.history || []
    events.value = result.events || []
    summary.value = result.summary || {}
    checkedAt.value = result.checked_at_ms || 0
  } catch (cause: any) {
    error.value = cause?.message || '读取异常审计失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-4 max-w-[1500px] mx-auto">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div><h1 class="text-lg font-black font-mono">异常与未闭环</h1><p class="text-xs mt-1 muted">同时检查 AI 决策、执行台账、实时持仓、保护覆盖、心跳和安全门一致性。</p></div>
      <button type="button" class="btn-admin-secondary inline-flex items-center gap-2" :disabled="loading" @click="load"><RefreshCw class="w-4 h-4" :class="loading ? 'animate-spin' : ''" />重新只读巡查</button>
    </div>
    <div v-if="error" class="panel p-4 text-sm text-red-300">{{ error }}</div>
    <section class="panel p-4 border-amber-500/50">
      <div class="flex items-start gap-3"><ShieldAlert class="w-5 h-5 text-amber-300 shrink-0" /><div><h2 class="font-bold text-sm">只读异常审计</h2><p class="text-xs muted mt-1">此页面只读取和留档，不会重跑 AI、重复下单、撤单或平仓。交易异常必须先按客户端订单号核对 Gate 实时状态。</p><p class="text-[11px] muted mt-1">最近检查：{{ formatTime(checkedAt) }}</p></div></div>
    </section>
    <section class="grid grid-cols-2 sm:grid-cols-4 gap-3">
      <div class="metric"><span>紧急</span><b class="text-red-300">{{ summary.critical || 0 }}</b></div><div class="metric"><span>错误</span><b class="text-orange-300">{{ summary.error || 0 }}</b></div><div class="metric"><span>警告</span><b class="text-amber-300">{{ summary.warning || 0 }}</b></div><div class="metric"><span>提示</span><b class="text-sky-300">{{ summary.info || 0 }}</b></div>
    </section>
    <div class="flex flex-wrap items-center gap-2">
      <button class="filter-btn" :class="activeTab === 'all' ? 'selected' : ''" @click="activeTab = 'all'">最近全部（{{ events.length }}）</button><button class="filter-btn" :class="activeTab === 'active' ? 'selected' : ''" @click="activeTab = 'active'">当前异常（{{ items.length }}）</button><button class="filter-btn" :class="activeTab === 'resolved' ? 'selected' : ''" @click="activeTab = 'resolved'">已恢复历史</button>
      <select v-model="category" class="select ml-auto"><option value="all">全部类别</option><option v-for="value in categories" :key="value" :value="value">{{ categoryLabel[value] || value }}</option></select>
    </div>
    <section v-if="filtered.length" class="space-y-3">
      <article v-for="(item, index) in filtered" :key="`${item.fingerprint}-${item.event || item.status}-${item.observed_at_ms || item.last_seen_ms}-${index}`" class="panel p-4">
        <div class="flex flex-wrap items-center justify-between gap-2"><div class="flex flex-wrap items-center gap-2"><span class="tag" :class="severityClass[item.severity]">{{ severityLabel[item.severity] || item.severity }}</span><strong :class="item.status === 'resolved' ? 'text-emerald-300' : 'text-amber-200'">{{ item.title }}</strong><span class="text-[10px] muted">{{ categoryLabel[item.category] || item.category }} · {{ (item.environment || '').toUpperCase() }}</span></div><span class="text-xs font-mono muted">{{ item.contract || item.client_id || item.code }}</span></div>
        <p class="mt-3 text-xs leading-relaxed">{{ item.detail }}</p>
        <div class="grid sm:grid-cols-3 gap-2 mt-3 text-[11px] muted"><span>首次发现：{{ formatTime(item.first_seen_ms || item.observed_at_ms) }}</span><span>{{ item.event ? '本次记录' : '最后发现' }}：{{ formatTime(item.observed_at_ms || item.last_seen_ms) }}</span><span v-if="item.event === 'resolved' || item.status === 'resolved'" class="text-emerald-300">已恢复</span><span v-else-if="item.event">事件：{{ { detected: '首次发现', observed: '持续出现', occurred: '任务异常' }[item.event] || item.event }}</span><span v-else>累计：{{ item.occurrences || 1 }} 次</span></div>
        <p class="mt-3 rounded p-2 text-[11px] bg-black/20 text-sky-200">建议：{{ item.suggestion }}</p>
        <details v-if="item.evidence && Object.keys(item.evidence).length" class="mt-2"><summary class="text-[11px] muted cursor-pointer">查看检测证据</summary><pre class="mt-2 whitespace-pre-wrap text-[11px] bg-black/20 rounded p-3 overflow-x-auto">{{ JSON.stringify(item.evidence, null, 2) }}</pre></details>
      </article>
    </section>
    <section v-else class="panel p-10 text-center"><CheckCircle2 v-if="activeTab === 'active'" class="w-8 h-8 text-emerald-300 mx-auto mb-3" /><AlertTriangle v-else class="w-8 h-8 text-slate-400 mx-auto mb-3" /><p class="text-sm" :class="activeTab === 'active' ? 'text-emerald-300' : 'muted'">{{ activeTab === 'active' ? '当前没有检测到异常闭环' : activeTab === 'all' ? '暂无异常事件记录' : '没有符合筛选条件的恢复记录' }}</p></section>
  </div>
</template>

<style scoped>
.panel{border:1px solid var(--border-subtle);border-radius:8px;background:var(--bg-card)}.muted{color:var(--text-muted)}.metric{border:1px solid var(--border-subtle);border-radius:8px;background:var(--bg-card);padding:12px;display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--text-muted)}.metric b{font-size:20px}.tag{border-width:1px;border-radius:999px;padding:2px 8px;font-size:10px;font-weight:800}.filter-btn,.select{border:1px solid var(--border-subtle);border-radius:6px;background:var(--bg-input);color:var(--text-muted);padding:7px 10px;font-size:12px}.filter-btn.selected{border-color:#10b981;color:#6ee7b7}
</style>
