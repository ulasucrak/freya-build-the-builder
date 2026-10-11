# Pre-hook of a document-mode service: put the flat tool fields into the request envelope the bank's documents
# describe (fields under one key, plus constant parts). The tool itself keeps flat fields, the verified form.
config = extra
env = RT.get("envelope") or dict()
key = str(env.get("fields_under") or "")
if key:
    body = body_of(config)
    wrapped = dict()
    wrapped[key] = dict(body)
    for k, v in (env.get("constant") or dict()).items():
        wrapped[k] = v
    config["body"] = wrapped
