"""Transcripts of chosen cases from a run_cases --out file: speech, tool calls (args) and results, node changes.
    python tools/fail_transcripts.py <case ids, comma-separated | FAILED> <out dir> <results/run-xxx.json>"""
import json, sys
sys.path.insert(0, '.')
import freya
d = json.load(open(sys.argv[3] if len(sys.argv) > 3 else 'results/run-edge-250.json'))
want = sys.argv[1].split(',') if sys.argv[1] != 'FAILED' else [r['id'] for r in d['rows'] if not r['pass']]
OUT = sys.argv[2]
for r in d['rows']:
    if r['id'] not in want or not r.get('call_id'): continue
    import time
    for k in range(4):
        try: c = freya.get_call(r['call_id']); break
        except Exception: time.sleep(3 * (k + 1))
    else: continue
    lines = [f"#### {r['id']}  call {r['call_id']}  ended={c.get('endedReason')}"]
    for m in c.get('messages', []):
        t = m.get('message', ''); role = m['role']
        if role in ('assistant', 'user'):
            lines.append(f"{'A' if role=='assistant' else 'U'}: {t}")
            continue
        try: e = json.loads(t)
        except Exception: continue
        ev = e.get('event')
        if ev == 'node_enter': lines.append(f"   [node {e.get('node')}]")
        elif ev == 'function_call_start':
            a = dict(e.get('args') or {}); a.pop('code', None); a.pop('timeout_ms', None)
            p = a.get('params') or a
            if isinstance(p, dict): p = {k: v for k, v in p.items() if k not in ('code', 'timeout_ms', 'customer_id', 'url', 'method', 'headers')}
            lines.append(f"   >> {e.get('function')} {json.dumps(p)[:300]}")
        elif ev == 'function_call_complete':
            res = e.get('result') or {}
            data = res.get('data') if isinstance(res, dict) else res
            lines.append(f"   << {e.get('function')} {json.dumps(data)[:400]}")
    __import__('os').makedirs(OUT, exist_ok=True); open(f"{OUT}/{r['id']}.txt", 'w').write("\n".join(lines))
    print(r['id'], len(lines))
