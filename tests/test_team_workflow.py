"""Actual team compilation, actor gating, free-route projection, and HTTP boundaries."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from app import main
from app.db import get_conn, init_db, dumps, set_setting
from app.execution import MissionService, AppError
from app.free_models import project_models, probe_opencode, selected_route
from app.plan import PlanValidationError, topological_order
from app.team_workflow import compile_team_workflow
from tests.test_native_payloads import native_accounts, native_status


def workflow(n=2):
    blocks=[]
    for i in range(1,n+1):
        blocks.append({"id":f"B{i}","name":f"Team {i}","kind":"inspection" if i==n else "specialist",
                       "specialty":"Quality" if i==n else "Engineering", "goal":"Deliver a checked report",
                       "supervisor":{"agent":"cursor","model":"unknown"}, "deps":[f"B{i-1}"] if i>1 else [],
                       "employees":[{"agent":"cursor","model":"unknown","instruction":"Write the assigned report",
                                     "outputs":[f"team-test/{i}.md"],"acceptance":["Report is correct"]}]})
    return {"version":1,"blocks":blocks}


def model(**changes):
    obj={"id":"example-free","providerID":"opencode","api":{"url":"https://opencode.ai/zen/v1"},
         "cost":{"input":0,"output":0,"cache":{"read":0,"write":0}},"capabilities":{"toolcall":True}}
    obj.update(changes);return obj


class TeamCompilerTests(unittest.TestCase):
    def test_each_block_has_supervisor_employees_and_a_check(self):
        w=workflow();w['blocks'][0]['employees'].append({**copy.deepcopy(w['blocks'][0]['employees'][0]),"outputs":["team-test/extra.md"]})
        plan=compile_team_workflow(w,"mis_test","Goal",{"cursor"}); tasks={t['id']:t for t in plan['tasks']}
        self.assertEqual(tasks['B1_w1']['deps'],['B1_brief'])
        self.assertEqual(tasks['B1_check']['deps'],['B1_w1','B1_w2'])
        self.assertEqual(tasks['B2_brief']['deps'],['B1_check'])
        self.assertEqual(tasks['B2_check']['role'],'team_inspection')
        self.assertIn('team-test/1.md',tasks['B2_w1']['inputs'])
        self.assertIn('dispatch_id',tasks['B1_check']['instruction'])
        self.assertEqual(len(topological_order(plan['tasks'])),7)

    def test_eight_team_blocks_can_compile_more_than_eight_atomic_jobs(self):
        self.assertEqual(len(compile_team_workflow(workflow(8),'mis_test','Goal',{'cursor'})['tasks']),24)

    def test_rejects_cycles_unknown_routes_traversal_and_write_conflicts(self):
        variations=[]
        w=workflow();w['blocks'][0]['deps']=['B2'];variations.append(w)
        w=workflow();w['blocks'][0]['supervisor']['agent']='claude';variations.append(w)
        w=workflow();w['blocks'][0]['employees'][0]['outputs']=['../secret'];variations.append(w)
        w=workflow();w['blocks'][1]['deps']=[];w['blocks'][1]['employees'][0]['outputs']=['team-test/1.md'];variations.append(w)
        w=workflow();w['blocks'][0]['employees']=[];variations.append(w)
        w=workflow();w['blocks'][0]['position']={'x':float('nan'),'y':0};variations.append(w)
        for value in variations:
            with self.subTest(value=value),self.assertRaises(PlanValidationError):compile_team_workflow(value,'mis_test','Goal',{'cursor'})

    def test_actor_count_is_bounded_without_increasing_global_concurrency(self):
        w=workflow(1);w['blocks'][0]['employees']*=9
        with self.assertRaises(PlanValidationError):compile_team_workflow(w,'mis_test','Goal',{'cursor'})


class FreeModelTests(unittest.TestCase):
    def test_verbose_catalog_projects_only_zero_price_tool_capable_official_routes(self):
        out='opencode/example-free\n'+json.dumps(model())+'\n'
        for obj in [model(id='paid',cost={'input':0,'output':1}),model(id='unknown',cost={'input':0}),
                    model(id='no-tools',capabilities={'toolcall':False}),model(id='spoof',api={'url':'https://opencode.ai.evil.test/v1'}),
                    model(id='cache-paid',cost={'input':0,'output':0,'cache':{'read':1}}),
                    model(id='boolean',cost={'input':False,'output':0}),
                    model(id='reasoning-paid',cost={'input':0,'output':0,'reasoning':1}),
                    model(id='wrong-port',api={'url':'https://opencode.ai:8080/v1'})]:out+=json.dumps(obj)+'\n'
        projected=project_models(out)
        self.assertEqual([r['id'] for r in projected],['opencode/example-free'])
        self.assertIsNone(projected[0]['quota'])
        self.assertNotIn('api',projected[0])

    def test_probe_uses_fixed_no_inference_argv_and_does_not_return_raw_errors(self):
        with patch('app.free_models.shutil.which',return_value='/usr/bin/opencode'),patch('app.free_models.subprocess.run') as run:
            run.return_value=MagicMock(returncode=0,stdout=json.dumps(model()).encode())
            result=probe_opencode()
            self.assertEqual(run.call_args.args[0],['/usr/bin/opencode','models','opencode','--verbose'])
            self.assertFalse(run.call_args.kwargs['shell']);self.assertFalse(result['inference'])
            run.return_value=MagicMock(returncode=1,stdout=b'secret',stderr=b'secret')
            self.assertNotIn('secret',json.dumps(probe_opencode()))

    def test_stale_or_unselected_catalog_cannot_authorize_route(self):
        catalog={'status':'ok','checked_at_epoch':time.time(),'models':project_models(json.dumps(model()))}
        self.assertIsNotNone(selected_route(catalog,'opencode/example-free'))
        self.assertIsNone(selected_route(catalog,'opencode/paid'))
        catalog['checked_at_epoch']-=4000
        self.assertIsNone(selected_route(catalog,'opencode/example-free'))


class TeamServiceTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name);self.db=self.root/'db'
        init_db(self.db);self.conn=get_conn(self.db);self.addCleanup(self.conn.close)
        self.adapter=MagicMock();self.adapter.agent_installed.return_value={'agents':[{'id':'cursor'},{'id':'opencode'}]}
        self.adapter.account_list.return_value=native_accounts();self.adapter.status.return_value=native_status()
        for p in [patch('app.execution.RUNS_DIR',self.root/'runs'),patch('app.execution.LOCK_FILE',self.root/'lock'),
                  patch('app.execution.WORKSPACE',self.root)]:p.start();self.addCleanup(p.stop)
        self.service=MissionService(self.conn,self.adapter)
        self.mission=self.service.create_mission({'title':'Team test','objective':'Deliver checked report','quality_requirements':'Checked','scope':'team-test/','constraints':'Free only','coordinator':'cursor'})

    def test_save_compiles_and_versions_without_native_mutation(self):
        m=self.service.save_plan(self.mission['id'],{'team_workflow':workflow()},0)
        self.assertEqual(m['status'],'awaiting_approval');self.assertEqual(len(m['plan']['tasks']),6)
        self.assertEqual({c[0] for c in self.adapter.method_calls},{'account_list','agent_installed','status'})
        self.service.approve(m['id'],1)
        edited=self.service.save_plan(m['id'],{'team_workflow':workflow()},2)
        self.assertIsNone(edited['approved_at']);self.assertFalse(edited['can']['start'])

    def test_malformed_workflow_is_a_validation_error(self):
        for w in [None,{'blocks':['bad']},{'version':1,'blocks':['bad']},{'version':1,'blocks':[{}]}]:
            with self.subTest(w=w),self.assertRaises(AppError) as e:self.service.save_plan(self.mission['id'],{'team_workflow':w},0)
            self.assertEqual(e.exception.code,'invalid_plan')

    def test_http_requires_csrf_and_old_versions_do_not_overwrite(self):
        with patch.object(main,'_db_path',self.db),patch.object(main,'get_adapter',return_value=self.adapter):
            with TestClient(main.app,base_url='http://127.0.0.1:8765') as client:
                url=f"/api/missions/{self.mission['id']}/team-workflow";body={'plan_version':0,'plan':workflow()}
                self.assertEqual(client.put(url,json=body).status_code,403)
                token=client.get('/api/session').json()['token'];headers={'X-CC-Token':token}
                self.assertEqual(client.put(url,json=body,headers=headers).status_code,200)
                self.assertEqual(client.put(url,json=body,headers=headers).status_code,409)
                self.assertEqual(client.get('/teams.html').status_code,200)
                self.assertEqual(client.post('/api/free-models/refresh').status_code,403)
        self.adapter.worker_start.assert_not_called()

    def test_every_opencode_dispatch_revalidates_pin_and_never_uses_default(self):
        catalog={'status':'ok','checked_at_epoch':time.time(),'models':project_models(json.dumps(model()))}
        with patch('app.execution.probe_opencode',return_value=catalog):
            self.service.refresh_free_models();self.service.select_free_model('opencode/example-free')
            self.assertEqual(self.service.require_eligible('opencode'),'opencode/example-free')
            with self.assertRaises(AppError):self.service.require_eligible('opencode','opencode/paid')
        with patch('app.execution.probe_opencode',return_value={**catalog,'models':[]}):
            with self.assertRaises(AppError):self.service.require_eligible('opencode','opencode/example-free')
        self.adapter.worker_start.assert_not_called()

    def test_gate_accepts_only_this_dispatch_with_real_checks_and_no_findings(self):
        # Materialize compiled tasks through the real service, without an Orca runtime.
        saved=self.service.save_plan(self.mission['id'],{'team_workflow':workflow(1)},0)
        self.service.approve(saved['id'],1)
        self.adapter.terminal_create.return_value={'handle':'term_test'}
        self.adapter.run_create.return_value={'run_id':'run_test'}
        self.adapter.task_create.side_effect=[{'task_id':f'native_{i}'} for i in range(3)]
        self.adapter.worker_start.return_value={'dispatch_id':'dispatch_brief'}
        started=self.service.start(saved['id']);gate=next(t for t in started['tasks'] if t['role']=='team_inspection')
        target=self.root/gate['outputs'][0];target.parent.mkdir(parents=True,exist_ok=True)
        base={'verdict':'PASS','dispatch_id':'dispatch_check','checks':[{'criterion':'Output correct','evidence':'Compared file with acceptance','passed':True}],'findings':[]}
        for changes,expected in [({'dispatch_id':'old_dispatch'},'failed'),({'checks':[]},'failed'),({'findings':['broken']},'failed'),({},'succeeded')]:
            target.write_text(json.dumps({**base,**changes}),encoding='utf-8')
            self.service._settle_task(saved['id'],gate['id'],'dispatch_check','done')
            self.assertEqual(self.service.get_mission(saved['id'])['tasks'][-1]['status'],expected)

    def test_supervisors_and_selected_employees_share_two_slots_and_gate_the_next_block(self):
        w=workflow()
        for i in (2,3):
            w['blocks'][0]['employees'].append({**copy.deepcopy(w['blocks'][0]['employees'][0]),'outputs':[f'team-test/part-{i}.md']})
        saved=self.service.save_plan(self.mission['id'],{'team_workflow':w},0)
        self.service.approve(saved['id'],1)
        self.adapter.terminal_create.return_value={'handle':'term_team'}
        self.adapter.run_create.return_value={'run_id':'run_team'}
        self.adapter.task_create.side_effect=[{'task_id':f'native_{i}'} for i in range(8)]
        self.adapter.worker_start.side_effect=[{'dispatch_id':f'dispatch_{i}'} for i in range(8)]
        started=self.service.start(saved['id'])
        self.assertEqual(self.adapter.worker_start.call_count,1)
        self.conn.execute("UPDATE tasks SET status='succeeded' WHERE mission_id=? AND plan_task_id='B1_brief'",(saved['id'],))
        self.conn.execute("UPDATE dispatches SET state='done' WHERE mission_id=?",(saved['id'],))
        self.service._dispatch_ready(saved['id'])
        self.assertEqual(self.service.count_active_dispatches(),2)
        live=self.service.get_mission(saved['id'])
        self.assertEqual(len([t for t in live['tasks'] if t['status']=='running']),2)
        self.assertEqual(next(t for t in live['tasks'] if t['plan_task_id']=='B1_w3')['status'],'pending')
        self.conn.execute("UPDATE tasks SET status='succeeded' WHERE mission_id=? AND role='team_employee' AND plan_task_id LIKE 'B1_%'",(saved['id'],))
        self.conn.execute("UPDATE dispatches SET state='done' WHERE mission_id=?",(saved['id'],))
        self.service._dispatch_ready(saved['id'])
        self.assertEqual(self.service.count_active_dispatches(),1)
        self.conn.execute("UPDATE tasks SET status='failed' WHERE mission_id=? AND plan_task_id='B1_check'",(saved['id'],))
        self.conn.execute("UPDATE dispatches SET state='done' WHERE mission_id=?",(saved['id'],))
        calls=self.adapter.worker_start.call_count
        self.service._dispatch_ready(saved['id'])
        self.assertEqual(self.adapter.worker_start.call_count,calls)
        self.assertEqual(next(t for t in self.service.get_mission(saved['id'])['tasks'] if t['plan_task_id']=='B2_brief')['status'],'pending')

if __name__=='__main__':unittest.main()
