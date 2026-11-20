from curl_cffi import requests
import json

r = requests.get('https://hk.jobsdb.com/zh/ESG-jobs', impersonate='chrome124')
html = r.text

i = html.find('window.SEEK_REDUX_DATA')
s = html.find('{', i)
depth = 0
end = -1
instr = False
esc = False
for j in range(s, len(html)):
    c = html[j]
    if esc:
        esc = False
        continue
    if c == '\\':
        esc = True
        continue
    if c == '"':
        instr = not instr
        continue
    if instr:
        continue
    if c == '{':
        depth += 1
    elif c == '}':
        depth -= 1
        if depth == 0:
            end = j + 1
            break

d = json.loads(html[s:end])
print("TOP KEYS:", list(d.keys()))

def walk(o, path='', dep=0):
    if dep > 5:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            p = path + '/' + k
            if isinstance(v, list):
                print(f"{p}  list[{len(v)}]")
                if v and isinstance(v[0], dict):
                    print(f"    首元素字段: {list(v[0].keys())[:15]}")
            elif isinstance(v, dict):
                print(f"{p}  dict")
                walk(v, p, dep + 1)

walk(d)