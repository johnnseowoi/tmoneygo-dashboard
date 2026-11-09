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
  "taxi":   ["택시","tROAS","부산택시","지방권","빠른호출"],
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
    # 소재(ad_creative)는 제외 — 비용은 소재명이 있고 이벤트는 빈값이라 키가 어긋나 비용이 버려짐. 애드그룹 단위로 맞춤
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
    for (ch,cp,ag,cr) in (set(S_call)|set(S_comp)|set(S_su)|set(S_cost)):
      cc=S_call.get((ch,cp,ag,cr),[0]*n); mm=S_comp.get((ch,cp,ag,cr),[0]*n)
      ss=S_su.get((ch,cp,ag,cr),[0]*n);  kk=S_cost.get((ch,cp,ag,cr),[0]*n)
      if sum(cc)==0 and sum(mm)==0:
        if sum(kk)==0 or not is_purpose(bid, cp): continue    # 이벤트 없는 행은 '그 버킷 목적 캠페인 비용'만 유지(비용 누락 방지 + 버킷 누수 차단)
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
      def ret_kind(start_ev, ret_ev, period, day0=False):
        body={"from":cfrm.isoformat(),"to":cto.isoformat(),"granularity":"day","intervalsPeriod":period,
              "measurementOption":"general_retention","startEvents":[start_ev],"returnEvents":[ret_ev],
              "groupBy":{"fields":["channel"]}}
        j=retention_task(body) or retention_task(dict(body,filters=[],sorts=[],pagination={"skip":0,"size":1000}))
        acc={}
        if not j: return acc
        rp=j.get("retention") or (j.get("reportData") or {}).get("retention") or {}
        for g in ((rp.get("data") or {}).get("rows") or []):
          k=kind_of((g.get("metadata") or ["?"])[0]); tot=g.get("total") or {}
          cohort=((tot.get("totalValue") or {}).get("value",0)) or 0
          if day0:
            cv=0
            for v in (tot.get("values") or []):
              if v.get("periodIndex")==0: cv=v.get("value",0) or 0   # 당일 유니크(반복이벤트 중복집계 방지)
          else:
            cv=sum((v.get("value",0) or 0) for v in (tot.get("values") or []))  # 1회성 이벤트는 누적=순전환
          a=acc.setdefault(k,[0,0]); a[0]+=cohort; a[1]+=cv
        return acc
      # 설치→가입: 설치 코호트(재설치·기존회원 포함). 가입은 1회성이라 누적=순전환
      S=ret_kind(M_INSTALL, M_SIGNUP, 7, day0=False)
      # 서비스 이용은 '가입 코호트' 기준 당일 첫 이용 전환율로 재야 신규고객 퍼널이 제대로 좁아짐
      P=ret_kind(M_SIGNUP, M_TAXI_CALL, 1, day0=True)
      if not S: return None
      LAB={"google":"google.ads","insta":"insta","etc":"기타광고","org":"비광고"}
      out=[]; T=[0,0,0]
      for k in ("google","insta","etc","org"):
        if S.get(k) and S[k][0]>0:
          inst=S[k][0]; sg=S[k][1]
          rate_sp=(P[k][1]/P[k][0]) if (P.get(k) and P[k][0]) else 0   # 가입→당일이용 전환율(채널별)
          sv=round(sg*rate_sp)                                        # 이용 = 가입 × 전환율 → 반드시 가입 이하
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
TEMPLATE_HTML=_b64.b64decode("PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9ImtvIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLCB2aWV3cG9ydC1maXQ9Y292ZXIiPgo8dGl0bGU+7Yuw66i464uIR08g6rSR6rOgIOyEseqzvCDthrXtlanrt7A8L3RpdGxlPgo8c3R5bGU+CiAgOnJvb3R7LS1iZzojZjRmNWY3Oy0tY2FyZDojZmZmOy0tbGluZTojZWFlN2YzOy0taW5rOiMyYTI2NDA7LS1zdWI6IzhiODZhMDstLWFjY2VudDojODM3MGU2Oy0tYWNjZW50LXNvZnQ6I2YxZWRmYzstLWdvb2Q6IzJlOWU1YjstLWJhZDojYzA0NTNiOy0tbXV0ZWQ6I2JkYjhjYzstLW9yZzojZDZkMmUyOy0tc2hhZG93OjAgMXB4IDJweCByZ2JhKDQyLDM4LDY0LC4wNSksMCAycHggOHB4IHJnYmEoNDIsMzgsNjQsLjAzNSk7Ym94LXNpemluZzpib3JkZXItYm94O3BhZGRpbmctdG9wOmVudihzYWZlLWFyZWEtaW5zZXQtdG9wLDBweCk7cGFkZGluZy1ib3R0b206ZW52KHNhZmUtYXJlYS1pbnNldC1ib3R0b20sMHB4KX0KICBAbWVkaWEgKHByZWZlcnMtY29sb3Itc2NoZW1lOmRhcmspezpyb290Om5vdChbZGF0YS10aGVtZT0ibGlnaHQiXSl7LS1iZzojMTUxMjFkOy0tY2FyZDojMWUxYTJhOy0tbGluZTojMmYyOTQxOy0taW5rOiNlY2U5ZjU7LS1zdWI6Izk0OGVhYTstLWFjY2VudDojYTU5M2YwOy0tYWNjZW50LXNvZnQ6IzI3MWQzZDstLW11dGVkOiM1ZjU4NzU7LS1vcmc6IzQxM2E1NDstLXNoYWRvdzowIDFweCAycHggcmdiYSgwLDAsMCwuNCksMCAycHggMTBweCByZ2JhKDAsMCwwLC4zKX19CiAgKntib3gtc2l6aW5nOmJvcmRlci1ib3g7bWFyZ2luOjA7cGFkZGluZzowfQogIGJvZHl7YmFja2dyb3VuZDp2YXIoLS1iZyk7Y29sb3I6dmFyKC0taW5rKTtmb250LWZhbWlseToiQXBwbGUgU0QgR290aGljIE5lbyIsIk1hbGd1biBHb3RoaWMiLCJQcmV0ZW5kYXJkIixzeXN0ZW0tdWksc2Fucy1zZXJpZjtmb250LXNpemU6MTRweDtsaW5lLWhlaWdodDoxLjU7cGFkZGluZzoxNnB4fQogIC53cmFwe21heC13aWR0aDoxMTIwcHg7bWFyZ2luOjAgYXV0b30KICBoMXtmb250LXNpemU6MjBweDtmb250LXdlaWdodDo4MDA7bGV0dGVyLXNwYWNpbmc6LTAuNHB4fQogIC5oZHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2ZsZXgtd3JhcDp3cmFwO2dhcDo4cHg7cGFkZGluZy1ib3R0b206MTNweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCB2YXIoLS1saW5lKX0KICAuaGQtdGl0bGV7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OXB4fQogIC5oZCAuZG90e3dpZHRoOjlweDtoZWlnaHQ6OXB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6dmFyKC0tYWNjZW50KTtib3gtc2hhZG93OjAgMCAwIDRweCB2YXIoLS1hY2NlbnQtc29mdCl9CiAgLmhkLW1ldGF7Zm9udC1zaXplOjExLjVweDtjb2xvcjp2YXIoLS1tdXRlZCk7Zm9udC13ZWlnaHQ6NjAwfQogIC5iYW5uZXJ7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQtc29mdCk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKTtib3JkZXItcmFkaXVzOjEycHg7cGFkZGluZzoxMnB4IDE0cHg7Y29sb3I6dmFyKC0tYWNjZW50KTtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjEzcHg7bWFyZ2luLWJvdHRvbToxMnB4fQogIC5jb250cm9sc3tkaXNwbGF5OmZsZXg7ZmxleC13cmFwOndyYXA7Z2FwOjEwcHg7YWxpZ24taXRlbXM6Y2VudGVyO21hcmdpbjoxNHB4IDB9CiAgLnNlZ3tkaXNwbGF5OmZsZXg7Z2FwOjNweDtiYWNrZ3JvdW5kOnZhcigtLWNhcmQpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czo5OTlweDtwYWRkaW5nOjRweH0KICAuc2VnIGJ1dHRvbntib3JkZXI6bm9uZTtiYWNrZ3JvdW5kOm5vbmU7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTNweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tc3ViKTtwYWRkaW5nOjdweCAxNHB4O2JvcmRlci1yYWRpdXM6OTk5cHg7Y3Vyc29yOnBvaW50ZXJ9CiAgLnNlZyBidXR0b24ub257YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQpO2NvbG9yOiNmZmZ9CiAgLnJhbmdlbGFiZWx7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tc3ViKX0ucmFuZ2VsYWJlbCBie2NvbG9yOnZhcigtLWluayl9CiAgLmRhdGVwaWNre2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjZweDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tc3ViKX0KICAuZGF0ZXBpY2sgaW5wdXR7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTIuNXB4O2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjp2YXIoLS1pbmspO2JhY2tncm91bmQ6dmFyKC0tY2FyZCk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKTtib3JkZXItcmFkaXVzOjdweDtwYWRkaW5nOjZweCA4cHg7Y3Vyc29yOnBvaW50ZXJ9CiAgLmRhdGVwaWNrIGlucHV0Ojotd2Via2l0LWNhbGVuZGFyLXBpY2tlci1pbmRpY2F0b3J7Y3Vyc29yOnBvaW50ZXI7b3BhY2l0eTouNn0KICAudGFic3tkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2dhcDo5cHg7bWFyZ2luLWJvdHRvbToxNHB4fQogIC50YWJ7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzo3cHggMTZweDtib3JkZXItcmFkaXVzOjk5OXB4O2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTNweDtjb2xvcjp2YXIoLS1zdWIpO2N1cnNvcjpwb2ludGVyO3doaXRlLXNwYWNlOm5vd3JhcDtib3JkZXI6bm9uZTtiYWNrZ3JvdW5kOm5vbmU7Zm9udC1mYW1pbHk6aW5oZXJpdH0KICAudGFiLm9ue2JhY2tncm91bmQ6dmFyKC0tYWNjZW50KTtjb2xvcjojZmZmfQogIC5ub3RlbGluZXtmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS1zdWIpO21hcmdpbjoycHggMCAxMHB4fQogIC5rcGlze2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KGF1dG8tZml0LG1pbm1heCgxNTBweCwxZnIpKTtnYXA6MTJweDttYXJnaW4tYm90dG9tOjE2cHh9CiAgLmtwaXtiYWNrZ3JvdW5kOnZhcigtLWNhcmQpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxNHB4O3BhZGRpbmc6MTRweCAxNnB4O2JveC1zaGFkb3c6dmFyKC0tc2hhZG93KX0KICAua3BpLnByaW1hcnl7Ym9yZGVyLWNvbG9yOnZhcigtLWFjY2VudCk7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQtc29mdCl9CiAgLmtwaSAudHtmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS1zdWIpO2ZvbnQtd2VpZ2h0OjYwMH0KICAua3BpIC5iaWd7Zm9udC1zaXplOjI0cHg7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi10b3A6NnB4O2xldHRlci1zcGFjaW5nOi0wLjZweH0KICAua3BpIC5iaWcgLnV7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tc3ViKTtmb250LXdlaWdodDo2MDA7bWFyZ2luLWxlZnQ6MnB4fQogIC5rcGkgLmRlbHRhe2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMDttYXJnaW4tdG9wOjRweH0KICAudXB7Y29sb3I6dmFyKC0tZ29vZCl9LmRvd257Y29sb3I6dmFyKC0tYmFkKX0uZmxhdHtjb2xvcjp2YXIoLS1tdXRlZCl9CiAgLmNhcmR7YmFja2dyb3VuZDp2YXIoLS1jYXJkKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTRweDtwYWRkaW5nOjE3cHggMThweDttYXJnaW4tYm90dG9tOjEycHg7Ym94LXNoYWRvdzp2YXIoLS1zaGFkb3cpfQogIC5jYXJkIGgze2ZvbnQtc2l6ZToxNC41cHg7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi1ib3R0b206M3B4O2xldHRlci1zcGFjaW5nOi0wLjJweH0KICAuY2FyZCBoMyAubWluaXtjb2xvcjp2YXIoLS1zdWIpO2ZvbnQtd2VpZ2h0OjYwMDtmb250LXNpemU6MTJweH0KICAuY2FyZCAuY3N1Yntmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS1zdWIpO21hcmdpbi1ib3R0b206MTJweH0KICAuc3BsaXRiYXJ7ZGlzcGxheTpmbGV4O2hlaWdodDoyNnB4O2JvcmRlci1yYWRpdXM6N3B4O292ZXJmbG93OmhpZGRlbjttYXJnaW4tYm90dG9tOjEwcHg7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKX0KICAuc3BsaXRiYXIgLmFke2JhY2tncm91bmQ6dmFyKC0tYWNjZW50KX0uc3BsaXRiYXIgLm9yZ3tiYWNrZ3JvdW5kOnZhcigtLW9yZyl9CiAgLnNwbGl0YmFyIHNwYW57ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2NvbG9yOiNmZmY7Zm9udC1zaXplOjExcHg7Zm9udC13ZWlnaHQ6ODAwO21pbi13aWR0aDowfQogIC5zcGxpdGJhciAub3Jne2NvbG9yOnZhcigtLWluayl9CiAgLmNocm93e2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MTUwcHggMWZyIDkycHg7Z2FwOjEwcHg7YWxpZ24taXRlbXM6Y2VudGVyO3BhZGRpbmc6NXB4IDA7Zm9udC1zaXplOjEyLjVweH0KICAuY2hyb3cuZXhwcm93e2N1cnNvcjpwb2ludGVyfQogIC5jaHJvdyAubm17ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NXB4fQogIC5jaHJvdyAuYWR0YWd7Zm9udC1zaXplOjkuNXB4O2ZvbnQtd2VpZ2h0OjgwMDtwYWRkaW5nOjFweCA1cHg7Ym9yZGVyLXJhZGl1czo0cHg7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQtc29mdCk7Y29sb3I6dmFyKC0tYWNjZW50KX0KICAuYXJyb3d7ZGlzcGxheTppbmxpbmUtYmxvY2s7d2lkdGg6MTFweDtmb250LXNpemU6OXB4O2NvbG9yOnZhcigtLXN1Yik7dHJhbnNpdGlvbjp0cmFuc2Zvcm0gLjE1c30KICAudHJhY2t7aGVpZ2h0OjhweDtiYWNrZ3JvdW5kOnZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6NXB4O292ZXJmbG93OmhpZGRlbn0KICAudHJhY2s+c3BhbntkaXNwbGF5OmJsb2NrO2hlaWdodDoxMDAlO2JvcmRlci1yYWRpdXM6NXB4O2JhY2tncm91bmQ6dmFyKC0tb3JnKX0KICAudHJhY2s+c3Bhbi5hZGZ7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQpfQogIC5jaHJvdyAudmFse3RleHQtYWxpZ246cmlnaHQ7Y29sb3I6dmFyKC0tc3ViKX0uY2hyb3cgLnZhbCBie2NvbG9yOnZhcigtLWluayl9CiAgLmNoc3Vie3BhZGRpbmc6MnB4IDAgOHB4IDIwcHg7bWFyZ2luOjAgMCA0cHggOHB4O2JvcmRlci1sZWZ0OjJweCBzb2xpZCB2YXIoLS1hY2NlbnQtc29mdCl9CiAgLnN1YnJvd3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciBhdXRvO2dhcDoxMHB4O2ZvbnQtc2l6ZToxMS41cHg7Y29sb3I6dmFyKC0tc3ViKTtwYWRkaW5nOjNweCAwfQogIC5zdWJyb3cgLm5tMntjb2xvcjp2YXIoLS1pbmspfQogIC5lZmZzdWJ7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDoxMHB4O2ZvbnQtc2l6ZToxMS41cHg7Y29sb3I6dmFyKC0tc3ViKTtwYWRkaW5nOjRweCAwO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHZhcigtLWFjY2VudC1zb2Z0KX0KICAuZWZmc3ViIC5ubTJ7Y29sb3I6dmFyKC0taW5rKTtmb250LXdlaWdodDo2MDB9CiAgLmNvbnZncmlke2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KGF1dG8tZml0LG1pbm1heCgxMzBweCwxZnIpKTtnYXA6MTBweH0KICAuY29udntib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6OXB4O3BhZGRpbmc6MTFweCAxMnB4fQogIC5jb252IC50e2ZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLXN1Yik7Zm9udC13ZWlnaHQ6NjAwfQogIC5jb252IC5iaWd7Zm9udC1zaXplOjIwcHg7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi10b3A6NHB4fQogIC5jb252IC5iaWcgLnV7Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tc3ViKTttYXJnaW4tbGVmdDoxcHh9CiAgLmNvbnYgLnN1YjJ7Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tc3ViKTttYXJnaW4tdG9wOjFweH0KICAuc2Nyb2xse292ZXJmbG93LXg6YXV0b30KICB0YWJsZXt3aWR0aDoxMDAlO2JvcmRlci1jb2xsYXBzZTpjb2xsYXBzZTtmb250LXNpemU6MTJweDttaW4td2lkdGg6NjgwcHh9CiAgdGgsdGR7cGFkZGluZzo5cHggMTBweDt0ZXh0LWFsaWduOnJpZ2h0O2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHZhcigtLWxpbmUpO3doaXRlLXNwYWNlOm5vd3JhcH0KICB0aDpmaXJzdC1jaGlsZCx0ZDpmaXJzdC1jaGlsZHt0ZXh0LWFsaWduOmxlZnR9CiAgdGhlYWQgdGh7Y29sb3I6dmFyKC0tc3ViKTtmb250LXdlaWdodDo3MDB9CiAgdGgubGgsdGQubGh7dGV4dC1hbGlnbjpsZWZ0fQogIHRkLm5hbWV7bWF4LXdpZHRoOjI2MHB4O3doaXRlLXNwYWNlOm5vcm1hbDt3b3JkLWJyZWFrOmJyZWFrLXdvcmQ7bGluZS1oZWlnaHQ6MS4zNTt0ZXh0LWFsaWduOmxlZnR9CiAgdHIuZXhwcm93e2N1cnNvcjpwb2ludGVyfQogIC5rcGljb2x7Zm9udC13ZWlnaHQ6ODAwO2JhY2tncm91bmQ6cmdiYSgxMzEsMTEyLDIzMCwwLjA2KX10aC5rcGljb2x7Y29sb3I6dmFyKC0tc3ViKTtiYWNrZ3JvdW5kOnJnYmEoMTMxLDExMiwyMzAsMC4wNil9CiAgdGQuZGlte2NvbG9yOnZhcigtLW11dGVkKX0KICAuZGVsdGEtc3tmb250LXNpemU6MTAuNXB4O2ZvbnQtd2VpZ2h0OjcwMDttYXJnaW4tbGVmdDo1cHh9CiAgLmJhZGdle2Rpc3BsYXk6aW5saW5lLWJsb2NrO2ZvbnQtc2l6ZToxMC41cHg7Zm9udC13ZWlnaHQ6NzAwO3BhZGRpbmc6MXB4IDdweDtib3JkZXItcmFkaXVzOjVweH0KICAuYmFkZ2UucmV7YmFja2dyb3VuZDojZWZlYWZjO2NvbG9yOiM3YTYzZDZ9LmJhZGdlLnRye2JhY2tncm91bmQ6I2VlZjBmMztjb2xvcjojN2E4NTkzfQogIEBtZWRpYSAocHJlZmVycy1jb2xvci1zY2hlbWU6ZGFyayl7OnJvb3Q6bm90KFtkYXRhLXRoZW1lPSJsaWdodCJdKSAuYmFkZ2UucmV7YmFja2dyb3VuZDojMjcxZDNkO2NvbG9yOiNhOTkxZjV9OnJvb3Q6bm90KFtkYXRhLXRoZW1lPSJsaWdodCJdKSAuYmFkZ2UudHJ7YmFja2dyb3VuZDojMjQyYjM0fX0KICAuZGV0YWlsdGR7YmFja2dyb3VuZDp2YXIoLS1iZyk7cGFkZGluZzowfS5kZXRhaWx0ZCAuaW5uZXJ7cGFkZGluZzo0cHggMTBweCAxMHB4IDI2cHh9CiAgLnRvdHJvdyB0ZHtib3JkZXItdG9wOjJweCBzb2xpZCB2YXIoLS1saW5lKTtmb250LXdlaWdodDo4MDB9CiAgLnNlY2hkLXJvdyB0ZHtiYWNrZ3JvdW5kOnZhcigtLWFjY2VudC1zb2Z0KTtjb2xvcjp2YXIoLS1hY2NlbnQpO2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTIuNXB4O3RleHQtYWxpZ246bGVmdCFpbXBvcnRhbnQ7cGFkZGluZzo5cHggOHB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHZhcigtLWxpbmUpfQogIC5zZWdoZHtmb250LXNpemU6MTMuNXB4O2ZvbnQtd2VpZ2h0OjgwMDttYXJnaW46MTZweCAwIDhweH0uc2VnaGQgLnNlZ3N1Yntmb250LXNpemU6MTEuNXB4O2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjp2YXIoLS1zdWIpO21hcmdpbi1sZWZ0OjdweH0KICAuZm9vdHtmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS1tdXRlZCk7bWFyZ2luLXRvcDoxNnB4O2xpbmUtaGVpZ2h0OjEuNn0KPC9zdHlsZT4KPC9oZWFkPgo8Ym9keT4KPGRpdiBjbGFzcz0id3JhcCI+CiAgPGhlYWRlciBjbGFzcz0iaGQiPjxkaXYgY2xhc3M9ImhkLXRpdGxlIj48c3BhbiBjbGFzcz0iZG90Ij48L3NwYW4+PGgxPu2LsOuouOuLiEdPIOq0keqzoCDshLHqs7wg7Ya17ZWp67ewPC9oMT48L2Rpdj48ZGl2IGNsYXNzPSJoZC1tZXRhIiBpZD0iZ2VubWV0YSI+PC9kaXY+PC9oZWFkZXI+CiAgPGRpdiBjbGFzcz0iY29udHJvbHMiPgogICAgPGRpdiBjbGFzcz0iZGF0ZXBpY2siPuq4sOqwhCA8aW5wdXQgdHlwZT0iZGF0ZSIgaWQ9ImZyb21EYXRlIj4gfiA8aW5wdXQgdHlwZT0iZGF0ZSIgaWQ9InRvRGF0ZSI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzZWciIGlkPSJxdWlja3BpY2siPjxidXR0b24gZGF0YS1kPSIxIj4x7J28PC9idXR0b24+PGJ1dHRvbiBkYXRhLWQ9IjciPjfsnbw8L2J1dHRvbj48YnV0dG9uIGRhdGEtZD0iMTQiPjE07J28PC9idXR0b24+PGJ1dHRvbiBkYXRhLWQ9IjMwIj4zMOydvDwvYnV0dG9uPjxidXR0b24gZGF0YS1kPSI2MCI+NjDsnbw8L2J1dHRvbj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InJhbmdlbGFiZWwiIGlkPSJyYW5nZWxhYmVsIj48L2Rpdj4KICA8L2Rpdj4KICA8ZGl2IGNsYXNzPSJ0YWJzIiBpZD0idGFicyI+PC9kaXY+CiAgPGRpdiBpZD0idmlldyI+PC9kaXY+CiAgPGRpdiBjbGFzcz0iZm9vdCIgaWQ9ImZvb3QiPjwvZGl2Pgo8L2Rpdj4KPHNjcmlwdD4KLyogPT09PT0g642w7J207YSwIOyjvOyehSAoZ2VuZXJhdGUucHnqsIAgX19EQVRBX18g66W8IOyLpOy4oSBKU09O7Jy866GcIOq1kOyytCkgPT09PT0gKi8KY29uc3QgREFUQSA9IF9fREFUQV9fOwpjb25zdCBCVUNLRVRTID0gREFUQS5idWNrZXRzOwpjb25zdCBEQVlTID0gREFUQS5kYXlzOwpjb25zdCBDSCA9IERBVEEuY2hhbm5lbHM7CmNvbnN0IFRPVEFMUyA9IERBVEEudG90YWxzOwpjb25zdCBUQVhJVE9UID0gREFUQS50YXhpdG90Owpjb25zdCBDQU1QID0gREFUQS5jYW1wOwpjb25zdCBPTkIgPSBEQVRBLm9uYjsKY29uc3QgQURFRkYgPSBEQVRBLmFkZWZmOwpjb25zdCBST1dTID0gREFUQS5yb3dzOwpjb25zdCBCVE9UID0gREFUQS5idG90fHx7fTsKY29uc3QgQ1JTSE9XID0ge307CmNvbnN0IEZVTk5FTCA9IERBVEEuZnVubmVsfHxudWxsOwpjb25zdCBEUklMTCA9IERBVEEuZHJpbGx8fG51bGw7CmNvbnN0IFJFVE4gPSBEQVRBLnJldG58fG51bGw7CmxldCBNT0RFPSJhZHMiOyBsZXQgRFJJTExNRVRSSUM9InN1IjsgY29uc3QgREVYUD1uZXcgU2V0KCk7IGxldCBCVFJLPSJib3RoIjsgbGV0IExHSz0iYWxsIjsKCi8qID09PT09IOqzoOyglSDroZzsp4EgPT09PT0gKi8KY29uc3QgZj1uPT4obj09bnVsbCk/IuKAlCI6TWF0aC5yb3VuZChuKS50b0xvY2FsZVN0cmluZygna28tS1InKTsKbGV0IFJBPU1hdGgubWF4KDAsREFZUy5sZW5ndGgtNyksIFJCPURBWVMubGVuZ3RoLTE7ICAgLy8g7ISg7YOdIOq4sOqwhCjsnbwg7J24642x7IqkKQpmdW5jdGlvbiB3aW5kb3dzKCl7Y29uc3QgY3VyPVtdO2ZvcihsZXQgaT1SQTtpPD1SQjtpKyspY3VyLnB1c2goaSk7Y29uc3Qgbj1jdXIubGVuZ3RoO2NvbnN0IHNoPU1hdGguY2VpbChuLzcpKjc7Y29uc3QgcHJpb3I9Y3VyLm1hcChpPT5pLXNoKTtyZXR1cm4ge2N1cixwcmlvcjpwcmlvci5ldmVyeShpPT5pPj0wKT9wcmlvcjpbXX07fQpjb25zdCBzdW09KGFycixpZHgpPT5pZHgucmVkdWNlKChhLGkpPT5hKyhhcnJbaV18fDApLDApOwpjb25zdCBwY3Q9KGMscCk9PihwPT1udWxsfHxwPT09MCk/bnVsbDooYy1wKS9wKjEwMDsKZnVuY3Rpb24gZGVsdGFTcGFuKGMscCxnb29kVXA9dHJ1ZSxzbWFsbD1mYWxzZSl7Y29uc3Qgdj1wY3QoYyxwKTtpZih2PT1udWxsKXJldHVybiAnJztjb25zdCBjbHM9KHY9PT0wKT8nZmxhdCc6KCh2PjA9PT1nb29kVXApPyd1cCc6J2Rvd24nKSxhcj12PjA/J+KWsic6KHY8MD8n4pa8JzonJyk7cmV0dXJuIGA8c3BhbiBjbGFzcz0iJHtjbHN9ICR7c21hbGw/J2RlbHRhLXMnOidkZWx0YSd9Ij4ke2FyfSR7TWF0aC5hYnModikudG9GaXhlZCgwKX0lPC9zcGFuPmA7fQpmdW5jdGlvbiBrcGkobGFiZWwsY3VyLHByaW9yLG89e30pe3JldHVybiBgPGRpdiBjbGFzcz0ia3BpICR7by5wcmltYXJ5PydwcmltYXJ5JzonJ30iPjxkaXYgY2xhc3M9InQiPiR7bGFiZWx9PC9kaXY+PGRpdiBjbGFzcz0iYmlnIj4ke28ud29uPyfigqknOicnfSR7ZihjdXIpfSR7by51bml0P2A8c3BhbiBjbGFzcz0idSI+JHtvLnVuaXR9PC9zcGFuPmA6Jyd9PC9kaXY+JHtvLm5vZGVsdGE/Jyc6ZGVsdGFTcGFuKGN1cixwcmlvcixvLmdvb2RVcCE9PWZhbHNlKX0ke28uc3VifHwnJ308L2Rpdj5gO30KZnVuY3Rpb24gY2hUb3RhbChrZXksaWR4KXtyZXR1cm4gQ0gucmVkdWNlKChhLGMpPT5hK3N1bShjW2tleV0saWR4KSwwKTt9Ci8qIC0tLS0g7J287J6Q67OEIOy2lOydtCDrnbzsnbjssKjtirggKOuCtOyepSBTVkcsIDLsnbwg7J207IOB7J28IOuVjCkgLS0tLSAqLwpsZXQgVFJFTkRLRVk9InN1IjsKZnVuY3Rpb24gd2QoZCxzaG93Vyl7Y29uc3Qgcz1kLnNsaWNlKDUpOyBpZighc2hvd1cpIHJldHVybiBzOyBjb25zdCBkdD1uZXcgRGF0ZShkKydUMDA6MDA6MDAnKTsgcmV0dXJuIHMrJygnKyLsnbzsm5TtmZTsiJjrqqnquIjthqAiW2R0LmdldERheSgpXSsnKSc7fQpmdW5jdGlvbiB0cmVuZENoYXJ0KGRheXMsIHNlcmllcyl7CiAgY29uc3Qgbj1kYXlzLmxlbmd0aCwgUz1zZXJpZXMubGVuZ3RoLCBQTD00MixQUj0xMixQVD0yNCxQQj0zMiwgSD0xODQ7CiAgY29uc3QgZmM9dj0+e3Y9TWF0aC5yb3VuZCh2KTsgaWYodj49MTAwMDApcmV0dXJuICh2LzEwMDAwKS50b0ZpeGVkKHY+PTEwMDAwMD8wOjEpLnJlcGxhY2UoL1wuMCQvLCcnKSsn66eMJzsgaWYodj49MTAwMClyZXR1cm4gKHYvMTAwMCkudG9GaXhlZCgxKS5yZXBsYWNlKC9cLjAkLywnJykrJ+yynCc7IHJldHVybiB2LnRvTG9jYWxlU3RyaW5nKCdrby1LUicpO307CiAgY29uc3QgVz1NYXRoLm1heCg4NjAsIFBMK1BSK24qKFM+MT9TKjE1KzE4OjM0KSk7CiAgY29uc3QgbXg9TWF0aC5tYXgoMSwuLi5zZXJpZXMuZmxhdE1hcChzPT5zLnZhbHMpKSoxLjI4OwogIGNvbnN0IHBsb3RXPVctUEwtUFIsIHBsb3RIPUgtUFQtUEIsIGd3PXBsb3RXL247CiAgY29uc3QgYnc9TWF0aC5tYXgoMyxNYXRoLm1pbihTPjE/MjQ6Z3cqMC41MiwoZ3ctNikvUykpOwogIGNvbnN0IFk9dj0+UFQrcGxvdEgqKDEtdi9teCk7CiAgY29uc3QgbGJsU3RlcD1NYXRoLm1heCgxLE1hdGguY2VpbChuLzcpKTsKICBsZXQgZ3JpZD0nJzsgY29uc3QgRz00OwogIGZvcihsZXQgaT0wO2k8PUc7aSsrKXtjb25zdCBneT1QVCtwbG90SCppL0csIGd2PW14KigxLWkvRyk7CiAgICBncmlkKz1gPGxpbmUgeDE9IiR7UEx9IiB5MT0iJHtneS50b0ZpeGVkKDEpfSIgeDI9IiR7Vy1QUn0iIHkyPSIke2d5LnRvRml4ZWQoMSl9IiBzdHJva2U9InZhcigtLWxpbmUpIiBzdHJva2Utd2lkdGg9IjEiLz5gCiAgICAgICsoaTxHP2A8dGV4dCB4PSIke1BMLTZ9IiB5PSIkeyhneSszKS50b0ZpeGVkKDEpfSIgZm9udC1zaXplPSI4IiBmaWxsPSJ2YXIoLS1tdXRlZCkiIHRleHQtYW5jaG9yPSJlbmQiPiR7ZmMoZ3YpfTwvdGV4dD5gOicnKTt9CiAgbGV0IGJhcnM9Jyc7CiAgZm9yKGxldCBpPTA7aTxuO2krKyl7CiAgICBjb25zdCBneD1QTCtndyppKyhndy1idypTKS8yOwogICAgc2VyaWVzLmZvckVhY2goKHMsc2kpPT57CiAgICAgIGNvbnN0IHY9cy52YWxzW2ldfHwwLCB4PWd4K3NpKmJ3LCB5PVkodiksIGhoPVBUK3Bsb3RILXksIGN4PXgrYncqMC40NTsKICAgICAgYmFycys9YDxyZWN0IHg9IiR7eC50b0ZpeGVkKDEpfSIgeT0iJHt5LnRvRml4ZWQoMSl9IiB3aWR0aD0iJHsoYncqMC44NikudG9GaXhlZCgxKX0iIGhlaWdodD0iJHtNYXRoLm1heCgwLGhoKS50b0ZpeGVkKDEpfSIgcng9IjIuNSIgZmlsbD0iJHtzLmNvbG9yfSI+PHRpdGxlPiR7ZGF5c1tpXX0gJHtzLm5hbWV9OiAke2Yodil9PC90aXRsZT48L3JlY3Q+YDsKICAgICAgaWYoaSVsYmxTdGVwPT09MCYmdj4wKSBiYXJzKz1gPHRleHQgeD0iJHtjeC50b0ZpeGVkKDEpfSIgeT0iJHsoeS0zKS50b0ZpeGVkKDEpfSIgZm9udC1zaXplPSI1LjUiIGZvbnQtd2VpZ2h0PSI1MDAiIGZpbGw9InZhcigtLW11dGVkKSIgdGV4dC1hbmNob3I9Im1pZGRsZSI+JHtmYyh2KX08L3RleHQ+YDsKICAgIH0pOwogIH0KICBjb25zdCBzaG93Vz1uPD0xNCwgeHN0ZXA9TWF0aC5tYXgoMSxNYXRoLmNlaWwobi84KSk7CiAgbGV0IHhsPScnOwogIGZvcihsZXQgaT0wO2k8bjtpKz14c3RlcCl7Y29uc3QgY3g9UEwrZ3cqaStndy8yOyB4bCs9YDx0ZXh0IHg9IiR7Y3gudG9GaXhlZCgxKX0iIHk9IiR7SC0xMn0iIGZvbnQtc2l6ZT0iOSIgZmlsbD0idmFyKC0tbXV0ZWQpIiB0ZXh0LWFuY2hvcj0ibWlkZGxlIj4ke3dkKGRheXNbaV0sc2hvd1cpfTwvdGV4dD5gO30KICBjb25zdCBsZWdlbmQ9Uz4xP2A8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7Z2FwOjE0cHg7Zm9udC1zaXplOjExcHg7bWFyZ2luLXRvcDo4cHg7ZmxleC13cmFwOndyYXAiPmArc2VyaWVzLm1hcChzPT5gPHNwYW4gc3R5bGU9ImNvbG9yOnZhcigtLXN1YikiPjxzcGFuIHN0eWxlPSJkaXNwbGF5OmlubGluZS1ibG9jazt3aWR0aDoxMHB4O2hlaWdodDoxMHB4O2JvcmRlci1yYWRpdXM6M3B4O2JhY2tncm91bmQ6JHtzLmNvbG9yfTttYXJnaW4tcmlnaHQ6NXB4O3ZlcnRpY2FsLWFsaWduOm1pZGRsZSI+PC9zcGFuPiR7cy5uYW1lfTwvc3Bhbj5gKS5qb2luKCcnKStgPC9kaXY+YDonJzsKICByZXR1cm4gYDxkaXYgY2xhc3M9InNjcm9sbCI+PHN2ZyB2aWV3Qm94PSIwIDAgJHtXfSAke0h9IiB3aWR0aD0iMTAwJSIgc3R5bGU9Im1pbi13aWR0aDoke01hdGgubWluKFcsNTIwKS50b0ZpeGVkKDApfXB4O2Rpc3BsYXk6YmxvY2siPiR7Z3JpZH0ke2JhcnN9JHt4bH08L3N2Zz48L2Rpdj4ke2xlZ2VuZH1gOwp9CmZ1bmN0aW9uIHN1bW1hcnlUcmVuZChXKXsKICBpZihXLmN1ci5sZW5ndGg8MikgcmV0dXJuICcnOwogIGNvbnN0IE09e2luc3RhbGw6WyLslbHshKTsuZgiLFRPVEFMUy5pbnN0YWxsXSx2aXNpdDpbIuyVsSDrsKnrrLgiLFRPVEFMUy52aXNpdF0sc3U6WyLtmozsm5DqsIDsnoUiLFRPVEFMUy5zdV0sY2FyZDpbIuq1kO2Gtey5tOuTnCDrk7HroZ0iLFRPVEFMUy5jYXJkXSxsb3VuZ2U6WyLrjIDspJHqtZDthrXrnbzsmrTsp4Ag67Cp66y4IixUT1RBTFMubG91bmdlXSxjYWxsOlsi7YOd7Iuc7Zi47LacIixUQVhJVE9UXX07CiAgaWYoIU1bVFJFTkRLRVldKSBUUkVOREtFWT0ic3UiOyBjb25zdCBtPU1bVFJFTkRLRVldOwogIGNvbnN0IGRheXM9Vy5jdXIubWFwKGk9PkRBWVNbaV0pLCB2YWxzPVcuY3VyLm1hcChpPT5tWzFdW2ldfHwwKTsKICBjb25zdCBidG5zPWA8ZGl2IGNsYXNzPSJzZWciIHN0eWxlPSJtYXJnaW4tYm90dG9tOjEwcHg7ZmxleC13cmFwOndyYXAiPmArT2JqZWN0LmtleXMoTSkubWFwKGs9PmA8YnV0dG9uIGNsYXNzPSIke2s9PT1UUkVOREtFWT8nb24nOicnfSIgZGF0YS10cmVuZD0iJHtrfSI+JHtNW2tdWzBdfTwvYnV0dG9uPmApLmpvaW4oJycpK2A8L2Rpdj5gOwogIHJldHVybiBgPGRpdiBjbGFzcz0iY2FyZCI+PGgzPuydvOyekOuzhCDstpTsnbQgPHNwYW4gY2xhc3M9Im1pbmkiPiR7bVswXX0gKOyghOyytCDsnKDsnoUpPC9zcGFuPjwvaDM+JHtidG5zfSR7dHJlbmRDaGFydChkYXlzLFt7bmFtZTptWzBdLGNvbG9yOicjODM3MGU2Jyx2YWxzfV0pfTwvZGl2PmA7Cn0KZnVuY3Rpb24gYnVja2V0VHJlbmQoQixyb3dzLFcpewogIGlmKFcuY3VyLmxlbmd0aDwyKSByZXR1cm4gJyc7CiAgY29uc3QgZGF5cz1XLmN1ci5tYXAoaT0+REFZU1tpXSksIGJ0PUJUT1RbQi5pZF18fHtjYWxsOltdfTsKICBjb25zdCB0b3RWPVcuY3VyLm1hcChpPT4oYnQuY2FsbHx8W10pW2ldfHwwKTsKICBjb25zdCBhZFY9Vy5jdXIubWFwKGk9PnJvd3MucmVkdWNlKChhLHIpPT5hKyhyLmNoIT09Im9yZyI/KHIucy5jYWxsW2ldfHwwKTowKSwwKSk7CiAgY29uc3QgYWxsPVt7bmFtZToi7KCE7LK0Iixjb2xvcjonI2I5YjNjYycsdmFsczp0b3RWLGtleTondG90J30se25hbWU6Iuq0keqzoCjsoITssrQpIixjb2xvcjonIzgzNzBlNicsdmFsczphZFYsa2V5OidhZCd9XTsKICBjb25zdCBzZXJpZXM9QlRSSz09PSdhZCc/YWxsLmZpbHRlcihzPT5zLmtleT09PSdhZCcpOmFsbDsKICBjb25zdCBzZWc9YDxkaXYgY2xhc3M9InNlZyIgc3R5bGU9ImFsaWduLXNlbGY6ZmxleC1zdGFydDttYXJnaW4tYm90dG9tOjhweCI+PGJ1dHRvbiBjbGFzcz0iJHtCVFJLIT09J2FkJz8nb24nOicnfSIgZGF0YS1idHJlbmQ9ImJvdGgiPuyghOyytCAo6rSR6rOgK+u5hOq0keqzoCk8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSIke0JUUks9PT0nYWQnPydvbic6Jyd9IiBkYXRhLWJ0cmVuZD0iYWQiPuq0keqzoDwvYnV0dG9uPjwvZGl2PmA7CiAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJjYXJkIj48aDM+7J287J6Q67OEIOy2lOydtCA8c3BhbiBjbGFzcz0ibWluaSI+JHtCLnByaW1hcnlMYWJlbH08L3NwYW4+PC9oMz4ke3NlZ30ke3RyZW5kQ2hhcnQoZGF5cyxzZXJpZXMpfTwvZGl2PmA7Cn0KCi8qIC0tLS0g7JqU7JW9IC0tLS0gKi8KZnVuY3Rpb24gY2hCbG9jayh0aXRsZSxrZXksVyl7CiAgY29uc3Qgcm93cz1DSC5tYXAoYz0+KHtuYW1lOmMubmFtZSxhZDpjLmFkLGN1cjpzdW0oY1trZXldLFcuY3VyKX0pKS5zb3J0KChhLGIpPT5iLmN1ci1hLmN1cik7CiAgY29uc3QgdG90YWw9cm93cy5yZWR1Y2UoKGEscik9PmErci5jdXIsMCl8fDE7CiAgY29uc3QgYWRQY3Q9cm93cy5maWx0ZXIocj0+ci5hZCkucmVkdWNlKChhLHIpPT5hK3IuY3VyLDApL3RvdGFsKjEwMCwgb3JnUGN0PTEwMC1hZFBjdDsKICBjb25zdCBiYXI9YDxkaXYgY2xhc3M9InNwbGl0YmFyIj48c3BhbiBjbGFzcz0iYWQiIHN0eWxlPSJ3aWR0aDoke2FkUGN0fSUiPiR7YWRQY3Q+PTEyPyfqtJHqs6AgJythZFBjdC50b0ZpeGVkKDApKyclJzonJ308L3NwYW4+PHNwYW4gY2xhc3M9Im9yZyIgc3R5bGU9IndpZHRoOiR7b3JnUGN0fSUiPiR7b3JnUGN0Pj0xMj8n67mE6rSR6rOgICcrb3JnUGN0LnRvRml4ZWQoMCkrJyUnOicnfTwvc3Bhbj48L2Rpdj5gOwogIGNvbnN0IGxpc3Q9cm93cy5tYXAocj0+e2NvbnN0IHc9ci5jdXIvdG90YWwqMTAwOwogICAgaWYoci5hZCl7CiAgICAgIHJldHVybiBgPGRpdiBjbGFzcz0iY2hyb3ciPjxkaXYgY2xhc3M9Im5tIj4ke3IubmFtZX08c3BhbiBjbGFzcz0iYWR0YWciPuq0keqzoDwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJ0cmFjayI+PHNwYW4gY2xhc3M9ImFkZiIgc3R5bGU9IndpZHRoOiR7d30lIj48L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0idmFsIj48Yj4ke2Yoci5jdXIpfTwvYj4gLyAke3cudG9GaXhlZCgwKX0lPC9kaXY+PC9kaXY+YDt9CiAgICByZXR1cm4gYDxkaXYgY2xhc3M9ImNocm93Ij48ZGl2IGNsYXNzPSJubSI+JHtyLm5hbWV9PC9kaXY+PGRpdiBjbGFzcz0idHJhY2siPjxzcGFuIHN0eWxlPSJ3aWR0aDoke3d9JSI+PC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9InZhbCI+PGI+JHtmKHIuY3VyKX08L2I+IC8gJHt3LnRvRml4ZWQoMCl9JTwvZGl2PjwvZGl2PmA7CiAgfSkuam9pbignJyk7CiAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJjYXJkIj48aDM+JHt0aXRsZX0gPHNwYW4gY2xhc3M9Im1pbmkiPuy0nSAke2YodG90YWwpfSAo7LGE64SQIO2VqeyCsCAvIOykkeuztSDtj6ztlagpPC9zcGFuPjwvaDM+JHtiYXJ9JHtsaXN0fTwvZGl2PmA7Cn0KZnVuY3Rpb24gb25iQmxvY2soVyl7CiAgY29uc3Qgc3U9c3VtKFRPVEFMUy5zdSxXLmN1ciksIHN1UD1XLnByaW9yLmxlbmd0aD9zdW0oVE9UQUxTLnN1LFcucHJpb3IpOm51bGw7CiAgY29uc3QgYW55RGF0YT1PTkIucmVkdWNlKChhLG8pPT5hK3N1bShvWzFdLFcuY3VyKSwwKT4wOwogIGlmKCFhbnlEYXRhKSByZXR1cm4gYDxkaXYgY2xhc3M9ImNhcmQiPjxoMz7smKjrs7TrlKkg7KCE7ZmY7JyoIDxzcGFuIGNsYXNzPSJtaW5pIj7soITssrQg6rCA7J6F7J6QIC8g6rCA7J6FIOuLueydvDwvc3Bhbj48L2gzPjxkaXYgY2xhc3M9ImNzdWIiIHN0eWxlPSJtYXJnaW46MCI+66as7YWQ7IWYIOuNsOydtO2EsCDsl7Drj5kg7KSA67mEIOykkSDigJQg6rCA7J6FIOuLueydvCDtlbXsi6ztlonrj5kg7KCE7ZmY7Jyo7J20IOqzpyDtkZzsi5zrkKnri4jri6QuPC9kaXY+PC9kaXY+YDsKICBjb25zdCBpdGVtcz1PTkIubWFwKG89Pntjb25zdCBjPXN1bShvWzFdLFcuY3VyKSxwPVcucHJpb3IubGVuZ3RoP3N1bShvWzFdLFcucHJpb3IpOm51bGw7Y29uc3QgcmF0ZT1zdT9jL3N1KjEwMDowLHJhdGVQPShzdVAmJnAhPW51bGwpP3Avc3VQKjEwMDpudWxsO3JldHVybiBgPGRpdiBjbGFzcz0iY29udiI+PGRpdiBjbGFzcz0idCI+JHtvWzBdfTwvZGl2PjxkaXYgY2xhc3M9ImJpZyI+JHtyYXRlLnRvRml4ZWQoMSl9PHNwYW4gY2xhc3M9InUiPiU8L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0ic3ViMiI+JHtmKGMpfeuqhSAvIOqwgOyehSAke2Yoc3UpfeuqhSAke2RlbHRhU3BhbihyYXRlLHJhdGVQLHRydWUsdHJ1ZSl9PC9kaXY+PC9kaXY+YDt9KS5qb2luKCcnKTsKICByZXR1cm4gYDxkaXYgY2xhc3M9ImNhcmQiPjxoMz7smKjrs7TrlKkg7KCE7ZmY7JyoIDxzcGFuIGNsYXNzPSJtaW5pIj7soITssrQg6rCA7J6F7J6QIC8g6rCA7J6FIOuLueydvDwvc3Bhbj48L2gzPjxkaXYgY2xhc3M9ImNzdWIiPuyghCDssYTrhJAg6rCA7J6F7J6QKOq0keqzoCvruYTqtJHqs6Ap6rCAIDxiPuqwgOyehSDri7nsnbw8L2I+IO2VtOuLuSDtlonrj5nsnYQg7ZWcIOu5hOycqCAvIOu2hOuqqCA9IOuLueydvCDqsIDsnoXsnpAg7IiYPC9kaXY+PGRpdiBjbGFzcz0iY29udmdyaWQiPiR7aXRlbXN9PC9kaXY+PC9kaXY+YDsKfQpmdW5jdGlvbiBlZmZCbG9jayhXKXsKICBjb25zdCBib2R5PUFERUZGLm1hcChhPT57CiAgICBjb25zdCBjb3N0PXN1bShhLmNvc3QsVy5jdXIpLGluc3Q9c3VtKGEuaW5zdGFsbHx8W10sVy5jdXIpLHN1PXN1bShhLnN1LFcuY3VyKSxjYWxsPXN1bShhLmNhbGwsVy5jdXIpOwogICAgY29uc3QgY3BzPXN1P2Nvc3Qvc3U6bnVsbCxjcGM9Y2FsbD9jb3N0L2NhbGw6bnVsbCxpMnM9aW5zdD9zdS9pbnN0KjEwMDpudWxsOwogICAgcmV0dXJuIGA8dHI+PHRkIGNsYXNzPSJuYW1lIj4ke2EubmFtZX08L3RkPjx0ZCBjbGFzcz0iZGltIj4ke2YoaW5zdCl9PC90ZD48dGQgY2xhc3M9ImRpbSI+JHtmKHN1KX08L3RkPjx0ZD4ke2kycyE9bnVsbD9pMnMudG9GaXhlZCgxKSsnJSc6J+KAlCd9PC90ZD48dGQ+4oKpJHtmKGNwcyl9PC90ZD48dGQgY2xhc3M9ImtwaWNvbCI+JHtmKGNhbGwpfTwvdGQ+PHRkPuKCqSR7ZihjcGMpfTwvdGQ+PHRkIGNsYXNzPSJkaW0iPuKCqSR7Zihjb3N0KX08L3RkPjwvdHI+YDsKICB9KS5qb2luKCcnKTsKICByZXR1cm4gYDxkaXYgY2xhc3M9ImNhcmQiPjxoMz7ssYTrhJDrs4Qg7Zqo7JyoPC9oMz48ZGl2IGNsYXNzPSJjc3ViIj7shKTsuZgg4oaSIOqwgOyehSDsoITtmZjqs7wg6rCA7J6FIDHrqoUg67CPIO2DneyLnO2YuOy2nCAx6rG064u5IOu5hOyaqTwvZGl2PgogICAgPGRpdiBjbGFzcz0ic2Nyb2xsIj48dGFibGUgc3R5bGU9Im1pbi13aWR0aDo3MjBweCI+PHRoZWFkPjx0cj48dGg+7LGE64SQPC90aD48dGg+7ISk7LmYPC90aD48dGg+6rCA7J6FPC90aD48dGg+6rCA7J6FL+yEpOy5mDwvdGg+PHRoPuqwgOyeheuLuSDruYTsmqk8L3RoPjx0aCBjbGFzcz0ia3BpY29sIj7tg53si5ztmLjstpw8L3RoPjx0aD7tmLjstpzri7kg67mE7JqpPC90aD48dGg+6rSR6rOg67mEPC90aD48L3RyPjwvdGhlYWQ+PHRib2R5PiR7Ym9keX08L3Rib2R5PjwvdGFibGU+PC9kaXY+PC9kaXY+YDsKfQpmdW5jdGlvbiByZXRuQmxvY2soKXsKICBpZighUkVUTnx8IVJFVE4ucG9pbnRzfHwhUkVUTi5wb2ludHMubGVuZ3RoKSByZXR1cm4gJyc7CiAgY29uc3QgbXg9TWF0aC5tYXgoLi4uUkVUTi5wb2ludHMubWFwKHA9PnBbMV0pLDAuMDEpOwogIGNvbnN0IGJhcnM9UkVUTi5wb2ludHMubWFwKHA9PmA8ZGl2IHN0eWxlPSJmbGV4OjE7dGV4dC1hbGlnbjpjZW50ZXIiPgogICAgPGRpdiBzdHlsZT0iaGVpZ2h0OjkycHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmZsZXgtZW5kO2p1c3RpZnktY29udGVudDpjZW50ZXIiPjxkaXYgc3R5bGU9IndpZHRoOjU4JTtiYWNrZ3JvdW5kOnZhcigtLWFjY2VudCk7Ym9yZGVyLXJhZGl1czo1cHggNXB4IDAgMDtoZWlnaHQ6JHtNYXRoLm1heCgzLHBbMV0vbXgqOTIpLnRvRml4ZWQoMCl9cHgiPjwvZGl2PjwvZGl2PgogICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi10b3A6NXB4Ij4keyhwWzFdKjEwMCkudG9GaXhlZCgxKX0lPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJkaW0iIHN0eWxlPSJmb250LXNpemU6MTJweCI+JHtwWzBdfTwvZGl2PjwvZGl2PmApLmpvaW4oJycpOwogIHJldHVybiBgPGRpdiBjbGFzcz0iY2FyZCI+PGgzPuyerOuwqeusuCjslbEg66as7YWQ7IWYKSA8c3BhbiBjbGFzcz0ibWluaSI+7ISk7LmYIOKGkiDslbEg64uk7IucIOyXtOq4sCAvICR7UkVUTi53aW5kb3d9IOy9lO2YuO2KuDwvc3Bhbj48L2gzPgogICAgPGRpdiBjbGFzcz0iY3N1YiIgc3R5bGU9Im1hcmdpbi1ib3R0b206MTBweCI+7JWxIOyEpOy5mO2VnCDsgqzrnozsnbQgRDEvRDcvRDE0L0QzMOyXkCDslbHsnYQg64uk7IucIOyXsCDruYTsnKggKOy9lO2YuO2KuCAke2YoUkVUTi5jb2hvcnQpfeuqhSwgRDMw6rmM7KeAIOyEseyImSk8L2Rpdj4KICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDtnYXA6MTJweDthbGlnbi1pdGVtczpmbGV4LWVuZCI+JHtiYXJzfTwvZGl2PjwvZGl2PmA7Cn0KZnVuY3Rpb24gcmVuZGVyU3VtbWFyeSgpewogIGNvbnN0IFc9d2luZG93cygpLHByPVcucHJpb3IubGVuZ3RoOwogIGNvbnN0IHN1PXN1bShUT1RBTFMuc3UsVy5jdXIpLHN1UD1wcj9zdW0oVE9UQUxTLnN1LFcucHJpb3IpOm51bGw7CiAgY29uc3QgY2FyZD1zdW0oVE9UQUxTLmNhcmQsVy5jdXIpLGNhcmRQPXByP3N1bShUT1RBTFMuY2FyZCxXLnByaW9yKTpudWxsOwogIGNvbnN0IGxvPXN1bShUT1RBTFMubG91bmdlLFcuY3VyKSxsb1A9cHI/c3VtKFRPVEFMUy5sb3VuZ2UsVy5wcmlvcik6bnVsbDsKICBjb25zdCBjYWxsPXN1bShUQVhJVE9ULFcuY3VyKSxjYWxsUD1wcj9zdW0oVEFYSVRPVCxXLnByaW9yKTpudWxsOwogIGNvbnN0IGluc3Q9c3VtKFRPVEFMUy5pbnN0YWxsfHxbXSxXLmN1ciksaW5zdFA9cHI/c3VtKFRPVEFMUy5pbnN0YWxsfHxbXSxXLnByaW9yKTpudWxsOwogIGNvbnN0IHZpcz1zdW0oVE9UQUxTLnZpc2l0fHxbXSxXLmN1ciksdmlzUD1wcj9zdW0oVE9UQUxTLnZpc2l0fHxbXSxXLnByaW9yKTpudWxsOwogIGNvbnN0IGNhcmRzPWA8ZGl2IGNsYXNzPSJrcGlzIj4KICAgICR7a3BpKCLslbHshKTsuZgiLGluc3QsaW5zdFAse3VuaXQ6IuuqhSIsc3ViOmFkU3ViKCJpbnN0YWxsIixpbnN0LFcpfSl9CiAgICAke2twaSgi7JWxIOuwqeusuCIsdmlzLHZpc1Ase3VuaXQ6IuuqhSIsc3ViOmFkU3ViKCJ2aXNpdCIsdmlzLFcpfSl9CiAgICAke2twaSgi7ZqM7JuQ6rCA7J6FIixzdSxzdVAse3ByaW1hcnk6dHJ1ZSx1bml0OiLrqoUiLHN1YjphZFN1Yigic3UiLHN1LFcpfSl9CiAgICAke2twaSgi6rWQ7Ya17Lm065OcIOuTseuhnSIsY2FyZCxjYXJkUCx7dW5pdDoi6rG0IixzdWI6YWRTdWIoImNhcmQiLGNhcmQsVyl9KX0KICAgICR7a3BpKCLrjIDspJHqtZDthrXrnbzsmrTsp4Ag67Cp66y4Iixsbyxsb1Ase3VuaXQ6IuuqhSIsc3ViOmFkU3ViKCJsb3VuZ2UiLGxvLFcpfSl9CiAgICAke2twaSgi7YOd7Iuc7Zi47LacIixjYWxsLGNhbGxQLHt1bml0OiLqsbQiLHN1YjphZFN1YigiY2FsbCIsY2FsbCxXKX0pfQogIDwvZGl2PmA7CiAgcmV0dXJuIGNhcmRzK3N1bW1hcnlUcmVuZChXKStkcmlsbEJsb2NrKFcpK2VmZkJsb2NrKFcpOwp9CgovKiAtLS0tIOuyhO2CtyDtg60gPSDqtJHqs6Ag6riw7Jes66eMIC0tLS0gKi8KZnVuY3Rpb24gYWdnUm93KHJvdyxpZHgpe2NvbnN0IHM9cm93LnM7cmV0dXJue2NhbGw6c3VtKHMuY2FsbCxpZHgpLGNvbXA6c3VtKHMuY29tcCxpZHgpLHN1OnN1bShzLnN1LGlkeCksY29zdDpzdW0ocy5jb3N0LGlkeCl9O30KZnVuY3Rpb24gYnVja2V0Um93cyhpZCl7cmV0dXJuIFJPV1MuZmlsdGVyKHI9PnIuYnVja2V0PT09aWQpO30KZnVuY3Rpb24gYnVja2V0QWN0aXZlKGlkKXtjb25zdCBhbGw9REFZUy5tYXAoKF8saSk9PmkpO3JldHVybiBidWNrZXRSb3dzKGlkKS5zb21lKHI9PnIuY2ghPT0ib3JnIiYmc3VtKHIucy5jYWxsLGFsbCk+MCk7fQpjb25zdCBLSU5EX0xBQkVMPXtnb29nbGU6Imdvb2dsZS5hZHMiLGluc3RhOiJpbnN0YSIsZXRjOiLruYTsmqkg66+47Jew64+ZIOq0keqzoCIseGFkczoi64uk66W4IOuqqeyggSDqtJHqs6AiLG9yZzoi6rSR6rOgIOyZuCDssYTrhJAifTsKLyog7ZWcIOuyhO2Ct+ydmCDrqqjrk6Ag7IS57IWY7J2EICftlZjrgpjsnZgg7ZGcJ+uhnCDigJQg7Je0IOychOy5mOqwgCDsoITrtoAg66ee7J2MKOykhOunnuy2pCkgKi8KZnVuY3Rpb24gYnVja2V0Q29scyhCKXsgLy8g6rO17Ya1IGNvbGdyb3VwICsg7Zek642UICjrsoTtgrcg64K0IOuqqOuToCDshLnshZgg64+Z7J28KSDigJQg7ZqM7JuQ6rCA7J6FIOyXtCDtla3sg4Eg7Y+s7ZWoCiAgbGV0IGNvbHcsIGhlYWQ7CiAgaWYoQi5kdWFsQ3BhKXsgY29sdz1bIjIyJSIsIjEzJSIsIjExJSIsIjEyJSIsIjExJSIsIjEyJSIsIjEwJSIsIjklIl07CiAgICBoZWFkPWA8dGg+7Lqg7Y6Y7J24PC90aD48dGggY2xhc3M9ImxoIj7slaDrk5zqt7jro7k8L3RoPjx0aD7tmozsm5DqsIDsnoU8L3RoPjx0aCBjbGFzcz0ia3BpY29sIj4ke0IucHJpbWFyeUxhYmVsfTwvdGg+PHRoPiR7Qi5jb21wTGFiZWx9PC90aD48dGg+6rSR6rOg67mEPC90aD48dGg+JHtCLmNwYUxhYmVsfTwvdGg+PHRoPiR7Qi5jb21wQ3BhTGFiZWx9PC90aD5gOwogIH0gZWxzZSBpZihCLnNob3dDb21wKXsgY29sdz1bIjI0JSIsIjE0JSIsIjEyJSIsIjE0JSIsIjEzJSIsIjEyJSIsIjExJSJdOwogICAgaGVhZD1gPHRoPuy6oO2OmOyduDwvdGg+PHRoIGNsYXNzPSJsaCI+7JWg65Oc6re466O5PC90aD48dGg+7ZqM7JuQ6rCA7J6FPC90aD48dGggY2xhc3M9ImtwaWNvbCI+JHtCLnByaW1hcnlMYWJlbH08L3RoPjx0aD4ke0IuY29tcExhYmVsfTwvdGg+PHRoPuq0keqzoOu5hDwvdGg+PHRoPiR7Qi5jcGFMYWJlbH08L3RoPmA7CiAgfSBlbHNlIHsgY29sdz1bIjI4JSIsIjE2JSIsIjE1JSIsIjE1JSIsIjE0JSIsIjEyJSJdOwogICAgaGVhZD1gPHRoPuy6oO2OmOyduDwvdGg+PHRoIGNsYXNzPSJsaCI+7JWg65Oc6re466O5PC90aD48dGg+7ZqM7JuQ6rCA7J6FPC90aD48dGggY2xhc3M9ImtwaWNvbCI+JHtCLnByaW1hcnlMYWJlbH08L3RoPjx0aD7qtJHqs6DruYQ8L3RoPjx0aD4ke0IuY3BhTGFiZWx9PC90aD5gOwogIH0KICBjb25zdCBjb2w9YDxjb2xncm91cD4ke2NvbHcubWFwKHc9PmA8Y29sIHN0eWxlPSJ3aWR0aDoke3d9Ij5gKS5qb2luKCcnKX08L2NvbGdyb3VwPmA7CiAgcmV0dXJuIHtuY29sOmNvbHcubGVuZ3RoLCBjb2wsIGhlYWQ6YDx0aGVhZD48dHI+JHtoZWFkfTwvdHI+PC90aGVhZD5gfTsKfQpmdW5jdGlvbiBzZWNSb3cobyxCLG5vQ29zdCxzaG93Q3IpewogIGxldCBjZWxscz1gPHRkIGNsYXNzPSJuYW1lIj4ke28uY2FtcH08L3RkPjx0ZCBjbGFzcz0ibmFtZSBkaW0iPiR7by5hZ3x8Jyd9JHtzaG93Q3ImJm8uY3I/YCA8c3BhbiBzdHlsZT0ib3BhY2l0eTouNyI+4oC6ICR7by5jcn08L3NwYW4+YDonJ308L3RkPmA7CiAgY2VsbHMrPWA8dGQgY2xhc3M9ImRpbSI+JHtmKG8uY19zdSl9PC90ZD5gOyAgIC8vIO2ajOybkOqwgOyehSAo7ZWt7IOBKQogIGlmKEIuc2hvd0NvbXApIGNlbGxzKz1gPHRkIGNsYXNzPSJrcGljb2wiPiR7ZihvLmNfY2FsbCl9JHtkZWx0YVNwYW4oby5jX2NhbGwsby5wX2NhbGw9PW51bGw/bnVsbDpvLnBfY2FsbCx0cnVlLHRydWUpfTwvdGQ+PHRkPiR7ZihvLmNfY29tcCl9PC90ZD5gOwogIGVsc2UgY2VsbHMrPWA8dGQgY2xhc3M9ImtwaWNvbCI+JHtmKG8uY19jYWxsKX0ke2RlbHRhU3BhbihvLmNfY2FsbCxvLnBfY2FsbD09bnVsbD9udWxsOm8ucF9jYWxsLHRydWUsdHJ1ZSl9PC90ZD5gOwogIGNlbGxzKz1gPHRkIGNsYXNzPSJkaW0iPiR7bm9Db3N0PycnOifigqknK2Yoby5jX2Nvc3QpfTwvdGQ+YDsKICBpZihCLmR1YWxDcGEpe2NvbnN0IGMxPW8uY19jYWxsP28uY19jb3N0L28uY19jYWxsOm51bGwsYzI9by5jX2NvbXA/by5jX2Nvc3Qvby5jX2NvbXA6bnVsbDtjZWxscys9YDx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzooYzEhPW51bGw/J+KCqScrZihjMSk6J+KAlCcpfTwvdGQ+PHRkIGNsYXNzPSJkaW0iPiR7bm9Db3N0PycnOihjMiE9bnVsbD8n4oKpJytmKGMyKTon4oCUJyl9PC90ZD5gO30KICBlbHNle2NvbnN0IGQ9Qi5zaG93Q29tcD9vLmNfY29tcDpvLmNfY2FsbCxjcGE9bm9Db3N0P251bGw6KGQ/by5jX2Nvc3QvZDpudWxsKTtjZWxscys9YDx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzooY3BhIT1udWxsPyfigqknK2YoY3BhKTon4oCUJyl9PC90ZD5gO30KICByZXR1cm4gYDx0cj4ke2NlbGxzfTwvdHI+YDsKfQpmdW5jdGlvbiBzZWNUb3RSb3coYWdnLEIsbm9Db3N0KXsKICBjb25zdCB0PWFnZy5yZWR1Y2UoKGEsbyk9PnthLnN1Kz1vLmNfc3V8fDA7YS5jYWxsKz1vLmNfY2FsbHx8MDthLmNvbXArPW8uY19jb21wfHwwO2EuY29zdCs9by5jX2Nvc3R8fDA7cmV0dXJuIGE7fSx7c3U6MCxjYWxsOjAsY29tcDowLGNvc3Q6MH0pOwogIGxldCBtaWQ9YDx0ZCBjbGFzcz0iZGltIj4ke2YodC5zdSl9PC90ZD5gKyhCLnNob3dDb21wP2A8dGQgY2xhc3M9ImtwaWNvbCI+JHtmKHQuY2FsbCl9PC90ZD48dGQ+JHtmKHQuY29tcCl9PC90ZD5gOmA8dGQgY2xhc3M9ImtwaWNvbCI+JHtmKHQuY2FsbCl9PC90ZD5gKTsKICBsZXQgY3BhQzsKICBpZihCLmR1YWxDcGEpe2NvbnN0IGMxPXQuY2FsbD90LmNvc3QvdC5jYWxsOm51bGwsYzI9dC5jb21wP3QuY29zdC90LmNvbXA6bnVsbDtjcGFDPWA8dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6KGMxIT1udWxsPyfigqknK2YoYzEpOifigJQnKX08L3RkPjx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzooYzIhPW51bGw/J+KCqScrZihjMik6J+KAlCcpfTwvdGQ+YDt9CiAgZWxzZXtjb25zdCBkPUIuc2hvd0NvbXA/dC5jb21wOnQuY2FsbCxjcGE9bm9Db3N0P251bGw6KGQ/dC5jb3N0L2Q6bnVsbCk7Y3BhQz1gPHRkIGNsYXNzPSJkaW0iPiR7bm9Db3N0PycnOihjcGEhPW51bGw/J+KCqScrZihjcGEpOifigJQnKX08L3RkPmA7fQogIHJldHVybiBgPHRyIGNsYXNzPSJ0b3Ryb3ciPjx0ZD7tlanqs4Q8L3RkPjx0ZD48L3RkPiR7bWlkfTx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzon4oKpJytmKHQuY29zdCl9PC90ZD4ke2NwYUN9PC90cj5gOwp9CmZ1bmN0aW9uIHNlY0Jsb2NrKHJvd3Msa2luZCxCLG5jb2wpewogIGNvbnN0IG5vQ29zdD0oa2luZD09PSJldGMifHxraW5kPT09Im9yZyIpOwogIGNvbnN0IHNob3dDcj0hIUNSU0hPV1tCLmlkXSYmKGtpbmQ9PT0iZ29vZ2xlInx8a2luZD09PSJpbnN0YSJ8fGtpbmQ9PT0iZXRjInx8a2luZD09PSJ4YWRzIik7CiAgY29uc3QgZ3JvdXBzPXt9OwogIHJvd3MuZm9yRWFjaChyPT57Y29uc3Qga2V5PXIuY2FtcCsnXHUwMDAxJysoci5hZ3x8JycpKyhzaG93Q3I/J1x1MDAwMScrKHIuY3J8fCcnKTonJyk7KGdyb3Vwc1trZXldfHwoZ3JvdXBzW2tleV09W10pKS5wdXNoKHIpO30pOwogIGNvbnN0IGFnZz1PYmplY3Qua2V5cyhncm91cHMpLm1hcChrZXk9Pntjb25zdCBncnA9Z3JvdXBzW2tleV0scjA9Z3JwWzBdO2NvbnN0IG89e2NhbXA6cjAuY2FtcCxhZzpyMC5hZyxjcjpzaG93Q3I/KHIwLmNyfHwnJyk6Jyd9O2NvbnN0IGN1PXtjYWxsOjAsY29tcDowLHN1OjAsY29zdDowfSxwcj1DVVJXLnByaW9yLmxlbmd0aD97Y2FsbDowLGNvbXA6MCxzdTowLGNvc3Q6MH06bnVsbDtncnAuZm9yRWFjaChyPT57Y29uc3QgYT1hZ2dSb3cocixDVVJXLmN1cik7Y3UuY2FsbCs9YS5jYWxsO2N1LmNvbXArPWEuY29tcDtjdS5zdSs9YS5zdTtjdS5jb3N0Kz1hLmNvc3Q7aWYocHIpe2NvbnN0IHA9YWdnUm93KHIsQ1VSVy5wcmlvcik7cHIuY2FsbCs9cC5jYWxsO3ByLmNvbXArPXAuY29tcDtwci5zdSs9cC5zdTtwci5jb3N0Kz1wLmNvc3Q7fX0pO1siY2FsbCIsImNvbXAiLCJzdSIsImNvc3QiXS5mb3JFYWNoKGs9PntvWyJjXyIra109Y3Vba107aWYocHIpb1sicF8iK2tdPXByW2tdO30pO3JldHVybiBvO30pLnNvcnQoKGEsYik9PmIuY19jYWxsLWEuY19jYWxsKTsKICBjb25zdCBoZD1gPHRyIGNsYXNzPSJzZWNoZC1yb3ciPjx0ZCBjb2xzcGFuPSIke25jb2x9Ij4ke0tJTkRfTEFCRUxba2luZF19PC90ZD48L3RyPmA7CiAgcmV0dXJuIGhkK2FnZy5tYXAobz0+c2VjUm93KG8sQixub0Nvc3Qsc2hvd0NyKSkuam9pbignJykrc2VjVG90Um93KGFnZyxCLG5vQ29zdCk7Cn0KbGV0IENVUlc9bnVsbDsKZnVuY3Rpb24gbG91bmdlRW50cnkoVyl7CiAgY29uc3QgYWQ9TEdLPT09J2FkJzsKICBjb25zdCBhcHAgPSBhZD8gZHJpbGxBZCgndmlzaXQnLFcuY3VyKSA6IHN1bShUT1RBTFMudmlzaXR8fFtdLFcuY3VyKTsKICBjb25zdCBsbmcgPSBhZD8gZHJpbGxBZCgnbG91bmdlJyxXLmN1cikgOiBzdW0oVE9UQUxTLmxvdW5nZXx8W10sVy5jdXIpOwogIGNvbnN0IGFwcFAgPSBXLnByaW9yLmxlbmd0aD8gKGFkPyBkcmlsbEFkKCd2aXNpdCcsVy5wcmlvcikgOiBzdW0oVE9UQUxTLnZpc2l0fHxbXSxXLnByaW9yKSkgOiBudWxsOwogIGNvbnN0IGxuZ1AgPSBXLnByaW9yLmxlbmd0aD8gKGFkPyBkcmlsbEFkKCdsb3VuZ2UnLFcucHJpb3IpIDogc3VtKFRPVEFMUy5sb3VuZ2V8fFtdLFcucHJpb3IpKSA6IG51bGw7CiAgY29uc3QgcmF0ZT1hcHA/bG5nL2FwcCoxMDA6MDsKICBjb25zdCBzZWc9YDxkaXYgY2xhc3M9InNlZyIgc3R5bGU9ImFsaWduLXNlbGY6ZmxleC1zdGFydDttYXJnaW4tYm90dG9tOjEwcHgiPjxidXR0b24gY2xhc3M9IiR7IWFkPydvbic6Jyd9IiBkYXRhLWxnaz0iYWxsIj7soITssrQgKOq0keqzoCvruYTqtJHqs6ApPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iJHthZD8nb24nOicnfSIgZGF0YS1sZ2s9ImFkIj7qtJHqs6A8L2J1dHRvbj48L2Rpdj5gOwogIHJldHVybiBgPGRpdiBjbGFzcz0iY2FyZCI+PGgzPuyVsSDrsKnrrLgg4oaSIOudvOyatOyngCDsp4TsnoU8L2gzPgogICAgJHtzZWd9CiAgICA8ZGl2IGNsYXNzPSJrcGlzIj4ke2twaSgi7JWxIOuwqeusuOyekCIsYXBwLGFwcFAse3VuaXQ6IuuqhSJ9KX0ke2twaSgi65287Jq07KeAIOuwqeusuOyekCIsbG5nLGxuZ1Ase3ByaW1hcnk6dHJ1ZSx1bml0OiLrqoUifSl9PGRpdiBjbGFzcz0ia3BpIj48ZGl2IGNsYXNzPSJ0Ij7rnbzsmrTsp4Ag7KeE7J6F66WgPC9kaXY+PGRpdiBjbGFzcz0iYmlnIiBzdHlsZT0iY29sb3I6dmFyKC0tYWNjZW50KSI+JHtyYXRlLnRvRml4ZWQoMSl9PHNwYW4gY2xhc3M9InUiPiU8L3NwYW4+PC9kaXY+PC9kaXY+PC9kaXY+PC9kaXY+YDsKfQpmdW5jdGlvbiByZW5kZXJCdWNrZXQoaWQpewogIGNvbnN0IEI9QlVDS0VUUy5maW5kKHg9PnguaWQ9PT1pZCkscm93cz1idWNrZXRSb3dzKGlkKTtDVVJXPXdpbmRvd3MoKTsKICBjb25zdCBpc0FkPXI9PnIuY2g9PT0iZ29vZ2xlInx8ci5jaD09PSJpbnN0YSJ8fHIuY2g9PT0iZXRjIjsKICBjb25zdCBhZ2c9KGlkeCxmaWx0KT0+cm93cy5yZWR1Y2UoKG8scik9PntpZihmaWx0JiYhZmlsdChyKSlyZXR1cm4gbztjb25zdCBhPWFnZ1JvdyhyLGlkeCk7by5jYWxsKz1hLmNhbGw7by5jb21wKz1hLmNvbXA7by5jb3N0Kz1hLmNvc3Q7cmV0dXJuIG87fSx7Y2FsbDowLGNvbXA6MCxjb3N0OjB9KTsKICBjb25zdCBjPWFnZyhDVVJXLmN1cixpc0FkKSxwPUNVUlcucHJpb3IubGVuZ3RoP2FnZyhDVVJXLnByaW9yLGlzQWQpOm51bGw7ICAgICAgICAgIC8vIOq0keqzoCDquLDsl6wo66qp7KCBIOy6oO2OmOyduCkKICBjb25zdCBidD1CVE9UW2lkXXx8e307ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAvLyDsoITssrQo7KCEIOyxhOuEkCwg7KeB7KCRIOynkeqzhCkKICBjb25zdCB0b3RDYWxsPXN1bShidC5jYWxsfHxbXSxDVVJXLmN1ciksIHRvdENhbGxQPUNVUlcucHJpb3IubGVuZ3RoP3N1bShidC5jYWxsfHxbXSxDVVJXLnByaW9yKTpudWxsOwogIGNvbnN0IHRvdENvbXA9c3VtKGJ0LmNvbXB8fFtdLENVUlcuY3VyKSwgdG90Q29tcFA9Q1VSVy5wcmlvci5sZW5ndGg/c3VtKGJ0LmNvbXB8fFtdLENVUlcucHJpb3IpOm51bGw7CiAgbGV0IGNhcmRzPWA8ZGl2IGNsYXNzPSJrcGlzIj5gOwogIGNvbnN0IFU9Qi51bml0fHwi6rG0IjsKICBjYXJkcys9a3BpKCLsoITssrQgIitCLnByaW1hcnlMYWJlbCx0b3RDYWxsLHRvdENhbGxQLHt1bml0OlV9KTsKICBjYXJkcys9a3BpKCLqtJHqs6AgIitCLnByaW1hcnlMYWJlbCxjLmNhbGwscCYmcC5jYWxsLHtwcmltYXJ5OnRydWUsdW5pdDpVfSk7CiAgaWYoQi5zaG93Q29tcCl7IGNhcmRzKz1rcGkoIuyghOyytCAiK0IuY29tcExhYmVsLHRvdENvbXAsdG90Q29tcFAse3VuaXQ6VX0pOyBjYXJkcys9a3BpKCLqtJHqs6AgIitCLmNvbXBMYWJlbCxjLmNvbXAscCYmcC5jb21wLHt1bml0OlV9KTsgfQogIGNhcmRzKz1rcGkoIuq0keqzoOu5hCIsYy5jb3N0LHAmJnAuY29zdCx7d29uOnRydWV9KTsKICBpZihCLmR1YWxDcGEpe2NvbnN0IGQxPXg9PnguY2FsbD94LmNvc3QveC5jYWxsOm51bGwsZDI9eD0+eC5jb21wP3guY29zdC94LmNvbXA6bnVsbDtjYXJkcys9a3BpKEIuY3BhTGFiZWwsZDEoYykscCYmZDEocCkse3dvbjp0cnVlLGdvb2RVcDpmYWxzZX0pK2twaShCLmNvbXBDcGFMYWJlbCxkMihjKSxwJiZkMihwKSx7d29uOnRydWUsZ29vZFVwOmZhbHNlfSk7fQogIGVsc2V7Y29uc3QgY3BhPXg9Pntjb25zdCBkPUIuc2hvd0NvbXA/eC5jb21wOnguY2FsbDtyZXR1cm4gZD94LmNvc3QvZDpudWxsO307Y2FyZHMrPWtwaShCLmNwYUxhYmVsLGNwYShjKSxwJiZjcGEocCkse3dvbjp0cnVlLGdvb2RVcDpmYWxzZX0pO30KICBjYXJkcys9YDwvZGl2PmA7CiAgLy8g7KCE7LK0IOuMgOu5hCDqtJHqs6Ag67mE7KSRIOKAlCDrtoTsnpDripQgJ+yghOyytCDqtJHqs6Ag6riw7JesJyjrqqnsoIEr64uk66W4IOuqqeyggSwgb3JnIOygnOyZuCkuIOu5hOyaqS9DUEHripQg66qp7KCB66eMKOychCBLUEkpCiAgY29uc3QgYWRBbGw9YWdnKENVUlcuY3VyLHI9PnIuY2ghPT0ib3JnIikuY2FsbDsKICBjb25zdCBhZFBjdD10b3RDYWxsP01hdGgubWluKDEwMCxhZEFsbC90b3RDYWxsKjEwMCk6MDsKICBjb25zdCBzaGFyZUJhcj1gPGRpdiBjbGFzcz0iY2FyZCIgc3R5bGU9InBhZGRpbmc6MTFweCAxNXB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpiYXNlbGluZTtnYXA6MTJweDtmbGV4LXdyYXA6d3JhcCI+PGRpdiBjbGFzcz0iY3N1YiIgc3R5bGU9Im1hcmdpbjowIj7qtJHqs6Ag6riw7JesIOu5hOykkTwvZGl2PjxkaXYgc3R5bGU9ImZvbnQtc2l6ZToyMHB4O2ZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1hY2NlbnQpO2xldHRlci1zcGFjaW5nOi0wLjVweCI+JHthZFBjdC50b0ZpeGVkKGFkUGN0PDEwPzE6MCl9JTwvZGl2PjxkaXYgY2xhc3M9ImNzdWIiIHN0eWxlPSJtYXJnaW46MCI+6rSR6rOgICR7ZihhZEFsbCl9IC8g7KCE7LK0ICR7Zih0b3RDYWxsKX08L2Rpdj48L2Rpdj5gOwogIGNvbnN0IG5vQWQ9YWRBbGw9PT0wOwogIGxldCBodG1sPW5vQWQKICAgID8gYDxkaXYgY2xhc3M9ImJhbm5lciI+7J20IOq4sOqwhCDqtJHqs6Ag7KeR7ZaJIOyXhuydjCDigJQg7ISg7YOd7ZWcIOq4sOqwhOyXkCAke0IubGFiZWx9IOq0keqzoCDsp5HtlonsnbQg7JeG7Iq164uI64ukLiDsoITssrQg7J207Jqp65+J66eMIO2RnOyLnOuPvOyalC48L2Rpdj5gK2NhcmRzCiAgICA6IGA8ZGl2IGNsYXNzPSJub3RlbGluZSIgc3R5bGU9ImxpbmUtaGVpZ2h0OjEuNyI+PGI+7KCE7LK0PC9iPiA9IOyghCDssYTrhJA8YnI+PGI+6rSR6rOgL+q0keqzoOu5hC9DUEE8L2I+ID0g66qp7KCBIOy6oO2OmOyduOunjDxicj48Yj7qtJHqs6Ag6riw7JesIOu5hOykkTwvYj4gPSDsoITssrQg6rSR6rOgIOq4sOyXrDwvZGl2PmArY2FyZHMrc2hhcmVCYXI7CiAgaWYoIW5vQWQgJiYgQi5ub3RlKSBodG1sKz1gPGRpdiBjbGFzcz0iY3N1YiIgc3R5bGU9Im1hcmdpbjotNnB4IDAgMTJweCI+4oC7ICR7Qi5ub3RlfTwvZGl2PmA7CiAgaWYoaWQ9PT0ibG91bmdlIikgaHRtbD1sb3VuZ2VFbnRyeShDVVJXKStodG1sOwogIGh0bWwrPWJ1Y2tldFRyZW5kKEIscm93cyxDVVJXKTsKICBpZighbm9BZCl7CiAgICBjb25zdCBDPWJ1Y2tldENvbHMoQik7CiAgICBjb25zdCBraW5kcz1CLnNob3dPcmc/WyJnb29nbGUiLCJpbnN0YSIsImV0YyIsInhhZHMiLCJvcmciXTpbImdvb2dsZSIsImluc3RhIiwiZXRjIiwieGFkcyJdOwogICAgbGV0IGJvZHk9IiI7CiAgICBraW5kcy5mb3JFYWNoKGtpbmQ9Pntjb25zdCBycz1yb3dzLmZpbHRlcihyPT5yLmNoPT09a2luZCk7aWYocnMubGVuZ3RoKWJvZHkrPXNlY0Jsb2NrKHJzLGtpbmQsQixDLm5jb2wpO30pOwogICAgY29uc3QgaGFzQ3I9cm93cy5zb21lKHI9PnIuY2ghPT0ib3JnIiYmci5jcik7CiAgICBjb25zdCBjckJ0bj1oYXNDcj9gPGRpdiBjbGFzcz0ic2VnIiBzdHlsZT0ibWFyZ2luOjJweCAwIDEwcHgiPjxidXR0b24gY2xhc3M9IiR7Q1JTSE9XW2lkXT8nb24nOicnfSIgZGF0YS1jcj0iJHtpZH0iPuyGjOyerOq5jOyngCDrs7TquLA8L2J1dHRvbj48L2Rpdj5gOicnOwogICAgaHRtbCs9Y3JCdG4rYDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9InNjcm9sbCI+PHRhYmxlIHN0eWxlPSJtaW4td2lkdGg6NjgwcHg7dGFibGUtbGF5b3V0OmZpeGVkIj4ke0MuY29sfSR7Qy5oZWFkfTx0Ym9keT4ke2JvZHl9PC90Ym9keT48L3RhYmxlPjwvZGl2PjwvZGl2PmA7CiAgfQogIHJldHVybiBodG1sOwp9CgovKiAtLS0tIOyFuCAtLS0tICovCmxldCBUQUI9InN1bW1hcnkiOwpmdW5jdGlvbiBkcmlsbEFkKG1ldHJpYyxpZHgpe3JldHVybiAoRFJJTEwmJkRSSUxMLnJvd3MpP0RSSUxMLnJvd3MucmVkdWNlKChhLHIpPT5hK3N1bShyLnZbbWV0cmljXXx8W10saWR4KSwwKTowO30KZnVuY3Rpb24gYWRTdWIobWV0cmljLHRvdGFsLFcpe2NvbnN0IGFkPWRyaWxsQWQobWV0cmljLFcuY3VyKTtjb25zdCBwPXRvdGFsP2FkL3RvdGFsKjEwMDowO3JldHVybiBgPGRpdiBzdHlsZT0iZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tYWNjZW50KTtmb250LXdlaWdodDo3MDA7bWFyZ2luLXRvcDo3cHg7cGFkZGluZy10b3A6NnB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHZhcigtLWxpbmUpIj7qtJHqs6AgJHtmKGFkKX0gLyAke3AudG9GaXhlZCgxKX0lPC9kaXY+YDt9CmZ1bmN0aW9uIGRyaWxsQmxvY2soVyl7CiAgaWYoIURSSUxMfHwhRFJJTEwucm93c3x8IURSSUxMLnJvd3MubGVuZ3RoKSByZXR1cm4gJyc7CiAgY29uc3QgbWs9RFJJTExNRVRSSUM7CiAgY29uc3Qgb3B0cz1EUklMTC5tZXRyaWNzLm1hcChtPT5gPG9wdGlvbiB2YWx1ZT0iJHttWzBdfSIgJHttWzBdPT09bWs/J3NlbGVjdGVkJzonJ30+JHttWzFdfTwvb3B0aW9uPmApLmpvaW4oJycpOwogIGNvbnN0IHJvb3Q9e2NoaWxkcmVuOnt9fTsKICBEUklMTC5yb3dzLmZvckVhY2gocj0+ewogICAgY29uc3QgdmFsPXN1bShyLnZbbWtdfHxbXSxXLmN1cik7IGlmKCF2YWwpIHJldHVybjsKICAgIGNvbnN0IHBhdGg9W3IuY2gsci5jYW1wLHIuYWcsci5jcl07IGxldCBub2RlPXJvb3QsIGtleT0nJzsKICAgIHBhdGguZm9yRWFjaCgoc2VnLGRlcHRoKT0+eyBrZXkrPSdcdTAwMDEnK3NlZzsKICAgICAgbm9kZS5jaGlsZHJlbltrZXldPW5vZGUuY2hpbGRyZW5ba2V5XXx8e25hbWU6c2VnLGRlcHRoLHZhbDowLGtleSxjaGlsZHJlbjp7fX07CiAgICAgIG5vZGUuY2hpbGRyZW5ba2V5XS52YWwrPXZhbDsgbm9kZT1ub2RlLmNoaWxkcmVuW2tleV07IH0pOwogIH0pOwogIGNvbnN0IGFkVG90YWw9T2JqZWN0LnZhbHVlcyhyb290LmNoaWxkcmVuKS5yZWR1Y2UoKGEsYyk9PmErYy52YWwsMCl8fDE7CiAgbGV0IGJvZHk9Jyc7CiAgZnVuY3Rpb24gd2Fsayhub2RlKXsKICAgIE9iamVjdC52YWx1ZXMobm9kZS5jaGlsZHJlbikuc29ydCgoYSxiKT0+Yi52YWwtYS52YWwpLmZvckVhY2goYz0+ewogICAgICBpZihjLnZhbDw9MCkgcmV0dXJuOwogICAgICBjb25zdCBoYXNDaD1jLmRlcHRoPDMgJiYgT2JqZWN0LmtleXMoYy5jaGlsZHJlbikubGVuZ3RoPjA7CiAgICAgIGNvbnN0IG9wZW49REVYUC5oYXMoYy5rZXkpLCBwY3Q9Yy52YWwvYWRUb3RhbCoxMDA7CiAgICAgIGJvZHkrPWA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7cGFkZGluZzo1cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCB2YXIoLS1saW5lKTtmb250LXNpemU6MTNweDtwYWRkaW5nLWxlZnQ6JHs2K2MuZGVwdGgqMTh9cHgiPmAKICAgICAgICArKGhhc0NoP2A8YnV0dG9uIGRhdGEtZGV4cD0iJHtlbmNvZGVVUklDb21wb25lbnQoYy5rZXkpfSIgc3R5bGU9ImJvcmRlcjpub25lO2JhY2tncm91bmQ6bm9uZTtjdXJzb3I6cG9pbnRlcjtjb2xvcjp2YXIoLS1zdWIpO2ZvbnQtc2l6ZToxMnB4O3dpZHRoOjE2cHg7cGFkZGluZzowIj4ke29wZW4/J+KWvic6J+KWuCd9PC9idXR0b24+YDonPHNwYW4gc3R5bGU9ImRpc3BsYXk6aW5saW5lLWJsb2NrO3dpZHRoOjE2cHgiPjwvc3Bhbj4nKQogICAgICAgICtgPHNwYW4gc3R5bGU9ImZsZXg6MTtvdmVyZmxvdzpoaWRkZW47dGV4dC1vdmVyZmxvdzplbGxpcHNpczt3aGl0ZS1zcGFjZTpub3dyYXA7JHtjLmRlcHRoPT09MD8nZm9udC13ZWlnaHQ6NzAwJzonJ30iPiR7Yy5uYW1lfTwvc3Bhbj5gCiAgICAgICAgK2A8c3BhbiBzdHlsZT0iZm9udC12YXJpYW50LW51bWVyaWM6dGFidWxhci1udW1zO2ZvbnQtd2VpZ2h0OjcwMCI+JHtmKGMudmFsKX0gPHNwYW4gY2xhc3M9ImRpbSIgc3R5bGU9ImZvbnQtd2VpZ2h0OjQwMCI+LyAke3BjdC50b0ZpeGVkKDEpfSU8L3NwYW4+PC9zcGFuPjwvZGl2PmA7CiAgICAgIGlmKG9wZW4mJmhhc0NoKSB3YWxrKGMpOwogICAgfSk7CiAgfQogIHdhbGsocm9vdCk7CiAgY29uc3QgbW5hbWU9KERSSUxMLm1ldHJpY3MuZmluZChtPT5tWzBdPT09bWspfHxbJycsJyddKSBbMV07CiAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJjYXJkIj48aDM+6rSR6rOgIOq4sOyXrCA8c3BhbiBjbGFzcz0ibWluaSI+KOyxhOuEkCDilrgg7Lqg7Y6Y7J24IOKWuCDslaDrk5zqt7jro7kg4pa4IOyGjOyerCDsiJwpPC9zcGFuPjwvaDM+CiAgICA8ZGl2IHN0eWxlPSJtYXJnaW4tYm90dG9tOjEwcHgiPjxzZWxlY3QgaWQ9ImRyaWxsc2VsIiBzdHlsZT0iZm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTNweDtmb250LXdlaWdodDo3MDA7cGFkZGluZzo2cHggMTBweDtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6N3B4O2JhY2tncm91bmQ6dmFyKC0tY2FyZCk7Y29sb3I6dmFyKC0taW5rKSI+JHtvcHRzfTwvc2VsZWN0PjwvZGl2PgogICAgPGRpdj4ke2JvZHl8fCc8ZGl2IGNsYXNzPSJjc3ViIj7shKDtg50g6riw6rCEIOuNsOydtO2EsCDsl4bsnYw8L2Rpdj4nfTwvZGl2PjwvZGl2PmA7Cn0KZnVuY3Rpb24gZnVubmVsQmxvY2soKXsKICBpZighRlVOTkVMfHwhRlVOTkVMLnJvd3N8fCFGVU5ORUwucm93cy5sZW5ndGgpIHJldHVybiAnJzsKICBjb25zdCB0b3Q9RlVOTkVMLnJvd3MuZmluZChyPT5yWzBdPT09J+yghOyytCcpfHxGVU5ORUwucm93c1swXTsKICBjb25zdCBpbnN0PXRvdFsxXSwgc2c9dG90WzJdLCBzdmM9dG90WzNdOwogIGNvbnN0IGkycz1pbnN0P3NnL2luc3QqMTAwOjA7CiAgY29uc3QgYWNxPWA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6dmFyKC0tYWNjZW50LXNvZnQpO2JvcmRlci1yYWRpdXM6MTBweDtwYWRkaW5nOjExcHggMTRweDttYXJnaW4tYm90dG9tOjE0cHg7Zm9udC1zaXplOjEzcHgiPjxzcGFuPjxiPu2ajeuTnTwvYj4gJm5ic3A77ISk7LmYICR7ZihpbnN0KX0g4oaSIOqwgOyehSAke2Yoc2cpfTwvc3Bhbj48c3BhbiBzdHlsZT0iY29sb3I6dmFyKC0tYWNjZW50KTtmb250LXdlaWdodDo4MDAiPuyEpOy5mCDrjIDruYQgJHtpMnMudG9GaXhlZCgxKX0lPC9zcGFuPjwvZGl2PmA7CiAgY29uc3Qgc3RhZ2VzPVtbIu2ajOybkOqwgOyehSIsc2ddLFsi7ISc67mE7IqkIOydtOyaqSIsc3ZjXV07CiAgY29uc3QgYmFycz1zdGFnZXMubWFwKChzLGkpPT57Y29uc3QgcD1zZz9zWzFdL3NnKjEwMDowOwogICAgcmV0dXJuIGA8ZGl2IHN0eWxlPSJtYXJnaW46N3B4IDAiPjxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2Vlbjtmb250LXNpemU6MTJweDttYXJnaW4tYm90dG9tOjNweCI+PHNwYW4+JHtzWzBdfTwvc3Bhbj48c3BhbiBjbGFzcz0iZGltIj4ke2k9PT0wP2Ake2Yoc1sxXSl966qFICjqsIDsnoUgMTAwJSlgOmAke2Yoc1sxXSl966qFIC8g6rCA7J6FIOuMgOu5hCAke3AudG9GaXhlZCgxKX0lYH08L3NwYW4+PC9kaXY+PGRpdiBzdHlsZT0iYmFja2dyb3VuZDp2YXIoLS1hY2NlbnQtc29mdCk7Ym9yZGVyLXJhZGl1czo2cHg7aGVpZ2h0OjEzcHgiPjxkaXYgc3R5bGU9IndpZHRoOiR7cC50b0ZpeGVkKDEpfSU7aGVpZ2h0OjEwMCU7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQpO2JvcmRlci1yYWRpdXM6NnB4Ij48L2Rpdj48L2Rpdj48L2Rpdj5gO30pLmpvaW4oJycpOwogIGNvbnN0IGNodXJuPU1hdGgubWF4KDAsc2ctc3ZjKTsKICBjb25zdCByb3dzPUZVTk5FTC5yb3dzLmZpbHRlcihyPT5yWzBdIT09J+yghOyytCcpLm1hcChyPT57Y29uc3QgczE9clsxXT9yWzJdL3JbMV0qMTAwOjAsczI9clsyXT9yWzNdL3JbMl0qMTAwOjA7CiAgICByZXR1cm4gYDx0cj48dGQgY2xhc3M9Im5hbWUiPiR7clswXX08L3RkPjx0ZCBjbGFzcz0iZGltIj4ke2YoclsxXSl9PC90ZD48dGQ+JHtmKHJbMl0pfSA8c3BhbiBjbGFzcz0iZGltIj4vICR7czEudG9GaXhlZCgwKX0lPC9zcGFuPjwvdGQ+PHRkPiR7ZihyWzNdKX0gPHNwYW4gY2xhc3M9ImRpbSI+LyAke3MyLnRvRml4ZWQoMCl9JTwvc3Bhbj48L3RkPjwvdHI+YDt9KS5qb2luKCcnKTsKICByZXR1cm4gYDxkaXYgY2xhc3M9ImNhcmQiPjxoMz7si6Dqt5zqs6DqsJ0g7Zmc7ISx7ZmUIO2NvOuEkCA8c3BhbiBjbGFzcz0ibWluaSI+6rCA7J6F7J6QIOq4sOykgCAvICR7RlVOTkVMLndpbmRvd30g7L2U7Zi47Yq4PC9zcGFuPjwvaDM+CiAgICAke2FjcX0KICAgIDxkaXYgY2xhc3M9ImNzdWIiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjZweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0taW5rKSI+7Zmc7ISx7ZmUICjqsIDsnoUgMTAwJSDquLDspIApPC9kaXY+CiAgICAke2JhcnN9CiAgICA8ZGl2IGNsYXNzPSJjc3ViIiBzdHlsZT0ibWFyZ2luOjEwcHggMCAycHgiPuqwgOyehSDtm4Qg7ISc67mE7IqkIOuvuOydtOyaqSjsnbTtg4gpIDxiIHN0eWxlPSJjb2xvcjp2YXIoLS1pbmspIj4ke2YoY2h1cm4pfeuqhTwvYj4g4oCUIOqwgOyehSAke2Yoc2cpfeuqhSDspJEgJHsoY2h1cm4vKHNnfHwxKSoxMDApLnRvRml4ZWQoMSl9JTwvZGl2PgogICAgPGRpdiBjbGFzcz0iY3N1YiIgc3R5bGU9Im1hcmdpbjoxNHB4IDAgN3B4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS1pbmspIj7ssYTrhJDrs4Qg7KCE7ZmYIDxzcGFuIGNsYXNzPSJkaW0iIHN0eWxlPSJmb250LXdlaWdodDo0MDAiPijruYTqtJHqs6AgPSDsmKTqsIDri4nCt0NSTSDrk7Eg67mE6rSR6rOgIOycoOyehSk8L3NwYW4+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzY3JvbGwiPjx0YWJsZSBzdHlsZT0ibWluLXdpZHRoOjQ0MHB4Ij48dGhlYWQ+PHRyPjx0aD7ssYTrhJA8L3RoPjx0aD7shKTsuZg8L3RoPjx0aD7qsIDsnoU8YnI+PHNwYW4gY2xhc3M9ImRpbSIgc3R5bGU9ImZvbnQtd2VpZ2h0OjQwMCI+7ISk7LmYIOuMgOu5hDwvc3Bhbj48L3RoPjx0aD7shJzruYTsiqQg7J207JqpPGJyPjxzcGFuIGNsYXNzPSJkaW0iIHN0eWxlPSJmb250LXdlaWdodDo0MDAiPuqwgOyehSDrjIDruYQ8L3NwYW4+PC90aD48L3RyPjwvdGhlYWQ+PHRib2R5PiR7cm93c308L3Rib2R5PjwvdGFibGU+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjc3ViIiBzdHlsZT0ibWFyZ2luLXRvcDo3cHgiPuyLoOq3nCDqs6DqsJ0gPSA8Yj7qsIDsnoXsnpA8L2I+IOq4sOykgCAvIOyEnOu5hOyKpCDsnbTsmqkgPSDqsIDsnoXsnpAg7KSRIOqwgOyehSDri7nsnbwg7LKrIOqysOygnC/snbTsmqkocHVyY2hhc2Vfc3RhcnRlZCkgLyDshLHsiJkg7L2U7Zi47Yq4PC9kaXY+PC9kaXY+YDsKfQpmdW5jdGlvbiByZW5kZXJKb3VybmV5KFcpewogIGNvbnN0IHByPVcucHJpb3IubGVuZ3RoOwogIGNvbnN0IGluc3Q9c3VtKFRPVEFMUy5pbnN0YWxsfHxbXSxXLmN1ciksIHN1PXN1bShUT1RBTFMuc3UsVy5jdXIpOwogIGNvbnN0IGluc3RQPXByP3N1bShUT1RBTFMuaW5zdGFsbHx8W10sVy5wcmlvcik6bnVsbCwgc3VQPXByP3N1bShUT1RBTFMuc3UsVy5wcmlvcik6bnVsbDsKICBjb25zdCBrcGlzPWA8ZGl2IGNsYXNzPSJrcGlzIj4ke2twaSgi7JWx7ISk7LmYIixpbnN0LGluc3RQLHt1bml0OiLrqoUifSl9JHtrcGkoIu2ajOybkOqwgOyehSIsc3Usc3VQLHtwcmltYXJ5OnRydWUsdW5pdDoi66qFIn0pfTwvZGl2PmA7CiAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJub3RlbGluZSI+PGI+7Iug6rec6rOg6rCdIOuLueydvCDsi6TsoIE8L2I+IOKAlCDshKDtg50g6riw6rCEIOyEpOy5mMK36rCA7J6F6rO8LCDqsIDsnoXsnpDsnZgg6rCA7J6FIOuLueydvCDtlbXsi6ztlonrj5kg7KCE7ZmY7JyoPC9kaXY+YAogICAgK2twaXMrb25iQmxvY2soVyk7Cn0KZnVuY3Rpb24gcmVuZGVyKCl7CiAgY29uc3QgVz13aW5kb3dzKCk7CiAgY29uc3QgY21wPSFXLnByaW9yLmxlbmd0aD8i67mE6rWQ6riw6rCEIOyXhuydjCI6KFcuY3VyLmxlbmd0aD09PTE/IuyghOyjvCDrj5nsmpTsnbwg64yA67mEIjpg7KCE7KO8IOuPmeq4sOqwhCDrjIDruYQoJHtXLmN1ci5sZW5ndGh97J28KWApOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJyYW5nZWxhYmVsIikuaW5uZXJIVE1MPWA8Yj4ke0RBWVNbUkFdfSB+ICR7REFZU1tSQl19PC9iPiAvICR7Vy5jdXIubGVuZ3RofeydvCAvICR7Y21wfWA7CiAgY29uc3QgbW9kZVNlZz1gPGRpdiBjbGFzcz0ic2VnIiBzdHlsZT0iYWxpZ24tc2VsZjpmbGV4LXN0YXJ0Ij48YnV0dG9uIGNsYXNzPSIke01PREU9PT0nYWRzJz8nb24nOicnfSIgZGF0YS1tb2RlPSJhZHMiPuq0keqzoCDtmqjsnKg8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSIke01PREU9PT0nam91cm5leSc/J29uJzonJ30iIGRhdGEtbW9kZT0iam91cm5leSI+7Iug6rec6rOg6rCdPC9idXR0b24+PC9kaXY+YDsKICBpZihNT0RFPT09J2pvdXJuZXknKXsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ0YWJzIikuaW5uZXJIVE1MPW1vZGVTZWc7CiAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgidmlldyIpLmlubmVySFRNTD1yZW5kZXJKb3VybmV5KFcpOwogICAgcmV0dXJuOwogIH0KICBjb25zdCBhY3RpdmU9QlVDS0VUUy5maWx0ZXIoYj0+YnVja2V0QWN0aXZlKGIuaWQpKTsKICBpZihUQUIhPT0ic3VtbWFyeSImJiFhY3RpdmUuc29tZShiPT5iLmlkPT09VEFCKSlUQUI9InN1bW1hcnkiOwogIGNvbnN0IHRhYnM9W1sic3VtbWFyeSIsIuyalOyVvSJdXS5jb25jYXQoYWN0aXZlLm1hcChiPT5bYi5pZCxiLmxhYmVsXSkpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ0YWJzIikuaW5uZXJIVE1MPW1vZGVTZWcrYDxkaXYgY2xhc3M9InNlZyIgc3R5bGU9ImFsaWduLXNlbGY6ZmxleC1zdGFydDtmbGV4LXdyYXA6d3JhcCI+YCt0YWJzLm1hcCh0PT5gPGJ1dHRvbiBjbGFzcz0idGFiICR7dFswXT09PVRBQj8nb24nOicnfSIgZGF0YS10PSIke3RbMF19Ij4ke3RbMV19PC9idXR0b24+YCkuam9pbignJykrYDwvZGl2PmA7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInZpZXciKS5pbm5lckhUTUw9KFRBQj09PSJzdW1tYXJ5Iik/cmVuZGVyU3VtbWFyeSgpOnJlbmRlckJ1Y2tldChUQUIpOwp9CihmdW5jdGlvbiBpbml0RGF0ZXMoKXsKICBjb25zdCBmZD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgiZnJvbURhdGUiKSwgdGQ9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInRvRGF0ZSIpOwogIGZkLm1pbj10ZC5taW49REFZU1swXTsgZmQubWF4PXRkLm1heD1EQVlTW0RBWVMubGVuZ3RoLTFdOwogIGZkLnZhbHVlPURBWVNbUkFdOyB0ZC52YWx1ZT1EQVlTW1JCXTsKICBmdW5jdGlvbiBhcHBseSgpewogICAgbGV0IGE9REFZUy5pbmRleE9mKGZkLnZhbHVlKSwgYj1EQVlTLmluZGV4T2YodGQudmFsdWUpOwogICAgaWYoYTwwKWE9UkE7IGlmKGI8MCliPVJCOyBpZihhPmIpe2NvbnN0IHQ9YTthPWI7Yj10O30KICAgIFJBPWE7IFJCPWI7IGZkLnZhbHVlPURBWVNbUkFdOyB0ZC52YWx1ZT1EQVlTW1JCXTsgcmVuZGVyKCk7CiAgfQogIGZkLmFkZEV2ZW50TGlzdGVuZXIoImNoYW5nZSIsYXBwbHkpOyB0ZC5hZGRFdmVudExpc3RlbmVyKCJjaGFuZ2UiLGFwcGx5KTsKICAvLyDruaDrpbgg7ISg7YOdOiDstZzqt7wgNy8xNC8zMOydvAogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJxdWlja3BpY2siKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsZT0+e2NvbnN0IGJ0bj1lLnRhcmdldC5jbG9zZXN0KCJidXR0b24iKTtpZighYnRuKXJldHVybjtjb25zdCBkPStidG4uZGF0YXNldC5kO1JCPURBWVMubGVuZ3RoLTE7UkE9TWF0aC5tYXgoMCxEQVlTLmxlbmd0aC1kKTtmZC52YWx1ZT1EQVlTW1JBXTt0ZC52YWx1ZT1EQVlTW1JCXTtyZW5kZXIoKTt9KTsKfSkoKTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInRhYnMiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsZT0+e2NvbnN0IGI9ZS50YXJnZXQuY2xvc2VzdCgiLnRhYiIpO2lmKCFiKXJldHVybjtUQUI9Yi5kYXRhc2V0LnQ7cmVuZGVyKCk7fSk7CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ0YWJzIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGU9Pntjb25zdCBiPWUudGFyZ2V0LmNsb3Nlc3QoIltkYXRhLW1vZGVdIik7aWYoIWIpcmV0dXJuO01PREU9Yi5kYXRhc2V0Lm1vZGU7cmVuZGVyKCk7fSk7CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ2aWV3IikuYWRkRXZlbnRMaXN0ZW5lcigiY2hhbmdlIixlPT57Y29uc3Qgcz1lLnRhcmdldC5jbG9zZXN0KCIjZHJpbGxzZWwiKTtpZighcylyZXR1cm47RFJJTExNRVRSSUM9cy52YWx1ZTtyZW5kZXIoKTt9KTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInZpZXciKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsZT0+e2NvbnN0IGI9ZS50YXJnZXQuY2xvc2VzdCgiW2RhdGEtZGV4cF0iKTtpZighYilyZXR1cm47Y29uc3Qgaz1kZWNvZGVVUklDb21wb25lbnQoYi5kYXRhc2V0LmRleHApO0RFWFAuaGFzKGspP0RFWFAuZGVsZXRlKGspOkRFWFAuYWRkKGspO3JlbmRlcigpO30pOwpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgidmlldyIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixlPT57Y29uc3QgYj1lLnRhcmdldC5jbG9zZXN0KCJbZGF0YS1idHJlbmRdIik7aWYoIWIpcmV0dXJuO0JUUks9Yi5kYXRhc2V0LmJ0cmVuZDtyZW5kZXIoKTt9KTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInZpZXciKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsZT0+e2NvbnN0IGI9ZS50YXJnZXQuY2xvc2VzdCgiW2RhdGEtbGdrXSIpO2lmKCFiKXJldHVybjtMR0s9Yi5kYXRhc2V0LmxnaztyZW5kZXIoKTt9KTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInZpZXciKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsZT0+e2NvbnN0IGI9ZS50YXJnZXQuY2xvc2VzdCgiW2RhdGEtdHJlbmRdIik7aWYoIWIpcmV0dXJuO1RSRU5ES0VZPWIuZGF0YXNldC50cmVuZDtyZW5kZXIoKTt9KTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInZpZXciKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsZT0+e2NvbnN0IGI9ZS50YXJnZXQuY2xvc2VzdCgiW2RhdGEtY3JdIik7aWYoIWIpcmV0dXJuO0NSU0hPV1tiLmRhdGFzZXQuY3JdPSFDUlNIT1dbYi5kYXRhc2V0LmNyXTtyZW5kZXIoKTt9KTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoImZvb3QiKS5pbm5lckhUTUw9IuyalOyVvT3soITssrQg7Jyg7J6FKOq0keqzoCvruYTqtJHqs6ApLCDqtJHqs6A967O06528IC8g67KE7YK3IO2DrT3qtJHqs6Ag6riw7Jes66eMKOuqqeyggSDsuqDtjpjsnbgpLCDquLDtg4Ag6rSR6rOgPeu5hOyaqSDrr7jsl7Drj5ko7Iuk7KCB66eMKTxicj7qtJHqs6Av6rSR6rOg67mEL0NQQT3rqqnsoIEg7Lqg7Y6Y7J2466eMLCDruYTspJE97KCE7LK0IOq0keqzoCDquLDsl6wgLyDsmKjrs7TrlKkg7KCE7ZmY7JyoPeqwgOyehSDri7nsnbwg7L2U7Zi47Yq4IOq4sOykgDxicj7rs4DtmZTsnKg97KCE7KO8IOuPmeq4sOqwhCjsmpTsnbwg66ee7LakKSDrjIDruYQgKOyngO2RnOKGkSDstIjroZ0sIOu5hOyaqeKGkSDruajqsJUpIC8g6riw6rCE7J2AIOuLrOugpeycvOuhnCDshKDtg50o7LWc6re8IDYw7J28IOuCtCksIOyDgeuLqCBb6rSR6rOgIO2aqOycqF3Ct1vqs6DqsJ0g7Jes7KCVXSDrt7Ag7KCE7ZmYIC8g7YOt7J2AIOyLpOyggSAw7J2066m0IOyekOuPmSDsiKjquYAiOwpjb25zdCBfZ209ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoImdlbm1ldGEiKTsgaWYoX2dtKSBfZ20udGV4dENvbnRlbnQ9REFUQS5nZW5lcmF0ZWR8fCIiOwpyZW5kZXIoKTsKPC9zY3JpcHQ+CjwvYm9keT4KPC9odG1sPgo=").decode("utf-8")


if __name__=="__main__":
    main()
