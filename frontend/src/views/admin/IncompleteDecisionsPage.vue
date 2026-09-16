<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { AlertTriangle, RefreshCw } from 'lucide-vue-next'
import { useApi } from '../../composables/useApi'

const { api } = useApi()
const loading = ref(false)
const error = ref('')
const items = ref<any[]>([])

async function load() {
  loading.value = true
  error.value = ''
  try {
    const result = await api('/api/v1/admin/incomplete-decisions?limit=100')
    items.value = result.items || []
  } catch (cause: any) {
    error.value = cause?.message || '读取未完成决策失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-4 max-w-[1500px] mx-auto">
    <div class="flex items-center justify-between gap-3">
      <div>
        <h1 class="text-lg font-black font-mono">未完成 AI 决策</h1>
        <p class="text-xs mt-1 muted">巡查异常退出、超时或卡住的交易巡检任务。</p>
      </div>
      <button type="button" class="btn-admin-secondary inline-flex items-center gap-2" :disabled="loading" @click="load">
        <RefreshCw class="w-4 h-4" :class="loading ? 'animate-spin' : ''" />刷新巡查
      </button>
    </div>
    <div v-if="error" class="panel p-4 text-sm text-red-300">{{ error }}</div>
    <section class="panel p-4 border-amber-500/50">
      <div class="flex items-start gap-3">
        <AlertTriangle class="w-5 h-5 text-amber-300 shrink-0" />
        <div><h2 class="font-bold text-sm">只读修复台账</h2><p class="text-xs muted mt-1">这里不会自动重跑 AI、补发订单或改变 Live 持仓。处理前必须先核对客户端订单号、执行台账、真实持仓和保护单覆盖。</p></div>
      </div>
    </section>
    <section v-if="items.length" class="space-y-3">
      <article v-for="item in items" :key="item.run_id" class="panel p-4">
        <div class="flex flex-wrap items-center justify-between gap-2"><strong class="text-amber-300">{{ item.status_label || '未完成' }}</strong><span class="text-xs font-mono muted">{{ item.started_at || '时间未记录' }}</span></div>
        <div class="grid sm:grid-cols-3 gap-2 mt-3 text-xs muted"><span>任务：交易巡检</span><span>失败阶段：{{ item.failure_stage || '未记录' }}</span><span>返回码：{{ item.return_code ?? '--' }}</span></div>
        <pre class="mt-3 whitespace-pre-wrap text-xs text-red-300 bg-black/20 rounded p-3 overflow-x-auto">{{ item.error_summary || '未记录错误摘要' }}</pre>
      </article>
    </section>
    <section v-else class="panel p-10 text-center"><p class="text-sm text-emerald-300">暂无未完成 AI 决策</p><p class="text-xs muted mt-2">每轮交易巡检完成后会自动关闭记录；异常任务会出现在这里。</p></section>
  </div>
</template>

<style scoped>
.panel { border: 1px solid var(--border-subtle); border-radius: 8px; background: var(--bg-card); }
.muted { color: var(--text-muted); }
</style>
