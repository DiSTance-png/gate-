<script setup lang="ts">
import { useDashboardStore } from '../stores/dashboard'
import { ShieldCheck, TriangleAlert } from 'lucide-vue-next'

const store = useDashboardStore()

const kindLabel: Record<string, string> = {
  take_profit: '止盈保护',
  stop_loss: '止损保护',
  plan: '计划委托',
}

function timeText(value: number) {
  return value > 0 ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '--'
}
</script>

<template>
  <section class="rounded-xl border p-4 sm:p-5" style="background-color: var(--bg-card); border-color: var(--border-subtle);">
    <div class="flex flex-wrap items-center justify-between gap-2 pb-3 mb-3 border-b" style="border-color: var(--border-subtle);">
      <div class="flex items-center gap-2.5">
        <div class="w-6 h-6 rounded-md flex items-center justify-center border" style="background-color: var(--color-up-bg); border-color: var(--color-up-border); color: var(--color-up);">
          <ShieldCheck class="w-3.5 h-3.5" />
        </div>
        <h2 class="text-xs sm:text-sm font-black font-mono">Gate 云端保护单</h2>
        <span class="px-2 py-0.5 rounded text-xs font-mono font-bold border" style="background-color: var(--bg-badge); border-color: var(--border-subtle); color: var(--color-brand);">{{ store.protectionOrders.length }} 单</span>
      </div>
      <span class="text-xs font-mono" style="color: var(--text-faint);">原生 price_orders · 与普通限价挂单分开</span>
    </div>

    <div v-if="!store.protectionOrders.length" class="py-4 text-center rounded-lg border border-dashed text-xs font-mono" style="border-color: var(--border-subtle); color: var(--text-muted);">
      当前没有 Gate 云端止盈、止损或计划委托
    </div>
    <div v-else class="overflow-x-auto">
      <table class="w-full text-left text-xs font-mono whitespace-nowrap">
        <thead><tr style="color: var(--text-muted);"><th class="px-3 py-2">合约</th><th class="px-3 py-2">类型</th><th class="px-3 py-2">触发价</th><th class="px-3 py-2">平仓数量</th><th class="px-3 py-2">关联状态</th><th class="px-3 py-2">创建时间</th><th class="px-3 py-2 text-right">Gate 单号</th></tr></thead>
        <tbody class="divide-y" style="border-color: var(--border-subtle);">
          <tr v-for="order in store.protectionOrders" :key="order.order_id">
            <td class="px-3 py-2.5 font-black" style="color: var(--text-main);">{{ order.contract }}</td>
            <td class="px-3 py-2.5 font-bold" :style="{ color: order.kind === 'stop_loss' ? 'var(--color-down)' : order.kind === 'take_profit' ? 'var(--color-up)' : 'var(--color-brand)' }">{{ kindLabel[order.kind] || '计划委托' }}</td>
            <td class="px-3 py-2.5 font-black num-tabular" style="color: var(--text-main);">${{ order.trigger_price || '--' }}</td>
            <td class="px-3 py-2.5 num-tabular">{{ order.size }} 张</td>
            <td class="px-3 py-2.5">
              <span v-if="order.relation === 'linked'" class="inline-flex items-center gap-1 font-bold" style="color: var(--color-up);"><ShieldCheck class="w-3 h-3" />已关联持仓</span>
              <span v-else class="inline-flex items-center gap-1 font-bold" style="color: var(--color-down);"><TriangleAlert class="w-3 h-3" />孤立保护单</span>
            </td>
            <td class="px-3 py-2.5" style="color: var(--text-muted);">{{ timeText(order.created_at_ms) }}</td>
            <td class="px-3 py-2.5 text-right" style="color: var(--text-faint);">{{ order.order_id }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
