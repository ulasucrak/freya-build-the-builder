"""Turn latency of a run: user message -> the agent has finished speaking, by node, by tools called, and by component.
    python tools/turn_latency.py <turns.json to write> <results/run-xxx.json>

Timing facts (measured, see HANDOFF section 8):
- An assistant message's secondsFromStart is when its generation STARTED. When it FINISHED is the epoch-ms suffix of its
  messageId (msg_N_<epochms>). All other events (user, route_evaluated, function_call_*, node_enter) carry wall-clock times.
  The call's time zero is epoch(any non-assistant message) - its secondsFromStart.
- Turn = user message -> end of the last assistant message before the next user message (pipeline code/api nodes included).
- Components: extract = user -> first event (extraction + routing); decide = model writing a tool call (event before it ->
  function_call_start); tool = function_call_start -> complete (pre-hook + call + post-hook, not separable in the log);
  speak = assistant generation start -> finished. Extraction wait inside a node shows as an 'extract' gap too."""
import json, statistics as st, collections, sys, time, concurrent.futures as cf, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import freya

def epoch(m):
    try: return int(m['messageId'].rsplit('_', 1)[1]) / 1000
    except Exception: return None

def analyse(c):
    """Turns of one call: list of dicts {node, tools, total, extract, decide, tool, speak}."""
    ms = c.get('messages', [])
    e0 = next((epoch(m) - float(m['secondsFromStart']) for m in ms if m['role'] != 'assistant' and epoch(m) and m.get('secondsFromStart') is not None), None)
    if e0 is None: return []
    turns, cur, prev_t, prev_ev, node = [], None, None, None, None
    def close():
        if cur and cur['end'] is not None:
            cur['total'] = cur['end'] - cur['t0']; cur['tools'] = tuple(sorted(set(cur['tools']))); turns.append(cur)
    for m in ms:
        if m.get('nodeId'): node = m['nodeId']
        t = m.get('secondsFromStart')
        if t is None: continue
        t = float(t)
        if m['role'] == 'user':
            close(); cur = dict(node=node, t0=t, end=None, tools=[], extract=None, decide=0.0, tool=0.0, speak=0.0); prev_t, prev_ev = t, 'user'; continue
        if cur is None: continue
        if m['role'] == 'assistant':
            ep = epoch(m); end = (ep - e0) if ep else t
            cur['speak'] += max(0.0, end - t); cur['end'] = max(cur['end'] or 0, end); cur['node'] = m.get('nodeId') or cur['node']
            prev_t, prev_ev = max(t, end), 'assistant'; continue
        try: ev = json.loads(m.get('message', '')).get('event'); fn = json.loads(m['message']).get('function')
        except Exception: continue
        if cur['extract'] is None: cur['extract'] = t - cur['t0']
        if ev == 'function_call_start':
            cur['tools'].append(fn)
            if prev_ev in ('user', 'route_evaluated', 'node_enter', 'assistant', 'variable_extracted'): cur['decide'] += max(0.0, t - prev_t)
            cur['_s'] = t
        elif ev in ('function_call_complete', 'function_call_error') and '_s' in cur:
            cur['tool'] += t - cur.pop('_s'); cur['end'] = max(cur['end'] or 0, t)
        elif ev == 'route_evaluated': cur['end'] = max(cur['end'] or 0, t) if cur['end'] is not None else None
        prev_t, prev_ev = t, ev
    close()
    return turns

def calls_turns(cid):
    for k in range(4):
        try: return analyse(freya.get_call(cid))
        except Exception: time.sleep(3 * (k + 1))
    return []

def pct(xs, p): return xs[min(len(xs) - 1, int(p * len(xs)))]
def show(label, xs):
    xs = sorted(xs)
    if xs: print(f"{label:44} n={len(xs):5} median {st.median(xs):5.2f}s  p90 {pct(xs, .9):5.2f}s  max {xs[-1]:5.1f}s  mean {st.mean(xs):5.2f}s")

if __name__ == '__main__':
    d = json.load(open(sys.argv[2] if len(sys.argv) > 2 else 'results/run-edge-250.json'))
    ids = [r['call_id'] for r in d['rows'] if r.get('call_id')]
    with cf.ThreadPoolExecutor(4) as ex: allt = [x for r in ex.map(calls_turns, ids) for x in r]
    json.dump(allt, open(sys.argv[1], 'w'))
    show('TURN TOTAL (user -> agent finished)', [x['total'] for x in allt])
    print('components, over all turns (0 where absent) / over turns where present:')
    for k, label in (('extract', 'user -> first event (extraction+routing)'), ('decide', 'decide (model emits tool call)'), ('tool', 'tool (pre-hook+call+post-hook)'), ('speak', 'speak (dialogue generation)')):
        show('  ' + label, [x[k] for x in allt if x[k] is not None])
        if k in ('decide', 'tool'): show('    only turns with a tool', [x[k] for x in allt if x['tools']])
    by = collections.defaultdict(list)
    for x in allt: by[x['node']].append(x['total'])
    for n, xs in sorted(by.items(), key=lambda kv: -len(kv[1])): show('node ' + str(n), xs)
    bt = collections.defaultdict(list)
    for x in allt: bt[','.join(t for t in x['tools'] if t != 'code_exec') or '(no tool)'].append(x['total'])
    for k, xs in sorted(bt.items(), key=lambda kv: -len(kv[1]))[:10]: show('tools ' + k, xs)
