
/* AstrBot 在 body 结束处注入 bridge SDK（晚于本脚本执行）；且资源令牌 60s 过期，
   浏览器缓存旧 HTML 时 SDK 可能 401 永不出现 —— 因此内置协议兼容的备用桥。
   注意：脚本内不要出现 body 或 script 的闭合标签字面量，避免被注入逻辑误伤。 */
let S = null;
let FB = null;
let maisoulTab = 'core';
function setMsTab(t){collect();maisoulTab=t;render('maisoul')}
const TITLES = {overview:'概览',maisoul:'麦麦设置',personamgr:'人格管理',rhythm:'发言节奏',learn:'学习',bridge:'管家桥',observe:'麦麦观察',models:'模型管理',exprv:'表达审核'};
const PAGE_VERSION = 'v6.27.0';

const sleep = ms => new Promise(r => setTimeout(r, ms));

function makeFallbackBridge(){
  const CHANNEL = 'astrbot-plugin-page';
  const pending = new Map();
  let counter = 0;
  window.addEventListener('message', (event) => {
    if (event.source !== window.parent) return;
    const m = event.data;
    if (!m || m.channel !== CHANNEL || m.kind !== 'response') return;
    const p = pending.get(m.requestId);
    if (!p) return;
    pending.delete(m.requestId);
    if (m.ok) p.resolve(m.data); else p.reject(new Error(m.error || 'bridge request failed'));
  });
  window.parent.postMessage({channel: CHANNEL, kind: 'ready'}, '*');
  const request = (action, payload) => new Promise((resolve, reject) => {
    const requestId = 'maisoul_fb_' + (++counter);
    pending.set(requestId, {resolve, reject});
    window.parent.postMessage({channel: CHANNEL, kind: 'request', requestId, action, ...payload}, '*');
    setTimeout(() => { if (pending.has(requestId)) { pending.delete(requestId); reject(new Error('请求超时')); } }, 15000);
  });
  return {
    apiGet: (endpoint, params) => request('api:get', {endpoint, params: params || {}}),
    apiPost: (endpoint, body) => request('api:post', {endpoint, body: body || {}}),
  };
}

function getBridge(){ return window.AstrBotPluginPage || FB }
async function ensureBridge(timeout=2500){
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    if (window.AstrBotPluginPage) return window.AstrBotPluginPage;
    await sleep(120);
  }
  FB = makeFallbackBridge();
  return FB;
}

function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2200)}
async function apiGet(name,params){ const b=getBridge(); if(!b) throw new Error('WebUI 桥未就绪，请从 AstrBot WebUI 插件页进入'); const r=await b.apiGet(name,params||{}); if(r&&r.success===false) throw new Error(r.error||'读取失败'); return r?.data??r }
async function apiPost(name,body){ const b=getBridge(); if(!b) throw new Error('WebUI 桥未就绪'); const r=await b.apiPost(name,body); if(r&&r.success===false) throw new Error(r.error||'保存失败'); return r }

/* ---------------- 页面切换 ---------------- */
/* ---------------- 渲染辅助（对齐部署版动态配置表单：分区 panel + 表单行 frow） ---------------- */
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
/* 配置分区：rounded-lg border bg-card p-4 sm:p-6 space-y-3 + h3 + 描述（部署版 bot 配置分区样式） */
function panel(id,title,desc,inner){
  return `<section class="panel" id="card_${id}">${title?`<h3>${title}</h3>`:''}${desc?`<div class="pdesc">${desc}</div>`:''}<div>${inner}</div></section>`
}
function card(id,title,desc,inner){ return panel(id,title,desc,inner) }
/* 表单行：label+说明左侧，控件右侧（sm 起两栏）；wide=true 控件整行 */
function field(label,desc,inner,cnt,wide){
  return `<div class="frow ${wide?'wide':''}"><div class="fmeta"><div class="flab"><span>${label}</span>${cnt?`<span class="fcnt">${cnt}</span>`:''}</div>${desc?`<div class="fdesc">${desc}</div>`:''}</div><div class="fctl">${inner}</div></div>`
}
function textarea(id,v,ph){return `<textarea id="${id}" placeholder="${esc(ph||'')}">${esc(v)}</textarea>`}
function chips(id,arr,ph){return `<div class="chips" data-chips="${id}">${(arr||[]).map((t,i)=>`<span class="chip-tag">${esc(t)}<i onclick="delChip('${id}',${i})">×</i></span>`).join('')}<input placeholder="${esc(ph||'输入后回车添加')}" onkeydown="if(event.key==='Enter'){event.preventDefault();addChip('${id}',this)}"></div>`}
function slider(id,v,min,max,step,unit){return `<div class="fslider"><input type="range" id="${id}" min="${min}" max="${max}" step="${step}" value="${v}" oninput="onFreqSlide(this)"><span class="slider-val" id="${id}v">${v}</span>${unit?`<small class="sunit">${unit}</small>`:''}</div>`}
/* 开关行：部署版 Switch h-5 w-9（选中 primary），标题+副文案在左 */
function switchRow(id,v,title,sub){return `<div class="frow"><div class="fmeta"><div class="flab">${title}</div>${sub?`<div class="fdesc">${sub}</div>`:''}</div><div class="fctl free"><label class="ui-switch"><input type="checkbox" id="${id}" ${v?'checked':''}><span class="th"></span></label></div></div>`}
/* 小按钮（部署版 Button size=sm）带 lucide 图标 */
function smBtn(cls,icon,txt,attrs){return `<button class="ui-btn sm ${cls||'outline'}" ${attrs||''}>${icon?moIcon(icon,14):''}<span>${txt}</span></button>`}
function iconBtn(icon,title,attrs,danger){return `<button class="ui-btn ghost icon sm" title="${esc(title)}" ${attrs||''} style="${danger?'color:hsl(var(--destructive))':''}">${moIcon(icon,14)}</button>`}

function onFreqSlide(el){
  document.getElementById(el.id+'v').textContent=el.value;
  if(el.id!=='talk_value')return;
  const f=parseFloat(el.value);
  const mode=document.getElementById('reply_trigger_mode')?.value||S.reply_trigger_mode||'frequency';
  const th=f>0?Math.ceil(mode==='reply_necessity'?1/(f*f):1/f):0;
  const tip=document.getElementById('freq_tip');
  if(tip)tip.textContent=`触发阈值 = ceil(1/${mode==='reply_necessity'?'f²':'f'}) = ${th} 条积压消息${f<=0?'（频率 0 = 静默接收，不发言）':''}`;
}

/* ---------------- 侧栏导航（分组/图标/字号对齐部署版侧栏） ---------------- */
const NAV=[
  {group:'概览',items:[{p:'overview',icon:'house',label:'首页'},{p:'observe',icon:'activity',label:'麦麦观察'}]},
  {group:'麦麦配置编辑',items:[{p:'maisoul',icon:'settings',label:'麦麦设置'},{p:'rhythm',icon:'clock',label:'发言节奏'},{p:'models',icon:'cpu',label:'模型管理'}]},
  {group:'麦麦资源管理',items:[{p:'personamgr',icon:'user',label:'人格管理'},{p:'learn',icon:'book-open',label:'学习'},{p:'exprv',icon:'circle-check',label:'表达审核'}]},
  {group:'扩展与集成',items:[{p:'bridge',icon:'wrench',label:'管家桥'}]}
];
function navHTML(){return NAV.map(g=>`<div class="sb-group"><div class="sb-gtitle">${g.group}</div>${g.items.map(it=>`<button class="sb-item ${curPage===it.p?'on':''}" onclick="render('${it.p}')"><span class="ic">${moIcon(it.icon,20)}</span><span class="lb">${it.label}</span></button>`).join('')}</div>`).join('')}
function toggleSidebar(){
  const sb=document.getElementById('sb');if(!sb)return;
  sb.classList.toggle('mini');
  const btn=document.getElementById('tb_sb');
  if(btn)btn.innerHTML=moIcon(sb.classList.contains('mini')?'chevron-right':'chevron-left',16);
}
function render(p){
  curPage=p;
  /* 离开麦麦观察页即拆数据通道（v6.20.3）：此前轮询/SSE 一直打到页面卸载，
     MO.started 门闩还保证"进过一次就永远轮询"；seenIds/events 保留，
     重进页面时增量去重不受影响 */
  if(p!=='observe'&&typeof MO!=='undefined'&&MO.started)moTeardown();
  const nav=document.getElementById('nav');if(nav)nav.innerHTML=navHTML();
  document.getElementById('ptitle').textContent=TITLES[p]||'';
  const sv=document.getElementById('saveBtn');if(sv)sv.style.display=(p==='observe'||p==='overview'||p==='models'||p==='exprv')?'none':'';
  const sc=document.querySelector('.pagescroll');if(sc)sc.scrollTop=0;
  /* 表达审核页全幅：破 .page 的 max-width/内边距，右缘贴齐内容区、框体拉满高度 */
  const pb=document.getElementById('pagebox');
  if(pb){if(p==='exprv'){pb.style.maxWidth='none';pb.style.padding='12px'}else{pb.style.maxWidth='';pb.style.padding=''}}
  const b=document.getElementById('body');
  if(p==='overview'){b.innerHTML=overviewHTML();loadStatus();return}
  if(p==='observe'){b.innerHTML=observeHTML();startMonitor();return}
  if(p==='exprv'){b.innerHTML='<div style="height:calc(100vh - 104px);min-height:520px"><div class="exr-wrap" id="exr_root"></div></div>';exLoad(true);return}
  if(!S){b.innerHTML='<div class="loading">配置未加载</div>';return}
  const map={
    maisoul: ()=>{
      const core = [
        panel('ident','人格配置','机器人昵称、别名与人格设定。',
          field('机器人昵称（bot_name）','麦麦显示和自称时使用的名字。',`<input type="text" id="bot_name" value="${esc(S.bot_name)}">`)+
          field('别名（aliases）','别人可能用来称呼麦麦的名字，用于辅助识别提及。',chips('aliases',S.aliases,'小麦'),'',true)+
          field('人格设定（personality）','麦麦的人格和身份设定，建议简短描述她是谁、是什么性格。',textarea('personality',S.personality,'她是谁、什么性格…'),`${String(S.personality||'').length} 字`,true)
        ),
        panel('express','表达风格','麦麦平时说话的风格，例如简短、温和、吐槽或正式。',
          field('表达风格（reply_style）','麦麦平时说话的风格，例如简短、温和、吐槽或正式。',textarea('reply_style',S.reply_style,''),`${String(S.reply_style||'').length} 字`,true)
        ),
        panel('preset','预设对话','示例对话展示麦麦应有的说话风格，生成回复时作为风格参考注入；只参考语气与用词，不照搬内容。',
          `<div id="pds">${(S.preset_dialogues||[]).map((d,i)=>pdRow(i,d)).join('')}</div>
           ${smBtn('outline','plus','添加示例对话','onclick="addPdRow()"')}`
        ),
        panel('behave','行为风格','Planner 使用的行动准则，例如何时参与聊天、如何观察局面以及何时保持安静。',
          field('行为风格（behavior_style）','Planner 使用的行动准则，例如何时参与聊天、如何观察局面以及何时保持安静。',textarea('behavior_style',S.behavior_style,''),`${String(S.behavior_style||'').length} 字`,true)
        ),
        panel('attn','群聊注意事项','群聊通用提示词，告诉麦麦群聊中该怎么说话。',
          field('群聊提示词（group_chat_prompt）','群聊通用提示词，告诉麦麦群聊中该怎么说话。',textarea('group_chat_prompt',S.group_chat_prompt,''),`${String(S.group_chat_prompt||'').length} 字`,true)
        ),
        panel('pattn','私聊提示词','私聊通用提示词，告诉麦麦私聊中该怎么说话。',
          field('私聊提示词（private_chat_prompts）','私聊通用提示词，告诉麦麦私聊中该怎么说话。',textarea('private_chat_prompts',S.private_chat_prompts||'',''),`${String(S.private_chat_prompts||'').length} 字`,true)
        ),
      ].join('');
      const detail = [
        panel('lottery','随机风格彩票','备用说话风格；触发后只影响本次回复。',
          `<div id="mrs">${(S.multiple_reply_style||[]).map((t,i)=>styleItem(i,t)).join('')}</div>
           ${smBtn('outline','plus','添加备用风格','onclick="addStyle()"')}`+
          field('触发概率（multiple_probability）','随机启用备用风格的概率；0 表示不随机切换。',slider('multiple_probability',S.multiple_probability??0,0,100,1,'%'))
        ),
        panel('kwr','关键词反应','命中关键词后，给麦麦追加一段固定反应提示。正则用命名捕获组，reaction 里 <code>[名字]</code> 会被替换为匹配内容。',
          `<div class="lab"><span>关键词规则（keyword_rules）</span></div>
           <div id="kwrs">${(S.keyword_rules||[]).map((r,i)=>kwRow(i,r)).join('')}</div>
           ${smBtn('outline','plus','添加关键词规则','onclick="addKwRow()"')}
           <div class="lab" style="margin-top:14px"><span>正则规则（regex_rules）</span></div>
           <div id="rxrs">${(S.regex_rules||[]).map((r,i)=>rxRow(i,r)).join('')}</div>
           ${smBtn('outline','plus','添加正则规则','onclick="addRxRow()"')}`
        ),
        panel('chatp','额外 Prompt','给指定群聊或私聊额外补充聊天要求；有特殊群规或语气要求时再加。平台与目标 ID 精确匹配（平台填适配器名如 qq），同一目标多条自动拼接。',
          `<div id="cps">${(S.chat_prompts||[]).map((c,i)=>cpItem(i,c)).join('')}</div>
           ${smBtn('outline','plus','添加规则','onclick="addCp()"')}`
        ),
        panel('ctools','聊天工具暴露','把工具/技能直接暴露给聊天 LLM。工具只加 <b>MaiBot 原生工具的 AstrBot 等价物</b>（如 send_meme ≈ MaiBot 的 send_emoji 表情包）——MaiBot 没有的能力不要加，那些通过 call_maid 管家交给 AstrBot 的 agent 模型执行。技能经 AstrBot 原生 build_skills_prompt 注入系统提示词。',
          smBtn('outline','search','从 AstrBot 选取工具 / 技能','onclick="openToolPicker()"')+
          field('暴露的工具（chat_tools）','从 AstrBot 已注册的 llm_tool 中选取（插件/MCP/核心），需与注册名一致',chips('chat_tools',S.chat_tools,'如 send_meme'),'',true)+
          field('暴露的技能（chat_skills）','从 AstrBot 技能库选取（SKILL.md），注入后模型可按技能说明执行',chips('chat_skills',S.chat_skills,'技能名'),'',true)+
          `<div class="fdesc" style="margin-top:8px">管家 <code>call_maid</code>：<span id="ct_maid_state">${maidStateHTML()}</span>，随管家桥开关自动加入/移出聊天工具集。等价关系：MaiBot send_emoji ↔ stealer 的 send_meme；记忆查询类暂无等价物。</div>`
        ),
      ].join('');
      return `<div class="ui-tabs full" style="grid-template-columns:1fr 1fr">
          <button class="${maisoulTab==='core'?'on':''}" onclick="setMsTab('core')">核心设置</button>
          <button class="${maisoulTab==='detail'?'on':''}" onclick="setMsTab('detail')">详细设置</button>
        </div>${maisoulTab==='core'?core:detail}`;
    },
    personamgr: ()=>{
      const names = (S.personas||[]).map(p=>str(p.name||'').trim()).filter(Boolean);
      const opt = (sel)=>['',...names].map(n=>`<option value="${esc(n)}" ${sel===n?'selected':''}>${n||'主配置（兜底）'}</option>`).join('');
      const bound = new Set((S.group_persona||[]).map(g=>str(g.name||'').trim()).filter(Boolean));
      const q = (peSearch||'').toLowerCase();
      const mainName = str(S.bot_name||'麦麦').trim()||'麦麦';
      const mainHit = !q || mainName.toLowerCase().includes(q) || str(S.personality||'').toLowerCase().includes(q);
      const mainRow = mainHit ? `<tr>
          <td style="color:hsl(var(--muted-foreground));text-align:center">—</td>
          <td><b>${esc(mainName)}</b></td>
          <td>${esc(mainName)}</td>
          <td><span class="ui-badge soft">${esc(str(S.personality||'').slice(0,14))||'未设定'}</span></td>
          <td>
            <span class="ui-badge secondary">主配置</span>
            ${!str(S.default_persona||'').trim()?'<span class="ui-badge default">默认</span>':''}
          </td>
          <td style="text-align:right">
            <button class="ui-btn ghost sm" onclick="openPersona('main')" title="编辑">${moIcon('pencil',14)}</button>
          </td>
        </tr>` : '';
      const rows = (S.personas||[])
        .map((p,i)=>({p,i}))
        .filter(({p})=>!q || str(p.name||'').toLowerCase().includes(q) || str(p.personality||'').toLowerCase().includes(q))
        .map(({p,i})=>`<tr>
          <td><input type="checkbox" class="tbl-check pe-ck" data-idx="${i}"></td>
          <td><b>${esc(p.name||'未命名')}</b></td>
          <td>${esc(p.bot_name||'-')}</td>
          <td><span class="ui-badge soft">${esc(str(p.personality||'').slice(0,14))||'未设定'}</span></td>
          <td>
            ${str(S.default_persona||'').trim()===str(p.name||'').trim()?'<span class="ui-badge default">默认</span> ':''}
            ${bound.has(str(p.name||'').trim())?'<span class="ui-badge outline">已绑定群</span>':''}
          </td>
          <td style="text-align:right">
            <button class="ui-btn ghost sm" onclick="openPersona(${i})" title="编辑">${moIcon('pencil',14)}</button>
            <button class="ui-btn ghost sm" onclick="askDelPersona(${i})" title="删除" style="color:hsl(var(--destructive))">${moIcon('trash-2',14)}</button>
          </td>
        </tr>`).join('');
      const stats = [
        {l:'人格总数（含主配置）', v:(S.personas||[]).length+1, big:false},
        {l:'默认人格', v:S.default_persona||(mainName+'·主配置'), big:false},
        {l:'已绑定群', v:(S.group_persona||[]).filter(g=>g.chat&&g.name).length, big:false},
      ].map(s=>`<div class="statc"><small>${s.l}</small><b>${esc(str(s.v))}</b></div>`).join('');
      return `
        <div class="statgrid">${stats}</div>
        ${panel('compat','兼容 astrbot_plugin_persona_switch','管理员在群里发 <code>/persona 人格名</code> 切换会话人格后，maisoul 读取同一会话状态——若存在<b>同名</b> maisoul 人格则立即生效（最高优先级）。',
          switchRow('follow_persona_switch',S.follow_persona_switch??true,'跟随 /persona 切换','会话人格命中同名 maisoul 人格时自动切换三件套'))}
        ${panel('lib','人格库','生效优先级：<b>/persona 实时切换 &gt; 群绑定 &gt; 默认人格 &gt; 主配置</b>。首行为主配置人格（固定、不可删除），其后为库内人格，勾选后可批量删除。',
          `<div class="tbl-toolbar">
             <div class="search">
               <span class="sic">${moIcon('search',15)}</span>
               <input type="text" id="pe_search" placeholder="搜索人格名或设定…" value="${esc(peSearch)}" oninput="peSearch=this.value;render('personamgr');setTimeout(()=>{const e=document.getElementById('pe_search');e.focus();e.setSelectionRange(e.value.length,e.value.length)},0)">
             </div>
             <div style="display:flex;gap:8px">
               ${smBtn('outline','trash-2','批量删除','onclick="batchDelPersona()"')}
               ${smBtn('default','plus','新建人格','onclick="openPersona(null)"')}
             </div>
           </div>
           <table class="ui-tbl" style="margin-top:14px">
             <tr><th style="width:34px"></th><th>人格名</th><th>机器人昵称</th><th>人格设定</th><th>标签</th><th style="text-align:right">操作</th></tr>
             ${mainRow}${rows || `<tr><td colspan="6" class="empty-cell">${mainRow?'暂无库内人格，点右上角「新建人格」创建':'没有匹配的人格'}</td></tr>`}
           </table>`)}
        ${panel('bind','生效规则','默认人格与按群静态绑定（群号支持后缀匹配与 * 通配）。',
          field('默认人格（default_persona）','所有未绑定群的兜底',`<select id="default_persona">${opt(S.default_persona||'')}</select>`)+
          `<div id="gps">${(S.group_persona||[]).map((g,i)=>gpRow(i,g,names)).join('')}</div>
           ${smBtn('outline','plus','添加群绑定','onclick="addGp()"')}`)}
        <div id="pe_modal_host"></div>`;
    },
    rhythm: ()=>[
      panel('mode','发言模式','<code>planner</code> = 完全对标 maisaka 决策层：触发后进入 LLM Planner 循环（reply/wait/send_emoji/fetch_history 工具，由它决定回不回/等多久/发表情）；<code>independent</code> = 触发后单轮生成；<code>native</code> = 触发后交给原生 agent。',
        `<div class="modes">
          <div class="mode ${S.mode==='planner'?'on':''}" onclick="setMode('planner')"><b>决策模式</b><p>MaiBot 原版：Planner agent 循环决策，wait 可再等等</p></div>
          <div class="mode ${S.mode==='independent'?'on':''}" onclick="setMode('independent')"><b>独立模式</b><p>触发后单轮生成：三件套 prompt → LLM → 后处理拟人发送</p></div>
          <div class="mode ${S.mode==='native'?'on':''}" onclick="setMode('native')"><b>协作模式</b><p>麦麦只做门控与三件套注入，生成走原生 agent</p></div>
        </div>`+
        switchRow('enable',S.enable,'总开关','关闭后群聊回到 AstrBot 原生路径')+
        switchRow('escape_at_wake',S.escape_at_wake===true,'逃生舱（@/唤醒放行原生）','默认关=聊天全面接管：@/唤醒前缀也进麦麦管线（at 级强制点名）；开启后恢复旧行为，@/唤醒交给原生 agent 回答。/指令恒放行不受影响')+
        switchRow('eco_injection',S.eco_injection!==false,'生态注入桥','心弦好感/记忆/世界书等注入型插件在麦麦管线内生效：生成前收集 on_llm_request 注入、发言后触发 on_llm_response 回写（记忆沉淀）')
      ),
      panel('plannercfg','Planner 决策设置','决策模式的规划器参数——wait 状态机、思考打断、空闲退避。',
        field('连续 wait 上限（max_consecutive_wait_count）','Planner 最多连续调用 wait 多少次；达到上限后视为对话进入休息',`<input type="number" id="max_consecutive_wait_count" min="1" max="10" value="${S.max_consecutive_wait_count??3}">`)+
        field('规划器连续打断上限（planner_interrupt_max_consecutive_count）','思考时来了新消息，最多重新思考多少次；0 表示不打断',`<input type="number" id="planner_interrupt_max_consecutive_count" min="0" max="10" value="${S.planner_interrupt_max_consecutive_count??0}">`)+
        field('空闲退避基准（no_action_backoff_base_seconds）','连续决定不回复后，下一次检查前先等多久',`<input type="number" id="no_action_backoff_base_seconds" min="0" max="600" step="1" value="${S.no_action_backoff_base_seconds??15}">`)+
        field('空闲退避上限（no_action_backoff_cap_seconds）','不回复退避等待的最长时间',`<input type="number" id="no_action_backoff_cap_seconds" min="0" max="3600" value="${S.no_action_backoff_cap_seconds??300}">`)+
        field('空闲退避起点（no_action_backoff_start_count）','连续几次不回复后开始放慢检查',`<input type="number" id="no_action_backoff_start_count" min="1" max="10" value="${S.no_action_backoff_start_count??2}">`)+
        field('空闲退避绕过消息数（no_action_backoff_bypass_pending_count）','等待期间新消息达到多少条就立刻重新处理；0 表示不按条数打断',`<input type="number" id="no_action_backoff_bypass_pending_count" min="0" max="50" value="${S.no_action_backoff_bypass_pending_count??6}">`)+
        switchRow('enable_reply_quote',S.enable_reply_quote??true,'启用引用回复','回复时是否可以引用上一条或相关消息')
      ),
      panel('timing','什么时候发言','决定新消息何时进入生成——触发方式与发言频率。',
        field('回复触发模式（reply_trigger_mode）','frequency=频率触发：按新消息数量（ceil(1/f) 条 + 空窗补偿）决定；reply_necessity=必要性触发：综合数量、内容、过往发言评分（≥80）',
          `<select id="reply_trigger_mode">${['frequency','reply_necessity'].map(m=>`<option value="${m}" ${S.reply_trigger_mode===m?'selected':''}>${m==='frequency'?'频率触发':'必要性触发'}</option>`).join('')}</select>`)+
        field('群聊频率（talk_value）','群聊里麦麦主动说话的频率；越小越安静，0 为静默接收',slider('talk_value',S.talk_value??1,0,1,0.01,'')+`<div class="fdesc" id="freq_tip" style="margin-top:6px"></div>`)+
        field('私聊频率（private_talk_value）','私聊里麦麦主动说话的频率；越小越安静，0 为静默接收（私聊 wait 期间新消息会立即唤醒）',slider('private_talk_value',S.private_talk_value??1,0,1,0.01,''))+
        switchRow('inevitable_at_reply',S.inevitable_at_reply??true,'At 必回复','被 @ 时会尽量回复（强制触发）')+
        switchRow('mentioned_bot_reply',S.mentioned_bot_reply??false,'提及必回复','只要消息提到麦麦名字就更容易回复（强制触发）')+
        field('群聊上下文（max_context_size）','群聊回复时参考的最近消息数量；越大越懂上下文，也更耗模型',`<input type="number" id="max_context_size" min="5" max="200" value="${S.max_context_size??40}">`)+
        field('私聊上下文（max_private_context_size）','私聊回复时参考的最近消息数量',`<input type="number" id="max_private_context_size" min="5" max="300" value="${S.max_private_context_size??60}">`)+
        switchRow('enable_context_optimization',S.enable_context_optimization??true,'优化上下文','压缩部分上下文——自己的旧发言只保留最近 3 条；一般建议开启')+
        switchRow('enable_image_context',S.enable_image_context??false,'识图上下文（Planner 决策轮）','开启后把最近几张聊天图片附给 Planner 模型理解；需 Planner 任务绑定的模型在 AstrBot「模型能力」里勾选图像。回复生成不看此开关——自动跟随其模型条目的图像勾选')+
        field('识图张数上限（image_context_max_num）','识图上下文一次最多附带的图片张数（Planner 决策轮与回复生成共用）',`<input type="number" id="image_context_max_num" min="1" max="10" value="${S.image_context_max_num??3}">`)
      ),
      panel('filter','消息过滤','命中的消息整条丢弃——不进缓存、不进门控、不触发回复；指令类消息不受影响。',
        field('过滤词（ban_words）','包含这些词的消息直接丢弃，一行一个',textarea('ban_words',(S.ban_words||[]).join('\n'),'一行一个过滤词'),'',true)+
        field('正则过滤（ban_msgs_regex）','逐行一条正则表达式，命中即丢弃；适合更复杂的过滤规则',textarea('ban_msgs_regex',(S.ban_msgs_regex||[]).join('\n'),'一行一条正则'),'',true)
      ),
      panel('rules','动态发言频率规则','按群或时段单独调整频率；精确匹配优先于通配，时段命中优先于留空。',
        switchRow('enable_talk_value_rules',S.enable_talk_value_rules??false,'启用动态发言频率规则','开启后按聊天/时间段调整发言频率')+
        `<div id="tvrs">${(S.talk_value_rules||[]).map((r,i)=>tvrRow(i,r)).join('')}</div>
         ${smBtn('outline','plus','添加规则','onclick="addTvr()"')}`),
      panel('pp','回复后处理','错别字、分段、打字节奏等拟人后处理。',
        switchRow('enable_response_post_process',S.enable_response_post_process??true,'启用回复后处理','开启后会对回复做错别字、分段等后处理')+
        field('打字速度（typing_speed）','模拟打字等待时间；0 最快（不等待），1 默认，2 更慢',slider('typing_speed',S.typing_speed??1,0,2,0.1,'倍'))
      ),
      panel('typo','错别字','基于拼音+字频的同音字引擎（源码级移植 MaiBot typo_generator）：声调错误、同音字、整词替换，50% 概率补发纠正。',
        switchRow('typo_enable',S.typo_enable??true,'启用错别字','让麦麦偶尔打错字，更像真人聊天')+
        switchRow('typo_enable_correction_quote',S.typo_enable_correction_quote??true,'纠正时引用原消息','AstrBot 引用原语暂未接，当前仅记录标记')+
        field('纠正引用概率（typo_correction_quote_probability）','',slider('typo_correction_quote_probability',S.typo_correction_quote_probability??1,0,1,0.05,''))+
        field('单字错字概率（typo_error_rate）','单个字被替换成错字的概率',slider('typo_error_rate',S.typo_error_rate??0.01,0,0.2,0.005,''))+
        field('最小字频（typo_min_freq）','只对常见程度达到该值的字尝试制造错字',`<input type="number" id="typo_min_freq" min="0" max="100" value="${S.typo_min_freq??9}">`)+
        field('声调错字概率（typo_tone_error_rate）','按相近声调制造错字的概率',slider('typo_tone_error_rate',S.typo_tone_error_rate??0.1,0,1,0.05,''))+
        field('整词替换概率（typo_word_replace_rate）','整词被替换成错词的概率',slider('typo_word_replace_rate',S.typo_word_replace_rate??0.006,0,0.1,0.002,''))
      ),
      panel('split','回复分割','分句（引号/冒号保护+概率合并）→ 错字 → 条数上限 → 合并。',
        switchRow('splitter_enable',S.splitter_enable??true,'启用回复分割','把过长回复拆成多条发送')+
        field('单条最大长度（splitter_max_length）','基本全中文且超过 2 倍该值时返回默认回复',`<input type="number" id="splitter_max_length" min="50" max="2000" value="${S.splitter_max_length??512}">`)+
        field('单条最大句数（splitter_max_sentence_num）','分割后超过该条数触发超限策略',`<input type="number" id="splitter_max_sentence_num" min="1" max="30" value="${S.splitter_max_sentence_num??8}">`)+
        field('最多分割条数（splitter_max_split_num）','最终合并到最多几条消息发出',`<input type="number" id="splitter_max_split_num" min="1" max="10" value="${S.splitter_max_split_num??3}">`)+
        switchRow('splitter_enable_kaomoji_protection',S.splitter_enable_kaomoji_protection??false,'保护颜文字','分割前把颜文字替换为占位符，分割后恢复')+
        switchRow('splitter_enable_overflow_return_all',S.splitter_enable_overflow_return_all??false,'超限保留全文','消息数量过多时直接返回原文而非默认回复')
      ),
    ].join(''),
    learn: ()=>{
      const lrRow=(i,r)=>`<div class="cp-row" data-elr="${i}" data-lt="${esc(r.type||'group')}">
        <input type="text" class="elr-platform" placeholder="平台(空=全部)" style="width:100px" value="${esc(r.platform||'')}">
        <input type="text" class="elr-item" placeholder="群号(空=全部)" style="width:110px" value="${esc(r.item_id||'')}">
        <div class="lr-toggle"><small>使用</small><label class="ui-switch"><input type="checkbox" class="elr-use" ${r.use!==false?'checked':''}><span class="th"></span></label></div>
        <div class="lr-toggle"><small>学习</small><label class="ui-switch"><input type="checkbox" class="elr-learn" ${r.learn!==false?'checked':''}><span class="th"></span></label></div>
        ${iconBtn('trash-2','删除','onclick="this.parentElement.remove()"')}</div>`;
      const elRows=(S.expression_learning_list||[]).map((r,i)=>lrRow(i,r)).join('');
      const jlRows=(S.jargon_learning_list||[]).map((r,i)=>lrRow(i,r)).join('');
      const grpRow=(i,g)=>`<div class="cp-row" data-egr="${i}">
        <input type="text" class="egr-targets" placeholder="qq:群号, qq:群号2（逗号分隔）" style="flex:1" value="${esc((g.targets||[]).map(t=>`${t.platform||''}:${t.item_id||''}`).join(', '))}">
        ${iconBtn('trash-2','删除','onclick="this.parentElement.remove()"')}</div>`;
      const egr=(S.expression_groups||[]).map((g,i)=>grpRow(i,g)).join('');
      const jgr=(S.jargon_groups||[]).map((g,i)=>grpRow(i,g)).join('');
      const keyOpts=Object.keys(L||{}).map(k=>`<option value="${esc(k)}" ${LKey===k?'selected':''}>${k==='global'?'全局库':esc(k)}</option>`).join('');
      const eCount=((L&&L[LKey])?L[LKey].expressions:[]).length, jCount=((L&&L[LKey])?L[LKey].jargons:[]).length;
      return [
        `<div class="statgrid">
          <div class="statc"><small>表达数量（${LKey==='global'?'全局库':esc(LKey)}）</small><b>${eCount}</b></div>
          <div class="statc"><small>黑话数量</small><b>${jCount}</b></div>
          <div class="statc"><small>表达学习范围</small><b>${(S.expression_learning_list||[]).length} 条规则</b></div>
          <div class="statc"><small>黑话学习范围</small><b>${(S.jargon_learning_list||[]).length} 条规则</b></div>
        </div>`,
        panel('expr','表达学习','发言后从群聊学习"当 X 时可以用 Y 表达"的语言风格，回复时抽候选注入【表达习惯参考】；学习库满 10 条后启用。',
          switchRow('expression_checked_only',S.expression_checked_only??true,'使用精选表达','仅使用通过 AI 检查的表达')+
          switchRow('expression_self_reflect',S.expression_self_reflect??true,'优化表达方式学习','写入表达方式前先让 AI 检查，减少学到奇怪内容')+
          field('表达使用方式（expression_selection_mode）','随手=随手抽取候选；超级精细=表达意图与嵌入召回（需在模型管理 embedding 任务配置嵌入模型，未配置自动回落随手）',
            `<select id="expression_selection_mode">${['legacy','vector_intent'].map(m=>`<option value="${m}" ${S.expression_selection_mode===m?'selected':''}>${m==='legacy'?'随手':'超级精细'}</option>`).join('')}</select>`)+
          field('向量候选上限（expression_vector_candidate_pool_size）','超级精细模式下每次召回进入精选的候选条数上限（1~50）',
            `<input id="expression_vector_candidate_pool_size" type="number" min="1" max="50" value="${S.expression_vector_candidate_pool_size??50}" class="ui-input">`)+
          field('学习并发上限（max_expression_learner）','同时运行的表达学习任务数量；太高可能占用更多资源',
            `<input type="number" id="max_expression_learner" min="1" max="10" value="${S.max_expression_learner??3}">`)+
          field('学习配置（expression_learning_list）','哪些聊天使用/学习表达方式；平台与群号都留空表示全局默认',
            `<div id="elrs">${elRows}</div>${smBtn('outline','plus','添加规则','onclick="addLrRow(\'elrs\')"')}`,null,true)+
          field('表达共享组（expression_groups）','组内聊天共享学到的表达方式',
            `<div id="egrs">${egr}</div>${smBtn('outline','plus','添加共享组','onclick="addGrpRow(\'egrs\')"')}`,null,true)+
          `<div class="lab" style="margin-top:14px"><span>表达库</span>
            <select id="lib_key" onchange="LKey=this.value;render('learn')">${keyOpts||'<option value="global">全局库</option>'}</select>
            ${smBtn('outline','plus','手动添加','onclick="addExprLib()"')}
            ${smBtn('outline','save','保存学习库','onclick="saveLearningLib()"')}</div>
           <table class="ui-tbl" id="lib_expr"><tr><th>情境 situation</th><th>表达 style</th><th style="width:56px">次数</th><th style="width:70px">精选</th><th style="width:44px"></th></tr>
            ${((L&&L[LKey])?L[LKey].expressions:[]).map((e,i)=>`<tr>
              <td><input type="text" class="le-sit" value="${esc(e.situation||'')}"></td>
              <td><input type="text" class="le-sty" value="${esc(e.style||'')}"></td>
              <td style="text-align:center" class="le-count">${e.count||1}</td>
              <td style="text-align:center"><input type="checkbox" class="le-ck" ${e.checked?'checked':''}></td>
              <td>${iconBtn('trash-2','删除','onclick="delLib(\'expressions\','+i+')"')}</td></tr>`).join('')||'<tr><td colspan="5" class="empty-cell">暂无学到的表达（发言后自动学习，满 10 条启用注入）</td></tr>'}
           </table>`
        ),
        panel('jargon','黑话学习','学习群内黑话/缩写/梗词及含义，上下文命中词条时注入【黑话参考】帮助理解语境。',
          field('学习配置（jargon_learning_list）','哪些聊天使用/学习黑话；平台与群号都留空表示全局默认',
            `<div id="jlrs">${jlRows}</div>${smBtn('outline','plus','添加规则','onclick="addLrRow(\'jlrs\')"')}`,null,true)+
          field('黑话共享组（jargon_groups）','组内聊天共享学到的黑话',
            `<div id="jgrs">${jgr}</div>${smBtn('outline','plus','添加共享组','onclick="addGrpRow(\'jgrs\')"')}`,null,true)+
          `<div class="lab" style="margin-top:14px"><span>黑话库</span>
            <select onchange="LKey=this.value;render('learn')">${keyOpts||'<option value="global">全局库</option>'}</select>
            ${smBtn('outline','plus','手动添加','onclick="addJargonLib()"')}
            ${smBtn('outline','save','保存学习库','onclick="saveLearningLib()"')}</div>
           <table class="ui-tbl" id="lib_jargon"><tr><th>词条 content</th><th>含义 meaning</th><th style="width:56px">次数</th><th style="width:44px"></th></tr>
            ${((L&&L[LKey])?L[LKey].jargons:[]).map((j,i)=>`<tr>
              <td><input type="text" class="lj-con" value="${esc(j.content||'')}"></td>
              <td><input type="text" class="lj-mea" value="${esc(j.meaning||'')}"></td>
              <td style="text-align:center" class="lj-cnt">${j.count||1}</td>
              <td>${iconBtn('trash-2','删除','onclick="delLib(\'jargons\','+i+')"')}</td></tr>`).join('')||'<tr><td colspan="4" class="empty-cell">暂无学到的黑话（发言后自动学习）</td></tr>'}
           </table>`
        ),
      ].join('');
    },
    models: ()=> modelsHTML(),
    bridge: ()=>[
      panel('maid','管家桥（maid_agent 联动）','聊天模型可召唤 maid_agent 的 <code>call_maid</code> 管家执行任务（查资料/跑任务），拿到结果后用麦麦口吻转达。需要已安装 astrbot_plugin_maid_agent。',
        switchRow('maid_bridge',S.maid_bridge,'启用管家桥','群里"帮我查/帮我做"类请求会派给管家 subagent')
      ),
      panel('arch','架构备忘','',
        `<table class="ui-tbl">
          <tr><th>层</th><th>归属</th></tr>
          <tr><td>闲聊人格</td><td>maisoul 麦麦流水线（档位评分/压力插话/打字延迟）</td></tr>
          <tr><td>任务执行</td><td>maid_agent 管家桥</td></tr>
          <tr><td>重度 agent / 全插件</td><td>唤醒词逃生舱 → 原生路径</td></tr>
        </table>`
      ),
    ].join('')
  };
  b.innerHTML=(map[p]||'')();
  if(document.getElementById('talk_value')) onFreqSlide(document.getElementById('talk_value'));
  if(p==='learn'&&L===null) loadLearning();
  if(p==='models') initModelsPage();
}
let curPage='overview';
let L = null;      // 学习库数据 {key: {expressions:[], jargons:[]}}
let LKey = 'global';
async function loadLearning(){
  try{
    L=await apiGet('learning');
    if(!L||typeof L!=='object')L={};
    if(!L[LKey])LKey='global';
    if(curPage==='learn')render('learn');
  }catch(e){L={};if(curPage==='learn')render('learn')}
}
async function saveLearningLib(){
  try{
    collectLibEdits();
    await apiPost('learning',L);
    toast('✓ 学习库已保存');render('learn');
  }catch(e){toast('保存学习库失败：'+e.message)}
}
function collectLibEdits(){
  if(!L||!L[LKey])return;
  const t=document.getElementById('lib_expr');
  if(t)L[LKey].expressions=[...t.querySelectorAll('tr')].slice(1).filter(r=>r.querySelector('.le-sit')).map(r=>({
    situation:r.querySelector('.le-sit').value.trim(),
    style:r.querySelector('.le-sty').value.trim(),
    count:parseInt(r.querySelector('.le-count')?.textContent)||1,
    checked:r.querySelector('.le-ck').checked
  })).filter(e=>e.situation&&e.style);
  const j=document.getElementById('lib_jargon');
  if(j)L[LKey].jargons=[...j.querySelectorAll('tr')].slice(1).filter(r=>r.querySelector('.lj-con')).map(r=>({
    content:r.querySelector('.lj-con').value.trim(),
    meaning:r.querySelector('.lj-mea').value.trim(),
    count:parseInt(r.querySelector('.lj-cnt')?.textContent)||1
  })).filter(x=>x.content&&x.meaning);
}
function ensureLibBucket(){if(!L)L={};if(!L[LKey])L[LKey]={expressions:[],jargons:[]};return L[LKey]}
function addExprLib(){ensureLibBucket().expressions.push({situation:'',style:'',count:1,checked:true});render('learn');setTimeout(()=>{const rows=document.querySelectorAll('#lib_expr tr');const last=rows[rows.length-1];if(last&&last.querySelector('.le-sit'))last.querySelector('.le-sit').focus()},0)}
function addJargonLib(){ensureLibBucket().jargons.push({content:'',meaning:'',count:1});render('learn');setTimeout(()=>{const rows=document.querySelectorAll('#lib_jargon tr');const last=rows[rows.length-1];if(last&&last.querySelector('.lj-con'))last.querySelector('.lj-con').focus()},0)}
function delLib(kind,i){collectLibEdits();ensureLibBucket()[kind].splice(i,1);render('learn')}
function addLrRow(hostId){document.getElementById(hostId).insertAdjacentHTML('beforeend',
  `<div class="cp-row" data-elr="x" data-lt="group">
    <input type="text" class="elr-platform" placeholder="平台(空=全部)" style="width:100px" value="">
    <input type="text" class="elr-item" placeholder="群号(空=全部)" style="width:110px" value="">
    <div class="lr-toggle"><small>使用</small><label class="ui-switch"><input type="checkbox" class="elr-use" checked><span class="th"></span></label></div>
    <div class="lr-toggle"><small>学习</small><label class="ui-switch"><input type="checkbox" class="elr-learn" checked><span class="th"></span></label></div>
    ${iconBtn('trash-2','删除','onclick="this.parentElement.remove()"')}</div>`)}
