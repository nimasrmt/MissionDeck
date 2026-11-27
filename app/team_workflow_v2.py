"""Council management, independent inspection, and explicit input/delivery contracts."""
from __future__ import annotations
import copy
from app.plan import PlanValidationError, validate_plan, validate_rel_path
from app.team_workflow import _compile_v1, MAX_TEAM_TASKS


def actor_errors(actor, eligible, label):
    if not isinstance(actor,dict) or not isinstance(actor.get('agent'),str) or not actor['agent'].strip():
        return [f'{label}: choose an AI agent']
    if eligible is not None and actor['agent'] not in eligible:
        return [f'{label}: agent is not currently eligible']
    if actor.get('model') is not None and (not isinstance(actor['model'],str) or not actor['model'].strip()):
        return [f'{label}: invalid model']
    return []


def block_outputs(block, mid):
    root=f".orca-runs/{mid}/teams/{block['id']}"
    if block['kind']=='management': return [f'{root}/manager.md']
    if block['kind']=='inspection': return [f'{root}/review.json']
    return [p for a in block['employees'] for p in a['outputs']]+[f'{root}/review.json']


def delivery_sources(workflow):
    blocks=workflow['blocks']; delivery=workflow.get('delivery') or {}
    sources=delivery.get('sources')
    if sources is not None: return sources
    return [b['id'] for b in blocks if b['kind']!='inspection' and not any(b['id'] in c.get('deps',[]) for c in blocks if c['kind']!='inspection')]


