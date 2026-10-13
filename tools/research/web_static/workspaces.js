/* Integrated model/endpoint workspaces. All inference remains in the durable worker. */
export function makeWorkspaces(ui) {
  const {h,view,field,button,request,renderHeader,stateBadge,fmtDate,pretty,setNotice,getState}=ui;
  let data=null,conversation=null,error='',key='',loading=false,polling=false,sequence=0;
  function selection() {
    const s=getState(),r=s.route;
    if(r.section==='endpoints')return {endpoint:r.id,endpointView:true};
    const configured=(s.state?.models||[]).find(m=>m.identifier===r.id);
    const endpoint=r.database==='registered'?configured?.access?.endpoint_workspace_url?.split('/').pop():r.database;
    return {endpoint,model:r.id,session:r.session};
  }
  function routeKey(){const r=getState().route;return [r.section,r.database,r.id,r.session].join('|');}
  function reset(){sequence++;key='';data=null;conversation=null;error='';loading=false;}
  function linkModel(endpoint,model,session=null){return `#/models/${encodeURIComponent(endpoint)}/${encodeURIComponent(model)}${session?'/'+encodeURIComponent(session):''}`;}
  function pairs(items){return h('dl',{class:'keyvals'},items.flatMap(([a,b])=>[h('dt',{text:a}),h('dd',{text:b??'Unavailable'})]));}
  async function loadRoute(force=false) {
    const selected=selection(),target=routeKey();
    if(!selected.endpoint || (!selected.endpointView&&!selected.model))return;
    if(loading || (!force && key===target && data))return;
    const requestID=++sequence;loading=true;key=target;error='';
    try {
      const next=selected.endpointView?await request(`/api/endpoints/${selected.endpoint}`):
        await request('/api/model-workspace?'+new URLSearchParams({endpoint:selected.endpoint,model:selected.model}));
      let history=null;
      const id=selected.session;
      if(id)history=await request(`/api/conversations/${id}`);
      if(target!==routeKey()||requestID!==sequence)return;
      if(history&&(history.endpoint_id!==selected.endpoint||history.model_identifier!==selected.model))throw new Error('Conversation belongs to a different model. Open its original model workspace.');
      data=next;conversation=history;
    } catch(e){if(target===routeKey()&&requestID===sequence)error=e.message;}
    finally{if(requestID===sequence){loading=false;if(target===routeKey())renderSelected();}}
  }
  function renderSelected(){selection().endpointView?renderEndpoint():renderModel(true);}
  function waiting(){return h('p',{class:error?'error-state':'loading',text:error||'Checking the configured local service…'});}
  function health(ep){return h('div',{},[
    pairs([['Provider / runtime',ep.runtime],['Base URL',ep.endpoint],['Connection',ep.connected?'Provider model listing responded':'Service not reachable / not verified'],['Observed',fmtDate(ep.observed_at)],['Connection check',`${ep.latency_ms} ms · measured by Command Center`],['Loaded models',ep.loaded_models===null?'Unavailable from this provider':ep.loaded_models.length?ep.loaded_models.join(', '):'None reported by provider']]),
    ...(ep.errors||[]).map(text=>h('p',{class:'form-error',text})),
    h('div',{class:'actions'},[button('Test Connection','endpoint-test',{'data-endpoint':ep.id}),ep.inspection_url?h('a',{class:'button-link quiet',href:ep.inspection_url,target:'_blank',rel:'noopener noreferrer',text:'Developer: raw model listing'}):null]),
    h('p',{class:'hint',text:'Testing connection reads service state. It never starts a server or loads a model.'}),
  ]);}
  function renderEndpoint() {
    const selected=selection();renderHeader('Endpoint workspace','',selected.endpoint||'Configured services');
    if(!selected.endpoint){view.replaceChildren(h('div',{class:'endpoint-list'},(getState().bootstrap.endpoints||[]).map(e=>h('article',{},[h('h2',{text:e.provider==='ollama'?'Ollama':'Prism'}),h('p',{class:'mono',text:e.endpoint}),h('a',{class:'button-link',href:`#/endpoints/${e.id}`,text:'Open Endpoint'})]))));return;}
    if(error){view.replaceChildren(waiting(),button('Retry connection','workspace-reload'));return;}
    if(!data||key!==routeKey()){view.replaceChildren(waiting());if(!loading)loadRoute();return;}
    view.replaceChildren(h('a',{href:'#/models',text:'← Models'}),h('div',{class:'workspace-columns'},[
      h('section',{},[h('h2',{text:'Connection & health'}),health(data)]),
      h('section',{},[h('h2',{text:'Available models'}),h('p',{class:'hint',text:'Provider discovery supplies model identities. Configured models remain visible when their service is stopped.'}),
        h('div',{class:'endpoint-models'},data.models.map(m=>h('article',{},[h('strong',{class:'mono',text:m.identifier}),h('p',{class:'hint',text:`${m.availability} · ${m.reason}`}),
          m.chat_allowed?h('a',{class:'button-link',href:linkModel(data.id,m.identifier),text:'Chat with Model'}):h('p',{class:'hint',text:'This owned runtime requires an exact configured model. No substitution is permitted.'})])))])
    ]),h('section',{},[h('h2',{text:'Observed request activity'}),activityForEndpoint(data),h('p',{class:'hint',text:data.limitations.join(' ')})]));
  }
  function activityForEndpoint(endpointInfo) {
    const endpoint=endpointInfo.endpoint;
    const s=getState(),models=(s.models||s.state?.models||[]).filter(m=>m.endpoint===endpoint);
    const ids=new Set([...models.map(m=>m.identifier),...endpointInfo.models.map(m=>m.identifier)]);
    const rows=(s.state?.jobs||[]).filter(j=>ids.has(j.model_identifier)).slice(0,10);
    return rows.length?h('ul',{class:'workspace-activity'},rows.map(j=>h('li',{},[stateBadge(j.state),h('a',{href:ui.recordHref(j),text:j.name||j.id}),h('span',{class:'mono',text:j.model_identifier})]))):h('p',{class:'hint',text:'No matching persisted request records in the current window. Provider-wide traffic is unavailable.'});
  }
  function current(){return conversation?.turns?.at(-1);}
  function active(){const t=current();return t&&(['pending','running'].includes(t.state)||t.state==='completed'&&!t.released&&!t.cleanup_error);}
  function statusText(turn){
    if(turn.state==='pending')return 'Queued · waiting for the existing worker';
    if(turn.state==='cancelled')return 'Queued turn cancelled · no inference dispatched';
    if(turn.cleanup_error)return 'Resource release blocked · inspect job details';
    if(turn.state==='completed'&&!turn.released)return 'Response received · verifying resource release';
    if(turn.generation?.status==='cancelled')return turn.released?'Generation cancelled · release verified':'Cancellation requested · cleanup status below';
    if(turn.state==='running'){
      if(turn.generation?.status==='running')return 'Generating · streaming visible response';
      if(turn.generation?.status==='completed')return 'Provider stream completed · worker finalizing';
      if(turn.generation?.status==='error')return 'Stream failed · awaiting resource verification';
      const lifecycle=turn.lifecycle||getState().state?.lifecycle?.current;
      if(lifecycle?.task_id===turn.task_id)return `${lifecycle.state} · last observed worker lifecycle`;
      return turn.generation?.status==='running'?'Generating · streaming visible response':'Worker executing · live stream not yet observed';
    }
    return turn.state;
  }
  function transcript(){
    return conversation?.turns.length?h('div',{class:'conversation-transcript'},conversation.turns.map(turn=>h('article',{class:'conversation-turn'},[
      h('header',{},[h('strong',{text:'You'}),h('time',{text:fmtDate(turn.created_at)})]),h('div',{class:'conversation-text',text:turn.user}),
      h('header',{},[h('strong',{text:data.model.identifier}),h('span',{class:'hint',text:statusText(turn)})]),
      turn.response?h('div',{class:'conversation-text',text:turn.response}):h('p',{class:'hint',text:turn.state==='pending'?'Your turn is stored in the durable queue.':'No visible response has been recorded yet.'}),
      turn.error?h('p',{class:'form-error',text:turn.error}):null,turn.cleanup_error?h('p',{class:'form-error',text:turn.cleanup_error}):null,
      usageLine(turn),h('div',{class:'actions'},[h('a',{href:`#/jobs/research/${turn.task_id}`,text:'Execution details'}),turn.can_cancel?button(turn.state==='pending'?'Cancel queued turn':'Stop generation','chat-stop',{'data-task':turn.task_id}):null]),
    ]))):h('p',{class:'empty',text:'Start a conversation with this selected local model. Sending a message queues real inference; opening this page does not load it.'});
  }
  function usageLine(turn){
    const u=turn.usage;const metrics=[];
    if(u?.input_tokens!=null)metrics.push(`Input ${u.input_tokens} · provider reported`);
    if(u?.output_tokens!=null)metrics.push(`Output ${u.output_tokens} · provider reported`);
    if(u?.total_tokens!=null)metrics.push(`Total ${u.total_tokens} · ${u.total_source==='provider_reported'?'provider reported':'sum of provider counts'}`);
    if(u?.output_tokens_per_second!=null)metrics.push(`${u.output_tokens_per_second.toFixed(1)} tokens/s · ${u.throughput_source==='provider_eval_duration'?'provider timing':'Command Center measured interval'}`);
    if(turn.started_at)metrics.push(`Turn elapsed ${ui.fmtDuration(new Date(turn.completed_at||Date.now())-new Date(turn.started_at))} · includes model loading`);
    return h('p',{class:'hint generation-readout',text:metrics.length?metrics.join(' · '):'Live token counts unavailable until reported by the provider. Visible text is streamed without counting characters as tokens.'});
  }
  function agentBody(){
    return [h('summary',{text:'Recorded tool workflows · separate from direct chat'}),h('p',{class:'hint',text:'Existing opt-in capability workflows, refreshed from persisted events. Direct chat grants no tools. Prompts and tool arguments are omitted.'}),
      ...(data.agent_runs.length?data.agent_runs.map(run=>h('article',{},[h('strong',{class:'mono',text:run.id}),stateBadge(run.state),
        h('p',{class:'hint'},['Recorded capability selection: ',...(run.capabilities||[]).map(c=>h('a',{href:`#/capabilities/${encodeURIComponent(c.id)}`,text:c.name||c.id})).flatMap((x,i)=>i?[', ',x]:[x])]),
        h('ul',{},run.events.map(e=>h('li',{text:`${fmtDate(e.at)} · ${e.kind}${e.capability_id?' · '+e.capability_id:''}`})))])):[h('p',{class:'hint',text:'No recorded capability workflow for this selected model.'})])];
  }
  async function refreshMetadata(){
    const selected=selection(),target=routeKey();
    if(!data?.model||getState().route.section!=='models'||!selected.model||document.hidden)return;
    try {
      const next=await request('/api/model-workspace?'+new URLSearchParams({endpoint:selected.endpoint,model:selected.model}));
      if(target!==routeKey())return;data=next;
      const available=data.endpoint.models.find(m=>m.identifier===data.model.identifier);
      const badge=document.querySelector('#workspace-availability');if(badge){const nextBadge=stateBadge(available?.availability||'unknown');nextBadge.id='workspace-availability';badge.replaceWith(nextBadge);}
      const availabilityReason=document.querySelector('#workspace-availability-reason');if(availabilityReason)availabilityReason.textContent=available?.reason||'Availability unverified.';
      const activity=document.querySelector('#model-agent-activity');if(activity)activity.replaceChildren(...agentBody());
      const support=document.querySelector('#workspace-worker-support');if(support){support.textContent=data.worker_streaming.reason;support.className=data.worker_streaming.ready?'hint':'workspace-warning';}
    }catch(e){setNotice(`Workspace status unavailable: ${e.message}`,'error');}
  }
  function renderModel(force=false) {
    const selected=selection();renderHeader(selected.model||'Model workspace','', 'Direct model conversation');
    if(error){view.replaceChildren(waiting(),button('Retry connection','workspace-reload'));return;}
    if(!data||key!==routeKey()){view.replaceChildren(waiting());if(!loading)loadRoute();return;}
    if(!force&&document.querySelector('#chat-form'))return;
    const m=data.model,ep=data.endpoint,available=ep.models.find(x=>x.identifier===m.identifier);
    const oldForm=document.querySelector('#chat-form'),modelKey=ep.id+'|'+m.identifier;
    const draft=oldForm?.dataset.modelKey===modelKey?{content:oldForm.elements.content.value,tokens:oldForm.elements.max_tokens.value}:null;
    const badge=stateBadge(available?.availability||'unknown');badge.id='workspace-availability';
    const select=h('select',{name:'conversation','data-action':'chat-history','aria-label':'Conversation history'},[h('option',{value:'',text:'New conversation'}),...data.sessions.map(x=>h('option',{value:x.id,selected:conversation?.id===x.id?'selected':null,text:x.name}))]);
    const form=h('form',{id:'chat-form','data-model-key':modelKey},[
      field('Message',h('textarea',{name:'content',required:'required',maxlength:'8000',rows:'4',placeholder:`Message ${m.identifier}`}), 'Direct text conversation. No tools are granted to this chat.'),
      h('div',{class:'chat-composer-actions'},[field('Output token limit',h('input',{name:'max_tokens',type:'number',min:'1',max:String(m.max_tokens),value:String(Math.min(512,m.max_tokens))})),h('button',{type:'submit',class:'primary',disabled:active()?true:null,text:'Send message'})]),
      h('p',{id:'chat-error',class:'form-error',role:'status'})]);
    if(draft){form.elements.content.value=draft.content;form.elements.max_tokens.value=draft.tokens;}
    view.replaceChildren(h('div',{class:'actions workspace-links'},[h('a',{href:'#/models',text:'← Models'}),h('a',{href:`#/endpoints/${ep.id}`,text:'Open Endpoint'}),h('a',{href:'#/capabilities',text:'Skills & Tools catalog'})]),
      h('div',{class:'workspace-identity'},[h('strong',{text:ep.runtime}),h('span',{class:'mono',text:m.identifier}),badge,h('span',{class:'hint',text:`Context limit ${m.context_window} tokens · occupancy unavailable`})]),
      h('p',{id:'workspace-availability-reason',class:'hint',text:available?.reason}),
      h('p',{id:'workspace-worker-support',class:data.worker_streaming.ready?'hint':'workspace-warning',text:data.worker_streaming.reason}),
      h('div',{class:'toolbar'},[field('Conversation history',select),button('New conversation','chat-new'),button('Test Connection','endpoint-test',{'data-endpoint':ep.id})]),
      h('section',{id:'chat-transcript','aria-label':'Conversation'},transcript()),form,
      h('details',{id:'model-agent-activity',class:'workspace-agent-details'},agentBody()),
      h('details',{},[h('summary',{text:'Runtime details'}),health(ep),h('p',{class:'hint',text:data.context_note})]));
  }
  async function refreshConversation(){
    if(polling||!conversation||selection().endpointView||getState().route.section!=='models'||document.hidden)return;
    polling=true;const id=conversation.id,target=routeKey();
    try {const next=await request(`/api/conversations/${id}`);if(target!==routeKey())return;conversation=next;data.worker_streaming=next.worker_streaming;
      const node=document.querySelector('#chat-transcript');if(node)node.replaceChildren(transcript());
      const send=document.querySelector('#chat-form button[type=submit]');if(send)send.disabled=Boolean(active());
    }catch(e){setNotice(`Conversation connection lost: ${e.message}`,'error');}finally{polling=false;}
  }
  async function send(form){
    const content=form.elements.content.value,max_tokens=Number(form.elements.max_tokens.value),target=routeKey();
    const sendButton=form.querySelector('button[type=submit]');sendButton.disabled=true;
    try {
      if(!conversation){const created=await request('/api/conversations',{method:'POST',body:JSON.stringify({endpoint_id:selection().endpoint,model_identifier:selection().model})});conversation={...created,turns:[]};}
      await request(`/api/conversations/${conversation.id}/turns`,{method:'POST',body:JSON.stringify({content,max_tokens})});
      if(target!==routeKey())return;
      form.elements.content.value='';getState().dirty=false;
      const next=linkModel(selection().endpoint,selection().model,conversation.id);
      if(location.hash===next){await loadRoute(true);}else location.hash=next;
    }catch(e){form.querySelector('#chat-error').textContent=e.message;}finally{if(sendButton.isConnected)sendButton.disabled=Boolean(active());}
  }
  async function action(kind,node){
    try {
      if(kind==='endpoint-test'){node.disabled=true;const endpoint=selection().endpoint||node.dataset.endpoint;await request(`/api/endpoints/${endpoint}/test`,{method:'POST',body:'{}'});await loadRoute(true);setNotice('Connection check recorded. No inference was initiated.');}
      else if(kind==='workspace-reload')await loadRoute(true);
      else if(kind==='chat-new'){const form=document.querySelector('#chat-form');if(form)form.elements.content.value='';getState().dirty=false;conversation=null;location.hash=linkModel(selection().endpoint,selection().model);await loadRoute(true);conversation=null;renderModel(true);}
      else if(kind==='chat-stop'){await request(`/api/conversations/${conversation.id}/cancel`,{method:'POST',body:JSON.stringify({task_id:node.dataset.task})});await refreshConversation();}
    }catch(e){setNotice(e.message,'error');}finally{node.disabled=false;}
  }
  document.addEventListener('submit',e=>{if(e.target.id==='chat-form'){e.preventDefault();send(e.target);}});
  document.addEventListener('click',e=>{const node=e.target.closest('[data-action]');if(node&&['endpoint-test','workspace-reload','chat-new','chat-stop'].includes(node.dataset.action)){e.preventDefault();action(node.dataset.action,node);}});
  document.addEventListener('change',e=>{if(e.target.dataset.action==='chat-history'){conversation=null;location.hash=linkModel(selection().endpoint,selection().model,e.target.value||null);}});
  setInterval(()=>{if(active())refreshConversation();},1000);
  return {renderModel,renderEndpoint,loadRoute,reset,refreshConversation,refreshMetadata};
}
