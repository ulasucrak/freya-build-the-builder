"""Turn latency of a run: user message -> next agent message, by node and by tools called in between.
    python tools/turn_latency.py <turns.json to write> <results/run-xxx.json>"""
import json, statistics as st, collections, sys, concurrent.futures as cf
sys.path.insert(0, '.')
import freya
d = json.load(open(sys.argv[2] if len(sys.argv) > 2 else 'results/run-edge-250.json'))
ids = [r['call_id'] for r in d['rows'] if r.get('call_id')]
def turns(cid):
    import time
    for k in range(4):
        try: c = freya.get_call(cid); break
        except Exception: time.sleep(3 * (k + 1))
    else: return []
    out = []; last = None; node = None; tools = []
    for m in c.get('messages', []):
        if m.get('nodeId'): node = m['nodeId']
        t = m.get('secondsFromStart')
        if m['role'] == 'system':
            try: e = json.loads(m.get('message', ''))
            except Exception: continue
            if e.get('event') == 'function_call_start' and last is not None: tools.append(e.get('function'))
            continue
        if t is None: continue
        if m['role'] == 'user': last, tools = t, []
        elif m['role'] == 'assistant' and last is not None:
            out.append((node, tuple(sorted(set(tools))), t - last)); last = None
    return out
with cf.ThreadPoolExecutor(4) as ex: allt = [x for r in ex.map(turns, ids) for x in r]
json.dump(allt, open(sys.argv[1], 'w'))
def show(label, xs):
    xs = sorted(xs); print(f"{label:44} n={len(xs):5} median {st.median(xs):5.1f}s  p90 {xs[int(.9*len(xs))]:5.1f}s  max {xs[-1]:5.1f}s")
show('all turns', [x[2] for x in allt])
by = collections.defaultdict(list)
for n, tl, dt in allt: by[n].append(dt)
for n, xs in sorted(by.items(), key=lambda kv: -len(kv[1])): show('node ' + str(n), xs)
bt = collections.defaultdict(list)
for n, tl, dt in allt: bt[','.join(x for x in tl if x not in ('code_exec',)) or '(no tool)'].append(dt)
for k, xs in sorted(bt.items(), key=lambda kv: -len(kv[1]))[:10]: show('tools ' + k, xs)
