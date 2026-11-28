/* Browser coverage for peer managers, local images and honest artifact flow. */
const {test,before,after}=require('node:test');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require('playwright');
let server,browser,base;
before(async()=>{const root=path.resolve(__dirname,'../app/static');server=http.createServer((req,res)=>{const name=req.url==='/'?'index.html':req.url.replace(/^\/static\//,'').replace(/^\//,'').split('?')[0];if(!/^[a-z.-]+\.(html|js|css|json)$/.test(name)||!fs.existsSync(path.join(root,name))){res.writeHead(404);res.end();return;}res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':name.endsWith('.json')?'application/json':'text/html');res.end(fs.readFileSync(path.join(root,name)));});await new Promise(r=>server.listen(0,'127.0.0.1',r));base=`http://127.0.0.1:${server.address().port}`;browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();await new Promise(r=>server?.close(r));});
function workflow(){return {version:2,input:{text:'یک خروجی قابل بررسی بساز',files:[]},delivery:{sources:['ENG'],inspector:'QA'},blocks:[
 {id:'MGR',name:'Management',kind:'management',specialty:'Planning',goal:'Consult and consolidate',deps:[],advisors:[{agent:'cursor',model:'unknown'},{agent:'opencode',model:'opencode/free-one'}],position:{x:100,y:130}},
 {id:'ENG',name:'Engineering',kind:'specialist',specialty:'Engineering',goal:'Build report',deps:['MGR'],supervisor:{agent:'opencode',model:'opencode/free-one'},employees:[{agent:'opencode',model:'opencode/free-one',instruction:'Write a report',outputs:['out/report.md'],acceptance:['Checked']}],position:{x:470,y:130}},
 {id:'QA',name:'Inspection',kind:'inspection',specialty:'Quality assurance',goal:'Inspect the final output',deps:['ENG'],inspector:{agent:'cursor',model:'unknown'},position:{x:840,y:130}}
]};}
function mission(extra={}){return {id:'mis_council',title:'تیم پژوهش و توسعه',objective:'Deliver a checked report',status:'draft',plan:null,plan_version:0,tasks:[],artifacts:[],active_dispatches:0,global_active_dispatches:0,can:{save_plan:true,plan:true,approve:false,start:false,cancel:false},...extra};}
async function setup(initial=mission()){
 const context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage(),calls=[],errors=[];
 const state={mission:initial,turns:[],pin:'opencode/free-one',connections:[],apiTests:0};page.on('pageerror',e=>errors.push(e.message));
 const agents=()=>[{id:'cursor',name:'Cursor',icon_key:'cursor',installed:true,eligible:true,model:'unknown',billing:'included_plan',quota:null,reason:'Free included plan'},
 {id:'opencode',name:'OpenCode',icon_key:'opencode',installed:true,eligible:true,model:state.pin,billing:'free_models',quota:null,reason:'Verified free model'},
 {id:'claude',name:'Claude',icon_key:'claude',installed:true,eligible:false,model:'unknown',billing:'api_key_metered',quota:null,reason:'Paid route blocked'},
 {id:'codex',name:'Codex',icon_key:'openai',installed:true,eligible:false,model:'unknown',billing:'quota',quota:null,reason:'Quota unknown'},
 {id:'gemini',name:'Gemini CLI',icon_key:'gemini',installed:false,eligible:false,model:'unknown',billing:'unverified',quota:null,reason:'Not reported installed'}];
 await page.route('**/api/**',async route=>{const req=route.request(),p=new URL(req.url()).pathname,method=req.method(),body=req.postDataJSON();calls.push({p,method,body,token:req.headers()['x-cc-token']});const send=(d,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(d)});
 if(p==='/api/session')return send({token:'council-csrf'});
 if(p==='/api/capabilities')return send({agents:agents(),worker_cap:2});
 if(p==='/api/agent-catalog')return send({agents:agents(),models:[...['opencode/free-one','opencode/free-two'].map(id=>({id,agent:'opencode',icon_key:'opencode',eligible:true})),...state.connections.map(r=>({id:r.route,name:r.name,agent:'opencode',icon_key:'opencode',source:'user_api',eligible:r.verified}))]});
 if(p==='/api/api-models/selection'){state.pin=body.model;return send({selected:state.pin});}
 if(p==='/api/api-models'){
   if(method==='POST'){const id='mdapi_1111111111111111',copy={...body};delete copy.api_key;state.connections=[{...copy,id,route:id+'/'+body.model,has_key:true,verified:false,status:'needs_test',price:null,quota:null}];}
   return send({connections:state.connections});
 }
 if(p==='/api/api-models/mdapi_1111111111111111/test'){state.apiTests++;state.connections[0].verified=true;state.connections[0].status='enabled';return send({connections:state.connections});}
 if(p==='/api/api-models/mdapi_1111111111111111'&&method==='DELETE'){state.connections=[];return send({connections:[]});}
 if(p==='/api/missions/mis_council/team-workflow'){state.mission=mission({plan:{team_workflow:body.plan,tasks:[]},plan_version:state.mission.plan_version+1,status:'awaiting_approval',can:{save_plan:true,approve:true,plan:false,start:false,cancel:false}});return send(state.mission);}
 if(p==='/api/free-models/selection'){state.pin=body.model;return send({selected:state.pin});}
 if(p==='/api/missions'){if(method==='POST'){state.mission=mission({title:body.title,objective:body.objective});return send(state.mission);}return send({missions:state.mission?[state.mission]:[]});}
 if(p==='/api/missions/mis_council')return send(state.mission);
 if(p==='/api/missions/mis_council/data-flow')return send({checked_at:'2026-11-27T19:00:00Z',edges:state.mission?.status==='running'?[{source:'MGR',target:'ENG',state:'processing_inputs',files:[{path:'.orca-runs/mis_council/teams/MGR/manager.md',recorded:true,size:42,sha256:'abc123',artifact_id:'art_manager'}]}]:[]});
 if(p==='/api/missions/mis_council/manager')return send({turns:state.turns,global_active_dispatches:0});
 if(p.endsWith('/manager/messages')){state.turns.push({id:'turn_browser',block_id:body.block_id,message:body.message,status:'consulting',advisors:body.advisors.map(a=>({...a,status:'running'})),child_mission_id:'mis_child'});return send({turns:state.turns,global_active_dispatches:2});}
 if(p.endsWith('/manager/refresh')){state.turns=state.turns.map(t=>({...t,status:'ready',advisors:t.advisors.map(a=>({...a,status:'succeeded'})),reply:'برنامه آمادهٔ بررسی است. ابتدا خروجی را تعریف می‌کنیم، سپس تیم تخصصی و بازرس مستقل آن را بررسی می‌کنند.',proposal_available:true}));return send({turns:state.turns,global_active_dispatches:0});}
 if(p.endsWith('/manager/turn_browser/apply')){state.mission=mission({plan:{team_workflow:workflow(),tasks:[]},plan_version:1,status:'awaiting_approval',can:{save_plan:true,approve:true,plan:false,start:false,cancel:false}});return send(state.mission);}
 if(p.endsWith('/manager/stop')){state.turns=state.turns.map(t=>({...t,status:'failed',error:'Stopped by user'}));return send({turns:state.turns,global_active_dispatches:0});}
 return send({error:{message:`Unexpected API ${p}`}},404);
 });
 await page.goto(base+'/teams.html');await page.waitForFunction(()=>!document.getElementById('refresh').disabled);await page.waitForFunction(()=>document.querySelectorAll('.team-node').length===0||document.getElementById('world').children.length>0);
 return {context,page,calls,errors,state};
}
async function screenshot(page,name){if(process.env.MISSIONDECK_SCREENSHOT_DIR){fs.mkdirSync(process.env.MISSIONDECK_SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.MISSIONDECK_SCREENSHOT_DIR,name),fullPage:true});}}

test('image catalogue lists unavailable agents honestly, filters locally and performs no inference',async()=>{
 const x=await setup(),{page,calls}=x;
 await page.locator('#agent-library').click();await page.waitForSelector('.catalog-card');assert.equal(await page.locator('.catalog-card').count(),5);
 await page.waitForFunction(()=>[...document.querySelectorAll('.catalog-card img')].every(i=>i.complete&&i.naturalWidth>0));
 assert.match(await page.locator('.catalog-card').filter({hasText:'Claude'}).innerText(),/Paid route blocked/);
 await screenshot(page,'agent-gallery.png');
 await page.locator('#catalog-filter').selectOption('eligible');assert.equal(await page.locator('.catalog-card').count(),2);
 await page.locator('#agent-search').fill('Cursor');assert.equal(await page.locator('.catalog-card').count(),1);
 assert.equal(calls.filter(c=>c.method!=='GET').length,0);assert.deepEqual(x.errors,[]);await x.context.close();
});

test('management contains consulting AIs without staffing; image picker selects a distinct free model',async()=>{
 const x=await setup(),{page}=x;await page.locator('[data-kind="management"]').click();
 assert.equal(await page.getByLabel('Number of employees (1–8)').count(),0);assert.equal(await page.getByRole('group',{name:'AI supervisor'}).count(),0);
 assert.deepEqual(x.errors,[]);assert.equal(await page.getByRole('group',{name:'Council AI 1',exact:true}).count(),1,await page.locator('#inspector-body').innerText());await page.getByRole('group',{name:'Council AI 1',exact:true}).getByLabel('Agent',{exact:true}).selectOption('cursor');
 await page.getByRole('button',{name:'Add consulting AI',exact:true}).click();
 await page.getByRole('group',{name:'Council AI 2',exact:true}).getByRole('button',{name:'Browse AI images and models'}).click();
 await page.locator('#catalog-filter').selectOption('models');
 await page.locator('.catalog-card').filter({hasText:'opencode/free-two'}).getByRole('button',{name:'Use this free model'}).click();
 await page.waitForFunction(()=>!document.getElementById('agent-dialog').open);
 assert.match(await page.getByRole('group',{name:'Council AI 2',exact:true}).innerText(),/opencode\/free-two/);
 assert.equal(await page.locator('.satellite.council-member').count(),2);assert.equal(await page.locator('.satellite.supervisor').count(),0);
 assert.deepEqual(x.errors,[]);await x.context.close();
});

test('manager chat persists a Persian message, returns a verified reply and applies only an unapproved proposal',async()=>{
 const x=await setup(),{page,calls}=x;await page.locator('#open-manager').click();
 assert.equal(calls.filter(c=>c.method!=='GET').length,0);
 await page.locator('#manager-message').fill('برای ساختن گزارش با دو AI مشورت کن');await page.locator('#manager-message').press('Enter');
 await page.waitForFunction(()=>!document.getElementById('refresh').disabled);
 assert.match(await page.locator('#chat-history').innerText(),/برای ساختن گزارش/);
 const sent=calls.find(c=>c.p.endsWith('/manager/messages'));assert.equal(sent.token,'council-csrf');assert.equal(sent.body.workflow.version,3);assert.ok(sent.body.request_id);
 await page.locator('#check-replies').click();await page.waitForFunction(()=>!document.getElementById('refresh').disabled);
 assert.match(await page.locator('#chat-history').innerText(),/برنامه آماده/);
 await screenshot(page,'manager-council.png');
 page.once('dialog',dialog=>dialog.accept());await page.getByRole('button',{name:'Use proposed workflow'}).click();
 await page.waitForFunction(()=>document.getElementById('mission-status').textContent==='awaiting_approval');
 assert.equal(calls.filter(c=>c.p.endsWith('/approve')||c.p.endsWith('/start')).length,0);
 assert.equal(await page.locator('.team-node').count(),3);assert.equal(await page.locator('.endpoint-node').count(),2);
 await page.locator('#close-manager').click();await page.locator('#open-manager').click();assert.match(await page.locator('#chat-history').innerText(),/برنامه آماده/);
 assert.deepEqual(x.errors,[]);await x.context.close();
});

test('input, final delivery, independent inspector and saved-file flow are directly inspectable',async()=>{
 const w=workflow(),tasks=[];for(const b of w.blocks){for(const tid of b.kind==='management'?[`${b.id}_advice1`,`${b.id}_advice2`,`${b.id}_check`]:b.kind==='specialist'?[`${b.id}_brief`,`${b.id}_w1`,`${b.id}_check`]:[`${b.id}_check`])tasks.push({id:tid,team_block:b.id});}
 const m=mission({status:'running',plan:{team_workflow:w,tasks},tasks:tasks.map(t=>({plan_task_id:t.id,status:t.team_block==='MGR'?'succeeded':t.id==='ENG_brief'?'running':'pending'})),active_dispatches:1,can:{save_plan:false,plan:false,cancel:true}});
 const x=await setup(m),{page,calls}=x;await page.waitForSelector('.flow-active',{state:'attached'});assert.ok(await page.locator('.flow-active').evaluate(n=>n.getBoundingClientRect().width>0&&parseFloat(getComputedStyle(n).strokeWidth)>0));
 assert.equal(await page.locator('.endpoint-node').count(),2);assert.equal(await page.locator('.satellite.independent-inspector').count(),1);
 await page.locator('#fit').click();await page.locator('#flow-list').click();await page.getByRole('button',{name:/Management → Engineering/}).click();
 assert.match(await page.locator('#inspector-body').innerText(),/manager\.md/);assert.match(await page.locator('#inspector-body').innerText(),/SHA-256 abc123/);
 await page.emulateMedia({reducedMotion:'reduce'});assert.equal(await page.locator('.flow-active').evaluate(n=>getComputedStyle(n).animationName),'none');
 assert.equal(calls.filter(c=>c.method!=='GET').length,0);await screenshot(page,'data-handoffs.png');assert.deepEqual(x.errors,[]);await x.context.close();
});


test('new blocks have zero automatic wires; explicit drag, removal, undo and reload preserve only user links',async()=>{
 const x=await setup(),{page,calls,state}=x;
 await page.locator('[data-kind="specialist"]').click();await page.locator('#close-inspector').click();await page.locator('#fit').click();
 assert.equal(await page.locator('.connection').count(),0);
 await page.locator('#save').click();await page.waitForFunction(()=>!document.getElementById('refresh').disabled);
 assert.match(await page.locator('#notice').innerText(),/connect a path/);assert.equal(calls.filter(c=>c.p.endsWith('/team-workflow')).length,0);
 const from=await page.getByRole('button',{name:'Output Mission input',exact:true}).boundingBox(),to=await page.getByRole('button',{name:'Input Specialist team',exact:true}).boundingBox();
 await page.mouse.move(from.x+from.width/2,from.y+from.height/2);await page.mouse.down();await page.mouse.move(to.x+to.width/2,to.y+to.height/2,{steps:10});await page.mouse.up();
 await page.getByRole('button',{name:'Output Specialist team',exact:true}).click();await page.getByRole('button',{name:'Input Final delivery',exact:true}).click();
 assert.equal(await page.locator('.connection').count(),2);
 await page.locator('#flow-list').click();await page.getByRole('button',{name:/Mission input → Specialist team/}).click();await page.getByRole('button',{name:'Remove connection',exact:true}).click();
 assert.equal(await page.locator('.connection').count(),1);await page.locator('#close-inspector').click();await page.locator('#undo').click();assert.equal(await page.locator('.connection').count(),2);
 await page.locator('#save').click();await page.waitForFunction(()=>!document.getElementById('refresh').disabled);
 assert.deepEqual(state.mission.plan.team_workflow.connections,[{source:'__input',target:'B1'},{source:'B1',target:'__output'}]);
 await page.reload();await page.waitForFunction(()=>!document.getElementById('refresh').disabled);assert.equal(await page.locator('.connection').count(),2);
 await page.locator('[data-kind="inspection"]').click();assert.equal(await page.locator('.connection').count(),2);assert.equal(await page.locator('.team-node').count(),2);
 assert.deepEqual(x.errors,[]);assert.equal(calls.filter(c=>c.p.endsWith('/start')).length,0);await x.context.close();
});

test('API form saves a write-only key without inference, tests explicitly, and selects the exact model for a council role',async()=>{
 const x=await setup(),{page,calls,state}=x;
 await page.locator('#api-models').click();await page.waitForFunction(()=>document.getElementById('api-dialog').open&&!document.getElementById('save-api-model').disabled);
 assert.equal(calls.filter(c=>c.method!=='GET').length,0);
 await page.locator('#api-form [name="name"]').fill('My private research model');await page.locator('#api-form [name="base_url"]').fill('https://api.provider.example/v1');await page.locator('#api-form [name="model"]').fill('research/model-one');await page.locator('#api-form [name="api_key"]').fill('test-only-key-never-returned');
 await page.locator('#save-api-model').click();await page.waitForFunction(()=>!document.getElementById('test-api-model').disabled);
 assert.equal(state.apiTests,0);assert.equal(await page.locator('#api-form [name="api_key"]').inputValue(),'');assert.doesNotMatch(await page.locator('#api-dialog').innerText(),/test-only-key-never-returned/);
 assert.match(await page.locator('#api-model-message').innerText(),/No model request/);
 await page.locator('#test-api-model').click();await page.waitForFunction(()=>!document.getElementById('test-api-model').disabled);assert.equal(state.apiTests,1);
 assert.match(await page.locator('#api-connections').innerText(),/Tested/);await page.setViewportSize({width:1366,height:768});const resultBox=await page.locator('#api-model-message').boundingBox(),useBox=await page.locator('#use-api-model').boundingBox();assert.ok(resultBox.y+resultBox.height<=768,'API test result must fit the laptop viewport');assert.ok(useBox.y+useBox.height<=768,'API selection must fit the laptop viewport');await screenshot(page,'api-model-settings.png');
 await page.locator('#close-api-models').click();await page.locator('[data-kind="management"]').click();
 await page.getByRole('group',{name:'Council AI 1',exact:true}).getByRole('button',{name:'Browse AI images and models'}).click();await page.locator('#catalog-filter').selectOption('apis');
 await page.getByRole('button',{name:'Use this API model',exact:true}).click();await page.waitForFunction(()=>!document.getElementById('agent-dialog').open);
 assert.match(await page.getByRole('group',{name:'Council AI 1',exact:true}).innerText(),/mdapi_1111111111111111\/research\/model-one/);
 assert.equal(calls.filter(c=>c.p.endsWith('/manager/messages')||c.p.endsWith('/start')).length,0);assert.deepEqual(x.errors,[]);await x.context.close();
});

test('manual connection controls include endpoints, reject cycles and keep reduced motion for native flow',async()=>{
 const x=await setup(),{page}=x;await page.locator('[data-kind="management"]').click();await page.locator('[data-kind="specialist"]').click();await page.locator('[data-kind="inspection"]').click();
 assert.equal(await page.locator('.connection').count(),0);await page.locator('#flow-list').click();
 for(const[source,target]of[['__input','B1'],['B1','B2'],['B2','B3'],['B3','__output']]){await page.getByLabel('Connection source',{exact:true}).selectOption(source);await page.getByLabel('Connection target',{exact:true}).selectOption(target);await page.getByRole('button',{name:'Add connection',exact:true}).click();}
 assert.equal(await page.locator('.connection').count(),4);await page.getByLabel('Connection source',{exact:true}).selectOption('B2');await page.getByLabel('Connection target',{exact:true}).selectOption('B1');await page.getByRole('button',{name:'Add connection',exact:true}).click();assert.equal(await page.locator('.connection').count(),4);
 await page.locator('#close-flow-list').click();await page.locator('#close-inspector').click();await page.keyboard.press('Escape');await page.locator('#fit').click();await screenshot(page,'manual-team-workflow.png');assert.deepEqual(x.errors,[]);await x.context.close();
});
