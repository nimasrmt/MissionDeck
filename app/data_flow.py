"""Declared file handoffs projected from saved task state and recorded artifacts."""
from __future__ import annotations


def project_flow(mission):
    plan=mission.get('plan') or {}; workflow=plan.get('team_workflow') or {}; blocks=workflow.get('blocks') or []
    tasks=plan.get('tasks') or []; states={t['plan_task_id']:t['status'] for t in mission.get('tasks',[])}
    artifacts={a['rel_path']:a for a in mission.get('artifacts',[])}
    outputs={b['id']:{p for t in tasks if t.get('team_block')==b['id'] for p in t.get('outputs',[])} for b in blocks}
    def state(bid):
        own=[states[t['id']] for t in tasks if t.get('team_block')==bid and t['id'] in states]
        if not own:return 'planned'
        for value in ('failed','unknown','running','blocked','cancelled'):
            if value in own:return value
        return 'succeeded' if all(s=='succeeded' for s in own) else 'waiting'
    edges=[]
    def edge(src,dst,paths,text=None):
        source_state='available' if src=='__input' else state(src)
        target_state=mission.get('status') if dst=='__output' else state(dst)
        active=dst!='__output' and target_state=='running' and source_state in ('available','succeeded')
        files=[]
        for p in sorted(set(paths)):
            a=artifacts.get(p)
            files.append({'path':p,'recorded':bool(a and a.get('verified')),'size':a.get('size') if a else None,
                          'sha256':a.get('sha256') if a else None,'artifact_id':a.get('id') if a else None})
        edges.append({'source':src,'target':dst,'files':files,'text':text,'source_state':source_state,'target_state':target_state,
                      'state':'processing_inputs' if active else 'failed' if source_state in ('failed','unknown','blocked') else 'completed' if target_state=='succeeded' else 'available' if source_state in ('available','succeeded') else 'waiting'})
    for b in blocks:
        if not b.get('deps'):
            edge('__input',b['id'],(workflow.get('input') or {}).get('files',[]),(workflow.get('input') or {}).get('text') or mission.get('objective'))
        for dep in b.get('deps',[]):
            inputs={p for t in tasks if t.get('team_block')==b['id'] for p in t.get('inputs',[])}
            edge(dep,b['id'],outputs.get(dep,set()) & inputs)
    if workflow.get('version')==2:
        delivery=workflow.get('delivery') or {};sources=delivery.get('sources') or []
    else:
        sources=[b['id'] for b in blocks if not any(b['id'] in other.get('deps',[]) for other in blocks)]
    for src in sources:edge(src,'__output',outputs.get(src,set()))
    inspector=(workflow.get('delivery') or {}).get('inspector')
    if inspector and inspector not in sources:edge(inspector,'__output',outputs.get(inspector,set()))
    return {'edges':edges,'checked_at':mission.get('freshness'),
            'telemetry':'Declared file contracts and saved native task states; movement means processing inputs, not measured byte traffic.'}
