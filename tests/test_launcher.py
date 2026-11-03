import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mmbot.launcher import load_project, prepare, launch, status, template


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root/'competition.json'
        self.raw = template()
        self.raw['connection'] = dict(gateway_url='https://school.example',
                                      futures=['IF2612.CFE','IH2612.CFE','IM2612.CFE','IC2612.CFE'])
        self.raw['strategy']['symbols'] = ['100001.SH','100002.SH']
        self.raw['strategy']['calibration_symbol'] = '100001.SH'
        self.raw['strategy']['confirmations'] = {k:'离线测试依据' for k in ('valuation','quote_rule','recovery_test','risk_free_rate')}
        self.raw['probe'] = dict(trade_date=20261103, premium_budget=500, attempts=[
            dict(id='p2',probe='P2',product_id='100001.SH',at='09:31:00')],observations=[])
        self.path.write_text(json.dumps(self.raw))
        self.env = dict(TRADING_API_KEY='ak_'+'x'*43,TRADING_DAY_START_NET='5000000')

    def project(self):
        return load_project(self.path)

    def test_four_stages_share_connection_and_data_directory(self):
        configs = [prepare(self.project(),stage,self.env) for stage in (1,2,3,4)]
        self.assertEqual({c.config.data_dir for c in configs},{self.root/'data'})
        self.assertIsNone(configs[0].probe_plan)
        self.assertEqual(configs[1].probe_plan['attempts'][0]['probe'],'P2')
        self.assertEqual(configs[2].strategy_settings.symbols,('100001.SH',))
        self.assertEqual(configs[2].strategy_settings.coverage.max_symbols,1)
        self.assertEqual(configs[3].strategy_settings.symbols,('100001.SH','100002.SH'))
        self.assertIsNone(configs[3].strategy_settings.probe_state_path)

    def test_missing_later_stage_fields_do_not_block_readonly(self):
        raw = copy.deepcopy(self.raw)
        raw['probe']['trade_date'] = None
        raw['strategy']['symbols'] = []
        self.path.write_text(json.dumps(raw))
        self.assertEqual(prepare(self.project(),1,self.env).stage,1)
        with self.assertRaises(ValueError): prepare(self.project(),2,self.env)

    def test_requires_execute_flag_for_any_trading_stage(self):
        with patch('mmbot.main.run') as run:
            for stage in (2,3,4):
                with self.assertRaises(ValueError): launch(self.project(),stage,self.env,execute=False)
            run.assert_not_called()

    def test_dispatches_same_runtime_and_records_exit_not_acceptance(self):
        with patch('mmbot.main.run',return_value={'events':3}) as run:
            launch(self.project(),3,self.env,execute=True,duration=2)
            kwargs = run.call_args.kwargs
            self.assertEqual(kwargs['strategy_settings'].stage,3)
            self.assertIsNone(kwargs['probe_plan'])
        record = json.loads((self.root/'data'/'workflow.json').read_text())
        self.assertEqual(record['last_run']['status'],'exited')
        self.assertNotIn('ak_',json.dumps(record))

    def test_exception_recorded_and_propagated(self):
        with patch('mmbot.main.run',side_effect=ValueError('准入未满足')):
            with self.assertRaises(ValueError): launch(self.project(),4,self.env,execute=True)
        record = json.loads((self.root/'data'/'workflow.json').read_text())
        self.assertEqual(record['last_run']['status'],'error')

    def test_environment_cannot_silently_change_state_directory(self):
        with self.assertRaises(ValueError):
            prepare(self.project(),1,dict(self.env,TRADING_DATA_DIR='/tmp/elsewhere'))

    def test_api_key_is_never_loaded_from_project_file(self):
        raw = copy.deepcopy(self.raw)
        raw['connection']['api_key'] = 'bad'
        self.path.write_text(json.dumps(raw))
        with self.assertRaises(ValueError): load_project(self.path)

    def test_status_needs_no_credentials_and_does_not_create_data(self):
        result = status(self.project())
        self.assertFalse(result['data_exists'])
        self.assertFalse((self.root/'data').exists())

    def test_invalid_duration_does_not_connect(self):
        with patch('mmbot.main.run') as run:
            with self.assertRaises(ValueError): launch(self.project(),1,self.env,duration=float('nan'))
            run.assert_not_called()

    def test_prepare_does_not_mutate_shared_config(self):
        project = self.project()
        before = copy.deepcopy(project.raw)
        prepare(project,2,self.env)
        prepare(project,3,self.env)
        self.assertEqual(project.raw,before)

    def test_concurrent_launcher_is_blocked_before_runtime(self):
        import fcntl
        data = self.root/'data'
        data.mkdir()
        with (data/'launcher.lock').open('a') as held, patch('mmbot.main.run') as run:
            fcntl.flock(held.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(RuntimeError): launch(self.project(),1,self.env)
            run.assert_not_called()

    def test_init_does_not_overwrite_existing_config(self):
        from mmbot.launcher import main
        before = self.path.read_text()
        with patch('sys.stderr'), patch('logging.basicConfig'):
            self.assertEqual(main(['--config',str(self.path),'--init']),1)
        self.assertEqual(self.path.read_text(),before)

    def test_interactive_key_is_not_persisted_or_added_to_environment(self):
        import os
        from mmbot.launcher import main
        key = 'ak_'+'z'*43
        with patch.dict(os.environ,{},clear=True),patch('sys.stdin.isatty',return_value=True), \
             patch('mmbot.launcher.getpass.getpass',return_value=key) as prompt, \
             patch('mmbot.main.run',return_value={}),patch('logging.basicConfig'):
            code = main(['--config',str(self.path),'--stage','1','--duration','1'])
            self.assertEqual(code,0)
            prompt.assert_called_once()
            self.assertNotIn('TRADING_API_KEY',os.environ)
        self.assertNotIn(key,self.path.read_text())
        self.assertNotIn(key,(self.root/'data'/'workflow.json').read_text())

    def test_check_without_api_key_is_offline(self):
        import os
        from mmbot.launcher import main
        with patch.dict(os.environ,{},clear=True),patch('mmbot.main.run') as run,patch('logging.basicConfig'):
            self.assertEqual(main(['--config',str(self.path),'--check','--stage','1']),0)
            run.assert_not_called()


if __name__ == '__main__': unittest.main()
