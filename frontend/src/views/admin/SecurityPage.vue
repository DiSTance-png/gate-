<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useApi } from '../../composables/useApi'
import { RefreshCw, Save, ShieldCheck, Wallet, OctagonAlert } from 'lucide-vue-next'
const { api } = useApi()
const runtime = ref<any>({ risk: {}, credentials: {} }); const snapshot = ref<any>(null); const loading = ref(false); const message = ref(''); const verifiedEnvironment = ref('')
const universe = ref<any>({ contracts: [], catalog: [], limits: { minimum: 1, maximum: 6 } })
const selectedContracts = ref<string[]>([])
const universeSearch = ref('')
const universePreview = ref<any>(null)
const universeConfirmation = ref('')
const form = reactive({ environment: 'testnet', testnet_api_key: '', testnet_api_secret: '', live_api_key: '', live_api_secret: '', testnet_execute_trades: false, live_trading_enabled: false, live_confirmation: '', proxy_url: '', risk_profile: 'standard', max_entries_per_cycle: 1, leverage: 3, max_position_notional_usd: 1000, max_total_margin_usd: 100, max_order_margin_usd: 25, entry_intent_enabled: false, max_entry_slippage_pct: 0.003, breakout_expiration_seconds: 840 })
const selectedProfile = computed(() => (runtime.value.risk_profiles || []).find((p:any) => p.key === form.risk_profile) || {})
const effectiveMargin = computed(() => Number(form.max_order_margin_usd || 0) * Number(selectedProfile.value.margin_ratio || 0))
function onProfileChange(){ const max=Number(selectedProfile.value.max_leverage || 1); if(form.leverage>max) form.leverage=max; const entries=Number(selectedProfile.value.max_entries_per_cycle ?? 1); form.max_entries_per_cycle=Math.min(Number(form.max_entries_per_cycle||1),entries); if(entries===0){form.testnet_execute_trades=false;form.live_trading_enabled=false} }
function onEnvironmentChange(){ verifiedEnvironment.value=''; universePreview.value=null; universeConfirmation.value=''; if(form.environment==='testnet') form.live_trading_enabled=false; else form.testnet_execute_trades=false }
const filteredUniverseCatalog = computed(() => {
  const query = universeSearch.value.trim().toUpperCase()
  const rows = universe.value.catalog || []
  return rows.filter((item:any) => !query || String(item.contract || '').includes(query)).slice(0, 80)
})
const universeDirty = computed(() => {
  const current = [...(universe.value.contracts || [])].sort().join(',')
  return [...selectedContracts.value].sort().join(',') !== current
})
const universeEnvironmentReady = computed(() => String(universe.value.environment || '') === String(form.environment || ''))
function toggleUniverse(contract: string) {
  universePreview.value = null; universeConfirmation.value = ''
  if (selectedContracts.value.includes(contract)) selectedContracts.value = selectedContracts.value.filter(x => x !== contract)
  else if (selectedContracts.value.length < 6) selectedContracts.value = [...selectedContracts.value, contract]
  else message.value = '自选交易对最多 6 个，请先取消一个'
}
async function loadUniverse(){
  try {
    universe.value = await api('/api/v1/admin/gate/instruments')
    selectedContracts.value = [...(universe.value.contracts || [])]
    universePreview.value = null; universeConfirmation.value = ''
  } catch (e:any) {
    message.value = `自选交易对目录加载失败：${e.message}`
  }
}
async function previewUniverse(){
  if(!universeEnvironmentReady.value){message.value='请先保存并检测当前交易环境，再修改该环境的自选交易对';return}
  if(selectedContracts.value.length<1 || selectedContracts.value.length>6){message.value='请选择 1 至 6 个 Gate USDT 永续合约';return}
  loading.value=true; message.value=`正在读取 Gate ${String(universe.value.environment||'').toUpperCase()} 持仓、挂单、计划单和保护单进行预检…`
  try{
    universePreview.value=await api('/api/v1/admin/gate/instruments/preview',{method:'POST',body:JSON.stringify({contracts:selectedContracts.value})})
    universeConfirmation.value=''
    message.value='✓ 预检通过。请核对新增/取消币种，并输入确认短语后应用。'
  }catch(e:any){universePreview.value=null;message.value=`预检失败：${e.message}`}
  finally{loading.value=false}
}
async function applyUniverse(){
  if(!universePreview.value)return
  if(!universeEnvironmentReady.value){message.value='交易环境已变化，请重新加载并预检自选交易对';return}
  loading.value=true; message.value=`正在二次校验账户状态并原子更新 ${String(universe.value.environment||'').toUpperCase()} 交易对…`
  try{
    const result=await api('/api/v1/admin/gate/instruments/apply',{method:'POST',body:JSON.stringify({token:universePreview.value.token,confirmation:universeConfirmation.value})})
    message.value=`✓ 自选交易对已更新：${(result.contracts||[]).join('、')}。主页立即刷新，下一轮 AI 决策按新配置运行。`
    await loadUniverse(); await readAccount()
  }catch(e:any){message.value=`应用失败，原配置未改变：${e.message}`}
  finally{loading.value=false}
}
const RISK_FIELDS = ['risk_profile', 'max_entries_per_cycle', 'leverage', 'max_position_notional_usd', 'max_total_margin_usd', 'max_order_margin_usd', 'entry_intent_enabled', 'max_entry_slippage_pct', 'breakout_expiration_seconds'] as const
function riskFieldEqual(key: string, left: any, right: any) {
  if (key === 'risk_profile') return String(left || '') === String(right || '')
  if (key === 'entry_intent_enabled') return Boolean(left) === Boolean(right)
  return Number(left) === Number(right)
}
const riskDirty = computed(() => {
  const current = runtime.value.risk || {}
  if (current.risk_profile == null && current.leverage == null) return false
  return RISK_FIELDS.some((key) => !riskFieldEqual(key, (form as any)[key], current[key]))
})
const riskSummary = computed(() => `${selectedProfile.value.label || form.risk_profile} · ${Number(form.leverage || 0)}x`)
function riskSavePayload() {
  return {
    environment: runtime.value.environment || form.environment,
    live_trading_enabled: !!runtime.value.live_trading_enabled,
    testnet_execute_trades: !!runtime.value.testnet_execute_trades,
    proxy_url: runtime.value.proxy_url || '',
    live_confirmation: '',
    risk_profile: form.risk_profile,
    max_entries_per_cycle: Number(form.max_entries_per_cycle),
    leverage: Number(form.leverage),
    max_position_notional_usd: Number(form.max_position_notional_usd),
    max_total_margin_usd: Number(form.max_total_margin_usd),
    max_order_margin_usd: Number(form.max_order_margin_usd),
    entry_intent_enabled: !!form.entry_intent_enabled,
    max_entry_slippage_pct: Number(form.max_entry_slippage_pct),
    breakout_expiration_seconds: Number(form.breakout_expiration_seconds),
  }
}
async function saveRiskSettings() {
  if (!riskDirty.value) {
    message.value = '风险档位与杠杆没有未保存修改'
    return
  }
  loading.value = true
  message.value = '正在保存风险档位与杠杆…'
  try {
    const keepLive = !!runtime.value.live_trading_enabled
    await api('/api/v1/admin/gate/config', { method: 'PUT', body: JSON.stringify(riskSavePayload()) })
    await loadRuntime()
    const label = selectedProfile.value.label || form.risk_profile
    const liveNote = keepLive ? 'Live 保持原状态，未改 API 密钥' : '未开启 Live，也未改 API 密钥'
    message.value = `✓ 杠杆已保存为 ${Number(form.leverage)}x / 档位：${label}。${liveNote}；下一轮 AI 决策起生效。`
  } catch (e: any) {
    message.value = `保存失败：${e.message}`
  } finally {
    loading.value = false
  }
}
async function loadRuntime(){ runtime.value=await api('/api/v1/admin/gate/runtime'); form.environment=runtime.value.environment; form.testnet_execute_trades=!!runtime.value.testnet_execute_trades; form.live_trading_enabled=!!runtime.value.live_trading_enabled; form.proxy_url=runtime.value.proxy_url||''; Object.assign(form,runtime.value.risk) }
async function check(){ loading.value=true; message.value=`正在检测当前 ${form.environment.toUpperCase()} 环境（只读，不会下单）…`; try{ const result=await api('/api/v1/admin/gate/check'); verifiedEnvironment.value=result.ok&&result.environment===form.environment?result.environment:''; message.value=result.ok?`✓ ${result.detail}（${result.environment.toUpperCase()}），已解锁该环境的自动交易选项`:`✕ ${result.detail}` }catch(e:any){verifiedEnvironment.value=''; message.value=`✕ ${e.message}`}finally{loading.value=false} }
async function save(){
  const enablingLive = !!form.live_trading_enabled && !runtime.value.live_trading_enabled
  if(enablingLive && form.live_confirmation.trim() !== 'ENABLE GATE LIVE'){
    message.value='开启 Gate Live 前，请在页面内输入确认短语：ENABLE GATE LIVE'
    return
  }
  loading.value=true; message.value='正在保存并执行只读连接检测…'; try{ await api('/api/v1/admin/gate/config',{method:'PUT',body:JSON.stringify(form)}); form.testnet_api_key=form.testnet_api_secret=form.live_api_key=form.live_api_secret=form.live_confirmation=''; await loadRuntime(); await loadUniverse(); const result=await api('/api/v1/admin/gate/check'); verifiedEnvironment.value=result.ok&&result.environment===form.environment?result.environment:''; message.value=result.ok?`✓ 配置已保存，${result.detail}（${result.environment.toUpperCase()}），已解锁自动交易选项`:`配置已保存，但检测失败：${result.detail}` }catch(e:any){verifiedEnvironment.value=''; message.value=`保存失败：${e.message}`}finally{loading.value=false} }
