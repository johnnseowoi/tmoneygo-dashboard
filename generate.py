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
TEMPLATE_HTML=_b64.b64decode("PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9ImtvIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLCB2aWV3cG9ydC1maXQ9Y292ZXIiPgo8dGl0bGU+7Yuw66i464uIR08g6rSR6rOgIOyEseqzvCDCtyDthrXtlanrt7A8L3RpdGxlPgo8c3R5bGU+CiAgOnJvb3R7LS1iZzojZjZmNWZiOy0tY2FyZDojZmZmOy0tbGluZTojZWFlN2YzOy0taW5rOiMyYTI2NDA7LS1zdWI6IzhiODZhMDstLWFjY2VudDojODM3MGU2Oy0tYWNjZW50LXNvZnQ6I2YxZWRmYzstLWdvb2Q6IzJlOWU1YjstLWJhZDojYzA0NTNiOy0tbXV0ZWQ6I2JkYjhjYzstLW9yZzojZDZkMmUyO2JveC1zaXppbmc6Ym9yZGVyLWJveDtwYWRkaW5nLXRvcDplbnYoc2FmZS1hcmVhLWluc2V0LXRvcCwwcHgpO3BhZGRpbmctYm90dG9tOmVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tLDBweCl9CiAgQG1lZGlhIChwcmVmZXJzLWNvbG9yLXNjaGVtZTpkYXJrKXs6cm9vdDpub3QoW2RhdGEtdGhlbWU9ImxpZ2h0Il0pey0tYmc6IzE1MTIxZDstLWNhcmQ6IzFlMWEyYTstLWxpbmU6IzJmMjk0MTstLWluazojZWNlOWY1Oy0tc3ViOiM5NDhlYWE7LS1hY2NlbnQ6I2E1OTNmMDstLWFjY2VudC1zb2Z0OiMyNzFkM2Q7LS1tdXRlZDojNWY1ODc1Oy0tb3JnOiM0MTNhNTR9fQogICp7Ym94LXNpemluZzpib3JkZXItYm94O21hcmdpbjowO3BhZGRpbmc6MH0KICBib2R5e2JhY2tncm91bmQ6dmFyKC0tYmcpO2NvbG9yOnZhcigtLWluayk7Zm9udC1mYW1pbHk6IkFwcGxlIFNEIEdvdGhpYyBOZW8iLCJNYWxndW4gR290aGljIiwiUHJldGVuZGFyZCIsc3lzdGVtLXVpLHNhbnMtc2VyaWY7Zm9udC1zaXplOjE0cHg7bGluZS1oZWlnaHQ6MS41O3BhZGRpbmc6MTZweH0KICAud3JhcHttYXgtd2lkdGg6MTEyMHB4O21hcmdpbjowIGF1dG99CiAgaDF7Zm9udC1zaXplOjE5cHg7Zm9udC13ZWlnaHQ6ODAwO2xldHRlci1zcGFjaW5nOi0wLjNweH0KICAuY29udHJvbHN7ZGlzcGxheTpmbGV4O2ZsZXgtd3JhcDp3cmFwO2dhcDoxMHB4O2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW46MTRweCAwfQogIC5zZWd7ZGlzcGxheTpmbGV4O2dhcDozcHg7YmFja2dyb3VuZDp2YXIoLS1jYXJkKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6OXB4O3BhZGRpbmc6NHB4fQogIC5zZWcgYnV0dG9ue2JvcmRlcjpub25lO2JhY2tncm91bmQ6bm9uZTtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxM3B4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS1zdWIpO3BhZGRpbmc6N3B4IDEzcHg7Ym9yZGVyLXJhZGl1czo2cHg7Y3Vyc29yOnBvaW50ZXJ9CiAgLnNlZyBidXR0b24ub257YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQpO2NvbG9yOiNmZmZ9CiAgLnJhbmdlbGFiZWx7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tc3ViKX0ucmFuZ2VsYWJlbCBie2NvbG9yOnZhcigtLWluayl9CiAgLnRhYnN7ZGlzcGxheTpmbGV4O2dhcDo0cHg7YmFja2dyb3VuZDp2YXIoLS1jYXJkKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTBweDtwYWRkaW5nOjVweDttYXJnaW4tYm90dG9tOjE0cHg7b3ZlcmZsb3cteDphdXRvfQogIC50YWJ7ZmxleDoxO21pbi13aWR0aDo5MnB4O3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6OXB4IDhweDtib3JkZXItcmFkaXVzOjdweDtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjEzcHg7Y29sb3I6dmFyKC0tc3ViKTtjdXJzb3I6cG9pbnRlcjt3aGl0ZS1zcGFjZTpub3dyYXA7Ym9yZGVyOm5vbmU7YmFja2dyb3VuZDpub25lO2ZvbnQtZmFtaWx5OmluaGVyaXR9CiAgLnRhYi5vbntiYWNrZ3JvdW5kOnZhcigtLWFjY2VudCk7Y29sb3I6I2ZmZn0KICAubm90ZWxpbmV7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tc3ViKTttYXJnaW46MnB4IDAgMTBweH0KICAua3Bpc3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdChhdXRvLWZpdCxtaW5tYXgoMTUwcHgsMWZyKSk7Z2FwOjEwcHg7bWFyZ2luLWJvdHRvbToxNHB4fQogIC5rcGl7YmFja2dyb3VuZDp2YXIoLS1jYXJkKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTBweDtwYWRkaW5nOjEzcHggMTRweH0KICAua3BpLnByaW1hcnl7Ym9yZGVyLWNvbG9yOnZhcigtLWFjY2VudCk7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQtc29mdCl9CiAgLmtwaSAudHtmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS1zdWIpO2ZvbnQtd2VpZ2h0OjYwMH0KICAua3BpIC5iaWd7Zm9udC1zaXplOjIycHg7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi10b3A6NXB4O2xldHRlci1zcGFjaW5nOi0wLjVweH0KICAua3BpIC5iaWcgLnV7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tc3ViKTtmb250LXdlaWdodDo2MDA7bWFyZ2luLWxlZnQ6MnB4fQogIC5rcGkgLmRlbHRhe2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMDttYXJnaW4tdG9wOjRweH0KICAudXB7Y29sb3I6dmFyKC0tZ29vZCl9LmRvd257Y29sb3I6dmFyKC0tYmFkKX0uZmxhdHtjb2xvcjp2YXIoLS1tdXRlZCl9CiAgLmNhcmR7YmFja2dyb3VuZDp2YXIoLS1jYXJkKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTBweDtwYWRkaW5nOjE1cHg7bWFyZ2luLWJvdHRvbToxMnB4fQogIC5jYXJkIGgze2ZvbnQtc2l6ZToxNHB4O2ZvbnQtd2VpZ2h0OjgwMDttYXJnaW4tYm90dG9tOjJweH0KICAuY2FyZCBoMyAubWluaXtjb2xvcjp2YXIoLS1zdWIpO2ZvbnQtd2VpZ2h0OjYwMDtmb250LXNpemU6MTJweH0KICAuY2FyZCAuY3N1Yntmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS1zdWIpO21hcmdpbi1ib3R0b206MTJweH0KICAuc3BsaXRiYXJ7ZGlzcGxheTpmbGV4O2hlaWdodDoyNnB4O2JvcmRlci1yYWRpdXM6N3B4O292ZXJmbG93OmhpZGRlbjttYXJnaW4tYm90dG9tOjEwcHg7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKX0KICAuc3BsaXRiYXIgLmFke2JhY2tncm91bmQ6dmFyKC0tYWNjZW50KX0uc3BsaXRiYXIgLm9yZ3tiYWNrZ3JvdW5kOnZhcigtLW9yZyl9CiAgLnNwbGl0YmFyIHNwYW57ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2NvbG9yOiNmZmY7Zm9udC1zaXplOjExcHg7Zm9udC13ZWlnaHQ6ODAwO21pbi13aWR0aDowfQogIC5zcGxpdGJhciAub3Jne2NvbG9yOnZhcigtLWluayl9CiAgLmNocm93e2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MTUwcHggMWZyIDkycHg7Z2FwOjEwcHg7YWxpZ24taXRlbXM6Y2VudGVyO3BhZGRpbmc6NXB4IDA7Zm9udC1zaXplOjEyLjVweH0KICAuY2hyb3cuZXhwcm93e2N1cnNvcjpwb2ludGVyfQogIC5jaHJvdyAubm17ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NXB4fQogIC5jaHJvdyAuYWR0YWd7Zm9udC1zaXplOjkuNXB4O2ZvbnQtd2VpZ2h0OjgwMDtwYWRkaW5nOjFweCA1cHg7Ym9yZGVyLXJhZGl1czo0cHg7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQtc29mdCk7Y29sb3I6dmFyKC0tYWNjZW50KX0KICAuYXJyb3d7ZGlzcGxheTppbmxpbmUtYmxvY2s7d2lkdGg6MTFweDtmb250LXNpemU6OXB4O2NvbG9yOnZhcigtLXN1Yik7dHJhbnNpdGlvbjp0cmFuc2Zvcm0gLjE1c30KICAudHJhY2t7aGVpZ2h0OjhweDtiYWNrZ3JvdW5kOnZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6NXB4O292ZXJmbG93OmhpZGRlbn0KICAudHJhY2s+c3BhbntkaXNwbGF5OmJsb2NrO2hlaWdodDoxMDAlO2JvcmRlci1yYWRpdXM6NXB4O2JhY2tncm91bmQ6dmFyKC0tb3JnKX0KICAudHJhY2s+c3Bhbi5hZGZ7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQpfQogIC5jaHJvdyAudmFse3RleHQtYWxpZ246cmlnaHQ7Y29sb3I6dmFyKC0tc3ViKX0uY2hyb3cgLnZhbCBie2NvbG9yOnZhcigtLWluayl9CiAgLmNoc3Vie3BhZGRpbmc6MnB4IDAgOHB4IDIwcHg7bWFyZ2luOjAgMCA0cHggOHB4O2JvcmRlci1sZWZ0OjJweCBzb2xpZCB2YXIoLS1hY2NlbnQtc29mdCl9CiAgLnN1YnJvd3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciBhdXRvO2dhcDoxMHB4O2ZvbnQtc2l6ZToxMS41cHg7Y29sb3I6dmFyKC0tc3ViKTtwYWRkaW5nOjNweCAwfQogIC5zdWJyb3cgLm5tMntjb2xvcjp2YXIoLS1pbmspfQogIC5lZmZzdWJ7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDoxMHB4O2ZvbnQtc2l6ZToxMS41cHg7Y29sb3I6dmFyKC0tc3ViKTtwYWRkaW5nOjRweCAwO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHZhcigtLWFjY2VudC1zb2Z0KX0KICAuZWZmc3ViIC5ubTJ7Y29sb3I6dmFyKC0taW5rKTtmb250LXdlaWdodDo2MDB9CiAgLmNvbnZncmlke2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KGF1dG8tZml0LG1pbm1heCgxMzBweCwxZnIpKTtnYXA6MTBweH0KICAuY29udntib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6OXB4O3BhZGRpbmc6MTFweCAxMnB4fQogIC5jb252IC50e2ZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLXN1Yik7Zm9udC13ZWlnaHQ6NjAwfQogIC5jb252IC5iaWd7Zm9udC1zaXplOjIwcHg7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi10b3A6NHB4fQogIC5jb252IC5iaWcgLnV7Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tc3ViKTttYXJnaW4tbGVmdDoxcHh9CiAgLmNvbnYgLnN1YjJ7Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tc3ViKTttYXJnaW4tdG9wOjFweH0KICAuc2Nyb2xse292ZXJmbG93LXg6YXV0b30KICB0YWJsZXt3aWR0aDoxMDAlO2JvcmRlci1jb2xsYXBzZTpjb2xsYXBzZTtmb250LXNpemU6MTJweDttaW4td2lkdGg6NjgwcHh9CiAgdGgsdGR7cGFkZGluZzo3cHggOHB4O3RleHQtYWxpZ246cmlnaHQ7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgdmFyKC0tbGluZSk7d2hpdGUtc3BhY2U6bm93cmFwfQogIHRoOmZpcnN0LWNoaWxkLHRkOmZpcnN0LWNoaWxke3RleHQtYWxpZ246bGVmdH0KICB0aGVhZCB0aHtjb2xvcjp2YXIoLS1zdWIpO2ZvbnQtd2VpZ2h0OjcwMH0KICB0aC5saCx0ZC5saHt0ZXh0LWFsaWduOmxlZnR9CiAgdGQubmFtZXttYXgtd2lkdGg6MjYwcHg7d2hpdGUtc3BhY2U6bm9ybWFsO3dvcmQtYnJlYWs6YnJlYWstd29yZDtsaW5lLWhlaWdodDoxLjM1O3RleHQtYWxpZ246bGVmdH0KICB0ci5leHByb3d7Y3Vyc29yOnBvaW50ZXJ9CiAgLmtwaWNvbHtmb250LXdlaWdodDo4MDA7YmFja2dyb3VuZDpyZ2JhKDEzMSwxMTIsMjMwLDAuMDYpfXRoLmtwaWNvbHtjb2xvcjp2YXIoLS1zdWIpO2JhY2tncm91bmQ6cmdiYSgxMzEsMTEyLDIzMCwwLjA2KX0KICB0ZC5kaW17Y29sb3I6dmFyKC0tbXV0ZWQpfQogIC5kZWx0YS1ze2ZvbnQtc2l6ZToxMC41cHg7Zm9udC13ZWlnaHQ6NzAwO21hcmdpbi1sZWZ0OjVweH0KICAuYmFkZ2V7ZGlzcGxheTppbmxpbmUtYmxvY2s7Zm9udC1zaXplOjEwLjVweDtmb250LXdlaWdodDo3MDA7cGFkZGluZzoxcHggN3B4O2JvcmRlci1yYWRpdXM6NXB4fQogIC5iYWRnZS5yZXtiYWNrZ3JvdW5kOiNlZmVhZmM7Y29sb3I6IzdhNjNkNn0uYmFkZ2UudHJ7YmFja2dyb3VuZDojZWVmMGYzO2NvbG9yOiM3YTg1OTN9CiAgQG1lZGlhIChwcmVmZXJzLWNvbG9yLXNjaGVtZTpkYXJrKXs6cm9vdDpub3QoW2RhdGEtdGhlbWU9ImxpZ2h0Il0pIC5iYWRnZS5yZXtiYWNrZ3JvdW5kOiMyNzFkM2Q7Y29sb3I6I2E5OTFmNX06cm9vdDpub3QoW2RhdGEtdGhlbWU9ImxpZ2h0Il0pIC5iYWRnZS50cntiYWNrZ3JvdW5kOiMyNDJiMzR9fQogIC5kZXRhaWx0ZHtiYWNrZ3JvdW5kOnZhcigtLWJnKTtwYWRkaW5nOjB9LmRldGFpbHRkIC5pbm5lcntwYWRkaW5nOjRweCAxMHB4IDEwcHggMjZweH0KICAudG90cm93IHRke2JvcmRlci10b3A6MnB4IHNvbGlkIHZhcigtLWxpbmUpO2ZvbnQtd2VpZ2h0OjgwMH0KICAuc2VjaGQtcm93IHRke2JhY2tncm91bmQ6dmFyKC0tYWNjZW50LXNvZnQpO2NvbG9yOnZhcigtLWFjY2VudCk7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxMi41cHg7dGV4dC1hbGlnbjpsZWZ0IWltcG9ydGFudDtwYWRkaW5nOjlweCA4cHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgdmFyKC0tbGluZSl9CiAgLnNlZ2hke2ZvbnQtc2l6ZToxMy41cHg7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbjoxNnB4IDAgOHB4fS5zZWdoZCAuc2Vnc3Vie2ZvbnQtc2l6ZToxMS41cHg7Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOnZhcigtLXN1Yik7bWFyZ2luLWxlZnQ6N3B4fQogIC5mb290e2ZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLW11dGVkKTttYXJnaW4tdG9wOjE2cHg7bGluZS1oZWlnaHQ6MS42fQo8L3N0eWxlPgo8L2hlYWQ+Cjxib2R5Pgo8ZGl2IGNsYXNzPSJ3cmFwIj4KICA8aDE+7Yuw66i464uIR08g6rSR6rOgIOyEseqzvCDigJQg7Ya17ZWp67ewPC9oMT4KICA8ZGl2IGNsYXNzPSJjb250cm9scyI+PGRpdiBjbGFzcz0ic2VnIiBpZD0icGVyaW9kc2VnIj48L2Rpdj48ZGl2IGNsYXNzPSJyYW5nZWxhYmVsIiBpZD0icmFuZ2VsYWJlbCI+PC9kaXY+PC9kaXY+CiAgPGRpdiBjbGFzcz0idGFicyIgaWQ9InRhYnMiPjwvZGl2PgogIDxkaXYgaWQ9InZpZXciPjwvZGl2PgogIDxkaXYgY2xhc3M9ImZvb3QiIGlkPSJmb290Ij48L2Rpdj4KPC9kaXY+CjxzY3JpcHQ+Ci8qID09PT09IOuNsOydtO2EsCDso7zsnoUgKGdlbmVyYXRlLnB56rCAIF9fREFUQV9fIOulvCDsi6TsuKEgSlNPTuycvOuhnCDqtZDssrQpID09PT09ICovCmNvbnN0IERBVEEgPSBfX0RBVEFfXzsKY29uc3QgQlVDS0VUUyA9IERBVEEuYnVja2V0czsKY29uc3QgREFZUyA9IERBVEEuZGF5czsKY29uc3QgQ0ggPSBEQVRBLmNoYW5uZWxzOwpjb25zdCBUT1RBTFMgPSBEQVRBLnRvdGFsczsKY29uc3QgVEFYSVRPVCA9IERBVEEudGF4aXRvdDsKY29uc3QgQ0FNUCA9IERBVEEuY2FtcDsKY29uc3QgT05CID0gREFUQS5vbmI7CmNvbnN0IEFERUZGID0gREFUQS5hZGVmZjsKY29uc3QgUk9XUyA9IERBVEEucm93czsKY29uc3QgQlRPVCA9IERBVEEuYnRvdHx8e307CgovKiA9PT09PSDqs6DsoJUg66Gc7KeBID09PT09ICovCmNvbnN0IGY9bj0+KG49PW51bGwpPyLigJQiOk1hdGgucm91bmQobikudG9Mb2NhbGVTdHJpbmcoJ2tvLUtSJyk7CmxldCBQRVJJT0Q9NzsKY29uc3QgUEVSSU9EUz1bWyIxIiwi7LWc6re8IDHsnbwiLDFdLFsiNyIsIuy1nOq3vCA37J28Iiw3XSxbIjE0Iiwi7KCE7LK0KDE07J28KSIsMTRdXTsKZnVuY3Rpb24gd2luZG93cyhuKXtjb25zdCBlbmQ9REFZUy5sZW5ndGgsY3VyUz1NYXRoLm1heCgwLGVuZC1uKSxjdXI9W107Zm9yKGxldCBpPWN1clM7aTxlbmQ7aSsrKWN1ci5wdXNoKGkpO2NvbnN0IHNoaWZ0PShuPD03KT83Om47Y29uc3QgcHJpb3I9Y3VyLm1hcChpPT5pLXNoaWZ0KTtyZXR1cm4ge2N1cixwcmlvcjpwcmlvci5ldmVyeShpPT5pPj0wKT9wcmlvcjpbXX07fQpjb25zdCBzdW09KGFycixpZHgpPT5pZHgucmVkdWNlKChhLGkpPT5hKyhhcnJbaV18fDApLDApOwpjb25zdCBwY3Q9KGMscCk9PihwPT1udWxsfHxwPT09MCk/bnVsbDooYy1wKS9wKjEwMDsKZnVuY3Rpb24gZGVsdGFTcGFuKGMscCxnb29kVXA9dHJ1ZSxzbWFsbD1mYWxzZSl7Y29uc3Qgdj1wY3QoYyxwKTtpZih2PT1udWxsKXJldHVybiAnJztjb25zdCBjbHM9KHY9PT0wKT8nZmxhdCc6KCh2PjA9PT1nb29kVXApPyd1cCc6J2Rvd24nKSxhcj12PjA/J+KWsic6KHY8MD8n4pa8JzonwrcnKTtyZXR1cm4gYDxzcGFuIGNsYXNzPSIke2Nsc30gJHtzbWFsbD8nZGVsdGEtcyc6J2RlbHRhJ30iPiR7YXJ9JHtNYXRoLmFicyh2KS50b0ZpeGVkKDApfSU8L3NwYW4+YDt9CmZ1bmN0aW9uIGtwaShsYWJlbCxjdXIscHJpb3Isbz17fSl7cmV0dXJuIGA8ZGl2IGNsYXNzPSJrcGkgJHtvLnByaW1hcnk/J3ByaW1hcnknOicnfSI+PGRpdiBjbGFzcz0idCI+JHtsYWJlbH08L2Rpdj48ZGl2IGNsYXNzPSJiaWciPiR7by53b24/J+KCqSc6Jyd9JHtmKGN1cil9JHtvLnVuaXQ/YDxzcGFuIGNsYXNzPSJ1Ij4ke28udW5pdH08L3NwYW4+YDonJ308L2Rpdj4ke28ubm9kZWx0YT8nJzpkZWx0YVNwYW4oY3VyLHByaW9yLG8uZ29vZFVwIT09ZmFsc2UpfTwvZGl2PmA7fQpmdW5jdGlvbiBjaFRvdGFsKGtleSxpZHgpe3JldHVybiBDSC5yZWR1Y2UoKGEsYyk9PmErc3VtKGNba2V5XSxpZHgpLDApO30KCi8qIC0tLS0g7JqU7JW9IC0tLS0gKi8KZnVuY3Rpb24gY2hCbG9jayh0aXRsZSxrZXksVyl7CiAgY29uc3Qgcm93cz1DSC5tYXAoYz0+KHtuYW1lOmMubmFtZSxhZDpjLmFkLGN1cjpzdW0oY1trZXldLFcuY3VyKX0pKS5zb3J0KChhLGIpPT5iLmN1ci1hLmN1cik7CiAgY29uc3QgdG90YWw9cm93cy5yZWR1Y2UoKGEscik9PmErci5jdXIsMCl8fDE7CiAgY29uc3QgYWRQY3Q9cm93cy5maWx0ZXIocj0+ci5hZCkucmVkdWNlKChhLHIpPT5hK3IuY3VyLDApL3RvdGFsKjEwMCwgb3JnUGN0PTEwMC1hZFBjdDsKICBjb25zdCBiYXI9YDxkaXYgY2xhc3M9InNwbGl0YmFyIj48c3BhbiBjbGFzcz0iYWQiIHN0eWxlPSJ3aWR0aDoke2FkUGN0fSUiPiR7YWRQY3Q+PTEyPyfqtJHqs6AgJythZFBjdC50b0ZpeGVkKDApKyclJzonJ308L3NwYW4+PHNwYW4gY2xhc3M9Im9yZyIgc3R5bGU9IndpZHRoOiR7b3JnUGN0fSUiPiR7b3JnUGN0Pj0xMj8n67mE6rSR6rOgICcrb3JnUGN0LnRvRml4ZWQoMCkrJyUnOicnfTwvc3Bhbj48L2Rpdj5gOwogIGNvbnN0IGxpc3Q9cm93cy5tYXAocj0+e2NvbnN0IHc9ci5jdXIvdG90YWwqMTAwOwogICAgaWYoci5hZCl7CiAgICAgIHJldHVybiBgPGRpdiBjbGFzcz0iY2hyb3ciPjxkaXYgY2xhc3M9Im5tIj4ke3IubmFtZX08c3BhbiBjbGFzcz0iYWR0YWciPuq0keqzoDwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJ0cmFjayI+PHNwYW4gY2xhc3M9ImFkZiIgc3R5bGU9IndpZHRoOiR7d30lIj48L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0idmFsIj48Yj4ke2Yoci5jdXIpfTwvYj4gLyAke3cudG9GaXhlZCgwKX0lPC9kaXY+PC9kaXY+YDt9CiAgICByZXR1cm4gYDxkaXYgY2xhc3M9ImNocm93Ij48ZGl2IGNsYXNzPSJubSI+JHtyLm5hbWV9PC9kaXY+PGRpdiBjbGFzcz0idHJhY2siPjxzcGFuIHN0eWxlPSJ3aWR0aDoke3d9JSI+PC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9InZhbCI+PGI+JHtmKHIuY3VyKX08L2I+IC8gJHt3LnRvRml4ZWQoMCl9JTwvZGl2PjwvZGl2PmA7CiAgfSkuam9pbignJyk7CiAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJjYXJkIj48aDM+JHt0aXRsZX0gPHNwYW4gY2xhc3M9Im1pbmkiPuy0nSAke2YodG90YWwpfSAo7LGE64SQIO2VqeyCsCAvIOykkeuztSDtj6ztlagpPC9zcGFuPjwvaDM+JHtiYXJ9JHtsaXN0fTwvZGl2PmA7Cn0KZnVuY3Rpb24gb25iQmxvY2soVyl7CiAgY29uc3Qgc3U9Y2hUb3RhbCgnc3UnLFcuY3VyKSwgc3VQPVcucHJpb3IubGVuZ3RoP2NoVG90YWwoJ3N1JyxXLnByaW9yKTpudWxsOwogIGNvbnN0IGl0ZW1zPU9OQi5tYXAobz0+e2NvbnN0IGM9c3VtKG9bMV0sVy5jdXIpLHA9Vy5wcmlvci5sZW5ndGg/c3VtKG9bMV0sVy5wcmlvcik6bnVsbDtjb25zdCByYXRlPXN1P2Mvc3UqMTAwOjAscmF0ZVA9KHN1UCYmcCE9bnVsbCk/cC9zdVAqMTAwOm51bGw7cmV0dXJuIGA8ZGl2IGNsYXNzPSJjb252Ij48ZGl2IGNsYXNzPSJ0Ij4ke29bMF19PC9kaXY+PGRpdiBjbGFzcz0iYmlnIj4ke3JhdGUudG9GaXhlZCgwKX08c3BhbiBjbGFzcz0idSI+JTwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJzdWIyIj4ke2YoYyl966qFICR7ZGVsdGFTcGFuKHJhdGUscmF0ZVAsdHJ1ZSx0cnVlKX08L2Rpdj48L2Rpdj5gO30pLmpvaW4oJycpOwogIHJldHVybiBgPGRpdiBjbGFzcz0iY2FyZCI+PGgzPuyYqOuztOuUqSDsoITtmZjsnKg8L2gzPjxkaXYgY2xhc3M9ImNzdWIiPu2ajOybkOqwgOyehe2VnCDsgqzrnozsnbQg7ZW17IusIO2WieuPmeycvOuhnCDsnbTslrTsp4DripQg67mE7JyoICjqtJHqs6Ag7Y+s7ZWoIOyghOyytCDssYTrhJAg6riw7KSAKTwvZGl2PjxkaXYgY2xhc3M9ImNvbnZncmlkIj4ke2l0ZW1zfTwvZGl2PjwvZGl2PmA7Cn0KZnVuY3Rpb24gZWZmQmxvY2soVyl7CiAgY29uc3QgYm9keT1BREVGRi5tYXAoYT0+ewogICAgY29uc3QgY29zdD1zdW0oYS5jb3N0LFcuY3VyKSxzdT1zdW0oYS5zdSxXLmN1ciksY2FsbD1zdW0oYS5jYWxsLFcuY3VyKTsKICAgIGNvbnN0IGNwcz1zdT9jb3N0L3N1Om51bGwsY3BjPWNhbGw/Y29zdC9jYWxsOm51bGw7CiAgICByZXR1cm4gYDx0cj48dGQgY2xhc3M9Im5hbWUiPiR7YS5uYW1lfTwvdGQ+PHRkIGNsYXNzPSJkaW0iPiR7ZihzdSl9PC90ZD48dGQ+4oKpJHtmKGNwcyl9PC90ZD48dGQgY2xhc3M9ImtwaWNvbCI+JHtmKGNhbGwpfTwvdGQ+PHRkPuKCqSR7ZihjcGMpfTwvdGQ+PHRkIGNsYXNzPSJkaW0iPuKCqSR7Zihjb3N0KX08L3RkPjwvdHI+YDsKICB9KS5qb2luKCcnKTsKICByZXR1cm4gYDxkaXYgY2xhc3M9ImNhcmQiPjxoMz7ssYTrhJDrs4Qg7Zqo7JyoPC9oMz48ZGl2IGNsYXNzPSJjc3ViIj7qtJHqs6Ag7LGE64SQ7J20IOqwgOyehSAx66qFIC8g7YOd7Iuc7Zi47LacIDHqsbTsnYQg66eM65Oc64qUIOuNsCDsk7Qg67mE7JqpICjrgq7snYTsiJjroZ0g7Zqo7Jyo7KCBKTwvZGl2PgogICAgPGRpdiBjbGFzcz0ic2Nyb2xsIj48dGFibGUgc3R5bGU9Im1pbi13aWR0aDo2MjBweCI+PHRoZWFkPjx0cj48dGg+7LGE64SQPC90aD48dGg+6rCA7J6FPC90aD48dGg+6rCA7J6F64u5IOu5hOyaqTwvdGg+PHRoIGNsYXNzPSJrcGljb2wiPu2DneyLnO2YuOy2nDwvdGg+PHRoPu2YuOy2nOuLuSDruYTsmqk8L3RoPjx0aD7qtJHqs6DruYQ8L3RoPjwvdHI+PC90aGVhZD48dGJvZHk+JHtib2R5fTwvdGJvZHk+PC90YWJsZT48L2Rpdj48L2Rpdj5gOwp9CmZ1bmN0aW9uIHJlbmRlclN1bW1hcnkoKXsKICBjb25zdCBXPXdpbmRvd3MoUEVSSU9EKSxwcj1XLnByaW9yLmxlbmd0aDsKICBjb25zdCBzdT1zdW0oVE9UQUxTLnN1LFcuY3VyKSxzdVA9cHI/c3VtKFRPVEFMUy5zdSxXLnByaW9yKTpudWxsOwogIGNvbnN0IGNhcmQ9c3VtKFRPVEFMUy5jYXJkLFcuY3VyKSxjYXJkUD1wcj9zdW0oVE9UQUxTLmNhcmQsVy5wcmlvcik6bnVsbDsKICBjb25zdCBsbz1zdW0oVE9UQUxTLmxvdW5nZSxXLmN1ciksbG9QPXByP3N1bShUT1RBTFMubG91bmdlLFcucHJpb3IpOm51bGw7CiAgY29uc3QgY2FsbD1zdW0oVEFYSVRPVCxXLmN1ciksY2FsbFA9cHI/c3VtKFRBWElUT1QsVy5wcmlvcik6bnVsbDsKICBjb25zdCBjYXJkcz1gPGRpdiBjbGFzcz0ia3BpcyI+CiAgICAke2twaSgi7ZqM7JuQ6rCA7J6FIixzdSxzdVAse3ByaW1hcnk6dHJ1ZSx1bml0OiLrqoUifSl9CiAgICAke2twaSgi6rWQ7Ya17Lm065OcIOuTseuhnSIsY2FyZCxjYXJkUCx7dW5pdDoi6rG0In0pfQogICAgJHtrcGkoIuuMgOykkeq1kO2GteudvOyatOyngCDrsKnrrLgiLGxvLGxvUCx7dW5pdDoi66qFIn0pfQogICAgJHtrcGkoIu2DneyLnO2YuOy2nCIsY2FsbCxjYWxsUCx7dW5pdDoi6rG0In0pfQogIDwvZGl2PmA7CiAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJub3RlbGluZSI+PGI+6rSR6rOgIO2PrO2VqCDsoITssrQg7LGE64SQIOq4sOykgDwvYj48L2Rpdj5gK2NhcmRzK29uYkJsb2NrKFcpK2VmZkJsb2NrKFcpK2NoQmxvY2soIu2ajOybkOqwgOyehSIsInN1IixXKStjaEJsb2NrKCLqtZDthrXsubTrk5wg65Ox66GdIiwiY2FyZCIsVykrY2hCbG9jaygi64yA7KSR6rWQ7Ya165287Jq07KeAIOuwqeusuCIsImxvdW5nZSIsVyk7Cn0KCi8qIC0tLS0g67KE7YK3IO2DrSA9IOq0keqzoCDquLDsl6zrp4wgLS0tLSAqLwpmdW5jdGlvbiBhZ2dSb3cocm93LGlkeCl7Y29uc3Qgcz1yb3cucztyZXR1cm57Y2FsbDpzdW0ocy5jYWxsLGlkeCksY29tcDpzdW0ocy5jb21wLGlkeCksc3U6c3VtKHMuc3UsaWR4KSxjb3N0OnN1bShzLmNvc3QsaWR4KX07fQpmdW5jdGlvbiBidWNrZXRSb3dzKGlkKXtyZXR1cm4gUk9XUy5maWx0ZXIocj0+ci5idWNrZXQ9PT1pZCk7fQpmdW5jdGlvbiBidWNrZXRBY3RpdmUoaWQsVyl7cmV0dXJuIGJ1Y2tldFJvd3MoaWQpLnNvbWUocj0+c3VtKHIucy5jYWxsLFcuY3VyKT4wKTt9CmNvbnN0IEtJTkRfTEFCRUw9e2dvb2dsZToiZ29vZ2xlLmFkcyIsaW5zdGE6Imluc3RhIixldGM6Iuu5hOyaqSDrr7jsl7Drj5kg6rSR6rOgIix4YWRzOiLri6Trpbgg66qp7KCBIOq0keqzoCIsb3JnOiLqtJHqs6Ag7Jm4IOyxhOuEkCJ9OwovKiDtlZwg67KE7YK37J2YIOuqqOuToCDshLnshZjsnYQgJ+2VmOuCmOydmCDtkZwn66GcIOKAlCDsl7Qg7JyE7LmY6rCAIOyghOu2gCDrp57snYwo7KSE66ee7LakKSAqLwpmdW5jdGlvbiBidWNrZXRDb2xzKEIpeyAvLyDqs7XthrUgY29sZ3JvdXAgKyDtl6TrjZQgKOuyhO2CtyDrgrQg66qo65OgIOyEueyFmCDrj5nsnbwpCiAgbGV0IGNvbHcsIGhlYWQ7CiAgaWYoQi5kdWFsQ3BhKXsgY29sdz1bIjI0JSIsIjE0JSIsIjEzJSIsIjEzJSIsIjEzJSIsIjEyJSIsIjExJSJdOwogICAgaGVhZD1gPHRoPuy6oO2OmOyduDwvdGg+PHRoIGNsYXNzPSJsaCI+7JWg65Oc6re466O5PC90aD48dGggY2xhc3M9ImtwaWNvbCI+JHtCLnByaW1hcnlMYWJlbH08L3RoPjx0aD4ke0IuY29tcExhYmVsfTwvdGg+PHRoPuq0keqzoOu5hDwvdGg+PHRoPiR7Qi5jcGFMYWJlbH08L3RoPjx0aD4ke0IuY29tcENwYUxhYmVsfTwvdGg+YDsKICB9IGVsc2UgaWYoQi5zaG93Q29tcCl7IGNvbHc9WyIyOCUiLCIxNiUiLCIxNSUiLCIxNSUiLCIxNCUiLCIxMiUiXTsKICAgIGhlYWQ9YDx0aD7suqDtjpjsnbg8L3RoPjx0aCBjbGFzcz0ibGgiPuyVoOuTnOq3uOujuTwvdGg+PHRoIGNsYXNzPSJrcGljb2wiPiR7Qi5wcmltYXJ5TGFiZWx9PC90aD48dGg+JHtCLmNvbXBMYWJlbH08L3RoPjx0aD7qtJHqs6DruYQ8L3RoPjx0aD4ke0IuY3BhTGFiZWx9PC90aD5gOwogIH0gZWxzZSB7IGNvbHc9WyIyOCUiLCIxNiUiLCIxNSUiLCIxNSUiLCIxNCUiLCIxMiUiXTsKICAgIGhlYWQ9YDx0aD7suqDtjpjsnbg8L3RoPjx0aCBjbGFzcz0ibGgiPuyVoOuTnOq3uOujuTwvdGg+PHRoPu2ajOybkOqwgOyehTwvdGg+PHRoIGNsYXNzPSJrcGljb2wiPiR7Qi5wcmltYXJ5TGFiZWx9PC90aD48dGg+6rSR6rOg67mEPC90aD48dGg+JHtCLmNwYUxhYmVsfTwvdGg+YDsKICB9CiAgY29uc3QgY29sPWA8Y29sZ3JvdXA+JHtjb2x3Lm1hcCh3PT5gPGNvbCBzdHlsZT0id2lkdGg6JHt3fSI+YCkuam9pbignJyl9PC9jb2xncm91cD5gOwogIHJldHVybiB7bmNvbDpjb2x3Lmxlbmd0aCwgY29sLCBoZWFkOmA8dGhlYWQ+PHRyPiR7aGVhZH08L3RyPjwvdGhlYWQ+YH07Cn0KZnVuY3Rpb24gc2VjUm93KG8sQixub0Nvc3QpewogIGxldCBjZWxscz1gPHRkIGNsYXNzPSJuYW1lIj4ke28uY2FtcH08L3RkPjx0ZCBjbGFzcz0ibmFtZSBkaW0iPiR7by5hZ3x8Jyd9PC90ZD5gOwogIGlmKEIuc2hvd0NvbXApIGNlbGxzKz1gPHRkIGNsYXNzPSJrcGljb2wiPiR7ZihvLmNfY2FsbCl9JHtkZWx0YVNwYW4oby5jX2NhbGwsby5wX2NhbGw9PW51bGw/bnVsbDpvLnBfY2FsbCx0cnVlLHRydWUpfTwvdGQ+PHRkPiR7ZihvLmNfY29tcCl9PC90ZD5gOwogIGVsc2UgY2VsbHMrPWA8dGQgY2xhc3M9ImRpbSI+JHtmKG8uY19zdSl9PC90ZD48dGQgY2xhc3M9ImtwaWNvbCI+JHtmKG8uY19jYWxsKX0ke2RlbHRhU3BhbihvLmNfY2FsbCxvLnBfY2FsbD09bnVsbD9udWxsOm8ucF9jYWxsLHRydWUsdHJ1ZSl9PC90ZD5gOwogIGNlbGxzKz1gPHRkIGNsYXNzPSJkaW0iPiR7bm9Db3N0PycnOifigqknK2Yoby5jX2Nvc3QpfTwvdGQ+YDsKICBpZihCLmR1YWxDcGEpe2NvbnN0IGMxPW8uY19jYWxsP28uY19jb3N0L28uY19jYWxsOm51bGwsYzI9by5jX2NvbXA/by5jX2Nvc3Qvby5jX2NvbXA6bnVsbDtjZWxscys9YDx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzooYzEhPW51bGw/J+KCqScrZihjMSk6J+KAlCcpfTwvdGQ+PHRkIGNsYXNzPSJkaW0iPiR7bm9Db3N0PycnOihjMiE9bnVsbD8n4oKpJytmKGMyKTon4oCUJyl9PC90ZD5gO30KICBlbHNle2NvbnN0IGQ9Qi5zaG93Q29tcD9vLmNfY29tcDpvLmNfY2FsbCxjcGE9bm9Db3N0P251bGw6KGQ/by5jX2Nvc3QvZDpudWxsKTtjZWxscys9YDx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzooY3BhIT1udWxsPyfigqknK2YoY3BhKTon4oCUJyl9PC90ZD5gO30KICByZXR1cm4gYDx0cj4ke2NlbGxzfTwvdHI+YDsKfQpmdW5jdGlvbiBzZWNUb3RSb3coYWdnLEIsbm9Db3N0KXsKICBjb25zdCB0PWFnZy5yZWR1Y2UoKGEsbyk9PnthLnN1Kz1vLmNfc3V8fDA7YS5jYWxsKz1vLmNfY2FsbHx8MDthLmNvbXArPW8uY19jb21wfHwwO2EuY29zdCs9by5jX2Nvc3R8fDA7cmV0dXJuIGE7fSx7c3U6MCxjYWxsOjAsY29tcDowLGNvc3Q6MH0pOwogIGxldCBtaWQ9Qi5zaG93Q29tcD9gPHRkIGNsYXNzPSJrcGljb2wiPiR7Zih0LmNhbGwpfTwvdGQ+PHRkPiR7Zih0LmNvbXApfTwvdGQ+YDpgPHRkIGNsYXNzPSJkaW0iPiR7Zih0LnN1KX08L3RkPjx0ZCBjbGFzcz0ia3BpY29sIj4ke2YodC5jYWxsKX08L3RkPmA7CiAgbGV0IGNwYUM7CiAgaWYoQi5kdWFsQ3BhKXtjb25zdCBjMT10LmNhbGw/dC5jb3N0L3QuY2FsbDpudWxsLGMyPXQuY29tcD90LmNvc3QvdC5jb21wOm51bGw7Y3BhQz1gPHRkIGNsYXNzPSJkaW0iPiR7bm9Db3N0PycnOihjMSE9bnVsbD8n4oKpJytmKGMxKTon4oCUJyl9PC90ZD48dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6KGMyIT1udWxsPyfigqknK2YoYzIpOifigJQnKX08L3RkPmA7fQogIGVsc2V7Y29uc3QgZD1CLnNob3dDb21wP3QuY29tcDp0LmNhbGwsY3BhPW5vQ29zdD9udWxsOihkP3QuY29zdC9kOm51bGwpO2NwYUM9YDx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzooY3BhIT1udWxsPyfigqknK2YoY3BhKTon4oCUJyl9PC90ZD5gO30KICByZXR1cm4gYDx0ciBjbGFzcz0idG90cm93Ij48dGQ+7ZWp6rOEPC90ZD48dGQ+PC90ZD4ke21pZH08dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6J+KCqScrZih0LmNvc3QpfTwvdGQ+JHtjcGFDfTwvdHI+YDsKfQpmdW5jdGlvbiBzZWNCbG9jayhyb3dzLGtpbmQsQixuY29sKXsKICBjb25zdCBub0Nvc3Q9KGtpbmQ9PT0iZXRjInx8a2luZD09PSJvcmciKTsKICBjb25zdCBhZ2c9cm93cy5tYXAocj0+e2NvbnN0IG89e2NhbXA6ci5jYW1wLGFnOnIuYWd9O2NvbnN0IGN1PWFnZ1JvdyhyLENVUlcuY3VyKSxwcj1DVVJXLnByaW9yLmxlbmd0aD9hZ2dSb3cocixDVVJXLnByaW9yKTpudWxsO1siY2FsbCIsImNvbXAiLCJzdSIsImNvc3QiXS5mb3JFYWNoKGs9PntvWyJjXyIra109Y3Vba107aWYocHIpb1sicF8iK2tdPXByW2tdO30pO3JldHVybiBvO30pLnNvcnQoKGEsYik9PmIuY19jYWxsLWEuY19jYWxsKTsKICBjb25zdCBoZD1gPHRyIGNsYXNzPSJzZWNoZC1yb3ciPjx0ZCBjb2xzcGFuPSIke25jb2x9Ij4ke0tJTkRfTEFCRUxba2luZF19PC90ZD48L3RyPmA7CiAgcmV0dXJuIGhkK2FnZy5tYXAobz0+c2VjUm93KG8sQixub0Nvc3QpKS5qb2luKCcnKStzZWNUb3RSb3coYWdnLEIsbm9Db3N0KTsKfQpsZXQgQ1VSVz1udWxsOwpmdW5jdGlvbiByZW5kZXJCdWNrZXQoaWQpewogIGNvbnN0IEI9QlVDS0VUUy5maW5kKHg9PnguaWQ9PT1pZCkscm93cz1idWNrZXRSb3dzKGlkKTtDVVJXPXdpbmRvd3MoUEVSSU9EKTsKICBjb25zdCBpc0FkPXI9PnIuY2g9PT0iZ29vZ2xlInx8ci5jaD09PSJpbnN0YSJ8fHIuY2g9PT0iZXRjIjsKICBjb25zdCBhZ2c9KGlkeCxmaWx0KT0+cm93cy5yZWR1Y2UoKG8scik9PntpZihmaWx0JiYhZmlsdChyKSlyZXR1cm4gbztjb25zdCBhPWFnZ1JvdyhyLGlkeCk7by5jYWxsKz1hLmNhbGw7by5jb21wKz1hLmNvbXA7by5jb3N0Kz1hLmNvc3Q7cmV0dXJuIG87fSx7Y2FsbDowLGNvbXA6MCxjb3N0OjB9KTsKICBjb25zdCBjPWFnZyhDVVJXLmN1cixpc0FkKSxwPUNVUlcucHJpb3IubGVuZ3RoP2FnZyhDVVJXLnByaW9yLGlzQWQpOm51bGw7ICAgICAgICAgIC8vIOq0keqzoCDquLDsl6wo66qp7KCBIOy6oO2OmOyduCkKICBjb25zdCBidD1CVE9UW2lkXXx8e307ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAvLyDsoITssrQo7KCEIOyxhOuEkCwg7KeB7KCRIOynkeqzhCkKICBjb25zdCB0b3RDYWxsPXN1bShidC5jYWxsfHxbXSxDVVJXLmN1ciksIHRvdENhbGxQPUNVUlcucHJpb3IubGVuZ3RoP3N1bShidC5jYWxsfHxbXSxDVVJXLnByaW9yKTpudWxsOwogIGNvbnN0IHRvdENvbXA9c3VtKGJ0LmNvbXB8fFtdLENVUlcuY3VyKSwgdG90Q29tcFA9Q1VSVy5wcmlvci5sZW5ndGg/c3VtKGJ0LmNvbXB8fFtdLENVUlcucHJpb3IpOm51bGw7CiAgbGV0IGNhcmRzPWA8ZGl2IGNsYXNzPSJrcGlzIj5gOwogIGNvbnN0IFU9Qi51bml0fHwi6rG0IjsKICBjYXJkcys9a3BpKCLsoITssrQgIitCLnByaW1hcnlMYWJlbCx0b3RDYWxsLHRvdENhbGxQLHt1bml0OlV9KTsKICBjYXJkcys9a3BpKCLqtJHqs6AgIitCLnByaW1hcnlMYWJlbCxjLmNhbGwscCYmcC5jYWxsLHtwcmltYXJ5OnRydWUsdW5pdDpVfSk7CiAgaWYoQi5zaG93Q29tcCl7IGNhcmRzKz1rcGkoIuyghOyytCAiK0IuY29tcExhYmVsLHRvdENvbXAsdG90Q29tcFAse3VuaXQ6VX0pOyBjYXJkcys9a3BpKCLqtJHqs6AgIitCLmNvbXBMYWJlbCxjLmNvbXAscCYmcC5jb21wLHt1bml0OlV9KTsgfQogIGNhcmRzKz1rcGkoIuq0keqzoOu5hCIsYy5jb3N0LHAmJnAuY29zdCx7d29uOnRydWV9KTsKICBpZihCLmR1YWxDcGEpe2NvbnN0IGQxPXg9PnguY2FsbD94LmNvc3QveC5jYWxsOm51bGwsZDI9eD0+eC5jb21wP3guY29zdC94LmNvbXA6bnVsbDtjYXJkcys9a3BpKEIuY3BhTGFiZWwsZDEoYykscCYmZDEocCkse3dvbjp0cnVlLGdvb2RVcDpmYWxzZX0pK2twaShCLmNvbXBDcGFMYWJlbCxkMihjKSxwJiZkMihwKSx7d29uOnRydWUsZ29vZFVwOmZhbHNlfSk7fQogIGVsc2V7Y29uc3QgY3BhPXg9Pntjb25zdCBkPUIuc2hvd0NvbXA/eC5jb21wOnguY2FsbDtyZXR1cm4gZD94LmNvc3QvZDpudWxsO307Y2FyZHMrPWtwaShCLmNwYUxhYmVsLGNwYShjKSxwJiZjcGEocCkse3dvbjp0cnVlLGdvb2RVcDpmYWxzZX0pO30KICBjYXJkcys9YDwvZGl2PmA7CiAgbGV0IGh0bWw9YDxkaXYgY2xhc3M9Im5vdGVsaW5lIj48Yj7soITssrQ8L2I+ID0g7KCEIOyxhOuEkCDsp4HsoJEg7KeR6rOEKOyXkOyWtOu4jOumv+yngCDsoITssrTqsJIpIMK3IDxiPuq0keqzoDwvYj4gPSDrqqnsoIEg7Lqg7Y6Y7J24IOq4sOyXrOu2hDwvZGl2PmArY2FyZHM7CiAgaWYoQi5ub3RlKSBodG1sKz1gPGRpdiBjbGFzcz0iY3N1YiIgc3R5bGU9Im1hcmdpbjotNnB4IDAgMTJweCI+4oC7ICR7Qi5ub3RlfTwvZGl2PmA7CiAgY29uc3QgQz1idWNrZXRDb2xzKEIpOwogIGNvbnN0IGtpbmRzPUIuc2hvd09yZz9bImdvb2dsZSIsImluc3RhIiwiZXRjIiwieGFkcyIsIm9yZyJdOlsiZ29vZ2xlIiwiaW5zdGEiLCJldGMiLCJ4YWRzIl07CiAgbGV0IGJvZHk9IiI7CiAga2luZHMuZm9yRWFjaChraW5kPT57Y29uc3QgcnM9cm93cy5maWx0ZXIocj0+ci5jaD09PWtpbmQpO2lmKHJzLmxlbmd0aClib2R5Kz1zZWNCbG9jayhycyxraW5kLEIsQy5uY29sKTt9KTsKICBodG1sKz1gPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0ic2Nyb2xsIj48dGFibGUgc3R5bGU9Im1pbi13aWR0aDo2ODBweDt0YWJsZS1sYXlvdXQ6Zml4ZWQiPiR7Qy5jb2x9JHtDLmhlYWR9PHRib2R5PiR7Ym9keX08L3Rib2R5PjwvdGFibGU+PC9kaXY+PC9kaXY+YDsKICByZXR1cm4gaHRtbDsKfQoKLyogLS0tLSDshbggLS0tLSAqLwpsZXQgVEFCPSJzdW1tYXJ5IjsKZnVuY3Rpb24gcmVuZGVyKCl7CiAgY29uc3QgVz13aW5kb3dzKFBFUklPRCksY2Q9Vy5jdXIubWFwKGk9PkRBWVNbaV0pOwogIGNvbnN0IGNtcD0hVy5wcmlvci5sZW5ndGg/Iuu5hOq1kOq4sOqwhCDsl4bsnYwiOihQRVJJT0Q9PT0xPyLsoITso7wg64+Z7JqU7J28IOuMgOu5hCI6KFBFUklPRD09PTc/IuyghOyjvCDrjIDruYQiOmDsp4HsoIQgJHtQRVJJT0R97J28IOuMgOu5hGApKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgicmFuZ2VsYWJlbCIpLmlubmVySFRNTD1gPGI+JHtjZFswXX0ke1BFUklPRD4xPyIgfiAiK2NkW2NkLmxlbmd0aC0xXToiIn08L2I+IC8gJHtjbXB9YDsKICBjb25zdCBhY3RpdmU9QlVDS0VUUy5maWx0ZXIoYj0+YnVja2V0QWN0aXZlKGIuaWQsVykpOwogIGlmKFRBQiE9PSJzdW1tYXJ5IiYmIWFjdGl2ZS5zb21lKGI9PmIuaWQ9PT1UQUIpKVRBQj0ic3VtbWFyeSI7CiAgY29uc3QgdGFicz1bWyJzdW1tYXJ5Iiwi7JqU7JW9Il1dLmNvbmNhdChhY3RpdmUubWFwKGI9PltiLmlkLGIubGFiZWxdKSk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInRhYnMiKS5pbm5lckhUTUw9dGFicy5tYXAodD0+YDxidXR0b24gY2xhc3M9InRhYiAke3RbMF09PT1UQUI/J29uJzonJ30iIGRhdGEtdD0iJHt0WzBdfSI+JHt0WzFdfTwvYnV0dG9uPmApLmpvaW4oJycpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ2aWV3IikuaW5uZXJIVE1MPShUQUI9PT0ic3VtbWFyeSIpP3JlbmRlclN1bW1hcnkoKTpyZW5kZXJCdWNrZXQoVEFCKTsKfQpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgicGVyaW9kc2VnIikuaW5uZXJIVE1MPVBFUklPRFMubWFwKHA9PmA8YnV0dG9uIGNsYXNzPSIkeygrcFsyXT09PVBFUklPRCk/J29uJzonJ30iIGRhdGEtcD0iJHtwWzJdfSI+JHtwWzFdfTwvYnV0dG9uPmApLmpvaW4oJycpOwpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgicGVyaW9kc2VnIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGU9Pntjb25zdCBiPWUudGFyZ2V0LmNsb3Nlc3QoImJ1dHRvbiIpO2lmKCFiKXJldHVybjtQRVJJT0Q9K2IuZGF0YXNldC5wO2RvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoIiNwZXJpb2RzZWcgYnV0dG9uIikuZm9yRWFjaCh4PT54LmNsYXNzTGlzdC50b2dnbGUoIm9uIiwreC5kYXRhc2V0LnA9PT1QRVJJT0QpKTtyZW5kZXIoKTt9KTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInRhYnMiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsZT0+e2NvbnN0IGI9ZS50YXJnZXQuY2xvc2VzdCgiLnRhYiIpO2lmKCFiKXJldHVybjtUQUI9Yi5kYXRhc2V0LnQ7cmVuZGVyKCk7fSk7CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJmb290IikuaW5uZXJIVE1MPSLsmpTslb097KCE7LK0IOycoOyehSjqtJHqs6Ar67mE6rSR6rOgKSwg6rSR6rOgPeuztOudvCAvIOuyhO2CtyDtg6096rSR6rOgIOq4sOyXrOunjCAo6riw7YOAIOq0keqzoD3ruYTsmqkg66+47Jew64+ZLCDsi6TsoIHrp4wpPGJyPuuzgO2ZlOycqD3soITso7wg64+Z7JqU7J28KDHsnbwpL+yghOyjvCg37J28KSDrjIDruYQgLyDsp4DtkZzihpE97LSI66GdLCDruYTsmqnihpE967mo6rCVPGJyPu2DreydgCDshKTsoJUr7J6Q64+ZIOyIqOq5gCjsi6TsoIEgMOydtOuptCDshozrqbgpIC8g6rWs6riAPey6oO2OmOyduCwg7J247Iqk7YOAPeyVoOuTnOq3uOujuSI7CnJlbmRlcigpOwo8L3NjcmlwdD4KPC9ib2R5Pgo8L2h0bWw+Cg==").decode("utf-8")


if __name__=="__main__":
    main()