function addGrpRow(hostId){document.getElementById(hostId).insertAdjacentHTML('beforeend',
  `<div class="cp-row" data-egr="x"><input type="text" class="egr-targets" placeholder="qq:群号, qq:群号2（逗号分隔）" style="flex:1" value="">${iconBtn('trash-2','删除','onclick="this.parentElement.remove()"')}</div>`)}

/* ---------------- 表达方式审核（像素对齐 MaiBot 部署版 ExpressionReviewer：卡片滑动 + 列表批量 + 创建/编辑弹窗） ----------------
   语义对齐部署版：通过人工审核的表达才会被使用（学习页「使用精选表达」开关），
   拒绝=直接删除；数据源 learning/expressions API（LearningStore 表达条目补 id） */
const EX={mode:'browse',status:'pending',page:1,pageSize:20,items:null,stats:{pending:0,passed:0,total:0},keys:[],sel:new Set(),idx:0,dlg:null,q:'',qFocus:false};
function exFiltered(){
  const arr=EX.items||[];
  if(EX.status==='pending')return arr.filter(x=>!x.checked);
  if(EX.status==='passed')return arr.filter(x=>x.checked);
  return arr;
}
function exSearchFiltered(){
  /* 列表搜索：情景/风格/共享组 子串实时过滤（大小写不敏感） */
  const q=EX.q.trim().toLowerCase();
  if(!q)return exFiltered();
  return exFiltered().filter(x=>(x.situation+'\n'+x.style+'\n'+x.key).toLowerCase().includes(q));
}
function exQ(v){EX.q=v;EX.qFocus=true;EX.page=1;exRender()}
function exPages(list){return Math.max(1,Math.ceil(list.length/EX.pageSize))}
function exLoad(reset){
  apiGet('expressions').then(d=>{
    EX.items=(d&&d.items)||[];EX.stats=(d&&d.stats)||{pending:0,passed:0,total:0};
    EX.keys=[...new Set(EX.items.map(x=>x.key))];
    if(reset){EX.page=1;EX.idx=0}
    const n=exFiltered().length;
    if(EX.idx>=n)EX.idx=Math.max(0,n-1);
    exRender();
  }).catch(e=>{EX.items=null;exRender();toast('加载表达失败：'+e.message)});
}
async function exReview(ids,action){
  if(!ids||!ids.length)return;
  try{
    const r=await apiPost('expressions/review',{ids:ids,action:action});
    if(r&&r.success===false)throw new Error(r.error||'操作失败');
    const idset=new Set(ids);
    if(action==='reject'){EX.items=(EX.items||[]).filter(x=>!idset.has(x.id));EX.sel.clear()}
    else EX.items.forEach(x=>{if(idset.has(x.id))x.checked=(action==='approve')});
    EX.stats.pending=EX.items.filter(x=>!x.checked).length;
    EX.stats.total=EX.items.length;EX.stats.passed=EX.items.length-EX.stats.pending;
    const n=exFiltered().length;
    if(EX.idx>=n)EX.idx=Math.max(0,n-1);
    exRender();
  }catch(e){toast('审核失败：'+e.message)}
}
function exBtn(d){
  /* 审核（通过/拒绝）只经按钮触发——拖拽与方向键只做上下条切换，
     误滑不再吞卡（owner 指令：已通过的随手一滑直接消失是不可接受的） */
  const list=exFiltered();const x=list[EX.idx];if(!x)return;
  const card=document.querySelector('.exr-card:not(.be1):not(.be2)');
  if(card){
    card.classList.add('fly');
    card.style.transform=`translateX(${d>0?420:-420}px) rotate(${d>0?24:-24}deg)`;
    card.style.opacity='0';
    setTimeout(()=>exReview([x.id],d>0?'approve':'reject'),200);
  }else exReview([x.id],d>0?'approve':'reject');
}
function exMove(d){const n=exFiltered().length;if(!n)return;EX.idx=(EX.idx+d+n)%n;exRender()}
function exMode(m){EX.mode=m;exRender()}
function exStatus(s){EX.status=s;EX.page=1;EX.idx=0;EX.sel.clear();exRender()}
function exGo(p){EX.page=Math.min(Math.max(1,p),exPages(exFiltered()));exRender()}
function exJump(){const el=document.getElementById('ex_jump');if(!el)return;const v=parseInt(el.value,10);if(!isNaN(v))exGo(v)}
function exSel(id,on){on?EX.sel.add(id):EX.sel.delete(id);exRender()}
function exSelAll(on){const rows=exFiltered().slice((EX.page-1)*EX.pageSize,EX.page*EX.pageSize);rows.forEach(x=>{on?EX.sel.add(x.id):EX.sel.delete(x.id)});exRender()}
function exSelClear(){EX.sel.clear();exRender()}
function exBatch(action){if(!EX.sel.size){toast('请先选择要审核的表达方式');return}exReview([...EX.sel],action)}
function exStyleBadges(style,max){
  const parts=String(style||'').split(/[,，]/).map(s=>s.trim()).filter(Boolean);
  const shown=parts.slice(0,max||3).map(s=>`<span class="exr-badge">${esc(s)}</span>`).join('');
  return shown+(parts.length>(max||3)?'<span class="exr-badge">…</span>':'');
}
function exCardHTML(x,cls){
  return `<div class="exr-card ${cls||''}">
    <div><div class="exr-lab">情景</div><div class="exr-sit" style="margin-top:6px">${esc(x.situation)}</div></div>
    <div><div class="exr-lab">风格</div><div class="exr-stys" style="margin-top:6px">${exStyleBadges(x.style,12)||'<span class="exr-badge">—</span>'}</div></div>
    <div class="exr-foot"><div class="exr-who"><span class="exr-av">${moIcon('sparkles',12)}</span><span title="${esc(x.key)}" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500">${esc(x.key)}</span>${x.checked?`<span class="exr-ok-tag">${moIcon('circle-check',12)}人工通过</span>`:''}</div><span style="flex:none;font-family:ui-monospace,SFMono-Regular,monospace">×${x.count} 次</span></div>
  </div>`;
}
function exBrowseHTML(list){
  if(!list.length)return `<div class="exr-empty">${EX.status==='pending'?'全部审核完成！':'当前筛选条件下没有待处理的项目'}</div>`;
  const x=list[EX.idx]||list[0];
  return `<div class="exr-deck">
      <div style="position:absolute;top:10px;right:16px;display:flex;align-items:center;gap:8px;font-size:12px;color:hsl(var(--muted-foreground))">第 ${EX.idx+1} / ${list.length} 条
        <button class="exr-abtn" title="编辑此条" onclick="exDlg(${x.id})">${moIcon('pencil',15)}</button>
      </div>
      ${list[EX.idx+2]?exCardHTML(list[EX.idx+2],'be2'):''}${list[EX.idx+1]?exCardHTML(list[EX.idx+1],'be1'):''}
      ${exCardHTML(x,'')}
    </div>
    <div class="exr-acts">
      <button class="exr-rbtn no" title="拒绝" onclick="exBtn(-1)">${moIcon('x',28)}</button>
      <button class="exr-rbtn ok" title="通过" onclick="exBtn(1)">${moIcon('check',28)}</button>
    </div>
    <div class="exr-hint">
      <span style="display:flex;align-items:center;gap:4px"><span class="exr-kbd">←</span>上一条</span>
      <span style="display:flex;align-items:center;gap:4px"><span class="exr-kbd">→</span>下一条</span>
      <span style="opacity:.5">|</span><span>拖拽卡片切换上一条 / 下一条</span>
      <span style="opacity:.5">|</span><span>通过 / 拒绝请点下方按钮</span>
    </div>`;
}
function exPageBtns(pages){
  const c=[],p=EX.page;
  if(pages<=7){for(let i=1;i<=pages;i++)c.push(i)}
  else{c.push(1);if(p>3)c.push('…');for(let i=Math.max(2,p-1);i<=Math.min(pages-1,p+1);i++)c.push(i);if(p<pages-2)c.push('…');c.push(pages)}
  return c.map(i=>i==='…'?'<span style="padding:0 4px">…</span>':`<button class="exr-pg ${i===p?'on':''}" onclick="exGo(${i})">${i}</button>`).join('');
}
function exRowHTML(x){
  /* 行尾按钮照抄部署版：待审=绿✓通过+红✕拒绝；已通过=红✕改为拒绝 */
  const acts = x.checked
    ? `<button class="exr-abtn no" title="改为拒绝" onclick="exRowReview(${x.id},'reject')">${moIcon('x',16)}</button>`
    : `<button class="exr-abtn ok" title="同意" onclick="exRowReview(${x.id},'approve')">${moIcon('check',16)}</button>
       <button class="exr-abtn no" title="拒绝" onclick="exRowReview(${x.id},'reject')">${moIcon('x',16)}</button>`;
  return `<div class="exr-row ${EX.sel.has(x.id)?'sel':''}">
    <div class="exr-row-in">
      <input type="checkbox" style="margin-top:5px" ${EX.sel.has(x.id)?'checked':''} onchange="exSel(${x.id},this.checked)">
      <div style="flex:1;min-width:0">
        <div class="exr-row-grid">
          <div style="min-width:0"><span class="exr-row-lab">情景：</span><p class="exr-row-sit">${esc(x.situation)}</p></div>
          <div style="min-width:0"><span class="exr-row-lab">风格：</span><p class="exr-row-sty">${esc(x.style)}</p></div>
        </div>
        <div class="exr-row-meta">
          <span>#${x.id}</span><span>·</span>
          <span title="${esc(x.key)}" style="max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(x.key)}</span>
          <span>·</span><span>×${x.count} 次</span>
          ${x.checked?`<span class="exr-ok-tag">${moIcon('circle-check',12)}人工通过</span>`:''}
        </div>
      </div>
      <div style="display:flex;flex:none;align-items:center;gap:4px">
        ${acts}
        <button class="exr-abtn" title="编辑" onclick="exDlg(${x.id})">${moIcon('pencil',16)}</button>
      </div>
    </div></div>`;
}
function exListHTML(list){
  if(!list.length)return `<div class="exr-empty">${EX.status==='pending'?'全部审核完成！':'没有找到表达方式'}</div>`;
  EX.page=Math.min(Math.max(1,EX.page),exPages(list));
  const rows=list.slice((EX.page-1)*EX.pageSize,EX.page*EX.pageSize);
  const allSel=rows.length&&rows.every(x=>EX.sel.has(x.id));
  return `<div class="exr-listwrap">
    <div class="exr-batch">
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer"><input type="checkbox" onchange="exSelAll(this.checked)" ${allSel?'checked':''}>${allSel?`已全选当前页 (${rows.length} 条)`:`全选当前页 (${rows.length} 条)`}</label>
      ${EX.sel.size?`<span style="color:hsl(var(--foreground))">已选 ${EX.sel.size} 条</span>
        <button class="ui-btn sm" onclick="exBatch('approve')">${moIcon('circle-check',14)}<span>批量通过</span></button>
        <button class="ui-btn sm outline" onclick="exBatch('reject')" style="color:hsl(var(--destructive))">${moIcon('trash-2',14)}<span>批量拒绝</span></button>
        <button class="ui-btn sm ghost" onclick="exSelClear()">取消选择</button>`:''}
    </div>
    <div class="exr-rows">${rows.map(exRowHTML).join('')}</div>
    <div class="exr-pager">
      <span>共 ${list.length} 条 · 每页 ${EX.pageSize} 条</span>
      <span style="display:flex;align-items:center;gap:4px;flex-wrap:wrap">
        <button class="exr-pg" ${EX.page<=1?'disabled':''} onclick="exGo(${EX.page-1})">上一页</button>
        ${exPageBtns(exPages(list))}
        <button class="exr-pg" ${EX.page>=exPages(list)?'disabled':''} onclick="exGo(${EX.page+1})">下一页</button>
        <span style="margin-left:8px;display:flex;align-items:center;gap:4px">跳至<input class="exr-pginput" id="ex_jump" onkeydown="if(event.key==='Enter')exJump()">页</span>
      </span>
    </div></div>`;
}
function exRowReview(id,action){exReview([id],action)}
function exListDlgHTML(){
  const list=exSearchFiltered();
  return `<div class="exr-bigback" onclick="if(event.target===this)exMode('browse')">
    <div class="exr-big">
      <div class="exr-big-h">
        <b>表达方式审核 · 列表</b>
        <span class="exr-count">共 ${list.length} 条</span>
        <div class="exr-srch">${moIcon('search',15)}<input id="ex_q" placeholder="搜索情景或风格..." value="${esc(EX.q)}" oninput="exQ(this.value)"></div>
        <span class="exr-sp"></span>
        <button class="exr-abtn" title="关闭" onclick="exMode('browse')">${moIcon('x',16)}</button>
      </div>
      ${exListHTML(list)}
    </div></div>`;
}
function exKeyOptions(){
  /* 候选共享组：global + 配置「表达共享组」派生键（group_0…，见 learning.share_key）+ 库里已出现的键 */
  const opts=['global'];
  ((S&&S.expression_groups)||[]).forEach((_,i)=>{if(!opts.includes('group_'+i))opts.push('group_'+i)});
  (EX.keys||[]).forEach(k=>{if(!opts.includes(k))opts.push(k)});
  return opts;
}
function exDlg(id){
  if(id==null){EX.dlg={id:null,key:EX.keys[0]||'global',situation:'',style:'',checked:true}}
  else{const x=(EX.items||[]).find(i=>i.id===id);if(!x)return;EX.dlg={id:x.id,key:x.key,situation:x.situation,style:x.style,checked:x.checked}}
  exRender();
}
function exDlgClose(){EX.dlg=null;exRender()}
function exDlgHTML(){
  const d=EX.dlg;
  return `<div class="exr-dlgback" onclick="if(event.target===this)exDlgClose()">
    <div class="exr-dlg">
      <div class="exr-dlg-h"><h3>${d.id==null?'创建新的表达方式记录':'修改表达方式的信息'}</h3>
        <p>${d.id==null?'手动添加一条「当 X 时可以用 Y 表达」，创建即视为人工通过':'修改情景或风格，保存后立即生效'}</p></div>
      <div class="exr-dlg-b">
        <div><label>共享组</label>${d.id==null
          ?`<input class="exr-sel" id="ex_dlg_key" list="ex_dlg_keys" value="${esc(d.key)}" placeholder="global">
            <datalist id="ex_dlg_keys">${exKeyOptions().map(k=>`<option value="${esc(k)}">`).join('')}</datalist>
            <div style="margin-top:4px;font-size:11px;color:hsl(var(--muted-foreground))">组键来自学习页「表达共享组」（group_0…），也可直接输入自定义键；global = 全局共享</div>`
          :`<input class="exr-sel" value="${esc(d.key)}" disabled>`}</div>
        <div><label>情景（当…时）</label><textarea class="exr-ta" id="ex_dlg_sit" placeholder="例：群友分享日常小事时">${esc(d.situation)}</textarea></div>
        <div><label>风格（可以用…表达，逗号分隔多个）</label><textarea class="exr-ta" id="ex_dlg_sty" placeholder="例：简短接话，带点调侃，像熟人之间">${esc(d.style)}</textarea></div>
        <label style="display:flex;align-items:center;gap:8px;font-weight:500;cursor:pointer"><input type="checkbox" id="ex_dlg_ck" ${d.checked?'checked':''}>已人工通过（开启「使用精选表达」时才会被使用）</label>
      </div>
      <div class="exr-dlg-f">
        <button class="ui-btn sm outline" onclick="exDlgClose()">取消</button>
        <button class="ui-btn sm" onclick="exDlgSave()">${moIcon('save',14)}<span>保存</span></button>
      </div>
    </div></div>`;
}
async function exDlgSave(){
  const d=EX.dlg;if(!d)return;
  const sit=(document.getElementById('ex_dlg_sit').value||'').trim();
  const sty=(document.getElementById('ex_dlg_sty').value||'').trim();
  if(!sit||!sty){toast('情景与风格都不能为空');return}
  const sel=document.getElementById('ex_dlg_key');
  const key=d.id==null?(sel?sel.value:'global'):d.key;
  const ck=document.getElementById('ex_dlg_ck');
  try{
    const r=await apiPost('expressions/save',{id:d.id,key:key,situation:sit,style:sty,checked:ck?ck.checked:true});
    if(r&&r.success===false)throw new Error(r.error||'保存失败');
    EX.dlg=null;toast(d.id==null?'创建成功':'保存成功');exLoad(false);
  }catch(e){toast((d.id==null?'创建失败：':'保存失败：')+e.message)}
}
function exBindDrag(){
  const card=document.querySelector('.exr-card:not(.be1):not(.be2)');if(!card)return;
  let sx=0,dx=0,drag=false;
  card.addEventListener('pointerdown',e=>{if(e.button!==undefined&&e.button!==0)return;drag=true;sx=e.clientX;dx=0;card.classList.remove('fly');try{card.setPointerCapture(e.pointerId)}catch(_){}});
  card.addEventListener('pointermove',e=>{if(!drag)return;dx=e.clientX-sx;card.style.transform=`translateX(${dx}px) rotate(${dx*0.05}deg)`;card.style.opacity=String(Math.max(.5,1-Math.abs(dx)/320))});
  const end=()=>{
    if(!drag)return;drag=false;
    if(Math.abs(dx)>90){
      /* 左滑=下一条，右滑=上一条（只导航，不审核） */
      card.classList.add('fly');
      card.style.transform=`translateX(${dx>0?200:-200}px)`;
      card.style.opacity='0';
      setTimeout(()=>exMove(dx<0?1:-1),160);
    }else{
      card.classList.add('fly');card.style.transform='';card.style.opacity='';
      setTimeout(()=>card.classList.remove('fly'),320);
    }};
  card.addEventListener('pointerup',end);
  card.addEventListener('pointercancel',end);
}
function exRender(){
  const root=document.getElementById('exr_root');if(!root)return;
  if(EX.items===null){root.innerHTML='<div class="exr-empty">加载失败，请刷新重试</div>';return}
  const st=EX.stats||{};
  const chips=[['pending','待审',st.pending],['passed','已通过',st.passed],['all','全部',st.total]];
  const list=exFiltered();
  root.innerHTML=`
    <div class="exr-tabs">
      <button class="exr-tab ${EX.mode==='list'?'on':''}" onclick="exMode('list')">${moIcon('layout-dashboard',16)}<span>列表模式</span></button>
      <button class="exr-tab ${EX.mode==='browse'?'on':''}" onclick="exMode('browse')">${moIcon('sparkles',16)}<span>浏览模式</span></button>
    </div>
    <div class="exr-bar">
      ${chips.map(c=>`<button class="exr-chip ${EX.status===c[0]?'on':''}" onclick="exStatus('${c[0]}')">${c[1]}<span class="n">${c[2]??0}</span></button>`).join('')}
      <span class="exr-sp"></span>
      <span class="exr-count" style="text-align:right">审核麦麦学习到的表达方式。通过人工审核的才会被使用（可在学习页调整），拒绝的会被直接删除。</span>
      <button class="ui-btn sm outline" onclick="exLoad()">${moIcon('refresh-cw',14)}<span>刷新</span></button>
      <button class="ui-btn sm" onclick="exDlg(null)">${moIcon('plus',14)}<span>创建表达</span></button>
    </div>
    <div class="exr-body">${exBrowseHTML(list)}</div>
    ${EX.mode==='list'?exListDlgHTML():''}
    ${EX.dlg?exDlgHTML():''}`;
  if(EX.mode==='browse')exBindDrag();
  /* 列表搜索框重渲染后回焦点、光标置尾（oninput 全量重绘会丢焦） */
  if(EX.mode==='list'&&EX.qFocus){
    const q=document.getElementById('ex_q');
    if(q){q.focus();try{q.setSelectionRange(q.value.length,q.value.length)}catch(_){}}
    EX.qFocus=false;
  }
}
document.addEventListener('keydown',e=>{
  if(curPage!=='exprv'||EX.dlg||EX.mode!=='browse')return;
  const t=e.target;
  if(t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.tagName==='SELECT'))return;
  /* 方向键只做导航；审核走按钮（owner 指令） */
  if(e.key==='ArrowLeft'||e.key==='ArrowUp'){e.preventDefault();exMove(-1)}
  else if(e.key==='ArrowRight'||e.key==='ArrowDown'){e.preventDefault();exMove(1)}
});

