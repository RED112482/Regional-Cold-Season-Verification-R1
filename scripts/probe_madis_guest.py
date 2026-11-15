#!/usr/bin/env python3
from html.parser import HTMLParser
import json, urllib.request
from pathlib import Path

URL="https://madis-data.ncep.noaa.gov/public/sfcdumpguest.shtml"
OUT=Path("site/data/madis_guest_form.json")

class P(HTMLParser):
    def __init__(self):
        super().__init__(); self.forms=[]; self.cur=None; self.select=None
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=="form":
            self.cur={"action":a.get("action",""),"method":a.get("method","get"),"inputs":[],"selects":[]}
            self.forms.append(self.cur)
        elif self.cur is not None and tag=="input":
            self.cur["inputs"].append({k:v for k,v in a.items() if k in ("name","type","value","checked")})
        elif self.cur is not None and tag=="select":
            self.select={"name":a.get("name"),"options":[]}; self.cur["selects"].append(self.select)
        elif self.select is not None and tag=="option":
            self.select["options"].append({"value":a.get("value",""),"selected":"selected" in a})
    def handle_endtag(self,tag):
        if tag=="select": self.select=None
        elif tag=="form": self.cur=None

req=urllib.request.Request(URL,headers={"User-Agent":"Mozilla/5.0"})
with urllib.request.urlopen(req,timeout=45) as r:
    txt=r.read().decode("utf-8","replace")
p=P(); p.feed(txt)
OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text(json.dumps({"url":URL,"forms":p.forms},indent=2))
print(json.dumps({"forms":p.forms},indent=2))
