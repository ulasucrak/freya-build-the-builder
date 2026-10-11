"""Lists the Drive folder with gdown (paths + file ids, no download) and downloads every file through Google's
direct download link into a staging folder. Prints the staging folder. Nothing downloaded is executed."""
import sys, urllib.request
import gdown

url, stage = sys.argv[1], sys.argv[2]
files = gdown.download_folder(url, output=stage, skip_download=True, quiet=True) or []
for f in files:
    # usercontent + confirm=t: the real file even where Drive would show a virus-scan page (e.g. .js files)
    req = urllib.request.Request("https://drive.usercontent.google.com/download?export=download&confirm=t&id=" + f.id,
                                 headers={"User-Agent": "Mozilla/5.0"})
    import os
    os.makedirs(os.path.dirname(f.local_path), exist_ok=True)
    with urllib.request.urlopen(req, timeout=120) as r, open(f.local_path, "wb") as out:
        out.write(r.read())
print(len(files))
