#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
티머니GO 광고 성과 통합뷰 — 데이터 생성기
  dashboard_template.html 의 __DATA__ 자리를 채워 dashboard.html 을 만든다.

사용법
  python generate.py --dummy                          # 더미로 생성(토큰 불필요, 모양 확인용)
  python generate.py --from 2026-10-01 --to 2026-10-06   # 실데이터 (AIRBRIDGE_API_TOKEN 필요)
  python generate.py                                   # 실데이터, 기본 최근 7일(어제까지)

토큰: 환경변수 AIRBRIDGE_API_TOKEN (Airbridge [Settings] > [Tokens]의 API Token)
API : POST /reports/api/v7/apps/{app}/actuals/query (비동기) → GET .../{taskId} 폴링
"""
import os, sys, json, time, argparse, datetime, random

APP = "tmoneygo"
TZ = datetime.timezone(datetime.timedelta(hours=9))   # KST
API_BASE = "https://api.airbridge.io"
# 템플릿 내장 (하단 TEMPLATE_HTML)
OUTFILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")

# ---- 이벤트/지표 키 (확정값) ----
M_SIGNUP="app_sign_up"; M_TPCARD="app_custom_register_tpcard"
M_LOUNGE="app_custom_view_lounge_main"            # 라운지 방문(건수) — 유저수 키 확인 후 교체 예정
M_LOUNGE_USERS="app_custom_users_view_lounge_main"  # 라운지 방문자수 (설정값복사 확정: 커스텀 유저수는 app_custom_users_<이벤트>)
M_SIGNUP_USERS="app_sign_up_users"               # 회원가입 유저수
M_TPCARD_USERS="app_custom_users_register_tpcard" # 교통카드 유저수
VT={"dimension":"is_view_through","filterType":"IN","values":["false"]}  # 뷰스루 제외(에어브릿지 리포트와 동일)
M_TAXI_CALL="app_custom_purchase_started"         # + event_action=taxi
M_COST="cost_channel"
# 버킷별 primary/comp 이벤트 (event_action 필터값)
BUCKET_SPEC = {
  "taxi":  {"primary":(M_TAXI_CALL,["taxi"]),                 "comp":None},
  "rvtaxi":{"primary":("app_custom_purchase_rvtaxi_started",None),"comp":("app_custom_reserve_taxi_completed",None)},
  "pm":    {"primary":("app_custom_purchase_started",["scoot"]),"comp":("app_custom_purchase_started",["pass_scoot"])},  # 탑승=started@scoot, 이용권=started@pass_scoot (0이면 completed로 교체)
  "lounge":{"primary":(M_LOUNGE_USERS,None),                  "comp":None},  # 방문자수(유저). 전체=전체유저수, 광고/캠페인=채널·캠페인별 유저수(중복제거)
}
# 버킷별 "목적 캠페인" 판별 (캠페인명에 이 키워드가 있으면 그 버킷의 목적 광고로 인정)
PURPOSE = {
  "taxi":   ["택시","tROAS","부산택시","지방권"],
  "rvtaxi": ["예약택시"],
  "pm":     ["킥보드","자전거","라이딩","PM","스쿠터"],
  "lounge": ["라운지","대중교통"],
}
def is_purpose(bid, camp):
  camp=camp or ""
  return any(k in camp for k in PURPOSE.get(bid,[]))

# 채널 분류/표시
AD_PAID   = {"google.adwords","facebook.business","adison","naver_bsa","moloco","worldclass","qanda","toss"}  # 유료(광고)
DISPLAY   = {"google.adwords":"google.ads","facebook.business":"insta","unattributed":"오가닉(Organic)"}
def disp(ch): return DISPLAY.get(ch, ch)
def kind_of(ch):  # 버킷 섹션 분류
  if ch=="google.adwords": return "google"
  if ch=="facebook.business": return "insta"
  if ch in AD_PAID: return "etc"      # 비용 미연동 유료
  return "org"                         # 광고 외(오가닉·제휴·CRM)

# 대시보드 버킷 설정 (템플릿 BUCKETS 와 동일)
BUCKETS = [
  {"id":"taxi","label":"택시","primaryLabel":"택시호출","cpaLabel":"호출당 비용","showComp":False,"showOrg":True},
  {"id":"rvtaxi","label":"예약택시","primaryLabel":"예약택시호출","cpaLabel":"완료당 비용","showComp":True,"compLabel":"예약택시완료","showOrg":True},
  {"id":"pm","label":"전기자전거/킥보드","primaryLabel":"탑승","compLabel":"이용권 결제","cpaLabel":"탑승당 비용","compCpaLabel":"결제당 비용","showComp":True,"dualCpa":True,"showOrg":True},
  {"id":"lounge","label":"대중교통라운지","primaryLabel":"방문자수","cpaLabel":"방문자당 비용","showComp":False,"showOrg":True,"unit":"명","note":"방문자수(유저)는 중복 제거 기준이라 채널·캠페인 합이 전체와 정확히 일치하지 않을 수 있음"},
]

def daylist(frm,to):
  a=datetime.date.fromisoformat(frm); b=datetime.date.fromisoformat(to); out=[]
  while a<=b: out.append(a.isoformat()); a+=datetime.timedelta(days=1)
  return out

# ========================= Airbridge API =========================
def _session(insecure=False):
  import requests
  s=requests.Session()
  tok=os.environ.get("AIRBRIDGE_API_TOKEN")
  if not tok: sys.exit("환경변수 AIRBRIDGE_API_TOKEN 가 필요합니다 (실데이터 모드).")
  s.headers.update({"Authorization":"Bearer "+tok,"Content-Type":"application/json","Accept-Language":"ko"})
  if insecure:
    s.verify=False
    import urllib3; urllib3.disable_warnings()
    print("  ⚠️ SSL 검증 생략(--insecure) — 사내 신뢰망에서만 사용하세요")
  return s

def query(sess, frm, to, group_bys, metrics, filters=None, timeout=180):
  """Actuals 비동기 쿼리 → rows 반환. 각 row: {groupBys:[...], values:{metric:{value}}}"""
  url=f"{API_BASE}/reports/api/v7/apps/{APP}/actuals/query"
  body={"from":frm,"to":to,"groupBys":group_bys,"metrics":metrics}
  if filters: body["filters"]=filters
  r=sess.post(url,data=json.dumps(body)); r.raise_for_status()
  task_id=r.json()["task"]["taskId"]
  g=f"{url}/{task_id}"
  t0=time.time()
  while time.time()-t0<timeout:
    gr=sess.get(g,params={"skip":0,"size":10000,"viewFormat":"false"}); gr.raise_for_status()
    j=gr.json(); st=j.get("task",{}).get("status")
    if st=="SUCCESS": return j["actuals"]["data"]["rows"]
    if st in ("FAILED","ERROR"): raise RuntimeError(f"쿼리 실패: {json.dumps(j)[:400]}")
    time.sleep(2)
  raise TimeoutError("쿼리 타임아웃")

def val(row, metric): 
  v=row.get("values",{}).get(metric); return (v or {}).get("value",0) or 0

def to_daily(rows, days, key_idx, metric, want_key=None):
  """rows → {groupByKey: [day0..dayN]} (key_idx: event_date 인덱스는 0 가정, 키는 want_key 인덱스)"""
  idx={d:i for i,d in enumerate(days)}
  out={}
  for r in rows:
    gb=r.get("groupBys",[])
    date=gb[0] if gb else None
    if date not in idx: continue
    k = gb[want_key] if (want_key is not None and want_key<len(gb)) else "_"
    out.setdefault(k,[0]*len(days))[idx[date]] += val(r,metric)
  return out

# ========================= 실데이터 빌드 =========================
def build_live(frm,to,insecure=False):
  sess=_session(insecure); days=daylist(frm,to)
  # 1) 채널 비중 = 이벤트 수(합산 가능). 유저수는 채널별로 더하면 중복돼 틀림 → 비중엔 이벤트 수 사용
  r=query(sess,frm,to,["event_date","channel"],[M_SIGNUP_USERS,M_TPCARD_USERS,M_LOUNGE_USERS])
  su=to_daily(r,days,0,M_SIGNUP_USERS,1); card=to_daily(r,days,0,M_TPCARD_USERS,1); lo=to_daily(r,days,0,M_LOUNGE_USERS,1)
  chans=sorted(set(list(su)+list(card)+list(lo)), key=lambda c:-sum(su.get(c,[0])))
  channels=[{"name":disp(c),"ad":(c in AD_PAID),"su":su.get(c,[0]*len(days)),"card":card.get(c,[0]*len(days)),"lounge":lo.get(c,[0]*len(days))} for c in chans]
  # 1-b) KPI 총계 = 유저수(중복제거) 전체값 (그룹바이 없이)
  rt=query(sess,frm,to,["event_date"],[M_SIGNUP_USERS,M_TPCARD_USERS,M_LOUNGE_USERS])
  def totd(m): d=to_daily(rt,days,0,m); return d.get("_",[0]*len(days))
  totals={"su":totd(M_SIGNUP_USERS),"card":totd(M_TPCARD_USERS),"lounge":totd(M_LOUNGE_USERS)}
  # 2) 전체 택시호출 — 전체값은 귀속 필터(VT) 없이. VT를 걸면 오가닉(미귀속)이 빠져 전체가 과소집계됨
  r=query(sess,frm,to,["event_date"],[M_TAXI_CALL],[{"dimension":"event_action","filterType":"IN","values":["taxi"]}])
  taxitot=to_daily(r,days,0,M_TAXI_CALL)["_"] if r else [0]*len(days)
  # 3) 채널별 효율(광고): 가입+비용(필터 없이) / 택시호출(event_action=taxi) 따로
  adset=["google.adwords","facebook.business","adison","naver_bsa"]
  chf={"dimension":"channel","filterType":"IN","values":adset}
  r=query(sess,frm,to,["event_date","channel"],[M_SIGNUP_USERS],[chf,VT]); eff_su=to_daily(r,days,0,M_SIGNUP_USERS,1)
  r=query(sess,frm,to,["event_date","channel"],[M_COST],[chf]); eff_cost=to_daily(r,days,0,M_COST,1)
  r=query(sess,frm,to,["event_date","channel"],[M_TAXI_CALL],[chf,{"dimension":"event_action","filterType":"IN","values":["taxi"]},VT]); eff_call=to_daily(r,days,0,M_TAXI_CALL,1)
  adeff=[{"name":disp(c),"cost":eff_cost.get(c,[0]*len(days)),"su":eff_su.get(c,[0]*len(days)),"call":eff_call.get(c,[0]*len(days))} for c in adset]
  # 4) 버킷 4개
  rows=[]; camp={}; btot={}; n=len(days)
  def smap_ccag(r,metric):  # groupby [date,channel,campaign,ad_group] → {(ch,cp,ag):[days]}
    idx={d:i for i,d in enumerate(days)}; out={}
    for row in (r or []):
      gb=row.get("groupBys",[]); d=gb[0]
      if d not in idx: continue
      key=(gb[1], gb[2] if len(gb)>2 else "", gb[3] if len(gb)>3 else "")
      out.setdefault(key,[0]*len(days))[idx[d]] += val(row,metric)
    return out
  def smap_ch(r,metric):    # groupby [date,channel] → {ch:[days]}
    idx={d:i for i,d in enumerate(days)}; out={}
    for row in (r or []):
      gb=row.get("groupBys",[]); d=gb[0]
      if d not in idx: continue
      out.setdefault(gb[1],[0]*len(days))[idx[d]] += val(row,metric)
    return out
  for b in BUCKETS:
    bid=b["id"]; spec=BUCKET_SPEC[bid]
    pmet,pact=spec["primary"]
    ea_p=[{"dimension":"event_action","filterType":"IN","values":pact}] if pact else []
    cmet=None; ea_c=[]
    # 전체(btot): 전 채널·그룹바이 없이·VT 없이(오가닉 포함) → 에어브릿지 전체값과 일치
    rpt=query(sess,frm,to,["event_date"],[pmet],ea_p or None)
    bt={"call":to_daily(rpt,days,0,pmet).get("_",[0]*len(days))}
    # 광고 캠페인 행: adset + event_action + VT(click-through 귀속)
    rp=query(sess,frm,to,["event_date","channel","campaign","ad_group"],[pmet],[chf]+ea_p+[VT])
    # 광고 외(org) 채널 행: 채널 단위·VT 없이(오가닉 포함)
    rog=query(sess,frm,to,["event_date","channel"],[pmet],ea_p or None)
    rc=None; rogc=None
    if spec["comp"]:
      cmet,cact=spec["comp"]; ea_c=[{"dimension":"event_action","filterType":"IN","values":cact}] if cact else []
      rct=query(sess,frm,to,["event_date"],[cmet],ea_c or None)
      bt["comp"]=to_daily(rct,days,0,cmet).get("_",[0]*len(days))
      rc=query(sess,frm,to,["event_date","channel","campaign","ad_group"],[cmet],[chf]+ea_c+[VT])
      rogc=query(sess,frm,to,["event_date","channel"],[cmet],ea_c or None)
    btot[bid]=bt
    # 광고 캠페인 가입(VT)/비용(필터없음) — adset만
    rs=query(sess,frm,to,["event_date","channel","campaign","ad_group"],[M_SIGNUP_USERS],[chf,VT])
    rk=query(sess,frm,to,["event_date","channel","campaign","ad_group"],[M_COST],[chf])
    S_call=smap_ccag(rp,pmet); S_comp=smap_ccag(rc,cmet) if rc else {}
    S_su=smap_ccag(rs,M_SIGNUP_USERS); S_cost=smap_ccag(rk,M_COST)
    for (ch,cp,ag) in (set(S_call)|set(S_comp)|set(S_su)|set(S_cost)):
      cc=S_call.get((ch,cp,ag),[0]*n); mm=S_comp.get((ch,cp,ag),[0]*n)
      ss=S_su.get((ch,cp,ag),[0]*n);  kk=S_cost.get((ch,cp,ag),[0]*n)
      if sum(cc)==0 and sum(mm)==0: continue
      k=kind_of(ch)
      if k in ("google","insta","etc") and not is_purpose(bid, cp): k="xads"
      rows.append({"bucket":bid,"ch":k,"camp":(cp or disp(ch)),"ag":ag,"seg":"-","s":{"call":cc,"comp":mm,"su":ss,"cost":kk}})
    # org 채널 행 (VT 없는 채널 단위 집계에서 광고 외 채널만)
    O_call=smap_ch(rog,pmet); O_comp=smap_ch(rogc,cmet) if rogc else {}
    for ch in (set(O_call)|set(O_comp)):
      if kind_of(ch)!="org": continue
      cc=O_call.get(ch,[0]*n); mm=O_comp.get(ch,[0]*n)
      if sum(cc)==0 and sum(mm)==0: continue
      rows.append({"bucket":bid,"ch":"org","camp":disp(ch),"ag":"","seg":"-","s":{"call":cc,"comp":mm,"su":[0]*n,"cost":[0]*n}})
  # 5) 온보딩 전환율: Actuals로는 '가입자 코호트' 직접 산출 불가(퍼널/리텐션 필요) → 보류 플래그
  onb=[["교통카드 등록",[0]*len(days)],["택시호출",[0]*len(days)],["대중교통라운지 방문",[0]*len(days)],["고속/시외버스 결제",[0]*len(days)]]
  onb_note=True
  data={"generated":datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")+" 기준","days":days,"buckets":BUCKETS,
        "channels":channels,"taxitot":taxitot,"totals":totals,"adeff":adeff,"onb":onb,"rows":rows,"camp":camp,"btot":btot,"onbNote":onb_note}
  return data

def build_camp(channels, rows):
  """광고 채널별 캠페인/애드그룹 가중치 (확장 토글용). 택시 버킷 호출 기준 비중."""
  out={}
  by={}
  for r in rows:
    if r["bucket"]!="taxi": continue
    chdisp = "google.ads" if r["ch"]=="google" else ("insta" if r["ch"]=="insta" else (r["camp"]))
    if r["ch"] not in ("google","insta"): continue
    tot=sum(r["s"]["call"])
    by.setdefault(chdisp,[]).append((r["camp"],r["ag"],tot))
  for ch,lst in by.items():
    s=sum(x[2] for x in lst) or 1
    out[ch]=[[c,a,round(t/s,3)] for c,a,t in sorted(lst,key=lambda x:-x[2]) if t>0][:8]
  # adison/naver 단일
  out.setdefault("adison",[["ridingforever","",1.0]])
  out.setdefault("naver_bsa",[["메인","",1.0]])
  return out

# ========================= 더미 빌드(토큰 불필요) =========================
def mb(seed):
  s=[seed & 0xffffffff]
  def r():
    s[0]=(s[0]+0x6D2B79F5)&0xffffffff; t=s[0]
    t=( (t ^ (t>>15)) * (1|t) )&0xffffffff
    t=( t + ((t ^ (t>>7)) * (61|t)) )&0xffffffff
    return (((t ^ (t>>14))&0xffffffff))/4294967296
  return r
def dser(seed,base,spread,n):
  r=mb(seed); return [max(0,round(base*(0.55+r()*spread))) for _ in range(n)]
def build_dummy(frm=None,to=None):
  base=datetime.date(2026,9,28); days=[(base-datetime.timedelta(days=i)).isoformat() for i in range(13,-1,-1)]; n=len(days)
  CH=[("오가닉(Organic)",False,30,16,24),("google.ads",True,48,22,10),("adison",True,13,6,2),("naver_bsa",True,5,2,1),
      ("insta",True,1,1,4),("tmap",False,9,5,6),("braze(CRM)",False,22,14,20),("kakao",False,6,3,3)]
  channels=[{"name":c[0],"ad":c[1],"su":dser(i*4+1,c[2],1.0,n),"card":dser(i*4+2,c[3],1.0,n),"lounge":dser(i*4+3,c[4],1.0,n)} for i,c in enumerate(CH)]
  taxitot=dser(99,52000,0.5,n)
  camp={"google.ads":[["지방권_강원_tROAS","",0.22],["티머니GO_신규가입_상시","",0.25],["자전거킥보드_ACi_구매완료","",0.20],["부산택시_tROAS","",0.15],["예약택시_ACi_예약완료","",0.10],["지방권_강원_ACe","",0.08]],
        "insta":[["지방택시 활성화_트래픽","강원+1854MF",0.38],["지방택시 활성화_트래픽","대전+1854MF",0.20],["지방택시 활성화_트래픽","충주+1854MF",0.12],["지방택시 활성화_트래픽","제주+1854MF",0.10],["예약택시_트래픽","2049MF + 여행",0.12],["자전거킥보드_트래픽","1829MF",0.08]],
        "adison":[["ridingforever","",1.0]],"naver_bsa":[["메인","",0.7],["썸네일1","",0.3]]}
  onb=[["교통카드 등록",dser(31,70,0.8,n)],["택시호출",dser(32,55,0.8,n)],["대중교통라운지 방문",dser(33,28,1.0,n)],["고속/시외버스 결제",dser(34,11,1.1,n)]]
  adeff=[{"name":"google.ads","cost":dser(41,1400000,0.6,n),"su":dser(42,48,0.9,n),"call":dser(43,60,0.8,n)},
         {"name":"insta","cost":dser(44,1100000,0.6,n),"su":dser(45,1,1.5,n),"call":dser(46,50,0.7,n)},
         {"name":"adison","cost":dser(47,300000,0.8,n),"su":dser(48,13,1.1,n),"call":dser(49,5,1.3,n)},
         {"name":"naver_bsa","cost":dser(50,200000,0.9,n),"su":dser(51,5,1.2,n),"call":dser(52,8,1.2,n)}]
  DEF=[("taxi","google","지방권_강원_tROAS","","신규",11),("taxi","google","지방권_강원_ACe","","재참여",16),
       ("taxi","google","지방권_대전_ACe","","재참여",9),("taxi","google","부산택시_tROAS","","신규",10),
       ("taxi","google","티머니GO_신규가입_상시","","신규",8),("taxi","google","자전거킥보드_ACi_구매완료","","신규",13),
       ("taxi","insta","지방택시 활성화_트래픽","강원+1854MF","신규",26),("taxi","insta","지방택시 활성화_트래픽","대전+1854MF","신규",13),
       ("taxi","insta","지방택시 활성화_트래픽","충주+1854MF","신규",6),("taxi","insta","지방택시 활성화_트래픽","제주+1854MF","신규",5),
       ("taxi","etc","adison 오퍼월","","-",9),("taxi","etc","naver 성과형DA","","-",6),("taxi","etc","카카오모먼트","","-",4),
       ("taxi","org","오가닉(Organic)","","-",430),("taxi","org","tmap","","-",180),("taxi","org","braze(CRM)","","-",95),("taxi","org","tmoneygo","","-",60),
       ("rvtaxi","google","예약택시_ACi_예약완료","예약택시","신규",7),("rvtaxi","google","대중교통_백만드로우_신규가입","숏드라마관","신규",3),
       ("rvtaxi","google","부산택시_신규가입","부산_기본료무료","신규",2),("rvtaxi","insta","예약택시_트래픽","2049MF + 여행","신규",2),("rvtaxi","etc","adison 오퍼월","","-",1),
       ("rvtaxi","org","오가닉(Organic)","","-",38),("rvtaxi","org","tmap","","-",14),("rvtaxi","org","braze(CRM)","","-",9),
       ("pm","google","자전거킥보드_ACi_구매완료","0원라이딩","신규",18),("pm","insta","자전거킥보드_트래픽","1829MF","신규",4),("pm","etc","adison 라이딩오퍼월","","-",7),
       ("pm","org","오가닉(Organic)","","-",40),("pm","org","braze(CRM)","","-",12),
       ("lounge","google","대중교통_백만드로우_신규가입","라운지_신규","신규",9),("lounge","insta","대중교통라운지_트래픽","1849MF","신규",5),("lounge","etc","seoul_9988(제휴)","","-",6),
       ("lounge","org","오가닉(Organic)","","-",22),("lounge","org","tmoneymobility(자체)","","-",14),("lounge","org","braze(CRM)","","-",9),("lounge","org","tmap","","-",5)]
  rows=[]
  for i,d in enumerate(DEF):
    r=mb(i*131+13); call=[];comp=[];suu=[];cost=[]
    for _ in range(n):
      w=0.55+r()*0.95; c=max(0,round(d[5]*w)); call.append(c); comp.append(round(c*(0.45+r()*0.3)))
      suu.append(round(r()*2) if d[1]=="insta" else round(c*(0.3+r()*0.6)))
      cost.append(0 if d[1] in("etc","org") else (round(60000+r()*320000) if d[1]=="insta" else round(30000+r()*300000)))
    rows.append({"bucket":d[0],"ch":d[1],"camp":d[2],"ag":d[3],"seg":d[4],"s":{"call":call,"comp":comp,"su":suu,"cost":cost}})
  for r in rows:
    if r["ch"] in ("google","insta","etc") and not is_purpose(r["bucket"], r["camp"]): r["ch"]="xads"
  totals={k:[sum(c[k][i] for c in channels) for i in range(n)] for k in ("su","card","lounge")}
  btot={"taxi":{"call":taxitot},
        "rvtaxi":{"call":dser(60,380,0.4,n),"comp":dser(61,210,0.4,n)},
        "pm":{"call":dser(62,70,0.5,n),"comp":dser(63,45,0.5,n)},
        "lounge":{"call":dser(64,70000,0.3,n)}}
  return {"generated":"더미 샘플","days":days,"buckets":BUCKETS,"channels":channels,"taxitot":taxitot,"totals":totals,
          "adeff":adeff,"onb":onb,"rows":rows,"camp":camp,"btot":btot,"onbNote":False}

# ========================= 출력 =========================
def write(data):
  html=TEMPLATE_HTML.replace("__DATA__", json.dumps(data,ensure_ascii=False))
  open(OUTFILE,"w",encoding="utf-8").write(html)
  print(f"[생성] {OUTFILE}  (days={len(data['days'])}, rows={len(data['rows'])}, channels={len(data['channels'])})")
  if data.get("onbNote"): print("  ⚠️ 온보딩 전환율: Actuals로 코호트 직접 산출 불가 → 0으로 둠(리텐션 리포트 필요, 추후 보완)")

def main():
  ap=argparse.ArgumentParser()
  ap.add_argument("--dummy",action="store_true")
  ap.add_argument("--from",dest="frm"); ap.add_argument("--to",dest="to")
  ap.add_argument("--insecure",action="store_true",help="사내망 SSL 검증 생략(테스트용)")
  a=ap.parse_args()
  if a.dummy: data=build_dummy()
  else:
    to=a.to or (datetime.datetime.now(TZ).date()-datetime.timedelta(days=1)).isoformat()
    frm=a.frm or (datetime.date.fromisoformat(to)-datetime.timedelta(days=6)).isoformat()
    if frm>to: sys.exit("from>to")
    data=build_live(frm,to,a.insecure)
  write(data)

















import base64 as _b64
TEMPLATE_HTML=_b64.b64decode("PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9ImtvIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLCB2aWV3cG9ydC1maXQ9Y292ZXIiPgo8dGl0bGU+7Yuw66i464uIR08g6rSR6rOgIOyEseqzvCDthrXtlanrt7A8L3RpdGxlPgo8c3R5bGU+CiAgOnJvb3R7LS1iZzojZjZmNWZiOy0tY2FyZDojZmZmOy0tbGluZTojZWFlN2YzOy0taW5rOiMyYTI2NDA7LS1zdWI6IzhiODZhMDstLWFjY2VudDojODM3MGU2Oy0tYWNjZW50LXNvZnQ6I2YxZWRmYzstLWdvb2Q6IzJlOWU1YjstLWJhZDojYzA0NTNiOy0tbXV0ZWQ6I2JkYjhjYzstLW9yZzojZDZkMmUyO2JveC1zaXppbmc6Ym9yZGVyLWJveDtwYWRkaW5nLXRvcDplbnYoc2FmZS1hcmVhLWluc2V0LXRvcCwwcHgpO3BhZGRpbmctYm90dG9tOmVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tLDBweCl9CiAgQG1lZGlhIChwcmVmZXJzLWNvbG9yLXNjaGVtZTpkYXJrKXs6cm9vdDpub3QoW2RhdGEtdGhlbWU9ImxpZ2h0Il0pey0tYmc6IzE1MTIxZDstLWNhcmQ6IzFlMWEyYTstLWxpbmU6IzJmMjk0MTstLWluazojZWNlOWY1Oy0tc3ViOiM5NDhlYWE7LS1hY2NlbnQ6I2E1OTNmMDstLWFjY2VudC1zb2Z0OiMyNzFkM2Q7LS1tdXRlZDojNWY1ODc1Oy0tb3JnOiM0MTNhNTR9fQogICp7Ym94LXNpemluZzpib3JkZXItYm94O21hcmdpbjowO3BhZGRpbmc6MH0KICBib2R5e2JhY2tncm91bmQ6dmFyKC0tYmcpO2NvbG9yOnZhcigtLWluayk7Zm9udC1mYW1pbHk6IkFwcGxlIFNEIEdvdGhpYyBOZW8iLCJNYWxndW4gR290aGljIiwiUHJldGVuZGFyZCIsc3lzdGVtLXVpLHNhbnMtc2VyaWY7Zm9udC1zaXplOjE0cHg7bGluZS1oZWlnaHQ6MS41O3BhZGRpbmc6MTZweH0KICAud3JhcHttYXgtd2lkdGg6MTEyMHB4O21hcmdpbjowIGF1dG99CiAgaDF7Zm9udC1zaXplOjE5cHg7Zm9udC13ZWlnaHQ6ODAwO2xldHRlci1zcGFjaW5nOi0wLjNweH0KICAuY29udHJvbHN7ZGlzcGxheTpmbGV4O2ZsZXgtd3JhcDp3cmFwO2dhcDoxMHB4O2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW46MTRweCAwfQogIC5zZWd7ZGlzcGxheTpmbGV4O2dhcDozcHg7YmFja2dyb3VuZDp2YXIoLS1jYXJkKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6OXB4O3BhZGRpbmc6NHB4fQogIC5zZWcgYnV0dG9ue2JvcmRlcjpub25lO2JhY2tncm91bmQ6bm9uZTtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxM3B4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS1zdWIpO3BhZGRpbmc6N3B4IDEzcHg7Ym9yZGVyLXJhZGl1czo2cHg7Y3Vyc29yOnBvaW50ZXJ9CiAgLnNlZyBidXR0b24ub257YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQpO2NvbG9yOiNmZmZ9CiAgLnJhbmdlbGFiZWx7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tc3ViKX0ucmFuZ2VsYWJlbCBie2NvbG9yOnZhcigtLWluayl9CiAgLnRhYnN7ZGlzcGxheTpmbGV4O2dhcDo0cHg7YmFja2dyb3VuZDp2YXIoLS1jYXJkKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTBweDtwYWRkaW5nOjVweDttYXJnaW4tYm90dG9tOjE0cHg7b3ZlcmZsb3cteDphdXRvfQogIC50YWJ7ZmxleDoxO21pbi13aWR0aDo5MnB4O3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6OXB4IDhweDtib3JkZXItcmFkaXVzOjdweDtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjEzcHg7Y29sb3I6dmFyKC0tc3ViKTtjdXJzb3I6cG9pbnRlcjt3aGl0ZS1zcGFjZTpub3dyYXA7Ym9yZGVyOm5vbmU7YmFja2dyb3VuZDpub25lO2ZvbnQtZmFtaWx5OmluaGVyaXR9CiAgLnRhYi5vbntiYWNrZ3JvdW5kOnZhcigtLWFjY2VudCk7Y29sb3I6I2ZmZn0KICAubm90ZWxpbmV7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tc3ViKTttYXJnaW46MnB4IDAgMTBweH0KICAua3Bpc3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdChhdXRvLWZpdCxtaW5tYXgoMTUwcHgsMWZyKSk7Z2FwOjEwcHg7bWFyZ2luLWJvdHRvbToxNHB4fQogIC5rcGl7YmFja2dyb3VuZDp2YXIoLS1jYXJkKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTBweDtwYWRkaW5nOjEzcHggMTRweH0KICAua3BpLnByaW1hcnl7Ym9yZGVyLWNvbG9yOnZhcigtLWFjY2VudCk7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQtc29mdCl9CiAgLmtwaSAudHtmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS1zdWIpO2ZvbnQtd2VpZ2h0OjYwMH0KICAua3BpIC5iaWd7Zm9udC1zaXplOjIycHg7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi10b3A6NXB4O2xldHRlci1zcGFjaW5nOi0wLjVweH0KICAua3BpIC5iaWcgLnV7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tc3ViKTtmb250LXdlaWdodDo2MDA7bWFyZ2luLWxlZnQ6MnB4fQogIC5rcGkgLmRlbHRhe2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMDttYXJnaW4tdG9wOjRweH0KICAudXB7Y29sb3I6dmFyKC0tZ29vZCl9LmRvd257Y29sb3I6dmFyKC0tYmFkKX0uZmxhdHtjb2xvcjp2YXIoLS1tdXRlZCl9CiAgLmNhcmR7YmFja2dyb3VuZDp2YXIoLS1jYXJkKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTBweDtwYWRkaW5nOjE1cHg7bWFyZ2luLWJvdHRvbToxMnB4fQogIC5jYXJkIGgze2ZvbnQtc2l6ZToxNHB4O2ZvbnQtd2VpZ2h0OjgwMDttYXJnaW4tYm90dG9tOjJweH0KICAuY2FyZCBoMyAubWluaXtjb2xvcjp2YXIoLS1zdWIpO2ZvbnQtd2VpZ2h0OjYwMDtmb250LXNpemU6MTJweH0KICAuY2FyZCAuY3N1Yntmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS1zdWIpO21hcmdpbi1ib3R0b206MTJweH0KICAuc3BsaXRiYXJ7ZGlzcGxheTpmbGV4O2hlaWdodDoyNnB4O2JvcmRlci1yYWRpdXM6N3B4O292ZXJmbG93OmhpZGRlbjttYXJnaW4tYm90dG9tOjEwcHg7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKX0KICAuc3BsaXRiYXIgLmFke2JhY2tncm91bmQ6dmFyKC0tYWNjZW50KX0uc3BsaXRiYXIgLm9yZ3tiYWNrZ3JvdW5kOnZhcigtLW9yZyl9CiAgLnNwbGl0YmFyIHNwYW57ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2NvbG9yOiNmZmY7Zm9udC1zaXplOjExcHg7Zm9udC13ZWlnaHQ6ODAwO21pbi13aWR0aDowfQogIC5zcGxpdGJhciAub3Jne2NvbG9yOnZhcigtLWluayl9CiAgLmNocm93e2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MTUwcHggMWZyIDkycHg7Z2FwOjEwcHg7YWxpZ24taXRlbXM6Y2VudGVyO3BhZGRpbmc6NXB4IDA7Zm9udC1zaXplOjEyLjVweH0KICAuY2hyb3cuZXhwcm93e2N1cnNvcjpwb2ludGVyfQogIC5jaHJvdyAubm17ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NXB4fQogIC5jaHJvdyAuYWR0YWd7Zm9udC1zaXplOjkuNXB4O2ZvbnQtd2VpZ2h0OjgwMDtwYWRkaW5nOjFweCA1cHg7Ym9yZGVyLXJhZGl1czo0cHg7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQtc29mdCk7Y29sb3I6dmFyKC0tYWNjZW50KX0KICAuYXJyb3d7ZGlzcGxheTppbmxpbmUtYmxvY2s7d2lkdGg6MTFweDtmb250LXNpemU6OXB4O2NvbG9yOnZhcigtLXN1Yik7dHJhbnNpdGlvbjp0cmFuc2Zvcm0gLjE1c30KICAudHJhY2t7aGVpZ2h0OjhweDtiYWNrZ3JvdW5kOnZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6NXB4O292ZXJmbG93OmhpZGRlbn0KICAudHJhY2s+c3BhbntkaXNwbGF5OmJsb2NrO2hlaWdodDoxMDAlO2JvcmRlci1yYWRpdXM6NXB4O2JhY2tncm91bmQ6dmFyKC0tb3JnKX0KICAudHJhY2s+c3Bhbi5hZGZ7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQpfQogIC5jaHJvdyAudmFse3RleHQtYWxpZ246cmlnaHQ7Y29sb3I6dmFyKC0tc3ViKX0uY2hyb3cgLnZhbCBie2NvbG9yOnZhcigtLWluayl9CiAgLmNoc3Vie3BhZGRpbmc6MnB4IDAgOHB4IDIwcHg7bWFyZ2luOjAgMCA0cHggOHB4O2JvcmRlci1sZWZ0OjJweCBzb2xpZCB2YXIoLS1hY2NlbnQtc29mdCl9CiAgLnN1YnJvd3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciBhdXRvO2dhcDoxMHB4O2ZvbnQtc2l6ZToxMS41cHg7Y29sb3I6dmFyKC0tc3ViKTtwYWRkaW5nOjNweCAwfQogIC5zdWJyb3cgLm5tMntjb2xvcjp2YXIoLS1pbmspfQogIC5lZmZzdWJ7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDoxMHB4O2ZvbnQtc2l6ZToxMS41cHg7Y29sb3I6dmFyKC0tc3ViKTtwYWRkaW5nOjRweCAwO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHZhcigtLWFjY2VudC1zb2Z0KX0KICAuZWZmc3ViIC5ubTJ7Y29sb3I6dmFyKC0taW5rKTtmb250LXdlaWdodDo2MDB9CiAgLmNvbnZncmlke2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KGF1dG8tZml0LG1pbm1heCgxMzBweCwxZnIpKTtnYXA6MTBweH0KICAuY29udntib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6OXB4O3BhZGRpbmc6MTFweCAxMnB4fQogIC5jb252IC50e2ZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLXN1Yik7Zm9udC13ZWlnaHQ6NjAwfQogIC5jb252IC5iaWd7Zm9udC1zaXplOjIwcHg7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi10b3A6NHB4fQogIC5jb252IC5iaWcgLnV7Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tc3ViKTttYXJnaW4tbGVmdDoxcHh9CiAgLmNvbnYgLnN1YjJ7Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tc3ViKTttYXJnaW4tdG9wOjFweH0KICAuc2Nyb2xse292ZXJmbG93LXg6YXV0b30KICB0YWJsZXt3aWR0aDoxMDAlO2JvcmRlci1jb2xsYXBzZTpjb2xsYXBzZTtmb250LXNpemU6MTJweDttaW4td2lkdGg6NjgwcHh9CiAgdGgsdGR7cGFkZGluZzo3cHggOHB4O3RleHQtYWxpZ246cmlnaHQ7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgdmFyKC0tbGluZSk7d2hpdGUtc3BhY2U6bm93cmFwfQogIHRoOmZpcnN0LWNoaWxkLHRkOmZpcnN0LWNoaWxke3RleHQtYWxpZ246bGVmdH0KICB0aGVhZCB0aHtjb2xvcjp2YXIoLS1zdWIpO2ZvbnQtd2VpZ2h0OjcwMH0KICB0aC5saCx0ZC5saHt0ZXh0LWFsaWduOmxlZnR9CiAgdGQubmFtZXttYXgtd2lkdGg6MjYwcHg7d2hpdGUtc3BhY2U6bm9ybWFsO3dvcmQtYnJlYWs6YnJlYWstd29yZDtsaW5lLWhlaWdodDoxLjM1O3RleHQtYWxpZ246bGVmdH0KICB0ci5leHByb3d7Y3Vyc29yOnBvaW50ZXJ9CiAgLmtwaWNvbHtmb250LXdlaWdodDo4MDA7YmFja2dyb3VuZDpyZ2JhKDEzMSwxMTIsMjMwLDAuMDYpfXRoLmtwaWNvbHtjb2xvcjp2YXIoLS1zdWIpO2JhY2tncm91bmQ6cmdiYSgxMzEsMTEyLDIzMCwwLjA2KX0KICB0ZC5kaW17Y29sb3I6dmFyKC0tbXV0ZWQpfQogIC5kZWx0YS1ze2ZvbnQtc2l6ZToxMC41cHg7Zm9udC13ZWlnaHQ6NzAwO21hcmdpbi1sZWZ0OjVweH0KICAuYmFkZ2V7ZGlzcGxheTppbmxpbmUtYmxvY2s7Zm9udC1zaXplOjEwLjVweDtmb250LXdlaWdodDo3MDA7cGFkZGluZzoxcHggN3B4O2JvcmRlci1yYWRpdXM6NXB4fQogIC5iYWRnZS5yZXtiYWNrZ3JvdW5kOiNlZmVhZmM7Y29sb3I6IzdhNjNkNn0uYmFkZ2UudHJ7YmFja2dyb3VuZDojZWVmMGYzO2NvbG9yOiM3YTg1OTN9CiAgQG1lZGlhIChwcmVmZXJzLWNvbG9yLXNjaGVtZTpkYXJrKXs6cm9vdDpub3QoW2RhdGEtdGhlbWU9ImxpZ2h0Il0pIC5iYWRnZS5yZXtiYWNrZ3JvdW5kOiMyNzFkM2Q7Y29sb3I6I2E5OTFmNX06cm9vdDpub3QoW2RhdGEtdGhlbWU9ImxpZ2h0Il0pIC5iYWRnZS50cntiYWNrZ3JvdW5kOiMyNDJiMzR9fQogIC5kZXRhaWx0ZHtiYWNrZ3JvdW5kOnZhcigtLWJnKTtwYWRkaW5nOjB9LmRldGFpbHRkIC5pbm5lcntwYWRkaW5nOjRweCAxMHB4IDEwcHggMjZweH0KICAudG90cm93IHRke2JvcmRlci10b3A6MnB4IHNvbGlkIHZhcigtLWxpbmUpO2ZvbnQtd2VpZ2h0OjgwMH0KICAuc2VjaGQtcm93IHRke2JhY2tncm91bmQ6dmFyKC0tYWNjZW50LXNvZnQpO2NvbG9yOnZhcigtLWFjY2VudCk7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxMi41cHg7dGV4dC1hbGlnbjpsZWZ0IWltcG9ydGFudDtwYWRkaW5nOjlweCA4cHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgdmFyKC0tbGluZSl9CiAgLnNlZ2hke2ZvbnQtc2l6ZToxMy41cHg7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbjoxNnB4IDAgOHB4fS5zZWdoZCAuc2Vnc3Vie2ZvbnQtc2l6ZToxMS41cHg7Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOnZhcigtLXN1Yik7bWFyZ2luLWxlZnQ6N3B4fQogIC5mb290e2ZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLW11dGVkKTttYXJnaW4tdG9wOjE2cHg7bGluZS1oZWlnaHQ6MS42fQo8L3N0eWxlPgo8L2hlYWQ+Cjxib2R5Pgo8ZGl2IGNsYXNzPSJ3cmFwIj4KICA8aDE+7Yuw66i464uIR08g6rSR6rOgIOyEseqzvCDthrXtlanrt7A8L2gxPgogIDxkaXYgY2xhc3M9ImNvbnRyb2xzIj48ZGl2IGNsYXNzPSJzZWciIGlkPSJwZXJpb2RzZWciPjwvZGl2PjxkaXYgY2xhc3M9InJhbmdlbGFiZWwiIGlkPSJyYW5nZWxhYmVsIj48L2Rpdj48L2Rpdj4KICA8ZGl2IGNsYXNzPSJ0YWJzIiBpZD0idGFicyI+PC9kaXY+CiAgPGRpdiBpZD0idmlldyI+PC9kaXY+CiAgPGRpdiBjbGFzcz0iZm9vdCIgaWQ9ImZvb3QiPjwvZGl2Pgo8L2Rpdj4KPHNjcmlwdD4KLyogPT09PT0g642w7J207YSwIOyjvOyehSAoZ2VuZXJhdGUucHnqsIAgX19EQVRBX18g66W8IOyLpOy4oSBKU09O7Jy866GcIOq1kOyytCkgPT09PT0gKi8KY29uc3QgREFUQSA9IF9fREFUQV9fOwpjb25zdCBCVUNLRVRTID0gREFUQS5idWNrZXRzOwpjb25zdCBEQVlTID0gREFUQS5kYXlzOwpjb25zdCBDSCA9IERBVEEuY2hhbm5lbHM7CmNvbnN0IFRPVEFMUyA9IERBVEEudG90YWxzOwpjb25zdCBUQVhJVE9UID0gREFUQS50YXhpdG90Owpjb25zdCBDQU1QID0gREFUQS5jYW1wOwpjb25zdCBPTkIgPSBEQVRBLm9uYjsKY29uc3QgQURFRkYgPSBEQVRBLmFkZWZmOwpjb25zdCBST1dTID0gREFUQS5yb3dzOwpjb25zdCBCVE9UID0gREFUQS5idG90fHx7fTsKCi8qID09PT09IOqzoOyglSDroZzsp4EgPT09PT0gKi8KY29uc3QgZj1uPT4obj09bnVsbCk/IuKAlCI6TWF0aC5yb3VuZChuKS50b0xvY2FsZVN0cmluZygna28tS1InKTsKbGV0IFBFUklPRD03Owpjb25zdCBQRVJJT0RTPVtbIjEiLCLstZzqt7wgMeydvCIsMV0sWyI3Iiwi7LWc6re8IDfsnbwiLDddLFsiMTQiLCLsoITssrQoMTTsnbwpIiwxNF1dOwpmdW5jdGlvbiB3aW5kb3dzKG4pe2NvbnN0IGVuZD1EQVlTLmxlbmd0aCxjdXJTPU1hdGgubWF4KDAsZW5kLW4pLGN1cj1bXTtmb3IobGV0IGk9Y3VyUztpPGVuZDtpKyspY3VyLnB1c2goaSk7Y29uc3Qgc2hpZnQ9KG48PTcpPzc6bjtjb25zdCBwcmlvcj1jdXIubWFwKGk9Pmktc2hpZnQpO3JldHVybiB7Y3VyLHByaW9yOnByaW9yLmV2ZXJ5KGk9Pmk+PTApP3ByaW9yOltdfTt9CmNvbnN0IHN1bT0oYXJyLGlkeCk9PmlkeC5yZWR1Y2UoKGEsaSk9PmErKGFycltpXXx8MCksMCk7CmNvbnN0IHBjdD0oYyxwKT0+KHA9PW51bGx8fHA9PT0wKT9udWxsOihjLXApL3AqMTAwOwpmdW5jdGlvbiBkZWx0YVNwYW4oYyxwLGdvb2RVcD10cnVlLHNtYWxsPWZhbHNlKXtjb25zdCB2PXBjdChjLHApO2lmKHY9PW51bGwpcmV0dXJuICcnO2NvbnN0IGNscz0odj09PTApPydmbGF0JzooKHY+MD09PWdvb2RVcCk/J3VwJzonZG93bicpLGFyPXY+MD8n4payJzoodjwwPyfilrwnOifCtycpO3JldHVybiBgPHNwYW4gY2xhc3M9IiR7Y2xzfSAke3NtYWxsPydkZWx0YS1zJzonZGVsdGEnfSI+JHthcn0ke01hdGguYWJzKHYpLnRvRml4ZWQoMCl9JTwvc3Bhbj5gO30KZnVuY3Rpb24ga3BpKGxhYmVsLGN1cixwcmlvcixvPXt9KXtyZXR1cm4gYDxkaXYgY2xhc3M9ImtwaSAke28ucHJpbWFyeT8ncHJpbWFyeSc6Jyd9Ij48ZGl2IGNsYXNzPSJ0Ij4ke2xhYmVsfTwvZGl2PjxkaXYgY2xhc3M9ImJpZyI+JHtvLndvbj8n4oKpJzonJ30ke2YoY3VyKX0ke28udW5pdD9gPHNwYW4gY2xhc3M9InUiPiR7by51bml0fTwvc3Bhbj5gOicnfTwvZGl2PiR7by5ub2RlbHRhPycnOmRlbHRhU3BhbihjdXIscHJpb3Isby5nb29kVXAhPT1mYWxzZSl9PC9kaXY+YDt9CmZ1bmN0aW9uIGNoVG90YWwoa2V5LGlkeCl7cmV0dXJuIENILnJlZHVjZSgoYSxjKT0+YStzdW0oY1trZXldLGlkeCksMCk7fQoKLyogLS0tLSDsmpTslb0gLS0tLSAqLwpmdW5jdGlvbiBjaEJsb2NrKHRpdGxlLGtleSxXKXsKICBjb25zdCByb3dzPUNILm1hcChjPT4oe25hbWU6Yy5uYW1lLGFkOmMuYWQsY3VyOnN1bShjW2tleV0sVy5jdXIpfSkpLnNvcnQoKGEsYik9PmIuY3VyLWEuY3VyKTsKICBjb25zdCB0b3RhbD1yb3dzLnJlZHVjZSgoYSxyKT0+YStyLmN1ciwwKXx8MTsKICBjb25zdCBhZFBjdD1yb3dzLmZpbHRlcihyPT5yLmFkKS5yZWR1Y2UoKGEscik9PmErci5jdXIsMCkvdG90YWwqMTAwLCBvcmdQY3Q9MTAwLWFkUGN0OwogIGNvbnN0IGJhcj1gPGRpdiBjbGFzcz0ic3BsaXRiYXIiPjxzcGFuIGNsYXNzPSJhZCIgc3R5bGU9IndpZHRoOiR7YWRQY3R9JSI+JHthZFBjdD49MTI/J+q0keqzoCAnK2FkUGN0LnRvRml4ZWQoMCkrJyUnOicnfTwvc3Bhbj48c3BhbiBjbGFzcz0ib3JnIiBzdHlsZT0id2lkdGg6JHtvcmdQY3R9JSI+JHtvcmdQY3Q+PTEyPyfruYTqtJHqs6AgJytvcmdQY3QudG9GaXhlZCgwKSsnJSc6Jyd9PC9zcGFuPjwvZGl2PmA7CiAgY29uc3QgbGlzdD1yb3dzLm1hcChyPT57Y29uc3Qgdz1yLmN1ci90b3RhbCoxMDA7CiAgICBpZihyLmFkKXsKICAgICAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJjaHJvdyI+PGRpdiBjbGFzcz0ibm0iPiR7ci5uYW1lfTxzcGFuIGNsYXNzPSJhZHRhZyI+6rSR6rOgPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9InRyYWNrIj48c3BhbiBjbGFzcz0iYWRmIiBzdHlsZT0id2lkdGg6JHt3fSUiPjwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJ2YWwiPjxiPiR7ZihyLmN1cil9PC9iPiAvICR7dy50b0ZpeGVkKDApfSU8L2Rpdj48L2Rpdj5gO30KICAgIHJldHVybiBgPGRpdiBjbGFzcz0iY2hyb3ciPjxkaXYgY2xhc3M9Im5tIj4ke3IubmFtZX08L2Rpdj48ZGl2IGNsYXNzPSJ0cmFjayI+PHNwYW4gc3R5bGU9IndpZHRoOiR7d30lIj48L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0idmFsIj48Yj4ke2Yoci5jdXIpfTwvYj4gLyAke3cudG9GaXhlZCgwKX0lPC9kaXY+PC9kaXY+YDsKICB9KS5qb2luKCcnKTsKICByZXR1cm4gYDxkaXYgY2xhc3M9ImNhcmQiPjxoMz4ke3RpdGxlfSA8c3BhbiBjbGFzcz0ibWluaSI+7LSdICR7Zih0b3RhbCl9ICjssYTrhJAg7ZWp7IKwIC8g7KSR67O1IO2PrO2VqCk8L3NwYW4+PC9oMz4ke2Jhcn0ke2xpc3R9PC9kaXY+YDsKfQpmdW5jdGlvbiBvbmJCbG9jayhXKXsKICBjb25zdCBzdT1jaFRvdGFsKCdzdScsVy5jdXIpLCBzdVA9Vy5wcmlvci5sZW5ndGg/Y2hUb3RhbCgnc3UnLFcucHJpb3IpOm51bGw7CiAgY29uc3QgaXRlbXM9T05CLm1hcChvPT57Y29uc3QgYz1zdW0ob1sxXSxXLmN1cikscD1XLnByaW9yLmxlbmd0aD9zdW0ob1sxXSxXLnByaW9yKTpudWxsO2NvbnN0IHJhdGU9c3U/Yy9zdSoxMDA6MCxyYXRlUD0oc3VQJiZwIT1udWxsKT9wL3N1UCoxMDA6bnVsbDtyZXR1cm4gYDxkaXYgY2xhc3M9ImNvbnYiPjxkaXYgY2xhc3M9InQiPiR7b1swXX08L2Rpdj48ZGl2IGNsYXNzPSJiaWciPiR7cmF0ZS50b0ZpeGVkKDApfTxzcGFuIGNsYXNzPSJ1Ij4lPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9InN1YjIiPiR7ZihjKX3rqoUgJHtkZWx0YVNwYW4ocmF0ZSxyYXRlUCx0cnVlLHRydWUpfTwvZGl2PjwvZGl2PmA7fSkuam9pbignJyk7CiAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJjYXJkIj48aDM+7Jio67O065SpIOyghO2ZmOycqDwvaDM+PGRpdiBjbGFzcz0iY3N1YiI+7ZqM7JuQ6rCA7J6F7ZWcIOyCrOuejOydtCDtlbXsi6wg7ZaJ64+Z7Jy866GcIOydtOyWtOyngOuKlCDruYTsnKggKOq0keqzoCDtj6ztlagg7KCE7LK0IOyxhOuEkCDquLDspIApPC9kaXY+PGRpdiBjbGFzcz0iY29udmdyaWQiPiR7aXRlbXN9PC9kaXY+PC9kaXY+YDsKfQpmdW5jdGlvbiBlZmZCbG9jayhXKXsKICBjb25zdCBib2R5PUFERUZGLm1hcChhPT57CiAgICBjb25zdCBjb3N0PXN1bShhLmNvc3QsVy5jdXIpLHN1PXN1bShhLnN1LFcuY3VyKSxjYWxsPXN1bShhLmNhbGwsVy5jdXIpOwogICAgY29uc3QgY3BzPXN1P2Nvc3Qvc3U6bnVsbCxjcGM9Y2FsbD9jb3N0L2NhbGw6bnVsbDsKICAgIHJldHVybiBgPHRyPjx0ZCBjbGFzcz0ibmFtZSI+JHthLm5hbWV9PC90ZD48dGQgY2xhc3M9ImRpbSI+JHtmKHN1KX08L3RkPjx0ZD7igqkke2YoY3BzKX08L3RkPjx0ZCBjbGFzcz0ia3BpY29sIj4ke2YoY2FsbCl9PC90ZD48dGQ+4oKpJHtmKGNwYyl9PC90ZD48dGQgY2xhc3M9ImRpbSI+4oKpJHtmKGNvc3QpfTwvdGQ+PC90cj5gOwogIH0pLmpvaW4oJycpOwogIHJldHVybiBgPGRpdiBjbGFzcz0iY2FyZCI+PGgzPuyxhOuEkOuzhCDtmqjsnKg8L2gzPjxkaXYgY2xhc3M9ImNzdWIiPuq0keqzoCDssYTrhJDsnbQg6rCA7J6FIDHrqoUgLyDtg53si5ztmLjstpwgMeqxtOydhCDrp4zrk5zripQg642wIOyTtCDruYTsmqkgKOuCruydhOyImOuhnSDtmqjsnKjsoIEpPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzY3JvbGwiPjx0YWJsZSBzdHlsZT0ibWluLXdpZHRoOjYyMHB4Ij48dGhlYWQ+PHRyPjx0aD7ssYTrhJA8L3RoPjx0aD7qsIDsnoU8L3RoPjx0aD7qsIDsnoXri7kg67mE7JqpPC90aD48dGggY2xhc3M9ImtwaWNvbCI+7YOd7Iuc7Zi47LacPC90aD48dGg+7Zi47Lac64u5IOu5hOyaqTwvdGg+PHRoPuq0keqzoOu5hDwvdGg+PC90cj48L3RoZWFkPjx0Ym9keT4ke2JvZHl9PC90Ym9keT48L3RhYmxlPjwvZGl2PjwvZGl2PmA7Cn0KZnVuY3Rpb24gcmVuZGVyU3VtbWFyeSgpewogIGNvbnN0IFc9d2luZG93cyhQRVJJT0QpLHByPVcucHJpb3IubGVuZ3RoOwogIGNvbnN0IHN1PXN1bShUT1RBTFMuc3UsVy5jdXIpLHN1UD1wcj9zdW0oVE9UQUxTLnN1LFcucHJpb3IpOm51bGw7CiAgY29uc3QgY2FyZD1zdW0oVE9UQUxTLmNhcmQsVy5jdXIpLGNhcmRQPXByP3N1bShUT1RBTFMuY2FyZCxXLnByaW9yKTpudWxsOwogIGNvbnN0IGxvPXN1bShUT1RBTFMubG91bmdlLFcuY3VyKSxsb1A9cHI/c3VtKFRPVEFMUy5sb3VuZ2UsVy5wcmlvcik6bnVsbDsKICBjb25zdCBjYWxsPXN1bShUQVhJVE9ULFcuY3VyKSxjYWxsUD1wcj9zdW0oVEFYSVRPVCxXLnByaW9yKTpudWxsOwogIGNvbnN0IGNhcmRzPWA8ZGl2IGNsYXNzPSJrcGlzIj4KICAgICR7a3BpKCLtmozsm5DqsIDsnoUiLHN1LHN1UCx7cHJpbWFyeTp0cnVlLHVuaXQ6IuuqhSJ9KX0KICAgICR7a3BpKCLqtZDthrXsubTrk5wg65Ox66GdIixjYXJkLGNhcmRQLHt1bml0OiLqsbQifSl9CiAgICAke2twaSgi64yA7KSR6rWQ7Ya165287Jq07KeAIOuwqeusuCIsbG8sbG9QLHt1bml0OiLrqoUifSl9CiAgICAke2twaSgi7YOd7Iuc7Zi47LacIixjYWxsLGNhbGxQLHt1bml0OiLqsbQifSl9CiAgPC9kaXY+YDsKICByZXR1cm4gYDxkaXYgY2xhc3M9Im5vdGVsaW5lIj48Yj7qtJHqs6Ag7Y+s7ZWoIOyghOyytCDssYTrhJAg6riw7KSAPC9iPjwvZGl2PmArY2FyZHMrb25iQmxvY2soVykrZWZmQmxvY2soVykrY2hCbG9jaygi7ZqM7JuQ6rCA7J6FIiwic3UiLFcpK2NoQmxvY2soIuq1kO2Gtey5tOuTnCDrk7HroZ0iLCJjYXJkIixXKStjaEJsb2NrKCLrjIDspJHqtZDthrXrnbzsmrTsp4Ag67Cp66y4IiwibG91bmdlIixXKTsKfQoKLyogLS0tLSDrsoTtgrcg7YOtID0g6rSR6rOgIOq4sOyXrOunjCAtLS0tICovCmZ1bmN0aW9uIGFnZ1Jvdyhyb3csaWR4KXtjb25zdCBzPXJvdy5zO3JldHVybntjYWxsOnN1bShzLmNhbGwsaWR4KSxjb21wOnN1bShzLmNvbXAsaWR4KSxzdTpzdW0ocy5zdSxpZHgpLGNvc3Q6c3VtKHMuY29zdCxpZHgpfTt9CmZ1bmN0aW9uIGJ1Y2tldFJvd3MoaWQpe3JldHVybiBST1dTLmZpbHRlcihyPT5yLmJ1Y2tldD09PWlkKTt9CmZ1bmN0aW9uIGJ1Y2tldEFjdGl2ZShpZCxXKXtyZXR1cm4gYnVja2V0Um93cyhpZCkuc29tZShyPT5zdW0oci5zLmNhbGwsVy5jdXIpPjApO30KY29uc3QgS0lORF9MQUJFTD17Z29vZ2xlOiJnb29nbGUuYWRzIixpbnN0YToiaW5zdGEiLGV0Yzoi67mE7JqpIOuvuOyXsOuPmSDqtJHqs6AiLHhhZHM6IuuLpOuluCDrqqnsoIEg6rSR6rOgIixvcmc6Iuq0keqzoCDsmbgg7LGE64SQIn07Ci8qIO2VnCDrsoTtgrfsnZgg66qo65OgIOyEueyFmOydhCAn7ZWY64KY7J2YIO2RnCfroZwg4oCUIOyXtCDsnITsuZjqsIAg7KCE67aAIOunnuydjCjspITrp57stqQpICovCmZ1bmN0aW9uIGJ1Y2tldENvbHMoQil7IC8vIOqzte2GtSBjb2xncm91cCArIO2XpOuNlCAo67KE7YK3IOuCtCDrqqjrk6Ag7IS57IWYIOuPmeydvCkKICBsZXQgY29sdywgaGVhZDsKICBpZihCLmR1YWxDcGEpeyBjb2x3PVsiMjQlIiwiMTQlIiwiMTMlIiwiMTMlIiwiMTMlIiwiMTIlIiwiMTElIl07CiAgICBoZWFkPWA8dGg+7Lqg7Y6Y7J24PC90aD48dGggY2xhc3M9ImxoIj7slaDrk5zqt7jro7k8L3RoPjx0aCBjbGFzcz0ia3BpY29sIj4ke0IucHJpbWFyeUxhYmVsfTwvdGg+PHRoPiR7Qi5jb21wTGFiZWx9PC90aD48dGg+6rSR6rOg67mEPC90aD48dGg+JHtCLmNwYUxhYmVsfTwvdGg+PHRoPiR7Qi5jb21wQ3BhTGFiZWx9PC90aD5gOwogIH0gZWxzZSBpZihCLnNob3dDb21wKXsgY29sdz1bIjI4JSIsIjE2JSIsIjE1JSIsIjE1JSIsIjE0JSIsIjEyJSJdOwogICAgaGVhZD1gPHRoPuy6oO2OmOyduDwvdGg+PHRoIGNsYXNzPSJsaCI+7JWg65Oc6re466O5PC90aD48dGggY2xhc3M9ImtwaWNvbCI+JHtCLnByaW1hcnlMYWJlbH08L3RoPjx0aD4ke0IuY29tcExhYmVsfTwvdGg+PHRoPuq0keqzoOu5hDwvdGg+PHRoPiR7Qi5jcGFMYWJlbH08L3RoPmA7CiAgfSBlbHNlIHsgY29sdz1bIjI4JSIsIjE2JSIsIjE1JSIsIjE1JSIsIjE0JSIsIjEyJSJdOwogICAgaGVhZD1gPHRoPuy6oO2OmOyduDwvdGg+PHRoIGNsYXNzPSJsaCI+7JWg65Oc6re466O5PC90aD48dGg+7ZqM7JuQ6rCA7J6FPC90aD48dGggY2xhc3M9ImtwaWNvbCI+JHtCLnByaW1hcnlMYWJlbH08L3RoPjx0aD7qtJHqs6DruYQ8L3RoPjx0aD4ke0IuY3BhTGFiZWx9PC90aD5gOwogIH0KICBjb25zdCBjb2w9YDxjb2xncm91cD4ke2NvbHcubWFwKHc9PmA8Y29sIHN0eWxlPSJ3aWR0aDoke3d9Ij5gKS5qb2luKCcnKX08L2NvbGdyb3VwPmA7CiAgcmV0dXJuIHtuY29sOmNvbHcubGVuZ3RoLCBjb2wsIGhlYWQ6YDx0aGVhZD48dHI+JHtoZWFkfTwvdHI+PC90aGVhZD5gfTsKfQpmdW5jdGlvbiBzZWNSb3cobyxCLG5vQ29zdCl7CiAgbGV0IGNlbGxzPWA8dGQgY2xhc3M9Im5hbWUiPiR7by5jYW1wfTwvdGQ+PHRkIGNsYXNzPSJuYW1lIGRpbSI+JHtvLmFnfHwnJ308L3RkPmA7CiAgaWYoQi5zaG93Q29tcCkgY2VsbHMrPWA8dGQgY2xhc3M9ImtwaWNvbCI+JHtmKG8uY19jYWxsKX0ke2RlbHRhU3BhbihvLmNfY2FsbCxvLnBfY2FsbD09bnVsbD9udWxsOm8ucF9jYWxsLHRydWUsdHJ1ZSl9PC90ZD48dGQ+JHtmKG8uY19jb21wKX08L3RkPmA7CiAgZWxzZSBjZWxscys9YDx0ZCBjbGFzcz0iZGltIj4ke2Yoby5jX3N1KX08L3RkPjx0ZCBjbGFzcz0ia3BpY29sIj4ke2Yoby5jX2NhbGwpfSR7ZGVsdGFTcGFuKG8uY19jYWxsLG8ucF9jYWxsPT1udWxsP251bGw6by5wX2NhbGwsdHJ1ZSx0cnVlKX08L3RkPmA7CiAgY2VsbHMrPWA8dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6J+KCqScrZihvLmNfY29zdCl9PC90ZD5gOwogIGlmKEIuZHVhbENwYSl7Y29uc3QgYzE9by5jX2NhbGw/by5jX2Nvc3Qvby5jX2NhbGw6bnVsbCxjMj1vLmNfY29tcD9vLmNfY29zdC9vLmNfY29tcDpudWxsO2NlbGxzKz1gPHRkIGNsYXNzPSJkaW0iPiR7bm9Db3N0PycnOihjMSE9bnVsbD8n4oKpJytmKGMxKTon4oCUJyl9PC90ZD48dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6KGMyIT1udWxsPyfigqknK2YoYzIpOifigJQnKX08L3RkPmA7fQogIGVsc2V7Y29uc3QgZD1CLnNob3dDb21wP28uY19jb21wOm8uY19jYWxsLGNwYT1ub0Nvc3Q/bnVsbDooZD9vLmNfY29zdC9kOm51bGwpO2NlbGxzKz1gPHRkIGNsYXNzPSJkaW0iPiR7bm9Db3N0PycnOihjcGEhPW51bGw/J+KCqScrZihjcGEpOifigJQnKX08L3RkPmA7fQogIHJldHVybiBgPHRyPiR7Y2VsbHN9PC90cj5gOwp9CmZ1bmN0aW9uIHNlY1RvdFJvdyhhZ2csQixub0Nvc3QpewogIGNvbnN0IHQ9YWdnLnJlZHVjZSgoYSxvKT0+e2Euc3UrPW8uY19zdXx8MDthLmNhbGwrPW8uY19jYWxsfHwwO2EuY29tcCs9by5jX2NvbXB8fDA7YS5jb3N0Kz1vLmNfY29zdHx8MDtyZXR1cm4gYTt9LHtzdTowLGNhbGw6MCxjb21wOjAsY29zdDowfSk7CiAgbGV0IG1pZD1CLnNob3dDb21wP2A8dGQgY2xhc3M9ImtwaWNvbCI+JHtmKHQuY2FsbCl9PC90ZD48dGQ+JHtmKHQuY29tcCl9PC90ZD5gOmA8dGQgY2xhc3M9ImRpbSI+JHtmKHQuc3UpfTwvdGQ+PHRkIGNsYXNzPSJrcGljb2wiPiR7Zih0LmNhbGwpfTwvdGQ+YDsKICBsZXQgY3BhQzsKICBpZihCLmR1YWxDcGEpe2NvbnN0IGMxPXQuY2FsbD90LmNvc3QvdC5jYWxsOm51bGwsYzI9dC5jb21wP3QuY29zdC90LmNvbXA6bnVsbDtjcGFDPWA8dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6KGMxIT1udWxsPyfigqknK2YoYzEpOifigJQnKX08L3RkPjx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzooYzIhPW51bGw/J+KCqScrZihjMik6J+KAlCcpfTwvdGQ+YDt9CiAgZWxzZXtjb25zdCBkPUIuc2hvd0NvbXA/dC5jb21wOnQuY2FsbCxjcGE9bm9Db3N0P251bGw6KGQ/dC5jb3N0L2Q6bnVsbCk7Y3BhQz1gPHRkIGNsYXNzPSJkaW0iPiR7bm9Db3N0PycnOihjcGEhPW51bGw/J+KCqScrZihjcGEpOifigJQnKX08L3RkPmA7fQogIHJldHVybiBgPHRyIGNsYXNzPSJ0b3Ryb3ciPjx0ZD7tlanqs4Q8L3RkPjx0ZD48L3RkPiR7bWlkfTx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzon4oKpJytmKHQuY29zdCl9PC90ZD4ke2NwYUN9PC90cj5gOwp9CmZ1bmN0aW9uIHNlY0Jsb2NrKHJvd3Msa2luZCxCLG5jb2wpewogIGNvbnN0IG5vQ29zdD0oa2luZD09PSJldGMifHxraW5kPT09Im9yZyIpOwogIGNvbnN0IGFnZz1yb3dzLm1hcChyPT57Y29uc3Qgbz17Y2FtcDpyLmNhbXAsYWc6ci5hZ307Y29uc3QgY3U9YWdnUm93KHIsQ1VSVy5jdXIpLHByPUNVUlcucHJpb3IubGVuZ3RoP2FnZ1JvdyhyLENVUlcucHJpb3IpOm51bGw7WyJjYWxsIiwiY29tcCIsInN1IiwiY29zdCJdLmZvckVhY2goaz0+e29bImNfIitrXT1jdVtrXTtpZihwcilvWyJwXyIra109cHJba107fSk7cmV0dXJuIG87fSkuc29ydCgoYSxiKT0+Yi5jX2NhbGwtYS5jX2NhbGwpOwogIGNvbnN0IGhkPWA8dHIgY2xhc3M9InNlY2hkLXJvdyI+PHRkIGNvbHNwYW49IiR7bmNvbH0iPiR7S0lORF9MQUJFTFtraW5kXX08L3RkPjwvdHI+YDsKICByZXR1cm4gaGQrYWdnLm1hcChvPT5zZWNSb3cobyxCLG5vQ29zdCkpLmpvaW4oJycpK3NlY1RvdFJvdyhhZ2csQixub0Nvc3QpOwp9CmxldCBDVVJXPW51bGw7CmZ1bmN0aW9uIHJlbmRlckJ1Y2tldChpZCl7CiAgY29uc3QgQj1CVUNLRVRTLmZpbmQoeD0+eC5pZD09PWlkKSxyb3dzPWJ1Y2tldFJvd3MoaWQpO0NVUlc9d2luZG93cyhQRVJJT0QpOwogIGNvbnN0IGlzQWQ9cj0+ci5jaD09PSJnb29nbGUifHxyLmNoPT09Imluc3RhInx8ci5jaD09PSJldGMiOwogIGNvbnN0IGFnZz0oaWR4LGZpbHQpPT5yb3dzLnJlZHVjZSgobyxyKT0+e2lmKGZpbHQmJiFmaWx0KHIpKXJldHVybiBvO2NvbnN0IGE9YWdnUm93KHIsaWR4KTtvLmNhbGwrPWEuY2FsbDtvLmNvbXArPWEuY29tcDtvLmNvc3QrPWEuY29zdDtyZXR1cm4gbzt9LHtjYWxsOjAsY29tcDowLGNvc3Q6MH0pOwogIGNvbnN0IGM9YWdnKENVUlcuY3VyLGlzQWQpLHA9Q1VSVy5wcmlvci5sZW5ndGg/YWdnKENVUlcucHJpb3IsaXNBZCk6bnVsbDsgICAgICAgICAgLy8g6rSR6rOgIOq4sOyXrCjrqqnsoIEg7Lqg7Y6Y7J24KQogIGNvbnN0IGJ0PUJUT1RbaWRdfHx7fTsgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIC8vIOyghOyytCjsoIQg7LGE64SQLCDsp4HsoJEg7KeR6rOEKQogIGNvbnN0IHRvdENhbGw9c3VtKGJ0LmNhbGx8fFtdLENVUlcuY3VyKSwgdG90Q2FsbFA9Q1VSVy5wcmlvci5sZW5ndGg/c3VtKGJ0LmNhbGx8fFtdLENVUlcucHJpb3IpOm51bGw7CiAgY29uc3QgdG90Q29tcD1zdW0oYnQuY29tcHx8W10sQ1VSVy5jdXIpLCB0b3RDb21wUD1DVVJXLnByaW9yLmxlbmd0aD9zdW0oYnQuY29tcHx8W10sQ1VSVy5wcmlvcik6bnVsbDsKICBsZXQgY2FyZHM9YDxkaXYgY2xhc3M9ImtwaXMiPmA7CiAgY29uc3QgVT1CLnVuaXR8fCLqsbQiOwogIGNhcmRzKz1rcGkoIuyghOyytCAiK0IucHJpbWFyeUxhYmVsLHRvdENhbGwsdG90Q2FsbFAse3VuaXQ6VX0pOwogIGNhcmRzKz1rcGkoIuq0keqzoCAiK0IucHJpbWFyeUxhYmVsLGMuY2FsbCxwJiZwLmNhbGwse3ByaW1hcnk6dHJ1ZSx1bml0OlV9KTsKICBpZihCLnNob3dDb21wKXsgY2FyZHMrPWtwaSgi7KCE7LK0ICIrQi5jb21wTGFiZWwsdG90Q29tcCx0b3RDb21wUCx7dW5pdDpVfSk7IGNhcmRzKz1rcGkoIuq0keqzoCAiK0IuY29tcExhYmVsLGMuY29tcCxwJiZwLmNvbXAse3VuaXQ6VX0pOyB9CiAgY2FyZHMrPWtwaSgi6rSR6rOg67mEIixjLmNvc3QscCYmcC5jb3N0LHt3b246dHJ1ZX0pOwogIGlmKEIuZHVhbENwYSl7Y29uc3QgZDE9eD0+eC5jYWxsP3guY29zdC94LmNhbGw6bnVsbCxkMj14PT54LmNvbXA/eC5jb3N0L3guY29tcDpudWxsO2NhcmRzKz1rcGkoQi5jcGFMYWJlbCxkMShjKSxwJiZkMShwKSx7d29uOnRydWUsZ29vZFVwOmZhbHNlfSkra3BpKEIuY29tcENwYUxhYmVsLGQyKGMpLHAmJmQyKHApLHt3b246dHJ1ZSxnb29kVXA6ZmFsc2V9KTt9CiAgZWxzZXtjb25zdCBjcGE9eD0+e2NvbnN0IGQ9Qi5zaG93Q29tcD94LmNvbXA6eC5jYWxsO3JldHVybiBkP3guY29zdC9kOm51bGw7fTtjYXJkcys9a3BpKEIuY3BhTGFiZWwsY3BhKGMpLHAmJmNwYShwKSx7d29uOnRydWUsZ29vZFVwOmZhbHNlfSk7fQogIGNhcmRzKz1gPC9kaXY+YDsKICBsZXQgaHRtbD1gPGRpdiBjbGFzcz0ibm90ZWxpbmUiPjxiPuyghOyytDwvYj4gPSDsoIQg7LGE64SQIOyngeygkSDsp5Hqs4Qo7JeQ7Ja067iM66a/7KeAIOyghOyytOqwkikgwrcgPGI+6rSR6rOgPC9iPiA9IOuqqeyggSDsuqDtjpjsnbgg6riw7Jes67aEPC9kaXY+YCtjYXJkczsKICBpZihCLm5vdGUpIGh0bWwrPWA8ZGl2IGNsYXNzPSJjc3ViIiBzdHlsZT0ibWFyZ2luOi02cHggMCAxMnB4Ij7igLsgJHtCLm5vdGV9PC9kaXY+YDsKICBjb25zdCBDPWJ1Y2tldENvbHMoQik7CiAgY29uc3Qga2luZHM9Qi5zaG93T3JnP1siZ29vZ2xlIiwiaW5zdGEiLCJldGMiLCJ4YWRzIiwib3JnIl06WyJnb29nbGUiLCJpbnN0YSIsImV0YyIsInhhZHMiXTsKICBsZXQgYm9keT0iIjsKICBraW5kcy5mb3JFYWNoKGtpbmQ9Pntjb25zdCBycz1yb3dzLmZpbHRlcihyPT5yLmNoPT09a2luZCk7aWYocnMubGVuZ3RoKWJvZHkrPXNlY0Jsb2NrKHJzLGtpbmQsQixDLm5jb2wpO30pOwogIGh0bWwrPWA8ZGl2IGNsYXNzPSJjYXJkIj48ZGl2IGNsYXNzPSJzY3JvbGwiPjx0YWJsZSBzdHlsZT0ibWluLXdpZHRoOjY4MHB4O3RhYmxlLWxheW91dDpmaXhlZCI+JHtDLmNvbH0ke0MuaGVhZH08dGJvZHk+JHtib2R5fTwvdGJvZHk+PC90YWJsZT48L2Rpdj48L2Rpdj5gOwogIHJldHVybiBodG1sOwp9CgovKiAtLS0tIOyFuCAtLS0tICovCmxldCBUQUI9InN1bW1hcnkiOwpmdW5jdGlvbiByZW5kZXIoKXsKICBjb25zdCBXPXdpbmRvd3MoUEVSSU9EKSxjZD1XLmN1ci5tYXAoaT0+REFZU1tpXSk7CiAgY29uc3QgY21wPSFXLnByaW9yLmxlbmd0aD8i67mE6rWQ6riw6rCEIOyXhuydjCI6KFBFUklPRD09PTE/IuyghOyjvCDrj5nsmpTsnbwg64yA67mEIjooUEVSSU9EPT09Nz8i7KCE7KO8IOuMgOu5hCI6YOyngeyghCAke1BFUklPRH3snbwg64yA67mEYCkpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJyYW5nZWxhYmVsIikuaW5uZXJIVE1MPWA8Yj4ke2NkWzBdfSR7UEVSSU9EPjE/IiB+ICIrY2RbY2QubGVuZ3RoLTFdOiIifTwvYj4gLyAke2NtcH1gOwogIGNvbnN0IGFjdGl2ZT1CVUNLRVRTLmZpbHRlcihiPT5idWNrZXRBY3RpdmUoYi5pZCxXKSk7CiAgaWYoVEFCIT09InN1bW1hcnkiJiYhYWN0aXZlLnNvbWUoYj0+Yi5pZD09PVRBQikpVEFCPSJzdW1tYXJ5IjsKICBjb25zdCB0YWJzPVtbInN1bW1hcnkiLCLsmpTslb0iXV0uY29uY2F0KGFjdGl2ZS5tYXAoYj0+W2IuaWQsYi5sYWJlbF0pKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgidGFicyIpLmlubmVySFRNTD10YWJzLm1hcCh0PT5gPGJ1dHRvbiBjbGFzcz0idGFiICR7dFswXT09PVRBQj8nb24nOicnfSIgZGF0YS10PSIke3RbMF19Ij4ke3RbMV19PC9idXR0b24+YCkuam9pbignJyk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInZpZXciKS5pbm5lckhUTUw9KFRBQj09PSJzdW1tYXJ5Iik/cmVuZGVyU3VtbWFyeSgpOnJlbmRlckJ1Y2tldChUQUIpOwp9CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJwZXJpb2RzZWciKS5pbm5lckhUTUw9UEVSSU9EUy5tYXAocD0+YDxidXR0b24gY2xhc3M9IiR7KCtwWzJdPT09UEVSSU9EKT8nb24nOicnfSIgZGF0YS1wPSIke3BbMl19Ij4ke3BbMV19PC9idXR0b24+YCkuam9pbignJyk7CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJwZXJpb2RzZWciKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsZT0+e2NvbnN0IGI9ZS50YXJnZXQuY2xvc2VzdCgiYnV0dG9uIik7aWYoIWIpcmV0dXJuO1BFUklPRD0rYi5kYXRhc2V0LnA7ZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgiI3BlcmlvZHNlZyBidXR0b24iKS5mb3JFYWNoKHg9PnguY2xhc3NMaXN0LnRvZ2dsZSgib24iLCt4LmRhdGFzZXQucD09PVBFUklPRCkpO3JlbmRlcigpO30pOwpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgidGFicyIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixlPT57Y29uc3QgYj1lLnRhcmdldC5jbG9zZXN0KCIudGFiIik7aWYoIWIpcmV0dXJuO1RBQj1iLmRhdGFzZXQudDtyZW5kZXIoKTt9KTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoImZvb3QiKS5pbm5lckhUTUw9IuyalOyVvT3soITssrQg7Jyg7J6FKOq0keqzoCvruYTqtJHqs6ApLCDqtJHqs6A967O06528IC8g67KE7YK3IO2DrT3qtJHqs6Ag6riw7Jes66eMICjquLDtg4Ag6rSR6rOgPeu5hOyaqSDrr7jsl7Drj5ksIOyLpOyggeunjCk8YnI+67OA7ZmU7JyoPeyghOyjvCDrj5nsmpTsnbwoMeydvCkv7KCE7KO8KDfsnbwpIOuMgOu5hCAvIOyngO2RnOKGkT3stIjroZ0sIOu5hOyaqeKGkT3ruajqsJU8YnI+7YOt7J2AIOyEpOyglSvsnpDrj5kg7Iio6rmAKOyLpOyggSAw7J2066m0IOyGjOupuCkgLyDqtazquIA97Lqg7Y6Y7J24LCDsnbjsiqTtg4A97JWg65Oc6re466O5IjsKcmVuZGVyKCk7Cjwvc2NyaXB0Pgo8L2JvZHk+CjwvaHRtbD4K").decode("utf-8")


if __name__=="__main__":
    main()
