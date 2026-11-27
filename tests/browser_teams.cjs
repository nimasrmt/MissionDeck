/* Team canvas in a real browser; API fixtures never launch an agent. */
const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require('playwright');
let browser,server,base;
before(async()=>{const root=path.resolve(__dirname,'../app/static');server=http.createServer((req,res)=>{const name=req.url==='/'?'index.html':req.url.replace(/^\/static\//,'').replace(/^\//,'').split('?')[0];if(!/^[a-z.-]+\.(html|css|js)$/.test(name)||!fs.existsSync(path.join(root,name))){res.writeHead(404);res.end();return;}res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html');res.end(fs.readFileSync(path.join(root,name)));});await new Promise(r=>server.listen(0,'127.0.0.1',r));base=`http://127.0.0.1:${server.address().port}`;browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();await new Promise(r=>server?.close(r));});
const agent={agent:'cursor',model:'unknown'};
function workflow(){return {version:1,blocks:[{id:'B1',name:'Management',specialty:'Planning',kind:'management',goal:'Coordinate checked outputs',deps:[],supervisor:{...agent},employees:[{...agent,instruction:'Write the brief',outputs:['report.md'],acceptance:['Checked']}],position:{x:100,y:130}},{id:'B2',name:'Engineering',specialty:'Implementation',kind:'specialist',goal:'Implement the feature',deps:['B1'],supervisor:{...agent},employees:[{...agent,instruction:'Implement',outputs:['result.md'],acceptance:['Checked']}],position:{x:470,y:130}},{id:'B3',name:'Inspection',specialty:'Quality assurance',kind:'inspection',goal:'Inspect the outputs',deps:['B2'],supervisor:{...agent},employees:[{...agent,instruction:'Inspect',outputs:['findings.md'],acceptance:['Checked']}],position:{x:840,y:130}}]};}
function mission(extra={}){return {id:'mis_team',title:'Visual team test',objective:'Deliver checked output',status:'draft',plan_version:0,plan:null,active_dispatches:0,tasks:[],artifacts:[],can:{save_plan:true,plan:true,approve:false,start:false,cancel:false},...extra};}
async function setup(initial=mission(),url='/teams.html'){
 const context=await browser.newContext({viewport:{width:1366,height:768}}),page=await context.newPage(),calls=[],errors=[];
 let current=initial;const state={conflict:false};page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/api/**',async route=>{const req=route.request(),p=new URL(req.url()).pathname,method=req.method(),body=req.postDataJSON();calls.push({p,method,body,token:req.headers()['x-cc-token']});const send=(d,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(d)});
 if(p==='/api/session')return send({token:'team-token'});
 if(p==='/api/capabilities')return send({agents:[{id:'cursor',name:'Cursor',eligible:true,model:'unknown'},{id:'opencode',name:'OpenCode',eligible:false,model:'unknown',reason:'Check free route'}]});
 if(p==='/api/missions'){if(method==='POST'){current=mission({title:body.title,objective:body.objective});return send(current);}return send({missions:current?[current]:[]});}
 if(p==='/api/missions/mis_team/team-workflow'){
   if(state.conflict)return send({error:{message:'Saved plan changed',code:'version_conflict'}},409);
   current={...current,plan:{team_workflow:body.plan},plan_version:current.plan_version+1,status:'awaiting_approval',can:{...current.can,approve:true,plan:false}};return send(current);
 }
 if(p==='/api/missions/mis_team')return send(current);
 if(p==='/api/free-models/refresh'||p==='/api/free-models/selection')return send({status:'ok',reason:'Zero-price catalog checked',models:[{id:'opencode/example-free'}],selected:method==='PUT'?'opencode/example-free':null});
 if(p==='/api/free-models')return send({models:[]});
 return send({error:{message:`Unexpected ${p}`}},404);
 });
 await page.goto(base+url);await page.waitForSelector('#route-status');await page.waitForFunction(()=>!document.getElementById('refresh').disabled);return {context,page,calls,errors,state};
}
async function screenshot(page,name){if(process.env.MISSIONDECK_SCREENSHOT_DIR){fs.mkdirSync(process.env.MISSIONDECK_SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.MISSIONDECK_SCREENSHOT_DIR,name),fullPage:true});}}

test('build specialist blocks, choose employee count, connect dependencies, reject cycles, and save without inference',async()=>{
 const x=await setup(),{page,calls}=x;
 await page.locator('[data-kind="management"]').click();
 await page.locator('[data-kind="specialist"]').click();
 await page.getByLabel('Number of employees (1–8)').fill('2');await page.getByLabel('Number of employees (1–8)').press('Tab');
 await page.locator('[data-kind="inspection"]').click();
 await page.getByRole('button',{name:'Close settings'}).click();
 assert.equal(await page.locator('.team-node').count(),3);assert.equal(await page.locator('.satellite.supervisor').count(),3);assert.equal(await page.locator('.satellite.employee').count(),4);
 await page.getByRole('button',{name:'Output Management',exact:true}).click();await page.getByRole('button',{name:'Input Specialist team',exact:true}).click();
 await page.getByRole('button',{name:'Output Specialist team',exact:true}).click();await page.getByRole('button',{name:'Input Inspection',exact:true}).click();
 assert.equal(await page.locator('.connection').count(),2);
 await page.getByRole('button',{name:'Output Inspection',exact:true}).click();await page.getByRole('button',{name:'Input Management',exact:true}).click();
 assert.match(await page.locator('#notice').innerText(),/cycle/);assert.equal(await page.locator('.connection').count(),2);
 await page.getByRole('button',{name:'Save workflow',exact:true}).click();await page.waitForFunction(()=>document.getElementById('mission-status').textContent==='awaiting_approval');
 const save=calls.find(c=>c.p.endsWith('/team-workflow'));assert.equal(save.method,'PUT');assert.equal(save.token,'team-token');assert.equal(save.body.plan.blocks[1].employees.length,2);assert.deepEqual(save.body.plan.blocks[2].deps,['B2']);
 assert.equal(calls.filter(c=>c.method==='POST').length,0);assert.deepEqual(x.errors,[]);await page.getByRole('button',{name:'Fit',exact:true}).click();await screenshot(page,'team-canvas.png');await x.context.close();
});

test('root opens team canvas; provider setup only discovers and pins a free route',async()=>{
 const x=await setup(mission(),'/'),{page,calls}=x;
 await page.waitForURL('**/teams.html');await page.getByRole('button',{name:'Free model setup',exact:true}).click();await page.getByRole('button',{name:'Check free catalog',exact:true}).click();
 const choose=page.getByRole('button',{name:/opencode\/example-free/});await choose.click();await page.waitForFunction(()=>!document.getElementById('refresh').disabled);
 assert.deepEqual(calls.filter(c=>c.method!=='GET').map(c=>c.p),['/api/free-models/refresh','/api/free-models/selection']);
 assert.match(await choose.innerText(),/Selected/);assert.deepEqual(x.errors,[]);await x.context.close();
});

test('version conflict preserves the canvas and execution locks settings',async()=>{
 const x=await setup(),{page,state}=x;await page.locator('[data-kind="specialist"]').click();state.conflict=true;await page.getByRole('button',{name:'Save workflow',exact:true}).click();await page.waitForFunction(()=>!document.getElementById('refresh').disabled);assert.match(await page.locator('#notice').innerText(),/changed/);assert.equal(await page.locator('.team-node').count(),1);assert.match(await page.locator('#saved-state').innerText(),/Unsaved/);assert.deepEqual(x.errors,[]);await x.context.close();
 const live=mission({status:'running',active_dispatches:1,plan:{team_workflow:workflow(),tasks:[]},tasks:[{plan_task_id:'B1_brief',status:'running'}],can:{save_plan:false,plan:false,cancel:true}});
 live.plan.tasks=[{id:'B1_brief',team_block:'B1'}];
 const y=await setup(live);await y.page.getByRole('button',{name:'Configure Management',exact:true}).click();assert.equal(await y.page.getByLabel('Specialty',{exact:true}).isDisabled(),true);assert.equal(await y.page.locator('.team-node.running').count(),1);assert.equal(y.calls.filter(c=>c.method!=='GET').length,0);await screenshot(y.page,'team-execution.png');assert.deepEqual(y.errors,[]);await y.context.close();
});

test('canvas pans, zooms, keeps unsaved edits on refresh, and fits at a narrow viewport',async()=>{
 const x=await setup(),{page}=x;await page.locator('[data-kind="management"]').click();await page.getByRole('button',{name:'Close settings'}).click();await page.getByRole('button',{name:'Zoom in',exact:true}).click();assert.equal(await page.locator('#zoom').innerText(),'110%');
 await page.getByRole('button',{name:'Refresh saved state',exact:true}).click();await page.waitForFunction(()=>!document.getElementById('refresh').disabled);assert.equal(await page.locator('.team-node').count(),1);assert.match(await page.locator('#saved-state').innerText(),/Unsaved/);
 await page.setViewportSize({width:900,height:768});await page.getByRole('button',{name:'Fit',exact:true}).click();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);assert.deepEqual(x.errors,[]);await x.context.close();
});


test('saving a canvas before mission creation preserves every configured block',async()=>{
 const x=await setup(null),{page,calls}=x;
 await page.locator('[data-kind="management"]').click();
 await page.getByRole('button',{name:'Close settings'}).click();
 await page.locator('#objective').fill('Preserve the configured team');
 await page.getByRole('button',{name:'Save workflow',exact:true}).click();
 await page.locator('#create-form input[name="title"]').fill('New team draft');
 assert.equal(await page.locator('#create-form textarea[name="objective"]').inputValue(),'Preserve the configured team');
 await page.getByRole('button',{name:'Create draft',exact:true}).click();
 await page.waitForFunction(()=>document.getElementById('mission-status').textContent==='awaiting_approval');
 assert.equal(await page.locator('.team-node').count(),1);
 const save=calls.find(c=>c.p.endsWith('/team-workflow'));assert.equal(save.body.plan.blocks[0].kind,'management');
 assert.equal(calls.filter(c=>c.method==='POST').length,1);assert.deepEqual(x.errors,[]);await x.context.close();
});
