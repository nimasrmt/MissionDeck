/* Provider discovery reads model metadata only. Keys are never probed across services. */
"use strict";
(() => {
  const $=id=>document.getElementById(id), form=$("api-form"), dialog=$("api-dialog");
  const state={connections:[],providers:[],models:[],selected:null,busy:false,dirty:false};
  const request=(...args)=>window.missiondeckAPI(...args), field=name=>form.elements.namedItem(name);
  const say=text=>{$("api-model-message").textContent=text;};
  const element=(tag,text,cls)=>{const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;};
  const preset=()=>state.providers.find(p=>p.id===field("provider").value);
  function controls(){
    for(const n of dialog.querySelectorAll("input,select,button"))n.disabled=state.busy;
    const chosen=Boolean(field("model").value),provider=Boolean(field("provider").value);
    $("test-api-model").disabled=state.busy||state.dirty||!state.selected;
    $("save-api-model").disabled=state.busy||!chosen||!provider;
    $("load-api-models").disabled=state.busy||!provider;
    field("model").disabled=state.busy||!state.models.length;
    $("api-model-search").disabled=state.busy||!state.models.length;
    field("base_url").readOnly=Boolean(preset());field("protocol").disabled=state.busy||Boolean(preset());
    $("delete-api-model").hidden=!state.selected;
    $("use-api-model").hidden=!state.selected?.verified||state.dirty;
    $("save-api-model").textContent=state.busy?"Working…":"Save connection";
    $("unknown-api-help").hidden=provider;
  }
  async function run(fn){if(state.busy)return;state.busy=true;say("");controls();try{await fn();}catch(e){say(e.message);}finally{state.busy=false;controls();}}
  function paintModels(selected=field("model").value){
    const query=$("api-model-search").value.trim().toLowerCase(),select=field("model");select.replaceChildren(new Option("Choose a model from the service list",""));
    let shown=0;
    for(const model of state.models){if(query&&!(`${model.name} ${model.id}`).toLowerCase().includes(query)&&model.id!==selected)continue;const option=new Option(model.name===model.id?model.id:`${model.name} · ${model.id}`,model.id);select.append(option);shown++;}
    if(state.models.some(m=>m.id===selected))select.value=selected;
    $("api-model-count").textContent=state.models.length?`${shown} shown · ${state.models.length} reported models · availability needs a test`:"Load models to see the service's current names.";
  }
  function choose(row){
    state.selected=row||null;state.dirty=false;state.models=[];form.reset();$("api-model-search").value="";
    for(const key of ["id","name","protocol","base_url","context_budget","output_budget"]){if(row&&row[key]!=null)field(key).value=row[key];}
    const pid=row?.provider||state.providers.find(p=>p.base_url===row?.base_url)?.id||(row?"custom":"");field("provider").value=pid;
    if(row?.model)state.models=[{id:row.model,name:row.model}];paintModels(row?.model||"");field("api_key").value="";
    $("api-key-note").textContent=row?.has_key?"Key saved locally. Leave blank to reuse it for this service.":"Paste the key from the selected service. It is never returned by settings.";
    paint();controls();
  }
  function paint(){const host=$("api-connections");host.replaceChildren();for(const row of state.connections){const card=element("button","",`api-connection${row.id===state.selected?.id?" selected":""}`);card.type="button";card.disabled=state.busy;card.append(element("b",row.name),element("small",row.model),element("span",row.verified?"✓ Tested · enabled":"○ Needs test",row.verified?"api-enabled":"api-pending"));card.onclick=()=>{say("");choose(row);};host.append(card);}if(!state.connections.length)host.append(element("p","Choose your API service, load its model list, then select the model you want.","hint"));}
  function update(data,id){state.connections=data.connections||[];choose(state.connections.find(r=>r.id===id));window.dispatchEvent(new Event("missiondeck-routes-changed"));}
  function paintProviders(){const select=field("provider");select.replaceChildren(new Option("Choose service / سرویس را انتخاب کن",""));for(const row of state.providers)select.append(new Option(row.name,row.id));select.append(new Option("Custom / private gateway","custom"));}
  const open=(fresh=false)=>{if(state.busy)return;choose(fresh?null:state.connections[0]);if(!dialog.open)dialog.showModal();return run(async()=>{say("Loading saved connections…");const [data,presets]=await Promise.all([request("/api/api-models"),request("/api/api-model-providers")]);state.connections=data.connections||[];state.providers=presets.providers||[];paintProviders();choose(fresh?null:state.connections[0]);say("");});};
  $("api-models").onclick=()=>open();$("api-quick-add").onclick=()=>open(true);window.addEventListener("missiondeck-open-api",()=>open(true));
  $("new-api-model").onclick=()=>{say("");choose(null);field("provider").focus();};
  $("close-api-models").onclick=()=>dialog.close();dialog.addEventListener("close",()=>{field("api_key").value="";});
  field("provider").onchange=()=>{const row=preset();field("base_url").value=row?.base_url||"";field("protocol").value=row?.protocol||"openai";state.models=[];paintModels("");state.dirty=true;$("api-advanced").open=field("provider").value==="custom";say(row?`${row.name} address filled automatically. Load models to choose one.`:"For a private gateway, its issuing service must supply the base URL.");controls();};
  form.addEventListener("input",e=>{if(e.target.id==="api-model-search")return;state.dirty=true;controls();});
  $("api-model-search").oninput=()=>paintModels();
  $("load-api-models").onclick=()=>run(async()=>{
    const body={provider:field("provider").value,base_url:field("base_url").value,protocol:field("protocol").value,api_key:field("api_key").value,connection_id:state.selected?.id||null};
    try{const selected=field("model").value;const result=await request("/api/api-models/discover","POST",body);state.models=result.models||[];paintModels(selected);say(state.models.length?`Loaded ${state.models.length} model names. Select one below.${result.truncated?" The service returned a partial catalog.":""} No generation request was sent.`:"The service returned no supported model names. Check the selected service and key.");}
    finally{body.api_key="";}
  });
  $("api-use-free").onclick=()=>{dialog.close();window.dispatchEvent(new Event("missiondeck-open-free"));};
  form.onsubmit=e=>{e.preventDefault();if(!form.reportValidity())return;const values=Object.fromEntries(new FormData(form)),id=values.id;delete values.id;values.protocol=field("protocol").value;values.context_budget=Number(values.context_budget);values.output_budget=Number(values.output_budget);run(async()=>{try{const before=new Set(state.connections.map(r=>r.id));const data=await request(id?`/api/api-models/${encodeURIComponent(id)}`:"/api/api-models",id?"PUT":"POST",values);update(data,id||data.connections.find(r=>!before.has(r.id))?.id);say("Saved locally. No generation request was sent. Test & enable when ready.");}finally{values.api_key="";field("api_key").value="";}});};
  $("test-api-model").onclick=()=>run(async()=>{const id=state.selected.id;try{update(await request(`/api/api-models/${encodeURIComponent(id)}/test`,"POST",{}),id);say("Tool-call test passed. The model is available for roles in your workflow.");}catch(e){try{update(await request("/api/api-models"),id);}catch{}throw e;}});
  $("use-api-model").onclick=()=>run(async()=>{await request("/api/api-models/selection","PUT",{model:state.selected.route});window.dispatchEvent(new Event("missiondeck-routes-changed"));say("Selected for new AI roles. Existing roles keep their model. Cost and quota remain unknown.");});
  $("delete-api-model").onclick=()=>run(async()=>{update(await request(`/api/api-models/${encodeURIComponent(state.selected.id)}`,"DELETE"));say("Connection and its local key files removed.");});
})();
