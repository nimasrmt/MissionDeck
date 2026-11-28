"""Safe actionable native errors and Windows path serialization."""
import json
from pathlib import PureWindowsPath
from unittest import TestCase
from unittest.mock import MagicMock
from app.orca_adapter import OrcaAdapter, OrcaError, failure_guidance

class TerminalGuidanceTests(TestCase):
    def test_known_codes_produce_static_guidance_without_exception_text(self):
        out=failure_guidance('terminal_create',json.dumps({'error':{'code':'selector_not_found','message':'private arbitrary native text'}}),'')
        self.assertEqual(out['native_code'],'selector_not_found'); self.assertIn('workspace folder',out['hint'])
        self.assertNotIn('private',json.dumps(out))
        self.assertIn('runtime',failure_guidance('terminal_create','[]','runtime_not_running')['hint'])
        self.assertIn('terminal',failure_guidance('terminal_create','not json','opaque error')['hint'])
        self.assertNotIn('native_code',failure_guidance('terminal_create','{"error":{"code":[]}}','opaque error'))

    def test_nonzero_and_rejected_envelopes_keep_native_code(self):
        for rc in [0,1]:
            a=OrcaAdapter(runner=lambda *args,**kw:MagicMock(returncode=rc,stdout=b'{"ok":false,"error":{"code":"workspace_not_found"}}',stderr=b''))
            with self.assertRaises(OrcaError) as err:a.terminal_create(title='Test')
            self.assertEqual(err.exception.details['native_code'],'workspace_not_found')

    def test_windows_terminal_and_worker_paths_use_forward_slashes(self):
        calls=[]
        def runner(argv,**kwargs):
            calls.append(argv); return MagicMock(returncode=0,stdout=b'{"ok":true}',stderr=b'')
        a=OrcaAdapter(runner=runner); a.workspace=PureWindowsPath('C:/Users/Test/MissionDeck-Test')
        a.terminal_create(title='Test'); a.worker_start(run_id='r',task_id='t',agent='opencode',from_handle='h')
        for argv in calls:self.assertIn('path:C:/Users/Test/MissionDeck-Test',argv)
        self.assertIn('--keep-open',calls[0])
