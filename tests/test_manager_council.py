"""Council chat, independent inspection and real artifact handoff boundaries."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from app import main
from app.db import init_db,get_conn,loads
from app.execution import MissionService,AppError
from app.manager_dialogue import send,history,refresh,apply_proposal,stop
from app.team_workflow import compile_team_workflow
from app.plan import PlanValidationError
from app.data_flow import project_flow
from app.agent_catalog import catalogue,NAMES
from tests.test_native_payloads import native_status,native_accounts


def flow():
    return {'version':2,'input':{'text':'Build and verify a report','files':[]},
            'delivery':{'sources':['ENG'],'inspector':'QA'},'blocks':[
              {'id':'MGR','name':'Management','kind':'management','specialty':'Planning','goal':'Discuss and plan','deps':[],
               'advisors':[{'agent':'cursor','model':'unknown'},{'agent':'cursor','model':'unknown'}]},
              {'id':'ENG','name':'Engineering','kind':'specialist','specialty':'Engineering','goal':'Write the report','deps':['MGR'],
               'supervisor':{'agent':'cursor','model':'unknown'},'employees':[{'agent':'cursor','model':'unknown','instruction':'Write a report','outputs':['out/report.md'],'acceptance':['The report is correct']}]},
              {'id':'QA','name':'Inspection','kind':'inspection','specialty':'Quality assurance','goal':'Inspect output','deps':['ENG'],
               'inspector':{'agent':'cursor','model':'unknown'}}]}


class CouncilSchemaTests(unittest.TestCase):
    def test_management_is_peers_and_synthesis_inspection_is_one_separate_task(self):
        plan=compile_team_workflow(flow(),'mis_parent','Goal',{'cursor'})
        manager=[t for t in plan['tasks'] if t['team_block']=='MGR']
        self.assertEqual([t['role'] for t in manager],['manager_consultation','manager_consultation','manager_consensus'])
        self.assertEqual(manager[-1]['deps'],['MGR_advice1','MGR_advice2'])
        inspectors=[t for t in plan['tasks'] if t['team_block']=='QA']
        self.assertEqual(len(inspectors),1);self.assertEqual(inspectors[0]['role'],'team_inspection')
        self.assertEqual(inspectors[0]['write_scope'],['.orca-runs/mis_parent/teams/QA/review.json'])
        self.assertIn('out/report.md',inspectors[0]['inputs'])
        self.assertEqual(plan['team_workflow']['delivery']['sources'],['ENG'])

    def test_management_rejects_staff_and_inspection_must_cover_delivery(self):
        candidates=[]
        w=flow();w['blocks'][0]['employees']=[];candidates.append(w)
        w=flow();w['blocks'][2]['deps']=[];candidates.append(w)
        w=flow();w['blocks'][2]['require_different_agent']=True;candidates.append(w)
        w=flow();w['input']['files']=['../secret'];candidates.append(w)
        w=flow();w['delivery']['inspector']=[];candidates.append(w)
        for candidate in candidates:
            with self.subTest(candidate=candidate),self.assertRaises(PlanValidationError):compile_team_workflow(candidate,'mis_parent','Goal',{'cursor'})

    def test_catalogue_includes_all_builtins_and_unknown_local_agents_without_enabling_them(self):
        result=catalogue({'agents':[{'id':'cursor','installed':True,'eligible':True},{'id':'custom-local','name':'Custom','installed':True,'eligible':False} ]},{})
        by_id={a['id']:a for a in result['agents']}
        self.assertTrue(set(NAMES).issubset(by_id));self.assertIn('custom-local',by_id)
        self.assertFalse(by_id['claude']['eligible']);self.assertFalse(by_id['custom-local']['eligible'])
        self.assertIsNone(by_id['claude']['quota'])

    def test_file_motion_requires_saved_recipient_running_and_declared_upstream_artifacts(self):
        plan=compile_team_workflow(flow(),'mis_parent','Goal',{'cursor'})
        tasks=[{'plan_task_id':t['id'],'status':'succeeded' if t['team_block']=='MGR' else 'running' if t['id']=='ENG_brief' else 'pending'} for t in plan['tasks']]
        item={'id':'art1','rel_path':'.orca-runs/mis_parent/teams/MGR/manager.md','verified':True,'size':10,'sha256':'hash'}
        result=project_flow({'plan':plan,'tasks':tasks,'artifacts':[item],'objective':'Goal','status':'running'})
        edge=next(e for e in result['edges'] if e['source']=='MGR' and e['target']=='ENG')
        self.assertEqual(edge['state'],'processing_inputs');self.assertEqual(edge['files'][0]['sha256'],'hash')
        self.assertFalse(any(e['state']=='processing_inputs' for e in result['edges'] if e['target']=='__output'))
        tasks[0]['status']='unknown'
        edge=next(e for e in project_flow({'plan':plan,'tasks':tasks,'artifacts':[],'status':'running'})['edges'] if e['source']=='MGR' and e['target']=='ENG')
        self.assertNotEqual(edge['state'],'processing_inputs')


class ManagerServiceTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name);self.db=self.root/'db'
        init_db(self.db);self.conn=get_conn(self.db);self.addCleanup(self.conn.close)
        self.adapter=MagicMock();self.adapter.status.return_value=native_status();self.adapter.account_list.return_value=native_accounts()
        self.adapter.agent_installed.return_value={'agents':[{'id':'cursor'}]}
        self.adapter.terminal_create.return_value={'handle':'term_council'}
        self.adapter.run_create.return_value={'run_id':'run_council'}
        self.adapter.task_create.side_effect=[{'task_id':f'nt_{i}'} for i in range(30)]
        self.adapter.worker_start.side_effect=[{'dispatch_id':f'ctx_{i}'} for i in range(30)]
        self.adapter.worker_show.return_value={'state':'done'}
        for p in [patch('app.execution.RUNS_DIR',self.root/'runs'),patch('app.execution.LOCK_FILE',self.root/'lock'),patch('app.execution.WORKSPACE',self.root)]:p.start();self.addCleanup(p.stop)
        self.service=MissionService(self.conn,self.adapter)
        self.parent=self.service.create_mission({'title':'User mission','objective':'Checked report','quality_requirements':'Checked','scope':'out/','constraints':'Free only','coordinator':'cursor'})

    def send(self,count=2,key='request_12345678'):
        return send(self.service,self.parent['id'],message='برای این کار یک برنامه بده',advisors=[{'agent':'cursor','model':'unknown'} for _ in range(count)],block_id='MGR',request_id=key,workflow=flow())

    def child(self):
        mid=self.conn.execute('SELECT child_mission_id FROM manager_turns').fetchone()[0]
        return self.service.get_mission(mid)

    def outputs(self,tasks):
        for t in tasks:
            for rel in t['outputs']:
                p=self.root/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('Public opinion with concrete proposed steps',encoding='utf-8')

    def finish(self,reply=None):
        child=self.child();self.outputs([t for t in child['tasks'] if t['role']=='manager_advice'])
        refresh(self.service,self.parent['id'])
        task=next(t for t in self.child()['tasks'] if t['role']=='manager_reply')
        path=self.root/task['outputs'][0];path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(reply or {'reply':'این برنامهٔ پیشنهادی است.','dispatch_id':task['dispatch_id'],'team_workflow':flow()}),encoding='utf-8')
        return refresh(self.service,self.parent['id'])

    def test_send_runs_only_consultation_and_leaves_project_plan_unapproved(self):
        result=self.send();self.assertEqual(self.adapter.worker_start.call_count,2)
        self.assertEqual(len(result['turns']),1);self.assertEqual(result['turns'][0]['status'],'consulting')
        self.assertIsNone(self.service.get_mission(self.parent['id'])['plan'])
        self.assertEqual({t['role'] for t in self.child()['plan']['tasks']},{'manager_advice','manager_reply'})
        self.assertTrue(all(p.startswith(f".orca-runs/{self.parent['id']}/manager/") for t in self.child()['tasks'] for p in t['outputs']))
        self.assertEqual([m['id'] for m in self.service.list_missions()],[self.parent['id']])
        self.assertEqual(self.service.count_active_dispatches(self.parent['id']),0)
        self.assertEqual(self.service.count_active_dispatches(),2)

    def test_idempotent_send_and_history_reads_never_duplicate_dispatch(self):
        first=self.send();calls=self.adapter.worker_start.call_count
        second=self.send();self.assertEqual(first['turns'][0]['id'],second['turns'][0]['id'])
        history(self.service,self.parent['id']);self.assertEqual(self.adapter.worker_start.call_count,calls)
        with self.assertRaises(AppError):self.send(key='new_request_12345')

    def test_response_is_verified_and_proposal_needs_explicit_versioned_application(self):
        self.send();result=self.finish();turn=result['turns'][0]
        self.assertEqual(turn['status'],'ready');self.assertTrue(turn['proposal_available'])
        self.assertTrue(all(a['opinion_artifact_id'] for a in turn['advisors']))
        self.assertIsNone(self.service.get_mission(self.parent['id'])['plan'])
        calls=self.adapter.worker_start.call_count
        updated=apply_proposal(self.service,self.parent['id'],turn['id'],0)
        self.assertEqual(updated['status'],'awaiting_approval');self.assertIsNone(updated['approved_at'])
        self.assertEqual(self.adapter.worker_start.call_count,calls)
        with self.assertRaises(AppError):apply_proposal(self.service,self.parent['id'],turn['id'],0)

    def test_invalid_or_stale_native_reply_never_appears_as_an_answer(self):
        self.send();result=self.finish({'reply':'Old answer','dispatch_id':'stale_native_id'})
        self.assertEqual(result['turns'][0]['status'],'failed');self.assertIsNone(result['turns'][0]['reply'])

    def test_reading_history_does_not_start_a_queued_request_and_stop_is_scoped(self):
        # One-session cap for this test demonstrates queueing behind another owned task.
        from app.db import set_setting
        set_setting(self.conn,'worker_cap','1');self.conn.commit()
        self.send(count=1);other=self.service.create_mission({'title':'Other','objective':'Other','quality_requirements':'Checked','scope':'out/','constraints':'Free','coordinator':'cursor'})
        self.assertEqual(self.adapter.worker_start.call_count,1)
        stop(self.service,self.parent['id'])
        self.assertEqual(self.adapter.worker_stop.call_count,1)
        self.assertEqual(self.service.get_mission(other['id'])['status'],'draft')
        self.assertEqual(history(self.service,self.parent['id'])['turns'][0]['status'],'failed')

    def test_manager_http_post_requires_csrf_while_history_is_read_only(self):
        with patch.object(main,'_db_path',self.db),patch.object(main,'get_adapter',return_value=self.adapter):
            with TestClient(main.app,base_url='http://127.0.0.1:8765') as client:
                prefix=f"/api/missions/{self.parent['id']}/manager"
                self.assertEqual(client.post(prefix+'/messages',json={'message':'Hi','advisors':[{'agent':'cursor'}],'block_id':'MGR','request_id':'request_12345678'}).status_code,403)
                self.assertEqual(client.get(prefix).status_code,200)
                self.assertEqual(client.get(f"/api/missions/{self.parent['id']}/data-flow").status_code,200)
        self.adapter.worker_start.assert_not_called()

    def test_queueing_and_parent_stop_do_not_touch_other_missions(self):
        self.send()
        other=self.service.create_mission({'title':'Other','objective':'Other','quality_requirements':'Checked','scope':'out/','constraints':'Free','coordinator':'cursor'})
        queued=send(self.service,other['id'],message='Another bounded consultation',advisors=[{'agent':'cursor'}],block_id='MGR',request_id='other_request_123')
        self.assertEqual(queued['turns'][0]['status'],'queued')
        calls=self.adapter.worker_start.call_count
        history(self.service,other['id']);self.assertEqual(self.adapter.worker_start.call_count,calls)
        self.service.cancel(self.parent['id'],True)
        self.assertEqual(self.adapter.worker_stop.call_count,2)
        self.assertEqual(history(self.service,other['id'])['turns'][0]['status'],'queued')
        refreshed=refresh(self.service,other['id'])
        self.assertEqual(refreshed['turns'][0]['status'],'consulting')
        self.assertEqual(self.adapter.worker_start.call_count,calls+1)

    def test_unconfirmed_stop_holds_capacity_until_native_settlement(self):
        self.send(count=1)
        self.adapter.worker_show.return_value={'state':'running'}
        stop(self.service,self.parent['id'])
        self.assertEqual(self.service.count_active_dispatches(),1)
        calls=self.adapter.worker_start.call_count
        self.adapter.worker_show.return_value={'state':'stopped'}
        refresh(self.service,self.parent['id'])
        self.assertEqual(self.service.count_active_dispatches(),0)
        self.assertEqual(self.adapter.worker_start.call_count,calls)

    def test_distinct_verified_free_models_can_share_the_opencode_harness(self):
        from app.db import set_setting,dumps
        import time
        self.adapter.agent_installed.return_value={'agents':[{'id':'cursor'},{'id':'opencode'}]}
        catalog={'status':'ok','checked_at_epoch':time.time(),'models':[{'id':'opencode/free-one'},{'id':'opencode/free-two'}]}
        set_setting(self.conn,'free_model_catalog',dumps(catalog));set_setting(self.conn,'free_model_pin','opencode/free-one');self.conn.commit()
        with patch('app.execution.probe_opencode',return_value=catalog):
            send(self.service,self.parent['id'],message='Compare two free models',advisors=[{'agent':'opencode','model':'opencode/free-one'},{'agent':'opencode','model':'opencode/free-two'}],block_id='MGR',request_id='multi_model_1234')
        self.assertEqual([c.kwargs['model'] for c in self.adapter.worker_start.call_args_list],['opencode/free-one','opencode/free-two'])

    def test_independent_inspector_cannot_pass_after_rewriting_a_recorded_producer_file(self):
        saved=self.service.save_plan(self.parent['id'],{'team_workflow':flow()},0)
        self.service.approve(saved['id'],1)
        started=self.service.start(saved['id'])
        producer=next(t for t in started['tasks'] if t['plan_task_id']=='ENG_w1')
        inspector=next(t for t in started['tasks'] if t['role']=='team_inspection')
        report=self.root/'out/report.md';report.parent.mkdir(parents=True);report.write_text('Original accepted output')
        self.conn.execute("INSERT INTO artifacts(id,mission_id,task_id,rel_path,size,sha256,kind,verified,created_at) VALUES(?,?,?,?,?,?,?,?,?)",('art_producer',saved['id'],producer['id'],'out/report.md',report.stat().st_size,hashlib.sha256(report.read_bytes()).hexdigest(),'md',1,'2026-11-27T00:00:00Z'))
        target=self.root/inspector['outputs'][0];target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps({'verdict':'PASS','dispatch_id':'ctx_inspector','findings':[],'checks':[{'criterion':'Deliverable correct','evidence':'Read original output','passed':True}]}))
        report.write_text('Inspector rewrote the producer output')
        self.service._settle_task(saved['id'],inspector['id'],'ctx_inspector','done')
        result=next(t for t in self.service.get_mission(saved['id'])['tasks'] if t['id']==inspector['id'])
        self.assertEqual(result['status'],'failed')
        self.assertTrue(any(e['type']=='inspection_input_changed' for e in result['evidence']))

if __name__=='__main__':unittest.main()
