# Pre-hook of every api tool of the generic workflow. The model (and the baked constants / call variables) fill flat
# body fields; the service expects nested objects (e.g. an envelope with a data and a call object). RT["envelope"]:
#   paths    {flat field: [[key, key, ...], type]}   where the field goes in the real body, and its schema type
#   objects  [[key, ...], ...]                       objects of the schema that are always sent (may stay empty)
# Unresolved placeholders and missing values are left out; numbers and strings are coerced to the schema type. A flat
# field the map does not know is sent at the top level as it came.
env = RT.get("envelope") or dict()
paths = env.get("paths") or dict()
flat = body_of(extra)


def unset(v):
    if v is None:
        return True
    s = str(v).strip()
    return s.startswith("{" + "{") and s.endswith("}" + "}")


def coerce(v, typ):
    try:
        if typ in ("integer", "number") and isinstance(v, str) and re.fullmatch(r"\s*-?\d+(?:[.,]\d+)?\s*", v):
            n = float(v.strip().replace(",", "."))
            return int(n) if typ == "integer" and n == int(n) else n
        if typ == "string" and isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(int(v)) if float(v) == int(v) else str(v)
        if typ == "boolean" and isinstance(v, str) and v.strip().lower() in ("true", "false"):
            return v.strip().lower() == "true"
    except Exception:
        pass
    return v


def place(out, keys, value):
    cur = out
    for k in keys[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = dict()
            cur[k] = nxt
        cur = nxt
    if keys:
        cur[keys[-1]] = value


body = dict()
for obj in env.get("objects") or []:
    if isinstance(obj, list) and obj:
        place(body, obj, dict())
for k, v in flat.items():
    if unset(v):
        continue
    spec = paths.get(k)
    if isinstance(spec, list) and spec and isinstance(spec[0], list) and spec[0]:
        place(body, spec[0], coerce(v, spec[1] if len(spec) > 1 else ""))
    else:
        body[k] = v
if flat or body:
    extra["body"] = body