function overviewHTML(){
  return `<div class="page-head"><h1>概览</h1></div>
    <div class="statgrid" id="stats"><div class="loading" style="padding:20px;grid-column:1/-1">读取运行状态…</div></div>
    ${panel('tips','使用提示','',`· 保存后立即生效，无需重启<br>
     · 与 Heartflow 插件同为意愿门控，建议只启用一个<br>
     · <code>/maisoul</code> 在聊天里查看状态；<code>/maisoul on|off</code> 快捷开关<br>
     · 触发模式：频率触发=攒 ceil(1/f) 条消息（或空窗补偿）说话｜必要性触发=@直通、提及 80 分、普通消息靠"内容+积压压力"攒分`)}
    ${panel('gtable','活跃群','各群的消息缓冲、积压压力与发言情况','<div id="gtable"><div class="loading" style="padding:16px">…</div></div>')}`;
}
async function loadStatus(){
  try{
    const d=await apiGet('status');
    const st=document.getElementById('stats');
    if(st)st.innerHTML=`
      <div class="statc"><small>运行状态</small><b class="big">${d.enable?'运行中':'已停用'}</b></div>
      <div class="statc"><small>发言模式</small><b>${d.mode==='native'?'协作':d.mode==='planner'?'决策':'独立'}</b></div>
      <div class="statc"><small>触发模式</small><b>${d.reply_trigger_mode==='reply_necessity'?'必要性':'频率'}</b></div>
      <div class="statc"><small>群聊频率</small><b>${d.talk_value!=null?d.talk_value:'—'}</b><small style="margin-top:2px">阈值 ${d.trigger_threshold??'—'} 条</small></div>
      <div class="statc"><small>管家桥</small><b>${d.maid_bridge?'已接通':'关闭'}</b></div>
      <div class="statc"><small>人格</small><b>${esc(d.bot_name)}</b></div>`;
    const gt=document.getElementById('gtable');
    const g=Object.entries(d.groups||{});
    if(gt)gt.innerHTML=g.length?`<table class="ui-tbl"><tr><th>群</th><th>缓冲</th><th>积压</th><th>5min自发</th><th>上次发言</th></tr>${
      g.map(([k,v])=>`<tr><td>${esc(k.slice(0,22))}</td><td>${v.buffer}</td><td>${v.pending} 条</td><td>${v.recent_self}</td><td>${v.last_fire_ago!=null?v.last_fire_ago+' 秒前':'—'}</td></tr>`).join('')}</table>`
      :'<div class="fdesc" style="padding:8px 0">暂无活跃群（收到群消息后出现）</div>';
  }catch(e){const st=document.getElementById('stats');if(st)st.innerHTML=`<div class="panel" style="grid-column:1/-1">状态读取失败：${esc(e.message)}</div>`}
}

/* ---------------- 备用风格列表 ---------------- */
function styleItem(i,t){return `<div class="style-item"><textarea data-style="${i}">${esc(t)}</textarea>${iconBtn('trash-2','删除','onclick="this.parentElement.remove()"')}</div>`}
function str(v){return String(v??'')}
let pe = null;        // 正在编辑的人格 {idx: number|null, data: {...}}
let peSearch = '';    // 人格库搜索词
let delTarget = null; // 待删除确认的人格 idx

function openPersona(idx){
  if(idx==='main'){
    pe = {idx:'main', data:{name:'主配置', bot_name:str(S.bot_name||''), aliases:[...(S.aliases||[])],
      personality:str(S.personality||''), behavior_style:str(S.behavior_style||''),
      reply_style:str(S.reply_style||''), group_chat_prompt:str(S.group_chat_prompt||'')}};
  } else {
    pe = idx==null
      ? {idx:null, data:{name:'',bot_name:'',aliases:[],personality:'',behavior_style:'',reply_style:'',group_chat_prompt:''}}
      : {idx, data:JSON.parse(JSON.stringify(S.personas[idx]))};
    if(!Array.isArray(pe.data.aliases)) pe.data.aliases=pe.data.nicknames||[]; // 兼容旧数据
  }
  S.pe_nick = [...(pe.data.aliases||[])];
  showPeModal(true);
}
function showPeModal(open){
  const host = document.getElementById('pe_modal_host');
  if(!host) return;
  if(!open || !pe){ host.innerHTML=''; return; }
  const p = pe.data, isNew = pe.idx==null, isMain = pe.idx==='main';
  const suf = isMain ? '' : '。留空沿用主配置';
  const nameRow = isMain
    ? field('人格名 = 机器人昵称（bot_name）','主配置人格的人格名就是机器人昵称——群里发 <code>/persona '+esc(S.bot_name||'麦麦')+'</code> 即可切到它；修改会同步主配置 bot_name',`<input type="text" id="pe_botname" value="${esc(p.bot_name||'')}" placeholder="麦麦">`)
    : `<div class="row">
        ${field('人格名（name）','人格库唯一标识；/persona 切换与按群绑定都用这个名字匹配',`<input type="text" id="pe_name" value="${esc(p.name||'')}" placeholder="如：傲娇">`)}
        ${field('机器人昵称（bot_name）','麦麦显示和自称时使用的名字'+suf,`<input type="text" id="pe_botname" value="${esc(p.bot_name||'')}" placeholder="${isMain?'麦麦':'留空沿用主配置'}">`)}
      </div>`;
  host.innerHTML = `<div class="modal-mask" onclick="if(event.target===this)closePersona()">
    <div class="modal">
      <div class="modal-head">
        <div><b>${isMain?'编辑主配置人格：'+esc(S.bot_name||'麦麦'):(isNew?'新建人格':'编辑人格'+(isNew?'':'：'+esc(p.name||'')))}${isMain?' <span class="ui-badge secondary">兜底</span>':''}</b>
          <div class="desc">${isMain
            ? '主配置人格：未命中 /persona 切换、群绑定与默认人格时的兜底。修改直接写回主配置，与「麦麦设置」同步。'
            : '非空字段覆盖主配置同名项，留空即沿用。修改需点「保存人格」写回人格库。'}</div></div>
        <button class="modal-x" onclick="closePersona()" title="关闭">${moIcon('x',16)}</button>
      </div>
      <div class="modal-body">
        ${nameRow}
        ${field('别名（aliases）','别人可能用来称呼麦麦的名字，用于辅助识别提及'+suf,chips('pe_nick',p.aliases||[],'小麦'),'',true)}
        ${field('人格设定（personality）','麦麦的人格和身份设定，建议简短描述她是谁、是什么性格'+suf,textarea('pe_personality',p.personality||'',''),'',true)}
        ${field('表达风格（reply_style）','麦麦平时说话的风格，例如简短、温和、吐槽或正式'+suf,textarea('pe_reply',p.reply_style||'',''),'',true)}
        ${field('行为风格（behavior_style）','Planner 使用的行动准则，例如何时参与聊天、如何观察局面以及何时保持安静'+suf,textarea('pe_behavior',p.behavior_style||'',''),'',true)}
        ${field('群聊提示词（group_chat_prompt）','群聊通用提示词，告诉麦麦群聊中该怎么说话'+suf,textarea('pe_gcp',p.group_chat_prompt||'',''),'',true)}
      </div>
      <div class="modal-foot">
        <button class="ui-btn outline" onclick="closePersona()">取消</button>
        <button class="ui-btn default" onclick="commitPersona()">${isMain?'保存主配置':'保存人格'}</button>
      </div>
    </div></div>`;
}
function closePersona(){ pe=null; showPeModal(false); }
function commitPersona(){
  const g=id=>{const el=document.getElementById(id);return el?el.value.trim():''};
  if(pe.idx==='main'){
    if(g('pe_botname')) S.bot_name=g('pe_botname');
    S.aliases=S.pe_nick||[];
    S.personality=g('pe_personality');
    S.behavior_style=g('pe_behavior');
    S.reply_style=g('pe_reply');
    S.group_chat_prompt=g('pe_gcp');
    pe=null; showPeModal(false); render('personamgr'); return;
  }
  pe.data.name=g('pe_name');
  pe.data.bot_name=g('pe_botname');
  pe.data.aliases=S.pe_nick||[];
  pe.data.personality=g('pe_personality');
  pe.data.behavior_style=g('pe_behavior');
  pe.data.reply_style=g('pe_reply');
  pe.data.group_chat_prompt=g('pe_gcp');
  if(!pe.data.name){toast('人格名不能为空');return}
  if(pe.idx==null)S.personas.push(pe.data); else S.personas[pe.idx]=pe.data;
  pe=null; showPeModal(false); render('personamgr');
}
function askDelPersona(idx){ delTarget = idx; renderDelConfirm(); }
function renderDelConfirm(){
  let host = document.getElementById('del_modal_host');
  if(!host){ host=document.createElement('div'); host.id='del_modal_host'; document.body.appendChild(host); }
  if(delTarget==null){ host.innerHTML=''; return; }
  const name = str((S.personas||[])[delTarget]?.name||'');
  host.innerHTML = `<div class="modal-mask" onclick="if(event.target===this){delTarget=null;renderDelConfirm()}">
    <div class="modal" style="max-width:420px">
      <div class="modal-head"><div><b>确认删除</b>
        <div class="desc">确定要删除人格 "${esc(name)}" 吗？此操作不可撤销（保存全部配置后生效）。</div></div></div>
      <div class="modal-foot">
        <button class="ui-btn outline" onclick="delTarget=null;renderDelConfirm()">取消</button>
        <button class="ui-btn destructive" onclick="S.personas.splice(delTarget,1);delTarget=null;renderDelConfirm();render('personamgr')">删除</button>
      </div>
    </div></div>`;
}
function batchDelPersona(){
  const idxs=[...document.querySelectorAll('.pe-ck:checked')].map(c=>+c.dataset.idx);
  if(!idxs.length){toast('先勾选要删除的人格');return}
  S.personas=S.personas.filter((_,i)=>!idxs.includes(i));
  render('personamgr');
}
function gpRow(i,g,names){
  const opt=['',...names].map(n=>`<option value="${esc(n)}" ${(g.name||'')===n?'selected':''}>${n||'— 选择人格 —'}</option>`).join('');
  return `<div class="cp-row" data-gp="${i}"><input type="text" class="gp-chat" placeholder="群号或*" value="${esc(g.chat||'')}"><select class="gp-name">${opt}</select>${iconBtn('trash-2','删除','onclick="this.parentElement.remove()"')}</div>`;
}
function cpItem(i,c){return `<div class="cp-row" data-cp="${i}" data-rt="${esc(c.rule_type||'group')}" style="flex-wrap:wrap"><input type="text" class="cp-platform" placeholder="平台(如qq)" style="width:90px" value="${esc(c.platform||'qq')}"><input type="text" class="cp-chat" placeholder="群号" style="width:110px" value="${esc(c.item_id||'')}"><textarea class="cp-prompt" placeholder="给这个聊天额外补充的要求">${esc(c.prompt||'')}</textarea>${iconBtn('trash-2','删除','onclick="this.parentElement.remove()"')}</div>`}
function addCp(){document.getElementById('cps').insertAdjacentHTML('beforeend',cpItem((document.querySelectorAll('[data-cp]').length)||0,{}))}
function tvrRow(i,r){return `<div class="cp-row" data-tvr="${i}" data-rt="${esc(r.rule_type||'group')}"><input type="text" class="tvr-platform" placeholder="平台(空=全部)" style="width:100px" value="${esc(r.platform||'')}"><input type="text" class="tvr-item" placeholder="群号(空=全部 *" style="width:110px" value="${esc(r.item_id||'')}"><input type="text" class="tvr-time" placeholder="00:00-08:59 或 *" style="width:120px" value="${esc(r.time||'')}"><input type="number" class="tvr-value" placeholder="0-1" style="width:70px" step="0.05" min="0" max="1" value="${r.value!=null?r.value:1}">${iconBtn('trash-2','删除','onclick="this.parentElement.remove()"')}</div>`}
function addTvr(){document.getElementById('tvrs').insertAdjacentHTML('beforeend',tvrRow((document.querySelectorAll('[data-tvr]').length)||0,{}))}
function pdRow(i,d){return `<div class="cp-row" data-pdr="${i}"><textarea class="pdr-user" placeholder="对方说（示例）" style="flex:1">${esc(d.user||'')}</textarea><textarea class="pdr-reply" placeholder="麦麦回（示例）" style="flex:1">${esc(d.reply||'')}</textarea>${iconBtn('trash-2','删除','onclick="this.parentElement.remove()"')}</div>`}
function addPdRow(){const h=document.getElementById('pds');if(!h)return;h.insertAdjacentHTML('beforeend',pdRow(h.children.length,{}))}
function kwRow(i,r){return `<div class="cp-row" data-kwr="${i}"><input type="text" class="kwr-keys" placeholder="关键词，逗号分隔" style="width:180px" value="${esc((r.keywords||[]).join(', '))}"><textarea class="kwr-reaction" placeholder="命中后给麦麦看的提示内容（不会直接发送）" style="flex:1">${esc(r.reaction||'')}</textarea>${iconBtn('trash-2','删除','onclick="this.parentElement.remove()"')}</div>`}
function addKwRow(){document.getElementById('kwrs').insertAdjacentHTML('beforeend',kwRow((document.querySelectorAll('[data-kwr]').length)||0,{}))}
function rxRow(i,r){return `<div class="cp-row" data-rxr="${i}"><textarea class="rxr-pattern" placeholder="正则（每行一个，用命名捕获组 (?P&lt;名字&gt;…)）" style="width:240px;height:44px">${esc((r.regex||[]).join('\n'))}</textarea><textarea class="rxr-reaction" placeholder="提示内容，[名字] 会被替换为捕获组内容" style="flex:1;height:44px">${esc(r.reaction||'')}</textarea>${iconBtn('trash-2','删除','onclick="this.parentElement.remove()"')}</div>`}
function addRxRow(){document.getElementById('rxrs').insertAdjacentHTML('beforeend',rxRow((document.querySelectorAll('[data-rxr]').length)||0,{}))}
function addStyle(){document.getElementById('mrs').insertAdjacentHTML('beforeend',styleItem((document.querySelectorAll('[data-style]').length)||0,''))}

/* ---------------- chips ---------------- */
function addChip(id,input){const v=input.value.trim();if(!v)return;S[id]=[...(S[id]||[]),v];input.value='';refreshChips(id)}
function delChip(id,i){S[id]=S[id].filter((_,j)=>j!==i);refreshChips(id)}
function refreshChips(id){const el=document.querySelector(`[data-chips="${id}"]`);if(!el)return;const keep=el.querySelector('input').value;const next=document.createElement('div');next.innerHTML=chips(id,S[id]);el.replaceWith(next.firstElementChild);const inp=document.querySelector(`[data-chips="${id}"] input`);if(inp){inp.value=keep;inp.focus()}}

