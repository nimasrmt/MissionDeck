"""Persistent user-facing council chat, executed as bounded native Orca jobs."""
from __future__ import annotations
import json
import hashlib
import re
from app.db import add_event, dumps, loads, new_id, utc_now, get_setting
from app.execution import AppError, MissionStartLock
from app.plan import validate_plan, PlanValidationError, validate_rel_path
from app.security import redact, resolve_under_workspace

REQUEST = re.compile(r'^[A-Za-z0-9_-]{8,80}$')
BLOCK = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,15}$')


def history(service, mid):
    service.get_mission_row(mid)
    rows=service.conn.execute('SELECT * FROM manager_turns WHERE parent_mission_id=? ORDER BY created_at DESC, rowid DESC LIMIT 20',(mid,)).fetchall()
    turns=[]
    for row in reversed(rows):
        r=dict(row); child=service.get_mission(r['child_mission_id']); reply=loads(r['reply_data_json'],{})
        advisors=[]
        for i,a in enumerate(loads(r['advisors_json'],[]),1):
            task=next((t for t in child['tasks'] if t['plan_task_id']==f'A{i}'),None)
            artifact=next((item for item in reversed(child['artifacts']) if task and item['rel_path'] in task['outputs'] and item['verified']),None)
            advisors.append({**a,'status':task['status'] if task else 'queued','opinion_artifact_id':artifact['id'] if artifact and task['status']=='succeeded' else None})
        turns.append({'id':r['id'],'block_id':r['block_id'],'message':r['message'],'advisors':advisors,
                      'status':r['status'],'reply':reply.get('reply'),'proposal_available':isinstance(reply.get('team_workflow'),dict),
                      'error':r['error'],'created_at':r['created_at'],'child_mission_id':r['child_mission_id'],'active_dispatches':child['active_dispatches']})
    return {'turns':turns,'global_active_dispatches':service.count_active_dispatches()}


