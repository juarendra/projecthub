#!/bin/bash
# ProjectHub task updater untuk OpenClaw/WA.
#   task.sh find "<kata>"
#   task.sh update <id> <done|inprogress|todo|review|selesai> ["catatan"]
export PATH=/usr/local/bin:/usr/bin:/bin
cd /home/rendra/projecthub || exit 1
BASE=http://127.0.0.1:5055
IK=$(python3 -c "import json,hmac,hashlib;a=json.load(open('data/auth.json'));print(hmac.new(bytes.fromhex(a['secret']),b'internal-digest',hashlib.sha256).hexdigest())")

case "$1" in
  find)
    Q=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$2")
    OUT=$(curl -s "$BASE/api/digest/tasks?key=$IK&q=$Q")
    echo "$OUT" | python3 -c 'import sys,json
ts=json.load(sys.stdin).get("tasks",[])
print("(tidak ada task cocok)") if not ts else [print("#%d [%s] %s (%s)"%(t["id"],t["status"],t["title"],t["lname"])) for t in ts]'
    ;;
  update)
    BODY=$(FIELD="$3" NOTE="$4" python3 -c 'import os,json
d={}
f=(os.environ.get("FIELD") or "").strip()
if f.lower() in ("done","selesai","kelar","beres","complete"): d["done"]=True
elif f: d["status"]=f
n=(os.environ.get("NOTE") or "").strip()
if n: d["note"]=n
print(json.dumps(d))')
    OUT=$(curl -s -X POST "$BASE/api/digest/task/$2?key=$IK" -H "Content-Type: application/json" -d "$BODY")
    echo "$OUT" | python3 -c 'import sys,json
r=json.load(sys.stdin)
if r.get("ok"):
    t=r["task"]; print("OK: #%d %s -> %s"%(t["id"],t["title"],t["status"])+((" (selesai "+t["completed_at"]+")") if t.get("completed_at") else ""))
else: print("GAGAL:", r.get("error") or r)'
    ;;
  *) echo "usage: task.sh find <kata> | task.sh update <id> <done|inprogress|todo|review> [catatan]";;
esac
