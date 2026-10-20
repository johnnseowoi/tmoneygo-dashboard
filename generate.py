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
  # 5) 온보딩 전환율(코호트) = 가입 당일 핵심행동 전환자수 (리텐션 리포트). 실패해도 0으로 안전하게 넘어감
  def retention_q(start_ev, ret_ev):
    # 리텐션 리포트는 v5 경로. POST 응답 taskId 위치/상태값이 Actuals와 다름.
    url=f"{API_BASE}/reports/api/v5/apps/{APP}/retention/query"
    base={"from":frm,"to":to,"granularity":"day","intervalsPeriod":1,
          "measurementOption":"general_retention","startEvents":[start_ev],"returnEvents":[ret_ev]}
    full=dict(base,groupBy={"fields":[]},filters=[],sorts=[],pagination={"skip":0,"size":1000})
    tid=None
    for body in (base,full):   # 최소 바디 먼저, 400이면 문서상 필수필드 포함해 재시도
      r=sess.post(url,data=json.dumps(body))
      if r.status_code>=300:
        print(f"  ⚠️ 리텐션 POST {r.status_code}: {r.text[:200]}"); continue
      j=r.json()
      tid=((j.get("data") or {}).get("taskId")) or ((j.get("task") or {}).get("taskId"))
      if tid: break
    if not tid: return None
    g=f"{url}/{tid}"; t0=time.time()
    while time.time()-t0<120:
      gr=sess.get(g,params={"skip":0,"size":10000,"viewFormat":"false"})
      if gr.status_code>=300:
        print(f"  ⚠️ 리텐션 GET {gr.status_code}: {gr.text[:200]}"); return None
      j=gr.json(); st=(j.get("task") or {}).get("status")
      if st=="SUCCESS": return j
      if st in ("FAILURE","FAILED","ERROR","CANCELED"):
        print(f"  ⚠️ 리텐션 상태={st}"); return None
      time.sleep(2)
    return None
  def onb_series(start_ev, ret_ev):
    idx={d:i for i,d in enumerate(days)}; out=[0]*len(days)
    try:
      j=retention_q(start_ev,ret_ev)
      rp=(j or {}).get("retention") or ((j or {}).get("reportData") or {}).get("retention") or {}
      rows=(rp.get("data") or {}).get("rows") or []
      cohorts=rows[0].get("rows",[]) if rows else []
      for cr in cohorts:
        d=(cr.get("date") or "")[:10]
        if d not in idx: continue
        for v in cr.get("values",[]):
          if v.get("periodIndex")==0: out[idx[d]]+=int(v.get("value",0) or 0)
      return out
    except Exception as e:
      print("  ⚠️ 온보딩(리텐션) 조회 실패 → 0으로 둠:", str(e)[:160]); return [0]*len(days)
  onb=[["첫 결제/이용", onb_series(M_SIGNUP, M_TAXI_CALL)],   # purchase_started 전체(택시/버스/킥보드 등)
       ["대중교통라운지 방문", onb_series(M_SIGNUP, M_LOUNGE)],
       ["교통카드 등록", onb_series(M_SIGNUP, M_TPCARD)]]
  onb_note = (sum(sum(o[1]) for o in onb)==0)
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
  base=datetime.date(2026,9,28); days=[(base-datetime.timedelta(days=i)).isoformat() for i in range(59,-1,-1)]; n=len(days)
  CH=[("오가닉(Organic)",False,30,16,24),("google.ads",True,48,22,10),("adison",True,13,6,2),("naver_bsa",True,5,2,1),
      ("insta",True,1,1,4),("tmap",False,9,5,6),("braze(CRM)",False,22,14,20),("kakao",False,6,3,3)]
  channels=[{"name":c[0],"ad":c[1],"su":dser(i*4+1,c[2],1.0,n),"card":dser(i*4+2,c[3],1.0,n),"lounge":dser(i*4+3,c[4],1.0,n)} for i,c in enumerate(CH)]
  taxitot=dser(99,52000,0.5,n)
  camp={"google.ads":[["지방권_강원_tROAS","",0.22],["티머니GO_신규가입_상시","",0.25],["자전거킥보드_ACi_구매완료","",0.20],["부산택시_tROAS","",0.15],["예약택시_ACi_예약완료","",0.10],["지방권_강원_ACe","",0.08]],
        "insta":[["지방택시 활성화_트래픽","강원+1854MF",0.38],["지방택시 활성화_트래픽","대전+1854MF",0.20],["지방택시 활성화_트래픽","충주+1854MF",0.12],["지방택시 활성화_트래픽","제주+1854MF",0.10],["예약택시_트래픽","2049MF + 여행",0.12],["자전거킥보드_트래픽","1829MF",0.08]],
        "adison":[["ridingforever","",1.0]],"naver_bsa":[["메인","",0.7],["썸네일1","",0.3]]}
  onb=[["첫 결제/이용",dser(31,85,0.4,n)],["대중교통라운지 방문",dser(33,21,0.5,n)],["교통카드 등록",dser(34,18,0.5,n)]]  # 가입 당일 코호트 전환자수(일자별)
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
TEMPLATE_HTML=_b64.b64decode("PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9ImtvIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLCB2aWV3cG9ydC1maXQ9Y292ZXIiPgo8dGl0bGU+7Yuw66i464uIR08g6rSR6rOgIOyEseqzvCDthrXtlanrt7A8L3RpdGxlPgo8c3R5bGU+CiAgOnJvb3R7LS1iZzojZjZmNWZiOy0tY2FyZDojZmZmOy0tbGluZTojZWFlN2YzOy0taW5rOiMyYTI2NDA7LS1zdWI6IzhiODZhMDstLWFjY2VudDojODM3MGU2Oy0tYWNjZW50LXNvZnQ6I2YxZWRmYzstLWdvb2Q6IzJlOWU1YjstLWJhZDojYzA0NTNiOy0tbXV0ZWQ6I2JkYjhjYzstLW9yZzojZDZkMmUyO2JveC1zaXppbmc6Ym9yZGVyLWJveDtwYWRkaW5nLXRvcDplbnYoc2FmZS1hcmVhLWluc2V0LXRvcCwwcHgpO3BhZGRpbmctYm90dG9tOmVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tLDBweCl9CiAgQG1lZGlhIChwcmVmZXJzLWNvbG9yLXNjaGVtZTpkYXJrKXs6cm9vdDpub3QoW2RhdGEtdGhlbWU9ImxpZ2h0Il0pey0tYmc6IzE1MTIxZDstLWNhcmQ6IzFlMWEyYTstLWxpbmU6IzJmMjk0MTstLWluazojZWNlOWY1Oy0tc3ViOiM5NDhlYWE7LS1hY2NlbnQ6I2E1OTNmMDstLWFjY2VudC1zb2Z0OiMyNzFkM2Q7LS1tdXRlZDojNWY1ODc1Oy0tb3JnOiM0MTNhNTR9fQogICp7Ym94LXNpemluZzpib3JkZXItYm94O21hcmdpbjowO3BhZGRpbmc6MH0KICBib2R5e2JhY2tncm91bmQ6dmFyKC0tYmcpO2NvbG9yOnZhcigtLWluayk7Zm9udC1mYW1pbHk6IkFwcGxlIFNEIEdvdGhpYyBOZW8iLCJNYWxndW4gR290aGljIiwiUHJldGVuZGFyZCIsc3lzdGVtLXVpLHNhbnMtc2VyaWY7Zm9udC1zaXplOjE0cHg7bGluZS1oZWlnaHQ6MS41O3BhZGRpbmc6MTZweH0KICAud3JhcHttYXgtd2lkdGg6MTEyMHB4O21hcmdpbjowIGF1dG99CiAgaDF7Zm9udC1zaXplOjE5cHg7Zm9udC13ZWlnaHQ6ODAwO2xldHRlci1zcGFjaW5nOi0wLjNweH0KICAuY29udHJvbHN7ZGlzcGxheTpmbGV4O2ZsZXgtd3JhcDp3cmFwO2dhcDoxMHB4O2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW46MTRweCAwfQogIC5zZWd7ZGlzcGxheTpmbGV4O2dhcDozcHg7YmFja2dyb3VuZDp2YXIoLS1jYXJkKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6OXB4O3BhZGRpbmc6NHB4fQogIC5zZWcgYnV0dG9ue2JvcmRlcjpub25lO2JhY2tncm91bmQ6bm9uZTtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxM3B4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS1zdWIpO3BhZGRpbmc6N3B4IDEzcHg7Ym9yZGVyLXJhZGl1czo2cHg7Y3Vyc29yOnBvaW50ZXJ9CiAgLnNlZyBidXR0b24ub257YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQpO2NvbG9yOiNmZmZ9CiAgLnJhbmdlbGFiZWx7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tc3ViKX0ucmFuZ2VsYWJlbCBie2NvbG9yOnZhcigtLWluayl9CiAgLmRhdGVwaWNre2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjZweDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tc3ViKX0KICAuZGF0ZXBpY2sgaW5wdXR7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTIuNXB4O2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjp2YXIoLS1pbmspO2JhY2tncm91bmQ6dmFyKC0tY2FyZCk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKTtib3JkZXItcmFkaXVzOjdweDtwYWRkaW5nOjZweCA4cHg7Y3Vyc29yOnBvaW50ZXJ9CiAgLmRhdGVwaWNrIGlucHV0Ojotd2Via2l0LWNhbGVuZGFyLXBpY2tlci1pbmRpY2F0b3J7Y3Vyc29yOnBvaW50ZXI7b3BhY2l0eTouNn0KICAudGFic3tkaXNwbGF5OmZsZXg7Z2FwOjRweDtiYWNrZ3JvdW5kOnZhcigtLWNhcmQpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6NXB4O21hcmdpbi1ib3R0b206MTRweDtvdmVyZmxvdy14OmF1dG99CiAgLnRhYntmbGV4OjE7bWluLXdpZHRoOjkycHg7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzo5cHggOHB4O2JvcmRlci1yYWRpdXM6N3B4O2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTNweDtjb2xvcjp2YXIoLS1zdWIpO2N1cnNvcjpwb2ludGVyO3doaXRlLXNwYWNlOm5vd3JhcDtib3JkZXI6bm9uZTtiYWNrZ3JvdW5kOm5vbmU7Zm9udC1mYW1pbHk6aW5oZXJpdH0KICAudGFiLm9ue2JhY2tncm91bmQ6dmFyKC0tYWNjZW50KTtjb2xvcjojZmZmfQogIC5ub3RlbGluZXtmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS1zdWIpO21hcmdpbjoycHggMCAxMHB4fQogIC5rcGlze2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KGF1dG8tZml0LG1pbm1heCgxNTBweCwxZnIpKTtnYXA6MTBweDttYXJnaW4tYm90dG9tOjE0cHh9CiAgLmtwaXtiYWNrZ3JvdW5kOnZhcigtLWNhcmQpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6MTNweCAxNHB4fQogIC5rcGkucHJpbWFyeXtib3JkZXItY29sb3I6dmFyKC0tYWNjZW50KTtiYWNrZ3JvdW5kOnZhcigtLWFjY2VudC1zb2Z0KX0KICAua3BpIC50e2ZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLXN1Yik7Zm9udC13ZWlnaHQ6NjAwfQogIC5rcGkgLmJpZ3tmb250LXNpemU6MjJweDtmb250LXdlaWdodDo4MDA7bWFyZ2luLXRvcDo1cHg7bGV0dGVyLXNwYWNpbmc6LTAuNXB4fQogIC5rcGkgLmJpZyAudXtmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS1zdWIpO2ZvbnQtd2VpZ2h0OjYwMDttYXJnaW4tbGVmdDoycHh9CiAgLmtwaSAuZGVsdGF7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6NzAwO21hcmdpbi10b3A6NHB4fQogIC51cHtjb2xvcjp2YXIoLS1nb29kKX0uZG93bntjb2xvcjp2YXIoLS1iYWQpfS5mbGF0e2NvbG9yOnZhcigtLW11dGVkKX0KICAuY2FyZHtiYWNrZ3JvdW5kOnZhcigtLWNhcmQpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6MTVweDttYXJnaW4tYm90dG9tOjEycHh9CiAgLmNhcmQgaDN7Zm9udC1zaXplOjE0cHg7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi1ib3R0b206MnB4fQogIC5jYXJkIGgzIC5taW5pe2NvbG9yOnZhcigtLXN1Yik7Zm9udC13ZWlnaHQ6NjAwO2ZvbnQtc2l6ZToxMnB4fQogIC5jYXJkIC5jc3Vie2ZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLXN1Yik7bWFyZ2luLWJvdHRvbToxMnB4fQogIC5zcGxpdGJhcntkaXNwbGF5OmZsZXg7aGVpZ2h0OjI2cHg7Ym9yZGVyLXJhZGl1czo3cHg7b3ZlcmZsb3c6aGlkZGVuO21hcmdpbi1ib3R0b206MTBweDtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpfQogIC5zcGxpdGJhciAuYWR7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQpfS5zcGxpdGJhciAub3Jne2JhY2tncm91bmQ6dmFyKC0tb3JnKX0KICAuc3BsaXRiYXIgc3BhbntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7Y29sb3I6I2ZmZjtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo4MDA7bWluLXdpZHRoOjB9CiAgLnNwbGl0YmFyIC5vcmd7Y29sb3I6dmFyKC0taW5rKX0KICAuY2hyb3d7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxNTBweCAxZnIgOTJweDtnYXA6MTBweDthbGlnbi1pdGVtczpjZW50ZXI7cGFkZGluZzo1cHggMDtmb250LXNpemU6MTIuNXB4fQogIC5jaHJvdy5leHByb3d7Y3Vyc29yOnBvaW50ZXJ9CiAgLmNocm93IC5ubXtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo1cHh9CiAgLmNocm93IC5hZHRhZ3tmb250LXNpemU6OS41cHg7Zm9udC13ZWlnaHQ6ODAwO3BhZGRpbmc6MXB4IDVweDtib3JkZXItcmFkaXVzOjRweDtiYWNrZ3JvdW5kOnZhcigtLWFjY2VudC1zb2Z0KTtjb2xvcjp2YXIoLS1hY2NlbnQpfQogIC5hcnJvd3tkaXNwbGF5OmlubGluZS1ibG9jazt3aWR0aDoxMXB4O2ZvbnQtc2l6ZTo5cHg7Y29sb3I6dmFyKC0tc3ViKTt0cmFuc2l0aW9uOnRyYW5zZm9ybSAuMTVzfQogIC50cmFja3toZWlnaHQ6OHB4O2JhY2tncm91bmQ6dmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czo1cHg7b3ZlcmZsb3c6aGlkZGVufQogIC50cmFjaz5zcGFue2Rpc3BsYXk6YmxvY2s7aGVpZ2h0OjEwMCU7Ym9yZGVyLXJhZGl1czo1cHg7YmFja2dyb3VuZDp2YXIoLS1vcmcpfQogIC50cmFjaz5zcGFuLmFkZntiYWNrZ3JvdW5kOnZhcigtLWFjY2VudCl9CiAgLmNocm93IC52YWx7dGV4dC1hbGlnbjpyaWdodDtjb2xvcjp2YXIoLS1zdWIpfS5jaHJvdyAudmFsIGJ7Y29sb3I6dmFyKC0taW5rKX0KICAuY2hzdWJ7cGFkZGluZzoycHggMCA4cHggMjBweDttYXJnaW46MCAwIDRweCA4cHg7Ym9yZGVyLWxlZnQ6MnB4IHNvbGlkIHZhcigtLWFjY2VudC1zb2Z0KX0KICAuc3Vicm93e2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIGF1dG87Z2FwOjEwcHg7Zm9udC1zaXplOjExLjVweDtjb2xvcjp2YXIoLS1zdWIpO3BhZGRpbmc6M3B4IDB9CiAgLnN1YnJvdyAubm0ye2NvbG9yOnZhcigtLWluayl9CiAgLmVmZnN1YntkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47Z2FwOjEwcHg7Zm9udC1zaXplOjExLjVweDtjb2xvcjp2YXIoLS1zdWIpO3BhZGRpbmc6NHB4IDA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgdmFyKC0tYWNjZW50LXNvZnQpfQogIC5lZmZzdWIgLm5tMntjb2xvcjp2YXIoLS1pbmspO2ZvbnQtd2VpZ2h0OjYwMH0KICAuY29udmdyaWR7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoYXV0by1maXQsbWlubWF4KDEzMHB4LDFmcikpO2dhcDoxMHB4fQogIC5jb252e2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czo5cHg7cGFkZGluZzoxMXB4IDEycHh9CiAgLmNvbnYgLnR7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tc3ViKTtmb250LXdlaWdodDo2MDB9CiAgLmNvbnYgLmJpZ3tmb250LXNpemU6MjBweDtmb250LXdlaWdodDo4MDA7bWFyZ2luLXRvcDo0cHh9CiAgLmNvbnYgLmJpZyAudXtmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS1zdWIpO21hcmdpbi1sZWZ0OjFweH0KICAuY29udiAuc3ViMntmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS1zdWIpO21hcmdpbi10b3A6MXB4fQogIC5zY3JvbGx7b3ZlcmZsb3cteDphdXRvfQogIHRhYmxle3dpZHRoOjEwMCU7Ym9yZGVyLWNvbGxhcHNlOmNvbGxhcHNlO2ZvbnQtc2l6ZToxMnB4O21pbi13aWR0aDo2ODBweH0KICB0aCx0ZHtwYWRkaW5nOjdweCA4cHg7dGV4dC1hbGlnbjpyaWdodDtib3JkZXItYm90dG9tOjFweCBzb2xpZCB2YXIoLS1saW5lKTt3aGl0ZS1zcGFjZTpub3dyYXB9CiAgdGg6Zmlyc3QtY2hpbGQsdGQ6Zmlyc3QtY2hpbGR7dGV4dC1hbGlnbjpsZWZ0fQogIHRoZWFkIHRoe2NvbG9yOnZhcigtLXN1Yik7Zm9udC13ZWlnaHQ6NzAwfQogIHRoLmxoLHRkLmxoe3RleHQtYWxpZ246bGVmdH0KICB0ZC5uYW1le21heC13aWR0aDoyNjBweDt3aGl0ZS1zcGFjZTpub3JtYWw7d29yZC1icmVhazpicmVhay13b3JkO2xpbmUtaGVpZ2h0OjEuMzU7dGV4dC1hbGlnbjpsZWZ0fQogIHRyLmV4cHJvd3tjdXJzb3I6cG9pbnRlcn0KICAua3BpY29se2ZvbnQtd2VpZ2h0OjgwMDtiYWNrZ3JvdW5kOnJnYmEoMTMxLDExMiwyMzAsMC4wNil9dGgua3BpY29se2NvbG9yOnZhcigtLXN1Yik7YmFja2dyb3VuZDpyZ2JhKDEzMSwxMTIsMjMwLDAuMDYpfQogIHRkLmRpbXtjb2xvcjp2YXIoLS1tdXRlZCl9CiAgLmRlbHRhLXN7Zm9udC1zaXplOjEwLjVweDtmb250LXdlaWdodDo3MDA7bWFyZ2luLWxlZnQ6NXB4fQogIC5iYWRnZXtkaXNwbGF5OmlubGluZS1ibG9jaztmb250LXNpemU6MTAuNXB4O2ZvbnQtd2VpZ2h0OjcwMDtwYWRkaW5nOjFweCA3cHg7Ym9yZGVyLXJhZGl1czo1cHh9CiAgLmJhZGdlLnJle2JhY2tncm91bmQ6I2VmZWFmYztjb2xvcjojN2E2M2Q2fS5iYWRnZS50cntiYWNrZ3JvdW5kOiNlZWYwZjM7Y29sb3I6IzdhODU5M30KICBAbWVkaWEgKHByZWZlcnMtY29sb3Itc2NoZW1lOmRhcmspezpyb290Om5vdChbZGF0YS10aGVtZT0ibGlnaHQiXSkgLmJhZGdlLnJle2JhY2tncm91bmQ6IzI3MWQzZDtjb2xvcjojYTk5MWY1fTpyb290Om5vdChbZGF0YS10aGVtZT0ibGlnaHQiXSkgLmJhZGdlLnRye2JhY2tncm91bmQ6IzI0MmIzNH19CiAgLmRldGFpbHRke2JhY2tncm91bmQ6dmFyKC0tYmcpO3BhZGRpbmc6MH0uZGV0YWlsdGQgLmlubmVye3BhZGRpbmc6NHB4IDEwcHggMTBweCAyNnB4fQogIC50b3Ryb3cgdGR7Ym9yZGVyLXRvcDoycHggc29saWQgdmFyKC0tbGluZSk7Zm9udC13ZWlnaHQ6ODAwfQogIC5zZWNoZC1yb3cgdGR7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQtc29mdCk7Y29sb3I6dmFyKC0tYWNjZW50KTtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjEyLjVweDt0ZXh0LWFsaWduOmxlZnQhaW1wb3J0YW50O3BhZGRpbmc6OXB4IDhweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCB2YXIoLS1saW5lKX0KICAuc2VnaGR7Zm9udC1zaXplOjEzLjVweDtmb250LXdlaWdodDo4MDA7bWFyZ2luOjE2cHggMCA4cHh9LnNlZ2hkIC5zZWdzdWJ7Zm9udC1zaXplOjExLjVweDtmb250LXdlaWdodDo2MDA7Y29sb3I6dmFyKC0tc3ViKTttYXJnaW4tbGVmdDo3cHh9CiAgLmZvb3R7Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tbXV0ZWQpO21hcmdpbi10b3A6MTZweDtsaW5lLWhlaWdodDoxLjZ9Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+CjxkaXYgY2xhc3M9IndyYXAiPgogIDxoMT7ti7DrqLjri4hHTyDqtJHqs6Ag7ISx6rO8IO2Gte2Vqeu3sDwvaDE+CiAgPGRpdiBjbGFzcz0iY29udHJvbHMiPgogICAgPGRpdiBjbGFzcz0iZGF0ZXBpY2siPuq4sOqwhCA8aW5wdXQgdHlwZT0iZGF0ZSIgaWQ9ImZyb21EYXRlIj4gfiA8aW5wdXQgdHlwZT0iZGF0ZSIgaWQ9InRvRGF0ZSI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzZWciIGlkPSJxdWlja3BpY2siPjxidXR0b24gZGF0YS1kPSIxIj4x7J28PC9idXR0b24+PGJ1dHRvbiBkYXRhLWQ9IjciPjfsnbw8L2J1dHRvbj48YnV0dG9uIGRhdGEtZD0iMTQiPjE07J28PC9idXR0b24+PGJ1dHRvbiBkYXRhLWQ9IjMwIj4zMOydvDwvYnV0dG9uPjxidXR0b24gZGF0YS1kPSI2MCI+NjDsnbw8L2J1dHRvbj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InJhbmdlbGFiZWwiIGlkPSJyYW5nZWxhYmVsIj48L2Rpdj4KICA8L2Rpdj4KICA8ZGl2IGNsYXNzPSJ0YWJzIiBpZD0idGFicyI+PC9kaXY+CiAgPGRpdiBpZD0idmlldyI+PC9kaXY+CiAgPGRpdiBjbGFzcz0iZm9vdCIgaWQ9ImZvb3QiPjwvZGl2Pgo8L2Rpdj4KPHNjcmlwdD4KLyogPT09PT0g642w7J207YSwIOyjvOyehSAoZ2VuZXJhdGUucHnqsIAgX19EQVRBX18g66W8IOyLpOy4oSBKU09O7Jy866GcIOq1kOyytCkgPT09PT0gKi8KY29uc3QgREFUQSA9IF9fREFUQV9fOwpjb25zdCBCVUNLRVRTID0gREFUQS5idWNrZXRzOwpjb25zdCBEQVlTID0gREFUQS5kYXlzOwpjb25zdCBDSCA9IERBVEEuY2hhbm5lbHM7CmNvbnN0IFRPVEFMUyA9IERBVEEudG90YWxzOwpjb25zdCBUQVhJVE9UID0gREFUQS50YXhpdG90Owpjb25zdCBDQU1QID0gREFUQS5jYW1wOwpjb25zdCBPTkIgPSBEQVRBLm9uYjsKY29uc3QgQURFRkYgPSBEQVRBLmFkZWZmOwpjb25zdCBST1dTID0gREFUQS5yb3dzOwpjb25zdCBCVE9UID0gREFUQS5idG90fHx7fTsKCi8qID09PT09IOqzoOyglSDroZzsp4EgPT09PT0gKi8KY29uc3QgZj1uPT4obj09bnVsbCk/IuKAlCI6TWF0aC5yb3VuZChuKS50b0xvY2FsZVN0cmluZygna28tS1InKTsKbGV0IFJBPU1hdGgubWF4KDAsREFZUy5sZW5ndGgtNyksIFJCPURBWVMubGVuZ3RoLTE7ICAgLy8g7ISg7YOdIOq4sOqwhCjsnbwg7J24642x7IqkKQpmdW5jdGlvbiB3aW5kb3dzKCl7Y29uc3QgY3VyPVtdO2ZvcihsZXQgaT1SQTtpPD1SQjtpKyspY3VyLnB1c2goaSk7Y29uc3Qgbj1jdXIubGVuZ3RoO2NvbnN0IHByaW9yPWN1ci5tYXAoaT0+aS1uKTtyZXR1cm4ge2N1cixwcmlvcjpwcmlvci5ldmVyeShpPT5pPj0wKT9wcmlvcjpbXX07fQpjb25zdCBzdW09KGFycixpZHgpPT5pZHgucmVkdWNlKChhLGkpPT5hKyhhcnJbaV18fDApLDApOwpjb25zdCBwY3Q9KGMscCk9PihwPT1udWxsfHxwPT09MCk/bnVsbDooYy1wKS9wKjEwMDsKZnVuY3Rpb24gZGVsdGFTcGFuKGMscCxnb29kVXA9dHJ1ZSxzbWFsbD1mYWxzZSl7Y29uc3Qgdj1wY3QoYyxwKTtpZih2PT1udWxsKXJldHVybiAnJztjb25zdCBjbHM9KHY9PT0wKT8nZmxhdCc6KCh2PjA9PT1nb29kVXApPyd1cCc6J2Rvd24nKSxhcj12PjA/J+KWsic6KHY8MD8n4pa8JzonJyk7cmV0dXJuIGA8c3BhbiBjbGFzcz0iJHtjbHN9ICR7c21hbGw/J2RlbHRhLXMnOidkZWx0YSd9Ij4ke2FyfSR7TWF0aC5hYnModikudG9GaXhlZCgwKX0lPC9zcGFuPmA7fQpmdW5jdGlvbiBrcGkobGFiZWwsY3VyLHByaW9yLG89e30pe3JldHVybiBgPGRpdiBjbGFzcz0ia3BpICR7by5wcmltYXJ5PydwcmltYXJ5JzonJ30iPjxkaXYgY2xhc3M9InQiPiR7bGFiZWx9PC9kaXY+PGRpdiBjbGFzcz0iYmlnIj4ke28ud29uPyfigqknOicnfSR7ZihjdXIpfSR7by51bml0P2A8c3BhbiBjbGFzcz0idSI+JHtvLnVuaXR9PC9zcGFuPmA6Jyd9PC9kaXY+JHtvLm5vZGVsdGE/Jyc6ZGVsdGFTcGFuKGN1cixwcmlvcixvLmdvb2RVcCE9PWZhbHNlKX08L2Rpdj5gO30KZnVuY3Rpb24gY2hUb3RhbChrZXksaWR4KXtyZXR1cm4gQ0gucmVkdWNlKChhLGMpPT5hK3N1bShjW2tleV0saWR4KSwwKTt9Ci8qIC0tLS0g7J287J6Q67OEIOy2lOydtCDrnbzsnbjssKjtirggKOuCtOyepSBTVkcsIDLsnbwg7J207IOB7J28IOuVjCkgLS0tLSAqLwpsZXQgVFJFTkRLRVk9InN1IjsKZnVuY3Rpb24gdHJlbmRDaGFydChkYXlzLCBzZXJpZXMpewogIGNvbnN0IFc9NTYwLEg9MTc2LFBMPTQ2LFBSPTE0LFBUPTI0LFBCPTI2LCBuPWRheXMubGVuZ3RoOwogIGNvbnN0IG14PU1hdGgubWF4KDEsLi4uc2VyaWVzLmZsYXRNYXAocz0+cy52YWxzKSk7CiAgY29uc3QgWD1pPT5QTCsobjw9MT8wOmkqKFctUEwtUFIpLyhuLTEpKSwgWT12PT5QVCsoMS12L214KSooSC1QVC1QQik7CiAgY29uc3Qgc2hvd0xibD1uPD0xNTsKICBjb25zdCBib2R5PXNlcmllcy5tYXAocz0+ewogICAgY29uc3QgcHRzPXMudmFscy5tYXAoKHYsaSk9PmAke1goaSkudG9GaXhlZCgxKX0sJHtZKHYpLnRvRml4ZWQoMSl9YCkuam9pbignICcpOwogICAgY29uc3QgZG90cz1zLnZhbHMubWFwKCh2LGkpPT5gPGNpcmNsZSBjeD0iJHtYKGkpLnRvRml4ZWQoMSl9IiBjeT0iJHtZKHYpLnRvRml4ZWQoMSl9IiByPSIyLjMiIGZpbGw9IiR7cy5jb2xvcn0iPjx0aXRsZT4ke2RheXNbaV19ICR7cy5uYW1lfTogJHtmKHYpfTwvdGl0bGU+PC9jaXJjbGU+YCkuam9pbignJyk7CiAgICBjb25zdCBsYmw9c2hvd0xibD9zLnZhbHMubWFwKCh2LGkpPT5gPHRleHQgeD0iJHtYKGkpLnRvRml4ZWQoMSl9IiB5PSIkeyhZKHYpLTcpLnRvRml4ZWQoMSl9IiBmb250LXNpemU9IjkiIGZvbnQtd2VpZ2h0PSI3MDAiIGZpbGw9InZhcigtLWluaykiIHRleHQtYW5jaG9yPSJtaWRkbGUiPiR7Zih2KX08L3RleHQ+YCkuam9pbignJyk6Jyc7CiAgICByZXR1cm4gYDxwb2x5bGluZSBwb2ludHM9IiR7cHRzfSIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIke3MuY29sb3J9IiBzdHJva2Utd2lkdGg9IjIiLz4ke2RvdHN9JHtsYmx9YDsKICB9KS5qb2luKCcnKTsKICBjb25zdCBzdGVwPU1hdGgubWF4KDEsTWF0aC5jZWlsKG4vNykpOyBsZXQgeGw9Jyc7CiAgZm9yKGxldCBpPTA7aTxuO2krPXN0ZXApIHhsKz1gPHRleHQgeD0iJHtYKGkpLnRvRml4ZWQoMSl9IiB5PSIke0gtOH0iIGZvbnQtc2l6ZT0iOSIgZmlsbD0idmFyKC0tbXV0ZWQpIiB0ZXh0LWFuY2hvcj0ibWlkZGxlIj4ke2RheXNbaV0uc2xpY2UoNSl9PC90ZXh0PmA7CiAgY29uc3QgbGVnZW5kPXNlcmllcy5sZW5ndGg+MT9gPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2dhcDoxNHB4O2ZvbnQtc2l6ZToxMXB4O21hcmdpbi10b3A6NnB4Ij5gK3Nlcmllcy5tYXAocz0+YDxzcGFuIHN0eWxlPSJjb2xvcjp2YXIoLS1zdWIpIj48c3BhbiBzdHlsZT0iZGlzcGxheTppbmxpbmUtYmxvY2s7d2lkdGg6OXB4O2hlaWdodDo5cHg7Ym9yZGVyLXJhZGl1czoycHg7YmFja2dyb3VuZDoke3MuY29sb3J9O21hcmdpbi1yaWdodDo0cHg7dmVydGljYWwtYWxpZ246bWlkZGxlIj48L3NwYW4+JHtzLm5hbWV9PC9zcGFuPmApLmpvaW4oJycpK2A8L2Rpdj5gOicnOwogIHJldHVybiBgPGRpdiBjbGFzcz0ic2Nyb2xsIj48c3ZnIHZpZXdCb3g9IjAgMCAke1d9ICR7SH0iIHdpZHRoPSIxMDAlIiBzdHlsZT0ibWluLXdpZHRoOjQ0MHB4O2Rpc3BsYXk6YmxvY2siPgogICAgPGxpbmUgeDE9IiR7UEx9IiB5MT0iJHtZKDApLnRvRml4ZWQoMSl9IiB4Mj0iJHtXLVBSfSIgeTI9IiR7WSgwKS50b0ZpeGVkKDEpfSIgc3Ryb2tlPSJ2YXIoLS1saW5lKSIvPgogICAgJHtzaG93TGJsPycnOmA8dGV4dCB4PSIke1BMLTZ9IiB5PSIkeyhZKG14KSszKS50b0ZpeGVkKDEpfSIgZm9udC1zaXplPSI5IiBmaWxsPSJ2YXIoLS1tdXRlZCkiIHRleHQtYW5jaG9yPSJlbmQiPiR7ZihteCl9PC90ZXh0PmB9CiAgICA8dGV4dCB4PSIke1BMLTZ9IiB5PSIkeyhZKDApKzMpLnRvRml4ZWQoMSl9IiBmb250LXNpemU9IjkiIGZpbGw9InZhcigtLW11dGVkKSIgdGV4dC1hbmNob3I9ImVuZCI+MDwvdGV4dD4KICAgICR7Ym9keX0ke3hsfTwvc3ZnPjwvZGl2PiR7bGVnZW5kfWA7Cn0KZnVuY3Rpb24gc3VtbWFyeVRyZW5kKFcpewogIGlmKFcuY3VyLmxlbmd0aDwyKSByZXR1cm4gJyc7CiAgY29uc3QgTT17c3U6WyLtmozsm5DqsIDsnoUiLFRPVEFMUy5zdV0sY2FyZDpbIuq1kO2Gtey5tOuTnCDrk7HroZ0iLFRPVEFMUy5jYXJkXSxsb3VuZ2U6WyLrjIDspJHqtZDthrXrnbzsmrTsp4Ag67Cp66y4IixUT1RBTFMubG91bmdlXSxjYWxsOlsi7YOd7Iuc7Zi47LacIixUQVhJVE9UXX07CiAgaWYoIU1bVFJFTkRLRVldKSBUUkVOREtFWT0ic3UiOyBjb25zdCBtPU1bVFJFTkRLRVldOwogIGNvbnN0IGRheXM9Vy5jdXIubWFwKGk9PkRBWVNbaV0pLCB2YWxzPVcuY3VyLm1hcChpPT5tWzFdW2ldfHwwKTsKICBjb25zdCBidG5zPWA8ZGl2IGNsYXNzPSJzZWciIHN0eWxlPSJtYXJnaW4tYm90dG9tOjEwcHg7ZmxleC13cmFwOndyYXAiPmArT2JqZWN0LmtleXMoTSkubWFwKGs9PmA8YnV0dG9uIGNsYXNzPSIke2s9PT1UUkVOREtFWT8nb24nOicnfSIgZGF0YS10cmVuZD0iJHtrfSI+JHtNW2tdWzBdfTwvYnV0dG9uPmApLmpvaW4oJycpK2A8L2Rpdj5gOwogIHJldHVybiBgPGRpdiBjbGFzcz0iY2FyZCI+PGgzPuydvOyekOuzhCDstpTsnbQgPHNwYW4gY2xhc3M9Im1pbmkiPiR7bVswXX0gKOyghOyytCDsnKDsnoUpPC9zcGFuPjwvaDM+JHtidG5zfSR7dHJlbmRDaGFydChkYXlzLFt7bmFtZTptWzBdLGNvbG9yOicjODM3MGU2Jyx2YWxzfV0pfTwvZGl2PmA7Cn0KZnVuY3Rpb24gYnVja2V0VHJlbmQoQixyb3dzLFcpewogIGlmKFcuY3VyLmxlbmd0aDwyKSByZXR1cm4gJyc7CiAgY29uc3QgZGF5cz1XLmN1ci5tYXAoaT0+REFZU1tpXSksIGJ0PUJUT1RbQi5pZF18fHtjYWxsOltdfTsKICBjb25zdCB0b3RWPVcuY3VyLm1hcChpPT4oYnQuY2FsbHx8W10pW2ldfHwwKTsKICBjb25zdCBhZFY9Vy5jdXIubWFwKGk9PnJvd3MucmVkdWNlKChhLHIpPT5hKyhyLmNoIT09Im9yZyI/KHIucy5jYWxsW2ldfHwwKTowKSwwKSk7CiAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJjYXJkIj48aDM+7J287J6Q67OEIOy2lOydtCA8c3BhbiBjbGFzcz0ibWluaSI+JHtCLnByaW1hcnlMYWJlbH08L3NwYW4+PC9oMz4ke3RyZW5kQ2hhcnQoZGF5cyxbe25hbWU6IuyghOyytCIsY29sb3I6JyNiOWIzY2MnLHZhbHM6dG90Vn0se25hbWU6Iuq0keqzoCjsoITssrQpIixjb2xvcjonIzgzNzBlNicsdmFsczphZFZ9XSl9PC9kaXY+YDsKfQoKLyogLS0tLSDsmpTslb0gLS0tLSAqLwpmdW5jdGlvbiBjaEJsb2NrKHRpdGxlLGtleSxXKXsKICBjb25zdCByb3dzPUNILm1hcChjPT4oe25hbWU6Yy5uYW1lLGFkOmMuYWQsY3VyOnN1bShjW2tleV0sVy5jdXIpfSkpLnNvcnQoKGEsYik9PmIuY3VyLWEuY3VyKTsKICBjb25zdCB0b3RhbD1yb3dzLnJlZHVjZSgoYSxyKT0+YStyLmN1ciwwKXx8MTsKICBjb25zdCBhZFBjdD1yb3dzLmZpbHRlcihyPT5yLmFkKS5yZWR1Y2UoKGEscik9PmErci5jdXIsMCkvdG90YWwqMTAwLCBvcmdQY3Q9MTAwLWFkUGN0OwogIGNvbnN0IGJhcj1gPGRpdiBjbGFzcz0ic3BsaXRiYXIiPjxzcGFuIGNsYXNzPSJhZCIgc3R5bGU9IndpZHRoOiR7YWRQY3R9JSI+JHthZFBjdD49MTI/J+q0keqzoCAnK2FkUGN0LnRvRml4ZWQoMCkrJyUnOicnfTwvc3Bhbj48c3BhbiBjbGFzcz0ib3JnIiBzdHlsZT0id2lkdGg6JHtvcmdQY3R9JSI+JHtvcmdQY3Q+PTEyPyfruYTqtJHqs6AgJytvcmdQY3QudG9GaXhlZCgwKSsnJSc6Jyd9PC9zcGFuPjwvZGl2PmA7CiAgY29uc3QgbGlzdD1yb3dzLm1hcChyPT57Y29uc3Qgdz1yLmN1ci90b3RhbCoxMDA7CiAgICBpZihyLmFkKXsKICAgICAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJjaHJvdyI+PGRpdiBjbGFzcz0ibm0iPiR7ci5uYW1lfTxzcGFuIGNsYXNzPSJhZHRhZyI+6rSR6rOgPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9InRyYWNrIj48c3BhbiBjbGFzcz0iYWRmIiBzdHlsZT0id2lkdGg6JHt3fSUiPjwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJ2YWwiPjxiPiR7ZihyLmN1cil9PC9iPiAvICR7dy50b0ZpeGVkKDApfSU8L2Rpdj48L2Rpdj5gO30KICAgIHJldHVybiBgPGRpdiBjbGFzcz0iY2hyb3ciPjxkaXYgY2xhc3M9Im5tIj4ke3IubmFtZX08L2Rpdj48ZGl2IGNsYXNzPSJ0cmFjayI+PHNwYW4gc3R5bGU9IndpZHRoOiR7d30lIj48L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0idmFsIj48Yj4ke2Yoci5jdXIpfTwvYj4gLyAke3cudG9GaXhlZCgwKX0lPC9kaXY+PC9kaXY+YDsKICB9KS5qb2luKCcnKTsKICByZXR1cm4gYDxkaXYgY2xhc3M9ImNhcmQiPjxoMz4ke3RpdGxlfSA8c3BhbiBjbGFzcz0ibWluaSI+7LSdICR7Zih0b3RhbCl9ICjssYTrhJAg7ZWp7IKwIC8g7KSR67O1IO2PrO2VqCk8L3NwYW4+PC9oMz4ke2Jhcn0ke2xpc3R9PC9kaXY+YDsKfQpmdW5jdGlvbiBvbmJCbG9jayhXKXsKICBjb25zdCBzdT1zdW0oVE9UQUxTLnN1LFcuY3VyKSwgc3VQPVcucHJpb3IubGVuZ3RoP3N1bShUT1RBTFMuc3UsVy5wcmlvcik6bnVsbDsKICBjb25zdCBhbnlEYXRhPU9OQi5yZWR1Y2UoKGEsbyk9PmErc3VtKG9bMV0sVy5jdXIpLDApPjA7CiAgaWYoIWFueURhdGEpIHJldHVybiBgPGRpdiBjbGFzcz0iY2FyZCI+PGgzPuyYqOuztOuUqSDsoITtmZjsnKggPHNwYW4gY2xhc3M9Im1pbmkiPuqwgOyehSDri7nsnbwg6riw7KSAPC9zcGFuPjwvaDM+PGRpdiBjbGFzcz0iY3N1YiIgc3R5bGU9Im1hcmdpbjowIj7rpqzthZDshZgg642w7J207YSwIOyXsOuPmSDspIDruYQg7KSRIOKAlCDqsIDsnoUg64u57J28IO2VteyLrO2WieuPmSDsoITtmZjsnKjsnbQg6rOnIO2RnOyLnOuQqeuLiOuLpC48L2Rpdj48L2Rpdj5gOwogIGNvbnN0IGl0ZW1zPU9OQi5tYXAobz0+e2NvbnN0IGM9c3VtKG9bMV0sVy5jdXIpLHA9Vy5wcmlvci5sZW5ndGg/c3VtKG9bMV0sVy5wcmlvcik6bnVsbDtjb25zdCByYXRlPXN1P2Mvc3UqMTAwOjAscmF0ZVA9KHN1UCYmcCE9bnVsbCk/cC9zdVAqMTAwOm51bGw7cmV0dXJuIGA8ZGl2IGNsYXNzPSJjb252Ij48ZGl2IGNsYXNzPSJ0Ij4ke29bMF19PC9kaXY+PGRpdiBjbGFzcz0iYmlnIj4ke3JhdGUudG9GaXhlZCgxKX08c3BhbiBjbGFzcz0idSI+JTwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJzdWIyIj4ke2YoYyl966qFIC8g6rCA7J6FICR7ZihzdSl966qFICR7ZGVsdGFTcGFuKHJhdGUscmF0ZVAsdHJ1ZSx0cnVlKX08L2Rpdj48L2Rpdj5gO30pLmpvaW4oJycpOwogIHJldHVybiBgPGRpdiBjbGFzcz0iY2FyZCI+PGgzPuyYqOuztOuUqSDsoITtmZjsnKggPHNwYW4gY2xhc3M9Im1pbmkiPuqwgOyehSDri7nsnbwg6riw7KSAPC9zcGFuPjwvaDM+PGRpdiBjbGFzcz0iY3N1YiI+6rCA7J6F7ZWcIOyCrOuejOydtCA8Yj7qsIDsnoUg64u57J28PC9iPiDtlbTri7kg7ZaJ64+Z7J2EIO2VnCDruYTsnKggKOy9lO2YuO2KuCDstpTsoIEgLyDqtJHqs6Ag7Y+s7ZWoIOyghOyytCk8L2Rpdj48ZGl2IGNsYXNzPSJjb252Z3JpZCI+JHtpdGVtc308L2Rpdj48L2Rpdj5gOwp9CmZ1bmN0aW9uIGVmZkJsb2NrKFcpewogIGNvbnN0IGJvZHk9QURFRkYubWFwKGE9PnsKICAgIGNvbnN0IGNvc3Q9c3VtKGEuY29zdCxXLmN1ciksc3U9c3VtKGEuc3UsVy5jdXIpLGNhbGw9c3VtKGEuY2FsbCxXLmN1cik7CiAgICBjb25zdCBjcHM9c3U/Y29zdC9zdTpudWxsLGNwYz1jYWxsP2Nvc3QvY2FsbDpudWxsOwogICAgcmV0dXJuIGA8dHI+PHRkIGNsYXNzPSJuYW1lIj4ke2EubmFtZX08L3RkPjx0ZCBjbGFzcz0iZGltIj4ke2Yoc3UpfTwvdGQ+PHRkPuKCqSR7ZihjcHMpfTwvdGQ+PHRkIGNsYXNzPSJrcGljb2wiPiR7ZihjYWxsKX08L3RkPjx0ZD7igqkke2YoY3BjKX08L3RkPjx0ZCBjbGFzcz0iZGltIj7igqkke2YoY29zdCl9PC90ZD48L3RyPmA7CiAgfSkuam9pbignJyk7CiAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJjYXJkIj48aDM+7LGE64SQ67OEIO2aqOycqDwvaDM+PGRpdiBjbGFzcz0iY3N1YiI+6rSR6rOgIOyxhOuEkOydtCDqsIDsnoUgMeuqhSAvIO2DneyLnO2YuOy2nCAx6rG07J2EIOunjOuTnOuKlCDrjbAg7JO0IOu5hOyaqSAo64Ku7J2E7IiY66GdIO2aqOycqOyggSk8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNjcm9sbCI+PHRhYmxlIHN0eWxlPSJtaW4td2lkdGg6NjIwcHgiPjx0aGVhZD48dHI+PHRoPuyxhOuEkDwvdGg+PHRoPuqwgOyehTwvdGg+PHRoPuqwgOyeheuLuSDruYTsmqk8L3RoPjx0aCBjbGFzcz0ia3BpY29sIj7tg53si5ztmLjstpw8L3RoPjx0aD7tmLjstpzri7kg67mE7JqpPC90aD48dGg+6rSR6rOg67mEPC90aD48L3RyPjwvdGhlYWQ+PHRib2R5PiR7Ym9keX08L3Rib2R5PjwvdGFibGU+PC9kaXY+PC9kaXY+YDsKfQpmdW5jdGlvbiByZW5kZXJTdW1tYXJ5KCl7CiAgY29uc3QgVz13aW5kb3dzKCkscHI9Vy5wcmlvci5sZW5ndGg7CiAgY29uc3Qgc3U9c3VtKFRPVEFMUy5zdSxXLmN1ciksc3VQPXByP3N1bShUT1RBTFMuc3UsVy5wcmlvcik6bnVsbDsKICBjb25zdCBjYXJkPXN1bShUT1RBTFMuY2FyZCxXLmN1ciksY2FyZFA9cHI/c3VtKFRPVEFMUy5jYXJkLFcucHJpb3IpOm51bGw7CiAgY29uc3QgbG89c3VtKFRPVEFMUy5sb3VuZ2UsVy5jdXIpLGxvUD1wcj9zdW0oVE9UQUxTLmxvdW5nZSxXLnByaW9yKTpudWxsOwogIGNvbnN0IGNhbGw9c3VtKFRBWElUT1QsVy5jdXIpLGNhbGxQPXByP3N1bShUQVhJVE9ULFcucHJpb3IpOm51bGw7CiAgY29uc3QgY2FyZHM9YDxkaXYgY2xhc3M9ImtwaXMiPgogICAgJHtrcGkoIu2ajOybkOqwgOyehSIsc3Usc3VQLHtwcmltYXJ5OnRydWUsdW5pdDoi66qFIn0pfQogICAgJHtrcGkoIuq1kO2Gtey5tOuTnCDrk7HroZ0iLGNhcmQsY2FyZFAse3VuaXQ6IuqxtCJ9KX0KICAgICR7a3BpKCLrjIDspJHqtZDthrXrnbzsmrTsp4Ag67Cp66y4Iixsbyxsb1Ase3VuaXQ6IuuqhSJ9KX0KICAgICR7a3BpKCLtg53si5ztmLjstpwiLGNhbGwsY2FsbFAse3VuaXQ6IuqxtCJ9KX0KICA8L2Rpdj5gOwogIHJldHVybiBgPGRpdiBjbGFzcz0ibm90ZWxpbmUiPjxiPuq0keqzoCDtj6ztlagg7KCE7LK0IOyxhOuEkCDquLDspIA8L2I+PC9kaXY+YCtjYXJkcytzdW1tYXJ5VHJlbmQoVykrb25iQmxvY2soVykrZWZmQmxvY2soVykrY2hCbG9jaygi7ZqM7JuQ6rCA7J6FIiwic3UiLFcpK2NoQmxvY2soIuq1kO2Gtey5tOuTnCDrk7HroZ0iLCJjYXJkIixXKStjaEJsb2NrKCLrjIDspJHqtZDthrXrnbzsmrTsp4Ag67Cp66y4IiwibG91bmdlIixXKTsKfQoKLyogLS0tLSDrsoTtgrcg7YOtID0g6rSR6rOgIOq4sOyXrOunjCAtLS0tICovCmZ1bmN0aW9uIGFnZ1Jvdyhyb3csaWR4KXtjb25zdCBzPXJvdy5zO3JldHVybntjYWxsOnN1bShzLmNhbGwsaWR4KSxjb21wOnN1bShzLmNvbXAsaWR4KSxzdTpzdW0ocy5zdSxpZHgpLGNvc3Q6c3VtKHMuY29zdCxpZHgpfTt9CmZ1bmN0aW9uIGJ1Y2tldFJvd3MoaWQpe3JldHVybiBST1dTLmZpbHRlcihyPT5yLmJ1Y2tldD09PWlkKTt9CmZ1bmN0aW9uIGJ1Y2tldEFjdGl2ZShpZCxXKXtyZXR1cm4gYnVja2V0Um93cyhpZCkuc29tZShyPT5zdW0oci5zLmNhbGwsVy5jdXIpPjApO30KY29uc3QgS0lORF9MQUJFTD17Z29vZ2xlOiJnb29nbGUuYWRzIixpbnN0YToiaW5zdGEiLGV0Yzoi67mE7JqpIOuvuOyXsOuPmSDqtJHqs6AiLHhhZHM6IuuLpOuluCDrqqnsoIEg6rSR6rOgIixvcmc6Iuq0keqzoCDsmbgg7LGE64SQIn07Ci8qIO2VnCDrsoTtgrfsnZgg66qo65OgIOyEueyFmOydhCAn7ZWY64KY7J2YIO2RnCfroZwg4oCUIOyXtCDsnITsuZjqsIAg7KCE67aAIOunnuydjCjspITrp57stqQpICovCmZ1bmN0aW9uIGJ1Y2tldENvbHMoQil7IC8vIOqzte2GtSBjb2xncm91cCArIO2XpOuNlCAo67KE7YK3IOuCtCDrqqjrk6Ag7IS57IWYIOuPmeydvCkg4oCUIO2ajOybkOqwgOyehSDsl7Qg7ZWt7IOBIO2PrO2VqAogIGxldCBjb2x3LCBoZWFkOwogIGlmKEIuZHVhbENwYSl7IGNvbHc9WyIyMiUiLCIxMyUiLCIxMSUiLCIxMiUiLCIxMSUiLCIxMiUiLCIxMCUiLCI5JSJdOwogICAgaGVhZD1gPHRoPuy6oO2OmOyduDwvdGg+PHRoIGNsYXNzPSJsaCI+7JWg65Oc6re466O5PC90aD48dGg+7ZqM7JuQ6rCA7J6FPC90aD48dGggY2xhc3M9ImtwaWNvbCI+JHtCLnByaW1hcnlMYWJlbH08L3RoPjx0aD4ke0IuY29tcExhYmVsfTwvdGg+PHRoPuq0keqzoOu5hDwvdGg+PHRoPiR7Qi5jcGFMYWJlbH08L3RoPjx0aD4ke0IuY29tcENwYUxhYmVsfTwvdGg+YDsKICB9IGVsc2UgaWYoQi5zaG93Q29tcCl7IGNvbHc9WyIyNCUiLCIxNCUiLCIxMiUiLCIxNCUiLCIxMyUiLCIxMiUiLCIxMSUiXTsKICAgIGhlYWQ9YDx0aD7suqDtjpjsnbg8L3RoPjx0aCBjbGFzcz0ibGgiPuyVoOuTnOq3uOujuTwvdGg+PHRoPu2ajOybkOqwgOyehTwvdGg+PHRoIGNsYXNzPSJrcGljb2wiPiR7Qi5wcmltYXJ5TGFiZWx9PC90aD48dGg+JHtCLmNvbXBMYWJlbH08L3RoPjx0aD7qtJHqs6DruYQ8L3RoPjx0aD4ke0IuY3BhTGFiZWx9PC90aD5gOwogIH0gZWxzZSB7IGNvbHc9WyIyOCUiLCIxNiUiLCIxNSUiLCIxNSUiLCIxNCUiLCIxMiUiXTsKICAgIGhlYWQ9YDx0aD7suqDtjpjsnbg8L3RoPjx0aCBjbGFzcz0ibGgiPuyVoOuTnOq3uOujuTwvdGg+PHRoPu2ajOybkOqwgOyehTwvdGg+PHRoIGNsYXNzPSJrcGljb2wiPiR7Qi5wcmltYXJ5TGFiZWx9PC90aD48dGg+6rSR6rOg67mEPC90aD48dGg+JHtCLmNwYUxhYmVsfTwvdGg+YDsKICB9CiAgY29uc3QgY29sPWA8Y29sZ3JvdXA+JHtjb2x3Lm1hcCh3PT5gPGNvbCBzdHlsZT0id2lkdGg6JHt3fSI+YCkuam9pbignJyl9PC9jb2xncm91cD5gOwogIHJldHVybiB7bmNvbDpjb2x3Lmxlbmd0aCwgY29sLCBoZWFkOmA8dGhlYWQ+PHRyPiR7aGVhZH08L3RyPjwvdGhlYWQ+YH07Cn0KZnVuY3Rpb24gc2VjUm93KG8sQixub0Nvc3QpewogIGxldCBjZWxscz1gPHRkIGNsYXNzPSJuYW1lIj4ke28uY2FtcH08L3RkPjx0ZCBjbGFzcz0ibmFtZSBkaW0iPiR7by5hZ3x8Jyd9PC90ZD5gOwogIGNlbGxzKz1gPHRkIGNsYXNzPSJkaW0iPiR7ZihvLmNfc3UpfTwvdGQ+YDsgICAvLyDtmozsm5DqsIDsnoUgKO2VreyDgSkKICBpZihCLnNob3dDb21wKSBjZWxscys9YDx0ZCBjbGFzcz0ia3BpY29sIj4ke2Yoby5jX2NhbGwpfSR7ZGVsdGFTcGFuKG8uY19jYWxsLG8ucF9jYWxsPT1udWxsP251bGw6by5wX2NhbGwsdHJ1ZSx0cnVlKX08L3RkPjx0ZD4ke2Yoby5jX2NvbXApfTwvdGQ+YDsKICBlbHNlIGNlbGxzKz1gPHRkIGNsYXNzPSJrcGljb2wiPiR7ZihvLmNfY2FsbCl9JHtkZWx0YVNwYW4oby5jX2NhbGwsby5wX2NhbGw9PW51bGw/bnVsbDpvLnBfY2FsbCx0cnVlLHRydWUpfTwvdGQ+YDsKICBjZWxscys9YDx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzon4oKpJytmKG8uY19jb3N0KX08L3RkPmA7CiAgaWYoQi5kdWFsQ3BhKXtjb25zdCBjMT1vLmNfY2FsbD9vLmNfY29zdC9vLmNfY2FsbDpudWxsLGMyPW8uY19jb21wP28uY19jb3N0L28uY19jb21wOm51bGw7Y2VsbHMrPWA8dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6KGMxIT1udWxsPyfigqknK2YoYzEpOifigJQnKX08L3RkPjx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzooYzIhPW51bGw/J+KCqScrZihjMik6J+KAlCcpfTwvdGQ+YDt9CiAgZWxzZXtjb25zdCBkPUIuc2hvd0NvbXA/by5jX2NvbXA6by5jX2NhbGwsY3BhPW5vQ29zdD9udWxsOihkP28uY19jb3N0L2Q6bnVsbCk7Y2VsbHMrPWA8dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6KGNwYSE9bnVsbD8n4oKpJytmKGNwYSk6J+KAlCcpfTwvdGQ+YDt9CiAgcmV0dXJuIGA8dHI+JHtjZWxsc308L3RyPmA7Cn0KZnVuY3Rpb24gc2VjVG90Um93KGFnZyxCLG5vQ29zdCl7CiAgY29uc3QgdD1hZ2cucmVkdWNlKChhLG8pPT57YS5zdSs9by5jX3N1fHwwO2EuY2FsbCs9by5jX2NhbGx8fDA7YS5jb21wKz1vLmNfY29tcHx8MDthLmNvc3QrPW8uY19jb3N0fHwwO3JldHVybiBhO30se3N1OjAsY2FsbDowLGNvbXA6MCxjb3N0OjB9KTsKICBsZXQgbWlkPWA8dGQgY2xhc3M9ImRpbSI+JHtmKHQuc3UpfTwvdGQ+YCsoQi5zaG93Q29tcD9gPHRkIGNsYXNzPSJrcGljb2wiPiR7Zih0LmNhbGwpfTwvdGQ+PHRkPiR7Zih0LmNvbXApfTwvdGQ+YDpgPHRkIGNsYXNzPSJrcGljb2wiPiR7Zih0LmNhbGwpfTwvdGQ+YCk7CiAgbGV0IGNwYUM7CiAgaWYoQi5kdWFsQ3BhKXtjb25zdCBjMT10LmNhbGw/dC5jb3N0L3QuY2FsbDpudWxsLGMyPXQuY29tcD90LmNvc3QvdC5jb21wOm51bGw7Y3BhQz1gPHRkIGNsYXNzPSJkaW0iPiR7bm9Db3N0PycnOihjMSE9bnVsbD8n4oKpJytmKGMxKTon4oCUJyl9PC90ZD48dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6KGMyIT1udWxsPyfigqknK2YoYzIpOifigJQnKX08L3RkPmA7fQogIGVsc2V7Y29uc3QgZD1CLnNob3dDb21wP3QuY29tcDp0LmNhbGwsY3BhPW5vQ29zdD9udWxsOihkP3QuY29zdC9kOm51bGwpO2NwYUM9YDx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzooY3BhIT1udWxsPyfigqknK2YoY3BhKTon4oCUJyl9PC90ZD5gO30KICByZXR1cm4gYDx0ciBjbGFzcz0idG90cm93Ij48dGQ+7ZWp6rOEPC90ZD48dGQ+PC90ZD4ke21pZH08dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6J+KCqScrZih0LmNvc3QpfTwvdGQ+JHtjcGFDfTwvdHI+YDsKfQpmdW5jdGlvbiBzZWNCbG9jayhyb3dzLGtpbmQsQixuY29sKXsKICBjb25zdCBub0Nvc3Q9KGtpbmQ9PT0iZXRjInx8a2luZD09PSJvcmciKTsKICBjb25zdCBhZ2c9cm93cy5tYXAocj0+e2NvbnN0IG89e2NhbXA6ci5jYW1wLGFnOnIuYWd9O2NvbnN0IGN1PWFnZ1JvdyhyLENVUlcuY3VyKSxwcj1DVVJXLnByaW9yLmxlbmd0aD9hZ2dSb3cocixDVVJXLnByaW9yKTpudWxsO1siY2FsbCIsImNvbXAiLCJzdSIsImNvc3QiXS5mb3JFYWNoKGs9PntvWyJjXyIra109Y3Vba107aWYocHIpb1sicF8iK2tdPXByW2tdO30pO3JldHVybiBvO30pLnNvcnQoKGEsYik9PmIuY19jYWxsLWEuY19jYWxsKTsKICBjb25zdCBoZD1gPHRyIGNsYXNzPSJzZWNoZC1yb3ciPjx0ZCBjb2xzcGFuPSIke25jb2x9Ij4ke0tJTkRfTEFCRUxba2luZF19PC90ZD48L3RyPmA7CiAgcmV0dXJuIGhkK2FnZy5tYXAobz0+c2VjUm93KG8sQixub0Nvc3QpKS5qb2luKCcnKStzZWNUb3RSb3coYWdnLEIsbm9Db3N0KTsKfQpsZXQgQ1VSVz1udWxsOwpmdW5jdGlvbiByZW5kZXJCdWNrZXQoaWQpewogIGNvbnN0IEI9QlVDS0VUUy5maW5kKHg9PnguaWQ9PT1pZCkscm93cz1idWNrZXRSb3dzKGlkKTtDVVJXPXdpbmRvd3MoKTsKICBjb25zdCBpc0FkPXI9PnIuY2g9PT0iZ29vZ2xlInx8ci5jaD09PSJpbnN0YSJ8fHIuY2g9PT0iZXRjIjsKICBjb25zdCBhZ2c9KGlkeCxmaWx0KT0+cm93cy5yZWR1Y2UoKG8scik9PntpZihmaWx0JiYhZmlsdChyKSlyZXR1cm4gbztjb25zdCBhPWFnZ1JvdyhyLGlkeCk7by5jYWxsKz1hLmNhbGw7by5jb21wKz1hLmNvbXA7by5jb3N0Kz1hLmNvc3Q7cmV0dXJuIG87fSx7Y2FsbDowLGNvbXA6MCxjb3N0OjB9KTsKICBjb25zdCBjPWFnZyhDVVJXLmN1cixpc0FkKSxwPUNVUlcucHJpb3IubGVuZ3RoP2FnZyhDVVJXLnByaW9yLGlzQWQpOm51bGw7ICAgICAgICAgIC8vIOq0keqzoCDquLDsl6wo66qp7KCBIOy6oO2OmOyduCkKICBjb25zdCBidD1CVE9UW2lkXXx8e307ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAvLyDsoITssrQo7KCEIOyxhOuEkCwg7KeB7KCRIOynkeqzhCkKICBjb25zdCB0b3RDYWxsPXN1bShidC5jYWxsfHxbXSxDVVJXLmN1ciksIHRvdENhbGxQPUNVUlcucHJpb3IubGVuZ3RoP3N1bShidC5jYWxsfHxbXSxDVVJXLnByaW9yKTpudWxsOwogIGNvbnN0IHRvdENvbXA9c3VtKGJ0LmNvbXB8fFtdLENVUlcuY3VyKSwgdG90Q29tcFA9Q1VSVy5wcmlvci5sZW5ndGg/c3VtKGJ0LmNvbXB8fFtdLENVUlcucHJpb3IpOm51bGw7CiAgbGV0IGNhcmRzPWA8ZGl2IGNsYXNzPSJrcGlzIj5gOwogIGNvbnN0IFU9Qi51bml0fHwi6rG0IjsKICBjYXJkcys9a3BpKCLsoITssrQgIitCLnByaW1hcnlMYWJlbCx0b3RDYWxsLHRvdENhbGxQLHt1bml0OlV9KTsKICBjYXJkcys9a3BpKCLqtJHqs6AgIitCLnByaW1hcnlMYWJlbCxjLmNhbGwscCYmcC5jYWxsLHtwcmltYXJ5OnRydWUsdW5pdDpVfSk7CiAgaWYoQi5zaG93Q29tcCl7IGNhcmRzKz1rcGkoIuyghOyytCAiK0IuY29tcExhYmVsLHRvdENvbXAsdG90Q29tcFAse3VuaXQ6VX0pOyBjYXJkcys9a3BpKCLqtJHqs6AgIitCLmNvbXBMYWJlbCxjLmNvbXAscCYmcC5jb21wLHt1bml0OlV9KTsgfQogIGNhcmRzKz1rcGkoIuq0keqzoOu5hCIsYy5jb3N0LHAmJnAuY29zdCx7d29uOnRydWV9KTsKICBpZihCLmR1YWxDcGEpe2NvbnN0IGQxPXg9PnguY2FsbD94LmNvc3QveC5jYWxsOm51bGwsZDI9eD0+eC5jb21wP3guY29zdC94LmNvbXA6bnVsbDtjYXJkcys9a3BpKEIuY3BhTGFiZWwsZDEoYykscCYmZDEocCkse3dvbjp0cnVlLGdvb2RVcDpmYWxzZX0pK2twaShCLmNvbXBDcGFMYWJlbCxkMihjKSxwJiZkMihwKSx7d29uOnRydWUsZ29vZFVwOmZhbHNlfSk7fQogIGVsc2V7Y29uc3QgY3BhPXg9Pntjb25zdCBkPUIuc2hvd0NvbXA/eC5jb21wOnguY2FsbDtyZXR1cm4gZD94LmNvc3QvZDpudWxsO307Y2FyZHMrPWtwaShCLmNwYUxhYmVsLGNwYShjKSxwJiZjcGEocCkse3dvbjp0cnVlLGdvb2RVcDpmYWxzZX0pO30KICBjYXJkcys9YDwvZGl2PmA7CiAgLy8g7KCE7LK0IOuMgOu5hCDqtJHqs6Ag67mE7KSRIOKAlCDrtoTsnpDripQgJ+yghOyytCDqtJHqs6Ag6riw7JesJyjrqqnsoIEr64uk66W4IOuqqeyggSwgb3JnIOygnOyZuCkuIOu5hOyaqS9DUEHripQg66qp7KCB66eMKOychCBLUEkpCiAgY29uc3QgYWRBbGw9YWdnKENVUlcuY3VyLHI9PnIuY2ghPT0ib3JnIikuY2FsbDsKICBjb25zdCBhZFBjdD10b3RDYWxsP01hdGgubWluKDEwMCxhZEFsbC90b3RDYWxsKjEwMCk6MDsKICBjb25zdCBzaGFyZUJhcj1gPGRpdiBjbGFzcz0iY2FyZCIgc3R5bGU9InBhZGRpbmc6MTFweCAxNXB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpiYXNlbGluZTtnYXA6MTJweDtmbGV4LXdyYXA6d3JhcCI+PGRpdiBjbGFzcz0iY3N1YiIgc3R5bGU9Im1hcmdpbjowIj7qtJHqs6Ag6riw7JesIOu5hOykkTwvZGl2PjxkaXYgc3R5bGU9ImZvbnQtc2l6ZToyMHB4O2ZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1hY2NlbnQpO2xldHRlci1zcGFjaW5nOi0wLjVweCI+JHthZFBjdC50b0ZpeGVkKGFkUGN0PDEwPzE6MCl9JTwvZGl2PjxkaXYgY2xhc3M9ImNzdWIiIHN0eWxlPSJtYXJnaW46MCI+6rSR6rOgICR7ZihhZEFsbCl9IC8g7KCE7LK0ICR7Zih0b3RDYWxsKX08L2Rpdj48L2Rpdj5gOwogIGxldCBodG1sPWA8ZGl2IGNsYXNzPSJub3RlbGluZSIgc3R5bGU9ImxpbmUtaGVpZ2h0OjEuNyI+PGI+7KCE7LK0PC9iPiA9IOyghCDssYTrhJAg7KeB7KCRIOynkeqzhCjsl5DslrTruIzrpr/sp4Ag7KCE7LK06rCSKTxicj48Yj7qtJHqs6Av6rSR6rOg67mEL0NQQTwvYj4gPSDrqqnsoIEg7Lqg7Y6Y7J2466eMPGJyPjxiPuu5hOykkTwvYj4gPSDsoITssrQg6rSR6rOgIOq4sOyXrDwvZGl2PmArY2FyZHMrc2hhcmVCYXI7CiAgaWYoQi5ub3RlKSBodG1sKz1gPGRpdiBjbGFzcz0iY3N1YiIgc3R5bGU9Im1hcmdpbjotNnB4IDAgMTJweCI+4oC7ICR7Qi5ub3RlfTwvZGl2PmA7CiAgaHRtbCs9YnVja2V0VHJlbmQoQixyb3dzLENVUlcpOwogIGNvbnN0IEM9YnVja2V0Q29scyhCKTsKICBjb25zdCBraW5kcz1CLnNob3dPcmc/WyJnb29nbGUiLCJpbnN0YSIsImV0YyIsInhhZHMiLCJvcmciXTpbImdvb2dsZSIsImluc3RhIiwiZXRjIiwieGFkcyJdOwogIGxldCBib2R5PSIiOwogIGtpbmRzLmZvckVhY2goa2luZD0+e2NvbnN0IHJzPXJvd3MuZmlsdGVyKHI9PnIuY2g9PT1raW5kKTtpZihycy5sZW5ndGgpYm9keSs9c2VjQmxvY2socnMsa2luZCxCLEMubmNvbCk7fSk7CiAgaHRtbCs9YDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9InNjcm9sbCI+PHRhYmxlIHN0eWxlPSJtaW4td2lkdGg6NjgwcHg7dGFibGUtbGF5b3V0OmZpeGVkIj4ke0MuY29sfSR7Qy5oZWFkfTx0Ym9keT4ke2JvZHl9PC90Ym9keT48L3RhYmxlPjwvZGl2PjwvZGl2PmA7CiAgcmV0dXJuIGh0bWw7Cn0KCi8qIC0tLS0g7IW4IC0tLS0gKi8KbGV0IFRBQj0ic3VtbWFyeSI7CmZ1bmN0aW9uIHJlbmRlcigpewogIGNvbnN0IFc9d2luZG93cygpOwogIGNvbnN0IGNtcD0hVy5wcmlvci5sZW5ndGg/Iuu5hOq1kOq4sOqwhCDsl4bsnYwiOmDsp4HsoIQgJHtXLmN1ci5sZW5ndGh97J28IOuMgOu5hGA7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInJhbmdlbGFiZWwiKS5pbm5lckhUTUw9YDxiPiR7REFZU1tSQV19IH4gJHtEQVlTW1JCXX08L2I+IC8gJHtXLmN1ci5sZW5ndGh97J28IC8gJHtjbXB9YDsKICBjb25zdCBhY3RpdmU9QlVDS0VUUy5maWx0ZXIoYj0+YnVja2V0QWN0aXZlKGIuaWQsVykpOwogIGlmKFRBQiE9PSJzdW1tYXJ5IiYmIWFjdGl2ZS5zb21lKGI9PmIuaWQ9PT1UQUIpKVRBQj0ic3VtbWFyeSI7CiAgY29uc3QgdGFicz1bWyJzdW1tYXJ5Iiwi7JqU7JW9Il1dLmNvbmNhdChhY3RpdmUubWFwKGI9PltiLmlkLGIubGFiZWxdKSk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInRhYnMiKS5pbm5lckhUTUw9dGFicy5tYXAodD0+YDxidXR0b24gY2xhc3M9InRhYiAke3RbMF09PT1UQUI/J29uJzonJ30iIGRhdGEtdD0iJHt0WzBdfSI+JHt0WzFdfTwvYnV0dG9uPmApLmpvaW4oJycpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ2aWV3IikuaW5uZXJIVE1MPShUQUI9PT0ic3VtbWFyeSIpP3JlbmRlclN1bW1hcnkoKTpyZW5kZXJCdWNrZXQoVEFCKTsKfQooZnVuY3Rpb24gaW5pdERhdGVzKCl7CiAgY29uc3QgZmQ9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoImZyb21EYXRlIiksIHRkPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ0b0RhdGUiKTsKICBmZC5taW49dGQubWluPURBWVNbMF07IGZkLm1heD10ZC5tYXg9REFZU1tEQVlTLmxlbmd0aC0xXTsKICBmZC52YWx1ZT1EQVlTW1JBXTsgdGQudmFsdWU9REFZU1tSQl07CiAgZnVuY3Rpb24gYXBwbHkoKXsKICAgIGxldCBhPURBWVMuaW5kZXhPZihmZC52YWx1ZSksIGI9REFZUy5pbmRleE9mKHRkLnZhbHVlKTsKICAgIGlmKGE8MClhPVJBOyBpZihiPDApYj1SQjsgaWYoYT5iKXtjb25zdCB0PWE7YT1iO2I9dDt9CiAgICBSQT1hOyBSQj1iOyBmZC52YWx1ZT1EQVlTW1JBXTsgdGQudmFsdWU9REFZU1tSQl07IHJlbmRlcigpOwogIH0KICBmZC5hZGRFdmVudExpc3RlbmVyKCJjaGFuZ2UiLGFwcGx5KTsgdGQuYWRkRXZlbnRMaXN0ZW5lcigiY2hhbmdlIixhcHBseSk7CiAgLy8g67mg66W4IOyEoO2DnTog7LWc6re8IDcvMTQvMzDsnbwKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgicXVpY2twaWNrIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGU9Pntjb25zdCBidG49ZS50YXJnZXQuY2xvc2VzdCgiYnV0dG9uIik7aWYoIWJ0bilyZXR1cm47Y29uc3QgZD0rYnRuLmRhdGFzZXQuZDtSQj1EQVlTLmxlbmd0aC0xO1JBPU1hdGgubWF4KDAsREFZUy5sZW5ndGgtZCk7ZmQudmFsdWU9REFZU1tSQV07dGQudmFsdWU9REFZU1tSQl07cmVuZGVyKCk7fSk7Cn0pKCk7CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ0YWJzIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGU9Pntjb25zdCBiPWUudGFyZ2V0LmNsb3Nlc3QoIi50YWIiKTtpZighYilyZXR1cm47VEFCPWIuZGF0YXNldC50O3JlbmRlcigpO30pOwpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgidmlldyIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixlPT57Y29uc3QgYj1lLnRhcmdldC5jbG9zZXN0KCJbZGF0YS10cmVuZF0iKTtpZighYilyZXR1cm47VFJFTkRLRVk9Yi5kYXRhc2V0LnRyZW5kO3JlbmRlcigpO30pOwpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgiZm9vdCIpLmlubmVySFRNTD0i7JqU7JW9PeyghOyytCDsnKDsnoUo6rSR6rOgK+u5hOq0keqzoCksIOq0keqzoD3rs7TrnbwgLyDrsoTtgrcg7YOtPeq0keqzoCDquLDsl6zrp4wo66qp7KCBIOy6oO2OmOyduCksIOq4sO2DgCDqtJHqs6A967mE7JqpIOuvuOyXsOuPmSjsi6TsoIHrp4wpPGJyPuq0keqzoC/qtJHqs6DruYQvQ1BBPeuqqeyggSDsuqDtjpjsnbjrp4wsIOu5hOykkT3soITssrQg6rSR6rOgIOq4sOyXrCAvIOyYqOuztOuUqSDsoITtmZjsnKg96rCA7J6FIOuLueydvCDsvZTtmLjtirgg6riw7KSAPGJyPuuzgO2ZlOycqD3sp4HsoIQg64+Z7J28IOq4uOydtCDquLDqsIQg64yA67mEICjsp4DtkZzihpEg7LSI66GdLCDruYTsmqnihpEg67mo6rCVKSAvIOq4sOqwhOydgCDri6zroKXsnLzroZwg7ISg7YOdKOy1nOq3vCA2MOydvCDrgrQpLCDtg63snYAg7Iuk7KCBIDDsnbTrqbQg7J6Q64+ZIOyIqOq5gCI7CnJlbmRlcigpOwo8L3NjcmlwdD4KPC9ib2R5Pgo8L2h0bWw+Cg==").decode("utf-8")


if __name__=="__main__":
    main()
