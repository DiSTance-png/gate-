const $=id=>document.getElementById(id); const num=(v,d=2)=>Number(v||0).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
setInterval(()=>$('clock').textContent=new Date().toLocaleString('zh-CN',{hour12:false}),1000);
function rowClass(v){return Number(v)>=0?'pos':'neg'}
async function json(url){const r=await fetch(url);if(!r.ok)throw new Error(await r.text());return r.json()}
async function refresh(){
  $('apiText').textContent='连接中'; $('apiDot').style.background='var(--amber)';
  try{
    const [h,m,d]=await Promise.all([json('/api/v1/health'),json('/api/v1/market/tickers'),json('/api/v1/dashboard')]);
    $('env').textContent=h.environment.toUpperCase()+' / MKT '+h.public_market_environment.toUpperCase(); $('env').style.color=h.environment==='live'?'var(--red)':'var(--green)';
    $('apiText').textContent='Gate API 正常'; $('apiDot').style.background='var(--green)'; $('apiBase').textContent=new URL(h.base_url).host;
    $('liveLock').textContent=h.environment==='live'&&h.live_trading_enabled?'UNLOCKED':'LOCKED'; $('liveLock').style.color=h.environment==='live'&&h.live_trading_enabled?'var(--red)':'var(--green)';
    $('maxPosition').textContent=num(h.risk.max_position_notional_usd)+' USDT'; $('maxMargin').textContent=num(h.risk.max_total_margin_usd)+' USDT'; $('maxOrder').textContent=num(h.risk.max_order_margin_usd)+' USDT'; $('proxy').textContent=h.proxy_configured?'ON':'OFF';
    $('tickers').innerHTML=m.items.length?m.items.map(x=>`<div class="ticker-row"><strong>${x.contract}</strong><span>${num(x.last,Number(x.last)<10?4:2)}</span><span class="${rowClass(x.change_percentage)}">${Number(x.change_percentage||0)>0?'+':''}${num(x.change_percentage)}%</span><span>${num(Number(x.funding_rate||0)*100,4)}%</span><span>${num(x.volume_24h_quote||x.volume_24h_usd,0)}</span></div>`).join(''):'<div class="empty">Gate 未返回所选合约行情</div>';
    const a=d.account||{}; $('equity').textContent=a.total?num(a.total):'--'; $('available').textContent=a.available?num(a.available):'--'; $('pnl').textContent=a.unrealised_pnl?num(a.unrealised_pnl):'--'; $('positionCount').textContent=(d.positions||[]).filter(p=>Number(p.size)).length;
    $('notice').textContent=d.private_available?'Gate 私有账户通道已连接，当前页面保持只读。':d.errors.join('；');
    const ps=(d.positions||[]).filter(p=>Number(p.size)); $('positions').innerHTML=ps.length?ps.map(p=>`<tr><td>${p.contract}</td><td class="${Number(p.size)>0?'pos':'neg'}">${Number(p.size)>0?'LONG':'SHORT'} ${Math.abs(Number(p.size))}</td><td>${p.entry_price||'--'}</td><td>${p.mark_price||'--'}</td><td>${p.leverage||'--'}x</td><td class="${rowClass(p.unrealised_pnl)}">${num(p.unrealised_pnl)}</td></tr>`).join(''):'<tr><td colspan="6" class="empty">暂无持仓数据</td></tr>';
    const os=[...(d.orders||[]).map(o=>({type:'普通委托',contract:o.contract,size:o.size,price:o.price,status:o.status})),...(d.protections||[]).map(o=>({type:'保护/计划',contract:o.initial?.contract,size:o.initial?.size,price:o.trigger?.price,status:o.status||'open'}))]; $('orderCount').textContent=os.length+' ORDERS'; $('orders').innerHTML=os.length?os.map(o=>`<tr><td>${o.type}</td><td>${o.contract||'--'}</td><td>${o.size||'--'}</td><td>${o.price||'--'}</td><td class="pos">${o.status}</td></tr>`).join(''):'<tr><td colspan="5" class="empty">暂无活动委托</td></tr>';
    $('protectionState').textContent=ps.length?'需逐仓校验':'无持仓'; $('events').innerHTML=(d.events||[]).map(e=>`<div class="event"><span>${new Date(e.created_at*1000).toLocaleTimeString()}</span><span class="${e.level==='ERROR'?'error':''}">${e.level}</span><span>${e.event}</span></div>`).join('');
  }catch(e){$('apiText').textContent='连接异常';$('apiDot').style.background='var(--red)';$('notice').textContent=e.message}
}
$('refresh').onclick=refresh; refresh(); setInterval(refresh,15000);
