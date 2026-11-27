/* User-authored workflow drafts. Saving and execution are separate actions. */
"use strict";
(() => {
  const $ = id => document.getElementById(id), clone = value => JSON.parse(JSON.stringify(value));
  let context = null, draft = null, history = [], dirty = false, busy = false, selected = null;
  const el = (tag, text, cls) => { const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;n.dir="auto";return n; };
  function announce(message) { $("editor-error").textContent=message||"";$("editor-error").hidden=!message; }
  function changed(fn) { history.push(clone(draft));if(history.length>30)history.shift();fn();dirty=true;draw(); }
  function syncReviews() { for(const t of draft.tasks) if(t.role==="implementation")t.review_required=draft.tasks.some(r=>r.role==="review"&&r.deps.includes(t.id)); }
  function draw() {
    syncReviews();$("editor-count").textContent=`${draft.tasks.length} / 8 steps${dirty?" · Unsaved changes":""}`;
    $("editor-add").disabled=busy||draft.tasks.length>=8;$("editor-undo").disabled=busy||!history.length;
    window.MissionGraph.render($("editor-canvas"),draft.tasks,id=>inspect(id),{
      editable:!busy,layoutKey:`${context.mission.id}:draft`,departments:draft.departments,
      onConnect:(from,to)=>{changed(()=>draft.tasks.find(t=>t.id===to).deps.push(from));if(selected)inspect(selected);}
    });
  }
  function field(form,name,label,value,kind="textarea",required=false) {
    const wrap=el("div",undefined,"field"),id=`step-${name}`,input=el(kind);input.id=id;input.name=name;input.dir="auto";
    if(kind==="textarea")input.rows=3;else input.type="text";
    input.value=Array.isArray(value)?value.join("\n"):value||"";input.required=required;
    const lab=el("label",label);lab.htmlFor=id;wrap.append(lab,input);form.append(wrap);return input;
  }
  function lines(value){return String(value||"").split("\n").map(s=>s.trim()).filter(Boolean);}
  function inspect(id) {
    selected=id;const task=draft.tasks.find(t=>t.id===id);if(!task)return;
    const panel=$("detail-panel");panel.hidden=false;panel.classList.add("is-open");document.querySelector(".workspace-body").classList.remove("detail-collapsed");
    $("drawer-backdrop").hidden=false;$("drawer-backdrop").classList.add("is-visible");$("detail-title").textContent=`Edit ${id}`;
    const body=$("detail-body"),form=el("form",undefined,"step-form");form.id="step-form";body.replaceChildren(form);
    field(form,"title","Step title",task.title,"input",true);
    const wrap=el("div",undefined,"field"),lab=el("label","Agent"),select=el("select");select.id="step-agent";select.name="agent";lab.htmlFor=select.id;
    for(const agent of context.agents){const option=el("option",`${agent.name}${agent.eligible?"":` · ${agent.reason}`}`);option.value=agent.id;option.disabled=!agent.eligible;select.append(option);}
    select.value=task.agent;wrap.append(lab,select);form.append(wrap);
    field(form,"outputs","Output files · one path per line",task.outputs,"textarea",true);
    field(form,"acceptance","Done when · one condition per line",task.acceptance,"textarea",true);
    const deps=el("fieldset",undefined,"step-dependencies");deps.append(el("legend","Starts after"));
    for(const previous of draft.tasks.filter(t=>t.id!==id)){
      const label=el("label"),check=el("input");check.type="checkbox";check.checked=task.deps.includes(previous.id);
      check.disabled=busy||(!check.checked&&!window.MissionGraph.canConnect(draft.tasks,previous.id,id));
      label.append(check,document.createTextNode(`${previous.id} · ${previous.title}`));deps.append(label);
      check.addEventListener("change",()=>{changed(()=>{task.deps=check.checked?[...task.deps,previous.id]:task.deps.filter(d=>d!==previous.id);});inspect(id);});
    }
    if(draft.tasks.length===1)deps.append(el("p","This step begins with the mission.","field-hint"));form.append(deps);
    const advanced=el("details",undefined,"step-advanced");advanced.append(el("summary","Execution details"));
    field(advanced,"write_scope","Files this step may change",task.write_scope);
    field(advanced,"inputs","Input files",task.inputs);field(advanced,"validation","Validation commands",task.validation);
    const roles=el("select");roles.name="role";roles.id="step-role";
    for(const [value,label] of [["implementation","Work"],["analysis","Analysis"],["review","Independent review"]]){const option=el("option",label);option.value=value;roles.append(option);}roles.value=task.role;
    const roleWrap=el("div",undefined,"field"),roleLabel=el("label","Step type");roleLabel.htmlFor=roles.id;roleWrap.append(roleLabel,roles);advanced.append(roleWrap);
    form.append(advanced);
    function apply(){
      const values=Object.fromEntries(new FormData(form));
      const updated={...task,title:values.title,agent:values.agent||task.agent,role:values.role,
        outputs:lines(values.outputs),acceptance:lines(values.acceptance),inputs:lines(values.inputs),validation:lines(values.validation),write_scope:lines(values.write_scope)};
      if(!updated.write_scope.length||JSON.stringify(updated.write_scope)===JSON.stringify(task.outputs))updated.write_scope=[...updated.outputs];
      if(updated.agent!==task.agent)updated.model="unknown";
      if(JSON.stringify(updated)!==JSON.stringify(task))changed(()=>Object.assign(task,updated));
    }
    form.addEventListener("change",event=>{if(!deps.contains(event.target))apply();});
    form.addEventListener("submit",event=>{event.preventDefault();apply();});
    const remove=el("button","Remove step","btn btn--danger");remove.type="button";remove.disabled=busy;
    remove.addEventListener("click",()=>{changed(()=>{draft.tasks=draft.tasks.filter(t=>t.id!==id);draft.tasks.forEach(t=>{t.deps=t.deps.filter(d=>d!==id);});});selected=null;context.callbacks.onCloseDetail();});body.append(remove);
    if(busy)form.querySelectorAll("input,textarea,select,button").forEach(n=>{n.disabled=true;});
  }
  function add(){
    if(busy||draft.tasks.length>=8)return;let n=1;while(draft.tasks.some(t=>t.id===`T${n}`))n++;
    const agent=context.agents.find(a=>a.eligible&&a.id===context.mission.coordinator)||context.agents.find(a=>a.eligible);
    const id=`T${n}`;changed(()=>draft.tasks.push({id,title:"New step",department:draft.departments[0].id,role:"implementation",agent:agent?.id||context.mission.coordinator,model:"unknown",deps:[],write_scope:[],inputs:[],outputs:[],acceptance:[],validation:[],review_required:false}));inspect(id);
  }
  function open(mission,agents,callbacks){
    context={mission,agents,callbacks};history=[];dirty=false;busy=false;selected=null;
    draft=mission.plan?clone(mission.plan):{schema_version:1,mission_summary:mission.objective,architecture:"Work follows the dependencies shown in this workflow.",selection_rationale:"Agents selected by the user from current eligibility evidence.",departments:[{id:"work",name:"Work"}],tasks:[],quality_gates:[{id:"outputs",description:mission.quality_requirements,evidence:"Declared outputs and validation results"}],risks:[]};
    $("plan-editor").hidden=false;$("plan-content").hidden=true;$("plan-empty").hidden=true;$("editor-goal").value=draft.mission_summary;announce("");callbacks.onState();draw();
  }
  function close(discard=true){
    if(!context)return true;if(busy)return false;
    if(discard&&dirty&&!window.confirm("Discard the unsaved workflow changes?"))return false;
    const callbacks=context.callbacks;context=null;draft=null;dirty=false;selected=null;$("plan-editor").hidden=true;callbacks.onCloseDetail();callbacks.onState();return true;
  }
  async function save(){
    if(!context||busy)return;document.getElementById("step-form")?.dispatchEvent(new Event("change",{bubbles:true}));
    const form=$("step-form");if(form&&!form.reportValidity())return;
    announce("");busy=true;context.callbacks.onBusy(true);draw();
    $("step-form")?.querySelectorAll("input,textarea,select,button").forEach(n=>{n.disabled=true;});
    for(const id of ["editor-save","editor-cancel","editor-goal"])$(id).disabled=true;
    try{
      const mission=await context.callbacks.onSave(clone(draft),context.mission.plan_version);
      const callbacks=context.callbacks;busy=false;dirty=false;close(false);callbacks.onSaved(mission);
    }catch(error){announce(error.message);}finally{
      busy=false;["editor-save","editor-cancel","editor-goal"].forEach(id=>{$(id).disabled=false;});
      if(context){context.callbacks.onBusy(false);draw();if(selected)inspect(selected);}else window.dispatchEvent(new Event("missiondeck-editor-idle"));
    }
  }
  $("editor-add").addEventListener("click",add);$("editor-save").addEventListener("click",save);
  $("editor-cancel").addEventListener("click",()=>{if(!context)return;const callbacks=context.callbacks;if(close(true))callbacks.onDiscard();});
  $("editor-undo").addEventListener("click",()=>{if(busy||!history.length)return;draft=history.pop();dirty=true;$("editor-goal").value=draft.mission_summary;draw();if(selected&&draft.tasks.some(t=>t.id===selected))inspect(selected);else context.callbacks.onCloseDetail();});
  $("editor-goal").addEventListener("change",()=>{if(context)changed(()=>{draft.mission_summary=$("editor-goal").value;});});
  window.addEventListener("beforeunload",event=>{if(context&&dirty){event.preventDefault();event.returnValue="";}});
  window.MissionPlanner={open,close,isOpen:()=>Boolean(context),isDirty:()=>dirty};
})();