def send(service, mid, *, message, advisors, block_id, request_id, workflow=None):
    with MissionStartLock():
        parent=dict(service.get_mission_row(mid))
        if not isinstance(message,str) or not message.strip() or len(message)>12000:raise AppError('validation','Write a message of 1..12000 characters')
        message=redact(message.strip())
        if not REQUEST.fullmatch(request_id or '') or not BLOCK.fullmatch(block_id or ''):raise AppError('validation','Invalid request or management block ID')
        old=service.conn.execute('SELECT * FROM manager_turns WHERE parent_mission_id=? AND request_id=?',(mid,request_id)).fetchone()
        if old:
            if old['message']!=message:raise AppError('conflict','Request ID already belongs to a different message')
            return history(service,mid)
        if service.conn.execute("SELECT COUNT(*) FROM manager_turns WHERE parent_mission_id=? AND (status IN ('queued','consulting') OR child_mission_id IN (SELECT mission_id FROM dispatches WHERE terminal_state IS NULL AND state NOT IN ('done','failed','stopped','cancelled','released')))",(mid,)).fetchone()[0]:
            raise AppError('busy','Finish or stop the active consultation before sending another message')
        if not isinstance(advisors,list) or not 1<=len(advisors)<=4:raise AppError('validation','Choose 1..4 consulting AIs')
        routes=[]
        for a in advisors:
            if not isinstance(a,dict) or not isinstance(a.get('agent'),str):raise AppError('validation','Invalid consulting AI')
            requested=a.get('model')
            if requested is not None and not isinstance(requested,str):raise AppError('validation','Invalid model')
            model=service.require_eligible(a['agent'],requested)
            routes.append({'agent':a['agent'],'model':model or requested or 'unknown'})
        source=workflow if isinstance(workflow,dict) else loads(parent.get('plan_json'),{}).get('team_workflow') or {}
        if len(json.dumps(source).encode('utf-8'))>64*1024:raise AppError('validation','Workflow context is too large')
        inlet=source.get('input') or {}
        if not isinstance(inlet,dict) or not isinstance(inlet.get('files',[]),list) or not all(isinstance(p,str) for p in inlet.get('files',[])):
            raise AppError('validation','Invalid manager input files')
        files=inlet.get('files',[])
        errors=[]
        for p in files:validate_rel_path(p,errors,field='manager inputs')
        if errors:raise AppError('validation','Invalid manager input files',{'errors':errors})
        turn_id=new_id('turn'); root=f'.orca-runs/{mid}/manager/{turn_id}'
        from app.execution import WORKSPACE
        resolve_under_workspace(root,workspace=WORKSPACE).mkdir(parents=True,exist_ok=True)
        reply_path=f'{root}/response.json'
        prior=[]
        for row in service.conn.execute('SELECT message, reply_data_json FROM manager_turns WHERE parent_mission_id=? ORDER BY created_at DESC LIMIT 8',(mid,)):
            prior.append({'user':row['message'],'manager':loads(row['reply_data_json'],{}).get('reply')})
        context=json.dumps(list(reversed(prior)),ensure_ascii=False)[-24000:]
        child=service.create_mission({'title':'Manager consultation','objective':message,'quality_requirements':'Bounded advice and a verified user-facing reply',
                                     'scope':root,'constraints':'Read-only consultation except own declared runtime outputs. No implementation, installs, model changes, paid fallback or nested workers.',
                                     'coordinator':routes[0]['agent']})
        tasks=[]
        def task(tid,title,route,deps,inputs,outputs,instruction,role):
            return {'id':tid,'title':title,'department':'council','role':role,'agent':route['agent'],'model':route['model'],
                    'deps':deps,'inputs':inputs,'write_scope':outputs,'outputs':outputs,'acceptance':['Answer the user with checkable claims and explicit uncertainties.'],
                    'validation':[],'review_required':False,'instruction':instruction}
        base=f'Mission objective: {parent["objective"]}\nUser message: {message}\nPrevious conversation: {context}\n'
        opinions=[]
        for i,a in enumerate(routes,1):
            output=f'{root}/advisor-{i}.md';opinions.append(output)
            perspective=['planning and practical delivery','risks and critical review','implementation and verification','scope and usability'][i-1]
            tasks.append(task(f'A{i}',f'Council AI {i}',a,[],files,[output],base+
                              f'You are a consulting peer focusing on {perspective}. Write only your final public advice, not private reasoning. '
                              'Do not change the project, run other models or start workers. State disagreements and concrete next steps. Write only your declared advice file.', 'manager_advice'))
        available=[{'agent':a['id'],'model':a['model']} for a in service.capabilities()['agents'] if a.get('eligible')]
        available += [a for a in routes if a not in available]
        schema=("An optional team_workflow uses version 3, input:{text,files:[]}, connections:[{source,target}], blocks:1..8 objects with id,name,specialty,goal,kind. Connections are explicit: __input leads through the teams to __output; every block must lie on that path. deps are derived. Final inspection connects to __output. "
                "management has advisors:1..4 {agent,model} and NO supervisor/employees; specialist has supervisor:{agent,model}, employees:1..8 {agent,model,instruction,outputs:[relative file],acceptance:[criterion]}; "
                "inspection has inspector:{agent,model} and NO supervisor/employees. The inspector receives all final producers through explicit connections. No cycles or conflicting writes. Only propose wiring; it is applied only after the user explicitly chooses the proposal.")
        instruction=base+'Read every advice file. Address the user conversationally in their language, summarize decisions and differences, and ask a concise question when the goal is ambiguous. '+\
                    'Write exactly the declared JSON reply file with reply:string (user-facing final answer), dispatch_id:YOUR current native dispatch ID, and optionally team_workflow:object when a concrete executable workflow is useful. '+\
                    'Never claim work was implemented or accepted. Proposals are not automatically applied, approved or executed. '+schema+\
                    f'\nEligible routes: {json.dumps(available)}\nCurrent workflow: {json.dumps(source,ensure_ascii=False)}'
        tasks.append(task('REPLY','Manager reply',routes[0],[f'A{i}' for i in range(1,len(routes)+1)],files+opinions,[reply_path],instruction,'manager_reply'))
        plan={'schema_version':1,'mission_summary':message,'architecture':'Consulting peers → consolidated reply','selection_rationale':'User-selected consulting AIs',
              'departments':[{'id':'council','name':'Management council'}],'tasks':tasks,'quality_gates':[],'risks':['Advice and proposals require user review before project execution.']}
        try:validate_plan(plan,eligible_agents={a['agent'] for a in routes})
        except PlanValidationError as exc:raise AppError('invalid_plan','Council plan invalid',{'errors':exc.errors}) from exc
        now=utc_now()
        service.conn.execute("UPDATE missions SET plan_json=?, plan_version=1,plan_status='valid',status='approved',approved_at=?,updated_at=? WHERE id=?",(dumps(plan),now,now,child['id']))
        service.conn.execute('INSERT INTO manager_turns(id,parent_mission_id,block_id,request_id,child_mission_id,message,advisors_json,reply_path,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                             (turn_id,mid,block_id,request_id,child['id'],message,dumps(routes),reply_path,'queued',now,now))
        add_event(service.conn,mid,'manager_message','User requested a bounded council consultation',{'turn_id':turn_id,'advisors':len(routes)})
        service.conn.commit()
        if service.count_active_dispatches()<int(get_setting(service.conn,'worker_cap','2')):
            try:
                service._start_locked(child['id'])
                service.conn.execute("UPDATE manager_turns SET status='consulting',updated_at=? WHERE id=?",(utc_now(),turn_id));service.conn.commit()
            except Exception:
                # Persist the request and native state; never automatically issue a second launch attempt.
                service.conn.execute("UPDATE manager_turns SET status='failed',error='Consultation could not start; inspect the native run before retrying',updated_at=? WHERE id=?",(utc_now(),turn_id));service.conn.commit()
                raise
        return history(service,mid)


def refresh(service,mid):
    from app.execution import WORKSPACE
    with MissionStartLock():
        service.get_mission_row(mid)
        rows=service.conn.execute("SELECT * FROM manager_turns WHERE parent_mission_id=? AND (status IN ('queued','consulting') OR child_mission_id IN (SELECT mission_id FROM dispatches WHERE terminal_state IS NULL AND state NOT IN ('done','failed','stopped','cancelled','released'))) ORDER BY created_at",(mid,)).fetchall()
        for row in rows:
            r=dict(row); child=service.get_mission(r['child_mission_id'])
            if child['status']=='approved':
                cap=int(get_setting(service.conn,'worker_cap','2'))
                if service.count_active_dispatches()<cap:child=service._start_locked(child['id'])
            elif child['status'] in ('running','review','cancelled'):child=service._refresh_locked(child['id'])
            status='queued' if child['status']=='approved' else 'consulting';reply_data=None;error=None
            if child['status']=='succeeded':
                try:
                    task=next(t for t in child['tasks'] if t['role']=='manager_reply')
                    path=resolve_under_workspace(r['reply_path'],workspace=WORKSPACE,must_exist=True)
                    if path.stat().st_size>256*1024:raise ValueError('Reply too large')
                    with path.open('rb') as handle: raw=handle.read(256*1024+1)
                    artifact=next((a for a in reversed(child['artifacts']) if a['rel_path']==r['reply_path']),None)
                    if len(raw)>256*1024 or not artifact or not artifact['verified'] or hashlib.sha256(raw).hexdigest()!=artifact['sha256']:raise ValueError('Reply artifact changed')
                    data=json.loads(raw.decode('utf-8'))
                    if not isinstance(data,dict) or data.get('dispatch_id')!=task['dispatch_id'] or not isinstance(data.get('reply'),str) or not data['reply'].strip() or len(data['reply'])>12000:raise ValueError('Invalid reply')
                    data={'reply':redact(data['reply']),**({'team_workflow':data['team_workflow']} if isinstance(data.get('team_workflow'),dict) else {})}
                    reply_data=dumps(data);status='ready'
                except (ValueError,OSError,StopIteration):status='failed';error='The manager reply was missing, invalid or from an old dispatch'
            elif child['status'] in ('failed','blocked','cancelled','unknown') or any(t['status'] in ('failed','unknown') for t in child['tasks']):
                status='failed';error='Consultation needs attention; no completed reply was verified'
            service.conn.execute('UPDATE manager_turns SET status=?,reply_data_json=COALESCE(?,reply_data_json),error=?,updated_at=? WHERE id=?',(status,reply_data,error,utc_now(),r['id']))
        service.conn.commit()
        return history(service,mid)


def apply_proposal(service,mid,turn_id,version):
    row=service.conn.execute('SELECT * FROM manager_turns WHERE id=? AND parent_mission_id=?',(turn_id,mid)).fetchone()
    if not row or row['status']!='ready':raise AppError('invalid_state','A verified manager reply is required')
    proposal=loads(row['reply_data_json'],{}).get('team_workflow')
    if not isinstance(proposal,dict) or proposal.get('version') not in (2,3):raise AppError('validation','This reply needs a version 2 or 3 council workflow proposal')
    return service.save_plan(mid,{'team_workflow':proposal},version)


def stop(service,mid):
    service.get_mission_row(mid)
    rows=service.conn.execute("SELECT id,child_mission_id,status FROM manager_turns WHERE parent_mission_id=?",(mid,)).fetchall()
    for row in rows:
        child=service.get_mission(row['child_mission_id'])
        if row['status'] not in ('queued','consulting') and not child['active_dispatches']:continue
        if child['can']['cancel']:service.cancel(child['id'],True)
        elif child['status']=='approved':service.conn.execute("UPDATE missions SET status='cancelled',cancel_requested=1 WHERE id=?",(child['id'],))
        service.conn.execute("UPDATE manager_turns SET status='failed',error='Stopped by the user',updated_at=? WHERE id=?",(utc_now(),row['id']))
    service.conn.commit()
    return history(service,mid)
