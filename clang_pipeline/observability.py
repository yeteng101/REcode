from __future__ import annotations

import contextvars
import json
import math
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

_USAGE_SINK = contextvars.ContextVar('recode_usage_sink', default=None)


def record_usage(model, usage, *, provider='openai', cost_usd=None):
    sink = _USAGE_SINK.get()
    if sink is not None:
        sink(model, usage or {}, provider=provider, cost_usd=cost_usd)


class Observability:
    """Durable metrics and append-only application audit in the queue database."""
    def __init__(self, queue):
        self.queue = queue
        with queue.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, time REAL NOT NULL, action TEXT NOT NULL,
                    subject TEXT NOT NULL, outcome TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS spans (
                    job_id TEXT NOT NULL, attempt TEXT NOT NULL, span_id TEXT NOT NULL,
                    time REAL NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(job_id, attempt, span_id));
                CREATE TABLE IF NOT EXISTS attempts (
                    job_id TEXT NOT NULL, claim TEXT NOT NULL, time REAL NOT NULL,
                    status TEXT NOT NULL, duration REAL NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(job_id,claim));
                CREATE TABLE IF NOT EXISTS usage (
                    id INTEGER PRIMARY KEY, time REAL NOT NULL, model TEXT NOT NULL,
                    provider TEXT NOT NULL, input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL, cost_usd REAL);
                CREATE TABLE IF NOT EXISTS alert_state (
                    name TEXT PRIMARY KEY, active INTEGER NOT NULL, updated REAL NOT NULL,
                    message TEXT NOT NULL);
            ''')

    def audit(self, action, subject='', outcome='ok'):
        # Deliberately do not persist bodies, questions, credentials or query strings.
        with self.queue.transaction() as db:
            db.execute('INSERT INTO audit(time,action,subject,outcome) VALUES(?,?,?,?)',
                       (time.time(), str(action)[:80], str(subject).split('?')[0][:300], str(outcome)[:80]))

    @contextmanager
    def usage_scope(self):
        token = _USAGE_SINK.set(self.usage)
        try:
            yield
        finally:
            _USAGE_SINK.reset(token)

    def usage(self, model, usage, *, provider='openai', cost_usd=None):
        incoming = max(0, int(usage.get('input_tokens', usage.get('prompt_tokens', 0))))
        outgoing = max(0, int(usage.get('output_tokens', usage.get('completion_tokens', 0))))
        if cost_usd is None:
            prices = json.loads(os.environ.get('CRA_MODEL_PRICES_JSON', '{}')).get(str(model))
            if prices:
                cost_usd = (incoming * float(prices['input_per_million']) + outgoing * float(prices['output_per_million'])) / 1_000_000
        if cost_usd is not None and (not math.isfinite(float(cost_usd)) or float(cost_usd) < 0):
            raise ValueError('invalid token cost')
        with self.queue.transaction() as db:
            db.execute('INSERT INTO usage(time,model,provider,input_tokens,output_tokens,cost_usd) VALUES(?,?,?,?,?,?)',
                       (time.time(), str(model)[:100], provider, incoming, outgoing, cost_usd))

    def ingest(self, state):
        job_dir = Path(state['job_dir'])
        if job_dir.is_symlink() or not job_dir.exists():
            return
        paths = sorted(job_dir.glob('attempt-*/trace/tool-trace.jsonl'))
        paths += sorted(job_dir.glob('attempt-*/analysis/trace/tool-trace.jsonl'))
        for path in paths:
            if path.is_symlink() or not path.resolve().is_relative_to(job_dir.resolve()):
                continue
            # Bound per-file ingestion; never execute or render repository markup.
            with path.open(encoding='utf-8', errors='replace') as stream:
                consumed = 0
                for line in stream:
                    consumed += len(line)
                    if consumed > 16 * 1024 * 1024:
                        break
                    try:
                        row = json.loads(line)
                        span_id = str(row['span_id'])
                        from .tool_trace import _sanitize
                        payload = json.dumps(_sanitize(row), ensure_ascii=False)
                    except (ValueError, KeyError, TypeError):
                        continue  # tolerate the incomplete last line of a running trace
                    try:
                        occurred = datetime.fromisoformat(str(row.get('finished_at', '')).replace('Z','+00:00')).timestamp()
                    except ValueError:
                        occurred = time.time()
                    with self.queue.transaction() as db:
                        db.execute('INSERT OR IGNORE INTO spans VALUES(?,?,?,?,?)',
                            (state['job_id'], str(path.parent.parent.relative_to(job_dir)), span_id, occurred, payload))

    def attempt(self, state, claim, status, duration, result):
        self.ingest(state)
        labels = []
        root = Path(state['output_dir'])
        for relative in ('external/race-analysis.json', 'analysis/external/race-analysis.json'):
            path = root / relative
            if path.is_file() and path.stat().st_size < 16 * 1024 * 1024:
                data = json.loads(path.read_text(encoding='utf-8'))
                items = [item.get('label') for item in data.get('items', []) if item.get('label')]
                labels += items or ([data['label']] if data.get('label') else [])
        payload = {'unknown': sum(label == 'unknown' for label in labels), 'answers': len(labels),
                   'resources': (result or {}).get('resources'),
                   'sandbox': (result or {}).get('sandbox')}
        with self.queue.transaction() as db:
            db.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?,?,?,?)',
                       (state['job_id'], claim, time.time(), status, duration, json.dumps(payload)))
        self.audit('job.attempt', state['job_id'], status)

    def trace(self, job_id, *, limit=500, offset=0):
        state = self.queue.get(job_id)
        self.ingest(state)
        with self.queue.transaction() as db:
            rows = db.execute('SELECT attempt,payload FROM spans WHERE job_id=? ORDER BY time,attempt,span_id LIMIT ? OFFSET ?',
                              (job_id, min(500, max(1, limit)), max(0, offset))).fetchall()
            total = db.execute('SELECT COUNT(*) FROM spans WHERE job_id=?', (job_id,)).fetchone()[0]
        return {'job_id': job_id, 'total': total, 'offset': offset,
                'events': [{'attempt': row['attempt'], **json.loads(row['payload'])} for row in rows],
                'lifecycle': state['trace']}

    def snapshot(self, window=86400):
        now = time.time()
        cutoff = now - window
        with self.queue.transaction() as db:
            jobs = [json.loads(row[0]) for row in db.execute('SELECT state FROM jobs ORDER BY rowid DESC LIMIT 50')]
            statuses = {row[0]: row[1] for row in db.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status')}
            spans = [json.loads(row[0]) for row in db.execute('SELECT payload FROM spans WHERE time>=?', (cutoff,))]
            attempts = [dict(row) for row in db.execute('SELECT * FROM attempts WHERE time>=? ORDER BY time', (cutoff,))]
            usage = dict(db.execute('SELECT COALESCE(SUM(input_tokens),0) AS input_tokens, COALESCE(SUM(output_tokens),0) AS output_tokens, SUM(cost_usd) AS cost_usd, COUNT(*) AS calls, COUNT(cost_usd) AS priced_calls FROM usage WHERE time>=?', (cutoff,)).fetchone())
            audit = [dict(row) for row in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 50')]
        terminal_count = sum(statuses.get(key, 0) for key in ('succeeded','failed','timed_out','cancelled'))
        success = statuses.get('succeeded', 0) / terminal_count if terminal_count else None
        errors = sum(s.get('status') in {'error','failed','timed_out','cancelled'} for s in spans)
        stages = {}
        for span in spans:
            name = str(span.get('tool', 'unknown'))
            bucket = stages.setdefault(name, {'calls': 0, 'errors': 0, 'total_ms': 0})
            bucket['calls'] += 1
            bucket['errors'] += span.get('status') in {'error','failed','timed_out','cancelled'}
            bucket['total_ms'] += max(0, float(span.get('duration_ms', 0)))
        for bucket in stages.values():
            bucket['mean_ms'] = bucket['total_ms'] / bucket['calls']
        payloads = [json.loads(a['payload']) for a in attempts]
        unknown = sum(p['unknown'] for p in payloads)
        answers = sum(p['answers'] for p in payloads)
        metrics = {'generated_at': now, 'window_seconds': window, 'jobs': statuses,
            'job_success_rate': success, 'job_terminal_count': terminal_count,
            'tool_failure_rate': errors / len(spans) if spans else None,
            'tool_calls': len(spans), 'tool_failures': errors, 'stages': stages,
            'usage': usage, 'unknown_rate': unknown / answers if answers else None,
            'unknown_count': unknown, 'answer_count': answers,
            'attempt_count': len(attempts), 'audit': audit,
            'resources': [{'job_id': a['job_id'], **json.loads(a['payload'])} for a in attempts[-20:]],
            'recent_jobs': [{k: j.get(k) for k in ('job_id','status','repository_url','attempts','created_at')} for j in sorted(jobs, key=lambda x:x['created_at'], reverse=True)[:50]]}
        metrics['alerts'] = self.evaluate_alerts(metrics)
        return metrics

    def evaluate_alerts(self, metrics):
        rules = {
            'job_failure_rate': (metrics['job_terminal_count'] >= int(os.environ.get('CRA_ALERT_MIN_JOBS','5')) and metrics['job_success_rate'] is not None and metrics['job_success_rate'] < float(os.environ.get('CRA_ALERT_SUCCESS_FLOOR','.8')), 'task success rate below threshold'),
            'tool_failure_rate': (metrics['tool_calls'] >= 5 and (metrics['tool_failure_rate'] or 0) > float(os.environ.get('CRA_ALERT_TOOL_FAILURE_CEILING','.2')), 'tool failure rate above threshold'),
            'unknown_rate': (metrics['answer_count'] >= 5 and (metrics['unknown_rate'] or 0) > float(os.environ.get('CRA_ALERT_UNKNOWN_CEILING','.3')), 'unknown rate above threshold'),
            'token_budget': ((metrics['usage']['cost_usd'] or 0) > float(os.environ.get('CRA_ALERT_DAILY_COST_USD','100')), 'priced token usage exceeds budget'),
        }
        rules['queue_backlog'] = (metrics['jobs'].get('queued', 0) > int(os.environ.get('CRA_ALERT_QUEUE_LIMIT','20')), 'queued tasks exceed threshold')
        rules['resource_pressure'] = (any(float((item.get('resources') or {}).get('memory_percent', 0)) > float(os.environ.get('CRA_ALERT_MEMORY_PERCENT','90')) for item in metrics['resources']), 'container memory sample exceeds threshold')
        with self.queue.transaction() as db:
            for name, (active, message) in rules.items():
                old = db.execute('SELECT active FROM alert_state WHERE name=?', (name,)).fetchone()
                if old is None or bool(old[0]) != bool(active):
                    db.execute('INSERT OR REPLACE INTO alert_state VALUES(?,?,?,?)', (name, int(active), time.time(), message))
                    if active or old is not None:
                        db.execute('INSERT INTO audit(time,action,subject,outcome) VALUES(?,?,?,?)',
                            (time.time(), 'alert.transition', name, 'firing' if active else 'resolved'))
            return [dict(row) for row in db.execute('SELECT * FROM alert_state ORDER BY name')]

    def prometheus(self):
        data = self.snapshot()
        lines = ['# TYPE recode_jobs gauge']
        for status, count in data['jobs'].items():
            lines.append(f'recode_jobs{{status="{status}"}} {count}')
        for name in ('job_success_rate','tool_failure_rate','unknown_rate','tool_calls','tool_failures','unknown_count','answer_count','job_terminal_count'):
            if data[name] is not None:
                lines.append(f'recode_{name} {data[name]}')
        for name in ('input_tokens','output_tokens','cost_usd','calls','priced_calls'):
            if data['usage'][name] is not None:
                lines.append(f'recode_usage_{name} {data["usage"][name]}')
        for name, stage in data['stages'].items():
            label = json.dumps(name)
            lines.append(f'recode_stage_duration_seconds_sum{{tool={label}}} {stage["total_ms"]/1000}')
            lines.append(f'recode_stage_duration_seconds_count{{tool={label}}} {stage["calls"]}')
        for alert in data['alerts']:
            lines.append(f'recode_alert_active{{name="{alert["name"]}"}} {alert["active"]}')
        return '\n'.join(lines) + '\n'

    def prune(self, days=30):
        cutoff = time.time() - days * 86400
        with self.queue.transaction() as db:
            for table in ('audit','spans','attempts','usage'):
                db.execute(f'DELETE FROM {table} WHERE time<?', (cutoff,))
