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
M_INSTALL="app_installs"; M_INSTALL_USERS="app_install_users"   # 앱설치(유저수)
M_OPEN_USERS="app_open_users"                                   # 앱 방문(실행) 유저수
M_OPEN="app_open"                                               # 앱 열기(재방문 리텐션 return)
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
  {"id":"lounge","label":"대중교통라운지","primaryLabel":"방문자수","cpaLabel":"방문자당 비용","showComp":False,"showOrg":True,"unit":"명","note":""},
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
  rt=query(sess,frm,to,["event_date"],[M_INSTALL_USERS,M_OPEN_USERS,M_SIGNUP_USERS,M_TPCARD_USERS,M_LOUNGE_USERS])
  def totd(m): d=to_daily(rt,days,0,m); return d.get("_",[0]*len(days))
  totals={"install":totd(M_INSTALL_USERS),"visit":totd(M_OPEN_USERS),"su":totd(M_SIGNUP_USERS),"card":totd(M_TPCARD_USERS),"lounge":totd(M_LOUNGE_USERS)}
  # 2) 전체 택시호출 — 전체값은 귀속 필터(VT) 없이. VT를 걸면 오가닉(미귀속)이 빠져 전체가 과소집계됨
  r=query(sess,frm,to,["event_date"],[M_TAXI_CALL],[{"dimension":"event_action","filterType":"IN","values":["taxi"]}])
  taxitot=to_daily(r,days,0,M_TAXI_CALL)["_"] if r else [0]*len(days)
  # 3) 채널별 효율(광고): 가입+비용(필터 없이) / 택시호출(event_action=taxi) 따로
  adset=["google.adwords","facebook.business","adison","naver_bsa"]
  chf={"dimension":"channel","filterType":"IN","values":adset}
  r=query(sess,frm,to,["event_date","channel"],[M_SIGNUP_USERS],[chf,VT]); eff_su=to_daily(r,days,0,M_SIGNUP_USERS,1)
  r=query(sess,frm,to,["event_date","channel"],[M_INSTALL_USERS],[chf,VT]); eff_inst=to_daily(r,days,0,M_INSTALL_USERS,1)
  r=query(sess,frm,to,["event_date","channel"],[M_COST],[chf]); eff_cost=to_daily(r,days,0,M_COST,1)
  r=query(sess,frm,to,["event_date","channel"],[M_TAXI_CALL],[chf,{"dimension":"event_action","filterType":"IN","values":["taxi"]},VT]); eff_call=to_daily(r,days,0,M_TAXI_CALL,1)
  adeff=[{"name":disp(c),"cost":eff_cost.get(c,[0]*len(days)),"install":eff_inst.get(c,[0]*len(days)),"su":eff_su.get(c,[0]*len(days)),"call":eff_call.get(c,[0]*len(days))} for c in adset]
  # 4) 버킷 4개
  rows=[]; camp={}; btot={}; n=len(days)
  def smap_ccag(r,metric):  # groupby [date,channel,campaign,ad_group] → {(ch,cp,ag):[days]}
    idx={d:i for i,d in enumerate(days)}; out={}
    for row in (r or []):
      gb=row.get("groupBys",[]); d=gb[0]
      if d not in idx: continue
      key=(gb[1], gb[2] if len(gb)>2 else "", gb[3] if len(gb)>3 else "", gb[4] if len(gb)>4 else "")
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
    rp=query(sess,frm,to,["event_date","channel","campaign","ad_group","ad_creative"],[pmet],[chf]+ea_p+[VT])
    # 광고 외(org) 채널 행: 채널 단위·VT 없이(오가닉 포함)
    rog=query(sess,frm,to,["event_date","channel"],[pmet],ea_p or None)
    rc=None; rogc=None
    if spec["comp"]:
      cmet,cact=spec["comp"]; ea_c=[{"dimension":"event_action","filterType":"IN","values":cact}] if cact else []
      rct=query(sess,frm,to,["event_date"],[cmet],ea_c or None)
      bt["comp"]=to_daily(rct,days,0,cmet).get("_",[0]*len(days))
      rc=query(sess,frm,to,["event_date","channel","campaign","ad_group","ad_creative"],[cmet],[chf]+ea_c+[VT])
      rogc=query(sess,frm,to,["event_date","channel"],[cmet],ea_c or None)
    btot[bid]=bt
    # 광고 캠페인 가입(VT)/비용(필터없음) — adset만
    rs=query(sess,frm,to,["event_date","channel","campaign","ad_group","ad_creative"],[M_SIGNUP_USERS],[chf,VT])
    rk=query(sess,frm,to,["event_date","channel","campaign","ad_group","ad_creative"],[M_COST],[chf])
    S_call=smap_ccag(rp,pmet); S_comp=smap_ccag(rc,cmet) if rc else {}
    S_su=smap_ccag(rs,M_SIGNUP_USERS); S_cost=smap_ccag(rk,M_COST)
    for (ch,cp,ag,cr) in (set(S_call)|set(S_comp)|set(S_su)|set(S_cost)):
      cc=S_call.get((ch,cp,ag,cr),[0]*n); mm=S_comp.get((ch,cp,ag,cr),[0]*n)
      ss=S_su.get((ch,cp,ag,cr),[0]*n);  kk=S_cost.get((ch,cp,ag,cr),[0]*n)
      if sum(cc)==0 and sum(mm)==0: continue
      k=kind_of(ch)
      if k in ("google","insta","etc") and not is_purpose(bid, cp): k="xads"
      rows.append({"bucket":bid,"ch":k,"camp":(cp or disp(ch)),"ag":ag,"cr":cr,"seg":"-","s":{"call":cc,"comp":mm,"su":ss,"cost":kk}})
    # org 채널 행 (VT 없는 채널 단위 집계에서 광고 외 채널만)
    O_call=smap_ch(rog,pmet); O_comp=smap_ch(rogc,cmet) if rogc else {}
    for ch in (set(O_call)|set(O_comp)):
      if kind_of(ch)!="org": continue
      cc=O_call.get(ch,[0]*n); mm=O_comp.get(ch,[0]*n)
      if sum(cc)==0 and sum(mm)==0: continue
      rows.append({"bucket":bid,"ch":"org","camp":disp(ch),"ag":"","cr":"","seg":"-","s":{"call":cc,"comp":mm,"su":[0]*n,"cost":[0]*n}})
  # 5) 온보딩 전환율(코호트) = 가입 당일 핵심행동 전환자수 (리텐션 리포트). 실패해도 0으로 안전하게 넘어감
  def retention_task(body):
    # 리텐션 리포트는 v5 경로. POST 응답 taskId 위치/상태값이 Actuals와 다름.
    url=f"{API_BASE}/reports/api/v5/apps/{APP}/retention/query"
    r=sess.post(url,data=json.dumps(body))
    if r.status_code>=300:
      print(f"  ⚠️ 리텐션 POST {r.status_code}: {r.text[:200]}"); return None
    j=r.json(); tid=((j.get("data") or {}).get("taskId")) or ((j.get("task") or {}).get("taskId"))
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
  def retention_q(start_ev, ret_ev):
    base={"from":frm,"to":to,"granularity":"day","intervalsPeriod":1,
          "measurementOption":"general_retention","startEvents":[start_ev],"returnEvents":[ret_ev]}
    # 최소 바디 먼저, 실패하면 문서상 필수필드 포함해 재시도
    return retention_task(base) or retention_task(dict(base,groupBy={"fields":[]},filters=[],sorts=[],pagination={"skip":0,"size":1000}))
  def onb_series(start_ev, ret_ev, with_cohort=False):
    idx={d:i for i,d in enumerate(days)}; out=[0]*len(days); coh=[0]*len(days)
    try:
      j=retention_q(start_ev,ret_ev)
      rp=(j or {}).get("retention") or ((j or {}).get("reportData") or {}).get("retention") or {}
      rows=(rp.get("data") or {}).get("rows") or []
      cohorts=rows[0].get("rows",[]) if rows else []
      for cr in cohorts:
        d=(cr.get("date") or "")[:10]
        if d not in idx: continue
        coh[idx[d]]+=int(((cr.get("totalValue") or {}).get("value",0)) or 0)  # 일자별 가입 코호트
        for v in cr.get("values",[]):
          if v.get("periodIndex")==0: out[idx[d]]+=int(v.get("value",0) or 0)  # 당일 전환자
      return (out,coh) if with_cohort else out
    except Exception as e:
      print("  ⚠️ 온보딩(리텐션) 조회 실패 → 0으로 둠:", str(e)[:160])
      return ([0]*len(days),[0]*len(days)) if with_cohort else [0]*len(days)
  # 가입→첫결제는 코호트(가입수)도 함께 받아 '당일 비결제자 = 가입 − 당일 결제' 계산에 사용
  onb=[["첫 결제/이용", onb_series(M_SIGNUP, M_TAXI_CALL)],   # purchase_started 전체(택시/버스/킥보드 등)
       ["대중교통라운지 방문", onb_series(M_SIGNUP, M_LOUNGE)],
       ["교통카드 등록", onb_series(M_SIGNUP, M_TPCARD)]]
  onb_note = (sum(sum(o[1]) for o in onb)==0)
  # 5-b) 재방문(앱 리텐션) — 설치 코호트가 D1/D7/D14/D30에 앱을 다시 연 비율. D30까지 성숙한 고정 코호트
  def build_retn():
    try:
      cto=datetime.date.fromisoformat(to)-datetime.timedelta(days=31)  # D30까지 성숙하도록
      cfrm=cto-datetime.timedelta(days=27)
      body={"from":cfrm.isoformat(),"to":cto.isoformat(),"granularity":"day","intervalsPeriod":30,
            "measurementOption":"general_retention","startEvents":[M_INSTALL],"returnEvents":[M_OPEN]}
      j=retention_task(body) or retention_task(dict(body,groupBy={"fields":[]},filters=[],sorts=[],pagination={"skip":0,"size":1000}))
      if not j: return None
      rp=j.get("retention") or (j.get("reportData") or {}).get("retention") or {}
      rows=(rp.get("data") or {}).get("rows") or []
      tot=(rows[0].get("total") if rows else {}) or {}
      cohort=((tot.get("totalValue") or {}).get("value",0)) or 0
      rate={}
      for v in (tot.get("values") or []): rate[v.get("periodIndex")]=v.get("rate",0) or 0
      pts=[["D1",round(rate.get(1,0),4)],["D7",round(rate.get(7,0),4)],["D14",round(rate.get(14,0),4)],["D30",round(rate.get(30,0),4)]]
      return {"window":f"{cfrm.isoformat()} ~ {cto.isoformat()}","cohort":cohort,"points":pts}
    except Exception as e:
      print("  ⚠️ 재방문 리텐션 조회 실패 → 생략:", str(e)[:160]); return None
  retn=build_retn()
  # 5-c) 설치→가입 전환(채널별) — 성숙 코호트(7일 경과). 설치·가입은 1회성이라 7일 누적=순전환
  def build_funnel():
    # 신규고객 퍼널: 설치 코호트 기준 → 회원가입 → 서비스 이용(첫 결제/이용). 채널별. 성숙(7일) 코호트
    try:
      cto=datetime.date.fromisoformat(to)-datetime.timedelta(days=8)
      cfrm=cto-datetime.timedelta(days=27)
      def by_kind(start_ev, ret_ev):
        body={"from":cfrm.isoformat(),"to":cto.isoformat(),"granularity":"day","intervalsPeriod":7,
              "measurementOption":"general_retention","startEvents":[start_ev],"returnEvents":[ret_ev],
              "groupBy":{"fields":["channel"]}}
        j=retention_task(body) or retention_task(dict(body,filters=[],sorts=[],pagination={"skip":0,"size":1000}))
        acc={}
        if not j: return acc
        rp=j.get("retention") or (j.get("reportData") or {}).get("retention") or {}
        for g in ((rp.get("data") or {}).get("rows") or []):
          k=kind_of((g.get("metadata") or ["?"])[0]); tot=g.get("total") or {}
          cohort=((tot.get("totalValue") or {}).get("value",0)) or 0
          cv=sum((v.get("value",0) or 0) for v in (tot.get("values") or []))
          a=acc.setdefault(k,[0,0]); a[0]+=cohort; a[1]+=cv
        return acc
      S=by_kind(M_INSTALL, M_SIGNUP)        # 설치→가입
      U=by_kind(M_INSTALL, M_TAXI_CALL)     # 설치→서비스 이용(purchase_started 전체)
      if not S: return None
      LAB={"google":"google.ads","insta":"insta","etc":"기타광고","org":"비광고"}
      out=[]; T=[0,0,0]
      for k in ("google","insta","etc","org"):
        if S.get(k) and S[k][0]>0:
          inst=S[k][0]; sg=S[k][1]; sv=(U.get(k) or [0,0])[1]
          out.append([LAB[k],inst,sg,sv]); T[0]+=inst; T[1]+=sg; T[2]+=sv
      if T[0]>0: out.insert(0,["전체",T[0],T[1],T[2]])
      return {"window":f"{cfrm.isoformat()} ~ {cto.isoformat()}","rows":out}
    except Exception as e:
      print("  ⚠️ 신규고객 퍼널 조회 실패 → 생략:", str(e)[:160]); return None
  funnel=build_funnel()
  # 5-d) 지표별 광고 기여 드릴다운 — 채널→캠페인→애드그룹→소재. 광고(adset)만, VT 귀속. 일자별(달력 연동)
  gb=["event_date","channel","campaign","ad_group","ad_creative"]
  rd =query(sess,frm,to,gb,[M_INSTALL_USERS,M_OPEN_USERS,M_SIGNUP_USERS,M_TPCARD_USERS,M_LOUNGE_USERS],[chf,VT])
  rdc=query(sess,frm,to,gb,[M_TAXI_CALL],[chf,{"dimension":"event_action","filterType":"IN","values":["taxi"]},VT])
  def dmap(r,metric):
    idx={d:i for i,d in enumerate(days)}; out={}
    for row in (r or []):
      g=row.get("groupBys",[]); d=g[0]
      if d not in idx: continue
      key=(g[1], g[2] if len(g)>2 else "", g[3] if len(g)>3 else "", g[4] if len(g)>4 else "")
      out.setdefault(key,[0]*len(days))[idx[d]] += val(row,metric)
    return out
  D_inst=dmap(rd,M_INSTALL_USERS); D_vis=dmap(rd,M_OPEN_USERS); D_su=dmap(rd,M_SIGNUP_USERS); D_card=dmap(rd,M_TPCARD_USERS); D_lo=dmap(rd,M_LOUNGE_USERS); D_call=dmap(rdc,M_TAXI_CALL)
  drill_rows=[]
  for key in (set(D_inst)|set(D_vis)|set(D_su)|set(D_card)|set(D_lo)|set(D_call)):
    ch,cp,ag,cr=key
    v={"install":D_inst.get(key,[0]*len(days)),"visit":D_vis.get(key,[0]*len(days)),"su":D_su.get(key,[0]*len(days)),"card":D_card.get(key,[0]*len(days)),
       "lounge":D_lo.get(key,[0]*len(days)),"call":D_call.get(key,[0]*len(days))}
    if sum(sum(s) for s in v.values())==0: continue
    drill_rows.append({"ch":disp(ch),"camp":(cp or "(캠페인 없음)"),"ag":(ag or "(애드그룹 없음)"),"cr":(cr or "(소재 없음)"),"v":v})
  drill={"metrics":[["install","앱설치"],["visit","앱 방문"],["su","회원가입"],["card","교통카드 등록"],["lounge","대중교통라운지 방문"],["call","택시호출"]],"rows":drill_rows}
  data={"generated":datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")+" 기준","days":days,"buckets":BUCKETS,
        "channels":channels,"taxitot":taxitot,"totals":totals,"adeff":adeff,"onb":onb,"rows":rows,"camp":camp,"btot":btot,"onbNote":onb_note,"funnel":funnel,"retn":retn,"drill":drill}
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
  adeff=[{"name":"google.ads","cost":dser(41,1400000,0.6,n),"install":dser(53,120,0.7,n),"su":dser(42,48,0.9,n),"call":dser(43,60,0.8,n)},
         {"name":"insta","cost":dser(44,1100000,0.6,n),"install":dser(54,40,0.8,n),"su":dser(45,1,1.5,n),"call":dser(46,50,0.7,n)},
         {"name":"adison","cost":dser(47,300000,0.8,n),"install":dser(55,70,0.9,n),"su":dser(48,13,1.1,n),"call":dser(49,5,1.3,n)},
         {"name":"naver_bsa","cost":dser(50,200000,0.9,n),"install":dser(56,30,1.0,n),"su":dser(51,5,1.2,n),"call":dser(52,8,1.2,n)}]
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
    rows.append({"bucket":d[0],"ch":d[1],"camp":d[2],"ag":d[3],"cr":("소재A" if d[1] in ("google","insta") else ""),"seg":d[4],"s":{"call":call,"comp":comp,"su":suu,"cost":cost}})
  # 더미: 소재 토글 미리보기용으로 insta 택시 행을 소재A/소재B로 분할
  extra=[]
  for r in rows:
    if r["bucket"]=="taxi" and r["ch"]=="insta":
      half=lambda arr:[round(x*0.45) for x in arr]
      b={**r,"cr":"소재B","s":{k:half(r["s"][k]) for k in r["s"]}}
      r["cr"]="소재A"; extra.append(b)
  rows+=extra
  for r in rows:
    if r["ch"] in ("google","insta","etc") and not is_purpose(r["bucket"], r["camp"]): r["ch"]="xads"
  totals={k:[sum(c[k][i] for c in channels) for i in range(n)] for k in ("su","card","lounge")}
  btot={"taxi":{"call":taxitot},
        "rvtaxi":{"call":dser(60,380,0.4,n),"comp":dser(61,210,0.4,n)},
        "pm":{"call":dser(62,70,0.5,n),"comp":dser(63,45,0.5,n)},
        "lounge":{"call":dser(64,70000,0.3,n)}}
  totals["install"]=dser(200,5200,0.4,n); totals["visit"]=dser(205,72000,0.3,n)
  funnel={"window":"2026-08-26 ~ 2026-09-22","rows":[["전체",165000,29400,12800],["google.ads",8200,2540,1180],["insta",2600,700,300],["기타광고",5400,1180,520],["비광고",148000,24400,10700]]}
  retn={"window":"2026-07-30 ~ 2026-08-26","cohort":158000,"points":[["D1",0.182],["D7",0.094],["D14",0.067],["D30",0.041]]}
  # 더미 드릴다운: adset 채널의 (캠페인·애드그룹·소재)별 5개 지표 일자별
  dd=[("google.ads","지방권_강원_tROAS","강원_1천원","배너A"),("google.ads","지방권_강원_tROAS","강원_1천원","배너B"),
      ("google.ads","티머니GO_신규가입_상시","신규_상시","리스폰시브"),("google.ads","부산택시_tROAS","부산_1천원",""),
      ("insta","지방택시 활성화_트래픽","강원+1854MF","강원 통합"),("insta","지방택시 활성화_트래픽","대전+1854MF","대전 통합"),
      ("insta","예약택시_트래픽","2049MF + 여행","예약택시"),("adison","ridingforever","",""),("naver_bsa","메인","","썸네일1")]
  drill_rows=[]
  for i,(ch,cp,ag,cr) in enumerate(dd):
    rr=mb(i*97+5)
    mk=lambda b:[max(0,round(b*(0.5+rr()*1.0))) for _ in range(n)]
    v={"install":mk(18+i*3),"visit":mk(40+i*5),"su":mk(10+i*2),"card":mk(5+i),"lounge":mk(3+i),"call":mk(14+i*2)}
    drill_rows.append({"ch":ch,"camp":cp,"ag":(ag or "(애드그룹 없음)"),"cr":(cr or "(소재 없음)"),"v":v})
  drill={"metrics":[["install","앱설치"],["visit","앱 방문"],["su","회원가입"],["card","교통카드 등록"],["lounge","대중교통라운지 방문"],["call","택시호출"]],"rows":drill_rows}
  return {"generated":"더미 샘플","days":days,"buckets":BUCKETS,"channels":channels,"taxitot":taxitot,"totals":totals,
          "adeff":adeff,"onb":onb,"rows":rows,"camp":camp,"btot":btot,"onbNote":False,"funnel":funnel,"retn":retn,"drill":drill}

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
TEMPLATE_HTML=_b64.b64decode("PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9ImtvIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLCB2aWV3cG9ydC1maXQ9Y292ZXIiPgo8dGl0bGU+7Yuw66i464uIR08g6rSR6rOgIOyEseqzvCDthrXtlanrt7A8L3RpdGxlPgo8c3R5bGU+CiAgOnJvb3R7LS1iZzojZjRmNWY3Oy0tY2FyZDojZmZmOy0tbGluZTojZWFlN2YzOy0taW5rOiMyYTI2NDA7LS1zdWI6IzhiODZhMDstLWFjY2VudDojODM3MGU2Oy0tYWNjZW50LXNvZnQ6I2YxZWRmYzstLWdvb2Q6IzJlOWU1YjstLWJhZDojYzA0NTNiOy0tbXV0ZWQ6I2JkYjhjYzstLW9yZzojZDZkMmUyOy0tc2hhZG93OjAgMXB4IDJweCByZ2JhKDQyLDM4LDY0LC4wNSksMCAycHggOHB4IHJnYmEoNDIsMzgsNjQsLjAzNSk7Ym94LXNpemluZzpib3JkZXItYm94O3BhZGRpbmctdG9wOmVudihzYWZlLWFyZWEtaW5zZXQtdG9wLDBweCk7cGFkZGluZy1ib3R0b206ZW52KHNhZmUtYXJlYS1pbnNldC1ib3R0b20sMHB4KX0KICBAbWVkaWEgKHByZWZlcnMtY29sb3Itc2NoZW1lOmRhcmspezpyb290Om5vdChbZGF0YS10aGVtZT0ibGlnaHQiXSl7LS1iZzojMTUxMjFkOy0tY2FyZDojMWUxYTJhOy0tbGluZTojMmYyOTQxOy0taW5rOiNlY2U5ZjU7LS1zdWI6Izk0OGVhYTstLWFjY2VudDojYTU5M2YwOy0tYWNjZW50LXNvZnQ6IzI3MWQzZDstLW11dGVkOiM1ZjU4NzU7LS1vcmc6IzQxM2E1NDstLXNoYWRvdzowIDFweCAycHggcmdiYSgwLDAsMCwuNCksMCAycHggMTBweCByZ2JhKDAsMCwwLC4zKX19CiAgKntib3gtc2l6aW5nOmJvcmRlci1ib3g7bWFyZ2luOjA7cGFkZGluZzowfQogIGJvZHl7YmFja2dyb3VuZDp2YXIoLS1iZyk7Y29sb3I6dmFyKC0taW5rKTtmb250LWZhbWlseToiQXBwbGUgU0QgR290aGljIE5lbyIsIk1hbGd1biBHb3RoaWMiLCJQcmV0ZW5kYXJkIixzeXN0ZW0tdWksc2Fucy1zZXJpZjtmb250LXNpemU6MTRweDtsaW5lLWhlaWdodDoxLjU7cGFkZGluZzoxNnB4fQogIC53cmFwe21heC13aWR0aDoxMTIwcHg7bWFyZ2luOjAgYXV0b30KICBoMXtmb250LXNpemU6MjBweDtmb250LXdlaWdodDo4MDA7bGV0dGVyLXNwYWNpbmc6LTAuNHB4fQogIC5oZHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2ZsZXgtd3JhcDp3cmFwO2dhcDo4cHg7cGFkZGluZy1ib3R0b206MTNweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCB2YXIoLS1saW5lKX0KICAuaGQtdGl0bGV7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OXB4fQogIC5oZCAuZG90e3dpZHRoOjlweDtoZWlnaHQ6OXB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6dmFyKC0tYWNjZW50KTtib3gtc2hhZG93OjAgMCAwIDRweCB2YXIoLS1hY2NlbnQtc29mdCl9CiAgLmhkLW1ldGF7Zm9udC1zaXplOjExLjVweDtjb2xvcjp2YXIoLS1tdXRlZCk7Zm9udC13ZWlnaHQ6NjAwfQogIC5iYW5uZXJ7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQtc29mdCk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKTtib3JkZXItcmFkaXVzOjEycHg7cGFkZGluZzoxMnB4IDE0cHg7Y29sb3I6dmFyKC0tYWNjZW50KTtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjEzcHg7bWFyZ2luLWJvdHRvbToxMnB4fQogIC5jb250cm9sc3tkaXNwbGF5OmZsZXg7ZmxleC13cmFwOndyYXA7Z2FwOjEwcHg7YWxpZ24taXRlbXM6Y2VudGVyO21hcmdpbjoxNHB4IDB9CiAgLnNlZ3tkaXNwbGF5OmZsZXg7Z2FwOjNweDtiYWNrZ3JvdW5kOnZhcigtLWNhcmQpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czo5OTlweDtwYWRkaW5nOjRweH0KICAuc2VnIGJ1dHRvbntib3JkZXI6bm9uZTtiYWNrZ3JvdW5kOm5vbmU7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTNweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tc3ViKTtwYWRkaW5nOjdweCAxNHB4O2JvcmRlci1yYWRpdXM6OTk5cHg7Y3Vyc29yOnBvaW50ZXJ9CiAgLnNlZyBidXR0b24ub257YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQpO2NvbG9yOiNmZmZ9CiAgLnJhbmdlbGFiZWx7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tc3ViKX0ucmFuZ2VsYWJlbCBie2NvbG9yOnZhcigtLWluayl9CiAgLmRhdGVwaWNre2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjZweDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tc3ViKX0KICAuZGF0ZXBpY2sgaW5wdXR7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTIuNXB4O2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjp2YXIoLS1pbmspO2JhY2tncm91bmQ6dmFyKC0tY2FyZCk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKTtib3JkZXItcmFkaXVzOjdweDtwYWRkaW5nOjZweCA4cHg7Y3Vyc29yOnBvaW50ZXJ9CiAgLmRhdGVwaWNrIGlucHV0Ojotd2Via2l0LWNhbGVuZGFyLXBpY2tlci1pbmRpY2F0b3J7Y3Vyc29yOnBvaW50ZXI7b3BhY2l0eTouNn0KICAudGFic3tkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2dhcDo5cHg7bWFyZ2luLWJvdHRvbToxNHB4fQogIC50YWJ7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzo3cHggMTZweDtib3JkZXItcmFkaXVzOjk5OXB4O2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTNweDtjb2xvcjp2YXIoLS1zdWIpO2N1cnNvcjpwb2ludGVyO3doaXRlLXNwYWNlOm5vd3JhcDtib3JkZXI6bm9uZTtiYWNrZ3JvdW5kOm5vbmU7Zm9udC1mYW1pbHk6aW5oZXJpdH0KICAudGFiLm9ue2JhY2tncm91bmQ6dmFyKC0tYWNjZW50KTtjb2xvcjojZmZmfQogIC5ub3RlbGluZXtmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS1zdWIpO21hcmdpbjoycHggMCAxMHB4fQogIC5rcGlze2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KGF1dG8tZml0LG1pbm1heCgxNTBweCwxZnIpKTtnYXA6MTJweDttYXJnaW4tYm90dG9tOjE2cHh9CiAgLmtwaXtiYWNrZ3JvdW5kOnZhcigtLWNhcmQpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxNHB4O3BhZGRpbmc6MTRweCAxNnB4O2JveC1zaGFkb3c6dmFyKC0tc2hhZG93KX0KICAua3BpLnByaW1hcnl7Ym9yZGVyLWNvbG9yOnZhcigtLWFjY2VudCk7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQtc29mdCl9CiAgLmtwaSAudHtmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS1zdWIpO2ZvbnQtd2VpZ2h0OjYwMH0KICAua3BpIC5iaWd7Zm9udC1zaXplOjI0cHg7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi10b3A6NnB4O2xldHRlci1zcGFjaW5nOi0wLjZweH0KICAua3BpIC5iaWcgLnV7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tc3ViKTtmb250LXdlaWdodDo2MDA7bWFyZ2luLWxlZnQ6MnB4fQogIC5rcGkgLmRlbHRhe2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMDttYXJnaW4tdG9wOjRweH0KICAudXB7Y29sb3I6dmFyKC0tZ29vZCl9LmRvd257Y29sb3I6dmFyKC0tYmFkKX0uZmxhdHtjb2xvcjp2YXIoLS1tdXRlZCl9CiAgLmNhcmR7YmFja2dyb3VuZDp2YXIoLS1jYXJkKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTRweDtwYWRkaW5nOjE3cHggMThweDttYXJnaW4tYm90dG9tOjEycHg7Ym94LXNoYWRvdzp2YXIoLS1zaGFkb3cpfQogIC5jYXJkIGgze2ZvbnQtc2l6ZToxNC41cHg7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi1ib3R0b206M3B4O2xldHRlci1zcGFjaW5nOi0wLjJweH0KICAuY2FyZCBoMyAubWluaXtjb2xvcjp2YXIoLS1zdWIpO2ZvbnQtd2VpZ2h0OjYwMDtmb250LXNpemU6MTJweH0KICAuY2FyZCAuY3N1Yntmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS1zdWIpO21hcmdpbi1ib3R0b206MTJweH0KICAuc3BsaXRiYXJ7ZGlzcGxheTpmbGV4O2hlaWdodDoyNnB4O2JvcmRlci1yYWRpdXM6N3B4O292ZXJmbG93OmhpZGRlbjttYXJnaW4tYm90dG9tOjEwcHg7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKX0KICAuc3BsaXRiYXIgLmFke2JhY2tncm91bmQ6dmFyKC0tYWNjZW50KX0uc3BsaXRiYXIgLm9yZ3tiYWNrZ3JvdW5kOnZhcigtLW9yZyl9CiAgLnNwbGl0YmFyIHNwYW57ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2NvbG9yOiNmZmY7Zm9udC1zaXplOjExcHg7Zm9udC13ZWlnaHQ6ODAwO21pbi13aWR0aDowfQogIC5zcGxpdGJhciAub3Jne2NvbG9yOnZhcigtLWluayl9CiAgLmNocm93e2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MTUwcHggMWZyIDkycHg7Z2FwOjEwcHg7YWxpZ24taXRlbXM6Y2VudGVyO3BhZGRpbmc6NXB4IDA7Zm9udC1zaXplOjEyLjVweH0KICAuY2hyb3cuZXhwcm93e2N1cnNvcjpwb2ludGVyfQogIC5jaHJvdyAubm17ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NXB4fQogIC5jaHJvdyAuYWR0YWd7Zm9udC1zaXplOjkuNXB4O2ZvbnQtd2VpZ2h0OjgwMDtwYWRkaW5nOjFweCA1cHg7Ym9yZGVyLXJhZGl1czo0cHg7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQtc29mdCk7Y29sb3I6dmFyKC0tYWNjZW50KX0KICAuYXJyb3d7ZGlzcGxheTppbmxpbmUtYmxvY2s7d2lkdGg6MTFweDtmb250LXNpemU6OXB4O2NvbG9yOnZhcigtLXN1Yik7dHJhbnNpdGlvbjp0cmFuc2Zvcm0gLjE1c30KICAudHJhY2t7aGVpZ2h0OjhweDtiYWNrZ3JvdW5kOnZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6NXB4O292ZXJmbG93OmhpZGRlbn0KICAudHJhY2s+c3BhbntkaXNwbGF5OmJsb2NrO2hlaWdodDoxMDAlO2JvcmRlci1yYWRpdXM6NXB4O2JhY2tncm91bmQ6dmFyKC0tb3JnKX0KICAudHJhY2s+c3Bhbi5hZGZ7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQpfQogIC5jaHJvdyAudmFse3RleHQtYWxpZ246cmlnaHQ7Y29sb3I6dmFyKC0tc3ViKX0uY2hyb3cgLnZhbCBie2NvbG9yOnZhcigtLWluayl9CiAgLmNoc3Vie3BhZGRpbmc6MnB4IDAgOHB4IDIwcHg7bWFyZ2luOjAgMCA0cHggOHB4O2JvcmRlci1sZWZ0OjJweCBzb2xpZCB2YXIoLS1hY2NlbnQtc29mdCl9CiAgLnN1YnJvd3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciBhdXRvO2dhcDoxMHB4O2ZvbnQtc2l6ZToxMS41cHg7Y29sb3I6dmFyKC0tc3ViKTtwYWRkaW5nOjNweCAwfQogIC5zdWJyb3cgLm5tMntjb2xvcjp2YXIoLS1pbmspfQogIC5lZmZzdWJ7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDoxMHB4O2ZvbnQtc2l6ZToxMS41cHg7Y29sb3I6dmFyKC0tc3ViKTtwYWRkaW5nOjRweCAwO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHZhcigtLWFjY2VudC1zb2Z0KX0KICAuZWZmc3ViIC5ubTJ7Y29sb3I6dmFyKC0taW5rKTtmb250LXdlaWdodDo2MDB9CiAgLmNvbnZncmlke2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KGF1dG8tZml0LG1pbm1heCgxMzBweCwxZnIpKTtnYXA6MTBweH0KICAuY29udntib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6OXB4O3BhZGRpbmc6MTFweCAxMnB4fQogIC5jb252IC50e2ZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLXN1Yik7Zm9udC13ZWlnaHQ6NjAwfQogIC5jb252IC5iaWd7Zm9udC1zaXplOjIwcHg7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi10b3A6NHB4fQogIC5jb252IC5iaWcgLnV7Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tc3ViKTttYXJnaW4tbGVmdDoxcHh9CiAgLmNvbnYgLnN1YjJ7Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tc3ViKTttYXJnaW4tdG9wOjFweH0KICAuc2Nyb2xse292ZXJmbG93LXg6YXV0b30KICB0YWJsZXt3aWR0aDoxMDAlO2JvcmRlci1jb2xsYXBzZTpjb2xsYXBzZTtmb250LXNpemU6MTJweDttaW4td2lkdGg6NjgwcHh9CiAgdGgsdGR7cGFkZGluZzo5cHggMTBweDt0ZXh0LWFsaWduOnJpZ2h0O2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHZhcigtLWxpbmUpO3doaXRlLXNwYWNlOm5vd3JhcH0KICB0aDpmaXJzdC1jaGlsZCx0ZDpmaXJzdC1jaGlsZHt0ZXh0LWFsaWduOmxlZnR9CiAgdGhlYWQgdGh7Y29sb3I6dmFyKC0tc3ViKTtmb250LXdlaWdodDo3MDB9CiAgdGgubGgsdGQubGh7dGV4dC1hbGlnbjpsZWZ0fQogIHRkLm5hbWV7bWF4LXdpZHRoOjI2MHB4O3doaXRlLXNwYWNlOm5vcm1hbDt3b3JkLWJyZWFrOmJyZWFrLXdvcmQ7bGluZS1oZWlnaHQ6MS4zNTt0ZXh0LWFsaWduOmxlZnR9CiAgdHIuZXhwcm93e2N1cnNvcjpwb2ludGVyfQogIC5rcGljb2x7Zm9udC13ZWlnaHQ6ODAwO2JhY2tncm91bmQ6cmdiYSgxMzEsMTEyLDIzMCwwLjA2KX10aC5rcGljb2x7Y29sb3I6dmFyKC0tc3ViKTtiYWNrZ3JvdW5kOnJnYmEoMTMxLDExMiwyMzAsMC4wNil9CiAgdGQuZGlte2NvbG9yOnZhcigtLW11dGVkKX0KICAuZGVsdGEtc3tmb250LXNpemU6MTAuNXB4O2ZvbnQtd2VpZ2h0OjcwMDttYXJnaW4tbGVmdDo1cHh9CiAgLmJhZGdle2Rpc3BsYXk6aW5saW5lLWJsb2NrO2ZvbnQtc2l6ZToxMC41cHg7Zm9udC13ZWlnaHQ6NzAwO3BhZGRpbmc6MXB4IDdweDtib3JkZXItcmFkaXVzOjVweH0KICAuYmFkZ2UucmV7YmFja2dyb3VuZDojZWZlYWZjO2NvbG9yOiM3YTYzZDZ9LmJhZGdlLnRye2JhY2tncm91bmQ6I2VlZjBmMztjb2xvcjojN2E4NTkzfQogIEBtZWRpYSAocHJlZmVycy1jb2xvci1zY2hlbWU6ZGFyayl7OnJvb3Q6bm90KFtkYXRhLXRoZW1lPSJsaWdodCJdKSAuYmFkZ2UucmV7YmFja2dyb3VuZDojMjcxZDNkO2NvbG9yOiNhOTkxZjV9OnJvb3Q6bm90KFtkYXRhLXRoZW1lPSJsaWdodCJdKSAuYmFkZ2UudHJ7YmFja2dyb3VuZDojMjQyYjM0fX0KICAuZGV0YWlsdGR7YmFja2dyb3VuZDp2YXIoLS1iZyk7cGFkZGluZzowfS5kZXRhaWx0ZCAuaW5uZXJ7cGFkZGluZzo0cHggMTBweCAxMHB4IDI2cHh9CiAgLnRvdHJvdyB0ZHtib3JkZXItdG9wOjJweCBzb2xpZCB2YXIoLS1saW5lKTtmb250LXdlaWdodDo4MDB9CiAgLnNlY2hkLXJvdyB0ZHtiYWNrZ3JvdW5kOnZhcigtLWFjY2VudC1zb2Z0KTtjb2xvcjp2YXIoLS1hY2NlbnQpO2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTIuNXB4O3RleHQtYWxpZ246bGVmdCFpbXBvcnRhbnQ7cGFkZGluZzo5cHggOHB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHZhcigtLWxpbmUpfQogIC5zZWdoZHtmb250LXNpemU6MTMuNXB4O2ZvbnQtd2VpZ2h0OjgwMDttYXJnaW46MTZweCAwIDhweH0uc2VnaGQgLnNlZ3N1Yntmb250LXNpemU6MTEuNXB4O2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjp2YXIoLS1zdWIpO21hcmdpbi1sZWZ0OjdweH0KICAuZm9vdHtmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS1tdXRlZCk7bWFyZ2luLXRvcDoxNnB4O2xpbmUtaGVpZ2h0OjEuNn0KPC9zdHlsZT4KPC9oZWFkPgo8Ym9keT4KPGRpdiBjbGFzcz0id3JhcCI+CiAgPGhlYWRlciBjbGFzcz0iaGQiPjxkaXYgY2xhc3M9ImhkLXRpdGxlIj48c3BhbiBjbGFzcz0iZG90Ij48L3NwYW4+PGgxPu2LsOuouOuLiEdPIOq0keqzoCDshLHqs7wg7Ya17ZWp67ewPC9oMT48L2Rpdj48ZGl2IGNsYXNzPSJoZC1tZXRhIiBpZD0iZ2VubWV0YSI+PC9kaXY+PC9oZWFkZXI+CiAgPGRpdiBjbGFzcz0iY29udHJvbHMiPgogICAgPGRpdiBjbGFzcz0iZGF0ZXBpY2siPuq4sOqwhCA8aW5wdXQgdHlwZT0iZGF0ZSIgaWQ9ImZyb21EYXRlIj4gfiA8aW5wdXQgdHlwZT0iZGF0ZSIgaWQ9InRvRGF0ZSI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzZWciIGlkPSJxdWlja3BpY2siPjxidXR0b24gZGF0YS1kPSIxIj4x7J28PC9idXR0b24+PGJ1dHRvbiBkYXRhLWQ9IjciPjfsnbw8L2J1dHRvbj48YnV0dG9uIGRhdGEtZD0iMTQiPjE07J28PC9idXR0b24+PGJ1dHRvbiBkYXRhLWQ9IjMwIj4zMOydvDwvYnV0dG9uPjxidXR0b24gZGF0YS1kPSI2MCI+NjDsnbw8L2J1dHRvbj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InJhbmdlbGFiZWwiIGlkPSJyYW5nZWxhYmVsIj48L2Rpdj4KICA8L2Rpdj4KICA8ZGl2IGNsYXNzPSJ0YWJzIiBpZD0idGFicyI+PC9kaXY+CiAgPGRpdiBpZD0idmlldyI+PC9kaXY+CiAgPGRpdiBjbGFzcz0iZm9vdCIgaWQ9ImZvb3QiPjwvZGl2Pgo8L2Rpdj4KPHNjcmlwdD4KLyogPT09PT0g642w7J207YSwIOyjvOyehSAoZ2VuZXJhdGUucHnqsIAgX19EQVRBX18g66W8IOyLpOy4oSBKU09O7Jy866GcIOq1kOyytCkgPT09PT0gKi8KY29uc3QgREFUQSA9IF9fREFUQV9fOwpjb25zdCBCVUNLRVRTID0gREFUQS5idWNrZXRzOwpjb25zdCBEQVlTID0gREFUQS5kYXlzOwpjb25zdCBDSCA9IERBVEEuY2hhbm5lbHM7CmNvbnN0IFRPVEFMUyA9IERBVEEudG90YWxzOwpjb25zdCBUQVhJVE9UID0gREFUQS50YXhpdG90Owpjb25zdCBDQU1QID0gREFUQS5jYW1wOwpjb25zdCBPTkIgPSBEQVRBLm9uYjsKY29uc3QgQURFRkYgPSBEQVRBLmFkZWZmOwpjb25zdCBST1dTID0gREFUQS5yb3dzOwpjb25zdCBCVE9UID0gREFUQS5idG90fHx7fTsKY29uc3QgQ1JTSE9XID0ge307CmNvbnN0IEZVTk5FTCA9IERBVEEuZnVubmVsfHxudWxsOwpjb25zdCBEUklMTCA9IERBVEEuZHJpbGx8fG51bGw7CmNvbnN0IFJFVE4gPSBEQVRBLnJldG58fG51bGw7CmxldCBNT0RFPSJhZHMiOyBsZXQgRFJJTExNRVRSSUM9InN1IjsgY29uc3QgREVYUD1uZXcgU2V0KCk7IGxldCBCVFJLPSJib3RoIjsKCi8qID09PT09IOqzoOyglSDroZzsp4EgPT09PT0gKi8KY29uc3QgZj1uPT4obj09bnVsbCk/IuKAlCI6TWF0aC5yb3VuZChuKS50b0xvY2FsZVN0cmluZygna28tS1InKTsKbGV0IFJBPU1hdGgubWF4KDAsREFZUy5sZW5ndGgtNyksIFJCPURBWVMubGVuZ3RoLTE7ICAgLy8g7ISg7YOdIOq4sOqwhCjsnbwg7J24642x7IqkKQpmdW5jdGlvbiB3aW5kb3dzKCl7Y29uc3QgY3VyPVtdO2ZvcihsZXQgaT1SQTtpPD1SQjtpKyspY3VyLnB1c2goaSk7Y29uc3Qgbj1jdXIubGVuZ3RoO2NvbnN0IHNoPU1hdGguY2VpbChuLzcpKjc7Y29uc3QgcHJpb3I9Y3VyLm1hcChpPT5pLXNoKTtyZXR1cm4ge2N1cixwcmlvcjpwcmlvci5ldmVyeShpPT5pPj0wKT9wcmlvcjpbXX07fQpjb25zdCBzdW09KGFycixpZHgpPT5pZHgucmVkdWNlKChhLGkpPT5hKyhhcnJbaV18fDApLDApOwpjb25zdCBwY3Q9KGMscCk9PihwPT1udWxsfHxwPT09MCk/bnVsbDooYy1wKS9wKjEwMDsKZnVuY3Rpb24gZGVsdGFTcGFuKGMscCxnb29kVXA9dHJ1ZSxzbWFsbD1mYWxzZSl7Y29uc3Qgdj1wY3QoYyxwKTtpZih2PT1udWxsKXJldHVybiAnJztjb25zdCBjbHM9KHY9PT0wKT8nZmxhdCc6KCh2PjA9PT1nb29kVXApPyd1cCc6J2Rvd24nKSxhcj12PjA/J+KWsic6KHY8MD8n4pa8JzonJyk7cmV0dXJuIGA8c3BhbiBjbGFzcz0iJHtjbHN9ICR7c21hbGw/J2RlbHRhLXMnOidkZWx0YSd9Ij4ke2FyfSR7TWF0aC5hYnModikudG9GaXhlZCgwKX0lPC9zcGFuPmA7fQpmdW5jdGlvbiBrcGkobGFiZWwsY3VyLHByaW9yLG89e30pe3JldHVybiBgPGRpdiBjbGFzcz0ia3BpICR7by5wcmltYXJ5PydwcmltYXJ5JzonJ30iPjxkaXYgY2xhc3M9InQiPiR7bGFiZWx9PC9kaXY+PGRpdiBjbGFzcz0iYmlnIj4ke28ud29uPyfigqknOicnfSR7ZihjdXIpfSR7by51bml0P2A8c3BhbiBjbGFzcz0idSI+JHtvLnVuaXR9PC9zcGFuPmA6Jyd9PC9kaXY+JHtvLm5vZGVsdGE/Jyc6ZGVsdGFTcGFuKGN1cixwcmlvcixvLmdvb2RVcCE9PWZhbHNlKX0ke28uc3VifHwnJ308L2Rpdj5gO30KZnVuY3Rpb24gY2hUb3RhbChrZXksaWR4KXtyZXR1cm4gQ0gucmVkdWNlKChhLGMpPT5hK3N1bShjW2tleV0saWR4KSwwKTt9Ci8qIC0tLS0g7J287J6Q67OEIOy2lOydtCDrnbzsnbjssKjtirggKOuCtOyepSBTVkcsIDLsnbwg7J207IOB7J28IOuVjCkgLS0tLSAqLwpsZXQgVFJFTkRLRVk9InN1IjsKZnVuY3Rpb24gd2QoZCxzaG93Vyl7Y29uc3Qgcz1kLnNsaWNlKDUpOyBpZighc2hvd1cpIHJldHVybiBzOyBjb25zdCBkdD1uZXcgRGF0ZShkKydUMDA6MDA6MDAnKTsgcmV0dXJuIHMrJygnKyLsnbzsm5TtmZTsiJjrqqnquIjthqAiW2R0LmdldERheSgpXSsnKSc7fQpmdW5jdGlvbiB0cmVuZENoYXJ0KGRheXMsIHNlcmllcyl7CiAgY29uc3Qgbj1kYXlzLmxlbmd0aCwgUz1zZXJpZXMubGVuZ3RoLCBQTD00MixQUj0xMixQVD0yNCxQQj0zMiwgSD0xODQ7CiAgY29uc3QgZmM9dj0+e3Y9TWF0aC5yb3VuZCh2KTsgaWYodj49MTAwMDApcmV0dXJuICh2LzEwMDAwKS50b0ZpeGVkKHY+PTEwMDAwMD8wOjEpLnJlcGxhY2UoL1wuMCQvLCcnKSsn66eMJzsgaWYodj49MTAwMClyZXR1cm4gKHYvMTAwMCkudG9GaXhlZCgxKS5yZXBsYWNlKC9cLjAkLywnJykrJ+yynCc7IHJldHVybiB2LnRvTG9jYWxlU3RyaW5nKCdrby1LUicpO307CiAgY29uc3QgVz1NYXRoLm1heCg4NjAsIFBMK1BSK24qKFM+MT9TKjE1KzE4OjM0KSk7CiAgY29uc3QgbXg9TWF0aC5tYXgoMSwuLi5zZXJpZXMuZmxhdE1hcChzPT5zLnZhbHMpKSoxLjI4OwogIGNvbnN0IHBsb3RXPVctUEwtUFIsIHBsb3RIPUgtUFQtUEIsIGd3PXBsb3RXL247CiAgY29uc3QgYnc9TWF0aC5tYXgoMyxNYXRoLm1pbihTPjE/MjQ6Z3cqMC41MiwoZ3ctNikvUykpOwogIGNvbnN0IFk9dj0+UFQrcGxvdEgqKDEtdi9teCk7CiAgY29uc3QgbGJsU3RlcD1NYXRoLm1heCgxLE1hdGguY2VpbChuLzcpKTsKICBsZXQgZ3JpZD0nJzsgY29uc3QgRz00OwogIGZvcihsZXQgaT0wO2k8PUc7aSsrKXtjb25zdCBneT1QVCtwbG90SCppL0csIGd2PW14KigxLWkvRyk7CiAgICBncmlkKz1gPGxpbmUgeDE9IiR7UEx9IiB5MT0iJHtneS50b0ZpeGVkKDEpfSIgeDI9IiR7Vy1QUn0iIHkyPSIke2d5LnRvRml4ZWQoMSl9IiBzdHJva2U9InZhcigtLWxpbmUpIiBzdHJva2Utd2lkdGg9IjEiLz5gCiAgICAgICsoaTxHP2A8dGV4dCB4PSIke1BMLTZ9IiB5PSIkeyhneSszKS50b0ZpeGVkKDEpfSIgZm9udC1zaXplPSI4IiBmaWxsPSJ2YXIoLS1tdXRlZCkiIHRleHQtYW5jaG9yPSJlbmQiPiR7ZmMoZ3YpfTwvdGV4dD5gOicnKTt9CiAgbGV0IGJhcnM9Jyc7CiAgZm9yKGxldCBpPTA7aTxuO2krKyl7CiAgICBjb25zdCBneD1QTCtndyppKyhndy1idypTKS8yOwogICAgc2VyaWVzLmZvckVhY2goKHMsc2kpPT57CiAgICAgIGNvbnN0IHY9cy52YWxzW2ldfHwwLCB4PWd4K3NpKmJ3LCB5PVkodiksIGhoPVBUK3Bsb3RILXksIGN4PXgrYncqMC40NTsKICAgICAgYmFycys9YDxyZWN0IHg9IiR7eC50b0ZpeGVkKDEpfSIgeT0iJHt5LnRvRml4ZWQoMSl9IiB3aWR0aD0iJHsoYncqMC44NikudG9GaXhlZCgxKX0iIGhlaWdodD0iJHtNYXRoLm1heCgwLGhoKS50b0ZpeGVkKDEpfSIgcng9IjIuNSIgZmlsbD0iJHtzLmNvbG9yfSI+PHRpdGxlPiR7ZGF5c1tpXX0gJHtzLm5hbWV9OiAke2Yodil9PC90aXRsZT48L3JlY3Q+YDsKICAgICAgaWYoaSVsYmxTdGVwPT09MCYmdj4wKSBiYXJzKz1gPHRleHQgeD0iJHtjeC50b0ZpeGVkKDEpfSIgeT0iJHsoeS0zKS50b0ZpeGVkKDEpfSIgZm9udC1zaXplPSI1LjUiIGZvbnQtd2VpZ2h0PSI1MDAiIGZpbGw9InZhcigtLW11dGVkKSIgdGV4dC1hbmNob3I9Im1pZGRsZSI+JHtmYyh2KX08L3RleHQ+YDsKICAgIH0pOwogIH0KICBjb25zdCBzaG93Vz1uPD0xNCwgeHN0ZXA9TWF0aC5tYXgoMSxNYXRoLmNlaWwobi84KSk7CiAgbGV0IHhsPScnOwogIGZvcihsZXQgaT0wO2k8bjtpKz14c3RlcCl7Y29uc3QgY3g9UEwrZ3cqaStndy8yOyB4bCs9YDx0ZXh0IHg9IiR7Y3gudG9GaXhlZCgxKX0iIHk9IiR7SC0xMn0iIGZvbnQtc2l6ZT0iOSIgZmlsbD0idmFyKC0tbXV0ZWQpIiB0ZXh0LWFuY2hvcj0ibWlkZGxlIj4ke3dkKGRheXNbaV0sc2hvd1cpfTwvdGV4dD5gO30KICBjb25zdCBsZWdlbmQ9Uz4xP2A8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7Z2FwOjE0cHg7Zm9udC1zaXplOjExcHg7bWFyZ2luLXRvcDo4cHg7ZmxleC13cmFwOndyYXAiPmArc2VyaWVzLm1hcChzPT5gPHNwYW4gc3R5bGU9ImNvbG9yOnZhcigtLXN1YikiPjxzcGFuIHN0eWxlPSJkaXNwbGF5OmlubGluZS1ibG9jazt3aWR0aDoxMHB4O2hlaWdodDoxMHB4O2JvcmRlci1yYWRpdXM6M3B4O2JhY2tncm91bmQ6JHtzLmNvbG9yfTttYXJnaW4tcmlnaHQ6NXB4O3ZlcnRpY2FsLWFsaWduOm1pZGRsZSI+PC9zcGFuPiR7cy5uYW1lfTwvc3Bhbj5gKS5qb2luKCcnKStgPC9kaXY+YDonJzsKICByZXR1cm4gYDxkaXYgY2xhc3M9InNjcm9sbCI+PHN2ZyB2aWV3Qm94PSIwIDAgJHtXfSAke0h9IiB3aWR0aD0iMTAwJSIgc3R5bGU9Im1pbi13aWR0aDoke01hdGgubWluKFcsNTIwKS50b0ZpeGVkKDApfXB4O2Rpc3BsYXk6YmxvY2siPiR7Z3JpZH0ke2JhcnN9JHt4bH08L3N2Zz48L2Rpdj4ke2xlZ2VuZH1gOwp9CmZ1bmN0aW9uIHN1bW1hcnlUcmVuZChXKXsKICBpZihXLmN1ci5sZW5ndGg8MikgcmV0dXJuICcnOwogIGNvbnN0IE09e2luc3RhbGw6WyLslbHshKTsuZgiLFRPVEFMUy5pbnN0YWxsXSx2aXNpdDpbIuyVsSDrsKnrrLgiLFRPVEFMUy52aXNpdF0sc3U6WyLtmozsm5DqsIDsnoUiLFRPVEFMUy5zdV0sY2FyZDpbIuq1kO2Gtey5tOuTnCDrk7HroZ0iLFRPVEFMUy5jYXJkXSxsb3VuZ2U6WyLrjIDspJHqtZDthrXrnbzsmrTsp4Ag67Cp66y4IixUT1RBTFMubG91bmdlXSxjYWxsOlsi7YOd7Iuc7Zi47LacIixUQVhJVE9UXX07CiAgaWYoIU1bVFJFTkRLRVldKSBUUkVOREtFWT0ic3UiOyBjb25zdCBtPU1bVFJFTkRLRVldOwogIGNvbnN0IGRheXM9Vy5jdXIubWFwKGk9PkRBWVNbaV0pLCB2YWxzPVcuY3VyLm1hcChpPT5tWzFdW2ldfHwwKTsKICBjb25zdCBidG5zPWA8ZGl2IGNsYXNzPSJzZWciIHN0eWxlPSJtYXJnaW4tYm90dG9tOjEwcHg7ZmxleC13cmFwOndyYXAiPmArT2JqZWN0LmtleXMoTSkubWFwKGs9PmA8YnV0dG9uIGNsYXNzPSIke2s9PT1UUkVOREtFWT8nb24nOicnfSIgZGF0YS10cmVuZD0iJHtrfSI+JHtNW2tdWzBdfTwvYnV0dG9uPmApLmpvaW4oJycpK2A8L2Rpdj5gOwogIHJldHVybiBgPGRpdiBjbGFzcz0iY2FyZCI+PGgzPuydvOyekOuzhCDstpTsnbQgPHNwYW4gY2xhc3M9Im1pbmkiPiR7bVswXX0gKOyghOyytCDsnKDsnoUpPC9zcGFuPjwvaDM+JHtidG5zfSR7dHJlbmRDaGFydChkYXlzLFt7bmFtZTptWzBdLGNvbG9yOicjODM3MGU2Jyx2YWxzfV0pfTwvZGl2PmA7Cn0KZnVuY3Rpb24gYnVja2V0VHJlbmQoQixyb3dzLFcpewogIGlmKFcuY3VyLmxlbmd0aDwyKSByZXR1cm4gJyc7CiAgY29uc3QgZGF5cz1XLmN1ci5tYXAoaT0+REFZU1tpXSksIGJ0PUJUT1RbQi5pZF18fHtjYWxsOltdfTsKICBjb25zdCB0b3RWPVcuY3VyLm1hcChpPT4oYnQuY2FsbHx8W10pW2ldfHwwKTsKICBjb25zdCBhZFY9Vy5jdXIubWFwKGk9PnJvd3MucmVkdWNlKChhLHIpPT5hKyhyLmNoIT09Im9yZyI/KHIucy5jYWxsW2ldfHwwKTowKSwwKSk7CiAgY29uc3QgYWxsPVt7bmFtZToi7KCE7LK0Iixjb2xvcjonI2I5YjNjYycsdmFsczp0b3RWLGtleTondG90J30se25hbWU6Iuq0keqzoCjsoITssrQpIixjb2xvcjonIzgzNzBlNicsdmFsczphZFYsa2V5OidhZCd9XTsKICBjb25zdCBzZXJpZXM9QlRSSz09PSdib3RoJz9hbGw6YWxsLmZpbHRlcihzPT5zLmtleT09PUJUUkspOwogIGNvbnN0IHNlZz1gPGRpdiBjbGFzcz0ic2VnIiBzdHlsZT0iYWxpZ24tc2VsZjpmbGV4LXN0YXJ0O21hcmdpbi1ib3R0b206OHB4Ij48YnV0dG9uIGNsYXNzPSIke0JUUks9PT0nYm90aCc/J29uJzonJ30iIGRhdGEtYnRyZW5kPSJib3RoIj7rkZgg64ukPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iJHtCVFJLPT09J3RvdCc/J29uJzonJ30iIGRhdGEtYnRyZW5kPSJ0b3QiPuyghOyytDwvYnV0dG9uPjxidXR0b24gY2xhc3M9IiR7QlRSSz09PSdhZCc/J29uJzonJ30iIGRhdGEtYnRyZW5kPSJhZCI+6rSR6rOgKOyghOyytCk8L2J1dHRvbj48L2Rpdj5gOwogIHJldHVybiBgPGRpdiBjbGFzcz0iY2FyZCI+PGgzPuydvOyekOuzhCDstpTsnbQgPHNwYW4gY2xhc3M9Im1pbmkiPiR7Qi5wcmltYXJ5TGFiZWx9PC9zcGFuPjwvaDM+JHtzZWd9JHt0cmVuZENoYXJ0KGRheXMsc2VyaWVzKX08L2Rpdj5gOwp9CgovKiAtLS0tIOyalOyVvSAtLS0tICovCmZ1bmN0aW9uIGNoQmxvY2sodGl0bGUsa2V5LFcpewogIGNvbnN0IHJvd3M9Q0gubWFwKGM9Pih7bmFtZTpjLm5hbWUsYWQ6Yy5hZCxjdXI6c3VtKGNba2V5XSxXLmN1cil9KSkuc29ydCgoYSxiKT0+Yi5jdXItYS5jdXIpOwogIGNvbnN0IHRvdGFsPXJvd3MucmVkdWNlKChhLHIpPT5hK3IuY3VyLDApfHwxOwogIGNvbnN0IGFkUGN0PXJvd3MuZmlsdGVyKHI9PnIuYWQpLnJlZHVjZSgoYSxyKT0+YStyLmN1ciwwKS90b3RhbCoxMDAsIG9yZ1BjdD0xMDAtYWRQY3Q7CiAgY29uc3QgYmFyPWA8ZGl2IGNsYXNzPSJzcGxpdGJhciI+PHNwYW4gY2xhc3M9ImFkIiBzdHlsZT0id2lkdGg6JHthZFBjdH0lIj4ke2FkUGN0Pj0xMj8n6rSR6rOgICcrYWRQY3QudG9GaXhlZCgwKSsnJSc6Jyd9PC9zcGFuPjxzcGFuIGNsYXNzPSJvcmciIHN0eWxlPSJ3aWR0aDoke29yZ1BjdH0lIj4ke29yZ1BjdD49MTI/J+u5hOq0keqzoCAnK29yZ1BjdC50b0ZpeGVkKDApKyclJzonJ308L3NwYW4+PC9kaXY+YDsKICBjb25zdCBsaXN0PXJvd3MubWFwKHI9Pntjb25zdCB3PXIuY3VyL3RvdGFsKjEwMDsKICAgIGlmKHIuYWQpewogICAgICByZXR1cm4gYDxkaXYgY2xhc3M9ImNocm93Ij48ZGl2IGNsYXNzPSJubSI+JHtyLm5hbWV9PHNwYW4gY2xhc3M9ImFkdGFnIj7qtJHqs6A8L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0idHJhY2siPjxzcGFuIGNsYXNzPSJhZGYiIHN0eWxlPSJ3aWR0aDoke3d9JSI+PC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9InZhbCI+PGI+JHtmKHIuY3VyKX08L2I+IC8gJHt3LnRvRml4ZWQoMCl9JTwvZGl2PjwvZGl2PmA7fQogICAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJjaHJvdyI+PGRpdiBjbGFzcz0ibm0iPiR7ci5uYW1lfTwvZGl2PjxkaXYgY2xhc3M9InRyYWNrIj48c3BhbiBzdHlsZT0id2lkdGg6JHt3fSUiPjwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJ2YWwiPjxiPiR7ZihyLmN1cil9PC9iPiAvICR7dy50b0ZpeGVkKDApfSU8L2Rpdj48L2Rpdj5gOwogIH0pLmpvaW4oJycpOwogIHJldHVybiBgPGRpdiBjbGFzcz0iY2FyZCI+PGgzPiR7dGl0bGV9IDxzcGFuIGNsYXNzPSJtaW5pIj7stJ0gJHtmKHRvdGFsKX0gKOyxhOuEkCDtlansgrAgLyDspJHrs7Ug7Y+s7ZWoKTwvc3Bhbj48L2gzPiR7YmFyfSR7bGlzdH08L2Rpdj5gOwp9CmZ1bmN0aW9uIG9uYkJsb2NrKFcpewogIGNvbnN0IHN1PXN1bShUT1RBTFMuc3UsVy5jdXIpLCBzdVA9Vy5wcmlvci5sZW5ndGg/c3VtKFRPVEFMUy5zdSxXLnByaW9yKTpudWxsOwogIGNvbnN0IGFueURhdGE9T05CLnJlZHVjZSgoYSxvKT0+YStzdW0ob1sxXSxXLmN1ciksMCk+MDsKICBpZighYW55RGF0YSkgcmV0dXJuIGA8ZGl2IGNsYXNzPSJjYXJkIj48aDM+7Jio67O065SpIOyghO2ZmOycqCA8c3BhbiBjbGFzcz0ibWluaSI+7KCE7LK0IOqwgOyeheyekCAvIOqwgOyehSDri7nsnbw8L3NwYW4+PC9oMz48ZGl2IGNsYXNzPSJjc3ViIiBzdHlsZT0ibWFyZ2luOjAiPuumrO2FkOyFmCDrjbDsnbTthLAg7Jew64+ZIOykgOu5hCDspJEg4oCUIOqwgOyehSDri7nsnbwg7ZW17Ius7ZaJ64+ZIOyghO2ZmOycqOydtCDqs6cg7ZGc7Iuc65Cp64uI64ukLjwvZGl2PjwvZGl2PmA7CiAgY29uc3QgaXRlbXM9T05CLm1hcChvPT57Y29uc3QgYz1zdW0ob1sxXSxXLmN1cikscD1XLnByaW9yLmxlbmd0aD9zdW0ob1sxXSxXLnByaW9yKTpudWxsO2NvbnN0IHJhdGU9c3U/Yy9zdSoxMDA6MCxyYXRlUD0oc3VQJiZwIT1udWxsKT9wL3N1UCoxMDA6bnVsbDtyZXR1cm4gYDxkaXYgY2xhc3M9ImNvbnYiPjxkaXYgY2xhc3M9InQiPiR7b1swXX08L2Rpdj48ZGl2IGNsYXNzPSJiaWciPiR7cmF0ZS50b0ZpeGVkKDEpfTxzcGFuIGNsYXNzPSJ1Ij4lPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9InN1YjIiPiR7ZihjKX3rqoUgLyDqsIDsnoUgJHtmKHN1KX3rqoUgJHtkZWx0YVNwYW4ocmF0ZSxyYXRlUCx0cnVlLHRydWUpfTwvZGl2PjwvZGl2PmA7fSkuam9pbignJyk7CiAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJjYXJkIj48aDM+7Jio67O065SpIOyghO2ZmOycqCA8c3BhbiBjbGFzcz0ibWluaSI+7KCE7LK0IOqwgOyeheyekCAvIOqwgOyehSDri7nsnbw8L3NwYW4+PC9oMz48ZGl2IGNsYXNzPSJjc3ViIj7soIQg7LGE64SQIOqwgOyeheyekCjqtJHqs6Ar67mE6rSR6rOgKeqwgCA8Yj7qsIDsnoUg64u57J28PC9iPiDtlbTri7kg7ZaJ64+Z7J2EIO2VnCDruYTsnKggLyDrtoTrqqggPSDri7nsnbwg6rCA7J6F7J6QIOyImDwvZGl2PjxkaXYgY2xhc3M9ImNvbnZncmlkIj4ke2l0ZW1zfTwvZGl2PjwvZGl2PmA7Cn0KZnVuY3Rpb24gZWZmQmxvY2soVyl7CiAgY29uc3QgYm9keT1BREVGRi5tYXAoYT0+ewogICAgY29uc3QgY29zdD1zdW0oYS5jb3N0LFcuY3VyKSxpbnN0PXN1bShhLmluc3RhbGx8fFtdLFcuY3VyKSxzdT1zdW0oYS5zdSxXLmN1ciksY2FsbD1zdW0oYS5jYWxsLFcuY3VyKTsKICAgIGNvbnN0IGNwcz1zdT9jb3N0L3N1Om51bGwsY3BjPWNhbGw/Y29zdC9jYWxsOm51bGwsaTJzPWluc3Q/c3UvaW5zdCoxMDA6bnVsbDsKICAgIHJldHVybiBgPHRyPjx0ZCBjbGFzcz0ibmFtZSI+JHthLm5hbWV9PC90ZD48dGQgY2xhc3M9ImRpbSI+JHtmKGluc3QpfTwvdGQ+PHRkIGNsYXNzPSJkaW0iPiR7ZihzdSl9PC90ZD48dGQ+JHtpMnMhPW51bGw/aTJzLnRvRml4ZWQoMSkrJyUnOifigJQnfTwvdGQ+PHRkPuKCqSR7ZihjcHMpfTwvdGQ+PHRkIGNsYXNzPSJrcGljb2wiPiR7ZihjYWxsKX08L3RkPjx0ZD7igqkke2YoY3BjKX08L3RkPjx0ZCBjbGFzcz0iZGltIj7igqkke2YoY29zdCl9PC90ZD48L3RyPmA7CiAgfSkuam9pbignJyk7CiAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJjYXJkIj48aDM+7LGE64SQ67OEIO2aqOycqDwvaDM+PGRpdiBjbGFzcz0iY3N1YiI+7ISk7LmYIOKGkiDqsIDsnoUg7KCE7ZmY6rO8IOqwgOyehSAx66qFIOuwjyDtg53si5ztmLjstpwgMeqxtOuLuSDruYTsmqk8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNjcm9sbCI+PHRhYmxlIHN0eWxlPSJtaW4td2lkdGg6NzIwcHgiPjx0aGVhZD48dHI+PHRoPuyxhOuEkDwvdGg+PHRoPuyEpOy5mDwvdGg+PHRoPuqwgOyehTwvdGg+PHRoPuqwgOyehS/shKTsuZg8L3RoPjx0aD7qsIDsnoXri7kg67mE7JqpPC90aD48dGggY2xhc3M9ImtwaWNvbCI+7YOd7Iuc7Zi47LacPC90aD48dGg+7Zi47Lac64u5IOu5hOyaqTwvdGg+PHRoPuq0keqzoOu5hDwvdGg+PC90cj48L3RoZWFkPjx0Ym9keT4ke2JvZHl9PC90Ym9keT48L3RhYmxlPjwvZGl2PjwvZGl2PmA7Cn0KZnVuY3Rpb24gcmV0bkJsb2NrKCl7CiAgaWYoIVJFVE58fCFSRVROLnBvaW50c3x8IVJFVE4ucG9pbnRzLmxlbmd0aCkgcmV0dXJuICcnOwogIGNvbnN0IG14PU1hdGgubWF4KC4uLlJFVE4ucG9pbnRzLm1hcChwPT5wWzFdKSwwLjAxKTsKICBjb25zdCBiYXJzPVJFVE4ucG9pbnRzLm1hcChwPT5gPGRpdiBzdHlsZT0iZmxleDoxO3RleHQtYWxpZ246Y2VudGVyIj4KICAgIDxkaXYgc3R5bGU9ImhlaWdodDo5MnB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpmbGV4LWVuZDtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyIj48ZGl2IHN0eWxlPSJ3aWR0aDo1OCU7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQpO2JvcmRlci1yYWRpdXM6NXB4IDVweCAwIDA7aGVpZ2h0OiR7TWF0aC5tYXgoMyxwWzFdL214KjkyKS50b0ZpeGVkKDApfXB4Ij48L2Rpdj48L2Rpdj4KICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxM3B4O2ZvbnQtd2VpZ2h0OjgwMDttYXJnaW4tdG9wOjVweCI+JHsocFsxXSoxMDApLnRvRml4ZWQoMSl9JTwvZGl2PgogICAgPGRpdiBjbGFzcz0iZGltIiBzdHlsZT0iZm9udC1zaXplOjEycHgiPiR7cFswXX08L2Rpdj48L2Rpdj5gKS5qb2luKCcnKTsKICByZXR1cm4gYDxkaXYgY2xhc3M9ImNhcmQiPjxoMz7snqzrsKnrrLgo7JWxIOumrO2FkOyFmCkgPHNwYW4gY2xhc3M9Im1pbmkiPuyEpOy5mCDihpIg7JWxIOuLpOyLnCDsl7TquLAgLyAke1JFVE4ud2luZG93fSDsvZTtmLjtirg8L3NwYW4+PC9oMz4KICAgIDxkaXYgY2xhc3M9ImNzdWIiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjEwcHgiPuyVsSDshKTsuZjtlZwg7IKs656M7J20IEQxL0Q3L0QxNC9EMzDsl5Ag7JWx7J2EIOuLpOyLnCDsl7Ag67mE7JyoICjsvZTtmLjtirggJHtmKFJFVE4uY29ob3J0KX3rqoUsIEQzMOq5jOyngCDshLHsiJkpPC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7Z2FwOjEycHg7YWxpZ24taXRlbXM6ZmxleC1lbmQiPiR7YmFyc308L2Rpdj48L2Rpdj5gOwp9CmZ1bmN0aW9uIHJlbmRlclN1bW1hcnkoKXsKICBjb25zdCBXPXdpbmRvd3MoKSxwcj1XLnByaW9yLmxlbmd0aDsKICBjb25zdCBzdT1zdW0oVE9UQUxTLnN1LFcuY3VyKSxzdVA9cHI/c3VtKFRPVEFMUy5zdSxXLnByaW9yKTpudWxsOwogIGNvbnN0IGNhcmQ9c3VtKFRPVEFMUy5jYXJkLFcuY3VyKSxjYXJkUD1wcj9zdW0oVE9UQUxTLmNhcmQsVy5wcmlvcik6bnVsbDsKICBjb25zdCBsbz1zdW0oVE9UQUxTLmxvdW5nZSxXLmN1ciksbG9QPXByP3N1bShUT1RBTFMubG91bmdlLFcucHJpb3IpOm51bGw7CiAgY29uc3QgY2FsbD1zdW0oVEFYSVRPVCxXLmN1ciksY2FsbFA9cHI/c3VtKFRBWElUT1QsVy5wcmlvcik6bnVsbDsKICBjb25zdCBpbnN0PXN1bShUT1RBTFMuaW5zdGFsbHx8W10sVy5jdXIpLGluc3RQPXByP3N1bShUT1RBTFMuaW5zdGFsbHx8W10sVy5wcmlvcik6bnVsbDsKICBjb25zdCB2aXM9c3VtKFRPVEFMUy52aXNpdHx8W10sVy5jdXIpLHZpc1A9cHI/c3VtKFRPVEFMUy52aXNpdHx8W10sVy5wcmlvcik6bnVsbDsKICBjb25zdCBjYXJkcz1gPGRpdiBjbGFzcz0ia3BpcyI+CiAgICAke2twaSgi7JWx7ISk7LmYIixpbnN0LGluc3RQLHt1bml0OiLrqoUiLHN1YjphZFN1YigiaW5zdGFsbCIsaW5zdCxXKX0pfQogICAgJHtrcGkoIuyVsSDrsKnrrLgiLHZpcyx2aXNQLHt1bml0OiLrqoUiLHN1YjphZFN1YigidmlzaXQiLHZpcyxXKX0pfQogICAgJHtrcGkoIu2ajOybkOqwgOyehSIsc3Usc3VQLHtwcmltYXJ5OnRydWUsdW5pdDoi66qFIixzdWI6YWRTdWIoInN1IixzdSxXKX0pfQogICAgJHtrcGkoIuq1kO2Gtey5tOuTnCDrk7HroZ0iLGNhcmQsY2FyZFAse3VuaXQ6IuqxtCIsc3ViOmFkU3ViKCJjYXJkIixjYXJkLFcpfSl9CiAgICAke2twaSgi64yA7KSR6rWQ7Ya165287Jq07KeAIOuwqeusuCIsbG8sbG9QLHt1bml0OiLrqoUiLHN1YjphZFN1YigibG91bmdlIixsbyxXKX0pfQogICAgJHtrcGkoIu2DneyLnO2YuOy2nCIsY2FsbCxjYWxsUCx7dW5pdDoi6rG0IixzdWI6YWRTdWIoImNhbGwiLGNhbGwsVyl9KX0KICA8L2Rpdj5gOwogIHJldHVybiBjYXJkcytzdW1tYXJ5VHJlbmQoVykrZHJpbGxCbG9jayhXKStlZmZCbG9jayhXKTsKfQoKLyogLS0tLSDrsoTtgrcg7YOtID0g6rSR6rOgIOq4sOyXrOunjCAtLS0tICovCmZ1bmN0aW9uIGFnZ1Jvdyhyb3csaWR4KXtjb25zdCBzPXJvdy5zO3JldHVybntjYWxsOnN1bShzLmNhbGwsaWR4KSxjb21wOnN1bShzLmNvbXAsaWR4KSxzdTpzdW0ocy5zdSxpZHgpLGNvc3Q6c3VtKHMuY29zdCxpZHgpfTt9CmZ1bmN0aW9uIGJ1Y2tldFJvd3MoaWQpe3JldHVybiBST1dTLmZpbHRlcihyPT5yLmJ1Y2tldD09PWlkKTt9CmZ1bmN0aW9uIGJ1Y2tldEFjdGl2ZShpZCl7Y29uc3QgYWxsPURBWVMubWFwKChfLGkpPT5pKTtyZXR1cm4gYnVja2V0Um93cyhpZCkuc29tZShyPT5yLmNoIT09Im9yZyImJnN1bShyLnMuY2FsbCxhbGwpPjApO30KY29uc3QgS0lORF9MQUJFTD17Z29vZ2xlOiJnb29nbGUuYWRzIixpbnN0YToiaW5zdGEiLGV0Yzoi67mE7JqpIOuvuOyXsOuPmSDqtJHqs6AiLHhhZHM6IuuLpOuluCDrqqnsoIEg6rSR6rOgIixvcmc6Iuq0keqzoCDsmbgg7LGE64SQIn07Ci8qIO2VnCDrsoTtgrfsnZgg66qo65OgIOyEueyFmOydhCAn7ZWY64KY7J2YIO2RnCfroZwg4oCUIOyXtCDsnITsuZjqsIAg7KCE67aAIOunnuydjCjspITrp57stqQpICovCmZ1bmN0aW9uIGJ1Y2tldENvbHMoQil7IC8vIOqzte2GtSBjb2xncm91cCArIO2XpOuNlCAo67KE7YK3IOuCtCDrqqjrk6Ag7IS57IWYIOuPmeydvCkg4oCUIO2ajOybkOqwgOyehSDsl7Qg7ZWt7IOBIO2PrO2VqAogIGxldCBjb2x3LCBoZWFkOwogIGlmKEIuZHVhbENwYSl7IGNvbHc9WyIyMiUiLCIxMyUiLCIxMSUiLCIxMiUiLCIxMSUiLCIxMiUiLCIxMCUiLCI5JSJdOwogICAgaGVhZD1gPHRoPuy6oO2OmOyduDwvdGg+PHRoIGNsYXNzPSJsaCI+7JWg65Oc6re466O5PC90aD48dGg+7ZqM7JuQ6rCA7J6FPC90aD48dGggY2xhc3M9ImtwaWNvbCI+JHtCLnByaW1hcnlMYWJlbH08L3RoPjx0aD4ke0IuY29tcExhYmVsfTwvdGg+PHRoPuq0keqzoOu5hDwvdGg+PHRoPiR7Qi5jcGFMYWJlbH08L3RoPjx0aD4ke0IuY29tcENwYUxhYmVsfTwvdGg+YDsKICB9IGVsc2UgaWYoQi5zaG93Q29tcCl7IGNvbHc9WyIyNCUiLCIxNCUiLCIxMiUiLCIxNCUiLCIxMyUiLCIxMiUiLCIxMSUiXTsKICAgIGhlYWQ9YDx0aD7suqDtjpjsnbg8L3RoPjx0aCBjbGFzcz0ibGgiPuyVoOuTnOq3uOujuTwvdGg+PHRoPu2ajOybkOqwgOyehTwvdGg+PHRoIGNsYXNzPSJrcGljb2wiPiR7Qi5wcmltYXJ5TGFiZWx9PC90aD48dGg+JHtCLmNvbXBMYWJlbH08L3RoPjx0aD7qtJHqs6DruYQ8L3RoPjx0aD4ke0IuY3BhTGFiZWx9PC90aD5gOwogIH0gZWxzZSB7IGNvbHc9WyIyOCUiLCIxNiUiLCIxNSUiLCIxNSUiLCIxNCUiLCIxMiUiXTsKICAgIGhlYWQ9YDx0aD7suqDtjpjsnbg8L3RoPjx0aCBjbGFzcz0ibGgiPuyVoOuTnOq3uOujuTwvdGg+PHRoPu2ajOybkOqwgOyehTwvdGg+PHRoIGNsYXNzPSJrcGljb2wiPiR7Qi5wcmltYXJ5TGFiZWx9PC90aD48dGg+6rSR6rOg67mEPC90aD48dGg+JHtCLmNwYUxhYmVsfTwvdGg+YDsKICB9CiAgY29uc3QgY29sPWA8Y29sZ3JvdXA+JHtjb2x3Lm1hcCh3PT5gPGNvbCBzdHlsZT0id2lkdGg6JHt3fSI+YCkuam9pbignJyl9PC9jb2xncm91cD5gOwogIHJldHVybiB7bmNvbDpjb2x3Lmxlbmd0aCwgY29sLCBoZWFkOmA8dGhlYWQ+PHRyPiR7aGVhZH08L3RyPjwvdGhlYWQ+YH07Cn0KZnVuY3Rpb24gc2VjUm93KG8sQixub0Nvc3Qsc2hvd0NyKXsKICBsZXQgY2VsbHM9YDx0ZCBjbGFzcz0ibmFtZSI+JHtvLmNhbXB9PC90ZD48dGQgY2xhc3M9Im5hbWUgZGltIj4ke28uYWd8fCcnfSR7c2hvd0NyJiZvLmNyP2AgPHNwYW4gc3R5bGU9Im9wYWNpdHk6LjciPuKAuiAke28uY3J9PC9zcGFuPmA6Jyd9PC90ZD5gOwogIGNlbGxzKz1gPHRkIGNsYXNzPSJkaW0iPiR7ZihvLmNfc3UpfTwvdGQ+YDsgICAvLyDtmozsm5DqsIDsnoUgKO2VreyDgSkKICBpZihCLnNob3dDb21wKSBjZWxscys9YDx0ZCBjbGFzcz0ia3BpY29sIj4ke2Yoby5jX2NhbGwpfSR7ZGVsdGFTcGFuKG8uY19jYWxsLG8ucF9jYWxsPT1udWxsP251bGw6by5wX2NhbGwsdHJ1ZSx0cnVlKX08L3RkPjx0ZD4ke2Yoby5jX2NvbXApfTwvdGQ+YDsKICBlbHNlIGNlbGxzKz1gPHRkIGNsYXNzPSJrcGljb2wiPiR7ZihvLmNfY2FsbCl9JHtkZWx0YVNwYW4oby5jX2NhbGwsby5wX2NhbGw9PW51bGw/bnVsbDpvLnBfY2FsbCx0cnVlLHRydWUpfTwvdGQ+YDsKICBjZWxscys9YDx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzon4oKpJytmKG8uY19jb3N0KX08L3RkPmA7CiAgaWYoQi5kdWFsQ3BhKXtjb25zdCBjMT1vLmNfY2FsbD9vLmNfY29zdC9vLmNfY2FsbDpudWxsLGMyPW8uY19jb21wP28uY19jb3N0L28uY19jb21wOm51bGw7Y2VsbHMrPWA8dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6KGMxIT1udWxsPyfigqknK2YoYzEpOifigJQnKX08L3RkPjx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzooYzIhPW51bGw/J+KCqScrZihjMik6J+KAlCcpfTwvdGQ+YDt9CiAgZWxzZXtjb25zdCBkPUIuc2hvd0NvbXA/by5jX2NvbXA6by5jX2NhbGwsY3BhPW5vQ29zdD9udWxsOihkP28uY19jb3N0L2Q6bnVsbCk7Y2VsbHMrPWA8dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6KGNwYSE9bnVsbD8n4oKpJytmKGNwYSk6J+KAlCcpfTwvdGQ+YDt9CiAgcmV0dXJuIGA8dHI+JHtjZWxsc308L3RyPmA7Cn0KZnVuY3Rpb24gc2VjVG90Um93KGFnZyxCLG5vQ29zdCl7CiAgY29uc3QgdD1hZ2cucmVkdWNlKChhLG8pPT57YS5zdSs9by5jX3N1fHwwO2EuY2FsbCs9by5jX2NhbGx8fDA7YS5jb21wKz1vLmNfY29tcHx8MDthLmNvc3QrPW8uY19jb3N0fHwwO3JldHVybiBhO30se3N1OjAsY2FsbDowLGNvbXA6MCxjb3N0OjB9KTsKICBsZXQgbWlkPWA8dGQgY2xhc3M9ImRpbSI+JHtmKHQuc3UpfTwvdGQ+YCsoQi5zaG93Q29tcD9gPHRkIGNsYXNzPSJrcGljb2wiPiR7Zih0LmNhbGwpfTwvdGQ+PHRkPiR7Zih0LmNvbXApfTwvdGQ+YDpgPHRkIGNsYXNzPSJrcGljb2wiPiR7Zih0LmNhbGwpfTwvdGQ+YCk7CiAgbGV0IGNwYUM7CiAgaWYoQi5kdWFsQ3BhKXtjb25zdCBjMT10LmNhbGw/dC5jb3N0L3QuY2FsbDpudWxsLGMyPXQuY29tcD90LmNvc3QvdC5jb21wOm51bGw7Y3BhQz1gPHRkIGNsYXNzPSJkaW0iPiR7bm9Db3N0PycnOihjMSE9bnVsbD8n4oKpJytmKGMxKTon4oCUJyl9PC90ZD48dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6KGMyIT1udWxsPyfigqknK2YoYzIpOifigJQnKX08L3RkPmA7fQogIGVsc2V7Y29uc3QgZD1CLnNob3dDb21wP3QuY29tcDp0LmNhbGwsY3BhPW5vQ29zdD9udWxsOihkP3QuY29zdC9kOm51bGwpO2NwYUM9YDx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzooY3BhIT1udWxsPyfigqknK2YoY3BhKTon4oCUJyl9PC90ZD5gO30KICByZXR1cm4gYDx0ciBjbGFzcz0idG90cm93Ij48dGQ+7ZWp6rOEPC90ZD48dGQ+PC90ZD4ke21pZH08dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6J+KCqScrZih0LmNvc3QpfTwvdGQ+JHtjcGFDfTwvdHI+YDsKfQpmdW5jdGlvbiBzZWNCbG9jayhyb3dzLGtpbmQsQixuY29sKXsKICBjb25zdCBub0Nvc3Q9KGtpbmQ9PT0iZXRjInx8a2luZD09PSJvcmciKTsKICBjb25zdCBzaG93Q3I9ISFDUlNIT1dbQi5pZF0mJihraW5kPT09Imdvb2dsZSJ8fGtpbmQ9PT0iaW5zdGEifHxraW5kPT09ImV0YyJ8fGtpbmQ9PT0ieGFkcyIpOwogIGNvbnN0IGdyb3Vwcz17fTsKICByb3dzLmZvckVhY2gocj0+e2NvbnN0IGtleT1yLmNhbXArJ1x1MDAwMScrKHIuYWd8fCcnKSsoc2hvd0NyPydcdTAwMDEnKyhyLmNyfHwnJyk6JycpOyhncm91cHNba2V5XXx8KGdyb3Vwc1trZXldPVtdKSkucHVzaChyKTt9KTsKICBjb25zdCBhZ2c9T2JqZWN0LmtleXMoZ3JvdXBzKS5tYXAoa2V5PT57Y29uc3QgZ3JwPWdyb3Vwc1trZXldLHIwPWdycFswXTtjb25zdCBvPXtjYW1wOnIwLmNhbXAsYWc6cjAuYWcsY3I6c2hvd0NyPyhyMC5jcnx8JycpOicnfTtjb25zdCBjdT17Y2FsbDowLGNvbXA6MCxzdTowLGNvc3Q6MH0scHI9Q1VSVy5wcmlvci5sZW5ndGg/e2NhbGw6MCxjb21wOjAsc3U6MCxjb3N0OjB9Om51bGw7Z3JwLmZvckVhY2gocj0+e2NvbnN0IGE9YWdnUm93KHIsQ1VSVy5jdXIpO2N1LmNhbGwrPWEuY2FsbDtjdS5jb21wKz1hLmNvbXA7Y3Uuc3UrPWEuc3U7Y3UuY29zdCs9YS5jb3N0O2lmKHByKXtjb25zdCBwPWFnZ1JvdyhyLENVUlcucHJpb3IpO3ByLmNhbGwrPXAuY2FsbDtwci5jb21wKz1wLmNvbXA7cHIuc3UrPXAuc3U7cHIuY29zdCs9cC5jb3N0O319KTtbImNhbGwiLCJjb21wIiwic3UiLCJjb3N0Il0uZm9yRWFjaChrPT57b1siY18iK2tdPWN1W2tdO2lmKHByKW9bInBfIitrXT1wcltrXTt9KTtyZXR1cm4gbzt9KS5zb3J0KChhLGIpPT5iLmNfY2FsbC1hLmNfY2FsbCk7CiAgY29uc3QgaGQ9YDx0ciBjbGFzcz0ic2VjaGQtcm93Ij48dGQgY29sc3Bhbj0iJHtuY29sfSI+JHtLSU5EX0xBQkVMW2tpbmRdfTwvdGQ+PC90cj5gOwogIHJldHVybiBoZCthZ2cubWFwKG89PnNlY1JvdyhvLEIsbm9Db3N0LHNob3dDcikpLmpvaW4oJycpK3NlY1RvdFJvdyhhZ2csQixub0Nvc3QpOwp9CmxldCBDVVJXPW51bGw7CmZ1bmN0aW9uIHJlbmRlckJ1Y2tldChpZCl7CiAgY29uc3QgQj1CVUNLRVRTLmZpbmQoeD0+eC5pZD09PWlkKSxyb3dzPWJ1Y2tldFJvd3MoaWQpO0NVUlc9d2luZG93cygpOwogIGNvbnN0IGlzQWQ9cj0+ci5jaD09PSJnb29nbGUifHxyLmNoPT09Imluc3RhInx8ci5jaD09PSJldGMiOwogIGNvbnN0IGFnZz0oaWR4LGZpbHQpPT5yb3dzLnJlZHVjZSgobyxyKT0+e2lmKGZpbHQmJiFmaWx0KHIpKXJldHVybiBvO2NvbnN0IGE9YWdnUm93KHIsaWR4KTtvLmNhbGwrPWEuY2FsbDtvLmNvbXArPWEuY29tcDtvLmNvc3QrPWEuY29zdDtyZXR1cm4gbzt9LHtjYWxsOjAsY29tcDowLGNvc3Q6MH0pOwogIGNvbnN0IGM9YWdnKENVUlcuY3VyLGlzQWQpLHA9Q1VSVy5wcmlvci5sZW5ndGg/YWdnKENVUlcucHJpb3IsaXNBZCk6bnVsbDsgICAgICAgICAgLy8g6rSR6rOgIOq4sOyXrCjrqqnsoIEg7Lqg7Y6Y7J24KQogIGNvbnN0IGJ0PUJUT1RbaWRdfHx7fTsgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIC8vIOyghOyytCjsoIQg7LGE64SQLCDsp4HsoJEg7KeR6rOEKQogIGNvbnN0IHRvdENhbGw9c3VtKGJ0LmNhbGx8fFtdLENVUlcuY3VyKSwgdG90Q2FsbFA9Q1VSVy5wcmlvci5sZW5ndGg/c3VtKGJ0LmNhbGx8fFtdLENVUlcucHJpb3IpOm51bGw7CiAgY29uc3QgdG90Q29tcD1zdW0oYnQuY29tcHx8W10sQ1VSVy5jdXIpLCB0b3RDb21wUD1DVVJXLnByaW9yLmxlbmd0aD9zdW0oYnQuY29tcHx8W10sQ1VSVy5wcmlvcik6bnVsbDsKICBsZXQgY2FyZHM9YDxkaXYgY2xhc3M9ImtwaXMiPmA7CiAgY29uc3QgVT1CLnVuaXR8fCLqsbQiOwogIGNhcmRzKz1rcGkoIuyghOyytCAiK0IucHJpbWFyeUxhYmVsLHRvdENhbGwsdG90Q2FsbFAse3VuaXQ6VX0pOwogIGNhcmRzKz1rcGkoIuq0keqzoCAiK0IucHJpbWFyeUxhYmVsLGMuY2FsbCxwJiZwLmNhbGwse3ByaW1hcnk6dHJ1ZSx1bml0OlV9KTsKICBpZihCLnNob3dDb21wKXsgY2FyZHMrPWtwaSgi7KCE7LK0ICIrQi5jb21wTGFiZWwsdG90Q29tcCx0b3RDb21wUCx7dW5pdDpVfSk7IGNhcmRzKz1rcGkoIuq0keqzoCAiK0IuY29tcExhYmVsLGMuY29tcCxwJiZwLmNvbXAse3VuaXQ6VX0pOyB9CiAgY2FyZHMrPWtwaSgi6rSR6rOg67mEIixjLmNvc3QscCYmcC5jb3N0LHt3b246dHJ1ZX0pOwogIGlmKEIuZHVhbENwYSl7Y29uc3QgZDE9eD0+eC5jYWxsP3guY29zdC94LmNhbGw6bnVsbCxkMj14PT54LmNvbXA/eC5jb3N0L3guY29tcDpudWxsO2NhcmRzKz1rcGkoQi5jcGFMYWJlbCxkMShjKSxwJiZkMShwKSx7d29uOnRydWUsZ29vZFVwOmZhbHNlfSkra3BpKEIuY29tcENwYUxhYmVsLGQyKGMpLHAmJmQyKHApLHt3b246dHJ1ZSxnb29kVXA6ZmFsc2V9KTt9CiAgZWxzZXtjb25zdCBjcGE9eD0+e2NvbnN0IGQ9Qi5zaG93Q29tcD94LmNvbXA6eC5jYWxsO3JldHVybiBkP3guY29zdC9kOm51bGw7fTtjYXJkcys9a3BpKEIuY3BhTGFiZWwsY3BhKGMpLHAmJmNwYShwKSx7d29uOnRydWUsZ29vZFVwOmZhbHNlfSk7fQogIGNhcmRzKz1gPC9kaXY+YDsKICAvLyDsoITssrQg64yA67mEIOq0keqzoCDruYTspJEg4oCUIOu2hOyekOuKlCAn7KCE7LK0IOq0keqzoCDquLDsl6wnKOuqqeyggSvri6Trpbgg66qp7KCBLCBvcmcg7KCc7Jm4KS4g67mE7JqpL0NQQeuKlCDrqqnsoIHrp4wo7JyEIEtQSSkKICBjb25zdCBhZEFsbD1hZ2coQ1VSVy5jdXIscj0+ci5jaCE9PSJvcmciKS5jYWxsOwogIGNvbnN0IGFkUGN0PXRvdENhbGw/TWF0aC5taW4oMTAwLGFkQWxsL3RvdENhbGwqMTAwKTowOwogIGNvbnN0IHNoYXJlQmFyPWA8ZGl2IGNsYXNzPSJjYXJkIiBzdHlsZT0icGFkZGluZzoxMXB4IDE1cHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmJhc2VsaW5lO2dhcDoxMnB4O2ZsZXgtd3JhcDp3cmFwIj48ZGl2IGNsYXNzPSJjc3ViIiBzdHlsZT0ibWFyZ2luOjAiPuq0keqzoCDquLDsl6wg67mE7KSRPC9kaXY+PGRpdiBzdHlsZT0iZm9udC1zaXplOjIwcHg7Zm9udC13ZWlnaHQ6ODAwO2NvbG9yOnZhcigtLWFjY2VudCk7bGV0dGVyLXNwYWNpbmc6LTAuNXB4Ij4ke2FkUGN0LnRvRml4ZWQoYWRQY3Q8MTA/MTowKX0lPC9kaXY+PGRpdiBjbGFzcz0iY3N1YiIgc3R5bGU9Im1hcmdpbjowIj7qtJHqs6AgJHtmKGFkQWxsKX0gLyDsoITssrQgJHtmKHRvdENhbGwpfTwvZGl2PjwvZGl2PmA7CiAgY29uc3Qgbm9BZD1hZEFsbD09PTA7CiAgbGV0IGh0bWw9bm9BZAogICAgPyBgPGRpdiBjbGFzcz0iYmFubmVyIj7snbQg6riw6rCEIOq0keqzoCDsp5Htlokg7JeG7J2MIOKAlCDshKDtg53tlZwg6riw6rCE7JeQICR7Qi5sYWJlbH0g6rSR6rOgIOynke2WieydtCDsl4bsirXri4jri6QuIOyghOyytCDsnbTsmqnrn4nrp4wg7ZGc7Iuc64+87JqULjwvZGl2PmArY2FyZHMKICAgIDogYDxkaXYgY2xhc3M9Im5vdGVsaW5lIiBzdHlsZT0ibGluZS1oZWlnaHQ6MS43Ij48Yj7soITssrQ8L2I+ID0g7KCEIOyxhOuEkDxicj48Yj7qtJHqs6Av6rSR6rOg67mEL0NQQTwvYj4gPSDrqqnsoIEg7Lqg7Y6Y7J2466eMPGJyPjxiPuq0keqzoCDquLDsl6wg67mE7KSRPC9iPiA9IOyghOyytCDqtJHqs6Ag6riw7JesPC9kaXY+YCtjYXJkcytzaGFyZUJhcjsKICBpZighbm9BZCAmJiBCLm5vdGUpIGh0bWwrPWA8ZGl2IGNsYXNzPSJjc3ViIiBzdHlsZT0ibWFyZ2luOi02cHggMCAxMnB4Ij7igLsgJHtCLm5vdGV9PC9kaXY+YDsKICBodG1sKz1idWNrZXRUcmVuZChCLHJvd3MsQ1VSVyk7CiAgaWYoIW5vQWQpewogICAgY29uc3QgQz1idWNrZXRDb2xzKEIpOwogICAgY29uc3Qga2luZHM9Qi5zaG93T3JnP1siZ29vZ2xlIiwiaW5zdGEiLCJldGMiLCJ4YWRzIiwib3JnIl06WyJnb29nbGUiLCJpbnN0YSIsImV0YyIsInhhZHMiXTsKICAgIGxldCBib2R5PSIiOwogICAga2luZHMuZm9yRWFjaChraW5kPT57Y29uc3QgcnM9cm93cy5maWx0ZXIocj0+ci5jaD09PWtpbmQpO2lmKHJzLmxlbmd0aClib2R5Kz1zZWNCbG9jayhycyxraW5kLEIsQy5uY29sKTt9KTsKICAgIGNvbnN0IGhhc0NyPXJvd3Muc29tZShyPT5yLmNoIT09Im9yZyImJnIuY3IpOwogICAgY29uc3QgY3JCdG49aGFzQ3I/YDxkaXYgY2xhc3M9InNlZyIgc3R5bGU9Im1hcmdpbjoycHggMCAxMHB4Ij48YnV0dG9uIGNsYXNzPSIke0NSU0hPV1tpZF0/J29uJzonJ30iIGRhdGEtY3I9IiR7aWR9Ij7shozsnqzquYzsp4Ag67O06riwPC9idXR0b24+PC9kaXY+YDonJzsKICAgIGh0bWwrPWNyQnRuK2A8ZGl2IGNsYXNzPSJjYXJkIj48ZGl2IGNsYXNzPSJzY3JvbGwiPjx0YWJsZSBzdHlsZT0ibWluLXdpZHRoOjY4MHB4O3RhYmxlLWxheW91dDpmaXhlZCI+JHtDLmNvbH0ke0MuaGVhZH08dGJvZHk+JHtib2R5fTwvdGJvZHk+PC90YWJsZT48L2Rpdj48L2Rpdj5gOwogIH0KICByZXR1cm4gaHRtbDsKfQoKLyogLS0tLSDshbggLS0tLSAqLwpsZXQgVEFCPSJzdW1tYXJ5IjsKZnVuY3Rpb24gZHJpbGxBZChtZXRyaWMsaWR4KXtyZXR1cm4gKERSSUxMJiZEUklMTC5yb3dzKT9EUklMTC5yb3dzLnJlZHVjZSgoYSxyKT0+YStzdW0oci52W21ldHJpY118fFtdLGlkeCksMCk6MDt9CmZ1bmN0aW9uIGFkU3ViKG1ldHJpYyx0b3RhbCxXKXtjb25zdCBhZD1kcmlsbEFkKG1ldHJpYyxXLmN1cik7Y29uc3QgcD10b3RhbD9hZC90b3RhbCoxMDA6MDtyZXR1cm4gYDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLWFjY2VudCk7Zm9udC13ZWlnaHQ6NzAwO21hcmdpbi10b3A6N3B4O3BhZGRpbmctdG9wOjZweDtib3JkZXItdG9wOjFweCBzb2xpZCB2YXIoLS1saW5lKSI+6rSR6rOgICR7ZihhZCl9IC8gJHtwLnRvRml4ZWQoMSl9JTwvZGl2PmA7fQpmdW5jdGlvbiBkcmlsbEJsb2NrKFcpewogIGlmKCFEUklMTHx8IURSSUxMLnJvd3N8fCFEUklMTC5yb3dzLmxlbmd0aCkgcmV0dXJuICcnOwogIGNvbnN0IG1rPURSSUxMTUVUUklDOwogIGNvbnN0IG9wdHM9RFJJTEwubWV0cmljcy5tYXAobT0+YDxvcHRpb24gdmFsdWU9IiR7bVswXX0iICR7bVswXT09PW1rPydzZWxlY3RlZCc6Jyd9PiR7bVsxXX08L29wdGlvbj5gKS5qb2luKCcnKTsKICBjb25zdCByb290PXtjaGlsZHJlbjp7fX07CiAgRFJJTEwucm93cy5mb3JFYWNoKHI9PnsKICAgIGNvbnN0IHZhbD1zdW0oci52W21rXXx8W10sVy5jdXIpOyBpZighdmFsKSByZXR1cm47CiAgICBjb25zdCBwYXRoPVtyLmNoLHIuY2FtcCxyLmFnLHIuY3JdOyBsZXQgbm9kZT1yb290LCBrZXk9Jyc7CiAgICBwYXRoLmZvckVhY2goKHNlZyxkZXB0aCk9Pnsga2V5Kz0nXHUwMDAxJytzZWc7CiAgICAgIG5vZGUuY2hpbGRyZW5ba2V5XT1ub2RlLmNoaWxkcmVuW2tleV18fHtuYW1lOnNlZyxkZXB0aCx2YWw6MCxrZXksY2hpbGRyZW46e319OwogICAgICBub2RlLmNoaWxkcmVuW2tleV0udmFsKz12YWw7IG5vZGU9bm9kZS5jaGlsZHJlbltrZXldOyB9KTsKICB9KTsKICBjb25zdCBhZFRvdGFsPU9iamVjdC52YWx1ZXMocm9vdC5jaGlsZHJlbikucmVkdWNlKChhLGMpPT5hK2MudmFsLDApfHwxOwogIGxldCBib2R5PScnOwogIGZ1bmN0aW9uIHdhbGsobm9kZSl7CiAgICBPYmplY3QudmFsdWVzKG5vZGUuY2hpbGRyZW4pLnNvcnQoKGEsYik9PmIudmFsLWEudmFsKS5mb3JFYWNoKGM9PnsKICAgICAgaWYoYy52YWw8PTApIHJldHVybjsKICAgICAgY29uc3QgaGFzQ2g9Yy5kZXB0aDwzICYmIE9iamVjdC5rZXlzKGMuY2hpbGRyZW4pLmxlbmd0aD4wOwogICAgICBjb25zdCBvcGVuPURFWFAuaGFzKGMua2V5KSwgcGN0PWMudmFsL2FkVG90YWwqMTAwOwogICAgICBib2R5Kz1gPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4O3BhZGRpbmc6NXB4IDA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgdmFyKC0tbGluZSk7Zm9udC1zaXplOjEzcHg7cGFkZGluZy1sZWZ0OiR7NitjLmRlcHRoKjE4fXB4Ij5gCiAgICAgICAgKyhoYXNDaD9gPGJ1dHRvbiBkYXRhLWRleHA9IiR7ZW5jb2RlVVJJQ29tcG9uZW50KGMua2V5KX0iIHN0eWxlPSJib3JkZXI6bm9uZTtiYWNrZ3JvdW5kOm5vbmU7Y3Vyc29yOnBvaW50ZXI7Y29sb3I6dmFyKC0tc3ViKTtmb250LXNpemU6MTJweDt3aWR0aDoxNnB4O3BhZGRpbmc6MCI+JHtvcGVuPyfilr4nOifilrgnfTwvYnV0dG9uPmA6JzxzcGFuIHN0eWxlPSJkaXNwbGF5OmlubGluZS1ibG9jazt3aWR0aDoxNnB4Ij48L3NwYW4+JykKICAgICAgICArYDxzcGFuIHN0eWxlPSJmbGV4OjE7b3ZlcmZsb3c6aGlkZGVuO3RleHQtb3ZlcmZsb3c6ZWxsaXBzaXM7d2hpdGUtc3BhY2U6bm93cmFwOyR7Yy5kZXB0aD09PTA/J2ZvbnQtd2VpZ2h0OjcwMCc6Jyd9Ij4ke2MubmFtZX08L3NwYW4+YAogICAgICAgICtgPHNwYW4gc3R5bGU9ImZvbnQtdmFyaWFudC1udW1lcmljOnRhYnVsYXItbnVtcztmb250LXdlaWdodDo3MDAiPiR7ZihjLnZhbCl9IDxzcGFuIGNsYXNzPSJkaW0iIHN0eWxlPSJmb250LXdlaWdodDo0MDAiPi8gJHtwY3QudG9GaXhlZCgxKX0lPC9zcGFuPjwvc3Bhbj48L2Rpdj5gOwogICAgICBpZihvcGVuJiZoYXNDaCkgd2FsayhjKTsKICAgIH0pOwogIH0KICB3YWxrKHJvb3QpOwogIGNvbnN0IG1uYW1lPShEUklMTC5tZXRyaWNzLmZpbmQobT0+bVswXT09PW1rKXx8WycnLCcnXSkgWzFdOwogIHJldHVybiBgPGRpdiBjbGFzcz0iY2FyZCI+PGgzPuq0keqzoCDquLDsl6wgPHNwYW4gY2xhc3M9Im1pbmkiPijssYTrhJAg4pa4IOy6oO2OmOyduCDilrgg7JWg65Oc6re466O5IOKWuCDshozsnqwg7IicKTwvc3Bhbj48L2gzPgogICAgPGRpdiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMHB4Ij48c2VsZWN0IGlkPSJkcmlsbHNlbCIgc3R5bGU9ImZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6NzAwO3BhZGRpbmc6NnB4IDEwcHg7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKTtib3JkZXItcmFkaXVzOjdweDtiYWNrZ3JvdW5kOnZhcigtLWNhcmQpO2NvbG9yOnZhcigtLWluaykiPiR7b3B0c308L3NlbGVjdD48L2Rpdj4KICAgIDxkaXY+JHtib2R5fHwnPGRpdiBjbGFzcz0iY3N1YiI+7ISg7YOdIOq4sOqwhCDrjbDsnbTthLAg7JeG7J2MPC9kaXY+J308L2Rpdj48L2Rpdj5gOwp9CmZ1bmN0aW9uIGZ1bm5lbEJsb2NrKCl7CiAgaWYoIUZVTk5FTHx8IUZVTk5FTC5yb3dzfHwhRlVOTkVMLnJvd3MubGVuZ3RoKSByZXR1cm4gJyc7CiAgY29uc3QgdG90PUZVTk5FTC5yb3dzLmZpbmQocj0+clswXT09PSfsoITssrQnKXx8RlVOTkVMLnJvd3NbMF07CiAgY29uc3Qgc3RhZ2VzPVtbIuyEpOy5mCIsdG90WzFdXSxbIu2ajOybkOqwgOyehSIsdG90WzJdXSxbIuyEnOu5hOyKpCDsnbTsmqkiLHRvdFszXV1dOwogIGNvbnN0IGJhc2U9dG90WzFdfHwxOwogIGNvbnN0IGJhcnM9c3RhZ2VzLm1hcCgocyxpKT0+e2NvbnN0IHA9c1sxXS9iYXNlKjEwMCwgc3RlcD0oaT4wJiZzdGFnZXNbaS0xXVsxXSk/c1sxXS9zdGFnZXNbaS0xXVsxXSoxMDA6bnVsbDsKICAgIHJldHVybiBgPGRpdiBzdHlsZT0ibWFyZ2luOjdweCAwIj48ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47Zm9udC1zaXplOjEycHg7bWFyZ2luLWJvdHRvbTozcHgiPjxzcGFuPiR7c1swXX08L3NwYW4+PHNwYW4gY2xhc3M9ImRpbSI+JHtpPT09MD9gJHtmKHNbMV0pfeuqhSAo6riw7KSAKWA6YCR7ZihzWzFdKX3rqoUgLyDshKTsuZgg64yA67mEICR7cC50b0ZpeGVkKDEpfSVgfTwvc3Bhbj48L2Rpdj48ZGl2IHN0eWxlPSJiYWNrZ3JvdW5kOnZhcigtLWFjY2VudC1zb2Z0KTtib3JkZXItcmFkaXVzOjZweDtoZWlnaHQ6MTNweCI+PGRpdiBzdHlsZT0id2lkdGg6JHtwLnRvRml4ZWQoMSl9JTtoZWlnaHQ6MTAwJTtiYWNrZ3JvdW5kOnZhcigtLWFjY2VudCk7Ym9yZGVyLXJhZGl1czo2cHgiPjwvZGl2PjwvZGl2PjwvZGl2PmA7fSkuam9pbignJyk7CiAgY29uc3Qgcm93cz1GVU5ORUwucm93cy5maWx0ZXIocj0+clswXSE9PSfsoITssrQnKS5tYXAocj0+e2NvbnN0IHMxPXJbMV0/clsyXS9yWzFdKjEwMDowLHMyPXJbMl0/clszXS9yWzJdKjEwMDowOwogICAgcmV0dXJuIGA8dHI+PHRkIGNsYXNzPSJuYW1lIj4ke3JbMF19PC90ZD48dGQgY2xhc3M9ImRpbSI+JHtmKHJbMV0pfTwvdGQ+PHRkPiR7ZihyWzJdKX0gPHNwYW4gY2xhc3M9ImRpbSI+LyAke3MxLnRvRml4ZWQoMCl9JTwvc3Bhbj48L3RkPjx0ZD4ke2YoclszXSl9IDxzcGFuIGNsYXNzPSJkaW0iPi8gJHtzMi50b0ZpeGVkKDApfSU8L3NwYW4+PC90ZD48L3RyPmA7fSkuam9pbignJyk7CiAgY29uc3QgY2h1cm49TWF0aC5tYXgoMCx0b3RbMl0tdG90WzNdKTsKICByZXR1cm4gYDxkaXYgY2xhc3M9ImNhcmQiPjxoMz7si6Dqt5zqs6DqsJ0g7KCE7ZmYIO2NvOuEkCA8c3BhbiBjbGFzcz0ibWluaSI+7KCE7LK0KOq0keqzoCvruYTqtJHqs6ApIOyEpOy5mCDquLDspIAgLyAke0ZVTk5FTC53aW5kb3d9IOy9lO2YuO2KuDwvc3Bhbj48L2gzPgogICAgJHtiYXJzfQogICAgPGRpdiBjbGFzcz0iY3N1YiIgc3R5bGU9Im1hcmdpbjoxMHB4IDAgMnB4Ij7qsIDsnoUg7ZuEIOyEnOu5hOyKpCDrr7jsnbTsmqko7J207YOIKSA8YiBzdHlsZT0iY29sb3I6dmFyKC0taW5rKSI+JHtmKGNodXJuKX3rqoU8L2I+IOKAlCDqsIDsnoUgJHtmKHRvdFsyXSl966qFIOykkSAkeyhjaHVybi8odG90WzJdfHwxKSoxMDApLnRvRml4ZWQoMSl9JTwvZGl2PgogICAgPGRpdiBjbGFzcz0iY3N1YiIgc3R5bGU9Im1hcmdpbjoxNHB4IDAgN3B4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS1pbmspIj7ssYTrhJDrs4Qg7KCE7ZmYIDxzcGFuIGNsYXNzPSJkaW0iIHN0eWxlPSJmb250LXdlaWdodDo0MDAiPijruYTqtJHqs6AgPSDsmKTqsIDri4nCt0NSTSDrk7Eg67mE6rSR6rOgIOycoOyehSk8L3NwYW4+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzY3JvbGwiPjx0YWJsZSBzdHlsZT0ibWluLXdpZHRoOjQ0MHB4Ij48dGhlYWQ+PHRyPjx0aD7ssYTrhJA8L3RoPjx0aD7shKTsuZg8L3RoPjx0aD7qsIDsnoU8YnI+PHNwYW4gY2xhc3M9ImRpbSIgc3R5bGU9ImZvbnQtd2VpZ2h0OjQwMCI+7ISk7LmYIOuMgOu5hDwvc3Bhbj48L3RoPjx0aD7shJzruYTsiqQg7J207JqpPGJyPjxzcGFuIGNsYXNzPSJkaW0iIHN0eWxlPSJmb250LXdlaWdodDo0MDAiPuqwgOyehSDrjIDruYQ8L3NwYW4+PC90aD48L3RyPjwvdGhlYWQ+PHRib2R5PiR7cm93c308L3Rib2R5PjwvdGFibGU+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjc3ViIiBzdHlsZT0ibWFyZ2luLXRvcDo3cHgiPuyghOyytCA9IOq0keqzoCvruYTqtJHqs6Ao7Jik6rCA64uJKSDshKTsuZggLyDshJzruYTsiqQg7J207JqpID0g7ISk7LmYIO2bhCA37J28IOuCtCDssqsg6rKw7KCcwrfsnbTsmqkocHVyY2hhc2Vfc3RhcnRlZCDsoITssrQpIC8g7ISx7IiZIOy9lO2YuO2KuDwvZGl2PjwvZGl2PmA7Cn0KZnVuY3Rpb24gcmVuZGVySm91cm5leShXKXsKICByZXR1cm4gYDxkaXYgY2xhc3M9Im5vdGVsaW5lIj48Yj7si6Dqt5zqs6DqsJ08L2I+IOKAlCDshKTsuZgg4oaSIOqwgOyehSDihpIg7ISc67mE7IqkIOydtOyaqSDtjbzrhJAo7LGE64SQ67OEKSAvIOqwgOyehSDri7nsnbwg7ZaJ64+ZIC8g7J6s67Cp66y4PC9kaXY+YAogICAgK2Z1bm5lbEJsb2NrKCkrb25iQmxvY2soVykrcmV0bkJsb2NrKCk7Cn0KZnVuY3Rpb24gcmVuZGVyKCl7CiAgY29uc3QgVz13aW5kb3dzKCk7CiAgY29uc3QgY21wPSFXLnByaW9yLmxlbmd0aD8i67mE6rWQ6riw6rCEIOyXhuydjCI6KFcuY3VyLmxlbmd0aD09PTE/IuyghOyjvCDrj5nsmpTsnbwg64yA67mEIjpg7KCE7KO8IOuPmeq4sOqwhCDrjIDruYQoJHtXLmN1ci5sZW5ndGh97J28KWApOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJyYW5nZWxhYmVsIikuaW5uZXJIVE1MPWA8Yj4ke0RBWVNbUkFdfSB+ICR7REFZU1tSQl19PC9iPiAvICR7Vy5jdXIubGVuZ3RofeydvCAvICR7Y21wfWA7CiAgY29uc3QgbW9kZVNlZz1gPGRpdiBjbGFzcz0ic2VnIiBzdHlsZT0iYWxpZ24tc2VsZjpmbGV4LXN0YXJ0Ij48YnV0dG9uIGNsYXNzPSIke01PREU9PT0nYWRzJz8nb24nOicnfSIgZGF0YS1tb2RlPSJhZHMiPuq0keqzoCDtmqjsnKg8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSIke01PREU9PT0nam91cm5leSc/J29uJzonJ30iIGRhdGEtbW9kZT0iam91cm5leSI+7Iug6rec6rOg6rCdPC9idXR0b24+PC9kaXY+YDsKICBpZihNT0RFPT09J2pvdXJuZXknKXsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ0YWJzIikuaW5uZXJIVE1MPW1vZGVTZWc7CiAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgidmlldyIpLmlubmVySFRNTD1yZW5kZXJKb3VybmV5KFcpOwogICAgcmV0dXJuOwogIH0KICBjb25zdCBhY3RpdmU9QlVDS0VUUy5maWx0ZXIoYj0+YnVja2V0QWN0aXZlKGIuaWQpKTsKICBpZihUQUIhPT0ic3VtbWFyeSImJiFhY3RpdmUuc29tZShiPT5iLmlkPT09VEFCKSlUQUI9InN1bW1hcnkiOwogIGNvbnN0IHRhYnM9W1sic3VtbWFyeSIsIuyalOyVvSJdXS5jb25jYXQoYWN0aXZlLm1hcChiPT5bYi5pZCxiLmxhYmVsXSkpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ0YWJzIikuaW5uZXJIVE1MPW1vZGVTZWcrYDxkaXYgY2xhc3M9InNlZyIgc3R5bGU9ImFsaWduLXNlbGY6ZmxleC1zdGFydDtmbGV4LXdyYXA6d3JhcCI+YCt0YWJzLm1hcCh0PT5gPGJ1dHRvbiBjbGFzcz0idGFiICR7dFswXT09PVRBQj8nb24nOicnfSIgZGF0YS10PSIke3RbMF19Ij4ke3RbMV19PC9idXR0b24+YCkuam9pbignJykrYDwvZGl2PmA7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInZpZXciKS5pbm5lckhUTUw9KFRBQj09PSJzdW1tYXJ5Iik/cmVuZGVyU3VtbWFyeSgpOnJlbmRlckJ1Y2tldChUQUIpOwp9CihmdW5jdGlvbiBpbml0RGF0ZXMoKXsKICBjb25zdCBmZD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgiZnJvbURhdGUiKSwgdGQ9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInRvRGF0ZSIpOwogIGZkLm1pbj10ZC5taW49REFZU1swXTsgZmQubWF4PXRkLm1heD1EQVlTW0RBWVMubGVuZ3RoLTFdOwogIGZkLnZhbHVlPURBWVNbUkFdOyB0ZC52YWx1ZT1EQVlTW1JCXTsKICBmdW5jdGlvbiBhcHBseSgpewogICAgbGV0IGE9REFZUy5pbmRleE9mKGZkLnZhbHVlKSwgYj1EQVlTLmluZGV4T2YodGQudmFsdWUpOwogICAgaWYoYTwwKWE9UkE7IGlmKGI8MCliPVJCOyBpZihhPmIpe2NvbnN0IHQ9YTthPWI7Yj10O30KICAgIFJBPWE7IFJCPWI7IGZkLnZhbHVlPURBWVNbUkFdOyB0ZC52YWx1ZT1EQVlTW1JCXTsgcmVuZGVyKCk7CiAgfQogIGZkLmFkZEV2ZW50TGlzdGVuZXIoImNoYW5nZSIsYXBwbHkpOyB0ZC5hZGRFdmVudExpc3RlbmVyKCJjaGFuZ2UiLGFwcGx5KTsKICAvLyDruaDrpbgg7ISg7YOdOiDstZzqt7wgNy8xNC8zMOydvAogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJxdWlja3BpY2siKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsZT0+e2NvbnN0IGJ0bj1lLnRhcmdldC5jbG9zZXN0KCJidXR0b24iKTtpZighYnRuKXJldHVybjtjb25zdCBkPStidG4uZGF0YXNldC5kO1JCPURBWVMubGVuZ3RoLTE7UkE9TWF0aC5tYXgoMCxEQVlTLmxlbmd0aC1kKTtmZC52YWx1ZT1EQVlTW1JBXTt0ZC52YWx1ZT1EQVlTW1JCXTtyZW5kZXIoKTt9KTsKfSkoKTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInRhYnMiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsZT0+e2NvbnN0IGI9ZS50YXJnZXQuY2xvc2VzdCgiLnRhYiIpO2lmKCFiKXJldHVybjtUQUI9Yi5kYXRhc2V0LnQ7cmVuZGVyKCk7fSk7CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ0YWJzIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGU9Pntjb25zdCBiPWUudGFyZ2V0LmNsb3Nlc3QoIltkYXRhLW1vZGVdIik7aWYoIWIpcmV0dXJuO01PREU9Yi5kYXRhc2V0Lm1vZGU7cmVuZGVyKCk7fSk7CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ2aWV3IikuYWRkRXZlbnRMaXN0ZW5lcigiY2hhbmdlIixlPT57Y29uc3Qgcz1lLnRhcmdldC5jbG9zZXN0KCIjZHJpbGxzZWwiKTtpZighcylyZXR1cm47RFJJTExNRVRSSUM9cy52YWx1ZTtyZW5kZXIoKTt9KTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInZpZXciKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsZT0+e2NvbnN0IGI9ZS50YXJnZXQuY2xvc2VzdCgiW2RhdGEtZGV4cF0iKTtpZighYilyZXR1cm47Y29uc3Qgaz1kZWNvZGVVUklDb21wb25lbnQoYi5kYXRhc2V0LmRleHApO0RFWFAuaGFzKGspP0RFWFAuZGVsZXRlKGspOkRFWFAuYWRkKGspO3JlbmRlcigpO30pOwpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgidmlldyIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixlPT57Y29uc3QgYj1lLnRhcmdldC5jbG9zZXN0KCJbZGF0YS1idHJlbmRdIik7aWYoIWIpcmV0dXJuO0JUUks9Yi5kYXRhc2V0LmJ0cmVuZDtyZW5kZXIoKTt9KTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInZpZXciKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsZT0+e2NvbnN0IGI9ZS50YXJnZXQuY2xvc2VzdCgiW2RhdGEtdHJlbmRdIik7aWYoIWIpcmV0dXJuO1RSRU5ES0VZPWIuZGF0YXNldC50cmVuZDtyZW5kZXIoKTt9KTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInZpZXciKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsZT0+e2NvbnN0IGI9ZS50YXJnZXQuY2xvc2VzdCgiW2RhdGEtY3JdIik7aWYoIWIpcmV0dXJuO0NSU0hPV1tiLmRhdGFzZXQuY3JdPSFDUlNIT1dbYi5kYXRhc2V0LmNyXTtyZW5kZXIoKTt9KTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoImZvb3QiKS5pbm5lckhUTUw9IuyalOyVvT3soITssrQg7Jyg7J6FKOq0keqzoCvruYTqtJHqs6ApLCDqtJHqs6A967O06528IC8g67KE7YK3IO2DrT3qtJHqs6Ag6riw7Jes66eMKOuqqeyggSDsuqDtjpjsnbgpLCDquLDtg4Ag6rSR6rOgPeu5hOyaqSDrr7jsl7Drj5ko7Iuk7KCB66eMKTxicj7qtJHqs6Av6rSR6rOg67mEL0NQQT3rqqnsoIEg7Lqg7Y6Y7J2466eMLCDruYTspJE97KCE7LK0IOq0keqzoCDquLDsl6wgLyDsmKjrs7TrlKkg7KCE7ZmY7JyoPeqwgOyehSDri7nsnbwg7L2U7Zi47Yq4IOq4sOykgDxicj7rs4DtmZTsnKg97KCE7KO8IOuPmeq4sOqwhCjsmpTsnbwg66ee7LakKSDrjIDruYQgKOyngO2RnOKGkSDstIjroZ0sIOu5hOyaqeKGkSDruajqsJUpIC8g6riw6rCE7J2AIOuLrOugpeycvOuhnCDshKDtg50o7LWc6re8IDYw7J28IOuCtCksIOyDgeuLqCBb6rSR6rOgIO2aqOycqF3Ct1vqs6DqsJ0g7Jes7KCVXSDrt7Ag7KCE7ZmYIC8g7YOt7J2AIOyLpOyggSAw7J2066m0IOyekOuPmSDsiKjquYAiOwpjb25zdCBfZ209ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoImdlbm1ldGEiKTsgaWYoX2dtKSBfZ20udGV4dENvbnRlbnQ9REFUQS5nZW5lcmF0ZWR8fCIiOwpyZW5kZXIoKTsKPC9zY3JpcHQ+CjwvYm9keT4KPC9odG1sPgo=").decode("utf-8")


if __name__=="__main__":
    main()