async function confirmEnableLive(){
  if(verifiedEnvironment.value!=='live'){ message.value='请先通过当前 LIVE 私有 API 只读检测'; return }
  if(form.live_confirmation.trim()!=='ENABLE GATE LIVE'){ message.value='请输入完整确认短语：ENABLE GATE LIVE'; return }
  form.live_trading_enabled=true
  await save()
  if(!runtime.value.live_trading_enabled) form.live_trading_enabled=false
}
async function disableLive(){ form.live_trading_enabled=false; form.live_confirmation=''; await save() }
async function resetDailyLoss(){
  const confirmation = window.prompt('此操作会忽略确认时刻之前的今日已实现亏损，并从零重新累计今日风险；每天每个环境仅允许一次。请输入：RESET GATE DAILY LOSS')
  if (confirmation === null) return
  loading.value = true; message.value = '正在重新核对账户并重置今日亏损基线…'
  try {
    const result = await api('/api/v1/admin/gate/daily-loss/reset', { method: 'POST', body: JSON.stringify({ confirmation }) })
    message.value = `✓ 今日亏损基线已重置；重置前亏损 ${Number(result.marker?.loss_usd_before_reset || 0).toFixed(4)} USDT，后续亏损重新按 ${Number(result.marker?.limit_usd || 0).toFixed(4)} USDT 上限累计。`
    await loadRuntime()
  } catch(e:any) { message.value = `✕ ${e.message}` }
  finally { loading.value = false }
}
async function readAccount(){ loading.value=true; message.value=''; try{snapshot.value=await api('/api/v1/admin/gate/account-snapshot')}catch(e:any){message.value=e.message}finally{loading.value=false} }
async function stopAndFlattenTestnet(){
  const confirmation = window.prompt('此操作将关闭 Testnet 新增交易、撤销入场挂单并平掉全部 Testnet 持仓。请输入确认短语：STOP TESTNET AND FLATTEN')
  if (confirmation === null) return
  loading.value = true; message.value = '正在关闭 Testnet 新增交易并执行平仓，请勿重复点击…'
  try {
    const result = await api('/api/v1/admin/gate/testnet/stop-and-flatten', { method: 'POST', body: JSON.stringify({ confirmation }) })
    form.testnet_execute_trades = false
    message.value = `✓ Testnet 已停止并确认平仓：撤销限价单 ${result.cancelled_orders?.length || 0}，撤销突破计划 ${result.cancelled_trigger_entries?.length || 0}，平仓 ${result.closed_positions?.length || 0}，清理系统保护单 ${result.cancelled_protections?.length || 0}，保留来源不明/人工保护单 ${result.retained_protections?.length || 0}`
    await readAccount()
  } catch(e:any) { message.value = `✕ ${e.message}`; await loadRuntime() }
  finally { loading.value = false }
}
onMounted(async()=>{await Promise.all([loadRuntime(),loadUniverse()])})
</script>
<template><div class="p-4 sm:p-6 space-y-4 max-w-[1500px] mx-auto">
  <div class="flex items-center justify-between gap-3"><div><h1 class="text-lg font-black font-mono">Gate 账户与合约池</h1><p class="text-xs mt-1 muted">Gate Futures 原生 API · Testnet / Live 凭证完全隔离</p></div><span class="px-3 py-1.5 rounded border text-xs font-bold font-mono" :class="runtime.ready?'text-emerald-400':'text-amber-400'">{{runtime.ready?'READY':'NOT CONFIGURED'}}</span></div>
  <div v-if="message" class="panel p-3 text-xs font-mono">{{message}}</div>
  <section class="panel p-4"><div class="title"><Wallet class="w-4 h-4 text-emerald-400"/><h2>环境与独立凭证</h2></div><div class="grid md:grid-cols-2 gap-4 text-xs font-mono">
    <label>交易环境<select v-model="form.environment" @change="onEnvironmentChange" class="input"><option value="testnet">Gate Testnet</option><option value="live">Gate Live</option></select><span>系统同一时间只运行一个账户环境；切换 Live 前 Testnet 必须无持仓和入场挂单。</span></label><label>独立代理<input v-model="form.proxy_url" class="input" :disabled="runtime.tunnel?.enabled" placeholder="http://127.0.0.1:18081"/><span v-if="runtime.tunnel?.enabled" :class="runtime.tunnel.running?'text-emerald-400':'text-red-400'">SSH 隧道 · {{runtime.tunnel.running?'CONNECTED':'DISCONNECTED'}}</span></label>
    <label>Testnet API Key（{{runtime.credentials.testnet_configured?'已配置':'未配置'}}）<input v-model="form.testnet_api_key" @input="verifiedEnvironment=''" class="input" autocomplete="off"/></label><label>Testnet API Secret<input v-model="form.testnet_api_secret" @input="verifiedEnvironment=''" class="input" type="password" autocomplete="new-password"/><span class="text-amber-300">Gate Secret 只在创建/重置时显示一次；更换 Key 时必须同时填写对应 Secret，不能混用旧 Secret。</span></label>
    <label>Live API Key（{{runtime.credentials.live_configured?'已配置':'未配置'}}）<input v-model="form.live_api_key" @input="verifiedEnvironment=''" class="input" autocomplete="off"/></label><label>Live API Secret<input v-model="form.live_api_secret" @input="verifiedEnvironment=''" class="input" type="password" autocomplete="new-password"/></label>
  </div><template v-if="form.environment==='testnet'"><label class="testnet"><input v-model="form.testnet_execute_trades" type="checkbox" :disabled="loading || (!form.testnet_execute_trades && verifiedEnvironment!=='testnet')"/> 启用 Gate Testnet 自动交易（仅模拟盘）</label><span v-if="verifiedEnvironment!=='testnet'" class="block mt-2 text-xs font-mono text-amber-300">请先保存配置并通过当前 TESTNET 私有 API 只读检测，自动交易选项才会解锁。</span></template><div v-else class="live-control mt-4"><div class="flex flex-wrap items-center justify-between gap-2"><b class="text-xs font-mono text-red-300">Gate Live 自动交易</b><span class="px-2.5 py-1 rounded border text-xs font-bold font-mono" :class="runtime.live_trading_enabled?'text-red-300 border-red-500':'text-emerald-300 border-emerald-500'">{{runtime.live_trading_enabled?'LIVE 自动交易已确认开启':'LIVE 自动交易已关闭'}}</span></div><template v-if="!runtime.live_trading_enabled"><span v-if="verifiedEnvironment!=='live'" class="block mt-3 text-xs font-mono text-amber-300">请先保存配置并通过当前 LIVE 私有 API 只读检测，确认按钮才会解锁。</span><template v-else><label class="mt-3 text-xs font-mono text-red-300">Live 实盘确认短语<input v-model="form.live_confirmation" class="input" autocomplete="off" placeholder="ENABLE GATE LIVE"/><span>开启时服务端会再次执行 Live 账户只读检测，失败则保持关闭。</span></label><button type="button" @click="confirmEnableLive" :disabled="loading || form.live_confirmation.trim()!=='ENABLE GATE LIVE'" class="btn danger mt-3"><ShieldCheck class="w-4 h-4"/>确认并开启 Live 自动交易</button></template></template><template v-else><p class="mt-3 text-xs font-mono text-red-300">系统允许提交 Gate Live 实盘订单。关闭 Live 不会自动平仓。修改风险档位和杠杆请用下方「保存风险档位与杠杆」，不必重新输入确认短语。</p><button type="button" @click="disableLive" :disabled="loading" class="btn secondary mt-3"><OctagonAlert class="w-4 h-4"/>关闭 Live 自动交易</button></template></div></section>
  <section class="panel p-4"><div class="title"><Wallet class="w-4 h-4 text-cyan-400"/><h2>AI 自选交易对（1–6 个）</h2></div>
    <p class="text-xs muted mb-3">当前生效环境：{{String(universe.environment||runtime.environment||'').toUpperCase()}}。修改后会同步 AI 决策合约、提示词标的、因子任务、行情矩阵、K 线选择和主页显示。取消币种前必须确认该币种无持仓、普通挂单、突破计划、保护单及未完成执行意图。</p>
    <div class="flex flex-wrap gap-2 mb-3"><button v-for="contract in selectedContracts" :key="contract" type="button" @click="toggleUniverse(contract)" class="px-2.5 py-1 rounded border text-xs font-mono text-cyan-300" style="border-color:var(--border-subtle)">{{contract.replace('_USDT','/USDT')}} ×</button><span v-if="!selectedContracts.length" class="text-xs text-red-300">至少保留 1 个交易对</span><span class="text-xs muted ml-auto">{{selectedContracts.length}} / 6</span></div>
    <input v-model="universeSearch" class="input mb-3" placeholder="搜索 Gate USDT 永续合约，例如 XRP、LINK、BNB"/>
    <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2 max-h-52 overflow-y-auto pr-1">
      <button v-for="item in filteredUniverseCatalog" :key="item.contract" type="button" @click="toggleUniverse(item.contract)" class="p-2 rounded border text-left text-xs font-mono" :class="selectedContracts.includes(item.contract)?'text-emerald-300 border-emerald-500':'muted'" style="background:var(--bg-card-subtle)"><b>{{item.symbol}}/USDT</b><span class="block text-[10px] mt-1">最小 {{item.order_size_min}} 张 · {{item.enable_decimal?'支持小数张':'整数张'}}</span></button>
    </div>
    <div class="flex flex-wrap gap-2 mt-4"><button type="button" @click="previewUniverse" :disabled="loading || !universeEnvironmentReady || !universeDirty || selectedContracts.length<1 || selectedContracts.length>6" class="btn"><ShieldCheck class="w-4 h-4"/>预检并生成确认步骤</button><span v-if="!universeEnvironmentReady" class="text-xs text-amber-300 self-center">页面选择的环境尚未保存，请先保存并检测环境。</span><span v-else-if="!universeDirty" class="text-xs muted self-center">当前选择与运行配置一致</span></div>
    <div v-if="universePreview" class="mt-4 p-3 rounded border space-y-3 text-xs font-mono" style="border-color:var(--border-subtle);background:var(--bg-card-subtle)">
      <div class="grid sm:grid-cols-2 gap-3"><div><b class="text-emerald-300">新增</b><p>{{universePreview.added?.length?universePreview.added.join('、'):'无'}}</p></div><div><b class="text-amber-300">取消</b><p>{{universePreview.removed?.length?universePreview.removed.join('、'):'无'}}</p></div></div>
      <p class="text-emerald-300">✓ 已确认取消币种当前无持仓、挂单、计划单、保护单和未完成执行意图；应用时服务端还会二次校验。</p>
      <label>确认短语<input v-model="universeConfirmation" class="input mt-1" autocomplete="off" :placeholder="universePreview.confirmation_phrase"/><span>请输入：{{universePreview.confirmation_phrase}}</span></label>
      <button type="button" @click="applyUniverse" :disabled="loading || universeConfirmation.trim().toUpperCase()!==String(universePreview.confirmation_phrase||'').toUpperCase()" class="btn danger"><Save class="w-4 h-4"/>确认修改当前环境交易对</button>
    </div>
  </section>
  <section class="panel p-4 danger-panel"><div class="title"><OctagonAlert class="w-4 h-4 text-red-400"/><h2>Testnet 紧急停止</h2></div><p class="text-xs muted mb-3">关闭 Testnet 新增交易，撤销入场挂单，平掉当前 Testnet 持仓，并在 Gate 确认持仓归零后清理本系统保护单。不会切换到 Live。</p><button @click="stopAndFlattenTestnet" :disabled="loading || form.environment !== 'testnet'" class="btn danger"><OctagonAlert class="w-4 h-4"/>停止 Testnet 自动交易并平仓</button><span v-if="form.environment !== 'testnet'" class="text-xs text-amber-300 ml-3">请先切换到 Gate Testnet</span></section>
  <section v-if="runtime.safety_status?.daily_loss?.tripped" class="panel p-4 danger-panel"><div class="title"><OctagonAlert class="w-4 h-4 text-red-400"/><h2>日亏损熔断</h2></div><p class="text-xs muted mb-3">已实现亏损 {{Number(runtime.safety_status.daily_loss.loss_usd || 0).toFixed(4)}} USDT，超过当前上限 {{Number(runtime.safety_status.daily_loss.limit_usd || 0).toFixed(4)}} USDT。人工重置不会删除交易记录；仅忽略确认时刻之前的今日盈亏，之后从零重新累计，且今日不能再次重置。</p><button @click="resetDailyLoss" :disabled="loading" class="btn danger"><ShieldCheck class="w-4 h-4"/>人工确认并重置今日亏损基线</button></section>
  <section class="panel p-4"><div class="title"><ShieldCheck class="w-4 h-4 text-blue-400"/><h2>AI 风险档位与 Gate 执行参数</h2></div>
    <div class="grid lg:grid-cols-2 gap-4 mb-4 text-xs font-mono">
      <label>AI 策略风险程度<select v-model="form.risk_profile" @change="onProfileChange" class="input"><option v-for="p in runtime.risk_profiles||[]" :key="p.key" :value="p.key">{{p.label}}</option></select><span>{{selectedProfile.description}}</span></label>
      <div class="profile"><b>{{selectedProfile.label || '--'}} · 有效规则</b><span>置信度 ≥ {{selectedProfile.min_confidence ?? '--'}}%（DOGE {{selectedProfile.doge_min_confidence ?? '--'}}%）</span><span>1H ADX ≥ {{selectedProfile.min_adx ?? '--'}} · R:R ≥ {{selectedProfile.min_rr ?? '--'}}（目标 {{selectedProfile.target_rr ?? '--'}}）</span><span>止损参考 {{selectedProfile.stop_atr_min ?? '--'}}~{{selectedProfile.stop_atr_max ?? '--'}}x 1H ATR · 保证金额度 {{Math.round((selectedProfile.margin_ratio||0)*100)}}%</span><span>单轮最多 {{selectedProfile.max_entries_per_cycle ?? 1}} 单</span><span v-if="!selectedProfile.execution_allowed" class="text-amber-300">观察模式只记录 AI 决策，任何环境都不会下单。</span></div>
    </div>
    <div class="grid md:grid-cols-5 gap-4 text-xs font-mono"><label>单轮最大开仓数量<select v-model.number="form.max_entries_per_cycle" class="input"><option :value="selectedProfile.max_entries_per_cycle===0?0:1">{{selectedProfile.max_entries_per_cycle===0?'0（观察模式）':'1 单'}}</option><option v-if="selectedProfile.max_entries_per_cycle>=2" :value="2">2 单</option></select><span>逐笔串行执行；待确认或被拒绝的币种不会遮挡后续合格信号。</span></label><label>执行杠杆倍数<input v-model.number="form.leverage" type="number" min="1" :max="selectedProfile.max_leverage||1" step="1" class="input"/><span>当前档位最多 {{selectedProfile.max_leverage||'--'}}x；保存后写入提示词、仓位计算和 Gate 原生杠杆接口。</span></label><label>总持仓名义敞口上限<input v-model.number="form.max_position_notional_usd" type="number" min="1" class="input"/><span>不同合约可同时持仓，累计敞口不得超过此值。</span></label><label>总保证金上限<input v-model.number="form.max_total_margin_usd" type="number" min="1" class="input"/><span>决定所有持仓与挂单合计最多占用多少保证金。</span></label><label>单笔保证金绝对上限<input v-model.number="form.max_order_margin_usd" type="number" min="1" class="input"/><span>当前档位有效上限：{{effectiveMargin.toFixed(2)}} USDT</span></label></div>
    <div class="risk-save"><div class="hint"><b>保存风险档位与杠杆</b><span :class="riskDirty?'pending':''">{{riskDirty?('有未保存修改：'+riskSummary):('当前运行中：'+riskSummary)}}</span><span>只写入档位、杠杆、单轮开仓数、保证金上限和入场意图参数，不会开启 Live，也不会改 API 密钥。</span></div><button type="button" @click="saveRiskSettings" :disabled="loading || !riskDirty" class="btn" :class="{dirty:riskDirty}"><Save class="w-4 h-4"/>{{loading?'处理中…':'保存风险档位与杠杆'}}</button></div>
    <div class="grid md:grid-cols-3 gap-4 mt-4 text-xs font-mono"><label class="profile"><span class="flex items-center gap-2"><input v-model="form.entry_intent_enabled" type="checkbox"/>启用显式入场意图</span><span>AI 仅增加 immediate / retracement / breakout 字段；首次启用前必须完成 Testnet 验证。</span></label><label>立即/突破最大滑点<input v-model.number="form.max_entry_slippage_pct" type="number" min="0" max="0.02" step="0.0005" class="input"/><span>小数比例：0.003 = 0.3%；由执行器控制，AI 无权修改。</span></label><label>突破计划有效期（秒）<input v-model.number="form.breakout_expiration_seconds" type="number" min="60" max="900" step="30" class="input"/><span>建议 840 秒，确保旧突破信号在下一轮决策前失效。</span></label></div>
    <p class="text-xs muted mt-4">凭证、环境切换和 Live 开关请用下面这个按钮，它会额外做一次只读连接检测。激进程度和杠杆请用上方「保存风险档位与杠杆」。</p><div class="flex gap-2 mt-2"><button type="button" @click="save" :disabled="loading" class="btn secondary"><Save class="w-4 h-4"/>{{loading?'处理中…':'保存并检测 Gate 配置'}}</button><button type="button" @click="check" :disabled="loading" class="btn secondary"><RefreshCw class="w-4 h-4"/>检测当前 {{form.environment.toUpperCase()}} 连接</button></div></section>
  <section class="panel"><div class="p-4 flex items-center justify-between border-b border-[var(--border-subtle)]"><div><h2 class="font-bold text-sm">账户、持仓、挂单与保护单</h2><p class="text-[11px] mt-1 muted">只读刷新，不会执行下单</p></div><button @click="readAccount" :disabled="loading" class="btn"><RefreshCw class="w-4 h-4"/>读取 Gate</button></div><div v-if="snapshot" class="p-4 space-y-4"><div class="grid sm:grid-cols-5 gap-3 text-xs font-mono"><div class="stat">权益<b>{{snapshot.account?.total||'--'}}</b></div><div class="stat">可用<b>{{snapshot.account?.available||'--'}}</b></div><div class="stat">持仓<b>{{snapshot.positions?.length||0}}</b></div><div class="stat">突破计划<b>{{snapshot.trigger_entries?.length||0}}</b></div><div class="stat">保护单<b>{{snapshot.protections?.length||0}}</b></div></div><div class="overflow-x-auto"><table class="w-full text-xs font-mono"><thead><tr><th>合约</th><th>最小张数</th><th>小数张</th><th>当前杠杆最低保证金</th><th>当前档位可执行</th></tr></thead><tbody><tr v-for="m in snapshot.contract_minimums||[]" :key="m.contract"><td>{{m.contract||m.error}}</td><td>{{m.order_size_min??'--'}}</td><td>{{m.enable_decimal?'是':'否'}}</td><td>{{m.minimum_margin_usdt??'--'}} USDT</td><td :class="m.executable_under_current_cap?'text-emerald-400':'text-amber-300'">{{m.executable_under_current_cap?'可以':'额度不足'}}</td></tr></tbody></table></div></div><div v-else class="p-8 text-center text-xs muted">配置 Gate Testnet 凭证后可读取账户快照</div></section>