def compile_v2(workflow, mid, objective, eligible):
    source=copy.deepcopy(workflow); blocks=source.get('blocks')
    if not isinstance(blocks,list) or not 1<=len(blocks)<=8: raise PlanValidationError(['Choose 1..8 blocks'])
    errors=[]; surrogate=[]
    for b in blocks:
        if not isinstance(b,dict): errors.append('Every block must be an object');continue
        kind=b.get('kind'); bid=b.get('id'); legacy=copy.deepcopy(b)
        if kind=='management':
            advisors=b.get('advisors')
            if not isinstance(advisors,list) or not 1<=len(advisors)<=4:
                errors.append(f'{bid}: management needs 1..4 consulting AIs'); continue
            if 'supervisor' in b or 'employees' in b:
                errors.append(f'{bid}: management has consulting AIs, not supervisors or employees')
            for a in advisors:errors.extend(actor_errors(a,eligible,f'{bid} council'))
            legacy['supervisor']=advisors[0]
            legacy['employees']=[{**a,'instruction':'Provide bounded advice','outputs':[f'.orca-runs/{mid}/teams/{bid}/advice-{i}.md'],'acceptance':['Advice addresses the goal']} for i,a in enumerate(advisors) if isinstance(a,dict)]
        elif kind=='inspection':
            inspector=b.get('inspector');errors.extend(actor_errors(inspector,eligible,f'{bid} inspector'))
            if 'supervisor' in b or 'employees' in b:errors.append(f'{bid}: inspection has one independent inspector')
            if isinstance(inspector,dict):
                legacy['supervisor']=inspector;legacy['employees']=[{**inspector,'instruction':'Inspect outputs','outputs':[f'.orca-runs/{mid}/teams/{bid}/inspection-notes.md'],'acceptance':['Evidence checked']}]
        surrogate.append(legacy)
    if sum(b.get('kind')=='management' for b in blocks if isinstance(b,dict))>1: errors.append('Use one management council per mission')
    if sum(b.get('kind')=='inspection' for b in blocks if isinstance(b,dict))>1: errors.append('Use one independent final inspector per mission')
    if errors: raise PlanValidationError(errors)
    _compile_v1({'version':1,'blocks':surrogate},mid,objective,eligible)
    inlet=source.get('input') or {'text':objective,'files':[]}
    if not isinstance(inlet,dict): raise PlanValidationError(['Input must be an object'])
    files=inlet.get('files',[])
    if not isinstance(files,list) or not all(isinstance(x,str) for x in files): errors.append('Input files must be a list of relative paths');files=[]
    for path in files:validate_rel_path(path,errors,field='input.files')
    if not isinstance(inlet.get('text',objective),str) or len(inlet.get('text',objective))>12000:errors.append('Input text must be at most 12000 characters')
    delivery=source.get('delivery') or {}
    if not isinstance(delivery,dict):raise PlanValidationError(['Delivery must be an object'])
    sources=delivery_sources(source); by_id={b['id']:b for b in blocks}
    if not isinstance(sources,list) or not sources or not all(isinstance(x,str) and x in by_id and by_id[x]['kind']!='inspection' for x in sources):
        errors.append('Choose at least one final producer block for delivery');sources=[]
    if len(sources)!=len(set(sources)):errors.append('Duplicate delivery sources')
    inspectors=[b for b in blocks if b['kind']=='inspection']; inspection=delivery.get('inspector')
    if inspection is None and inspectors: inspection=inspectors[0]['id']
    if inspection is not None and (not isinstance(inspection,str) or inspection not in by_id or by_id[inspection]['kind']!='inspection'):errors.append('Delivery inspector must refer to an inspection block')
    def ancestors(bid):
        seen=set();todo=list(by_id[bid]['deps'])
        while todo:
            node=todo.pop()
            if node in seen:continue
            seen.add(node);todo.extend(by_id[node]['deps'])
        return seen
    if isinstance(inspection,str) and inspection in by_id:
        upstream=ancestors(inspection)
        if not set(sources).issubset(upstream):errors.append('The independent inspector must receive every selected delivery source')
        if by_id[inspection].get('require_different_agent'):
            own=by_id[inspection]['inspector']['agent']
            for b in blocks:
                if b['id'] not in upstream:continue
                actors=b.get('advisors',[]) if b['kind']=='management' else [b['supervisor'],*b['employees']] if b['kind']=='specialist' else []
                if any(a['agent']==own for a in actors):errors.append('Inspector requires a different agent from all reviewed producers')
    if errors:raise PlanValidationError(errors)
    tasks=[]; outputs={b['id']:block_outputs(b,mid) for b in blocks}
    def make(b,tid,title,actor,role,deps,inputs,out,instruction,acceptance,validation=None,write_scope=None):
        return {'id':tid,'title':title,'department':b['id'],'role':role,'agent':actor['agent'],'model':actor.get('model') or 'unknown',
                'deps':deps,'inputs':inputs,'outputs':out,'write_scope':write_scope or out,'acceptance':acceptance,'validation':validation or [],
                'review_required':False,'team_block':b['id'],'specialty':b['specialty'],'instruction':instruction}
    for b in blocks:
        bid=b['id'];root=f'.orca-runs/{mid}/teams/{bid}'
        upstream=[p for dep in b['deps'] for p in outputs[dep]] or files
        terminal_deps=[f'{d}_check' for d in b['deps']]
        goal=f"Mission: {objective}. Input: {inlet.get('text',objective)}. Specialty: {b['specialty']}. Goal: {b['goal']}. "
        if b['kind']=='management':
            advice=[]
            for i,a in enumerate(b['advisors'],1):
                path=f'{root}/advice-{i}.md';advice.append(path)
                tasks.append(make(b,f'{bid}_advice{i}',f"{b['name']} · Council AI {i}",a,'manager_consultation',terminal_deps,upstream,[path],
                                  goal+f'You are consulting peer {i}. Give your own actionable proposal, risks and disagreements. Do not implement changes or launch workers.', ['Bounded advice with an actionable proposal and explicit uncertainties.']))
            tasks.append(make(b,f'{bid}_check',f"{b['name']} · Council synthesis",b['advisors'][0],'manager_consensus',
                              [f'{bid}_advice{i}' for i in range(1,len(b['advisors'])+1)],upstream+advice,outputs[bid],goal+
                              'Read every consulting AI response. Resolve or clearly record disagreements. Publish one manager brief with assignments, dependencies and acceptance criteria. No implementation or nested workers.', ['One consolidated brief backed by all council responses.']))
        elif b['kind']=='inspection':
            tasks.append(make(b,f'{bid}_check',f"{b['name']} · Independent inspection",b['inspector'],'team_inspection',terminal_deps,upstream,outputs[bid],goal+
                              'You are a separate inspection session. Read the actual upstream deliverables and acceptance criteria. Never modify producer outputs or report on behalf of another session. '
                              'Write JSON with verdict PASS or FAIL, dispatch_id equal to YOUR current native dispatch ID, findings array, and a nonempty checks array. Each check has criterion:string, evidence:string and passed:boolean. '
                              'PASS requires all checks true and no unresolved findings; missing evidence means FAIL. Separate-session independence does not imply a different model/provider unless explicitly configured.', ['A current-dispatch PASS with concrete evidence and no findings.']))
        else:
            brief=f'{root}/brief.md'
            tasks.append(make(b,f'{bid}_brief',f"{b['name']} · Supervisor brief",b['supervisor'],'team_supervisor',terminal_deps,upstream,[brief],goal+
                              f"Assign distinct employee jobs within these contracts: {b['employees']}. Write only the brief; do not implement the employee work or launch workers.", ['Every employee has a precise scoped assignment.']))
            for i,a in enumerate(b['employees'],1):
                tasks.append(make(b,f'{bid}_w{i}',f"{b['name']} · Employee {i}",a,'team_employee',[f'{bid}_brief'],[brief,*upstream,*(a.get('inputs') or [])],a['outputs'],goal+
                                  f"Read the supervisor brief. You are employee {i}. {a['instruction']}",a['acceptance'],a.get('validation'),a.get('write_scope')))
            tasks.append(make(b,f'{bid}_check',f"{b['name']} · Supervisor check",b['supervisor'],'team_check',[f'{bid}_w{i}' for i in range(1,len(b['employees'])+1)],
                              [brief,*upstream,*outputs[bid][:-1]],[f'{root}/review.json'],goal+
                              'Check every employee deliverable. Write JSON with verdict PASS/FAIL, dispatch_id equal to your current native dispatch ID, findings array, and nonempty checks array with criterion, evidence and passed. PASS requires every check true and findings empty. Do not rewrite employee files.', ['Current-dispatch PASS with concrete checks.']))
    source['input']={**inlet,'text':inlet.get('text',objective),'files':files}
    source['delivery']={**delivery,'sources':sources,'inspector':inspection}
    plan={'schema_version':1,'mission_summary':objective,'architecture':'Input → consulting manager → specialist teams → optional independent inspection → final delivery.',
          'selection_rationale':'Explicit peer councils, specialist staffing, and separate inspection sessions.','departments':[{'id':b['id'],'name':b['name']} for b in blocks],
          'tasks':tasks,'quality_gates':[{'id':t['id'],'description':t['title'],'evidence':t['outputs'][0]} for t in tasks if t['role'] in ('team_check','team_inspection')],
          'risks':['Session independence is distinct from agent/provider independence.'],'team_workflow':source}
    return validate_plan(plan,eligible_agents=eligible,max_tasks=MAX_TEAM_TASKS)