/* ---------------- 暴露工具/技能弹窗：列表读 AstrBot 原生注册表（/astrbot_plugin_maisoul/tools） ---------------- */
let tpRegistry=null, tpTab='tools', tpQuery='';
function jsq(s){return String(s??'').replace(/\\/g,'\\\\').replace(/'/g,"\\'")}
async function openToolPicker(){
  if(!tpRegistry){
    try{const d=await apiGet('tools');tpRegistry={tools:d.tools||[],skills:d.skills||[]}}
    catch(e){toast('读取 AstrBot 工具/技能失败：'+e.message);return}
  }
  tpTab='tools';tpQuery='';renderToolPicker();
}
function closeToolPicker(){const h=document.getElementById('tp_modal_host');if(h)h.innerHTML=''}
function tpMatch(it){
  const q=tpQuery.trim().toLowerCase();
  if(!q)return true;
  return str(it.name).toLowerCase().includes(q)
    ||str(it.description||'').toLowerCase().includes(q)
    ||str(it.origin_name).toLowerCase().includes(q)   // 工具的来源（插件/MCP 源名）
    ||str(it.plugin_name).toLowerCase().includes(q);  // 技能的来源插件字段名不同
}
function tpRowHTML(it,selKey){
  const sel=new Set(S[selKey]||[]);
  const maidOn=selKey==='chat_tools'&&S.maid_bridge!==false&&!sel.has('call_maid');  // 管家由开关默认加入，不在 chat_tools 数组里
  const viaBridge=selKey==='chat_tools'&&it.name==='call_maid'&&maidOn;
  const on=sel.has(it.name)||viaBridge;
  const badge=selKey==='chat_tools'
    ?`<span class="ui-badge ${it.origin==='builtin'?'default':it.origin==='plugin'?'secondary':'soft'}">${esc(it.origin==='builtin'?'AstrBot 核心':it.origin==='plugin'?'插件 · '+it.origin_name:it.origin==='mcp'?'MCP · '+it.origin_name:'未注册来源')}</span>${viaBridge?'<span class="ui-badge secondary">管家桥 · 默认</span>':''}`
    :`<span class="ui-badge ${it.source_type==='plugin'?'secondary':'soft'}">${esc(it.source_type==='plugin'?'插件 · '+(it.plugin_name||''):it.source_type==='sandbox_only'?'沙盒':'本地')}${it.active?'':' · 已停用'}</span>`;
  return `<div class="tp-item ${on?'on':''}" data-tpname="${esc(it.name)}" onclick="toggleExpose('${selKey}','${jsq(it.name)}')">
      <span class="tp-check">${on?moIcon('circle-check',12):''}</span>
      <div class="tp-info"><b>${esc(it.name)}</b>${badge}<small>${esc(str(it.description||'').slice(0,90))||'（无描述）'}</small></div>
    </div>`;
}
function tpListHTML(){
  const isTool=tpTab==='tools';
  const list=isTool?tpRegistry.tools:tpRegistry.skills;
  if(!list.length)return `<div class="tp-empty">AstrBot 中暂未${isTool?'注册任何 llm_tool':'安装任何技能'}</div>`;
  const rows=list.filter(tpMatch).map(it=>tpRowHTML(it,isTool?'chat_tools':'chat_skills')).join('');
  const q=tpQuery.trim();
  return rows||`<div class="tp-empty">没有匹配「${esc(q)}」的${isTool?'工具':'技能'}</div>`;
}
function tpFootText(){
  const maidOn=S.maid_bridge!==false&&!(S.chat_tools||[]).includes('call_maid');
  return `已选 工具 ${(S.chat_tools||[]).length+(maidOn?1:0)} · 技能 ${(S.chat_skills||[]).length}，应用后仍需「保存全部配置」`;
}
function renderToolPicker(){
  let host=document.getElementById('tp_modal_host');
  if(!host){host=document.createElement('div');host.id='tp_modal_host';document.body.appendChild(host)}
  const isTool=tpTab==='tools';
  host.innerHTML=`<div class="modal-mask" onclick="if(event.target===this)closeToolPicker()">
    <div class="modal">
      <div class="modal-head">
        <div><b>暴露工具与技能</b>
          <div class="desc">列表实时读取 AstrBot 原生注册表：TOOLS = llm_tool 管理器（插件/MCP/核心工具），SKILL = 技能库（SKILL.md）。勾选即加入暴露列表；管家 call_maid 由「管家桥」开关默认加入，在此勾选/取消即切换该开关。</div></div>
        <button class="modal-x" onclick="closeToolPicker()" title="关闭">${moIcon('x',16)}</button>
      </div>
      <div class="modal-body">
        <div class="ui-tabs" style="margin-bottom:12px">
          <button class="${isTool?'on':''}" onclick="tpTab='tools';renderToolPicker()">${moIcon('wrench',14)} TOOLS <span class="fcnt">${tpRegistry.tools.length}</span></button>
          <button class="${!isTool?'on':''}" onclick="tpTab='skills';renderToolPicker()">${moIcon('book-open',14)} SKILL <span class="fcnt">${tpRegistry.skills.length}</span></button>
        </div>
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
          <span style="color:hsl(var(--muted-foreground));display:inline-flex;flex:none">${moIcon('search',14)}</span>
          <input id="tp_search" class="ui-input" style="flex:1" placeholder="搜索${isTool?'工具':'技能'}：名称 / 描述 / 来源插件" value="${esc(tpQuery)}" oninput="tpQuery=this.value;tpFilterList()">
        </div>
        <div class="tp-list" id="tp_list">${tpListHTML()}</div>
      </div>
      <div class="modal-foot" style="justify-content:space-between;align-items:center">
        <small class="tp-foot" id="tp_foot_txt">${tpFootText()}</small>
        <div style="display:flex;gap:8px">
          <button class="ui-btn outline" onclick="closeToolPicker()">取消</button>
          <button class="ui-btn default" onclick="applyToolPicker()">应用</button>
        </div>
      </div>
    </div></div>`;
}
function tpFilterList(){
  const el=document.getElementById('tp_list');
  if(el)el.innerHTML=tpListHTML();  // 只重绘列表区，搜索框不动（输入焦点不丢）
}
function toggleExpose(key,name){
  const arr=S[key]||[];
  if(name==='call_maid'&&!arr.includes(name)){
    S.maid_bridge=S.maid_bridge===false;  // 不在 chat_tools 里 → 勾/取消就是切管家桥开关
  }else{
    S[key]=arr.includes(name)?arr.filter(n=>n!==name):[...arr,name];
  }
  // 原位更新被点的行与底栏计数——整弹窗重渲染会让列表滚动归零+闪动
  let row=null;
  document.querySelectorAll('#tp_list .tp-item').forEach(el=>{if(el.getAttribute('data-tpname')===name)row=el});
  if(row){
    const list=key==='chat_tools'?tpRegistry.tools:tpRegistry.skills;
    const it=list.find(x=>x.name===name);
    if(it){const tmp=document.createElement('div');tmp.innerHTML=tpRowHTML(it,key);row.replaceWith(tmp.firstElementChild)}
  }
  const ft=document.getElementById('tp_foot_txt');if(ft)ft.textContent=tpFootText();
}
function maidStateHTML(){return S.maid_bridge!==false?'<b style="color:hsl(var(--primary))">已默认暴露</b>（「管家桥」开关开）':'未暴露（「管家桥」开关关）'}
function applyToolPicker(){
  refreshChips('chat_tools');refreshChips('chat_skills');
  const st=document.getElementById('ct_maid_state');if(st)st.innerHTML=maidStateHTML();
  closeToolPicker();toast('✓ 已更新暴露列表，记得保存配置')
}

/* ---------------- 麦麦观察：实时事件流（像素对齐 MaiBot 部署版 /planner-monitor 部署 chunk） ---------------- */
/* lucide 图标 path 数据（取自部署版 icons chunk，viewBox 24 / stroke 2 / round） */
const MO_ICONS={"activity":[["path",{"d":"M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"}]],"bot":[["path",{"d":"M12 8V4H8"}],["rect",{"width":"16","height":"12","x":"4","y":"8","rx":"2"}],["path",{"d":"M2 14h2"}],["path",{"d":"M20 14h2"}],["path",{"d":"M15 13v2"}],["path",{"d":"M9 13v2"}]],"brain":[["path",{"d":"M12 18V5"}],["path",{"d":"M15 13a4.17 4.17 0 0 1-3-4 4.17 4.17 0 0 1-3 4"}],["path",{"d":"M17.598 6.5A3 3 0 1 0 12 5a3 3 0 1 0-5.598 1.5"}],["path",{"d":"M17.997 5.125a4 4 0 0 1 2.526 5.77"}],["path",{"d":"M18 18a4 4 0 0 0 2-7.464"}],["path",{"d":"M19.967 17.483A4 4 0 1 1 12 18a4 4 0 1 1-7.967-.517"}],["path",{"d":"M6 18a4 4 0 0 1-2-7.464"}],["path",{"d":"M6.003 5.125a4 4 0 0 0-2.526 5.77"}]],"timer":[["line",{"x1":"10","x2":"14","y1":"2","y2":"2"}],["line",{"x1":"12","x2":"15","y1":"14","y2":"11"}],["circle",{"cx":"12","cy":"14","r":"8"}]],"wrench":[["path",{"d":"M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"}]],"circle-alert":[["circle",{"cx":"12","cy":"12","r":"10"}],["line",{"x1":"12","x2":"12","y1":"8","y2":"12"}],["line",{"x1":"12","x2":"12.01","y1":"16","y2":"16"}]],"circle-check":[["circle",{"cx":"12","cy":"12","r":"10"}],["path",{"d":"m9 12 2 2 4-4"}]],"check":[["path",{"d":"M20 6 9 17l-5-5"}]],"clock":[["path",{"d":"M12 6v6l4 2"}],["circle",{"cx":"12","cy":"12","r":"10"}]],"chevron-down":[["path",{"d":"m6 9 6 6 6-6"}]],"chevron-right":[["path",{"d":"m9 18 6-6-6-6"}]],"chevron-left":[["path",{"d":"m15 18-6-6 6-6"}]],"eraser":[["path",{"d":"M21 21H8a2 2 0 0 1-1.42-.587l-3.994-3.999a2 2 0 0 1 0-2.828l10-10a2 2 0 0 1 2.829 0l5.999 6a2 2 0 0 1 0 2.828L12.834 21"}],["path",{"d":"m5.082 11.09 8.828 8.828"}]],"arrow-right":[["path",{"d":"M5 12h14"}],["path",{"d":"m12 5 7 7-7 7"}]],"arrow-left":[["path",{"d":"M19 12H5"}],["path",{"d":"m12 19-7-7 7-7"}]],"user":[["path",{"d":"M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"}],["circle",{"cx":"12","cy":"7","r":"4"}]],"circle-pause":[["circle",{"cx":"12","cy":"12","r":"10"}],["line",{"x1":"10","x2":"10","y1":"15","y2":"9"}],["line",{"x1":"14","x2":"14","y1":"15","y2":"9"}]],"house":[["path",{"d":"M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"}],["path",{"d":"M3 10a2 2 0 0 1 .709-1.528l7-6a2 2 0 0 1 2.582 0l7 6A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"}]],"settings":[["path",{"d":"M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"}],["circle",{"cx":"12","cy":"12","r":"3"}]],"book-open":[["path",{"d":"M12 7v14"}],["path",{"d":"M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"}]],"sliders-horizontal":[["path",{"d":"M10 5H3"}],["path",{"d":"M12 19H3"}],["path",{"d":"M14 3v4"}],["path",{"d":"M16 17v4"}],["path",{"d":"M21 12h-9"}],["path",{"d":"M21 19h-5"}],["path",{"d":"M21 5h-7"}],["path",{"d":"M8 10v4"}],["path",{"d":"M8 12H3"}]],"save":[["path",{"d":"M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"}],["path",{"d":"M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7"}],["path",{"d":"M7 3v4a1 1 0 0 0 1 1h7"}]],"refresh-cw":[["path",{"d":"M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"}],["path",{"d":"M21 3v5h-5"}],["path",{"d":"M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"}],["path",{"d":"M8 16H3v5"}]],"plus":[["path",{"d":"M5 12h14"}],["path",{"d":"M12 5v14"}]],"trash-2":[["path",{"d":"M10 11v6"}],["path",{"d":"M14 11v6"}],["path",{"d":"M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"}],["path",{"d":"M3 6h18"}],["path",{"d":"M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"}]],"search":[["path",{"d":"m21 21-4.34-4.34"}],["circle",{"cx":"11","cy":"11","r":"8"}]],"x":[["path",{"d":"M18 6 6 18"}],["path",{"d":"m6 6 12 12"}]],"pencil":[["path",{"d":"M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"}],["path",{"d":"m15 5 4 4"}]],"square-pen":[["path",{"d":"M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"}],["path",{"d":"M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"}]],"database":[["ellipse",{"cx":"12","cy":"5","rx":"9","ry":"3"}],["path",{"d":"M3 5V19A9 3 0 0 0 21 19V5"}],["path",{"d":"M3 12A9 3 0 0 0 21 12"}]],"sparkles":[["path",{"d":"M11.017 2.814a1 1 0 0 1 1.966 0l1.051 5.558a2 2 0 0 0 1.594 1.594l5.558 1.051a1 1 0 0 1 0 1.966l-5.558 1.051a2 2 0 0 0-1.594 1.594l-1.051 5.558a1 1 0 0 1-1.966 0l-1.051-5.558a2 2 0 0 0-1.594-1.594l-5.558-1.051a1 1 0 0 1 0-1.966l5.558-1.051a2 2 0 0 0 1.594-1.594z"}],["path",{"d":"M20 2v4"}],["path",{"d":"M22 4h-4"}],["circle",{"cx":"4","cy":"20","r":"2"}]],"link":[["path",{"d":"M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"}],["path",{"d":"M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"}]],"users":[["path",{"d":"M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"}],["path",{"d":"M16 3.128a4 4 0 0 1 0 7.744"}],["path",{"d":"M22 21v-2a4 4 0 0 0-3-3.87"}],["circle",{"cx":"9","cy":"7","r":"4"}]],"layout-dashboard":[["rect",{"width":"7","height":"9","x":"3","y":"3","rx":"1"}],["rect",{"width":"7","height":"5","x":"14","y":"3","rx":"1"}],["rect",{"width":"7","height":"9","x":"14","y":"12","rx":"1"}],["rect",{"width":"7","height":"5","x":"3","y":"16","rx":"1"}]],"store":[["path",{"d":"M15 21v-5a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v5"}],["path",{"d":"M17.774 10.31a1.12 1.12 0 0 0-1.549 0 2.5 2.5 0 0 1-3.451 0 1.12 1.12 0 0 0-1.548 0 2.5 2.5 0 0 1-3.452 0 1.12 1.12 0 0 0-1.549 0 2.5 2.5 0 0 1-3.77-3.248l2.889-4.184A2 2 0 0 1 7 2h10a2 2 0 0 1 1.653.873l2.895 4.192a2.5 2.5 0 0 1-3.774 3.244"}],["path",{"d":"M4 10.95V19a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8.05"}]],"puzzle":[["path",{"d":"M15.39 4.39a1 1 0 0 0 1.68-.474 2.5 2.5 0 1 1 3.014 3.015 1 1 0 0 0-.474 1.68l1.683 1.682a2.414 2.414 0 0 1 0 3.414L19.61 15.39a1 1 0 0 1-1.68-.474 2.5 2.5 0 1 0-3.014 3.015 1 1 0 0 1 .474 1.68l-1.683 1.682a2.414 2.414 0 0 1-3.414 0L8.61 19.61a1 1 0 0 0-1.68.474 2.5 2.5 0 1 1-3.014-3.015 1 1 0 0 0 .474-1.68l-1.683-1.682a2.414 2.414 0 0 1 0-3.414L4.39 8.61a1 1 0 0 1 1.68.474 2.5 2.5 0 1 0 3.014-3.015 1 1 0 0 1-.474-1.68l1.683-1.682a2.414 2.414 0 0 1 3.414 0z"}]],"file-text":[["path",{"d":"M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"}],["path",{"d":"M14 2v5a1 1 0 0 0 1 1h5"}],["path",{"d":"M10 9H8"}],["path",{"d":"M16 13H8"}],["path",{"d":"M16 17H8"}]],"rotate-cw":[["path",{"d":"M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"}],["path",{"d":"M21 3v5h-5"}]],"wand-sparkles":[["path",{"d":"m21.64 3.64-1.28-1.28a1.21 1.21 0 0 0-1.72 0L2.36 18.64a1.21 1.21 0 0 0 0 1.72l1.28 1.28a1.2 1.2 0 0 0 1.72 0L21.64 5.36a1.2 1.2 0 0 0 0-1.72"}],["path",{"d":"m14 7 3 3"}],["path",{"d":"M5 6v4"}],["path",{"d":"M19 14v4"}],["path",{"d":"M10 2v2"}],["path",{"d":"M7 8H3"}],["path",{"d":"M21 16h-4"}],["path",{"d":"M11 3H9"}]],"key-round":[["path",{"d":"M2.586 17.414A2 2 0 0 0 2 18.828V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.172a2 2 0 0 0 1.414-.586l.814-.814a6.5 6.5 0 1 0-4-4z"}],["circle",{"cx":"16.5","cy":"7.5","r":".5","fill":"currentColor"}]],"earth":[["path",{"d":"M21.54 15H17a2 2 0 0 0-2 2v4.54"}],["path",{"d":"M7 3.34V5a3 3 0 0 0 3 3a2 2 0 0 1 2 2c0 1.1.9 2 2 2a2 2 0 0 0 2-2c0-1.1.9-2 2-2h3.17"}],["path",{"d":"M11 21.95V18a2 2 0 0 0-2-2a2 2 0 0 1-2-2v-1a2 2 0 0 0-2-2H2.05"}],["circle",{"cx":"12","cy":"12","r":"10"}]],"cpu":[["path",{"d":"M2 17h2"}],["path",{"d":"M2 7h2"}],["path",{"d":"M20 12h2"}],["path",{"d":"M20 17h2"}],["path",{"d":"M20 7h2"}],["path",{"d":"M7 20v2"}],["path",{"d":"M7 2v2"}],["rect",{"x":"4","y":"4","width":"16","height":"16","rx":"2"}],["rect",{"x":"8","y":"8","width":"8","height":"8","rx":"1"}]],"chevrons-up-down":[["path",{"d":"m7 15 5 5 5-5"}],["path",{"d":"m7 9 5-5 5 5"}]],"maximize":[["path",{"d":"M8 3H5a2 2 0 0 0-2 2v3"}],["path",{"d":"M21 8V5a2 2 0 0 0-2-2h-3"}],["path",{"d":"M3 16v3a2 2 0 0 0 2 2h3"}],["path",{"d":"M16 21h3a2 2 0 0 0 2-2v-3"}]],"minimize":[["path",{"d":"M8 3v3a2 2 0 0 1-2 2H3"}],["path",{"d":"M21 8h-3a2 2 0 0 1-2-2V3"}],["path",{"d":"M3 16h3a2 2 0 0 1 2 2v3"}],["path",{"d":"M16 21v-3a2 2 0 0 1 2-2h3"}]]};
function moIcon(name,size){
  const arr=MO_ICONS[name]||[];
  let inner='';
  for(let i=0;i<arr.length;i++){
    const sh=arr[i];let at='';
    for(const k in sh[1]){at+=' '+k+'="'+sh[1][k]+'"'}
    inner+='<'+sh[0]+at+'/>';
  }
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:'+size+'px;height:'+size+'px" aria-hidden="true">'+inner+'</svg>';
}
let MO={events:[],sessions:{},stages:{},sel:null,connTxt:'',sseId:null,pollTimer:null,started:false,lastEventId:0,renderTimer:null,expanded:{},gotOpen:false,autoScroll:true,reasoning:null,reasonBuilt:null,page:'tl',full:false,rr:null,
        collapsed:(function(){try{return localStorage.getItem('maisaka-monitor-sidebar-collapsed')!=='false'}catch(e){return false}})()};
/* ---- 工具函数：照抄部署 chunk（Pe/fe/P/zt/Dt/Le/Ot） ---- */
function moFmtMs(ms){ms=ms||0;return ms<1000?Math.round(ms)+'ms':(ms/1000).toFixed(2)+'s'}
function moFmtTs(ts){const d=new Date((ts||0)*1000);const z=n=>String(n).padStart(2,'0');return z(d.getHours())+':'+z(d.getMinutes())+':'+z(d.getSeconds())}
function moRelTime(ts){const diff=Date.now()/1000-(ts||0);if(diff<10)return '刚刚';if(diff<60)return Math.round(diff)+'秒前';if(diff<3600)return Math.round(diff/60)+'分钟前';return Math.round(diff/3600)+'小时前'}
function moAgentLabel(s){const v=String(s==null?'':s).trim();const lv=v.toLowerCase();if(!lv||lv==='stop')return null;if(lv==='running')return '运行中';if(lv==='wait')return '等待中';return v}
function moIsWaiting(st){return st&&(st.stage==='等待消息'||String(st.detail||'').indexOf('等待消息')>=0||st.agent_state==='wait')}
function moInitial(name,fallback){const v=String(name==null?'':name).trim();return v?v.slice(0,1):fallback}
/* ---- 事件入口 ---- */
function moTouchSession(sid,ts){
  const s=MO.sessions[sid];
  if(s){s.last=ts||s.last||Date.now()/1000}
}
function moIngest(ev){
  if(!ev||!ev.event)return;
  const d=ev.data||{};
  const sid=d.session_id,ts=d.timestamp||Date.now()/1000;
  if(ev.event==='stream.open'){MO.gotOpen=true;MO.connTxt='已连接';moRenderConn();return}
  /* 按 event_id 去重提前（v6.20.3）：SSE 与轮询双通道重复投递同一事件时，
     session.start 若先重建会话卡再去重，其 count 会被重置为 0；
     stage.* 无 event_id 不受影响 */
  if(d.event_id){
    MO.seenIds=MO.seenIds||new Set();
    if(MO.seenIds.has(d.event_id))return;
    MO.seenIds.add(d.event_id);
  }
  if(ev.event==='stage.status'){MO.stages[sid]=d;moTouchSession(sid,ts);moSchedule();return}
  if(ev.event==='stage.removed'){delete MO.stages[sid];moTouchSession(sid,ts);moSchedule();return}
  if(ev.event==='session.start'){
    MO.sessions[sid]={name:d.session_name,platform:d.platform,is_group:d.is_group_chat,count:0,last:ts};
    if(MO.sel===null)MO.sel=sid;  // 部署版：首个出现的会话自动选中
    moSchedule();return;
  }
  if(!MO.sessions[sid])MO.sessions[sid]={name:sid,platform:'',is_group:false,count:0,last:ts};
  const s=MO.sessions[sid];
  s.count=(s.count||0)+1;s.last=ts;
  MO.events.push({id:d.event_id||('t'+Date.now()+Math.random()),type:ev.event,data:d,ts:ts,sid:sid});
  if(MO.events.length>2000)MO.events=MO.events.slice(-1500);
  if(d.event_id)MO.lastEventId=Math.max(MO.lastEventId,d.event_id);
  moSchedule();
}
function moSchedule(){
  if(MO.renderTimer)return;
  MO.renderTimer=setTimeout(()=>{MO.renderTimer=null;moRenderAll()},150);
}
/* ---- 页面骨架（部署版主布局：aside 聊天流 + 主列 阶段条+时间线 Card） ---- */
function moAsideHeadHTML(){
  const conn=MO.gotOpen||MO.pollTimer;
  return `<h2 class="mo-aside-title">
    <span class="mo-ticon">${moIcon('activity',16)}</span>聊天流
    ${conn?`<span class="mo-conn" title="${esc(MO.connTxt||'已连接')}"></span>`:''}
    <button class="mo-gbtn icon" onclick="moToggleFull()" title="${MO.full?'退出全屏 (Esc)':'全屏——铺满整个页面 (Esc 退出)'}">${moIcon(MO.full?'minimize':'maximize',14)}</button>
    <button class="mo-gbtn icon" onclick="MO.collapsed=!MO.collapsed;try{localStorage.setItem('maisaka-monitor-sidebar-collapsed',String(MO.collapsed))}catch(e){};moRefreshAside()" title="${MO.collapsed?'展开侧边栏':'折叠侧边栏'}">${moIcon(MO.collapsed?'chevron-right':'chevron-left',14)}</button>
  </h2>`;
}
function observeHTML(){
  return `<div class="mo-root${MO.full?' fullscreen':''}">
    <aside class="mo-aside ${MO.collapsed?'collapsed':''}" id="mo_aside">
      <div class="mo-aside-head">${moAsideHeadHTML()}</div>
      <div class="mo-sep"></div>
      <div class="mo-sessions" id="mo_sessions"></div>
      <div class="mo-aside-foot" id="mo_aside_foot"></div>
    </aside>
    <div class="mo-main">
      <div id="mo_stagebar"></div>
      <div class="mo-tlcard"><div class="mo-tl" id="mo_tl" onscroll="moOnScroll(this)"><div class="mo-tlin" id="mo_tlin"></div></div></div>
    </div>
  </div>`
}
function moRefreshAside(){
  const a=document.getElementById('mo_aside');if(!a)return;
  a.classList.toggle('collapsed',MO.collapsed);
  const h=a.querySelector('.mo-aside-head');if(h)h.innerHTML=moAsideHeadHTML();
  moRenderSide();
}
/* 全屏切换：fixed 盖住插件页 chrome；若浏览器允许 iframe 全屏（allowfullscreen）
   则再借 Fullscreen API 铺满整块屏幕，不允许就静默保持视口内全屏 */
function moToggleFull(){
  MO.full=!MO.full;
  const root=document.querySelector('.mo-root');
  if(root)root.classList.toggle('fullscreen',MO.full);
  if(MO.full){try{document.documentElement.requestFullscreen&&document.documentElement.requestFullscreen()}catch(e){}}
  else if(document.fullscreenElement){try{document.exitFullscreen()}catch(e){}}
  moRefreshAside();
}
function moRenderConn(){
  const a=document.getElementById('mo_aside');
  if(!a)return;
  const h=a.querySelector('.mo-aside-head');
  if(h&&((MO.gotOpen||MO.pollTimer)!==MO._connDrawn)){h.innerHTML=moAsideHeadHTML();MO._connDrawn=(MO.gotOpen||MO.pollTimer)}
}
/* ---- 会话侧栏（部署版 SessionSidebar：rounded-md 头像+状态点+事件数+相对时间+当前阶段） ---- */
/* 侧栏底座：整页推理视图时给「返回监控」出口（按钮在宿主层，不占推理页内部） */
function moRenderAsideFoot(){
  const foot=document.getElementById('mo_aside_foot');if(!foot)return;
  const want=MO.page==='reason';
  if(foot._drawn===want)return;  /* 没变化不重画，避免打断按钮 hover */
  foot._drawn=want;
  foot.innerHTML=want
    ?`<button class="mo-gbtn" onclick="moReasonBack()" title="返回监控时间线">${moIcon('arrow-left',14)}<span>返回监控</span></button>`
    :'';
}
function moRenderSide(){
  const host=document.getElementById('mo_sessions');if(!host)return;
  moRenderAsideFoot();
  const ids=Object.keys(MO.sessions).sort((a,b)=>(MO.sessions[b].last||0)-(MO.sessions[a].last||0));
  if(!ids.length){host.innerHTML=`<div class="mo-sess-empty"><span style="opacity:.4;display:flex">${moIcon('bot',32)}</span><p>等待 MaiSaka 会话…</p></div>`;return}
  host.innerHTML=ids.map(sid=>{
    const s=MO.sessions[sid],st=MO.stages[sid];
    const dot=st?`<span class="mo-sdot ${moIsWaiting(st)?'wait':''}"></span>`:'';
    return `<button class="mo-sess ${MO.sel===sid?'on':''}" onclick="moSel('${jsq(sid)}')" title="${esc(s.name||sid)}">
      <div class="r1"><div class="lft">
        <span class="mo-sava"><span class="img">${esc(moInitial(s.name,s.is_group?'群':'私'))}</span>${dot}</span>
        <span class="mo-sname">${esc(s.name||sid)}</span>
      </div><span class="mo-badge secondary h16">${s.count||0}</span></div>
      <div class="r2"><span class="tm">${moRelTime(s.last)}</span>${st?`<span class="stg">${esc(st.stage||'')}</span>`:''}</div>
    </button>`}).join('');
}
function moSel(sid){MO.sel=sid;moRenderAll()}
/* ---- 统计（部署版 reducer：ingested+sent→消息；finalized→循环+工具数；只统计当前选中会话） ---- */
function moCurEvents(){return MO.sel===null?MO.events:MO.events.filter(e=>e.sid===MO.sel)}
function moStats(){
  let messages=0,cycles=0,toolCalls=0;
  moCurEvents().forEach(e=>{
    if(e.type==='message.ingested'||e.type==='message.sent')messages++;
    else if(e.type==='planner.finalized'){cycles++;toolCalls+=(e.data.tools||[]).length}
  });
  return {messages,cycles,toolCalls};
}
/* ---- 阶段状态条（部署版 StageStatusPanel：统计chip+回到底部/清空 居中，阶段徽章组，更新于 ml-auto） ---- */
function moRenderStagebar(){
  const host=document.getElementById('mo_stagebar');if(!host)return;
  const st=MO.sel!==null?MO.stages[MO.sel]:null;
  const s=moStats();
  const bar=`<div class="mo-stats" title="消息：${s.messages}&#10;循环：${s.cycles}&#10;工具调用：${s.toolCalls}">${moIcon('activity',12)}<span class="lbl">统计</span></div>
    <div class="mo-act">
      <button class="mo-gbtn" onclick="moToBottom()" title="回到底部"><span class="chev ${MO.autoScroll?'on':''}" style="display:flex;margin-right:4px">${moIcon('chevron-down',12)}</span>回到底部</button>
      <button class="mo-gbtn icon" onclick="moClear()" title="清空" aria-label="清空">${moIcon('eraser',12)}</button>
    </div>`;
  if(!st){
    host.innerHTML=`<div class="mo-stagebar empty">${bar}<span class="mo-emptytxt">当前聊天流暂无阶段状态</span></div>`;
    return;
  }
  const ag=moAgentLabel(st.agent_state);
  host.innerHTML=`<div class="mo-stagebar">
    ${bar}
    <div class="mo-stagegrp">
      <span class="mo-badge default g05 p6 t10">${moIcon('activity',10)}${esc(st.stage||'未知阶段')}</span>
      ${st.round_text?`<span class="mo-badge secondary p6 t10">${esc(st.round_text)}</span>`:''}
      ${ag?`<span class="mo-badge ${String(st.agent_state||'').toLowerCase()==='running'?'default':'outline'} p6 t10">${esc(ag)}</span>`:''}
      <span class="mo-upd">更新于 ${moRelTime(st.updated_at||st.timestamp)}</span>
    </div>
    ${st.detail?`<p class="mo-sdetail">${esc(st.detail)}</p>`:''}
  </div>`;
}
/* ---- 折叠文本（部署版 CollapsibleText：超 N 行折叠为空格拼接，chevron 图标，文字色 primary） ---- */
function moCollapsible(text,maxLines,key,cls){
  if(!text)return '';
  const t=String(text);
  /* key 进 onclick 的 JS 字符串上下文必须走 jsq（v6.20.3）：esc 是 HTML
     转义不处理单引号，key 含 ' 即可逃出字符串注入脚本 */
  const k=jsq(String(key));
  const lines=t.split('\n');
  if(lines.length<=maxLines)return `<p class="mo-cpt ${cls||''}">${esc(t)}</p>`;
  if(MO.expanded[key])return `<div><p class="mo-cpt ${cls||''}">${esc(t)}</p>
    <button class="mo-cbtn" onclick="MO.expanded['${k}']=false;moRenderTimeline()">${moIcon('chevron-down',12)} 收起</button></div>`;
  return `<div><p class="mo-cpt ${cls||''}">${esc(lines.slice(0,maxLines).join(' '))}</p>
    <button class="mo-cbtn" onclick="MO.expanded['${k}']=true;moRenderTimeline()">${moIcon('chevron-right',12)} 展开全部 (${lines.length} 行)</button></div>`;
}
/* ---- 参数 chip（部署版 ToolArgumentBlock：h-6 圆角6 border，mono name = value） ---- */
function moArgChips(args){
  const entries=Object.entries(args||{});
  if(!entries.length)return '';
  return entries.map(([k,v])=>{
    const raw=typeof v==='string'?v:(v===undefined?'undefined':JSON.stringify(v,null,2));
    const tp=Array.isArray(v)?'array('+v.length+')':(v===null?'null':typeof v);
    const inline=String(raw).replace(/\s+/g,' ');
    return `<span class="mo-arg" title="${esc(k+' ('+tp+'): '+raw)}"><b>${esc(k)}</b><span class="eq">=</span><i>${esc(inline)}</i></span>`;
  }).join('');
}
function moTypeOf(v){return Array.isArray(v)?'array('+v.length+')':(v===null?'null':typeof v)}
/* ---- 单行工具摘要：默认时间线只显示结果概况，明细收进「推理过程」弹窗 ---- */
function moToolMini(x,i){
  const ok=x.success===true;
  return `<div class="mo-trow">
    <div class="mo-thead">
      <span class="mo-tname">${esc(x.tool_name||'unknown')}</span>
      <span class="mo-ic ${ok?'mo-okc':'mo-badc'}">${moIcon(ok?'circle-check':'user',14)}</span>
      <span class="mo-badge ${ok?'secondary':'destructive'} h20">${ok?'执行成功':'执行失败'}</span>
      ${(x.duration_ms||0)>0?`<span class="mo-ets md">${moFmtMs(x.duration_ms)}</span>`:''}
      <span class="mo-tidx">#${i+1}</span>
    </div>
  </div>`;}
/* ---- 时间线卡片（部署版 TimelineEventRenderer switch，未列事件不渲染） ---- */
function moToolRow(x,i){
  const ok=x.success===true;
  return `<div class="mo-trow">
    <div class="mo-thead">
      <span class="mo-tname">${esc(x.tool_name||'unknown')}</span>
      <span class="mo-ic ${ok?'mo-okc':'mo-badc'}">${moIcon(ok?'circle-check':'user',14)}</span>
      <span class="mo-badge ${ok?'secondary':'destructive'} h20">${ok?'执行成功':'执行失败'}</span>
      ${(x.duration_ms||0)>0?`<span class="mo-ets md">${moFmtMs(x.duration_ms)}</span>`:''}
      <span class="mo-tidx">#${i+1}</span>
    </div>
    <div class="mo-args">${moArgChips(x.tool_args)}
      <details class="mo-json"><summary title="完整调用 JSON">${moIcon('chevron-right',10)}<span>JSON</span></summary>
        <pre>${esc(JSON.stringify({tool_call_id:x.tool_call_id,tool_name:x.tool_name,tool_args:x.tool_args||{},success:x.success,duration_ms:x.duration_ms,summary:x.summary,tool_call_source:x.tool_call_source,tool_call_source_label:x.tool_call_source_label},null,2))}</pre></details>
    </div>
    <div class="mo-result"><span>执行结果</span><div class="mo-resc">${moCollapsible(x.summary||'',6,'res_'+x.tool_call_id,'mo-resp')}</div></div>
  </div>`;
}
function moDoneStrip(){
  return `<div class="mo-done-strip"><span class="mo-ic mo-okc">${moIcon('circle-check',14)}</span><b>本轮思考暂时结束</b><span class="m">等待新的消息。</span></div>`;
}
function moCard(e,waitChain){
  const d=e.data,ts=`<span class="mo-ets">${moFmtTs(e.ts)}</span>`;
  if(e.type==='message.ingested')
    return `<div class="mo-e"><div class="mo-ein"><div class="mo-row">
      <span class="mo-ava in">${esc(moInitial(d.speaker_name,'人'))}</span>
      <div class="mo-ecol"><div class="mo-ehead"><span class="mo-ename">${esc(d.speaker_name||'')}</span>${ts}</div>
      <p class="mo-cpt">${esc(d.content||'[空消息]')}</p></div></div></div></div>`;
  if(e.type==='message.sent')
    return `<div class="mo-e"><div class="mo-ein"><div class="mo-card-sent">
      <span class="mo-ava out">${moIcon('bot',14)}</span>
      <div class="mo-ecol"><div class="mo-ehead"><span class="mo-ename">${esc(d.speaker_name||'麦麦')}</span><span class="mo-badge outline t10">已发送</span>${ts}</div>
      <p class="mo-cpt">${esc(d.content||'[非文本消息]')}</p></div></div></div></div>`;
  if(e.type==='timing_gate.result'){
    const cfgs={continue:{label:'继续执行',variant:'default',icon:'arrow-right'},wait:{label:'等待',variant:'secondary',icon:'circle-pause'},no_action:{label:'不回复',variant:'destructive',icon:'user'}};
    const cfg=cfgs[d.action]||cfgs.continue;
    return `<div class="mo-e"><div class="mo-ein"><div class="mo-card-gate">
      <span class="mo-ava amber">${moIcon('timer',14)}</span>
      <div class="mo-ecol"><div class="mo-ehead wrap"><span class="mo-ename">反应</span><span class="mo-badge outline t10">react</span>
        <span class="mo-badge ${cfg.variant} g05 t10">${moIcon(cfg.icon,10)}${cfg.label}</span><span class="mo-ets">${moFmtMs(d.duration_ms)}</span></div>
      ${d.content?moCollapsible(d.content,3,'tg'+e.id):''}</div></div></div></div>`;
  }
  if(e.type==='planner.response'){
    const chips=(d.tool_calls||[]).map(c=>`<span class="mo-badge secondary g1 t10">${moIcon('wrench',10)}${esc(c.name)}</span>`).join('');
    return `<div class="mo-e"><div class="mo-ein"><div class="mo-row">
      <span class="mo-ava out">${moIcon('brain',14)}</span>
      <div class="mo-ecol"><div class="mo-ehead wrap"><span class="mo-ename">规划器思考</span>
        <span class="mo-ets">${moFmtMs(d.duration_ms)}</span></div>
      ${d.content?moCollapsible(d.content,6,'pr'+e.id):''}${chips?`<div class="mo-chips">${chips}</div>`:''}</div></div></div></div>`;
  }
  if(e.type==='planner.finalized'){
    const p=d.planner||{},r=d.request||{},tools=d.tools||[];
    /* 部署版 Fe：interrupted===true 即打断卡 */
    if(d.interrupted===true){
      return `<div class="mo-e"><div class="mo-ein"><div class="mo-card-warn">
        <div class="hd"><span class="mo-ic mo-amberc">${moIcon('circle-alert',16)}</span><b>Planner 被新消息打断</b>
          <span class="mo-badge outline t10" style="margin-left:auto">#${d.cycle_id==null?'':d.cycle_id}</span>
          ${(p.duration_ms||0)>0?`<span class="mo-ets">${moFmtMs(p.duration_ms)}</span>`:''}</div>
        <p class="mo-cpt2">${esc(p.content||'收到新消息，已停止当前思考并准备重新决策。')}</p>
      </div></div></div>`;
    }
    /* 部署版：timing_gate.result.action==='no_action' 时 finalized 不渲染 */
    if(d.timing_gate&&d.timing_gate.result&&d.timing_gate.result.action==='no_action')return '';
    const hasReq=r&&(r.selected_history_count!=null||r.tool_count!=null);
    /* 部署版：token>0 时 outline 徽章「输入+输出 tokens」（planner.prompt_tokens/completion_tokens） */
    const hasTok=p&&((p.prompt_tokens||0)>0||(p.completion_tokens||0)>0);
    const planCard=`<div class="mo-card plan"><div class="mo-cbody">
      <div class="mo-chead"><span class="mo-ic mo-okc">${moIcon('brain',16)}</span><span class="mo-ctitle">Planner</span>
        ${rrRoundsOf(e).length?`<button class="mo-rbtn" title="在新页面查看本轮完整工作流（推理过程）" onclick="MO.page='reason';MO.reasoning=${e.id};moRenderTimeline();window.scrollTo(0,0)">${moIcon('file-text',12)} 推理过程</button>`:''}
        ${p.model_name?`<span class="mo-badge outline t10 mono" title="本次 Planner 调用的模型${(p.model_name.includes(' / ')?'（多轮多模型去重并列）':'')}">${esc(p.model_name)}</span>`:''}
        <span class="mo-badge outline fnorm" style="margin-left:auto">${moFmtMs(p.duration_ms)}</span>
        ${hasReq?`<span class="mo-badge secondary t10">上下文 ${r.selected_history_count==null?0:r.selected_history_count} 条 / 可用工具 ${r.tool_count==null?0:r.tool_count}</span>`:''}
        ${hasTok?`<span class="mo-badge outline t10">${p.prompt_tokens||0}+${p.completion_tokens||0} tokens</span>`:''}
      </div>
      ${p.content?moCollapsible(p.content,6,'pf'+e.id,'f90'):'<p class="mo-cpt2">planner 本轮没有文本内容</p>'}
    </div></div>`;
    /* 部署版 rs：finish 工具单独渲染为结束条（maisoul 的 wait 决策即 finish 语义） */
    const isFinish=x=>String(x.tool_name||'').trim().toLowerCase()==='finish'||String(x.tool_name||'').trim().toLowerCase()==='wait';
    const finishLike=tools.filter(isFinish),normal=tools.filter(x=>!isFinish(x));
    let toolsBlock='';
    if(!normal.length&&finishLike.length){
      toolsBlock=`<div class="mo-card-done"><span class="mo-ic mo-okc">${moIcon('circle-check',16)}</span><b>本轮思考暂时结束</b><span class="m">等待新的消息。</span>${(waitChain||1)>1?`<span class="mo-badge secondary t10">等待续轮 ×${waitChain}</span>`:''}</div>`;
    }else if(normal.length){
      toolsBlock=`<div class="mo-card tools"><div class="mo-cbody g8">
        <div class="mo-chead nowrap"><span class="mo-ic mo-tealc">${moIcon('wrench',16)}</span><span class="mo-ctitle">使用工具</span>
          <span class="mo-badge secondary t10" style="margin-left:auto">${normal.length} 个</span></div>
        ${normal.map((x,i)=>moToolMini(x,i)).join('')}
        ${finishLike.length?moDoneStrip():''}
      </div></div>`;
    }
    return `<div class="mo-e"><div class="mo-ein" style="display:flex;flex-direction:column;gap:8px">${planCard}${toolsBlock}</div></div>`;
  }
  if(e.type==='replier.response'){
    return `<div class="mo-e"><div class="mo-ein"><div class="mo-card replier"><div class="mo-cbody rp">
      <div class="mo-chead nowrap"><span class="mo-ic mo-purplec">${moIcon('bot',16)}</span><span class="mo-ctitle">回复器响应</span>
        <span class="mo-badge outline fnorm" style="margin-left:auto">${moFmtMs(d.duration_ms)}</span>
        ${d.success?`<span class="mo-badge secondary g1">${moIcon('circle-check',12)} 成功</span>`:`<span class="mo-badge destructive g1">${moIcon('user',12)} 失败</span>`}
        <span class="mo-ets">${moFmtTs(d.timestamp||e.ts)}</span></div>
      ${d.content?moCollapsible(d.content,6,'rr'+e.id,'f90'):''}
      ${d.reasoning?`<details class="mo-details"><summary>思考过程</summary>${moCollapsible(d.reasoning,8,'rrr'+e.id,'mut')}</details>`:''}
    </div></div></div></div>`;
  }
  /* 部署版 default: return null —— 其余事件类型不渲染 */
  return '';
}
function moRenderTimeline(){
  const tl=document.getElementById('mo_tlin');if(!tl)return;
  moRenderAsideFoot();  /* 返回监控出口要跟着视图切换即时出现/消失，不能等下一次全量渲染 */
  /* 推理过程整页视图（对齐 MaiBot 的推理页：点击后整页展示，返回回时间线） */
  if(MO.page==='reason'){
    const ev=MO.events.find(x=>x.id===MO.reasoning);
    if(!ev||ev.type!=='planner.finalized'){MO.page='tl';MO.reasoning=null}
    else{
      /* 新事件到达会触发重渲染：同一轮已在展示就直接跳过，避免 srcdoc 重建白闪 + 滚动位置丢失 */
      if(MO.reasonBuilt===MO.reasoning&&document.getElementById('mo_reason_frame'))return;
      MO.reasonBuilt=MO.reasoning;
      tl.classList.add('mo-reasonhost');
      const sc=document.getElementById('mo_tl');if(sc)sc.classList.add('mo-reasonhost');
      /* 部署版推理页无聊天流侧栏：整页让位给左记录列表+右详情（返回监控后恢复） */
      const root=document.querySelector('.mo-root');if(root)root.classList.add('mo-reasonpage');
      tl.innerHTML=moReasoningPage(ev);moReasonFill(ev);return
    }
  }
  tl.classList.remove('mo-reasonhost');
  const sc2=document.getElementById('mo_tl');if(sc2)sc2.classList.remove('mo-reasonhost');
  const root2=document.querySelector('.mo-root');if(root2)root2.classList.remove('mo-reasonpage');
  MO.reasonBuilt=null;
  /* 时间线只显示当前选中会话的事件（部署版语义：点哪个聊天流看哪个） */
  const list=moCurEvents().slice(-400);
  /* wait 续轮链合并：连续的「纯 wait 决策」finalized（到期自动续轮产生）
     只渲染第一张，计数进 ×N 徽章——回复后不再连排多张思考结束卡 */
  const isWaitChain=x=>x&&x.type==='planner.finalized'
    &&(x.data&&x.data.final_state&&x.data.final_state.end_reason)==='wait'
    &&(x.data.tools||[]).length>0
    &&(x.data.tools||[]).every(t=>['finish','wait'].includes(String(t.tool_name||'').trim().toLowerCase()));
  const items=[];
  list.forEach(e=>{
    if(isWaitChain(e)){
      const prev=items[items.length-1];
      if(prev&&prev.waitChain){prev.waitChain++;return}
      items.push({e,waitChain:1});
    }else items.push({e});
  });
  tl.innerHTML=(items.length?items.map(it=>moCard(it.e,it.waitChain||1)).join('')
    :`<div class="mo-empty"><span style="opacity:.3;display:flex">${moIcon('clock',40)}</span><p>等待 MaiSaka 推理事件…</p><small>当 MaiSaka 处理新消息时，推理过程会实时展示在这里</small></div>`);
  if(MO.autoScroll){const box=document.getElementById('mo_tl');if(box)box.scrollTop=box.scrollHeight}
}
function moReasoningPage(ev){
  return `<iframe id="mo_reason_frame" title="推理过程" style="width:100%;height:100%;border:0;display:block;background:hsl(var(--background))"></iframe>`;
}
function moReasonFill(ev){
  const f=document.getElementById('mo_reason_frame');
  if(f)f.srcdoc=moBuildReasonDoc(ev);
}
/* srcdoc 与宿主同源：iframe 内控件经 parent 调宿主（返回监控/分页/筛选/记录展开） */
function moReasonBack(){MO.page='tl';MO.reasoning=null;delete MO.rrStage;moRenderAll()}
function moReasonStage(v){
  /* 类型切换（v6.20.0）：'planner'/'replyer'，null=类型选择视图。
     视图级切换置空 reasonBuilt 强制重建 srcdoc（增量协议只服务列表/详情） */
  MO.rrStage=(v==='planner'||v==='replyer')?v:null;
  MO.reasonBuilt=null;
  moRenderTimeline();
}
/* ---- 推理过程页：逐像素复刻部署版 ReasoningLogViewerPage（logs-D_9unzIh.js）
   的渲染产物——同类名 DOM + 原版 CSS（mbrc.css.js 的 MBRC_CSS，即 maibot_dashboard
   dist index-gQUKboHR.css，JetBrains 字体 base64），数据来自 maisoul 的
   planner.finalized 事件（request.system_prompt/messages[].tool_calls/
   tool_call_id 为 maisoul 扩展字段）。颜色映射（部署版实抓）：system=cyan、
   他人消息=emerald、自发消息=orange、reasoning=indigo、工具调用=fuchsia、
   工具结果=violet、assistant=amber。详情按部署版 fe 分区组件拆两区：
   「请求 Items」=产出本轮输出前的完整请求，「输出结果」=本轮模型产物
   （工具轮=该轮 assistant 消息；收尾轮=planner 思考+回复）。
   v6.15.3 全控件接真交互（owner 指令，
   头行页签/返回监控移除——返回监控移宿主聊天流侧栏底座）：类型/会话=真下拉
   筛选（moReasonSet）、上一页/下一页=真分页（RR_PAGE=20，moReasonPage）、
   记录点击=经宿主状态展开（moReasonRec） ---- */
const RR_VARS={
  system:{art:'border-cyan-300/70 bg-cyan-50/70 dark:border-cyan-700/60 dark:bg-cyan-950/25',badge:'border-cyan-400/70 bg-cyan-100/80 text-cyan-900 dark:border-cyan-700 dark:bg-cyan-950 dark:text-cyan-100',role:'system',type:'SystemMessageItem'},
  user:{art:'border-emerald-300/70 bg-emerald-50/70 dark:border-emerald-700/60 dark:bg-emerald-950/25',badge:'border-emerald-400/70 bg-emerald-100/80 text-emerald-900 dark:border-emerald-700 dark:bg-emerald-950 dark:text-emerald-100',role:'user',type:'UserMessageItem'},
  self:{art:'border-orange-300/70 bg-orange-50/75 dark:border-orange-700/60 dark:bg-orange-950/25',badge:'border-orange-400/70 bg-orange-100/85 text-orange-900 dark:border-orange-700 dark:bg-orange-950 dark:text-orange-100',role:'user',type:'UserMessageItem'},
  reasoning:{art:'border-indigo-300/70 bg-indigo-50/70 dark:border-indigo-700/60 dark:bg-indigo-950/25',badge:'border-indigo-400/70 bg-indigo-100/80 text-indigo-900 dark:border-indigo-700 dark:bg-indigo-950 dark:text-indigo-100',role:'reasoning',type:'ReasoningItem'},
  fncall:{art:'border-fuchsia-300/70 bg-fuchsia-50/70 dark:border-fuchsia-700/60 dark:bg-fuchsia-950/25',badge:'border-fuchsia-400/70 bg-fuchsia-100/80 text-fuchsia-900 dark:border-fuchsia-700 dark:bg-fuchsia-950 dark:text-fuchsia-100',role:'function call',type:'FunctionCallItem'},
  fnout:{art:'border-violet-300/70 bg-violet-50/70 dark:border-violet-700/60 dark:bg-violet-950/25',badge:'border-violet-400/70 bg-violet-100/80 text-violet-900 dark:border-violet-700 dark:bg-violet-950 dark:text-violet-100',role:'tool',type:'FunctionCallOutputItem'},
  assistant:{art:'border-amber-300/70 bg-amber-50/70 dark:border-amber-700/60 dark:bg-amber-950/25',badge:'border-amber-400/70 bg-amber-100/80 text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100',role:'assistant',type:'AssistantMessageItem'},
};
const RR_PRE='class="text-foreground text-sm leading-6 whitespace-pre-wrap" style="font-family: &quot;Microsoft YaHei UI&quot;, &quot;Microsoft YaHei&quot;, &quot;PingFang SC&quot;, &quot;Noto Sans SC&quot;, system-ui, sans-serif;"';
const RR_SVG_CHEV='<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-chevron-down h-3.5 w-3.5 shrink-0 transition-transform" aria-hidden="true"><path d="m6 9 6 6 6-6"></path></svg>';
const RR_SVG_CLOCK='<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-clock h-3.5 w-3.5 shrink-0" aria-hidden="true"><path d="M12 6v6l4 2"></path><circle cx="12" cy="12" r="10"></circle></svg>';
const RR_SVG_TIMER='<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-timer h-3.5 w-3.5 shrink-0" aria-hidden="true"><line x1="10" x2="14" y1="2" y2="2"></line><line x1="12" x2="15" y1="14" y2="11"></line><circle cx="12" cy="14" r="8"></circle></svg>';
const RR_SVG_CPU='<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-cpu h-3.5 w-3.5 shrink-0" aria-hidden="true"><path d="M12 20v2"></path><path d="M12 2v2"></path><path d="M17 20v2"></path><path d="M17 2v2"></path><path d="M2 12h2"></path><path d="M2 17h2"></path><path d="M2 7h2"></path><path d="M20 12h2"></path><path d="M20 17h2"></path><path d="M20 7h2"></path><path d="M7 20v2"></path><path d="M7 2v2"></path><rect x="4" y="4" width="16" height="16" rx="2"></rect><rect x="8" y="8" width="8" height="8" rx="1"></rect></svg>';
const RR_SVG_SEARCH='<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-search text-muted-foreground pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2" aria-hidden="true"><path d="m21 21-4.34-4.34"></path><circle cx="11" cy="11" r="8"></circle></svg>';
const RR_SVG_REFRESH='<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-refresh-cw h-4 w-4" aria-hidden="true"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"></path><path d="M21 3v5h-5"></path><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"></path><path d="M8 16H3v5"></path></svg>';
const RR_SVG_BACK='<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-arrow-left h-4 w-4" aria-hidden="true"><path d="m12 19-7-7 7-7"></path><path d="M19 12H5"></path></svg>';
function rrBadge(cls,txt,extra){return `<div data-dashboard-badge="true" class="inline-flex items-center rounded-md border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 ${cls}">${txt}</div>`}
function rrJsonBox(obj){
  /* 开合态类切换在 rrToggle（对齐部署版 Collapsible className 三元）：
     闭=lg:z-10 lg:w-48 lg:shadow-sm；开=lg:z-40（盖过后续条目的闭态盒）
     lg:w-[min(48%,46rem)] lg:shadow-xl——否则两条目相近时展开面板被后画
     的同级按钮遮住（DOM 靠后者同 z 值胜出） */
  const j=esc(typeof obj==='string'?obj:JSON.stringify(obj,null,2));
  return `<div data-state="closed" class="min-w-0 rounded-md border lg:absolute lg:top-3 lg:right-3 lg:z-10 lg:w-48 lg:shadow-sm" style="background-color: var(--retro-paper, hsl(var(--color-background)));"><button type="button" class="hover:bg-muted/50 flex w-full items-center justify-between gap-2 px-2.5 py-1.5 text-left text-xs transition-colors" onclick="rrToggle(this.parentElement)"><span>完整 Item JSON</span>${RR_SVG_CHEV}</button><div class="rr-json rr-hide min-w-0 border-t"><pre class="max-h-96 overflow-auto p-2.5 font-mono text-xs leading-5 whitespace-pre-wrap lg:max-h-[32rem]">${j}</pre></div></div>`;
}
function rrArticle(n,v,body,obj,headExtra){
  const V=RR_VARS[v];
  return `<article class="relative space-y-2 rounded-md border p-2.5 sm:p-3 ${V.art}"><div class="min-w-0 space-y-2"><div class="flex min-w-0 flex-wrap items-center gap-1.5">${rrBadge('text-foreground','#'+n)}${rrBadge('font-mono '+V.badge,V.role)}<span class="text-muted-foreground font-mono text-[11px]">${V.type}</span>${headExtra||''}</div>${body}</div>${obj!=null?rrJsonBox(obj):''}</article>`;
}
function rrUserArticle(n,selfFlag,name,time,mid,body,obj){
  const v=selfFlag?'self':'user';
  const head=`<div class="border-primary/60 border-l-2 pl-2"><div class="text-muted-foreground mb-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs"><span class="relative flex overflow-hidden rounded-full bg-background h-6 w-6 shrink-0 border"><span class="flex h-full w-full items-center justify-center rounded-full bg-muted text-[10px]">${esc((name||'?').slice(0,1))}</span></span>${rrBadge('text-foreground px-1.5 py-0 text-[11px]',esc(name||''))}<span>${esc(time||'')}</span>${mid?`<span class="max-w-full truncate" title="${esc(mid)}">msg ${esc(mid)}</span>`:''}</div><pre ${RR_PRE}>${esc(body)}</pre></div>`;
  return rrArticle(n,v,head,obj);
}
function rrPlainArticle(n,v,text,obj,headExtra){
  return rrArticle(n,v,`<pre ${RR_PRE}>${esc(text)}</pre>`,obj,headExtra);
}
function rrReasonArticle(n,text,obj){
/* 思考过程（ReasoningItem）：默认折叠——数据来自 planner.finalized 的
   request.messages[].reasoning 与 planner.reasoning（回复器思考） */
return rrArticle(n,'reasoning',`<details class="rr-think"><summary class="cursor-pointer select-none text-muted-foreground text-xs">思考过程</summary><pre ${RR_PRE} class="mt-2">${esc(text)}</pre></details>`,Object.assign({item_type:'ReasoningItem',content:text},obj||{}));
}
function rrAssistantTurn(m,next){
  /* assistant 产物内联序（部署版归一化同序）：思考 → 正文 → 工具调用 */
  const c=String(m.content==null?'':m.content),items=[];
  if(m.reasoning)items.push(rrReasonArticle(next(),m.reasoning,{item_type:'ReasoningItem',content:m.reasoning}));
  if(c.trim())items.push(rrPlainArticle(next(),'assistant',c,{item_type:'AssistantMessageItem',content:c}));
  (m.tool_calls||[]).forEach(tc=>{
    const fn=tc.function||{};
    items.push(rrArticle(next(),'fncall',
      `<div class="text-muted-foreground font-mono text-[11px]">call_id: ${esc(tc.id||'')}</div><pre class="text-foreground text-xs leading-5 whitespace-pre-wrap font-mono">${esc(typeof fn.arguments==='string'?fn.arguments:JSON.stringify(fn.arguments,null,2))}</pre>`,
      {item_type:'FunctionCallItem',name:fn.name,args:fn.arguments}));
  });
  return items;
}
function rrMsgItems(d,cut,n0){
  /* 请求 Items：system + 历史 messages；cut>=0 时截到下标 cut 前
     （工具轮把本轮输出消息让给「输出结果」分区） */
  const req=d.request||{},items=[];let n=n0||0;
  const next=()=>++n;
  if(req.system_prompt)items.push(rrPlainArticle(next(),'system',req.system_prompt,{item_type:'SystemMessageItem',content:req.system_prompt}));
  const msgs=cut==null?(req.messages||[]):(req.messages||[]).slice(0,cut);
  msgs.forEach(m=>{
    const c=String(m.content==null?'':m.content);
    if(m.role==='user'){
      const mm=c.match(/^<message ([^>]*)>\n?([\s\S]*)$/);
      if(mm){
        const at={};mm[1].replace(/(\w+)="([^"]*)"/g,(_,k,v)=>{at[k]=v});
        items.push(rrUserArticle(next(),at.is_self_message==='true',at.user||'',at.time||'',at.msg_id||'',mm[2],{item_type:'UserMessageItem',attrs:at,content:mm[2]}));
      }else items.push(rrPlainArticle(next(),'user',c,{item_type:'UserMessageItem',content:c}));
    }else if(m.role==='assistant')items.push(...rrAssistantTurn(m,next));
    else if(m.role==='tool'){
      items.push(rrArticle(next(),'fnout',
        (m.tool_call_id?`<div class="text-muted-foreground font-mono text-[11px]">call_id: ${esc(m.tool_call_id)}</div>`:'')+`<pre ${RR_PRE}>${esc(c)}</pre>`,
        {item_type:'FunctionCallOutputItem',content:c}));
    }else items.push(rrPlainArticle(next(),'user',c,{item_type:'UserMessageItem',role:m.role,content:c}));
  });
  return items;
}
function rrOutItems(d,outIdx,n0){
  /* 输出结果：工具轮=该轮 assistant 产物消息；收尾轮/旧事件=planner 思考+最终回复 */
  const items=[];let n=n0||0;
  const next=()=>++n;
  if(outIdx>=0){
    const m=((d.request||{}).messages||[])[outIdx];
    if(m&&m.role==='assistant')items.push(...rrAssistantTurn(m,next));
  }else{
    const pl=d.planner||{};
    if(pl.reasoning)items.push(rrReasonArticle(next(),pl.reasoning,{item_type:'ReasoningItem',output:true,content:pl.reasoning}));
    if(pl.content)items.push(rrPlainArticle(next(),'assistant',pl.content,{item_type:'AssistantMessageItem',output:true,content:pl.content}));
  }
  return items;
}
function rrSection(title,items,extra){
  /* 部署版 fe 分区容器：标题 + secondary 计数徽章；空分区给「没有 Items。」 */
  const cnt=rrBadge('border-transparent bg-secondary text-secondary-foreground shadow-sm',items.length+' Items');
  return `<section class="space-y-2 rounded-md border p-2.5 sm:p-3"><div class="flex flex-wrap items-center gap-2"><span class="text-sm font-semibold">${title}</span>${cnt}${extra||''}</div>${items.length?`<div class="space-y-2">${items.join('')}</div>`:`<p class="text-muted-foreground text-xs">没有 Items。</p>`}</section>`;
}
/* ---- 记录源：每轮动作一条（对齐部署版粒度）+ 会话/类型筛选 + 真分页 ----
   部署版左列 280px 记录列表：记录=一轮推理（assistant.tool_calls 消息开轮，
   即该轮输出结果），无动作收尾轮标题为空（实抓"reply 后跟空标题"即此）；
   右列 = 选中记录详情（「请求 Items」+「输出结果」两分区，对齐部署版 fe） ---- */
const RR_PAGE=20;
const RR_TYPES=[['','类型'],['tools','含工具调用'],['plain','无工具轮'],['wait','纯等待续轮']];
function rrRoundsOf(ev){
  const d=ev.data||{},msgs=((d.request||{}).messages)||[];
  /* start 指向该轮 assistant.tool_calls 消息（模型产物）；其后 tool 消息
     归下一轮的请求 Items（部署版 request/output 语义） */
  const rounds=[];
  msgs.forEach((m,i)=>{
    if(m.role==='assistant'&&Array.isArray(m.tool_calls)&&m.tool_calls.length)
      rounds.push({start:i,tools:m.tool_calls.map(tc=>((tc.function||{}).name)||'').filter(Boolean)})
  });
  let recs;
  if(rounds.length){
    recs=rounds.map((r,k)=>({key:ev.id+'_'+k,ev:ev,start:r.start,tools:r.tools,slice:true,ri:k}))
  }else{
    /* 旧事件（v6.14.3 前无 messages[].tool_calls 扩展）：按 d.tools 逐动作拆，详情给整循环 */
    const names=(d.tools||[]).map(t=>t&&t.tool_name).filter(Boolean);
    recs=names.map((n,k)=>({key:ev.id+'_t'+k,ev:ev,tools:[n],slice:false}))
  }
  if(!recs.length||((d.planner||{}).content))recs.push({key:ev.id+'_w',ev:ev,tools:[],slice:false});
  return recs
}
function rrTypeOfRec(rec){
  const ts=(rec.tools||[]).map(x=>String(x).toLowerCase());
  if(!ts.length)return 'plain';
  if(ts.every(n=>n==='finish'||n==='wait'))return 'wait';
  return 'tools'
}
function rrReplyerTraces(d){
  /* 兼容两代载荷：v6.27.1+ 的 replyers 列表（同循环多次 reply 按次全留——
     旧单槽只存最后一次，中间几次真实发送曾在本页不可见）；旧事件单键 replyer dict */
  const arr=(d&&d.replyers)||((d&&d.replyer)?[d.replyer]:[]);
  return Array.isArray(arr)?arr:[]
}
function rrRoundsOfReplyer(ev){
  /* 回复器流程：一次 finalized 的每次 reply 各一条（标题=输出预览——对齐
     部署版 replyer 记录 display_title=output_preview）；旧事件无素材不可见 */
  return rrReplyerTraces(ev.data||{}).map((t,i)=>({key:ev.id+'_rp'+i,ev:ev,rp:t,rpi:i,tools:[],slice:false}))
}
function rrFiltered(){
  const rr=MO.rr||{};
  let evs=MO.events.filter(x=>x.type==='planner.finalized');
  if(rr.sess)evs=evs.filter(x=>x.sid===rr.sess);
  /* stage 分流（对齐部署版 ?stage=planner|replyer）：replyer 只看带 replyer 块的事件 */
  const replyer=MO.rrStage==='replyer';
  if(replyer)evs=evs.filter(x=>rrReplyerTraces(x.data||{}).length);
  const recs=[];
  [...evs].reverse().forEach(ev=>{
    let rs=replyer?rrRoundsOfReplyer(ev):rrRoundsOf(ev);
    if(!replyer&&rr.type)rs=rs.filter(r=>rrTypeOfRec(r)===rr.type);
    recs.push(...rs)
  });
  return recs
}
function rrPageInfo(list){
  const pages=Math.max(1,Math.ceil(list.length/RR_PAGE));
  const page=Math.min(Math.max(1,(MO.rr&&MO.rr.page)||1),pages);
  return {total:list.length,pages,page};
}
/* 本次调用的模型（maisoul 扩展）：工具轮=该轮 assistant 消息的 model_name；
   收尾轮/旧事件=最后一条带模型的 assistant 轮，再回落整循环去重值 */
function rrModelOf(rec){
  const d=rec.ev.data||{},msgs=((d.request||{}).messages)||[];
  if(rec.slice&&rec.start!=null){const m=msgs[rec.start];if(m&&m.model_name)return String(m.model_name)}
  for(let i=msgs.length-1;i>=0;i--){const m=msgs[i];if(m&&m.role==='assistant'&&m.model_name)return String(m.model_name)}
  return String(((d.planner||{}).model_name)||'')
}
function rrRecMeta(rec){
  const d=rec.ev.data||{},p=d.planner||{};
  const ts=new Date((d.timestamp||rec.ev.ts)*1000);
  return {tstr:String(ts.getMonth()+1).padStart(2,'0')+'/'+String(ts.getDate()).padStart(2,'0')+' '+ts.toTimeString().slice(0,8),
    dur:((p.duration_ms||0)/1000).toFixed(2),tin:p.prompt_tokens||0,tout:p.completion_tokens||0,
    size:(JSON.stringify(((d.request||{}).messages)||[]).length/1024).toFixed(1),cycle:d.cycle_id,model:rrModelOf(rec)}
}
function rrRowHTML(rec,selected){
  const m=rrRecMeta(rec),names=(rec.tools||[]).join('、');
  const meta=`模型：${m.model||'maisoul planner'} · 耗时：${m.dur} s · 输入 ${m.tin.toLocaleString()} / 输出 ${m.tout.toLocaleString()} / 总计 ${(m.tin+m.tout).toLocaleString()} Token`;
  const cls='flex w-full flex-col gap-1.5 rounded-md border px-2.5 py-2 text-left text-sm transition-colors sm:gap-2 sm:px-3 '+(selected?'border-primary bg-primary/10 text-foreground selrow':'hover:border-border hover:bg-muted/60 border-transparent');
  return `<button type="button" class="${cls}" data-tools="${esc(names)}" onclick="rrSel('${jsq(rec.key)}')"><div class="flex items-start justify-between gap-2"><div class="min-w-0 flex-1"><div class="flex min-w-0 items-start gap-1.5"><div class="text-foreground line-clamp-2 min-w-0 text-sm font-medium" title="${esc(names)}">${esc(names)}</div></div></div><span class="text-muted-foreground flex shrink-0 items-center gap-1 text-xs">${RR_SVG_CLOCK}${m.tstr}</span></div><div class="text-muted-foreground flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs" title="${esc(meta)}"><span class="inline-flex min-w-0 items-center gap-1">${RR_SVG_CPU}<span class="truncate">循环 #${m.cycle==null?'-':m.cycle}</span></span><span class="inline-flex items-center gap-1">${RR_SVG_TIMER}${m.dur} s</span><span class="inline-flex items-center gap-1">输入 ${m.tin.toLocaleString()} / 输出 ${m.tout.toLocaleString()} / 总计 ${(m.tin+m.tout).toLocaleString()} Token</span><span class="shrink-0">${m.size} KB</span></div></button>`
}
function rrReplyerRowHTML(rec,selected){
  /* 回复器记录行（对齐部署版 replyer 记录：标题=输出预览，meta=模型/耗时）；
     同循环多次 reply 各一行，多行时标「第 N 次」 */
  const d=rec.ev.data||{},rp=rec.rp||d.replyer||{};
  const traces=rrReplyerTraces(d),multi=traces.length>1;
  const ts=new Date((d.timestamp||rec.ev.ts)*1000);
  const tstr=String(ts.getMonth()+1).padStart(2,'0')+'/'+String(ts.getDate()).padStart(2,'0')+' '+ts.toTimeString().slice(0,8);
  const dur=((rp.duration_ms||0)/1000).toFixed(2);
  const title=(String(rp.output||'').replace(/\s+/g,' ').trim()).slice(0,60)||'（无输出）';
  const meta=`模型：${rp.model_name||'maisoul replyer'} · 耗时：${dur} s`;
  const cls='flex w-full flex-col gap-1.5 rounded-md border px-2.5 py-2 text-left text-sm transition-colors sm:gap-2 sm:px-3 '+(selected?'border-primary bg-primary/10 text-foreground selrow':'hover:border-border hover:bg-muted/60 border-transparent');
  return `<button type="button" class="${cls}" data-tools="reply" onclick="rrSel('${jsq(rec.key)}')"><div class="flex items-start justify-between gap-2"><div class="min-w-0 flex-1"><div class="flex min-w-0 items-start gap-1.5"><div class="text-foreground line-clamp-2 min-w-0 text-sm font-medium" title="${esc(title)}">${esc(title)}</div></div></div><span class="text-muted-foreground flex shrink-0 items-center gap-1 text-xs">${RR_SVG_CLOCK}${tstr}</span></div><div class="text-muted-foreground flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs" title="${esc(meta)}"><span class="inline-flex min-w-0 items-center gap-1">${RR_SVG_CPU}<span class="truncate">循环 #${d.cycle_id==null?'-':d.cycle_id}</span></span>${multi?`<span class="inline-flex items-center gap-1">第 ${rec.rpi+1} 次</span>`:''}<span class="inline-flex items-center gap-1">${RR_SVG_TIMER}${dur} s</span>${rp.model_name?`<span class="inline-flex items-center gap-1">${RR_SVG_CPU}${esc(rp.model_name)}</span>`:''}</div></button>`
}
function rrRowsHTML(list,pi){
  return list.slice((pi.page-1)*RR_PAGE,pi.page*RR_PAGE).map(r=>r.rp?rrReplyerRowHTML(r,r.key===(MO.rr&&MO.rr.sel)):rrRowHTML(r,r.key===(MO.rr&&MO.rr.sel))).join('')
}
/* 详情两分区（对齐部署版 fe 分区组件）：请求 Items=产出本轮输出前的完整请求
   （工具轮截到输出消息前）；输出结果=本轮模型产物（工具轮=该轮 assistant
   消息；收尾轮/旧事件=planner 思考+回复）。条目编号跨分区连续 */
function rrEcoSection(eco){
  /* 生态注入区（maisoul 扩展）：final_state.eco_injection = replyer 收集的
     注入全文（心弦好感/记忆/世界书）。v6.11.6 引入、v6.14.3 推理页复刻
     部署版时被带丢（部署版页面无此区）、v6.18.2 在 master-detail 详情恢复
     （曾误记 v6.20.0——fetch_chat_history 改写注释时带入的笔误；恢复提交
     b0d3ab0 时点版本 v6.18.3，其注释与 AGENTS.md 均记 6.18.2）；
     v6.25.1 起**仅回复器流程详情渲染**——内容本就来自 replyer（planner
     决策轮零注入，坑 47），v6.19.0 独立回复器流程上线后 planner 流程侧
     同区属纯冗余（owner 决定删除）。
     折叠用原生 details——srcdoc 在 sandbox iframe 里，自定义控件回路只有
     postMessage 一条路（坑 57），原生元素零接线最稳；折叠语义对齐部署版
     CollapsibleText：≤6 行直出，超出折 6 行空格拼接预览 + 展开全部 */
  const t=String(eco||'');
  if(!t)return '';
  const lines=t.split('\n');
  const body=lines.length<=6
    ?`<pre ${RR_PRE}>${esc(t)}</pre>`
    :`<div><pre ${RR_PRE} style="margin-bottom:4px">${esc(lines.slice(0,6).join(' '))}</pre><details><summary class="cursor-pointer select-none text-xs text-primary" style="text-decoration:underline;text-underline-offset:2px"> 展开全部 (${lines.length} 行)</summary><pre ${RR_PRE} style="margin-top:4px">${esc(t)}</pre></details></div>`;
  const cnt=rrBadge('border-transparent bg-secondary text-secondary-foreground shadow-sm',t.length+' 字符');
  return `<section class="space-y-2 rounded-md border p-2.5 sm:p-3"><div class="flex flex-wrap items-center gap-2"><span class="text-sm font-semibold">生态注入</span>${cnt}</div>${body}</section>`;
}
function rrDetailHTML(rec){
  const d=rec.ev.data||{},req=d.request||{};
  /* 回复器详情：请求双段（system/user）+ 输出（思考+正文）+ 生态注入 */
  if(rec.rp){
    const rp=rec.rp||d.replyer||{};
    const ts=new Date((d.timestamp||rec.ev.ts)*1000);
    const tstr=String(ts.getMonth()+1).padStart(2,'0')+'/'+String(ts.getDate()).padStart(2,'0')+' '+ts.toTimeString().slice(0,8);
    const dur=((rp.duration_ms||0)/1000).toFixed(2);
    let n=0;const next=()=>++n;
    const reqItems=[];
    if(rp.system_prompt)reqItems.push(rrPlainArticle(next(),'system',rp.system_prompt,{item_type:'SystemMessageItem',content:rp.system_prompt}));
    if(rp.user_message)reqItems.push(rrPlainArticle(next(),'user',rp.user_message,{item_type:'UserMessageItem',content:rp.user_message}));
    const outItems=[];
    if(rp.reasoning)outItems.push(rrReasonArticle(next(),rp.reasoning,{item_type:'ReasoningItem',output:true,content:rp.reasoning}));
    if(rp.output)outItems.push(rrPlainArticle(next(),'assistant',rp.output,{item_type:'AssistantMessageItem',output:true,content:rp.output}));
    const mdlBadge=rp.model_name?rrBadge('border-transparent bg-secondary text-secondary-foreground shadow-sm font-mono','模型：'+esc(rp.model_name)):'';
    const head=`<div class="flex flex-wrap items-center gap-2 border-b px-3 py-2"><span class="text-sm font-medium">回复器</span><span class="text-muted-foreground text-xs">${tstr}</span><span class="text-muted-foreground ml-auto flex flex-wrap items-center gap-x-3 gap-y-1 text-xs"><span class="inline-flex items-center gap-1">${RR_SVG_CPU}循环 #${d.cycle_id==null?'-':d.cycle_id}</span><span class="inline-flex items-center gap-1">${RR_SVG_TIMER}${dur} s</span><span class="inline-flex items-center gap-1" title="推理过程编号（事件 id，跨会话稳定）">#${rec.ev.id}</span></span></div>`;
    return {head:head,items:rrSection('请求 Items',reqItems)+rrSection('输出 Items',outItems,mdlBadge)+rrEcoSection((d.final_state||{}).eco_injection)}
  }
  const outIdx=rec.slice&&rec.start!=null?rec.start:-1;
  const reqItems=rrMsgItems({request:req},outIdx>=0?outIdx:null,0);
  const outItems=rrOutItems(d,outIdx,reqItems.length);
  const m=rrRecMeta(rec),names=(rec.tools||[]).join('、');
  /* 输出结果分区：Items 计数徽章后标本次调用的模型（对齐部署版「模型：${model_name}」文案） */
  const mdlBadge=m.model?rrBadge('border-transparent bg-secondary text-secondary-foreground shadow-sm font-mono','模型：'+esc(m.model)):'';
  const head=`<div class="flex flex-wrap items-center gap-2 border-b px-3 py-2"><span class="text-sm font-medium">${esc(names)}</span><span class="text-muted-foreground text-xs">${m.tstr}</span><span class="text-muted-foreground ml-auto flex flex-wrap items-center gap-x-3 gap-y-1 text-xs"><span class="inline-flex items-center gap-1">${RR_SVG_CPU}循环 #${m.cycle==null?'-':m.cycle}</span><span class="inline-flex items-center gap-1">${RR_SVG_TIMER}${m.dur} s</span><span>输入 ${m.tin.toLocaleString()} / 输出 ${m.tout.toLocaleString()} / 总计 ${(m.tin+m.tout).toLocaleString()} Token</span><span class="inline-flex items-center gap-1" title="推理过程编号（事件 id，跨会话稳定）">#${rec.ev.id}${rec.ri!=null?('·R'+(rec.ri+1)):''}</span></span></div>`;
  return {head:head,items:rrSection('请求 Items',reqItems)+rrSection('输出结果',outItems,mdlBadge)}
}
function rrPagerHTML(pi){
  const cls='inline-flex cursor-pointer items-center gap-2 font-medium transition-colors disabled:pointer-events-none disabled:opacity-50 border border-input bg-background shadow-sm hover:bg-accent hover:text-accent-foreground rounded-md px-3 text-xs h-8';
  return `<button class="${cls}" ${pi.page<=1?'disabled':''} data-dashboard-button="true" onclick="rrGo(-1)">${RR_SVG_BACK}上一页</button><button class="${cls}" ${pi.page>=pi.pages?'disabled':''} data-dashboard-button="true" onclick="rrGo(1)">下一页${RR_SVG_CHEV}</button>`;
}
function rrFootTok(rec){
  if(!rec)return '';
  if(rec.rp){const rp=rec.rp||((rec.ev.data||{}).replyer)||{};return `耗时 ${((rp.duration_ms||0)/1000).toFixed(2)} s`}
  const m=rrRecMeta(rec);
  return `输入 ${m.tin.toLocaleString()} / 输出 ${m.tout.toLocaleString()} / 总计 ${(m.tin+m.tout).toLocaleString()} Token`;
}
function rrStageCards(){
  /* 类型选择视图的阶段卡数据（对齐部署版 /stages 卡片：会话数 + 最新时间）。
     无记录的阶段不渲染（部署版 Yn 分组过滤 items.length>0 同语义）；
     中文名照抄部署版 nn 映射：planner=规划器 replyer=回复器 */
  const fin=MO.events.filter(x=>x.type==='planner.finalized');
  const p2=n=>String(n).padStart(2,'0');
  const mk=(name,label,pred)=>{
    const evs=fin.filter(pred);
    if(!evs.length)return null;
    const latest=Math.max.apply(null,evs.map(x=>((x.data||{}).timestamp)||x.ts||0));
    const d=new Date(latest*1000);
    return {name:name,label:label,sessions:new Set(evs.map(x=>x.sid)).size,
      latest:latest>0?(p2(d.getMonth()+1)+'-'+p2(d.getDate())+' '+p2(d.getHours())+':'+p2(d.getMinutes())+':'+p2(d.getSeconds())):''}
  };
  return [mk('planner','规划器',()=>true),mk('replyer','回复器',x=>(x.data||{}).replyer)].filter(Boolean)
}
function moBuildReasonDoc(selEv){
  const css=MBRC_CSS;
  const styles=`<style>${css}</style><style>html,body{height:100%;margin:0}body{display:flex;flex-direction:column}.rr-hide{display:none!important}/* 开态 JSON 盒宽 lg:w-[min(48%,46rem)] 与网格类同因不在内嵌 dist 快照，按 Tailwind lg(64rem) 语义补齐；z-40/shadow-xl/max-h-[32rem] 直接用 dist 已有类 */@media(min-width:64rem){.lg\\:w-\\[min\\(48\\%\\,46rem\\)\\]{width:min(48%,46rem)}}/* 部署版左列 280px 任意值网格类不在其 dist 主 CSS；且插件页内容区≈1em 千像素，Tailwind lg(1024) 常年不触发——按 48rem 补网格+高度覆盖（模板字符串里反斜杠要写两个，单个会被转义吃掉） */
@media(min-width:48rem){.lg\\:grid-cols-\\[280px_minmax\\(0\\,1fr\\)\\]{grid-template-columns:280px minmax(0,1fr)}.lg\\:h-auto{height:auto}.lg\\:min-h-0{min-height:0}}.rr-drop{position:absolute;top:calc(100% + 4px);left:0;z-index:40;min-width:160px;background-color:hsl(var(--color-background));border:1px solid hsl(var(--color-border));border-radius:6px;box-shadow:0 4px 16px rgba(0,0,0,.18);max-height:260px;overflow-y:auto;padding:4px;display:flex;flex-direction:column;gap:1px}.rr-ditem{text-align:left;padding:6px 10px;font-size:12px;line-height:1.4;border-radius:4px;color:var(--color-foreground);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}.rr-ditem:hover{background-color:hsl(var(--color-muted))}.rr-ditem.on{background-color:hsl(var(--color-muted));font-weight:600}</style>`;
  /* 类型选择视图（对齐部署版 stage 卡片页）：主流程分组 + 阶段卡网格；
     进入推理页默认 planner 流程，点「类型」按钮到此，点卡片进对应流程 */
  if(MO.rrStage===null){
    const cards=rrStageCards();
    const card=c=>`<div class="group bg-background relative flex min-h-20 flex-col rounded-md border text-left shadow-sm transition-[border-color,background-color,box-shadow,transform] duration-150 ease-out hover:border-primary/80 hover:bg-primary/5 hover:-translate-y-0.5 hover:shadow-md"><button type="button" class="focus-visible:ring-ring flex min-h-20 flex-1 cursor-pointer flex-col justify-between rounded-md p-3 pr-10 text-left focus-visible:ring-2 focus-visible:outline-none" onclick="rrStage('${c.name}')"><div class="space-y-1.5"><div class="text-primary text-sm font-extrabold tracking-normal uppercase transition-colors sm:text-base">${c.name}</div><div class="text-foreground group-hover:text-primary text-sm font-semibold transition-colors">${c.label}</div></div><div class="text-muted-foreground group-hover:text-foreground/80 mt-2 text-xs transition-colors">${c.sessions} 个会话${c.latest?` · 最新 ${c.latest}`:''}</div></button></div>`;
    const body=cards.length
      ?`<section class="grid gap-2 sm:grid-cols-[72px_minmax(0,1fr)] sm:items-start"><div class="text-muted-foreground px-1 pt-1 text-xs font-medium sm:pt-3">主流程</div><div class="grid gap-2 sm:grid-cols-3 lg:grid-cols-4 2xl:grid-cols-5">${cards.map(card).join('')}</div></section>`
      :`<div class="text-muted-foreground px-3 py-10 text-center text-sm">没有找到推理过程类型</div>`;
    return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>推理过程</title>${styles}</head><body class="bg-background text-foreground"><main id="main-content" class="relative isolate min-h-0 flex-1 outline-none overflow-hidden bg-background" style="display:flex;flex-direction:column;height:100%;padding:8px"><div class="bg-background flex h-full min-h-0 flex-col overflow-hidden rounded-md border"><div class="flex items-center gap-2 border-b px-2 py-1.5"><button type="button" class="inline-flex cursor-pointer items-center gap-2 whitespace-nowrap font-medium transition-colors border border-input bg-background shadow-sm hover:bg-accent hover:text-accent-foreground rounded-md px-2.5 text-xs h-7" data-dashboard-button="true" onclick="rrBack()">${RR_SVG_BACK}返回监控</button><span class="text-muted-foreground truncate text-xs">推理过程 · 类型</span></div><div class="min-h-0 flex-1 overflow-auto"><div class="space-y-4 p-2 sm:p-3">${body}</div></div></div></main><script>
function rrHost(m){m.__moReason=1;try{parent.postMessage(m,'*')}catch(e){}}
function rrBack(){rrHost({cmd:'back'})}
function rrStage(v){rrHost({cmd:'stage',val:v||null})}
<\/script><\/body><\/html>`;
  }

  /* 整页交互状态：会话默认跟随侧栏选中；选中记录=点击循环的最后一条（收尾轮） */
  const allFin=MO.events.filter(x=>x.type==='planner.finalized');
  MO.rr={page:1,sess:MO.sel||'',type:'',sel:null};
  if(selEv){
    const own=rrRoundsOf(selEv);
    if(own.length)MO.rr.sel=own[own.length-1].key
  }
  const pre=rrFiltered();
  if(!MO.rr.sel&&pre.length)MO.rr.sel=pre[0].key;
  {const idx=pre.findIndex(r=>r.key===MO.rr.sel);if(idx>=0)MO.rr.page=Math.floor(idx/RR_PAGE)+1}
  const pi=rrPageInfo(pre);
  const selRec=pre.find(r=>r.key===MO.rr.sel)||null;
  const det=selRec?rrDetailHTML(selRec):null;
  const sessOpts=[{v:'',t:'全部会话'}].concat([...new Set(allFin.map(x=>x.sid))].map(s=>({v:s,t:(MO.sessions[s]&&MO.sessions[s].name)||s})));
  const dropItem=(key,o,cur)=>`<button type="button" class="rr-ditem${cur===o.v?' on':''}" data-v="${esc(o.v)}" onclick="rrPick('${key}',this.dataset.v)">${esc(o.t)}</button>`;
  const sessDrop=sessOpts.map(o=>dropItem('sess',o,MO.rr.sess)).join('');
  const sessLabel=(sessOpts.find(o=>o.v===MO.rr.sess)||sessOpts[0]).t;
  /* 部署版「类型」按钮：左箭头图标 + 固定文案，点击回推理过程类型选择视图
     （v6.20.0；原动作类型下拉撤销——部署版工具栏无此件，动作过滤输入框保留） */
  /* 部署版 master-detail 骨架：grid lg:[280px_1fr]，左=返回+条数/筛选/记录列表/分页，右=详情 */
  const leftHead=`<div class="flex items-center gap-2 border-b px-2 py-1.5"><button type="button" class="inline-flex cursor-pointer items-center gap-2 whitespace-nowrap font-medium transition-colors border border-input bg-background shadow-sm hover:bg-accent hover:text-accent-foreground rounded-md px-2.5 text-xs h-7" data-dashboard-button="true" onclick="rrBack()">${RR_SVG_BACK}返回监控</button><span class="text-muted-foreground truncate text-xs">推理过程</span></div>
  <div class="text-muted-foreground flex h-8 flex-shrink-0 items-center justify-between border-b px-2.5 text-xs"><span id="rr_cnt_total">${pi.total} 条记录</span><span id="rr_cnt_page">第 ${pi.page} / ${pi.pages} 页</span></div>
  <div class="flex flex-shrink-0 flex-col gap-2 border-b p-2"><div class="flex items-center gap-2"><div class="shrink-0"><button type="button" class="inline-flex cursor-pointer items-center gap-2 whitespace-nowrap font-medium transition-colors border border-input bg-background shadow-sm hover:bg-accent hover:text-accent-foreground rounded-md px-3 text-xs shrink-0 justify-start h-9" data-dashboard-button="true" title="切换推理过程类型（规划器/回复器）" onclick="rrStage()">${RR_SVG_BACK}<span>类型</span></button></div><div class="relative min-w-0 flex-1" data-drop><button type="button" id="rr_sessbtn" class="border-input ring-offset-background focus:ring-ring flex cursor-pointer items-center justify-between rounded-md border bg-transparent px-3 py-2 text-sm whitespace-nowrap shadow-sm focus:ring-1 focus:outline-none h-9 w-full" onclick="rrDrop('rr_sess_drop',event)"><span class="truncate" id="rr_sess_label">${esc(sessLabel)}</span>${RR_SVG_CHEV}</button><div id="rr_sess_drop" class="rr-drop rr-hide" style="right:0">${sessDrop}</div></div><button class="inline-flex cursor-pointer items-center justify-center gap-2 font-medium transition-colors border border-input bg-background shadow-sm hover:bg-accent hover:text-accent-foreground rounded-md text-xs shrink-0 p-0 h-8 w-8" data-dashboard-button="true" aria-label="刷新" title="刷新" onclick="location.reload()">${RR_SVG_REFRESH}</button></div><div class="relative"><input data-dashboard-input="true" id="rr_actfilter" class="border-input placeholder:text-muted-foreground focus-visible:ring-ring flex w-full rounded-md border bg-transparent px-3 py-1 text-base shadow-sm transition-colors focus-visible:ring-1 focus-visible:outline-none md:text-sm h-9" placeholder="动作过滤" value="" oninput="rrFilter()"></div><div class="relative">${RR_SVG_SEARCH}<input data-dashboard-input="true" id="rr_search" class="border-input placeholder:text-muted-foreground focus:ring-ring flex w-full rounded-md border bg-transparent px-3 py-1 text-base shadow-sm transition-colors focus:ring-1 focus:outline-none md:text-sm h-9 pl-9" placeholder="搜索会话、文件名、模型或记录摘要" value="" oninput="rrFilter()"></div></div>`;
  const foot=`<div class="flex h-11 flex-shrink-0 items-center justify-between border-t px-3 lg:h-12"><div class="flex items-center gap-2" id="rr_pager">${rrPagerHTML(pi)}</div><div class="text-muted-foreground mt-1 flex min-w-0 flex-wrap gap-x-3 gap-y-0.5 text-[11px] leading-4" id="rr_foot_tok">${rrFootTok(selRec)}</div></div>`;
  const emptyDet=`<div class="text-muted-foreground flex h-full items-center justify-center text-sm">在左侧选择一条记录查看详情</div>`;
  const doc=`<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>推理过程</title>${styles}</head><body class="bg-background text-foreground"><main id="main-content" class="relative isolate min-h-0 flex-1 outline-none overflow-hidden bg-background" style="display:flex;flex-direction:column;height:100%"><div class="flex h-full min-h-0 flex-col gap-2 overflow-hidden sm:gap-3 p-0"><div class="grid min-h-0 flex-1 grid-cols-1 gap-2 transition-[gap,grid-template-columns] duration-300 ease-out lg:gap-3 lg:grid-cols-[280px_minmax(0,1fr)]"><div class="bg-background flex h-[32vh] min-h-[180px] flex-col overflow-hidden rounded-md border lg:h-auto lg:min-h-0">${leftHead}<div class="relative overflow-auto min-h-0 flex-1"><div class="flex flex-col gap-1.5 p-2" id="rr_list">${rrRowsHTML(pre,pi)}</div></div>${foot}</div><div class="bg-background relative flex min-h-0 flex-col overflow-hidden rounded-md border"><div id="rr_detail_head">${det?det.head:''}</div><div class="relative overflow-auto min-h-0 flex-1"><div class="space-y-2 p-2 sm:space-y-3 sm:p-3" id="rr_detail">${det?det.items:emptyDet}</div></div></div></div></div></main><script>
function rrToggle(box){const open=box.dataset.state!=='open';box.dataset.state=open?'open':'closed';const b=box.querySelector('.rr-json');if(b)b.classList.toggle('rr-hide',!open);const OFF=['lg:z-10','lg:w-48','lg:shadow-sm'],ON=['lg:z-40','lg:w-[min(48%,46rem)]','lg:shadow-xl'];box.classList.remove(...(open?OFF:ON));box.classList.add(...(open?ON:OFF));const sv=box.querySelector('button svg');if(sv)sv.style.transform=open?'rotate(180deg)':''}
function rrFilter(){var a=(document.getElementById('rr_actfilter').value||'').toLowerCase();var s=(document.getElementById('rr_search').value||'').toLowerCase();document.querySelectorAll('#rr_list button[data-tools]').forEach(function(r){var tools=(r.getAttribute('data-tools')||'').toLowerCase();var full=r.textContent.toLowerCase();r.style.display=(!a||tools.indexOf(a)>=0)&&(!s||full.indexOf(s)>=0)?'':'none'})}
function rrCloseDrops(){document.querySelectorAll('.rr-drop').forEach(function(x){x.classList.add('rr-hide')})}
function rrDrop(id,ev){ev.stopPropagation();const el=document.getElementById(id);if(!el)return;const was=el.classList.contains('rr-hide');rrCloseDrops();if(was)el.classList.remove('rr-hide')}
document.addEventListener('click',function(e){if(!e.target.closest('[data-drop]'))rrCloseDrops()});
/* 宿主在 dashboard 的 sandbox iframe 里（无 allow-same-origin，透明源）——
   parent.* 直调与 contentDocument 直写都会被同源策略拦死，控件回路只能走 postMessage */
function rrHost(m){m.__moReason=1;try{parent.postMessage(m,'*')}catch(e){}}
function rrPick(k,v){rrHost({cmd:'set',key:k,val:v})}
function rrGo(d){rrHost({cmd:'page',delta:d})}
function rrSel(k){rrHost({cmd:'sel',key:k})}
function rrBack(){rrHost({cmd:'back'})}
function rrStage(v){rrHost({cmd:'stage',val:v||null})}
window.addEventListener('message',function(e){
  var m=e.data;if(!m||m.__moReasonUpd!==1)return;
  if(m.list!=null){var l=document.getElementById('rr_list');if(l){l.innerHTML=m.list;rrFilter()}}
  if(m.cntTotal!=null){var t=document.getElementById('rr_cnt_total');if(t)t.textContent=m.cntTotal}
  if(m.cntPage!=null){var p=document.getElementById('rr_cnt_page');if(p)p.textContent=m.cntPage}
  if(m.pager!=null){var g=document.getElementById('rr_pager');if(g)g.innerHTML=m.pager}
  if(m.sessLabel!=null){var s=document.getElementById('rr_sess_label');if(s)s.textContent=m.sessLabel}
  if(m.typeLabel!=null){var y=document.getElementById('rr_type_label');if(y)y.textContent=m.typeLabel}
  if(m.detailHead!=null){var dh=document.getElementById('rr_detail_head');if(dh)dh.innerHTML=m.detailHead}
  if(m.detail!=null){var dd=document.getElementById('rr_detail');if(dd)dd.innerHTML=m.detail}
  if(m.footTok!=null){var ft=document.getElementById('rr_foot_tok');if(ft)ft.textContent=m.footTok}
  rrCloseDrops();
  if(m.scrollTopReset){var sc=document.querySelector('#rr_list');var sc2=sc&&sc.parentElement;if(sc2)sc2.scrollTop=0}
  if(m.scrollSel){var sl=document.querySelector('#rr_list .selrow');if(sl)sl.scrollIntoView({block:'nearest'})}
  if(m.detailTop){var dt=document.getElementById('rr_detail');if(dt&&dt.parentElement)dt.parentElement.scrollTop=0}
});
const sel=document.querySelector('#rr_list .selrow');if(sel)sel.scrollIntoView({block:'nearest'});
<\/script><\/body><\/html>`;
  return doc;
}
/* iframe 内控件回调：选择/翻页/筛选在宿主重算，结果经 postMessage 下发
   （contentDocument 直写在 sandbox 透明源下抛 SecurityError，v6.14.8 改协议） */
function moReasonUpdate(scrollSel,detailTop){
  const f=document.getElementById('mo_reason_frame');if(!f||!f.contentWindow)return;
  const list=rrFiltered(),pi=rrPageInfo(list);
  const rr=MO.rr||(MO.rr={});rr.page=pi.page;
  if(rr.sel&&!list.some(r=>r.key===rr.sel))rr.sel=null;
  const detRec=rr.sel?list.find(r=>r.key===rr.sel):null;
  const det=detRec?rrDetailHTML(detRec):null;
  const sessLabel=(MO.sessions[rr.sess]&&MO.sessions[rr.sess].name)||rr.sess||'全部会话';
  const typeLabel=(RR_TYPES.find(o=>o[0]===rr.type)||RR_TYPES[0])[1];
  try{
    f.contentWindow.postMessage({__moReasonUpd:1,
      list:rrRowsHTML(list,pi),
      cntTotal:pi.total+' 条记录',cntPage:'第 '+pi.page+' / '+pi.pages+' 页',
      pager:rrPagerHTML(pi),sessLabel:sessLabel,
      detailHead:det?det.head:'',detail:det?det.items:'<div class="text-muted-foreground flex h-full items-center justify-center text-sm">在左侧选择一条记录查看详情</div>',
      footTok:rrFootTok(detRec),
      scrollSel:!!scrollSel,detailTop:!!detailTop},'*');
  }catch(e){}
}
function moReasonPage(delta){
  const rr=MO.rr||(MO.rr={});rr.page=(rr.page||1)+delta;
  moReasonUpdate(false,false);
}
function moReasonSet(key,val){
  const rr=MO.rr||(MO.rr={});rr[key]=val;rr.page=1;
  if(key==='sess')rr.sel=null;  /* 换数据源，原选中失效 */
  moReasonUpdate(false,false);
}
function moReasonSel(key){
  const rr=MO.rr||(MO.rr={});
  const list=rrFiltered();
  const idx=list.findIndex(r=>r.key===key);
  if(idx<0)return;
  rr.sel=key;
  rr.page=Math.floor(idx/RR_PAGE)+1;  /* 选中记录翻到所在页（部署版从监控跳转语义） */
  moReasonUpdate(true,true);
}
function moOnScroll(el){MO.autoScroll=el.scrollHeight-el.scrollTop-el.clientHeight<80;moRenderStagebar()}
function moToBottom(){const tl=document.getElementById('mo_tl');if(tl)tl.scrollTop=tl.scrollHeight;MO.autoScroll=true;moRenderStagebar()}
function moClear(){MO.events=[];MO.seenIds=new Set();if(MO.renderTimer){clearTimeout(MO.renderTimer);MO.renderTimer=null}moRenderAll()}
function moRenderAll(){moRenderSide();moRenderStagebar();moRenderTimeline()}
/* ---- 数据通道：轮询保底常开；SSE 可用（真正收到帧）才接管停轮询 ---- */
function moStopPoll(){
  if(MO.pollTimer){clearInterval(MO.pollTimer);MO.pollTimer=null}
}
function moTeardown(){
  /* 离开观察页拆通道（v6.20.3）：停轮询 + 退订 SSE + 复位 started 门闩；
     seenIds/events 保留，重进页面时增量去重与时间线延续 */
  moStopPoll();
  if(MO.sseId){const b=getBridge();if(b&&b.unsubscribeSSE)asyncBridgeUnsub(b);MO.sseId=null}
  MO.started=false;
}
async function startMonitor(){
  if(MO.started){moRenderAll();return}
  MO.started=true;
  /* 全屏回路：Esc 退出；浏览器原生退出全屏（Esc 被浏览器吃掉）时经 fullscreenchange 复位。
     插件页运行在宿主 iframe 里：焦点在内页命中本 document，焦点在宿主命中 parent（同源可挂） */
  const moEsc=e=>{if(e.key==='Escape'&&MO.full)moToggleFull()};
  document.addEventListener('keydown',moEsc);
  try{window.parent&&window.parent.addEventListener&&window.parent.addEventListener('keydown',moEsc)}catch(e){}
  document.addEventListener('fullscreenchange',()=>{
    if(!document.fullscreenElement&&MO.full){
      MO.full=false;
      const r=document.querySelector('.mo-root');if(r)r.classList.remove('fullscreen');
      moRefreshAside();
    }
  });
  /* 推理页 srcdoc 上行指令：本页在 dashboard sandbox iframe 里（透明源），
     srcdoc 只能经 postMessage 调到这里的 moReason* 系列 */
  window.addEventListener('message',e=>{
    const m=e.data;if(!m||m.__moReason!==1)return;
    try{
      if(m.cmd==='set')moReasonSet(m.key,m.val);
      else if(m.cmd==='page')moReasonPage(m.delta);
      else if(m.cmd==='sel')moReasonSel(m.key);
      else if(m.cmd==='back')moReasonBack();
      else if(m.cmd==='stage')moReasonStage(m.val);
    }catch(err){}
  });
  try{
    const r=await apiGet('monitor/replay',{since:0,limit:300});
    const arr=(r&&r.data)||r||[];
    arr.forEach(x=>moIngest(x));
    MO.connTxt=`已载入 ${arr.length} 条历史`;
  }catch(e){MO.connTxt='历史载入失败：'+e.message}
  moRenderAll();
  moStartPoll();  // 保底通道先跑起来——SSE 被扩展层缓冲/悬死时页面依然实时
  const b=getBridge();
  if(b&&b.subscribeSSE){
    try{
      MO.sseId=await b.subscribeSSE('monitor/stream',
        {onmessage:m=>{
           try{
             moIngest(JSON.parse(m));
             /* 收到真实帧 = SSE 通道活着 → 停轮询 */
             if(MO.gotOpen&&MO.pollTimer){moStopPoll();MO.connTxt='已连接';moRenderConn()}
           }catch(err){}
         },
         onstate:s=>{if(s!=='open'&&!MO.gotOpen)moStartPoll()}},{});
      MO.connTxt='实时连接…';
      // 看门狗：8s 内没有任何帧（含 stream.open）→ 退回并保持轮询
      setTimeout(()=>{
        if(!MO.gotOpen&&MO.sseId){
          try{if(b.unsubscribeSSE)asyncBridgeUnsub(b)}catch(e){}
          MO.sseId=null;
        }
      },8000);
      moRenderConn();
    }catch(e){MO.sseId=null;moRenderConn()}
  }
}
async function asyncBridgeUnsub(b){
  try{if(MO.sseId&&b.unsubscribeSSE)await b.unsubscribeSSE(MO.sseId)}catch(e){}
  MO.connTxt='轮询模式（2.5s）';moRenderConn();
}
function moStartPoll(){
  if(MO.pollTimer)return;
  if(!MO.gotOpen)MO.connTxt='轮询中（2.5s）';
  moRenderConn();
  MO.pollTimer=setInterval(async()=>{
    try{
      const r=await apiGet('monitor/replay',{since:MO.lastEventId,limit:200});
      const arr=(r&&r.data)||r||[];
      if(arr.length)arr.forEach(x=>moIngest(x));
    }catch(e){}
  },2500);
}

/* ---------------- 模型管理（仿 MaiBot /config/model：厂商浏览只读 + 任务分配） ---------------- */
const MD_TASKS=[
  {key:'planner',title:'规划模型 (planner)',short:'规划模型',desc:'负责决定麦麦什么时候回复、如何行动（reply / wait / 表情 / 工具调用）。',
   caps:[{t:'必须',d:'支持函数调用（Function Calling / Tool Use）——reply、wait、send_emoji、fetch_history、tool_search 全靠它调用'},
         {t:'必须',d:'指令遵循强——严格遵守 wait 纪律与工具参数格式，否则会出现连环发言或参数错误'},
         {t:'建议',d:'带思考的推理模型——何时开口、如何行动这类决策直接吃推理质量'},
         {t:'建议',d:'长上下文（≥64K）——planner 上下文为稳定窗的 2 倍，群聊场景消息量大'},
         {t:'加分',d:'中文语境理解好——群聊斗图/玩梗/阴阳怪气的判断全靠语感'}]},
  {key:'replyer',title:'回复模型 (replyer)',short:'回复模型',desc:'用于生成麦麦的可见回复，文风任务而非推理任务。',
   caps:[{t:'必须',d:'指令遵循强——输出指令要求只产出发言内容本身，不写旁白/解释/角色名'},
         {t:'必须',d:'中文口语自然——拟人文风、颜文字、语气词是它的本职'},
         {t:'建议',d:'非思考模型——回复意图已由规划模型确定，思考只增加发言延迟，且思考型模型正文留空时本轮会放弃发言'},
         {t:'建议',d:'响应快（首 token 快）——发送侧还要叠加拟人打字延迟，模型慢会显得呆'},
         {t:'按需',d:'视觉能力——开启识图时需要；当前识图跟随本任务的模型，绑视觉条目即可'}]},
  {key:'emoji',title:'表情选择模型 (emoji)',short:'表情选择模型',desc:'表情包发送前的语境检索词生成，单轮小调用。',
   caps:[{t:'必须',d:'单轮文本生成即可——无需工具调用、无需思考'},
         {t:'建议',d:'便宜且快——每次发表情包前多一次小调用'},
         {t:'建议',d:'中文语境理解——从聊天氛围提炼出贴图的检索词'}]},
  {key:'learner',title:'学习模型 (learner)',short:'学习模型',desc:'用于表达方式学习和黑话学习，发言后异步运行。',
   caps:[{t:'必须',d:'结构化输出稳定——表达提取与黑话推断的提示词要求固定格式'},
         {t:'建议',d:'便宜——发言后批量异步，调用量随聊天量增长'},
         {t:'建议',d:'非思考模型——后台任务不需要低延迟但需要低成本'}]},
  {key:'expression_use',title:'表达选择模型 (expression_use)',short:'表达选择模型',desc:'表达方式使用时的情境选择，单轮小调用。',
   caps:[{t:'必须',d:'文本理解与选择——从表达库中挑出贴合当前语境的条目'},
         {t:'建议',d:'快且便宜——每次发言前多一次小调用'},
         {t:'建议',d:'非思考模型'}]},
  {key:'embedding',title:'嵌入模型 (embedding)',short:'嵌入模型',desc:'「超级精细」表达方式（vector_intent）的语义召回用；列表只显示 AstrBot 模型提供商中的嵌入类 Provider，未配置时自动回落随手抽取。',
   caps:[{t:'必须',d:'嵌入类 Provider（provider_type=embedding）——普通聊天模型不能用于此处'},
         {t:'建议',d:'维度稳定——候选向量缓存在学习库，模型或维度变化会自动重算'},
         {t:'按需',d:'仅表达使用方式选「超级精细」时需要；随手模式可不配'}]},
];
const MD_STRATEGIES=[
  {v:'sequential',label:'按顺序优先（sequential）',tip:'优先使用模型列表中靠前的模型，前面的模型不可用时再尝试后面的模型。'},
  {v:'random',label:'随机选择（random）',tip:'每次请求从模型列表中随机选择一个模型，适合简单分散请求。'},
  {v:'balance',label:'负载均衡（balance）',tip:'优先选择当前使用次数较少的模型，适合多个同类模型共同承担请求。'},
];
let MM=null;
function mdTaskCfg(k){
  /* task_models 为 list 形态：[{task, models, strategy}]（AstrBot object 型 schema 不收任意嵌套） */
  const list=Array.isArray(S&&S.task_models)?S.task_models:[];
  const c=list.find(x=>x&&x.task===k)||{};
  const st=MD_STRATEGIES.some(s=>s.v===c.strategy)?c.strategy:'sequential';
  return {models:Array.isArray(c.models)?c.models.filter(m=>m&&m.provider&&m.model):[],strategy:st};
}
function mdTaskEntry(k){
  if(!Array.isArray(S.task_models))S.task_models=[];
  let e=S.task_models.find(x=>x&&x.task===k);
  if(!e){e={task:k,models:[],strategy:'sequential'};S.task_models.push(e)}
  if(!Array.isArray(e.models))e.models=[];
  return e;
}
function modelsHTML(){
  const uncfg=MD_TASKS.filter(t=>!mdTaskCfg(t.key).models.length).map(t=>t.short);
  return `<div class="page">
    <div class="page-head"><div><h1>模型管理</h1></div></div>
    ${uncfg.length?`<div class="md-alert"><strong>${moIcon('circle-alert',14)}以下任务未分配模型</strong><div class="md-alert-body">${uncfg.join('、')} 还未分配模型，将使用 AstrBot 默认 Provider。</div></div>`:''}
    <div class="ui-tabs full" style="grid-template-columns:1fr 1fr">
      <button id="md_tab_cfg" class="on" onclick="mdSwitchTab('cfg')">模型设置</button>
      <button id="md_tab_tasks" onclick="mdSwitchTab('tasks')">功能分配</button>
    </div>
    <div id="md_body" onclick="if(MM&&MM.msOpen){MM.msOpen=false;mdRender()}"><div class="loading">加载中…</div></div>
  </div>`;
}
async function initModelsPage(){
  S.task_models=mdNormalize(S.task_models);
  MM={providers:[],sel:'all',tab:'cfg',search:'',selTask:'planner',msOpen:false,msQuery:'',saveTimer:null,saveTxt:''};
  try{
    const r=await apiGet('models/list');
    MM.providers=(r&&r.providers)||[];
  }catch(e){MM.providers=[]}
  mdRender();
}
function mdSwitchTab(t){MM.tab=t;MM.msOpen=false;mdRender()}
function mdSelProv(p){MM.sel=p;MM.msOpen=false;mdRender()}
function mdSelTask(k){MM.selTask=k;MM.msOpen=false;mdRender()}
function mdUsedSet(){const s=new Set();MD_TASKS.forEach(t=>mdTaskCfg(t.key).models.forEach(m=>s.add(m.provider+'::'+m.model)));return s}
function mdRender(){
  const a=document.getElementById('md_tab_cfg'),b=document.getElementById('md_tab_tasks');
  if(a)a.classList.toggle('on',MM.tab==='cfg');
  if(b)b.classList.toggle('on',MM.tab==='tasks');
  const host=document.getElementById('md_body');
  if(host)host.innerHTML=MM.tab==='cfg'?mdCfgHTML():mdTasksHTML();
}

/* Tab1：厂商+模型 只读浏览 */
function mdCfgHTML(){
  const provs=MM.providers,total=provs.reduce((a,p)=>a+p.models.length,0);
  const selProv=MM.sel==='all'?null:provs.find(p=>p.id===MM.sel);
  const shown=selProv?[selProv]:provs;
  const q=(MM.search||'').toLowerCase(),used=mdUsedSet();
  let count=0;
  const rows=[];
  shown.forEach(p=>p.models.forEach(m=>{
    count++;
    if(q&&!(String(m).toLowerCase().includes(q)||String(p.id).toLowerCase().includes(q)))return;
    const on=used.has(p.id+'::'+m);
    rows.push(`<tr><td class="md-dotcell"><span class="md-dot ${on?'on':''}" title="${on?'已使用':'未使用'}"></span></td><td>${esc(m)}</td><td>${esc(p.id)}${p.enable?'':' <span class="ui-badge secondary">未启用</span>'}</td></tr>`);
  }));
  return `<div class="md-grid">
    <aside class="md-aside">
      <div class="md-ahead"><h2>模型厂商</h2></div>
      <div class="md-alist">
        <button class="md-item ${MM.sel==='all'?'on':''}" onclick="mdSelProv('all')"><span class="l2"><span class="lbl">全部</span></span><span class="cnt">${total}</span></button>
        ${provs.map(p=>`<button class="md-item ${MM.sel===p.id?'on':''} ${p.enable?'':'off'}" onclick="mdSelProv('${jsq(p.id)}')" title="${esc(p.type)}"><span class="l2"><span class="lbl">${esc(p.id)}</span></span><span class="cnt">${p.models.length}</span></button>`).join('')}
      </div>
    </aside>
    <section class="md-main">
      <div class="md-bar">
        <div class="md-search">${moIcon('search',14)}<input placeholder="搜索模型名称、标识符或提供商..." value="${esc(MM.search||'')}" oninput="MM.search=this.value;mdRefreshRows()"></div>
        <p class="md-found" id="md_found"></p>
      </div>
      ${selProv?`<div class="md-provbar"><div><h3>${esc(selProv.id)}</h3><p>类型：${esc(selProv.type)||'—'} · 默认模型：${esc(selProv.default_model)||'—'} · ${selProv.models.length} 个模型${selProv.enable?'':' · 未启用'}</p></div></div>`:''}
      <table class="ui-tbl"><thead><tr><th class="md-dotcell">使用</th><th>模型名称</th><th>提供商</th></tr></thead><tbody id="md_rows">${rows.length?rows.join(''):`<tr><td colspan="3" class="empty-cell">${MM.search?'未找到匹配的模型':'暂无模型配置'}</td></tr>`}</tbody></table>
    </section>
  </div>`;
}
function mdRefreshRows(){
  /* 搜索只刷新表格与计数，不重建输入框（保焦点） */
  const host=document.getElementById('md_body');if(!host||MM.tab!=='cfg')return;
  const tmp=document.createElement('div');
  /* 复用 mdCfgHTML 的行渲染：直接重渲染表格区域 */
  const provs=MM.providers;
  const selProv=MM.sel==='all'?null:provs.find(p=>p.id===MM.sel);
  const shown=selProv?[selProv]:provs;
  const q=(MM.search||'').toLowerCase(),used=mdUsedSet();
  const rows=[];
  shown.forEach(p=>p.models.forEach(m=>{
    if(q&&!(String(m).toLowerCase().includes(q)||String(p.id).toLowerCase().includes(q)))return;
    const on=used.has(p.id+'::'+m);
    rows.push(`<tr><td class="md-dotcell"><span class="md-dot ${on?'on':''}" title="${on?'已使用':'未使用'}"></span></td><td>${esc(m)}</td><td>${esc(p.id)}${p.enable?'':' <span class="ui-badge secondary">未启用</span>'}</td></tr>`);
  }));
  const tb=document.getElementById('md_rows');
  if(tb)tb.innerHTML=rows.length?rows.join(''):`<tr><td colspan="3" class="empty-cell">${MM.search?'未找到匹配的模型':'暂无模型配置'}</td></tr>`;
  const fd=document.getElementById('md_found');
  if(fd)fd.textContent=MM.search?`找到 ${rows.length} 个结果`:'';
}
/* Tab2：任务分配 */
function mdOptions(){const out=[];const wantEmb=MM.selTask==='embedding';MM.providers.forEach(p=>{if((p.type==='embedding')!==wantEmb)return;p.models.forEach(m=>out.push({provider:p.id,model:m}))});return out}
function mdMSListHTML(selected){
  const q=(MM.msQuery||'').toLowerCase();
  const filtered=mdOptions().filter(o=>!q||(o.model+' '+o.provider).toLowerCase().includes(q));
  if(!filtered.length)return `<div class="md-ms-empty">暂无可用模型</div>`;
  return filtered.map(o=>{
    const on=selected.some(s=>s.provider===o.provider&&s.model===o.model);
    return `<div class="md-ms-item" onclick="event.stopPropagation();mdToggleModel('${jsq(o.provider)}','${jsq(o.model)}')"><span class="md-check">${on?moIcon('circle-check',13):''}</span><span>${esc(o.provider)} · ${esc(o.model)}</span></div>`;
  }).join('');
}
function mdMultiSelectHTML(selected){
  return `<div class="md-ms" id="md_ms">
    <button type="button" class="md-ms-trig" onclick="event.stopPropagation();MM.msOpen=!MM.msOpen;mdRender()">
      <span class="md-ms-badges">${selected.length?selected.map(m=>`<span class="ui-badge secondary md-tag"><span>${esc(m.provider)} · ${esc(m.model)}</span><i title="移除" onclick="event.stopPropagation();mdToggleModel('${jsq(m.provider)}','${jsq(m.model)}')">${moIcon('x',10)}</i></span>`).join(''):'<span class="md-ms-ph">选择模型...</span>'}</span>
      ${moIcon('chevrons-up-down',14)}
    </button>
    ${MM.msOpen?`<div class="md-ms-pop" onclick="event.stopPropagation()">
      <div class="md-ms-search">${moIcon('search',13)}<input placeholder="搜索..." value="${esc(MM.msQuery||'')}" oninput="MM.msQuery=this.value;document.getElementById('md_ms_list').innerHTML=mdMSListHTML(mdTaskCfg(MM.selTask).models)"></div>
      <div class="md-ms-list" id="md_ms_list">${mdMSListHTML(selected)}</div>
    </div>`:''}
  </div>`;
}
function mdTasksHTML(){
  const meta=MD_TASKS.find(t=>t.key===MM.selTask)||MD_TASKS[0];
  const cur=mdTaskCfg(meta.key);
  const st=MD_STRATEGIES.find(s=>s.v===cur.strategy)||MD_STRATEGIES[0];
  return `<div class="md-grid">
    <aside class="md-aside">
      <div class="md-ahead"><h2>模型类别</h2></div>
      <div class="md-alist">
        ${MD_TASKS.map(t=>{const c=mdTaskCfg(t.key);return `<button class="md-item ${MM.selTask===t.key?'on':''}" onclick="mdSelTask('${t.key}')"><span class="l2"><span class="lbl">${t.short}</span><small class="sub">${c.models.length?c.models.map(m=>esc(m.model)).join('、'):'未配置模型'}</small></span><span class="cnt ${c.models.length?'ok':''}">${c.models.length?'已配置 · '+c.models.length:'未配置'}</span></button>`}).join('')}
      </div>
    </aside>
    <section class="md-main">
      <div class="md-thead"><h3>${meta.title}</h3><p>${meta.desc}</p>
        <div class="md-caps">${(meta.caps||[]).map(c=>`<div class="md-cap"><span class="md-cap-t ${c.t==='必须'?'must':(c.t==='按需'?'opt':'')}" style="border:1px solid hsl(var(--border));border-radius:6px;padding:1px 6px;font-size:11px;flex:none;${c.t==='必须'?'color:hsl(var(--destructive))':''}">${c.t}</span><span style="flex:1">${c.d}</span></div>`).join('')}</div>
      </div>
      <div class="md-fld"><label>模型列表</label>${mdMultiSelectHTML(cur.models)}</div>
      <div class="md-fld"><label>模型选择策略</label>
        <select class="md-sel" onchange="mdSetStrategy(this.value)">
          ${MD_STRATEGIES.map(s=>`<option value="${s.v}" ${cur.strategy===s.v?'selected':''}>${s.label}</option>`).join('')}
        </select>
        <p class="md-hint">${st.tip}</p>
      </div>
      <p class="md-note">温度 / 最大 Token 等参数跟随所选 Provider 在 AstrBot 模型设置中的配置（text_chat 不支持按次覆盖）；同理，思考开关也无法按次关闭——需要非思考行为时请直接选择非思考的模型条目。规划模型建议绑「支持工具调用 + 思考」的条目，回复模型建议绑「快 + 非思考」的条目，两类任务可在 AstrBot「模型提供商」中分属不同条目。</p>
      <div class="md-savest"><span id="md_savetxt">${MM.saveTxt||''}</span></div>
    </section>
  </div>`;
}
function mdToggleModel(p,m){
  const entry=mdTaskEntry(MM.selTask),list=entry.models;
  const idx=list.findIndex(x=>x.provider===p&&x.model===m);
  if(idx>=0)list.splice(idx,1);else list.push({provider:p,model:m});
  mdScheduleSave();mdRender();
}
function mdSetStrategy(v){
  mdTaskEntry(MM.selTask).strategy=v;
  mdScheduleSave();mdRender();
}
function mdNormalize(v){
  const out=[];const known={};
  if(Array.isArray(v))v.forEach(e=>{if(e&&e.task)known[e.task]=e});
  else if(v&&typeof v==='object')Object.keys(v).forEach(k=>{known[k]=Object.assign({task:k},v[k])});
  MD_TASKS.forEach(t=>{
    const e=known[t.key]||{};
    out.push({task:t.key,
      models:Array.isArray(e.models)?e.models.filter(m=>m&&m.provider&&m.model):[],
      strategy:MD_STRATEGIES.some(s=>s.v===e.strategy)?e.strategy:'sequential'});
  });
  return out;
}
function mdScheduleSave(){
  MM.saveTxt='配置将在 2 秒后自动保存';mdSaveTxt();
  if(MM.saveTimer)clearTimeout(MM.saveTimer);
  MM.saveTimer=setTimeout(mdDoSave,2000);
}
function mdSaveTxt(){const el=document.getElementById('md_savetxt');if(el)el.textContent=MM.saveTxt||''}
async function mdDoSave(){
  MM.saveTxt='保存中…';mdSaveTxt();
  try{
    await apiPost('config',{task_models:S.task_models});
    MM.saveTxt='已保存';
  }catch(e){MM.saveTxt='保存失败：'+e.message}
  mdSaveTxt();
}

/* ---------------- 模式 ---------------- */
function setMode(m){S.mode=m;document.querySelectorAll('.mode').forEach((el,i)=>el.classList.toggle('on',['planner','independent','native'][i]===m))}

/* ---------------- 收集 & 保存 ---------------- */
function collect(){
  const g=id=>{const el=document.getElementById(id);return el?el.value:null};
  const num=(id,dft)=>{const v=parseFloat(g(id));return isNaN(v)?dft:v};
  const bool=(id,dft)=>{const el=document.getElementById(id);return el?(el.type==='checkbox'?el.checked:dft):dft};
  if(g('bot_name')!==null)S.bot_name=g('bot_name')||'麦麦';
  ['personality','reply_style','behavior_style','group_chat_prompt','private_chat_prompts'].forEach(k=>{if(g(k)!==null)S[k]=g(k)});
  if(g('max_context_size')!==null)S.max_context_size=Math.max(5,parseInt(g('max_context_size'))||40);
  if(g('reply_trigger_mode')!==null)S.reply_trigger_mode=g('reply_trigger_mode');
  ['multiple_probability','typo_min_freq','splitter_max_length','splitter_max_sentence_num','splitter_max_split_num','max_private_context_size','max_consecutive_wait_count','planner_interrupt_max_consecutive_count','no_action_backoff_start_count','no_action_backoff_bypass_pending_count'].forEach(k=>{if(g(k)!==null)S[k]=num(k,S[k])});
  ['talk_value','private_talk_value','typing_speed','typo_correction_quote_probability','typo_error_rate','typo_tone_error_rate','typo_word_replace_rate','no_action_backoff_base_seconds','no_action_backoff_cap_seconds'].forEach(k=>{if(g(k)!==null)S[k]=num(k,S[k])});
  ['enable','enable_response_post_process','typo_enable','typo_enable_correction_quote','splitter_enable','splitter_enable_kaomoji_protection','splitter_enable_overflow_return_all','inevitable_at_reply','mentioned_bot_reply','enable_talk_value_rules','enable_reply_quote','expression_checked_only','expression_self_reflect'].forEach(k=>{S[k]=bool(k,S[k]??true)});
  if(g('expression_selection_mode')!==null)S.expression_selection_mode=g('expression_selection_mode');
  if(g('expression_vector_candidate_pool_size')!==null)S.expression_vector_candidate_pool_size=Math.min(50,Math.max(1,parseInt(g('expression_vector_candidate_pool_size'))||50));
  const mb=document.getElementById('maid_bridge');if(mb)S.maid_bridge=mb.checked;
  const eaw=document.getElementById('escape_at_wake');if(eaw)S.escape_at_wake=eaw.checked;
  const ei=document.getElementById('eco_injection');if(ei)S.eco_injection=ei.checked;
  const styles=[...document.querySelectorAll('[data-style]')].map(t=>t.value.trim()).filter(Boolean);
  if(styles.length||document.getElementById('mrs'))S.multiple_reply_style=styles;
  const cps=[...document.querySelectorAll('[data-cp]')].map(r=>({platform:r.querySelector('.cp-platform').value.trim(),item_id:r.querySelector('.cp-chat').value.trim(),rule_type:r.dataset.rt||'group',prompt:r.querySelector('.cp-prompt').value.trim()})).filter(c=>c.platform&&c.item_id&&c.prompt);
  if(cps.length||document.getElementById('cps'))S.chat_prompts=cps;
  const tvrs=[...document.querySelectorAll('[data-tvr]')].map(r=>({platform:r.querySelector('.tvr-platform').value.trim(),item_id:r.querySelector('.tvr-item').value.trim(),rule_type:r.dataset.rt||'group',time:r.querySelector('.tvr-time').value.trim(),value:parseFloat(r.querySelector('.tvr-value').value)||0}));
  if(tvrs.length||document.getElementById('tvrs'))S.talk_value_rules=tvrs;
  const gps=[...document.querySelectorAll('[data-gp]')].map(r=>({chat:r.querySelector('.gp-chat').value.trim(),name:r.querySelector('.gp-name').value}));
  if(gps.length||document.getElementById('gps'))S.group_persona=gps.filter(g=>g.chat&&g.name);
  const lrCollect=host=>host?[...host.querySelectorAll('[data-elr]')].map(r=>({platform:r.querySelector('.elr-platform').value.trim(),item_id:r.querySelector('.elr-item').value.trim(),type:r.dataset.lt||'group',use:r.querySelector('.elr-use').checked,learn:r.querySelector('.elr-learn').checked})):[];
  const elrHost=document.getElementById('elrs');
  if(elrHost)S.expression_learning_list=lrCollect(elrHost);
  const jlHost=document.getElementById('jlrs');
  if(jlHost)S.jargon_learning_list=lrCollect(jlHost);
  const grCollect=host=>host?[...host.querySelectorAll('[data-egr]')].map(r=>({targets:r.querySelector('.egr-targets').value.split(/[,，]/).map(s=>s.trim()).filter(Boolean).map(s=>{const [platform,item_id]=s.split(':').map(x=>x.trim());return {platform:platform||'',item_id:item_id||''}})})).filter(g=>g.targets.length):[];
  const egrHost=document.getElementById('egrs');
  if(egrHost)S.expression_groups=grCollect(egrHost);
  const jgrHost=document.getElementById('jgrs');
  if(jgrHost)S.jargon_groups=grCollect(jgrHost);
  const kwrHost=document.getElementById('kwrs');
  const pdrHost=document.getElementById('pds');
  if(pdrHost)S.preset_dialogues=[...pdrHost.querySelectorAll('[data-pdr]')].map(r=>({user:r.querySelector('.pdr-user').value.trim(),reply:r.querySelector('.pdr-reply').value.trim()})).filter(d=>d.user&&d.reply);
  if(kwrHost)S.keyword_rules=[...kwrHost.querySelectorAll('[data-kwr]')].map(r=>({keywords:r.querySelector('.kwr-keys').value.split(/[,，]/).map(s=>s.trim()).filter(Boolean),regex:[],reaction:r.querySelector('.kwr-reaction').value.trim()})).filter(r=>r.reaction&&r.keywords.length);
  const rxrHost=document.getElementById('rxrs');
  if(rxrHost)S.regex_rules=[...rxrHost.querySelectorAll('[data-rxr]')].map(r=>({keywords:[],regex:r.querySelector('.rxr-pattern').value.split('\n').map(s=>s.trim()).filter(Boolean),reaction:r.querySelector('.rxr-reaction').value.trim()})).filter(r=>r.reaction&&r.regex.length);
  const eco=document.getElementById('enable_context_optimization');if(eco)S.enable_context_optimization=eco.checked;
  S.enable_image_context=bool('enable_image_context',S.enable_image_context??false);
  if(g('image_context_max_num')!==null)S.image_context_max_num=Math.min(10,Math.max(1,parseInt(g('image_context_max_num'))||3));
  if(g('ban_words')!==null)S.ban_words=g('ban_words').split('\n').map(s=>s.trim()).filter(Boolean);
  if(g('ban_msgs_regex')!==null)S.ban_msgs_regex=g('ban_msgs_regex').split('\n').map(s=>s.trim()).filter(Boolean);
  if(g('max_expression_learner')!==null)S.max_expression_learner=Math.min(10,Math.max(1,parseInt(g('max_expression_learner'))||3));
  const dp=document.getElementById('default_persona'); if(dp)S.default_persona=dp.value;
  const fps=document.getElementById('follow_persona_switch'); if(fps)S.follow_persona_switch=fps.checked;
}
async function save(){
  const btn=document.getElementById('saveBtn'),bt=document.getElementById('saveBtn_t');
  if(btn)btn.disabled=true;if(bt)bt.textContent='保存中…';
  try{
    collect();
    /* pe_nick 是人格弹窗的 UI 临时键（v6.20.3：此前随整包进保存载荷） */
    const payload={...S};delete payload.pe_nick;
    await apiPost('config',payload);toast('已保存，立即生效')}
  catch(e){toast('保存失败：'+e.message)}
  if(btn)btn.disabled=false;if(bt)bt.textContent='保存全部配置';
}

/* ---------------- 启动：优先官方 SDK，2.5s 后自动切换内置备用桥 ---------------- */
(async()=>{
  // 壳图标与版本徽章
  const lg=document.getElementById('sb_logo_ic');if(lg)lg.innerHTML=moIcon('bot',24);
  const tb=document.getElementById('tb_sb');if(tb)tb.innerHTML=moIcon('chevron-left',16);
  const sv=document.getElementById('saveBtn');if(sv)sv.insertAdjacentHTML('afterbegin',moIcon('save',14));
  const ver=document.getElementById('tb_ver');if(ver)ver.textContent='maisoul '+PAGE_VERSION;
  const b = await ensureBridge();
  if(!b){
    document.getElementById('body').innerHTML =
      `<div class="panel"><h3>WebUI 桥不可用</h3>
       <div class="pdesc">本页面需要通过 <b>AstrBot WebUI → 插件 → astrbot_plugin_maisoul → 插件页面</b> 打开。
       若从此入口打开仍失败，请强制刷新（Ctrl+F5）清除缓存的旧页面后重试。页面版本：${PAGE_VERSION}</div></div>`;
    return;
  }
  try{S=await apiGet('config');['nicknames','threshold','frequency','context_size','cooldown','seg_min_delay','seg_max_delay','enable_typo','typo_rate','pe_nick'].forEach(k=>delete S[k]);render('overview')}
  catch(e){document.getElementById('body').innerHTML=`<div class="panel"><h3>配置加载失败</h3><div class="pdesc">${esc(e.message)}<br>请强制刷新（Ctrl+F5）后重试 | 页面版本 ${PAGE_VERSION}</div></div>`}
})();