</div></template>
<style scoped>.panel{border:1px solid var(--border-subtle);border-radius:8px;background:var(--bg-card)}.danger-panel{border-color:rgba(248,113,113,.45)}.live-control{padding:12px;border:1px solid rgba(248,113,113,.45);border-radius:6px;background:var(--bg-input)}.muted,label{color:var(--text-muted)}.title{display:flex;align-items:center;gap:8px;margin-bottom:16px}.title h2{font-size:14px;font-weight:700}label{display:flex;flex-direction:column;gap:7px}.input{width:100%;padding:9px 10px;border:1px solid var(--border-subtle);border-radius:6px;background:var(--bg-input);color:var(--text-main);outline:none}.input:focus{border-color:#10b981}.profile{display:flex;flex-direction:column;gap:6px;padding:12px;border:1px solid var(--border-subtle);border-radius:6px;background:var(--bg-input);color:var(--text-muted)}.profile b{color:var(--text-main)}.testnet,.live{margin-top:16px;flex-direction:row;font-weight:700}.testnet{color:#34d399}.live{color:#fb7185}.btn{display:inline-flex;align-items:center;gap:7px;padding:8px 12px;border:1px solid var(--border-subtle);border-radius:6px;background:var(--text-main);color:var(--bg-app);font-size:12px;font-weight:700;cursor:pointer}.btn:disabled{opacity:.5}.btn.secondary{background:transparent;color:var(--text-main)}.btn.dirty{border-color:#fbbf24;box-shadow:0 0 0 1px rgba(251,191,36,.35)}.btn.danger{background:#7f1d1d;color:#fecaca;border-color:#ef4444}.risk-save{margin-top:16px;padding:12px;border:1px solid var(--border-subtle);border-radius:6px;background:var(--bg-input);display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.risk-save .hint{display:flex;flex-direction:column;gap:4px;font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.risk-save .hint b{color:var(--text-main)}.risk-save .pending{color:#fbbf24}.stat{padding:12px;border:1px solid var(--border-subtle);border-radius:6px;color:var(--text-muted)}.stat b{display:block;color:var(--text-main);font-size:18px;margin-top:6px}th,td{text-align:left;padding:8px;border-bottom:1px solid var(--border-subtle);color:var(--text-muted)}th{color:var(--text-main)}</style>
