from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from clang_pipeline.agent_jobs import AnalysisJobManager
from clang_pipeline.observability import record_usage
from clang_pipeline.tests.test_durable_jobs import URL, runner, wait_for
from clang_pipeline.tests.test_job_http import JobHTTPTests


class ObservabilityTests(unittest.TestCase):
    def test_metrics_trace_dedup_restart_and_unknown(self):
        def analysis(url, *, output_dir, **kwargs):
            external = output_dir / 'external'
            external.mkdir(parents=True)
            (external/'race-analysis.json').write_text(json.dumps({'items':[{'label':'unknown'},{'label':'race'}]}))
            result = runner(url, output_dir=output_dir, **kwargs)
            result['resources']={'memory':'12MiB','cpu_percent':'1%'}
            return result
        with tempfile.TemporaryDirectory() as root:
            with AnalysisJobManager(root, runner=analysis, max_workers=1) as manager:
                job = manager.submit(URL)
                wait_for(manager, job['job_id'])
                first = manager.observability.trace(job['job_id'])
                second = manager.observability.trace(job['job_id'])
                self.assertEqual(first['total'],second['total'])
                self.assertGreater(first['total'],0)
                snapshot = manager.observability.snapshot()
                self.assertEqual(snapshot['job_success_rate'],1)
                self.assertEqual(snapshot['unknown_rate'],.5)
                self.assertEqual(snapshot['attempt_count'],1)
                self.assertTrue(snapshot['stages'])
                self.assertEqual(snapshot['resources'][0]['resources']['memory'],'12MiB')
                self.assertIn('recode_stage_duration_seconds_sum', manager.observability.prometheus())
            with AnalysisJobManager(root,max_workers=0) as restarted:
                self.assertEqual(restarted.observability.snapshot()['attempt_count'],1)
                self.assertEqual(restarted.observability.trace(job['job_id'])['total'],first['total'])

    def test_usage_pricing_unknown_cost_and_alert_transitions(self):
        with tempfile.TemporaryDirectory() as root:
            with AnalysisJobManager(root,max_workers=0) as manager:
                telemetry = manager.observability
                with patch.dict('os.environ', {'CRA_MODEL_PRICES_JSON':'{"model":{"input_per_million":1,"output_per_million":2}}'}):
                    with telemetry.usage_scope():
                        record_usage('model',{'input_tokens':1000000,'output_tokens':500000})
                        record_usage('unpriced',{'prompt_tokens':10,'completion_tokens':20})
                snapshot = telemetry.snapshot()
                self.assertEqual(snapshot['usage']['cost_usd'],2)
                self.assertEqual(snapshot['usage']['priced_calls'],1)
                self.assertEqual(snapshot['usage']['calls'],2)
                with patch.dict('os.environ', {'CRA_ALERT_DAILY_COST_USD':'1'}):
                    telemetry.snapshot()
                    telemetry.snapshot()
                telemetry.snapshot()
                with manager.queue.transaction() as db:
                    transitions = db.execute("SELECT outcome FROM audit WHERE action='alert.transition' AND subject='token_budget'").fetchall()
                self.assertEqual([row[0] for row in transitions],['firing','resolved'])

    def test_partial_jsonl_and_pagination(self):
        with tempfile.TemporaryDirectory() as root:
            with AnalysisJobManager(root,max_workers=0) as manager:
                job = manager.submit(URL)
                state, _, claim = manager.queue.claim('manual')
                path=Path(state['tool_trace_path'])
                path.parent.mkdir(parents=True)
                path.write_text('{"span_id":"s1","tool":"clang","status":"error","duration_ms":50}\n{"span_id":',encoding='utf-8')
                trace=manager.observability.trace(job['job_id'])
                self.assertEqual(trace['total'],1)
                self.assertEqual(manager.observability.trace(job['job_id'],offset=1)['events'],[])
                self.assertEqual(manager.observability.snapshot()['tool_failure_rate'],1)
                manager.queue.release('manual')


class ObservabilityHTTPTests(JobHTTPTests):
    def test_dashboard_metrics_and_trace_access(self):
        root=self.base.split('/api/')[0]
        with urlopen(root+'/dashboard') as response:
            self.assertIn('服务运行概览',response.read().decode('utf-8-sig'))
            self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
        with patch.dict('os.environ',{'CRA_ADMIN_TOKEN':'test-admin'}):
            with self.assertRaises(HTTPError) as error:
                urlopen(root+'/api/v1/observability')
            self.assertEqual(error.exception.code,401)
            request=Request(root+'/api/v1/observability',headers={'Authorization':'Bearer test-admin'})
            with urlopen(request) as response:
                self.assertIn('job_success_rate',json.load(response))
            with urlopen(Request(root+'/metrics',headers={'Authorization':'Bearer test-admin'})) as response:
                self.assertIn('recode',response.read().decode())
        status,_,_=self.request(body={'repository_url':URL})
        self.assertEqual(status,202)
        with self.manager.queue.transaction() as db:
            self.assertGreater(db.execute('SELECT COUNT(*) FROM audit').fetchone()[0],0)

    def test_production_host_question_is_disabled(self):
        root=self.base.split('/api/')[0]
        request=Request(root+'/api/v1/agent/ask', data=b'{"question":"read secrets","workspace":"/"}')
        with patch.dict('os.environ',{'CRA_ENV':'production','CRA_SANDBOX_MODE':'docker'}):
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code,403)


if __name__ == '__main__':
    unittest.main()
