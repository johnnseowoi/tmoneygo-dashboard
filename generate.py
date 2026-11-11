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
M_TAXI_CALL="app_custom_purchase_started"         # 택시 호출 건수 (+ event_action=taxi)
M_TAXI_CALL_USERS="app_custom_users_purchase_started"  # 택시 호출 유저수 (+ event_action=taxi)
M_COST="cost_channel"
UNIQ_WINS=[7,14,30,60]   # 기간 전체 중복제거(유니크) 집계를 받을 빠른선택 구간 (1일=당일은 일별값=유니크라 불필요)
LEVT_WINS=[7,14,30]      # 라운지 이벤트별 사용량 집계 구간
LOUNGE_EVENTS=["view_lounge_main","view_lounge_feed","view_lounge_article","write_lounge_article_comment",
  "view_lounge_game","view_lounge_game_ver2","view_shortdrama","view_lounge_gagisiro","view_lounge_md","md_intro",
  "get_md_mission_game","get_md_mission_game_ver2","view_lounge_balance_list","view_lounge_balance_detail",
  "completed_lounge_balance","write_balance_article_comment","get_lounge_mission_visit","get_lounge_mission_newsread",
  "get_lounge_mission_newscomment","get_lounge_mission_game","get_lounge_mission_game_ver2","get_lounge_mission_game_helloKiitty",
  "get_lounge_mission_shortform","get_lounge_mission_balance_result","get_lounge_mission_gagisiro","get_lounge_mission_webtoon",
  "click_lounge_bottombar","click_lounge_subway_notification_empty","click_lounge_bus_notification",
  "click_lounge_subway_notification_gagisiro","click_lounge_publicmenu","click_lounge_newsread","click_lounge_gameZone",
  "click_lounge_webtoon","click_lounge_shortdrama","click_lounge_balance","click_lounge_englishEdu","click_lounge_mission_scroll",
  "view_gameZone","click_gameZone_helloKiitty","click_gameZone_hamsterTown","click_gameZone_AppleGame","click_gameZone_Omok",
  "click_gameZone_tilePop","click_gameZone_zellyPop","view_lounge_webtoon","view_lounge_englishEdu","view_lounge_game_helloKitty",
  "view_lounge_game_hamsterTown","view_lounge_game_AppleGame","view_lounge_game_Omok","view_lounge_game_tilePop","view_lounge_game_zellyPop"]
def _lcat(nm):
  n=nm.lower()
  if n.startswith("get_") and "mission" in n: return "미션"
  if n.startswith("completed_"): return "완료"
  if n.startswith("write_"): return "댓글"
  if n.startswith("click_"): return "클릭"
  if n.startswith("view_") or n=="md_intro": return "조회"
  return "기타"
M_INSTALL="app_installs"; M_INSTALL_USERS="app_install_users"   # 앱설치(유저수)
M_ACTIVE="app_active_users"                                  # 활성 유저수(App 전체 이벤트 기준, ≈MAU)
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
  rt=query(sess,frm,to,["event_date"],[M_INSTALL_USERS,M_ACTIVE,M_SIGNUP_USERS,M_TPCARD_USERS,M_LOUNGE_USERS])
  def totd(m): d=to_daily(rt,days,0,m); return d.get("_",[0]*len(days))
  totals={"install":totd(M_INSTALL_USERS),"visit":totd(M_ACTIVE),"su":totd(M_SIGNUP_USERS),"card":totd(M_TPCARD_USERS),"lounge":totd(M_LOUNGE_USERS)}
  # 2) 전체 택시호출 — 전체값은 귀속 필터(VT) 없이. VT를 걸면 오가닉(미귀속)이 빠져 전체가 과소집계됨
  _taxi_f=[{"dimension":"event_action","filterType":"IN","values":["taxi"]}]
  r=query(sess,frm,to,["event_date"],[M_TAXI_CALL,M_TAXI_CALL_USERS],_taxi_f)
  taxitot=to_daily(r,days,0,M_TAXI_CALL).get("_",[0]*len(days))
  taxitotu=to_daily(r,days,0,M_TAXI_CALL_USERS).get("_",[0]*len(days))   # 택시 호출 유저수(일별, 커스텀 구간용 연인원)
  # 1-c) 기간 전체 유니크(중복제거) — 빠른선택 구간(7/14/30/60일)별. event_date 없이 집계 → 에어브릿지가 기간 유니크 반환
  def _puniq(f,t):
    ru=query(sess,f,t,[],[M_INSTALL_USERS,M_ACTIVE,M_SIGNUP_USERS,M_TPCARD_USERS,M_LOUNGE_USERS])
    row=(ru[0] if ru else {}); rx=query(sess,f,t,[],[M_TAXI_CALL,M_TAXI_CALL_USERS],_taxi_f); rowx=(rx[0] if rx else {})
    return {"install":int(val(row,M_INSTALL_USERS)),"visit":int(val(row,M_ACTIVE)),"su":int(val(row,M_SIGNUP_USERS)),
            "card":int(val(row,M_TPCARD_USERS)),"lounge":int(val(row,M_LOUNGE_USERS)),
            "call":int(val(rowx,M_TAXI_CALL)),"callu":int(val(rowx,M_TAXI_CALL_USERS))}
  uniq={}; _last=datetime.date.fromisoformat(days[-1])
  for w in UNIQ_WINS:
    if w>len(days): continue
    cf=_last-datetime.timedelta(days=w-1); sh=((w+6)//7)*7
    pf=cf-datetime.timedelta(days=sh); pt=_last-datetime.timedelta(days=sh)
    try: uniq[str(w)]={"cur":_puniq(cf.isoformat(),_last.isoformat()),"prior":_puniq(pf.isoformat(),pt.isoformat())}
    except Exception as e: print(f"  ⚠️ {w}일 유니크 집계 실패:", str(e)[:120])
  # 1-d) 라운지 이벤트: 일별 시리즈(발생수/순유저) + 구간별 순유저(중복제거). 상단 날짜로 연동.
  _lef=[{"dimension":"event_name","filterType":"IN","values":LOUNGE_EVENTS}]
  levt_daily={}
  try:
    rr=query(sess,frm,to,["event_date","event_name"],["app_events","app_unique_users"],_lef)
    idx={d:i for i,d in enumerate(days)}
    for r in rr:
      gb=r.get("groupBys",[]); dt=gb[0] if len(gb)>0 else None; nm=gb[1] if len(gb)>1 else None
      if nm is None or dt not in idx: continue
      o=levt_daily.setdefault(nm,{"ev":[0]*len(days),"uu":[0]*len(days)})
      o["ev"][idx[dt]]=int(val(r,"app_events")); o["uu"][idx[dt]]=int(val(r,"app_unique_users"))
  except Exception as e: print("  ⚠️ 라운지 이벤트 일별 실패:", str(e)[:120])
  levt_uniq={}
  for w in LEVT_WINS:
    if w>len(days): continue
    cf=(_last-datetime.timedelta(days=w-1)).isoformat()
    try:
      rr=query(sess,cf,_last.isoformat(),["event_name"],["app_unique_users"],_lef)
      levt_uniq[str(w)]={(r.get("groupBys") or [""])[0]:int(val(r,"app_unique_users")) for r in rr}
    except Exception as e: print("  ⚠️ 라운지 이벤트 유니크 실패:", str(e)[:120])
  # 라운지 전체 순방문자(이벤트 1건 이상, 중복제거) — 일별 + 구간별
  lev_totdaily=[0]*len(days); lev_totuniq={}
  try:
    rt=query(sess,frm,to,["event_date"],["app_unique_users"],_lef)
    lev_totdaily=to_daily(rt,days,0,"app_unique_users").get("_",[0]*len(days))
    for w in LEVT_WINS:
      if w>len(days): continue
      cf=(_last-datetime.timedelta(days=w-1)).isoformat()
      ru=query(sess,cf,_last.isoformat(),[],["app_unique_users"],_lef)
      lev_totuniq[str(w)]=int(val((ru[0] if ru else {}),"app_unique_users"))
  except Exception as e: print("  ⚠️ 라운지 순방문자 집계 실패:", str(e)[:120])
  levt={"daily":levt_daily,"uniq":levt_uniq,"totDaily":lev_totdaily,"totUniq":lev_totuniq}
  # 3) 채널별 효율(광고): 가입+비용(필터 없이) / 택시호출(event_action=taxi) 따로
  adset=["google.adwords","facebook.business","adison","naver_bsa"]
  chf={"dimension":"channel","filterType":"IN","values":adset}
  r=query(sess,frm,to,["event_date","channel"],[M_SIGNUP_USERS],[chf,VT]); eff_su=to_daily(r,days,0,M_SIGNUP_USERS,1)
  r=query(sess,frm,to,["event_date","channel"],[M_INSTALL_USERS],[chf,VT]); eff_inst=to_daily(r,days,0,M_INSTALL_USERS,1)
  r=query(sess,frm,to,["event_date","channel"],[M_COST],[chf]); eff_cost=to_daily(r,days,0,M_COST,1)
  r=query(sess,frm,to,["event_date","channel"],[M_TAXI_CALL],[chf,{"dimension":"event_action","filterType":"IN","values":["taxi"]},VT]); eff_call=to_daily(r,days,0,M_TAXI_CALL,1)
  adeff=[{"name":disp(c),"cost":eff_cost.get(c,[0]*len(days)),"install":eff_inst.get(c,[0]*len(days)),"su":eff_su.get(c,[0]*len(days)),"call":eff_call.get(c,[0]*len(days))} for c in adset]
  # 광고(adset) 택시호출 유저수 — 유저 기준 광고 기여 비중용
  r=query(sess,frm,to,["event_date"],[M_TAXI_CALL_USERS],[chf,{"dimension":"event_action","filterType":"IN","values":["taxi"]},VT])
  taxiadu=to_daily(r,days,0,M_TAXI_CALL_USERS).get("_",[0]*len(days))
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
  # 5-a') 첫 결제/이용 서비스 분포 — 유저별 첫 purchase_started의 서비스(event_action), 일별(첫 결제는 1회성→합산 정확)
  firstbuy={}
  try:
    rr=query(sess,frm,to,["event_date","event_action"],["app_unique_users"],
             [{"dimension":"event_name","filterType":"IN","values":["purchase_started"]},
              {"dimension":"is_first_event_per_user_id","filterType":"IN","values":["true"]}])
    idxd={d:i for i,d in enumerate(days)}
    for r in rr:
      gb=r.get("groupBys",[]); dt=gb[0] if len(gb)>0 else None; ac=gb[1] if len(gb)>1 else None
      if not ac or dt not in idxd or ac=="pkg": continue
      firstbuy.setdefault(ac,[0]*len(days))[idxd[dt]]=int(val(r,"app_unique_users"))
    # 예약택시는 별도 이벤트(purchase_rvtaxi_started)
    rr2=query(sess,frm,to,["event_date"],["app_unique_users"],
              [{"dimension":"event_name","filterType":"IN","values":["purchase_rvtaxi_started"]},
               {"dimension":"is_first_event_per_user_id","filterType":"IN","values":["true"]}])
    ser=[0]*len(days)
    for r in rr2:
      gb=r.get("groupBys",[]); dt=gb[0] if gb else None
      if dt in idxd: ser[idxd[dt]]=int(val(r,"app_unique_users"))
    if sum(ser)>0: firstbuy["rvtaxi"]=ser
  except Exception as e: print("  ⚠️ 첫 결제 서비스 분포 실패:", str(e)[:120])
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
  rd =query(sess,frm,to,gb,[M_INSTALL_USERS,M_ACTIVE,M_SIGNUP_USERS,M_TPCARD_USERS,M_LOUNGE_USERS],[chf,VT])
  rdc=query(sess,frm,to,gb,[M_TAXI_CALL],[chf,{"dimension":"event_action","filterType":"IN","values":["taxi"]},VT])
  def dmap(r,metric):
    idx={d:i for i,d in enumerate(days)}; out={}
    for row in (r or []):
      g=row.get("groupBys",[]); d=g[0]
      if d not in idx: continue
      key=(g[1], g[2] if len(g)>2 else "", g[3] if len(g)>3 else "", g[4] if len(g)>4 else "")
      out.setdefault(key,[0]*len(days))[idx[d]] += val(row,metric)
    return out
  D_inst=dmap(rd,M_INSTALL_USERS); D_vis=dmap(rd,M_ACTIVE); D_su=dmap(rd,M_SIGNUP_USERS); D_card=dmap(rd,M_TPCARD_USERS); D_lo=dmap(rd,M_LOUNGE_USERS); D_call=dmap(rdc,M_TAXI_CALL)
  drill_rows=[]
  for key in (set(D_inst)|set(D_vis)|set(D_su)|set(D_card)|set(D_lo)|set(D_call)):
    ch,cp,ag,cr=key
    v={"install":D_inst.get(key,[0]*len(days)),"visit":D_vis.get(key,[0]*len(days)),"su":D_su.get(key,[0]*len(days)),"card":D_card.get(key,[0]*len(days)),
       "lounge":D_lo.get(key,[0]*len(days)),"call":D_call.get(key,[0]*len(days))}
    if sum(sum(s) for s in v.values())==0: continue
    drill_rows.append({"ch":disp(ch),"camp":(cp or "(캠페인 없음)"),"ag":(ag or "(애드그룹 없음)"),"cr":(cr or "(소재 없음)"),"v":v})
  drill={"metrics":[["install","앱설치"],["visit","활성 유저"],["su","회원가입"],["card","교통카드 등록"],["lounge","대중교통라운지 방문"],["call","택시호출"]],"rows":drill_rows}
  data={"generated":datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")+" 기준","days":days,"buckets":BUCKETS,
        "channels":channels,"taxitot":taxitot,"taxitotu":taxitotu,"totals":totals,"adeff":adeff,"onb":onb,"rows":rows,"camp":camp,"btot":btot,"onbNote":onb_note,"funnel":funnel,"retn":retn,"drill":drill,"uniq":uniq,"levt":levt,"firstbuy":firstbuy,"taxiadu":taxiadu}
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
  taxitot=dser(99,52000,0.5,n); taxitotu=dser(98,9500,0.45,n)
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
  drill={"metrics":[["install","앱설치"],["visit","활성 유저"],["su","회원가입"],["card","교통카드 등록"],["lounge","대중교통라운지 방문"],["call","택시호출"]],"rows":drill_rows}
  # 더미 기간 유니크: 반복지표(방문·라운지·택시호출 유저수)는 일별합의 일부, 1회성(설치·가입·카드)은 합≈유니크
  def _ws(arr,w): return sum(arr[-w:])
  uniq={}
  for w in UNIQ_WINS:
    cur={"install":_ws(totals["install"],w),"visit":round(_ws(totals["visit"],w)*0.28),"su":_ws(totals["su"],w),
         "card":_ws(totals["card"],w),"lounge":round(_ws(totals["lounge"],w)*0.5),
         "call":_ws(taxitot,w),"callu":round(_ws(taxitotu,w)*1.0)}
    uniq[str(w)]={"cur":cur,"prior":{k:round(v*0.93) for k,v in cur.items()}}
  import random as _rnd; _rnd.seed(7); levt_daily={}
  for i,nm in enumerate(LOUNGE_EVENTS):
    if _rnd.random()<0.08: continue   # 일부 이벤트는 데이터 없음
    base=int(100000/(i+1.5)); ev=[max(0,int(base*_rnd.uniform(0.5,1.5))) for _ in range(n)]
    uu=[max(0,int(e*_rnd.uniform(0.2,0.6))) for e in ev]; levt_daily[nm]={"ev":ev,"uu":uu}
  levt_uniq={}
  for w in LEVT_WINS:
    levt_uniq[str(w)]={nm:max(1,int(sum(o["uu"][-w:])*_rnd.uniform(0.3,0.6))) for nm,o in levt_daily.items()}
  lev_totuniq={str(w):int((max(levt_uniq[str(w)].values()) if levt_uniq.get(str(w)) else 30000)*_rnd.uniform(1.2,1.5)) for w in LEVT_WINS}
  _tbase=lev_totuniq.get('30',lev_totuniq.get(str(LEVT_WINS[-1]),30000))/30
  lev_totdaily=[max(0,int(_tbase*_rnd.uniform(0.7,1.3))) for _ in range(n)]
  levt={"daily":levt_daily,"uniq":levt_uniq,"totDaily":lev_totdaily,"totUniq":lev_totuniq}
  taxiadu=dser(97,130,0.5,n)
  firstbuy={}
  for ac,base in [("expbus",6000),("airbus",1400),("taxi",800),("korail",180),("rvtaxi",120),("bike",120),("scoot",45),("travel",38),("air",10),("pass_scoot",9),("rencar",3)]:
    firstbuy[ac]=[max(0,int(base*_rnd.uniform(0.6,1.4))) for _ in range(n)]
  return {"generated":"더미 샘플","days":days,"buckets":BUCKETS,"channels":channels,"taxitot":taxitot,"taxitotu":taxitotu,"totals":totals,
          "adeff":adeff,"onb":onb,"rows":rows,"camp":camp,"btot":btot,"onbNote":False,"funnel":funnel,"retn":retn,"drill":drill,"uniq":uniq,"levt":levt,"firstbuy":firstbuy,"taxiadu":taxiadu}

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
TEMPLATE_HTML=_b64.b64decode("PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9ImtvIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLCB2aWV3cG9ydC1maXQ9Y292ZXIiPgo8dGl0bGU+7Yuw66i464uIR08g6rSR6rOgIOyEseqzvCDthrXtlanrt7A8L3RpdGxlPgo8c3R5bGU+CiAgOnJvb3R7LS1iZzojZjRmNWY3Oy0tY2FyZDojZmZmOy0tbGluZTojZWFlN2YzOy0taW5rOiMyYTI2NDA7LS1zdWI6IzhiODZhMDstLWFjY2VudDojODM3MGU2Oy0tYWNjZW50LXNvZnQ6I2YxZWRmYzstLWdvb2Q6IzJlOWU1YjstLWJhZDojYzA0NTNiOy0tbXV0ZWQ6I2JkYjhjYzstLW9yZzojZDZkMmUyOy0tc2hhZG93OjAgMXB4IDJweCByZ2JhKDQyLDM4LDY0LC4wNSksMCAycHggOHB4IHJnYmEoNDIsMzgsNjQsLjAzNSk7Ym94LXNpemluZzpib3JkZXItYm94O3BhZGRpbmctdG9wOmVudihzYWZlLWFyZWEtaW5zZXQtdG9wLDBweCk7cGFkZGluZy1ib3R0b206ZW52KHNhZmUtYXJlYS1pbnNldC1ib3R0b20sMHB4KX0KICBAbWVkaWEgKHByZWZlcnMtY29sb3Itc2NoZW1lOmRhcmspezpyb290Om5vdChbZGF0YS10aGVtZT0ibGlnaHQiXSl7LS1iZzojMTUxMjFkOy0tY2FyZDojMWUxYTJhOy0tbGluZTojMmYyOTQxOy0taW5rOiNlY2U5ZjU7LS1zdWI6Izk0OGVhYTstLWFjY2VudDojYTU5M2YwOy0tYWNjZW50LXNvZnQ6IzI3MWQzZDstLW11dGVkOiM1ZjU4NzU7LS1vcmc6IzQxM2E1NDstLXNoYWRvdzowIDFweCAycHggcmdiYSgwLDAsMCwuNCksMCAycHggMTBweCByZ2JhKDAsMCwwLC4zKX19CiAgKntib3gtc2l6aW5nOmJvcmRlci1ib3g7bWFyZ2luOjA7cGFkZGluZzowfQogIGJvZHl7YmFja2dyb3VuZDp2YXIoLS1iZyk7Y29sb3I6dmFyKC0taW5rKTtmb250LWZhbWlseToiQXBwbGUgU0QgR290aGljIE5lbyIsIk1hbGd1biBHb3RoaWMiLCJQcmV0ZW5kYXJkIixzeXN0ZW0tdWksc2Fucy1zZXJpZjtmb250LXNpemU6MTRweDtsaW5lLWhlaWdodDoxLjU7cGFkZGluZzoxNnB4fQogIC53cmFwe21heC13aWR0aDoxMTIwcHg7bWFyZ2luOjAgYXV0b30KICBoMXtmb250LXNpemU6MjBweDtmb250LXdlaWdodDo4MDA7bGV0dGVyLXNwYWNpbmc6LTAuNHB4fQogIC5oZHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2ZsZXgtd3JhcDp3cmFwO2dhcDo4cHg7cGFkZGluZy1ib3R0b206MTRweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCB2YXIoLS1saW5lKTttYXJnaW4tYm90dG9tOjIwcHh9CiAgLmhkLXRpdGxle2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjlweH0KICAuaGQgLmRvdHt3aWR0aDo5cHg7aGVpZ2h0OjlweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOnZhcigtLWFjY2VudCk7Ym94LXNoYWRvdzowIDAgMCA0cHggdmFyKC0tYWNjZW50LXNvZnQpfQogIC5oZC1tZXRhe2ZvbnQtc2l6ZToxMS41cHg7Y29sb3I6dmFyKC0tbXV0ZWQpO2ZvbnQtd2VpZ2h0OjYwMH0KICAuYmFubmVye2JhY2tncm91bmQ6dmFyKC0tYWNjZW50LXNvZnQpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxMnB4O3BhZGRpbmc6MTJweCAxNHB4O2NvbG9yOnZhcigtLWFjY2VudCk7Zm9udC13ZWlnaHQ6NzAwO2ZvbnQtc2l6ZToxM3B4O21hcmdpbi1ib3R0b206MTJweH0KICAuY29udHJvbHN7ZGlzcGxheTpmbGV4O2ZsZXgtd3JhcDp3cmFwO2dhcDoxMHB4O2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW46MTRweCAwfQogIC5zZWd7ZGlzcGxheTpmbGV4O2dhcDo3cHg7ZmxleC13cmFwOndyYXB9CiAgLm1vZGVzZWd7ZGlzcGxheTppbmxpbmUtZmxleDtnYXA6NHB4O2JhY2tncm91bmQ6dmFyKC0tY2FyZCk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKTtib3JkZXItcmFkaXVzOjEycHg7cGFkZGluZzo0cHh9CiAgLm1vZGVzZWcgYnV0dG9ue2JvcmRlcjpub25lO2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTQuNXB4O2ZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1zdWIpO3BhZGRpbmc6OXB4IDIwcHg7Ym9yZGVyLXJhZGl1czo5cHg7Y3Vyc29yOnBvaW50ZXI7bGV0dGVyLXNwYWNpbmc6LTAuM3B4O3RyYW5zaXRpb246YmFja2dyb3VuZCAuMTJzLGNvbG9yIC4xMnN9CiAgLm1vZGVzZWcgYnV0dG9uLm9ue2JhY2tncm91bmQ6dmFyKC0tYWNjZW50LXNvZnQpO2NvbG9yOnZhcigtLWFjY2VudCl9CiAgLm1vZGVzZWcgYnV0dG9uOmhvdmVyOm5vdCgub24pe2NvbG9yOnZhcigtLWluayl9CiAgLnN1YnNlZ3tkaXNwbGF5OmlubGluZS1mbGV4O2dhcDo0cHg7ZmxleC13cmFwOndyYXB9CiAgLnNlZyBidXR0b257Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKTtiYWNrZ3JvdW5kOnZhcigtLWNhcmQpO2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLXN1Yik7cGFkZGluZzo3cHggMTVweDtib3JkZXItcmFkaXVzOjlweDtjdXJzb3I6cG9pbnRlcn0KICAuc2VnIGJ1dHRvbi5vbntiYWNrZ3JvdW5kOnZhcigtLWFjY2VudCk7Y29sb3I6I2ZmZjtib3JkZXItY29sb3I6dmFyKC0tYWNjZW50KX0KICAucmFuZ2VsYWJlbHtmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS1zdWIpfS5yYW5nZWxhYmVsIGJ7Y29sb3I6dmFyKC0taW5rKX0KICAuZGF0ZXBpY2t7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NnB4O2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS1zdWIpfQogIC5kYXRlcGljayBpbnB1dHtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxMi41cHg7Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOnZhcigtLWluayk7YmFja2dyb3VuZDp2YXIoLS1jYXJkKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6N3B4O3BhZGRpbmc6NnB4IDhweDtjdXJzb3I6cG9pbnRlcn0KICAuZGF0ZXBpY2sgaW5wdXQ6Oi13ZWJraXQtY2FsZW5kYXItcGlja2VyLWluZGljYXRvcntjdXJzb3I6cG9pbnRlcjtvcGFjaXR5Oi42fQogIC50YWJze2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW47Z2FwOjEycHg7bWFyZ2luLWJvdHRvbToxNnB4fQogIC50YWJ7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzo4cHggMTZweDtib3JkZXItcmFkaXVzOjlweDtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjEzcHg7Y29sb3I6dmFyKC0tc3ViKTtjdXJzb3I6cG9pbnRlcjt3aGl0ZS1zcGFjZTpub3dyYXA7Ym9yZGVyOm5vbmU7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtmb250LWZhbWlseTppbmhlcml0O3RyYW5zaXRpb246YmFja2dyb3VuZCAuMTJzLGNvbG9yIC4xMnN9CiAgLnRhYi5vbntiYWNrZ3JvdW5kOnZhcigtLWFjY2VudC1zb2Z0KTtjb2xvcjp2YXIoLS1hY2NlbnQpfQogIC50YWI6aG92ZXI6bm90KC5vbil7Y29sb3I6dmFyKC0taW5rKX0KICAuaXRpcHtwb3NpdGlvbjpyZWxhdGl2ZTtkaXNwbGF5OmlubGluZS1mbGV4O3ZlcnRpY2FsLWFsaWduOm1pZGRsZTttYXJnaW4tbGVmdDo2cHg7Y3Vyc29yOmhlbHB9CiAgLml0aXA+Lmljb3tkaXNwbGF5OmlubGluZS1mbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO3dpZHRoOjE2cHg7aGVpZ2h0OjE2cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDp2YXIoLS1tdXRlZCk7Y29sb3I6I2ZmZjtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo4MDA7Zm9udC1zdHlsZTpub3JtYWw7bGluZS1oZWlnaHQ6MX0KICAuaXRpcD4ucG9we3Bvc2l0aW9uOmFic29sdXRlO2xlZnQ6LTRweDt0b3A6MTUwJTt6LWluZGV4OjYwO2Rpc3BsYXk6bm9uZTtiYWNrZ3JvdW5kOnZhcigtLWNhcmQpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxMnB4O2JveC1zaGFkb3c6MCA4cHggMjZweCByZ2JhKDQyLDM4LDY0LC4xOCk7cGFkZGluZzo2cHggOHB4O21pbi13aWR0aDoyNTBweH0KICAuaXRpcDpob3Zlcj4ucG9wLC5pdGlwOmZvY3VzPi5wb3AsLml0aXA6Zm9jdXMtd2l0aGluPi5wb3B7ZGlzcGxheTpibG9ja30KICAuaXRpcCAucG9wIHRhYmxle2JvcmRlci1jb2xsYXBzZTpjb2xsYXBzZTtmb250LXNpemU6MTNweDt3aWR0aDoxMDAlfQogIC5pdGlwIC5wb3AgdGR7cGFkZGluZzo2cHggOHB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHZhcigtLWxpbmUpO2NvbG9yOnZhcigtLXN1Yik7dGV4dC1hbGlnbjpsZWZ0O3ZlcnRpY2FsLWFsaWduOnRvcH0KICAuaXRpcCAucG9wIHRkOmZpcnN0LWNoaWxke2ZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1pbmspO3doaXRlLXNwYWNlOm5vd3JhcH0KICAuaXRpcCAucG9wIHRyOmxhc3QtY2hpbGQgdGR7Ym9yZGVyLWJvdHRvbTpub25lfQogIC5sZXZ0LXRibHt3aWR0aDoxMDAlO2JvcmRlci1jb2xsYXBzZTpjb2xsYXBzZX0KICAubGV2dC10YmwgdGh7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLXN1Yik7cGFkZGluZzo5cHggMTBweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCB2YXIoLS1saW5lKTt3aGl0ZS1zcGFjZTpub3dyYXB9CiAgLmxldnQtdGJsIHRke3BhZGRpbmc6OHB4IDEwcHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgdmFyKC0tbGluZSk7Zm9udC1zaXplOjEzcHh9CiAgLmxldnQtdGJsIHRib2R5IHRyOmxhc3QtY2hpbGQgdGR7Ym9yZGVyLWJvdHRvbTpub25lfQogIC5sY2F0e2Rpc3BsYXk6aW5saW5lLWJsb2NrO2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS1zdWIpO2JhY2tncm91bmQ6dmFyKC0tYmcpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czo2cHg7cGFkZGluZzoycHggN3B4O3doaXRlLXNwYWNlOm5vd3JhcH0KICAubm90ZWxpbmV7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tc3ViKTttYXJnaW46MnB4IDAgMTBweH0KICAua3Bpc3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdChhdXRvLWZpdCxtaW5tYXgoMTUwcHgsMWZyKSk7Z2FwOjEycHg7bWFyZ2luLWJvdHRvbToxNnB4fQogIC5rcGlzdW17Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCg1LG1pbm1heCgwLDFmcikpfQogIEBtZWRpYShtYXgtd2lkdGg6ODYwcHgpey5rcGlzdW17Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgyLG1pbm1heCgwLDFmcikpfX0KICAua3Bpe2JhY2tncm91bmQ6dmFyKC0tY2FyZCk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKTtib3JkZXItcmFkaXVzOjE0cHg7cGFkZGluZzoxNHB4IDE2cHg7Ym94LXNoYWRvdzp2YXIoLS1zaGFkb3cpfQogIC5rcGkucHJpbWFyeXtib3JkZXItY29sb3I6dmFyKC0tYWNjZW50KTtiYWNrZ3JvdW5kOnZhcigtLWFjY2VudC1zb2Z0KX0KICAua3BpIC50e2ZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLXN1Yik7Zm9udC13ZWlnaHQ6NjAwfQogIC5rcGkgLmJpZ3tmb250LXNpemU6MjRweDtmb250LXdlaWdodDo4MDA7bWFyZ2luLXRvcDo2cHg7bGV0dGVyLXNwYWNpbmc6LTAuNnB4fQogIC5rcGkgLmJpZyAudXtmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS1zdWIpO2ZvbnQtd2VpZ2h0OjYwMDttYXJnaW4tbGVmdDoycHh9CiAgLmtwaSAuZGVsdGF7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6NzAwO21hcmdpbi10b3A6NHB4fQogIC51cHtjb2xvcjp2YXIoLS1nb29kKX0uZG93bntjb2xvcjp2YXIoLS1iYWQpfS5mbGF0e2NvbG9yOnZhcigtLW11dGVkKX0KICAuY2FyZHtiYWNrZ3JvdW5kOnZhcigtLWNhcmQpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxNHB4O3BhZGRpbmc6MTdweCAxOHB4O21hcmdpbi1ib3R0b206MTJweDtib3gtc2hhZG93OnZhcigtLXNoYWRvdyl9CiAgLmNhcmQgaDN7Zm9udC1zaXplOjE2cHg7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi1ib3R0b206MTBweDtsZXR0ZXItc3BhY2luZzotMC4zcHg7Y29sb3I6dmFyKC0taW5rKX0KICAuZGNoaXBze2Rpc3BsYXk6ZmxleDtmbGV4LXdyYXA6d3JhcDtnYXA6NnB4O21hcmdpbi1ib3R0b206MTRweH0KICAuZGNoaXB7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTNweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tc3ViKTtiYWNrZ3JvdW5kOnZhcigtLWNhcmQpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czo5OTlweDtwYWRkaW5nOjdweCAxNXB4O2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246YmFja2dyb3VuZCAuMTJzLGNvbG9yIC4xMnMsYm9yZGVyLWNvbG9yIC4xMnN9CiAgLmRjaGlwLm9ue2JhY2tncm91bmQ6dmFyKC0tYWNjZW50LXNvZnQpO2NvbG9yOnZhcigtLWFjY2VudCk7Ym9yZGVyLWNvbG9yOnZhcigtLWFjY2VudC1zb2Z0KTtmb250LXdlaWdodDo4MDB9CiAgLmRjaGlwOmhvdmVyOm5vdCgub24pe2JvcmRlci1jb2xvcjp2YXIoLS1hY2NlbnQpO2NvbG9yOnZhcigtLWluayl9CiAgLmNhcmQgaDMgLm1pbml7Y29sb3I6dmFyKC0tc3ViKTtmb250LXdlaWdodDo2MDA7Zm9udC1zaXplOjEycHh9CiAgLmNhcmQgLmNzdWJ7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tc3ViKTttYXJnaW4tYm90dG9tOjEycHh9CiAgLnNwbGl0YmFye2Rpc3BsYXk6ZmxleDtoZWlnaHQ6MjZweDtib3JkZXItcmFkaXVzOjdweDtvdmVyZmxvdzpoaWRkZW47bWFyZ2luLWJvdHRvbToxMHB4O2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSl9CiAgLnNwbGl0YmFyIC5hZHtiYWNrZ3JvdW5kOnZhcigtLWFjY2VudCl9LnNwbGl0YmFyIC5vcmd7YmFja2dyb3VuZDp2YXIoLS1vcmcpfQogIC5zcGxpdGJhciBzcGFue2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtjb2xvcjojZmZmO2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjgwMDttaW4td2lkdGg6MH0KICAuc3BsaXRiYXIgLm9yZ3tjb2xvcjp2YXIoLS1pbmspfQogIC5jaHJvd3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjE1MHB4IDFmciA5MnB4O2dhcDoxMHB4O2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjVweCAwO2ZvbnQtc2l6ZToxMi41cHh9CiAgLmNocm93LmV4cHJvd3tjdXJzb3I6cG9pbnRlcn0KICAuY2hyb3cgLm5te2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjVweH0KICAuY2hyb3cgLmFkdGFne2ZvbnQtc2l6ZTo5LjVweDtmb250LXdlaWdodDo4MDA7cGFkZGluZzoxcHggNXB4O2JvcmRlci1yYWRpdXM6NHB4O2JhY2tncm91bmQ6dmFyKC0tYWNjZW50LXNvZnQpO2NvbG9yOnZhcigtLWFjY2VudCl9CiAgLmFycm93e2Rpc3BsYXk6aW5saW5lLWJsb2NrO3dpZHRoOjExcHg7Zm9udC1zaXplOjlweDtjb2xvcjp2YXIoLS1zdWIpO3RyYW5zaXRpb246dHJhbnNmb3JtIC4xNXN9CiAgLnRyYWNre2hlaWdodDo4cHg7YmFja2dyb3VuZDp2YXIoLS1saW5lKTtib3JkZXItcmFkaXVzOjVweDtvdmVyZmxvdzpoaWRkZW59CiAgLnRyYWNrPnNwYW57ZGlzcGxheTpibG9jaztoZWlnaHQ6MTAwJTtib3JkZXItcmFkaXVzOjVweDtiYWNrZ3JvdW5kOnZhcigtLW9yZyl9CiAgLnRyYWNrPnNwYW4uYWRme2JhY2tncm91bmQ6dmFyKC0tYWNjZW50KX0KICAuY2hyb3cgLnZhbHt0ZXh0LWFsaWduOnJpZ2h0O2NvbG9yOnZhcigtLXN1Yil9LmNocm93IC52YWwgYntjb2xvcjp2YXIoLS1pbmspfQogIC5jaHN1YntwYWRkaW5nOjJweCAwIDhweCAyMHB4O21hcmdpbjowIDAgNHB4IDhweDtib3JkZXItbGVmdDoycHggc29saWQgdmFyKC0tYWNjZW50LXNvZnQpfQogIC5zdWJyb3d7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgYXV0bztnYXA6MTBweDtmb250LXNpemU6MTEuNXB4O2NvbG9yOnZhcigtLXN1Yik7cGFkZGluZzozcHggMH0KICAuc3Vicm93IC5ubTJ7Y29sb3I6dmFyKC0taW5rKX0KICAuZWZmc3Vie2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtnYXA6MTBweDtmb250LXNpemU6MTEuNXB4O2NvbG9yOnZhcigtLXN1Yik7cGFkZGluZzo0cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCB2YXIoLS1hY2NlbnQtc29mdCl9CiAgLmVmZnN1YiAubm0ye2NvbG9yOnZhcigtLWluayk7Zm9udC13ZWlnaHQ6NjAwfQogIC5jb252Z3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdChhdXRvLWZpdCxtaW5tYXgoMTMwcHgsMWZyKSk7Z2FwOjEwcHh9CiAgLmNvbnZ7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKTtib3JkZXItcmFkaXVzOjlweDtwYWRkaW5nOjExcHggMTJweH0KICAuY29udiAudHtmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS1zdWIpO2ZvbnQtd2VpZ2h0OjYwMH0KICAuY29udiAuYmlne2ZvbnQtc2l6ZToyMHB4O2ZvbnQtd2VpZ2h0OjgwMDttYXJnaW4tdG9wOjRweH0KICAuY29udiAuYmlnIC51e2ZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXN1Yik7bWFyZ2luLWxlZnQ6MXB4fQogIC5jb252IC5zdWIye2ZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXN1Yik7bWFyZ2luLXRvcDoxcHh9CiAgLnNjcm9sbHtvdmVyZmxvdy14OmF1dG99CiAgdGFibGV7d2lkdGg6MTAwJTtib3JkZXItY29sbGFwc2U6Y29sbGFwc2U7Zm9udC1zaXplOjEzcHg7bWluLXdpZHRoOjY4MHB4fQogIHRoLHRke3BhZGRpbmc6OXB4IDEwcHg7dGV4dC1hbGlnbjpyaWdodDtib3JkZXItYm90dG9tOjFweCBzb2xpZCB2YXIoLS1saW5lKTt3aGl0ZS1zcGFjZTpub3dyYXB9CiAgdGg6Zmlyc3QtY2hpbGQsdGQ6Zmlyc3QtY2hpbGR7dGV4dC1hbGlnbjpsZWZ0fQogIHRoZWFkIHRoe2NvbG9yOnZhcigtLXN1Yik7Zm9udC13ZWlnaHQ6NzAwfQogIHRoLmxoLHRkLmxoe3RleHQtYWxpZ246bGVmdH0KICB0ZC5uYW1le21heC13aWR0aDoyNjBweDt3aGl0ZS1zcGFjZTpub3JtYWw7d29yZC1icmVhazpicmVhay13b3JkO2xpbmUtaGVpZ2h0OjEuMzU7dGV4dC1hbGlnbjpsZWZ0fQogIHRyLmV4cHJvd3tjdXJzb3I6cG9pbnRlcn0KICAua3BpY29se2ZvbnQtd2VpZ2h0OjgwMDtiYWNrZ3JvdW5kOnJnYmEoMTMxLDExMiwyMzAsMC4wNil9dGgua3BpY29se2NvbG9yOnZhcigtLXN1Yik7YmFja2dyb3VuZDpyZ2JhKDEzMSwxMTIsMjMwLDAuMDYpfQogIHRkLmRpbXtjb2xvcjp2YXIoLS1tdXRlZCl9CiAgLmRlbHRhLXN7Zm9udC1zaXplOjEwLjVweDtmb250LXdlaWdodDo3MDA7bWFyZ2luLWxlZnQ6NXB4fQogIC5iYWRnZXtkaXNwbGF5OmlubGluZS1ibG9jaztmb250LXNpemU6MTAuNXB4O2ZvbnQtd2VpZ2h0OjcwMDtwYWRkaW5nOjFweCA3cHg7Ym9yZGVyLXJhZGl1czo1cHh9CiAgLmJhZGdlLnJle2JhY2tncm91bmQ6I2VmZWFmYztjb2xvcjojN2E2M2Q2fS5iYWRnZS50cntiYWNrZ3JvdW5kOiNlZWYwZjM7Y29sb3I6IzdhODU5M30KICBAbWVkaWEgKHByZWZlcnMtY29sb3Itc2NoZW1lOmRhcmspezpyb290Om5vdChbZGF0YS10aGVtZT0ibGlnaHQiXSkgLmJhZGdlLnJle2JhY2tncm91bmQ6IzI3MWQzZDtjb2xvcjojYTk5MWY1fTpyb290Om5vdChbZGF0YS10aGVtZT0ibGlnaHQiXSkgLmJhZGdlLnRye2JhY2tncm91bmQ6IzI0MmIzNH19CiAgLmRldGFpbHRke2JhY2tncm91bmQ6dmFyKC0tYmcpO3BhZGRpbmc6MH0uZGV0YWlsdGQgLmlubmVye3BhZGRpbmc6NHB4IDEwcHggMTBweCAyNnB4fQogIC50b3Ryb3cgdGR7Ym9yZGVyLXRvcDoycHggc29saWQgdmFyKC0tbGluZSk7Zm9udC13ZWlnaHQ6ODAwfQogIC5zZWNoZC1yb3cgdGR7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQtc29mdCk7Y29sb3I6dmFyKC0tYWNjZW50KTtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjEyLjVweDt0ZXh0LWFsaWduOmxlZnQhaW1wb3J0YW50O3BhZGRpbmc6OXB4IDhweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCB2YXIoLS1saW5lKX0KICAuc2VnaGR7Zm9udC1zaXplOjEzLjVweDtmb250LXdlaWdodDo4MDA7bWFyZ2luOjE2cHggMCA4cHh9LnNlZ2hkIC5zZWdzdWJ7Zm9udC1zaXplOjExLjVweDtmb250LXdlaWdodDo2MDA7Y29sb3I6dmFyKC0tc3ViKTttYXJnaW4tbGVmdDo3cHh9CiAgLmZvb3R7Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tbXV0ZWQpO21hcmdpbi10b3A6MTZweDtsaW5lLWhlaWdodDoxLjZ9Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+CjxkaXYgY2xhc3M9IndyYXAiPgogIDxoZWFkZXIgY2xhc3M9ImhkIj48ZGl2IGNsYXNzPSJoZC10aXRsZSI+PHNwYW4gY2xhc3M9ImRvdCI+PC9zcGFuPjxoMT7ti7DrqLjri4hHTyDqtJHqs6Ag7ISx6rO8IO2Gte2Vqeu3sDwvaDE+PC9kaXY+PGRpdiBjbGFzcz0iaGQtbWV0YSIgaWQ9Imdlbm1ldGEiPjwvZGl2PjwvaGVhZGVyPgogIDxkaXYgY2xhc3M9ImNvbnRyb2xzIj4KICAgIDxkaXYgY2xhc3M9ImRhdGVwaWNrIj7quLDqsIQgPGlucHV0IHR5cGU9ImRhdGUiIGlkPSJmcm9tRGF0ZSI+IH4gPGlucHV0IHR5cGU9ImRhdGUiIGlkPSJ0b0RhdGUiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ic2VnIiBpZD0icXVpY2twaWNrIj48YnV0dG9uIGRhdGEtZD0iMSI+MeydvDwvYnV0dG9uPjxidXR0b24gZGF0YS1kPSI3Ij437J28PC9idXR0b24+PGJ1dHRvbiBkYXRhLWQ9IjE0Ij4xNOydvDwvYnV0dG9uPjxidXR0b24gZGF0YS1kPSIzMCI+MzDsnbw8L2J1dHRvbj48YnV0dG9uIGRhdGEtZD0iNjAiPjYw7J28PC9idXR0b24+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyYW5nZWxhYmVsIiBpZD0icmFuZ2VsYWJlbCI+PC9kaXY+CiAgPC9kaXY+CiAgPGRpdiBjbGFzcz0idGFicyIgaWQ9InRhYnMiPjwvZGl2PgogIDxkaXYgaWQ9InZpZXciPjwvZGl2PgogIDxkaXYgY2xhc3M9ImZvb3QiIGlkPSJmb290Ij48L2Rpdj4KPC9kaXY+CjxzY3JpcHQ+Ci8qID09PT09IOuNsOydtO2EsCDso7zsnoUgKGdlbmVyYXRlLnB56rCAIF9fREFUQV9fIOulvCDsi6TsuKEgSlNPTuycvOuhnCDqtZDssrQpID09PT09ICovCmNvbnN0IERBVEEgPSBfX0RBVEFfXzsKY29uc3QgQlVDS0VUUyA9IERBVEEuYnVja2V0czsKY29uc3QgREFZUyA9IERBVEEuZGF5czsKY29uc3QgQ0ggPSBEQVRBLmNoYW5uZWxzOwpjb25zdCBUT1RBTFMgPSBEQVRBLnRvdGFsczsKY29uc3QgVEFYSVRPVCA9IERBVEEudGF4aXRvdDsKY29uc3QgVEFYSVRPVFUgPSBEQVRBLnRheGl0b3R1fHxbXTsKY29uc3QgVU5JUSA9IERBVEEudW5pcXx8e307CmNvbnN0IExFVlQgPSBEQVRBLmxldnR8fHt9Owpjb25zdCBDQU1QID0gREFUQS5jYW1wOwpjb25zdCBPTkIgPSBEQVRBLm9uYjsKY29uc3QgQURFRkYgPSBEQVRBLmFkZWZmOwpjb25zdCBST1dTID0gREFUQS5yb3dzOwpjb25zdCBCVE9UID0gREFUQS5idG90fHx7fTsKY29uc3QgQ1JTSE9XID0ge307CmNvbnN0IEZVTk5FTCA9IERBVEEuZnVubmVsfHxudWxsOwpjb25zdCBEUklMTCA9IERBVEEuZHJpbGx8fG51bGw7CmNvbnN0IFJFVE4gPSBEQVRBLnJldG58fG51bGw7CmxldCBNT0RFPSJhZHMiOyBsZXQgRFJJTExNRVRSSUM9InN1IjsgY29uc3QgREVYUD1uZXcgU2V0KCk7IGxldCBCVFJLPSJib3RoIjsgbGV0IExHSz0iYWxsIjsgbGV0IExFV0lOPSIzMCI7IGxldCBMRVNPUlQ9ImV2IjsKCi8qID09PT09IOqzoOyglSDroZzsp4EgPT09PT0gKi8KY29uc3QgZj1uPT4obj09bnVsbCk/IuKAlCI6TWF0aC5yb3VuZChuKS50b0xvY2FsZVN0cmluZygna28tS1InKTsKbGV0IFJBPU1hdGgubWF4KDAsREFZUy5sZW5ndGgtNyksIFJCPURBWVMubGVuZ3RoLTE7ICAgLy8g7ISg7YOdIOq4sOqwhCjsnbwg7J24642x7IqkKQpmdW5jdGlvbiB3aW5kb3dzKCl7Y29uc3QgY3VyPVtdO2ZvcihsZXQgaT1SQTtpPD1SQjtpKyspY3VyLnB1c2goaSk7Y29uc3Qgbj1jdXIubGVuZ3RoO2NvbnN0IHNoPU1hdGguY2VpbChuLzcpKjc7Y29uc3QgcHJpb3I9Y3VyLm1hcChpPT5pLXNoKTtyZXR1cm4ge2N1cixwcmlvcjpwcmlvci5ldmVyeShpPT5pPj0wKT9wcmlvcjpbXX07fQpjb25zdCBzdW09KGFycixpZHgpPT5pZHgucmVkdWNlKChhLGkpPT5hKyhhcnJbaV18fDApLDApOwpjb25zdCBwY3Q9KGMscCk9PihwPT1udWxsfHxwPT09MCk/bnVsbDooYy1wKS9wKjEwMDsKZnVuY3Rpb24gZGVsdGFTcGFuKGMscCxnb29kVXA9dHJ1ZSxzbWFsbD1mYWxzZSl7Y29uc3Qgdj1wY3QoYyxwKTtpZih2PT1udWxsKXJldHVybiAnJztjb25zdCBjbHM9KHY9PT0wKT8nZmxhdCc6KCh2PjA9PT1nb29kVXApPyd1cCc6J2Rvd24nKSxhcj12PjA/J+KWsic6KHY8MD8n4pa8JzonJyk7cmV0dXJuIGA8c3BhbiBjbGFzcz0iJHtjbHN9ICR7c21hbGw/J2RlbHRhLXMnOidkZWx0YSd9Ij4ke2FyfSR7TWF0aC5hYnModikudG9GaXhlZCgwKX0lPC9zcGFuPmA7fQpmdW5jdGlvbiBrcGkobGFiZWwsY3VyLHByaW9yLG89e30pe3JldHVybiBgPGRpdiBjbGFzcz0ia3BpICR7KG8uYWN0aXZlfHxvLnByaW1hcnkpPydwcmltYXJ5JzonJ30iICR7by5rbT8oJ2RhdGEta209Iicrby5rbSsnIiBzdHlsZT0iY3Vyc29yOnBvaW50ZXIiJyk6Jyd9PjxkaXYgY2xhc3M9InQiPiR7bGFiZWx9PC9kaXY+PGRpdiBjbGFzcz0iYmlnIj4ke28ud29uPyfigqknOicnfSR7ZihjdXIpfSR7by51bml0P2A8c3BhbiBjbGFzcz0idSI+JHtvLnVuaXR9PC9zcGFuPmA6Jyd9PC9kaXY+JHtvLm5vZGVsdGE/Jyc6ZGVsdGFTcGFuKGN1cixwcmlvcixvLmdvb2RVcCE9PWZhbHNlKX0ke28uc3VifHwnJ308L2Rpdj5gO30KZnVuY3Rpb24gY2hUb3RhbChrZXksaWR4KXtyZXR1cm4gQ0gucmVkdWNlKChhLGMpPT5hK3N1bShjW2tleV0saWR4KSwwKTt9Ci8qIC0tLS0g7J287J6Q67OEIOy2lOydtCDrnbzsnbjssKjtirggKOuCtOyepSBTVkcsIDLsnbwg7J207IOB7J28IOuVjCkgLS0tLSAqLwpsZXQgVFJFTkRLRVk9InN1IjsKZnVuY3Rpb24gd2QoZCxzaG93Vyl7Y29uc3Qgcz1kLnNsaWNlKDUpOyBpZighc2hvd1cpIHJldHVybiBzOyBjb25zdCBkdD1uZXcgRGF0ZShkKydUMDA6MDA6MDAnKTsgcmV0dXJuIHMrJygnKyLsnbzsm5TtmZTsiJjrqqnquIjthqAiW2R0LmdldERheSgpXSsnKSc7fQpmdW5jdGlvbiB0cmVuZENoYXJ0KGRheXMsIHNlcmllcyl7CiAgY29uc3Qgbj1kYXlzLmxlbmd0aCwgUz1zZXJpZXMubGVuZ3RoLCBQTD00MixQUj0xMixQVD0yNCxQQj0zMiwgSD0xODQ7CiAgY29uc3QgZmM9dj0+e3Y9TWF0aC5yb3VuZCh2KTsgaWYodj49MTAwMDApcmV0dXJuICh2LzEwMDAwKS50b0ZpeGVkKHY+PTEwMDAwMD8wOjEpLnJlcGxhY2UoL1wuMCQvLCcnKSsn66eMJzsgaWYodj49MTAwMClyZXR1cm4gKHYvMTAwMCkudG9GaXhlZCgxKS5yZXBsYWNlKC9cLjAkLywnJykrJ+yynCc7IHJldHVybiB2LnRvTG9jYWxlU3RyaW5nKCdrby1LUicpO307CiAgY29uc3QgVz1NYXRoLm1heCg4NjAsIFBMK1BSK24qKFM+MT9TKjE1KzE4OjM0KSk7CiAgY29uc3QgbXg9TWF0aC5tYXgoMSwuLi5zZXJpZXMuZmxhdE1hcChzPT5zLnZhbHMpKSoxLjI4OwogIGNvbnN0IHBsb3RXPVctUEwtUFIsIHBsb3RIPUgtUFQtUEIsIGd3PXBsb3RXL247CiAgY29uc3QgYnc9TWF0aC5tYXgoMyxNYXRoLm1pbihTPjE/MjQ6Z3cqMC41MiwoZ3ctNikvUykpOwogIGNvbnN0IFk9dj0+UFQrcGxvdEgqKDEtdi9teCk7CiAgY29uc3QgbGJsU3RlcD1NYXRoLm1heCgxLE1hdGguY2VpbChuLzcpKTsKICBsZXQgZ3JpZD0nJzsgY29uc3QgRz00OwogIGZvcihsZXQgaT0wO2k8PUc7aSsrKXtjb25zdCBneT1QVCtwbG90SCppL0csIGd2PW14KigxLWkvRyk7CiAgICBncmlkKz1gPGxpbmUgeDE9IiR7UEx9IiB5MT0iJHtneS50b0ZpeGVkKDEpfSIgeDI9IiR7Vy1QUn0iIHkyPSIke2d5LnRvRml4ZWQoMSl9IiBzdHJva2U9InZhcigtLWxpbmUpIiBzdHJva2Utd2lkdGg9IjEiLz5gCiAgICAgICsoaTxHP2A8dGV4dCB4PSIke1BMLTZ9IiB5PSIkeyhneSszKS50b0ZpeGVkKDEpfSIgZm9udC1zaXplPSI4IiBmaWxsPSJ2YXIoLS1tdXRlZCkiIHRleHQtYW5jaG9yPSJlbmQiPiR7ZmMoZ3YpfTwvdGV4dD5gOicnKTt9CiAgbGV0IGJhcnM9Jyc7CiAgZm9yKGxldCBpPTA7aTxuO2krKyl7CiAgICBjb25zdCBneD1QTCtndyppKyhndy1idypTKS8yOwogICAgc2VyaWVzLmZvckVhY2goKHMsc2kpPT57CiAgICAgIGNvbnN0IHY9cy52YWxzW2ldfHwwLCB4PWd4K3NpKmJ3LCB5PVkodiksIGhoPVBUK3Bsb3RILXksIGN4PXgrYncqMC40NTsKICAgICAgYmFycys9YDxyZWN0IHg9IiR7eC50b0ZpeGVkKDEpfSIgeT0iJHt5LnRvRml4ZWQoMSl9IiB3aWR0aD0iJHsoYncqMC44NikudG9GaXhlZCgxKX0iIGhlaWdodD0iJHtNYXRoLm1heCgwLGhoKS50b0ZpeGVkKDEpfSIgcng9IjIuNSIgZmlsbD0iJHtzLmNvbG9yfSI+PHRpdGxlPiR7ZGF5c1tpXX0gJHtzLm5hbWV9OiAke2Yodil9PC90aXRsZT48L3JlY3Q+YDsKICAgICAgaWYoaSVsYmxTdGVwPT09MCYmdj4wKSBiYXJzKz1gPHRleHQgeD0iJHtjeC50b0ZpeGVkKDEpfSIgeT0iJHsoeS0zKS50b0ZpeGVkKDEpfSIgZm9udC1zaXplPSI4IiBmb250LXdlaWdodD0iNzAwIiBmaWxsPSJ2YXIoLS1zdWIpIiB0ZXh0LWFuY2hvcj0ibWlkZGxlIj4ke2ZjKHYpfTwvdGV4dD5gOwogICAgfSk7CiAgfQogIGNvbnN0IHNob3dXPW48PTE0LCB4c3RlcD1NYXRoLm1heCgxLE1hdGguY2VpbChuLzgpKTsKICBsZXQgeGw9Jyc7CiAgZm9yKGxldCBpPTA7aTxuO2krPXhzdGVwKXtjb25zdCBjeD1QTCtndyppK2d3LzI7IHhsKz1gPHRleHQgeD0iJHtjeC50b0ZpeGVkKDEpfSIgeT0iJHtILTEyfSIgZm9udC1zaXplPSI5IiBmaWxsPSJ2YXIoLS1tdXRlZCkiIHRleHQtYW5jaG9yPSJtaWRkbGUiPiR7d2QoZGF5c1tpXSxzaG93Vyl9PC90ZXh0PmA7fQogIGNvbnN0IGxlZ2VuZD1TPjE/YDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDtnYXA6MTRweDtmb250LXNpemU6MTFweDttYXJnaW4tdG9wOjhweDtmbGV4LXdyYXA6d3JhcCI+YCtzZXJpZXMubWFwKHM9PmA8c3BhbiBzdHlsZT0iY29sb3I6dmFyKC0tc3ViKSI+PHNwYW4gc3R5bGU9ImRpc3BsYXk6aW5saW5lLWJsb2NrO3dpZHRoOjEwcHg7aGVpZ2h0OjEwcHg7Ym9yZGVyLXJhZGl1czozcHg7YmFja2dyb3VuZDoke3MuY29sb3J9O21hcmdpbi1yaWdodDo1cHg7dmVydGljYWwtYWxpZ246bWlkZGxlIj48L3NwYW4+JHtzLm5hbWV9PC9zcGFuPmApLmpvaW4oJycpK2A8L2Rpdj5gOicnOwogIHJldHVybiBgPGRpdiBjbGFzcz0ic2Nyb2xsIj48c3ZnIHZpZXdCb3g9IjAgMCAke1d9ICR7SH0iIHdpZHRoPSIxMDAlIiBzdHlsZT0ibWluLXdpZHRoOiR7TWF0aC5taW4oVyw1MjApLnRvRml4ZWQoMCl9cHg7ZGlzcGxheTpibG9jayI+JHtncmlkfSR7YmFyc30ke3hsfTwvc3ZnPjwvZGl2PiR7bGVnZW5kfWA7Cn0KZnVuY3Rpb24gc3VtbWFyeVRyZW5kKFcpewogIGlmKFcuY3VyLmxlbmd0aDwyKSByZXR1cm4gJyc7CiAgY29uc3QgTT17aW5zdGFsbDpbIuyVseyEpOy5mCIsVE9UQUxTLmluc3RhbGxdLHZpc2l0Olsi7Zmc7ISxIOycoOyggCIsVE9UQUxTLnZpc2l0XSxzdTpbIu2ajOybkOqwgOyehSIsVE9UQUxTLnN1XSxjYXJkOlsi6rWQ7Ya17Lm065OcIOuTseuhnSIsVE9UQUxTLmNhcmRdLGxvdW5nZTpbIuuMgOykkeq1kO2GteudvOyatOyngCDrsKnrrLgiLFRPVEFMUy5sb3VuZ2VdLGNhbGw6WyLtg53si5ztmLjstpwiLFRBWElUT1RdfTsKICBpZighTVtUUkVOREtFWV0pIFRSRU5ES0VZPSJzdSI7IGNvbnN0IG09TVtUUkVOREtFWV07CiAgY29uc3QgZGF5cz1XLmN1ci5tYXAoaT0+REFZU1tpXSksIHZhbHM9Vy5jdXIubWFwKGk9Pm1bMV1baV18fDApOwogIGNvbnN0IGJ0bnM9YDxkaXYgY2xhc3M9InNlZyIgc3R5bGU9Im1hcmdpbjoxMnB4IDAgMTVweDtmbGV4LXdyYXA6d3JhcCI+YCtPYmplY3Qua2V5cyhNKS5tYXAoaz0+YDxidXR0b24gY2xhc3M9IiR7az09PVRSRU5ES0VZPydvbic6Jyd9IiBkYXRhLXRyZW5kPSIke2t9Ij4ke01ba11bMF19PC9idXR0b24+YCkuam9pbignJykrYDwvZGl2PmA7CiAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJjYXJkIj48aDM+7J287J6Q67OEIOy2lOydtCA8c3BhbiBjbGFzcz0ibWluaSI+JHttWzBdfSAo7KCE7LK0KTwvc3Bhbj48L2gzPiR7YnRuc30ke3RyZW5kQ2hhcnQoZGF5cyxbe25hbWU6bVswXSxjb2xvcjonIzgzNzBlNicsdmFsc31dKX08L2Rpdj5gOwp9CmZ1bmN0aW9uIGJ1Y2tldFRyZW5kKEIscm93cyxXKXsKICBpZihXLmN1ci5sZW5ndGg8MikgcmV0dXJuICcnOwogIGNvbnN0IGRheXM9Vy5jdXIubWFwKGk9PkRBWVNbaV0pLCBidD1CVE9UW0IuaWRdfHx7Y2FsbDpbXX07CiAgY29uc3QgdG90Vj1XLmN1ci5tYXAoaT0+KGJ0LmNhbGx8fFtdKVtpXXx8MCk7CiAgY29uc3QgYWRWPVcuY3VyLm1hcChpPT5yb3dzLnJlZHVjZSgoYSxyKT0+YSsoci5jaCE9PSJvcmciPyhyLnMuY2FsbFtpXXx8MCk6MCksMCkpOwogIGNvbnN0IGFsbD1be25hbWU6IuyghOyytCIsY29sb3I6JyNiOWIzY2MnLHZhbHM6dG90VixrZXk6J3RvdCd9LHtuYW1lOiLqtJHqs6Ao7KCE7LK0KSIsY29sb3I6JyM4MzcwZTYnLHZhbHM6YWRWLGtleTonYWQnfV07CiAgY29uc3Qgc2VyaWVzPUJUUks9PT0nYWQnP2FsbC5maWx0ZXIocz0+cy5rZXk9PT0nYWQnKTphbGw7CiAgY29uc3Qgc2VnPWA8ZGl2IGNsYXNzPSJzZWciIHN0eWxlPSJhbGlnbi1zZWxmOmZsZXgtc3RhcnQ7bWFyZ2luOjEwcHggMCAxM3B4Ij48YnV0dG9uIGNsYXNzPSIke0JUUkshPT0nYWQnPydvbic6Jyd9IiBkYXRhLWJ0cmVuZD0iYm90aCI+7KCE7LK0ICjqtJHqs6Ar67mE6rSR6rOgKTwvYnV0dG9uPjxidXR0b24gY2xhc3M9IiR7QlRSSz09PSdhZCc/J29uJzonJ30iIGRhdGEtYnRyZW5kPSJhZCI+6rSR6rOgPC9idXR0b24+PC9kaXY+YDsKICByZXR1cm4gYDxkaXYgY2xhc3M9ImNhcmQiPjxoMz7snbzsnpDrs4Qg7LaU7J20IDxzcGFuIGNsYXNzPSJtaW5pIj4ke0IucHJpbWFyeUxhYmVsfTwvc3Bhbj48L2gzPiR7c2VnfSR7dHJlbmRDaGFydChkYXlzLHNlcmllcyl9PC9kaXY+YDsKfQoKLyogLS0tLSDsmpTslb0gLS0tLSAqLwpmdW5jdGlvbiBjaEJsb2NrKHRpdGxlLGtleSxXKXsKICBjb25zdCByb3dzPUNILm1hcChjPT4oe25hbWU6Yy5uYW1lLGFkOmMuYWQsY3VyOnN1bShjW2tleV0sVy5jdXIpfSkpLnNvcnQoKGEsYik9PmIuY3VyLWEuY3VyKTsKICBjb25zdCB0b3RhbD1yb3dzLnJlZHVjZSgoYSxyKT0+YStyLmN1ciwwKXx8MTsKICBjb25zdCBhZFBjdD1yb3dzLmZpbHRlcihyPT5yLmFkKS5yZWR1Y2UoKGEscik9PmErci5jdXIsMCkvdG90YWwqMTAwLCBvcmdQY3Q9MTAwLWFkUGN0OwogIGNvbnN0IGJhcj1gPGRpdiBjbGFzcz0ic3BsaXRiYXIiPjxzcGFuIGNsYXNzPSJhZCIgc3R5bGU9IndpZHRoOiR7YWRQY3R9JSI+JHthZFBjdD49MTI/J+q0keqzoCAnK2FkUGN0LnRvRml4ZWQoMCkrJyUnOicnfTwvc3Bhbj48c3BhbiBjbGFzcz0ib3JnIiBzdHlsZT0id2lkdGg6JHtvcmdQY3R9JSI+JHtvcmdQY3Q+PTEyPyfruYTqtJHqs6AgJytvcmdQY3QudG9GaXhlZCgwKSsnJSc6Jyd9PC9zcGFuPjwvZGl2PmA7CiAgY29uc3QgbGlzdD1yb3dzLm1hcChyPT57Y29uc3Qgdz1yLmN1ci90b3RhbCoxMDA7CiAgICBpZihyLmFkKXsKICAgICAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJjaHJvdyI+PGRpdiBjbGFzcz0ibm0iPiR7ci5uYW1lfTxzcGFuIGNsYXNzPSJhZHRhZyI+6rSR6rOgPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9InRyYWNrIj48c3BhbiBjbGFzcz0iYWRmIiBzdHlsZT0id2lkdGg6JHt3fSUiPjwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJ2YWwiPjxiPiR7ZihyLmN1cil9PC9iPiAvICR7dy50b0ZpeGVkKDApfSU8L2Rpdj48L2Rpdj5gO30KICAgIHJldHVybiBgPGRpdiBjbGFzcz0iY2hyb3ciPjxkaXYgY2xhc3M9Im5tIj4ke3IubmFtZX08L2Rpdj48ZGl2IGNsYXNzPSJ0cmFjayI+PHNwYW4gc3R5bGU9IndpZHRoOiR7d30lIj48L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0idmFsIj48Yj4ke2Yoci5jdXIpfTwvYj4gLyAke3cudG9GaXhlZCgwKX0lPC9kaXY+PC9kaXY+YDsKICB9KS5qb2luKCcnKTsKICByZXR1cm4gYDxkaXYgY2xhc3M9ImNhcmQiPjxoMz4ke3RpdGxlfSA8c3BhbiBjbGFzcz0ibWluaSI+7LSdICR7Zih0b3RhbCl9ICjssYTrhJAg7ZWp7IKwIC8g7KSR67O1IO2PrO2VqCk8L3NwYW4+PC9oMz4ke2Jhcn0ke2xpc3R9PC9kaXY+YDsKfQpmdW5jdGlvbiBvbmJCbG9jayhXKXsKICBjb25zdCBzdT1zdW0oVE9UQUxTLnN1LFcuY3VyKSwgc3VQPVcucHJpb3IubGVuZ3RoP3N1bShUT1RBTFMuc3UsVy5wcmlvcik6bnVsbDsKICBjb25zdCBhbnlEYXRhPU9OQi5yZWR1Y2UoKGEsbyk9PmErc3VtKG9bMV0sVy5jdXIpLDApPjA7CiAgaWYoIWFueURhdGEpIHJldHVybiBgPGRpdiBjbGFzcz0iY2FyZCI+PGgzPuyYqOuztOuUqSDsoITtmZjsnKggPHNwYW4gY2xhc3M9Im1pbmkiPuyghOyytCDqsIDsnoXsnpAgLyDqsIDsnoUg64u57J28PC9zcGFuPjwvaDM+PGRpdiBjbGFzcz0iY3N1YiIgc3R5bGU9Im1hcmdpbjowIj7rpqzthZDshZgg642w7J207YSwIOyXsOuPmSDspIDruYQg7KSRIOKAlCDqsIDsnoUg64u57J28IO2VteyLrO2WieuPmSDsoITtmZjsnKjsnbQg6rOnIO2RnOyLnOuQqeuLiOuLpC48L2Rpdj48L2Rpdj5gOwogIGNvbnN0IGl0ZW1zPU9OQi5tYXAobz0+e2NvbnN0IGM9c3VtKG9bMV0sVy5jdXIpLHA9Vy5wcmlvci5sZW5ndGg/c3VtKG9bMV0sVy5wcmlvcik6bnVsbDtjb25zdCByYXRlPXN1P2Mvc3UqMTAwOjAscmF0ZVA9KHN1UCYmcCE9bnVsbCk/cC9zdVAqMTAwOm51bGw7cmV0dXJuIGA8ZGl2IGNsYXNzPSJjb252Ij48ZGl2IGNsYXNzPSJ0Ij4ke29bMF19PC9kaXY+PGRpdiBjbGFzcz0iYmlnIj4ke3JhdGUudG9GaXhlZCgxKX08c3BhbiBjbGFzcz0idSI+JTwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJzdWIyIj4ke2YoYyl966qFIC8g6rCA7J6FICR7ZihzdSl966qFICR7ZGVsdGFTcGFuKHJhdGUscmF0ZVAsdHJ1ZSx0cnVlKX08L2Rpdj48L2Rpdj5gO30pLmpvaW4oJycpOwogIHJldHVybiBgPGRpdiBjbGFzcz0iY2FyZCI+PGgzPuyYqOuztOuUqSDsoITtmZjsnKggPHNwYW4gY2xhc3M9Im1pbmkiPuyghOyytCDqsIDsnoXsnpAgLyDqsIDsnoUg64u57J28PC9zcGFuPjwvaDM+PGRpdiBjbGFzcz0iY3N1YiI+7KCEIOyxhOuEkCDqsIDsnoXsnpAo6rSR6rOgK+u5hOq0keqzoCnqsIAgPGI+6rCA7J6FIOuLueydvDwvYj4g7ZW064u5IO2WieuPmeydhCDtlZwg67mE7JyoIC8g67aE66qoID0g64u57J28IOqwgOyeheyekCDsiJg8L2Rpdj48ZGl2IGNsYXNzPSJjb252Z3JpZCI+JHtpdGVtc308L2Rpdj48L2Rpdj5gOwp9CmZ1bmN0aW9uIGVmZkJsb2NrKFcpewogIGNvbnN0IGJvZHk9QURFRkYubWFwKGE9PnsKICAgIGNvbnN0IGNvc3Q9c3VtKGEuY29zdCxXLmN1ciksaW5zdD1zdW0oYS5pbnN0YWxsfHxbXSxXLmN1ciksc3U9c3VtKGEuc3UsVy5jdXIpLGNhbGw9c3VtKGEuY2FsbCxXLmN1cik7CiAgICBjb25zdCBjcHM9c3U/Y29zdC9zdTpudWxsLGNwYz1jYWxsP2Nvc3QvY2FsbDpudWxsLGkycz1pbnN0P3N1L2luc3QqMTAwOm51bGw7CiAgICByZXR1cm4gYDx0cj48dGQgY2xhc3M9Im5hbWUiPiR7YS5uYW1lfTwvdGQ+PHRkIGNsYXNzPSJkaW0iPiR7ZihpbnN0KX08L3RkPjx0ZCBjbGFzcz0iZGltIj4ke2Yoc3UpfTwvdGQ+PHRkPiR7aTJzIT1udWxsP2kycy50b0ZpeGVkKDEpKyclJzon4oCUJ308L3RkPjx0ZD7igqkke2YoY3BzKX08L3RkPjx0ZCBjbGFzcz0ia3BpY29sIj4ke2YoY2FsbCl9PC90ZD48dGQ+4oKpJHtmKGNwYyl9PC90ZD48dGQgY2xhc3M9ImRpbSI+4oKpJHtmKGNvc3QpfTwvdGQ+PC90cj5gOwogIH0pLmpvaW4oJycpOwogIHJldHVybiBgPGRpdiBjbGFzcz0iY2FyZCI+PGgzPuyxhOuEkOuzhCDtmqjsnKg8L2gzPjxkaXYgY2xhc3M9ImNzdWIiPuyEpOy5mCDihpIg6rCA7J6FIOyghO2ZmOqzvCDqsIDsnoUgMeuqhSDrsI8g7YOd7Iuc7Zi47LacIDHqsbTri7kg67mE7JqpPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzY3JvbGwiPjx0YWJsZSBzdHlsZT0ibWluLXdpZHRoOjcyMHB4Ij48dGhlYWQ+PHRyPjx0aD7ssYTrhJA8L3RoPjx0aD7shKTsuZg8L3RoPjx0aD7qsIDsnoU8L3RoPjx0aD7qsIDsnoUv7ISk7LmYPC90aD48dGg+6rCA7J6F64u5IOu5hOyaqTwvdGg+PHRoIGNsYXNzPSJrcGljb2wiPu2DneyLnO2YuOy2nDwvdGg+PHRoPu2YuOy2nOuLuSDruYTsmqk8L3RoPjx0aD7qtJHqs6DruYQ8L3RoPjwvdHI+PC90aGVhZD48dGJvZHk+JHtib2R5fTwvdGJvZHk+PC90YWJsZT48L2Rpdj48L2Rpdj5gOwp9CmZ1bmN0aW9uIHJldG5CbG9jaygpewogIGlmKCFSRVROfHwhUkVUTi5wb2ludHN8fCFSRVROLnBvaW50cy5sZW5ndGgpIHJldHVybiAnJzsKICBjb25zdCBteD1NYXRoLm1heCguLi5SRVROLnBvaW50cy5tYXAocD0+cFsxXSksMC4wMSk7CiAgY29uc3QgYmFycz1SRVROLnBvaW50cy5tYXAocD0+YDxkaXYgc3R5bGU9ImZsZXg6MTt0ZXh0LWFsaWduOmNlbnRlciI+CiAgICA8ZGl2IHN0eWxlPSJoZWlnaHQ6OTJweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6ZmxleC1lbmQ7anVzdGlmeS1jb250ZW50OmNlbnRlciI+PGRpdiBzdHlsZT0id2lkdGg6NTglO2JhY2tncm91bmQ6dmFyKC0tYWNjZW50KTtib3JkZXItcmFkaXVzOjVweCA1cHggMCAwO2hlaWdodDoke01hdGgubWF4KDMscFsxXS9teCo5MikudG9GaXhlZCgwKX1weCI+PC9kaXY+PC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MTNweDtmb250LXdlaWdodDo4MDA7bWFyZ2luLXRvcDo1cHgiPiR7KHBbMV0qMTAwKS50b0ZpeGVkKDEpfSU8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImRpbSIgc3R5bGU9ImZvbnQtc2l6ZToxMnB4Ij4ke3BbMF19PC9kaXY+PC9kaXY+YCkuam9pbignJyk7CiAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJjYXJkIj48aDM+7J6s67Cp66y4KOyVsSDrpqzthZDshZgpIDxzcGFuIGNsYXNzPSJtaW5pIj7shKTsuZgg4oaSIOyVsSDri6Tsi5wg7Je06riwIC8gJHtSRVROLndpbmRvd30g7L2U7Zi47Yq4PC9zcGFuPjwvaDM+CiAgICA8ZGl2IGNsYXNzPSJjc3ViIiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMHB4Ij7slbEg7ISk7LmY7ZWcIOyCrOuejOydtCBEMS9ENy9EMTQvRDMw7JeQIOyVseydhCDri6Tsi5wg7JewIOu5hOycqCAo7L2U7Zi47Yq4ICR7ZihSRVROLmNvaG9ydCl966qFLCBEMzDquYzsp4Ag7ISx7IiZKTwvZGl2PgogICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2dhcDoxMnB4O2FsaWduLWl0ZW1zOmZsZXgtZW5kIj4ke2JhcnN9PC9kaXY+PC9kaXY+YDsKfQovLyDshKDtg50g6rWs6rCE7J20ICfslrTsoJwg6riw7KSAIOu5oOuluOyEoO2DnSg3LzE0LzMwLzYw7J28KSfsnbTrqbQg7ZW064u5IOyciOuPhOyasOydmCDsnKDri4jtgawg7Jyg7KCA7IiY66W8IOuwmO2ZmCwg7JWE64uI66m0IG51bGwKZnVuY3Rpb24gYWN0aXZlV2luKCl7IGlmKCFVTklRfHxSQiE9PURBWVMubGVuZ3RoLTEpIHJldHVybiBudWxsOyBjb25zdCBuPVJCLVJBKzE7IHJldHVybiBVTklRW1N0cmluZyhuKV0/U3RyaW5nKG4pOm51bGw7IH0KZnVuY3Rpb24gcmVuZGVyU3VtbWFyeSgpewogIGNvbnN0IFc9d2luZG93cygpLHByPVcucHJpb3IubGVuZ3RoLCB3aW49YWN0aXZlV2luKCksIFU9d2luP1VOSVFbd2luXTpudWxsOwogIGNvbnN0IGcgPShrLGFycik9PiBVPyBVLmN1cltrXSAgOiBzdW0oYXJyLFcuY3VyKTsKICBjb25zdCBncD0oayxhcnIpPT4gVT8gVS5wcmlvcltrXTogKHByP3N1bShhcnIsVy5wcmlvcik6bnVsbCk7CiAgY29uc3QgaW5zdD1nKCdpbnN0YWxsJyxUT1RBTFMuaW5zdGFsbHx8W10pLCBpbnN0UD1ncCgnaW5zdGFsbCcsVE9UQUxTLmluc3RhbGx8fFtdKTsKICBjb25zdCB2aXM9ZygndmlzaXQnLFRPVEFMUy52aXNpdHx8W10pLCB2aXNQPWdwKCd2aXNpdCcsVE9UQUxTLnZpc2l0fHxbXSk7CiAgY29uc3Qgc3U9Zygnc3UnLFRPVEFMUy5zdSksIHN1UD1ncCgnc3UnLFRPVEFMUy5zdSk7CiAgY29uc3QgY2FyZD1nKCdjYXJkJyxUT1RBTFMuY2FyZCksIGNhcmRQPWdwKCdjYXJkJyxUT1RBTFMuY2FyZCk7CiAgY29uc3QgbG89ZygnbG91bmdlJyxUT1RBTFMubG91bmdlKSwgbG9QPWdwKCdsb3VuZ2UnLFRPVEFMUy5sb3VuZ2UpOwogIGNvbnN0IGNhbGx1PWcoJ2NhbGx1JyxUQVhJVE9UVSksIGNhbGx1UD1ncCgnY2FsbHUnLFRBWElUT1RVKTsKICBjb25zdCBjYWxsPWcoJ2NhbGwnLFRBWElUT1QpLCBjYWxsUD1ncCgnY2FsbCcsVEFYSVRPVCk7CiAgY29uc3QgSz0obCxjLHAsdSxtKT0+a3BpKGwsYyxwLHt1bml0OnUsc3ViOmFkU3ViKG0sVyksa206bSxhY3RpdmU6RFJJTExNRVRSSUM9PT1tfSk7CiAgY29uc3QgY2FyZHM9YDxkaXYgY2xhc3M9ImtwaXMga3Bpc3VtIj4KICAgICR7Sygi7JWx7ISk7LmYIixpbnN0LGluc3RQLCLrqoUiLCJpbnN0YWxsIil9CiAgICAke0soIu2ZnOyEsSDsnKDsoIAiLHZpcyx2aXNQLCLrqoUiLCJ2aXNpdCIpfQogICAgJHtLKCLtmozsm5DqsIDsnoUiLHN1LHN1UCwi66qFIiwic3UiKX0KICAgICR7Sygi6rWQ7Ya17Lm065OcIOuTseuhnSIsY2FyZCxjYXJkUCwi6rG0IiwiY2FyZCIpfQogICAgJHtLKCLrjIDspJHqtZDthrXrnbzsmrTsp4Ag67Cp66y4Iixsbyxsb1AsIuuqhSIsImxvdW5nZSIpfQogIDwvZGl2PgogIDxkaXYgY2xhc3M9ImtwaXMga3Bpc3VtIiBzdHlsZT0ibWFyZ2luLXRvcDoxMnB4Ij4KICAgICR7Sygi7YOd7IucIO2YuOy2nCDsnKDsoIDsiJgiLGNhbGx1LGNhbGx1UCwi66qFIiwiY2FsbCIpfQogICAgJHtrcGkoIu2DneyLnCDtmLjstpwg6rG07IiYIixjYWxsLGNhbGxQLHt1bml0OiLqsbQiLHN1YjphZFN1YigiY2FsbCIsVyl9KX0KICA8L2Rpdj5gOwogIHJldHVybiBjYXJkcytzdW1tYXJ5VHJlbmQoVykrZHJpbGxCbG9jayhXKStlZmZCbG9jayhXKTsKfQoKLyogLS0tLSDrsoTtgrcg7YOtID0g6rSR6rOgIOq4sOyXrOunjCAtLS0tICovCmZ1bmN0aW9uIGFnZ1Jvdyhyb3csaWR4KXtjb25zdCBzPXJvdy5zO3JldHVybntjYWxsOnN1bShzLmNhbGwsaWR4KSxjb21wOnN1bShzLmNvbXAsaWR4KSxzdTpzdW0ocy5zdSxpZHgpLGNvc3Q6c3VtKHMuY29zdCxpZHgpfTt9CmZ1bmN0aW9uIGJ1Y2tldFJvd3MoaWQpe3JldHVybiBST1dTLmZpbHRlcihyPT5yLmJ1Y2tldD09PWlkKTt9CmZ1bmN0aW9uIGJ1Y2tldEFjdGl2ZShpZCl7Y29uc3QgYWxsPURBWVMubWFwKChfLGkpPT5pKTtyZXR1cm4gYnVja2V0Um93cyhpZCkuc29tZShyPT5yLmNoIT09Im9yZyImJnN1bShyLnMuY2FsbCxhbGwpPjApO30KY29uc3QgS0lORF9MQUJFTD17Z29vZ2xlOiJnb29nbGUuYWRzIixpbnN0YToiaW5zdGEiLGV0Yzoi67mE7JqpIOuvuOyXsOuPmSDqtJHqs6AiLHhhZHM6IuuLpOuluCDrqqnsoIEg6rSR6rOgIixvcmc6Iuq0keqzoCDsmbgg7LGE64SQIn07Ci8qIO2VnCDrsoTtgrfsnZgg66qo65OgIOyEueyFmOydhCAn7ZWY64KY7J2YIO2RnCfroZwg4oCUIOyXtCDsnITsuZjqsIAg7KCE67aAIOunnuydjCjspITrp57stqQpICovCmZ1bmN0aW9uIGJ1Y2tldENvbHMoQil7IC8vIOqzte2GtSBjb2xncm91cCArIO2XpOuNlCAo67KE7YK3IOuCtCDrqqjrk6Ag7IS57IWYIOuPmeydvCkg4oCUIO2ajOybkOqwgOyehSDsl7Qg7ZWt7IOBIO2PrO2VqAogIGxldCBjb2x3LCBoZWFkOwogIGlmKEIuZHVhbENwYSl7IGNvbHc9WyIyMiUiLCIxMyUiLCIxMSUiLCIxMiUiLCIxMSUiLCIxMiUiLCIxMCUiLCI5JSJdOwogICAgaGVhZD1gPHRoPuy6oO2OmOyduDwvdGg+PHRoIGNsYXNzPSJsaCI+7JWg65Oc6re466O5PC90aD48dGg+7ZqM7JuQ6rCA7J6FPC90aD48dGggY2xhc3M9ImtwaWNvbCI+JHtCLnByaW1hcnlMYWJlbH08L3RoPjx0aD4ke0IuY29tcExhYmVsfTwvdGg+PHRoPuq0keqzoOu5hDwvdGg+PHRoPiR7Qi5jcGFMYWJlbH08L3RoPjx0aD4ke0IuY29tcENwYUxhYmVsfTwvdGg+YDsKICB9IGVsc2UgaWYoQi5zaG93Q29tcCl7IGNvbHc9WyIyNCUiLCIxNCUiLCIxMiUiLCIxNCUiLCIxMyUiLCIxMiUiLCIxMSUiXTsKICAgIGhlYWQ9YDx0aD7suqDtjpjsnbg8L3RoPjx0aCBjbGFzcz0ibGgiPuyVoOuTnOq3uOujuTwvdGg+PHRoPu2ajOybkOqwgOyehTwvdGg+PHRoIGNsYXNzPSJrcGljb2wiPiR7Qi5wcmltYXJ5TGFiZWx9PC90aD48dGg+JHtCLmNvbXBMYWJlbH08L3RoPjx0aD7qtJHqs6DruYQ8L3RoPjx0aD4ke0IuY3BhTGFiZWx9PC90aD5gOwogIH0gZWxzZSB7IGNvbHc9WyIyOCUiLCIxNiUiLCIxNSUiLCIxNSUiLCIxNCUiLCIxMiUiXTsKICAgIGhlYWQ9YDx0aD7suqDtjpjsnbg8L3RoPjx0aCBjbGFzcz0ibGgiPuyVoOuTnOq3uOujuTwvdGg+PHRoPu2ajOybkOqwgOyehTwvdGg+PHRoIGNsYXNzPSJrcGljb2wiPiR7Qi5wcmltYXJ5TGFiZWx9PC90aD48dGg+6rSR6rOg67mEPC90aD48dGg+JHtCLmNwYUxhYmVsfTwvdGg+YDsKICB9CiAgY29uc3QgY29sPWA8Y29sZ3JvdXA+JHtjb2x3Lm1hcCh3PT5gPGNvbCBzdHlsZT0id2lkdGg6JHt3fSI+YCkuam9pbignJyl9PC9jb2xncm91cD5gOwogIHJldHVybiB7bmNvbDpjb2x3Lmxlbmd0aCwgY29sLCBoZWFkOmA8dGhlYWQ+PHRyPiR7aGVhZH08L3RyPjwvdGhlYWQ+YH07Cn0KZnVuY3Rpb24gc2VjUm93KG8sQixub0Nvc3Qsc2hvd0NyKXsKICBsZXQgY2VsbHM9YDx0ZCBjbGFzcz0ibmFtZSI+JHtvLmNhbXB9PC90ZD48dGQgY2xhc3M9Im5hbWUgZGltIj4ke28uYWd8fCcnfSR7c2hvd0NyJiZvLmNyP2AgPHNwYW4gc3R5bGU9Im9wYWNpdHk6LjciPuKAuiAke28uY3J9PC9zcGFuPmA6Jyd9PC90ZD5gOwogIGNlbGxzKz1gPHRkIGNsYXNzPSJkaW0iPiR7ZihvLmNfc3UpfTwvdGQ+YDsgICAvLyDtmozsm5DqsIDsnoUgKO2VreyDgSkKICBpZihCLnNob3dDb21wKSBjZWxscys9YDx0ZCBjbGFzcz0ia3BpY29sIj4ke2Yoby5jX2NhbGwpfSR7ZGVsdGFTcGFuKG8uY19jYWxsLG8ucF9jYWxsPT1udWxsP251bGw6by5wX2NhbGwsdHJ1ZSx0cnVlKX08L3RkPjx0ZD4ke2Yoby5jX2NvbXApfTwvdGQ+YDsKICBlbHNlIGNlbGxzKz1gPHRkIGNsYXNzPSJrcGljb2wiPiR7ZihvLmNfY2FsbCl9JHtkZWx0YVNwYW4oby5jX2NhbGwsby5wX2NhbGw9PW51bGw/bnVsbDpvLnBfY2FsbCx0cnVlLHRydWUpfTwvdGQ+YDsKICBjZWxscys9YDx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzon4oKpJytmKG8uY19jb3N0KX08L3RkPmA7CiAgaWYoQi5kdWFsQ3BhKXtjb25zdCBjMT1vLmNfY2FsbD9vLmNfY29zdC9vLmNfY2FsbDpudWxsLGMyPW8uY19jb21wP28uY19jb3N0L28uY19jb21wOm51bGw7Y2VsbHMrPWA8dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6KGMxIT1udWxsPyfigqknK2YoYzEpOifigJQnKX08L3RkPjx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzooYzIhPW51bGw/J+KCqScrZihjMik6J+KAlCcpfTwvdGQ+YDt9CiAgZWxzZXtjb25zdCBkPUIuc2hvd0NvbXA/by5jX2NvbXA6by5jX2NhbGwsY3BhPW5vQ29zdD9udWxsOihkP28uY19jb3N0L2Q6bnVsbCk7Y2VsbHMrPWA8dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6KGNwYSE9bnVsbD8n4oKpJytmKGNwYSk6J+KAlCcpfTwvdGQ+YDt9CiAgcmV0dXJuIGA8dHI+JHtjZWxsc308L3RyPmA7Cn0KZnVuY3Rpb24gc2VjVG90Um93KGFnZyxCLG5vQ29zdCl7CiAgY29uc3QgdD1hZ2cucmVkdWNlKChhLG8pPT57YS5zdSs9by5jX3N1fHwwO2EuY2FsbCs9by5jX2NhbGx8fDA7YS5jb21wKz1vLmNfY29tcHx8MDthLmNvc3QrPW8uY19jb3N0fHwwO3JldHVybiBhO30se3N1OjAsY2FsbDowLGNvbXA6MCxjb3N0OjB9KTsKICBsZXQgbWlkPWA8dGQgY2xhc3M9ImRpbSI+JHtmKHQuc3UpfTwvdGQ+YCsoQi5zaG93Q29tcD9gPHRkIGNsYXNzPSJrcGljb2wiPiR7Zih0LmNhbGwpfTwvdGQ+PHRkPiR7Zih0LmNvbXApfTwvdGQ+YDpgPHRkIGNsYXNzPSJrcGljb2wiPiR7Zih0LmNhbGwpfTwvdGQ+YCk7CiAgbGV0IGNwYUM7CiAgaWYoQi5kdWFsQ3BhKXtjb25zdCBjMT10LmNhbGw/dC5jb3N0L3QuY2FsbDpudWxsLGMyPXQuY29tcD90LmNvc3QvdC5jb21wOm51bGw7Y3BhQz1gPHRkIGNsYXNzPSJkaW0iPiR7bm9Db3N0PycnOihjMSE9bnVsbD8n4oKpJytmKGMxKTon4oCUJyl9PC90ZD48dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6KGMyIT1udWxsPyfigqknK2YoYzIpOifigJQnKX08L3RkPmA7fQogIGVsc2V7Y29uc3QgZD1CLnNob3dDb21wP3QuY29tcDp0LmNhbGwsY3BhPW5vQ29zdD9udWxsOihkP3QuY29zdC9kOm51bGwpO2NwYUM9YDx0ZCBjbGFzcz0iZGltIj4ke25vQ29zdD8nJzooY3BhIT1udWxsPyfigqknK2YoY3BhKTon4oCUJyl9PC90ZD5gO30KICByZXR1cm4gYDx0ciBjbGFzcz0idG90cm93Ij48dGQ+7ZWp6rOEPC90ZD48dGQ+PC90ZD4ke21pZH08dGQgY2xhc3M9ImRpbSI+JHtub0Nvc3Q/Jyc6J+KCqScrZih0LmNvc3QpfTwvdGQ+JHtjcGFDfTwvdHI+YDsKfQpmdW5jdGlvbiBzZWNCbG9jayhyb3dzLGtpbmQsQixuY29sKXsKICBjb25zdCBub0Nvc3Q9KGtpbmQ9PT0iZXRjInx8a2luZD09PSJvcmciKTsKICBjb25zdCBzaG93Q3I9ISFDUlNIT1dbQi5pZF0mJihraW5kPT09Imdvb2dsZSJ8fGtpbmQ9PT0iaW5zdGEifHxraW5kPT09ImV0YyJ8fGtpbmQ9PT0ieGFkcyIpOwogIGNvbnN0IGdyb3Vwcz17fTsKICByb3dzLmZvckVhY2gocj0+e2NvbnN0IGtleT1yLmNhbXArJ1x1MDAwMScrKHIuYWd8fCcnKSsoc2hvd0NyPydcdTAwMDEnKyhyLmNyfHwnJyk6JycpOyhncm91cHNba2V5XXx8KGdyb3Vwc1trZXldPVtdKSkucHVzaChyKTt9KTsKICBjb25zdCBhZ2c9T2JqZWN0LmtleXMoZ3JvdXBzKS5tYXAoa2V5PT57Y29uc3QgZ3JwPWdyb3Vwc1trZXldLHIwPWdycFswXTtjb25zdCBvPXtjYW1wOnIwLmNhbXAsYWc6cjAuYWcsY3I6c2hvd0NyPyhyMC5jcnx8JycpOicnfTtjb25zdCBjdT17Y2FsbDowLGNvbXA6MCxzdTowLGNvc3Q6MH0scHI9Q1VSVy5wcmlvci5sZW5ndGg/e2NhbGw6MCxjb21wOjAsc3U6MCxjb3N0OjB9Om51bGw7Z3JwLmZvckVhY2gocj0+e2NvbnN0IGE9YWdnUm93KHIsQ1VSVy5jdXIpO2N1LmNhbGwrPWEuY2FsbDtjdS5jb21wKz1hLmNvbXA7Y3Uuc3UrPWEuc3U7Y3UuY29zdCs9YS5jb3N0O2lmKHByKXtjb25zdCBwPWFnZ1JvdyhyLENVUlcucHJpb3IpO3ByLmNhbGwrPXAuY2FsbDtwci5jb21wKz1wLmNvbXA7cHIuc3UrPXAuc3U7cHIuY29zdCs9cC5jb3N0O319KTtbImNhbGwiLCJjb21wIiwic3UiLCJjb3N0Il0uZm9yRWFjaChrPT57b1siY18iK2tdPWN1W2tdO2lmKHByKW9bInBfIitrXT1wcltrXTt9KTtyZXR1cm4gbzt9KS5zb3J0KChhLGIpPT5iLmNfY2FsbC1hLmNfY2FsbCk7CiAgY29uc3QgaGQ9YDx0ciBjbGFzcz0ic2VjaGQtcm93Ij48dGQgY29sc3Bhbj0iJHtuY29sfSI+JHtLSU5EX0xBQkVMW2tpbmRdfTwvdGQ+PC90cj5gOwogIHJldHVybiBoZCthZ2cubWFwKG89PnNlY1JvdyhvLEIsbm9Db3N0LHNob3dDcikpLmpvaW4oJycpK3NlY1RvdFJvdyhhZ2csQixub0Nvc3QpOwp9CmxldCBDVVJXPW51bGw7CmZ1bmN0aW9uIGxvdW5nZUJsb2NrKFcsYyl7CiAgY29uc3Qgd2luPWFjdGl2ZVdpbigpLCBVPXdpbj9VTklRW3dpbl06bnVsbDsKICBjb25zdCBhcHBUPVU/VS5jdXIudmlzaXQ6c3VtKFRPVEFMUy52aXNpdHx8W10sVy5jdXIpLCBsbmdUPVU/VS5jdXIubG91bmdlOnN1bShUT1RBTFMubG91bmdlfHxbXSxXLmN1cik7CiAgY29uc3QgYXBwVHA9Vy5wcmlvci5sZW5ndGg/KFU/VS5wcmlvci52aXNpdDpzdW0oVE9UQUxTLnZpc2l0fHxbXSxXLnByaW9yKSk6bnVsbDsKICBjb25zdCBsbmdUcD1XLnByaW9yLmxlbmd0aD8oVT9VLnByaW9yLmxvdW5nZTpzdW0oVE9UQUxTLmxvdW5nZXx8W10sVy5wcmlvcikpOm51bGw7CiAgY29uc3QgcmF0ZVQ9YXBwVD9sbmdUL2FwcFQqMTAwOjA7CiAgY29uc3QgYXBwQT1kcmlsbEFkKCd2aXNpdCcsVy5jdXIpLCBsbmdBPWRyaWxsQWQoJ2xvdW5nZScsVy5jdXIpLCBjb3N0QT1jLmNvc3Q7CiAgY29uc3QgYXBwQXA9Vy5wcmlvci5sZW5ndGg/ZHJpbGxBZCgndmlzaXQnLFcucHJpb3IpOm51bGwsIGxuZ0FwPVcucHJpb3IubGVuZ3RoP2RyaWxsQWQoJ2xvdW5nZScsVy5wcmlvcik6bnVsbDsKICBjb25zdCBjcHY9bG5nQT9jb3N0QS9sbmdBOm51bGwsIHJhdGVBPWFwcEE/bG5nQS9hcHBBKjEwMDowOwogIGNvbnN0IHdob2xlPWA8ZGl2IGNsYXNzPSJjYXJkIj48aDM+64yA7KSR6rWQ7Ya165287Jq07KeAIOynhOyehSA8c3BhbiBjbGFzcz0ibWluaSI+7KCE7LK0ICjqtJHqs6Ar67mE6rSR6rOgKTwvc3Bhbj48L2gzPgogICAgPGRpdiBjbGFzcz0ia3BpcyI+JHtrcGkoIu2ZnOyEsSDsnKDsoIAiLGFwcFQsYXBwVHAse3VuaXQ6IuuqhSJ9KX0ke2twaSgi65287Jq07KeAIOuwqeusuOyekCIsbG5nVCxsbmdUcCx7cHJpbWFyeTp0cnVlLHVuaXQ6IuuqhSJ9KX08ZGl2IGNsYXNzPSJrcGkiPjxkaXYgY2xhc3M9InQiPuudvOyatOyngCDsp4TsnoXrpaA8L2Rpdj48ZGl2IGNsYXNzPSJiaWciIHN0eWxlPSJjb2xvcjp2YXIoLS1hY2NlbnQpIj4ke3JhdGVULnRvRml4ZWQoMSl9PHNwYW4gY2xhc3M9InUiPiU8L3NwYW4+PC9kaXY+PC9kaXY+PC9kaXY+PC9kaXY+YDsKICBjb25zdCBhZGM9YDxkaXYgY2xhc3M9ImNhcmQiPjxoMz7qtJHqs6Ag6riw7JesIDxzcGFuIGNsYXNzPSJtaW5pIj7qtJHqs6DroZwg7Jyg7J6F65CcIO2ZnOyEsSDsnKDsoIAg6riw7KSAPC9zcGFuPjwvaDM+CiAgICA8ZGl2IGNsYXNzPSJrcGlzIj4ke2twaSgi7Zmc7ISxIOycoOyggCIsYXBwQSxhcHBBcCx7dW5pdDoi66qFIn0pfSR7a3BpKCLrnbzsmrTsp4Ag67Cp66y47J6QIixsbmdBLGxuZ0FwLHtwcmltYXJ5OnRydWUsdW5pdDoi66qFIn0pfTxkaXYgY2xhc3M9ImtwaSI+PGRpdiBjbGFzcz0idCI+65287Jq07KeAIOynhOyeheuloDwvZGl2PjxkaXYgY2xhc3M9ImJpZyIgc3R5bGU9ImNvbG9yOnZhcigtLWFjY2VudCkiPiR7cmF0ZUEudG9GaXhlZCgxKX08c3BhbiBjbGFzcz0idSI+JTwvc3Bhbj48L2Rpdj48L2Rpdj4ke2twaSgi6rSR6rOg67mEIixjb3N0QSxudWxsLHt3b246dHJ1ZX0pfTxkaXYgY2xhc3M9ImtwaSI+PGRpdiBjbGFzcz0idCI+67Cp66y47J6Q64u5IOu5hOyaqTwvZGl2PjxkaXYgY2xhc3M9ImJpZyI+JHtjcHYhPW51bGw/J+KCqScrZihjcHYpOifigJQnfTwvZGl2PjwvZGl2PjwvZGl2PjwvZGl2PmA7CiAgY29uc3QgdlBjdD1sbmdUP01hdGgubWluKDEwMCxsbmdBL2xuZ1QqMTAwKTowOwogIGNvbnN0IHNoYXJlPWA8ZGl2IGNsYXNzPSJjYXJkIiBzdHlsZT0icGFkZGluZzoxM3B4IDE2cHgiPmAKICAgICtgPGRpdiBzdHlsZT0iZm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxNHB4O21hcmdpbi1ib3R0b206OXB4Ij7qtJHqs6Ag6riw7JesPC9kaXY+YAogICAgK2A8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6YmFzZWxpbmU7Z2FwOjlweCI+PGRpdiBjbGFzcz0iY3N1YiIgc3R5bGU9Im1hcmdpbjowIj7rsKnrrLjsnpAg6riw7KSAPC9kaXY+PGRpdiBzdHlsZT0iZm9udC1zaXplOjIwcHg7Zm9udC13ZWlnaHQ6ODAwO2NvbG9yOnZhcigtLWFjY2VudCkiPiR7dlBjdC50b0ZpeGVkKHZQY3Q8MTA/MTowKX0lPC9kaXY+PGRpdiBjbGFzcz0iY3N1YiIgc3R5bGU9Im1hcmdpbjowIj7qtJHqs6AgJHtmKGxuZ0EpfSAvIOyghOyytCAke2YobG5nVCl9PC9kaXY+PC9kaXY+PC9kaXY+YDsKICByZXR1cm4gd2hvbGUrYWRjK3NoYXJlOwp9CmZ1bmN0aW9uIHJlbmRlckxvdW5nZUV2ZW50cygpewogIGNvbnN0IFc9d2luZG93cygpLCBEPShMRVZULmRhaWx5KXx8e30sIG5hbWVzPU9iamVjdC5rZXlzKEQpOwogIGlmKCFuYW1lcy5sZW5ndGgpIHJldHVybiBgPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0iY3N1YiI+65287Jq07KeAIOydtOuypO2KuCDrjbDsnbTthLDqsIAg7JeG7Iq164uI64ukPC9kaXY+PC9kaXY+YDsKICBjb25zdCBuPVcuY3VyLmxlbmd0aCwgYW5jaG9yZWQ9KFJCPT09REFZUy5sZW5ndGgtMSk7CiAgY29uc3QgVT0oYW5jaG9yZWQgJiYgTEVWVC51bmlxICYmIExFVlQudW5pcVtTdHJpbmcobildKT9MRVZULnVuaXFbU3RyaW5nKG4pXTpudWxsOyAgLy8gNy8xNC8zMOydvCDruaDrpbjshKDtg53snbTrqbQg7KeE7KecIOycoOuLiO2BrAogIGNvbnN0IHJvd3M9bmFtZXMubWFwKG5tPT57CiAgICBjb25zdCBldj1zdW0oRFtubV0uZXZ8fFtdLFcuY3VyKTsKICAgIGNvbnN0IHV1PShVICYmIFVbbm1dIT1udWxsKT8gVVtubV0gOiBzdW0oRFtubV0udXV8fFtdLFcuY3VyKTsKICAgIHJldHVybiB7bmFtZTpubSxldix1dX07CiAgfSkuZmlsdGVyKHI9PnIuZXY+MCk7CiAgcm93cy5zb3J0KChhLGIpPT4gTEVTT1JUPT09J3V1Jz8gYi51dS1hLnV1IDogKExFU09SVD09PSduYW1lJz8gYS5uYW1lLmxvY2FsZUNvbXBhcmUoYi5uYW1lKSA6IGIuZXYtYS5ldikpOwogIGNvbnN0IGFyPWs9PkxFU09SVD09PWs/JyDilr4nOicnOwogIGNvbnN0IHRvdFU9KGFuY2hvcmVkICYmIExFVlQudG90VW5pcSAmJiBMRVZULnRvdFVuaXFbU3RyaW5nKG4pXSE9bnVsbCk/TEVWVC50b3RVbmlxW1N0cmluZyhuKV06c3VtKExFVlQudG90RGFpbHl8fFtdLFcuY3VyKTsKICBjb25zdCBoZWFkPWA8ZGl2IGNsYXNzPSJjYXJkIiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxNHB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpiYXNlbGluZTtnYXA6MTJweDtmbGV4LXdyYXA6d3JhcCI+PGRpdiBzdHlsZT0iZm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxNXB4Ij7rnbzsmrTsp4Ag7Iic67Cp66y47J6QPC9kaXY+PGRpdiBzdHlsZT0iZm9udC1zaXplOjI0cHg7Zm9udC13ZWlnaHQ6ODAwO2NvbG9yOnZhcigtLWFjY2VudCk7bGV0dGVyLXNwYWNpbmc6LTAuNXB4Ij4ke2YodG90VSl9PHNwYW4gc3R5bGU9ImZvbnQtc2l6ZToxNHB4O2ZvbnQtd2VpZ2h0OjcwMCI+IOuqhTwvc3Bhbj48L2Rpdj48L2Rpdj5gOwogIGNvbnN0IGJvZHk9cm93cy5tYXAocj0+YDx0cj48dGQgc3R5bGU9ImZvbnQtc2l6ZToxM3B4Ij4ke3IubmFtZX08L3RkPjx0ZCBzdHlsZT0idGV4dC1hbGlnbjpyaWdodDtmb250LXZhcmlhbnQtbnVtZXJpYzp0YWJ1bGFyLW51bXM7Zm9udC13ZWlnaHQ6NzAwIj4ke2Yoci5ldil9PC90ZD48dGQgc3R5bGU9InRleHQtYWxpZ246cmlnaHQ7Zm9udC12YXJpYW50LW51bWVyaWM6dGFidWxhci1udW1zO2ZvbnQtd2VpZ2h0OjcwMCI+JHtmKHIudXUpfTwvdGQ+PHRkIHN0eWxlPSJ0ZXh0LWFsaWduOnJpZ2h0O2ZvbnQtdmFyaWFudC1udW1lcmljOnRhYnVsYXItbnVtcztjb2xvcjp2YXIoLS1zdWIpIj4ke3IudXU/KHIuZXYvci51dSkudG9GaXhlZCgxKTonLSd9PC90ZD48L3RyPmApLmpvaW4oJycpOwogIHJldHVybiBoZWFkK2A8ZGl2IGNsYXNzPSJjYXJkIj48ZGl2IGNsYXNzPSJzY3JvbGwiPjx0YWJsZSBjbGFzcz0ibGV2dC10YmwiIHN0eWxlPSJtaW4td2lkdGg6NTIwcHgiPgogICAgICA8dGhlYWQ+PHRyPjx0aCBzdHlsZT0idGV4dC1hbGlnbjpsZWZ0O2N1cnNvcjpwb2ludGVyIiBkYXRhLWxlc29ydD0ibmFtZSI+7J2067Kk7Yq4JHthcignbmFtZScpfTwvdGg+PHRoIHN0eWxlPSJ0ZXh0LWFsaWduOnJpZ2h0O2N1cnNvcjpwb2ludGVyIiBkYXRhLWxlc29ydD0iZXYiPuuwnOyDneyImCR7YXIoJ2V2Jyl9PC90aD48dGggc3R5bGU9InRleHQtYWxpZ246cmlnaHQ7Y3Vyc29yOnBvaW50ZXIiIGRhdGEtbGVzb3J0PSJ1dSI+7Iic7Jyg7KCAJHthcigndXUnKX08L3RoPjx0aCBzdHlsZT0idGV4dC1hbGlnbjpyaWdodCI+MeyduOuLuTwvdGg+PC90cj48L3RoZWFkPgogICAgICA8dGJvZHk+JHtib2R5fTwvdGJvZHk+PC90YWJsZT48L2Rpdj48L2Rpdj5gOwp9CmZ1bmN0aW9uIGluZm9UaXAocm93cyl7Y29uc3QgYm9keT1yb3dzLm1hcChyPT5gPHRyPjx0ZD4ke3JbMF19PC90ZD48dGQ+JHtyWzFdfTwvdGQ+PC90cj5gKS5qb2luKCcnKTtyZXR1cm4gYDxzcGFuIGNsYXNzPSJpdGlwIiB0YWJpbmRleD0iMCI+PHNwYW4gY2xhc3M9ImljbyI+aTwvc3Bhbj48c3BhbiBjbGFzcz0icG9wIj48dGFibGU+JHtib2R5fTwvdGFibGU+PC9zcGFuPjwvc3Bhbj5gO30KZnVuY3Rpb24gdGF4aUJsb2NrKFcsYyxwLGFkQWxsLHRvdENhbGwpewogIGNvbnN0IHdpbj1hY3RpdmVXaW4oKSwgVT13aW4/VU5JUVt3aW5dOm51bGw7CiAgY29uc3QgdT1VP1UuY3VyLmNhbGx1OnN1bShUQVhJVE9UVXx8W10sVy5jdXIpLCB1UD1XLnByaW9yLmxlbmd0aD8oVT9VLnByaW9yLmNhbGx1OnN1bShUQVhJVE9UVXx8W10sVy5wcmlvcikpOm51bGw7CiAgY29uc3QgY2M9VT9VLmN1ci5jYWxsOnN1bShUQVhJVE9UfHxbXSxXLmN1ciksIGNjUD1XLnByaW9yLmxlbmd0aD8oVT9VLnByaW9yLmNhbGw6c3VtKFRBWElUT1R8fFtdLFcucHJpb3IpKTpudWxsOwogIGNvbnN0IGF2Zz11P2NjL3U6MCwgY3BjPWMuY2FsbD9jLmNvc3QvYy5jYWxsOm51bGw7CiAgY29uc3QgYWR1PXN1bShEQVRBLnRheGlhZHV8fFtdLFcuY3VyKSwgYWR1UD1XLnByaW9yLmxlbmd0aD9zdW0oREFUQS50YXhpYWR1fHxbXSxXLnByaW9yKTpudWxsOwogIGNvbnN0IHdob2xlPWA8ZGl2IGNsYXNzPSJjYXJkIj48aDM+7YOd7IucIO2YuOy2nCA8c3BhbiBjbGFzcz0ibWluaSI+7KCE7LK0ICjqtJHqs6Ar67mE6rSR6rOgKTwvc3Bhbj48L2gzPgogICAgPGRpdiBjbGFzcz0ia3BpcyI+JHtrcGkoIu2DneyLnCDtmLjstpwg7Jyg7KCA7IiYIix1LHVQLHt1bml0OiLrqoUifSl9JHtrcGkoIu2DneyLnCDtmLjstpwg6rG07IiYIixjYyxjY1Ase3ByaW1hcnk6dHJ1ZSx1bml0OiLqsbQifSl9PGRpdiBjbGFzcz0ia3BpIj48ZGl2IGNsYXNzPSJ0Ij4x7J2464u5IO2YuOy2nDwvZGl2PjxkaXYgY2xhc3M9ImJpZyIgc3R5bGU9ImNvbG9yOnZhcigtLWFjY2VudCkiPiR7YXZnLnRvRml4ZWQoMSl9PHNwYW4gY2xhc3M9InUiPu2ajDwvc3Bhbj48L2Rpdj48L2Rpdj48L2Rpdj48L2Rpdj5gOwogIGNvbnN0IGFkYz1gPGRpdiBjbGFzcz0iY2FyZCI+PGgzPuq0keqzoCDshLHqs7wgPHNwYW4gY2xhc3M9Im1pbmkiPuuqqeyggSDsuqDtjpjsnbg8L3NwYW4+PC9oMz4KICAgIDxkaXYgY2xhc3M9ImtwaXMiPiR7a3BpKCLtg53si5wg7Zi47LacIOycoOyggOyImCIsYWR1LGFkdVAse3VuaXQ6IuuqhSJ9KX0ke2twaSgi7YOd7IucIO2YuOy2nCDqsbTsiJgiLGMuY2FsbCxwJiZwLmNhbGwse3ByaW1hcnk6dHJ1ZSx1bml0OiLqsbQifSl9JHtrcGkoIuq0keqzoOu5hCIsYy5jb3N0LHAmJnAuY29zdCx7d29uOnRydWV9KX08ZGl2IGNsYXNzPSJrcGkiPjxkaXYgY2xhc3M9InQiPu2YuOy2nOuLuSDruYTsmqk8L2Rpdj48ZGl2IGNsYXNzPSJiaWciPiR7Y3BjIT1udWxsPyfigqknK2YoY3BjKTon4oCUJ308L2Rpdj48L2Rpdj48L2Rpdj48L2Rpdj5gOwogIGNvbnN0IHVQY3Q9dT9NYXRoLm1pbigxMDAsYWR1L3UqMTAwKTowLCBjUGN0PXRvdENhbGw/TWF0aC5taW4oMTAwLGMuY2FsbC90b3RDYWxsKjEwMCk6MDsKICBjb25zdCBzaGFyZT1gPGRpdiBjbGFzcz0iY2FyZCIgc3R5bGU9InBhZGRpbmc6MTNweCAxNnB4Ij5gCiAgICArYDxkaXYgc3R5bGU9ImZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTRweDttYXJnaW4tYm90dG9tOjlweCI+6rSR6rOgIOq4sOyXrDwvZGl2PmAKICAgICtgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2dhcDozMnB4O2ZsZXgtd3JhcDp3cmFwO2FsaWduLWl0ZW1zOmJhc2VsaW5lIj5gCiAgICArYDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpiYXNlbGluZTtnYXA6OXB4Ij48ZGl2IGNsYXNzPSJjc3ViIiBzdHlsZT0ibWFyZ2luOjAiPuycoOyggCDquLDspIA8L2Rpdj48ZGl2IHN0eWxlPSJmb250LXNpemU6MjBweDtmb250LXdlaWdodDo4MDA7Y29sb3I6dmFyKC0tYWNjZW50KSI+JHt1UGN0LnRvRml4ZWQodVBjdDwxMD8xOjApfSU8L2Rpdj48ZGl2IGNsYXNzPSJjc3ViIiBzdHlsZT0ibWFyZ2luOjAiPuq0keqzoCAke2YoYWR1KX0gLyDsoITssrQgJHtmKHUpfTwvZGl2PjwvZGl2PmAKICAgICtgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmJhc2VsaW5lO2dhcDo5cHgiPjxkaXYgY2xhc3M9ImNzdWIiIHN0eWxlPSJtYXJnaW46MCI+7Zi47LacIOq4sOykgDwvZGl2PjxkaXYgc3R5bGU9ImZvbnQtc2l6ZToyMHB4O2ZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1hY2NlbnQpIj4ke2NQY3QudG9GaXhlZChjUGN0PDEwPzE6MCl9JTwvZGl2PjxkaXYgY2xhc3M9ImNzdWIiIHN0eWxlPSJtYXJnaW46MCI+6rSR6rOgICR7ZihjLmNhbGwpfSAvIOyghOyytCAke2YodG90Q2FsbCl9PC9kaXY+PC9kaXY+PC9kaXY+PC9kaXY+YDsKICByZXR1cm4gd2hvbGUrYWRjK3NoYXJlOwp9CmZ1bmN0aW9uIHJlbmRlckJ1Y2tldChpZCl7CiAgY29uc3QgQj1CVUNLRVRTLmZpbmQoeD0+eC5pZD09PWlkKSxyb3dzPWJ1Y2tldFJvd3MoaWQpO0NVUlc9d2luZG93cygpOwogIGNvbnN0IGlzQWQ9cj0+ci5jaD09PSJnb29nbGUifHxyLmNoPT09Imluc3RhInx8ci5jaD09PSJldGMiOwogIGNvbnN0IGFnZz0oaWR4LGZpbHQpPT5yb3dzLnJlZHVjZSgobyxyKT0+e2lmKGZpbHQmJiFmaWx0KHIpKXJldHVybiBvO2NvbnN0IGE9YWdnUm93KHIsaWR4KTtvLmNhbGwrPWEuY2FsbDtvLmNvbXArPWEuY29tcDtvLmNvc3QrPWEuY29zdDtyZXR1cm4gbzt9LHtjYWxsOjAsY29tcDowLGNvc3Q6MH0pOwogIGNvbnN0IGM9YWdnKENVUlcuY3VyLGlzQWQpLHA9Q1VSVy5wcmlvci5sZW5ndGg/YWdnKENVUlcucHJpb3IsaXNBZCk6bnVsbDsgICAgICAgICAgLy8g6rSR6rOgIOq4sOyXrCjrqqnsoIEg7Lqg7Y6Y7J24KQogIGNvbnN0IGJ0PUJUT1RbaWRdfHx7fTsgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIC8vIOyghOyytCjsoIQg7LGE64SQLCDsp4HsoJEg7KeR6rOEKQogIGNvbnN0IHRvdENhbGw9c3VtKGJ0LmNhbGx8fFtdLENVUlcuY3VyKSwgdG90Q2FsbFA9Q1VSVy5wcmlvci5sZW5ndGg/c3VtKGJ0LmNhbGx8fFtdLENVUlcucHJpb3IpOm51bGw7CiAgY29uc3QgdG90Q29tcD1zdW0oYnQuY29tcHx8W10sQ1VSVy5jdXIpLCB0b3RDb21wUD1DVVJXLnByaW9yLmxlbmd0aD9zdW0oYnQuY29tcHx8W10sQ1VSVy5wcmlvcik6bnVsbDsKICBjb25zdCBhZEFsbD1hZ2coQ1VSVy5jdXIscj0+ci5jaCE9PSJvcmciKS5jYWxsOyAgIC8vIOyghOyytCDqtJHqs6Ag6riw7JesKOuqqeyggSvri6TrpbjrqqnsoIEsIG9yZyDsoJzsmbgpCiAgY29uc3Qgbm9BZD1hZEFsbD09PTA7CiAgbGV0IGhlYWQ7CiAgaWYoaWQ9PT0idGF4aSIpIGhlYWQ9dGF4aUJsb2NrKENVUlcsYyxwLGFkQWxsLHRvdENhbGwpOwogIGVsc2UgaWYoaWQ9PT0ibG91bmdlIikgaGVhZD1sb3VuZ2VCbG9jayhDVVJXLGMpOwogIGVsc2UgewogICAgY29uc3QgY3BhPXg9PnguY2FsbD94LmNvc3QveC5jYWxsOm51bGw7CiAgICBoZWFkPWA8ZGl2IGNsYXNzPSJrcGlzIj5gK2twaSgi7KCE7LK0ICIrQi5wcmltYXJ5TGFiZWwsdG90Q2FsbCx0b3RDYWxsUCx7dW5pdDpCLnVuaXR8fCLqsbQifSkra3BpKCLqtJHqs6AgIitCLnByaW1hcnlMYWJlbCxjLmNhbGwscCYmcC5jYWxsLHtwcmltYXJ5OnRydWUsdW5pdDpCLnVuaXR8fCLqsbQifSkra3BpKCLqtJHqs6DruYQiLGMuY29zdCxwJiZwLmNvc3Qse3dvbjp0cnVlfSkra3BpKEIuY3BhTGFiZWx8fCJDUEEiLGNwYShjKSxwJiZjcGEocCkse3dvbjp0cnVlLGdvb2RVcDpmYWxzZX0pK2A8L2Rpdj5gOwogIH0KICBsZXQgaHRtbD1oZWFkK2J1Y2tldFRyZW5kKEIscm93cyxDVVJXKTsKICBpZighbm9BZCl7CiAgICBjb25zdCBDPWJ1Y2tldENvbHMoQik7CiAgICBjb25zdCBraW5kcz1CLnNob3dPcmc/WyJnb29nbGUiLCJpbnN0YSIsImV0YyIsInhhZHMiLCJvcmciXTpbImdvb2dsZSIsImluc3RhIiwiZXRjIiwieGFkcyJdOwogICAgbGV0IGJvZHk9IiI7CiAgICBraW5kcy5mb3JFYWNoKGtpbmQ9Pntjb25zdCBycz1yb3dzLmZpbHRlcihyPT5yLmNoPT09a2luZCk7aWYocnMubGVuZ3RoKWJvZHkrPXNlY0Jsb2NrKHJzLGtpbmQsQixDLm5jb2wpO30pOwogICAgaHRtbCs9YDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9InNjcm9sbCI+PHRhYmxlIHN0eWxlPSJtaW4td2lkdGg6NjgwcHg7dGFibGUtbGF5b3V0OmZpeGVkIj4ke0MuY29sfSR7Qy5oZWFkfTx0Ym9keT4ke2JvZHl9PC90Ym9keT48L3RhYmxlPjwvZGl2PjwvZGl2PmA7CiAgfQogIHJldHVybiBodG1sOwp9CgovKiAtLS0tIOyFuCAtLS0tICovCmxldCBUQUI9InN1bW1hcnkiOwpmdW5jdGlvbiBkcmlsbEFkKG1ldHJpYyxpZHgpe3JldHVybiAoRFJJTEwmJkRSSUxMLnJvd3MpP0RSSUxMLnJvd3MucmVkdWNlKChhLHIpPT5hK3N1bShyLnZbbWV0cmljXXx8W10saWR4KSwwKTowO30KZnVuY3Rpb24gYWRTdWIobWV0cmljLFcpe2NvbnN0IGFkPWRyaWxsQWQobWV0cmljLFcuY3VyKTtjb25zdCB0bT17aW5zdGFsbDpUT1RBTFMuaW5zdGFsbCx2aXNpdDpUT1RBTFMudmlzaXQsc3U6VE9UQUxTLnN1LGNhcmQ6VE9UQUxTLmNhcmQsbG91bmdlOlRPVEFMUy5sb3VuZ2UsY2FsbDpUQVhJVE9UfTtjb25zdCB0b3Q9c3VtKHRtW21ldHJpY118fFtdLFcuY3VyKTtjb25zdCBwPXRvdD9NYXRoLm1pbigxMDAsYWQvdG90KjEwMCk6MDtyZXR1cm4gYDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLWFjY2VudCk7Zm9udC13ZWlnaHQ6NzAwO21hcmdpbi10b3A6N3B4O3BhZGRpbmctdG9wOjZweDtib3JkZXItdG9wOjFweCBzb2xpZCB2YXIoLS1saW5lKSI+6rSR6rOgICR7ZihhZCl9IC8gJHtwLnRvRml4ZWQoMSl9JTwvZGl2PmA7fQpmdW5jdGlvbiBkcmlsbEJsb2NrKFcpewogIGlmKCFEUklMTHx8IURSSUxMLnJvd3N8fCFEUklMTC5yb3dzLmxlbmd0aCkgcmV0dXJuICcnOwogIGNvbnN0IG1rPURSSUxMTUVUUklDOwogIGNvbnN0IGNoaXBzPURSSUxMLm1ldHJpY3MubWFwKG09PmA8YnV0dG9uIGNsYXNzPSJkY2hpcCAke21bMF09PT1taz8nb24nOicnfSIgZGF0YS1rbT0iJHttWzBdfSI+JHttWzFdfTwvYnV0dG9uPmApLmpvaW4oJycpOwogIGNvbnN0IHJvb3Q9e2NoaWxkcmVuOnt9fTsKICBEUklMTC5yb3dzLmZvckVhY2gocj0+ewogICAgY29uc3QgdmFsPXN1bShyLnZbbWtdfHxbXSxXLmN1cik7IGlmKCF2YWwpIHJldHVybjsKICAgIGNvbnN0IHBhdGg9W3IuY2gsci5jYW1wLHIuYWddOyBsZXQgbm9kZT1yb290LCBrZXk9Jyc7CiAgICBwYXRoLmZvckVhY2goKHNlZyxkZXB0aCk9Pnsga2V5Kz0nXHUwMDAxJytzZWc7CiAgICAgIG5vZGUuY2hpbGRyZW5ba2V5XT1ub2RlLmNoaWxkcmVuW2tleV18fHtuYW1lOnNlZyxkZXB0aCx2YWw6MCxjaGlsZHJlbjp7fX07CiAgICAgIG5vZGUuY2hpbGRyZW5ba2V5XS52YWwrPXZhbDsgbm9kZT1ub2RlLmNoaWxkcmVuW2tleV07IH0pOwogIH0pOwogIGNvbnN0IGFkVG90YWw9T2JqZWN0LnZhbHVlcyhyb290LmNoaWxkcmVuKS5yZWR1Y2UoKGEsYyk9PmErYy52YWwsMCl8fDE7CiAgbGV0IGJvZHk9Jyc7CiAgKGZ1bmN0aW9uIHdhbGsobm9kZSl7CiAgICBPYmplY3QudmFsdWVzKG5vZGUuY2hpbGRyZW4pLnNvcnQoKGEsYik9PmIudmFsLWEudmFsKS5mb3JFYWNoKGM9PnsKICAgICAgaWYoYy52YWw8PTApIHJldHVybjsKICAgICAgY29uc3QgcGN0PWMudmFsL2FkVG90YWwqMTAwOwogICAgICBjb25zdCBubT1jLmRlcHRoPT09Mj8oJ1x1MjAzYSAnK2MubmFtZSk6Yy5uYW1lOwogICAgICBjb25zdCB3dD1jLmRlcHRoPT09MD8nZm9udC13ZWlnaHQ6NzAwJzooYy5kZXB0aD09PTI/J2NvbG9yOnZhcigtLXN1YiknOicnKTsKICAgICAgYm9keSs9YDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweDtwYWRkaW5nOjVweCAwO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHZhcigtLWxpbmUpO2ZvbnQtc2l6ZToxM3B4O3BhZGRpbmctbGVmdDokezgrYy5kZXB0aCoxOH1weCI+PHNwYW4gc3R5bGU9ImZsZXg6MTtvdmVyZmxvdzpoaWRkZW47dGV4dC1vdmVyZmxvdzplbGxpcHNpczt3aGl0ZS1zcGFjZTpub3dyYXA7JHt3dH0iPiR7bm19PC9zcGFuPjxzcGFuIHN0eWxlPSJmb250LXZhcmlhbnQtbnVtZXJpYzp0YWJ1bGFyLW51bXM7Zm9udC13ZWlnaHQ6NzAwIj4ke2YoYy52YWwpfSA8c3BhbiBjbGFzcz0iZGltIiBzdHlsZT0iZm9udC13ZWlnaHQ6NDAwIj4vICR7cGN0LnRvRml4ZWQoMSl9JTwvc3Bhbj48L3NwYW4+PC9kaXY+YDsKICAgICAgd2FsayhjKTsKICAgIH0pOwogIH0pKHJvb3QpOwogIGNvbnN0IG1uYW1lPShEUklMTC5tZXRyaWNzLmZpbmQobT0+bVswXT09PW1rKXx8WycnLCcnXSlbMV07CiAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJjYXJkIj48aDM+6rSR6rOgIOq4sOyXrCA8YiBzdHlsZT0iY29sb3I6dmFyKC0tYWNjZW50KSI+JHttbmFtZX08L2I+IDxzcGFuIGNsYXNzPSJtaW5pIj7CtyDssYTrhJAg4pa4IOy6oO2OmOyduCDilrgg7JWg65Oc6re466O5PC9zcGFuPjwvaDM+CiAgICA8ZGl2IGNsYXNzPSJkY2hpcHMiPiR7Y2hpcHN9PC9kaXY+CiAgICA8ZGl2PiR7Ym9keXx8JzxkaXYgY2xhc3M9ImNzdWIiPuyEoO2DnSDquLDqsIQg642w7J207YSwIOyXhuydjDwvZGl2Pid9PC9kaXY+PC9kaXY+YDsKfQpmdW5jdGlvbiBmdW5uZWxCbG9jaygpewogIGlmKCFGVU5ORUx8fCFGVU5ORUwucm93c3x8IUZVTk5FTC5yb3dzLmxlbmd0aCkgcmV0dXJuICcnOwogIGNvbnN0IHRvdD1GVU5ORUwucm93cy5maW5kKHI9PnJbMF09PT0n7KCE7LK0Jyl8fEZVTk5FTC5yb3dzWzBdOwogIGNvbnN0IGluc3Q9dG90WzFdLCBzZz10b3RbMl0sIHN2Yz10b3RbM107CiAgY29uc3QgaTJzPWluc3Q/c2cvaW5zdCoxMDA6MDsKICBjb25zdCBhY3E9YDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpjZW50ZXI7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQtc29mdCk7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6MTFweCAxNHB4O21hcmdpbi1ib3R0b206MTRweDtmb250LXNpemU6MTNweCI+PHNwYW4+PGI+7ZqN65OdPC9iPiAmbmJzcDvshKTsuZggJHtmKGluc3QpfSDihpIg6rCA7J6FICR7ZihzZyl9PC9zcGFuPjxzcGFuIHN0eWxlPSJjb2xvcjp2YXIoLS1hY2NlbnQpO2ZvbnQtd2VpZ2h0OjgwMCI+7ISk7LmYIOuMgOu5hCAke2kycy50b0ZpeGVkKDEpfSU8L3NwYW4+PC9kaXY+YDsKICBjb25zdCBzdGFnZXM9W1si7ZqM7JuQ6rCA7J6FIixzZ10sWyLshJzruYTsiqQg7J207JqpIixzdmNdXTsKICBjb25zdCBiYXJzPXN0YWdlcy5tYXAoKHMsaSk9Pntjb25zdCBwPXNnP3NbMV0vc2cqMTAwOjA7CiAgICByZXR1cm4gYDxkaXYgc3R5bGU9Im1hcmdpbjo3cHggMCI+PGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2ZvbnQtc2l6ZToxMnB4O21hcmdpbi1ib3R0b206M3B4Ij48c3Bhbj4ke3NbMF19PC9zcGFuPjxzcGFuIGNsYXNzPSJkaW0iPiR7aT09PTA/YCR7ZihzWzFdKX3rqoUgKOqwgOyehSAxMDAlKWA6YCR7ZihzWzFdKX3rqoUgLyDqsIDsnoUg64yA67mEICR7cC50b0ZpeGVkKDEpfSVgfTwvc3Bhbj48L2Rpdj48ZGl2IHN0eWxlPSJiYWNrZ3JvdW5kOnZhcigtLWFjY2VudC1zb2Z0KTtib3JkZXItcmFkaXVzOjZweDtoZWlnaHQ6MTNweCI+PGRpdiBzdHlsZT0id2lkdGg6JHtwLnRvRml4ZWQoMSl9JTtoZWlnaHQ6MTAwJTtiYWNrZ3JvdW5kOnZhcigtLWFjY2VudCk7Ym9yZGVyLXJhZGl1czo2cHgiPjwvZGl2PjwvZGl2PjwvZGl2PmA7fSkuam9pbignJyk7CiAgY29uc3QgY2h1cm49TWF0aC5tYXgoMCxzZy1zdmMpOwogIGNvbnN0IHJvd3M9RlVOTkVMLnJvd3MuZmlsdGVyKHI9PnJbMF0hPT0n7KCE7LK0JykubWFwKHI9Pntjb25zdCBzMT1yWzFdP3JbMl0vclsxXSoxMDA6MCxzMj1yWzJdP3JbM10vclsyXSoxMDA6MDsKICAgIHJldHVybiBgPHRyPjx0ZCBjbGFzcz0ibmFtZSI+JHtyWzBdfTwvdGQ+PHRkIGNsYXNzPSJkaW0iPiR7ZihyWzFdKX08L3RkPjx0ZD4ke2YoclsyXSl9IDxzcGFuIGNsYXNzPSJkaW0iPi8gJHtzMS50b0ZpeGVkKDApfSU8L3NwYW4+PC90ZD48dGQ+JHtmKHJbM10pfSA8c3BhbiBjbGFzcz0iZGltIj4vICR7czIudG9GaXhlZCgwKX0lPC9zcGFuPjwvdGQ+PC90cj5gO30pLmpvaW4oJycpOwogIHJldHVybiBgPGRpdiBjbGFzcz0iY2FyZCI+PGgzPuyLoOq3nOqzoOqwnSDtmZzshLHtmZQg7Y2864SQIDxzcGFuIGNsYXNzPSJtaW5pIj7qsIDsnoXsnpAg6riw7KSAIC8gJHtGVU5ORUwud2luZG93fSDsvZTtmLjtirg8L3NwYW4+PC9oMz4KICAgICR7YWNxfQogICAgPGRpdiBjbGFzcz0iY3N1YiIgc3R5bGU9Im1hcmdpbi1ib3R0b206NnB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS1pbmspIj7tmZzshLHtmZQgKOqwgOyehSAxMDAlIOq4sOykgCk8L2Rpdj4KICAgICR7YmFyc30KICAgIDxkaXYgY2xhc3M9ImNzdWIiIHN0eWxlPSJtYXJnaW46MTBweCAwIDJweCI+6rCA7J6FIO2bhCDshJzruYTsiqQg66+47J207JqpKOydtO2DiCkgPGIgc3R5bGU9ImNvbG9yOnZhcigtLWluaykiPiR7ZihjaHVybil966qFPC9iPiDigJQg6rCA7J6FICR7ZihzZyl966qFIOykkSAkeyhjaHVybi8oc2d8fDEpKjEwMCkudG9GaXhlZCgxKX0lPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjc3ViIiBzdHlsZT0ibWFyZ2luOjE0cHggMCA3cHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLWluaykiPuyxhOuEkOuzhCDsoITtmZggPHNwYW4gY2xhc3M9ImRpbSIgc3R5bGU9ImZvbnQtd2VpZ2h0OjQwMCI+KOu5hOq0keqzoCA9IOyYpOqwgOuLicK3Q1JNIOuTsSDruYTqtJHqs6Ag7Jyg7J6FKTwvc3Bhbj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNjcm9sbCI+PHRhYmxlIHN0eWxlPSJtaW4td2lkdGg6NDQwcHgiPjx0aGVhZD48dHI+PHRoPuyxhOuEkDwvdGg+PHRoPuyEpOy5mDwvdGg+PHRoPuqwgOyehTxicj48c3BhbiBjbGFzcz0iZGltIiBzdHlsZT0iZm9udC13ZWlnaHQ6NDAwIj7shKTsuZgg64yA67mEPC9zcGFuPjwvdGg+PHRoPuyEnOu5hOyKpCDsnbTsmqk8YnI+PHNwYW4gY2xhc3M9ImRpbSIgc3R5bGU9ImZvbnQtd2VpZ2h0OjQwMCI+6rCA7J6FIOuMgOu5hDwvc3Bhbj48L3RoPjwvdHI+PC90aGVhZD48dGJvZHk+JHtyb3dzfTwvdGJvZHk+PC90YWJsZT48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNzdWIiIHN0eWxlPSJtYXJnaW4tdG9wOjdweCI+7Iug6recIOqzoOqwnSA9IDxiPuqwgOyeheyekDwvYj4g6riw7KSAIC8g7ISc67mE7IqkIOydtOyaqSA9IOqwgOyeheyekCDspJEg6rCA7J6FIOuLueydvCDssqsg6rKw7KCcL+ydtOyaqShwdXJjaGFzZV9zdGFydGVkKSAvIOyEseyImSDsvZTtmLjtirg8L2Rpdj48L2Rpdj5gOwp9CmZ1bmN0aW9uIHJlbmRlckpvdXJuZXkoVyl7CiAgY29uc3QgcHI9Vy5wcmlvci5sZW5ndGg7CiAgY29uc3QgaW5zdD1zdW0oVE9UQUxTLmluc3RhbGx8fFtdLFcuY3VyKSwgc3U9c3VtKFRPVEFMUy5zdSxXLmN1cik7CiAgY29uc3QgaW5zdFA9cHI/c3VtKFRPVEFMUy5pbnN0YWxsfHxbXSxXLnByaW9yKTpudWxsLCBzdVA9cHI/c3VtKFRPVEFMUy5zdSxXLnByaW9yKTpudWxsOwogIGNvbnN0IGkycz1pbnN0P3N1L2luc3QqMTAwOjA7CiAgY29uc3QgZnVubmVsPWA8ZGl2IGNsYXNzPSJjYXJkIiBzdHlsZT0icGFkZGluZzowO292ZXJmbG93OmhpZGRlbjttYXJnaW4tYm90dG9tOjE2cHgiPjxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpzdHJldGNoO2ZsZXgtd3JhcDp3cmFwIj5gCiAgICArYDxkaXYgc3R5bGU9ImZsZXg6MTttaW4td2lkdGg6MTUwcHg7cGFkZGluZzoxN3B4IDIwcHgiPjxkaXYgY2xhc3M9InQiPuyVseyEpOy5mDwvZGl2PjxkaXYgY2xhc3M9ImJpZyI+JHtmKGluc3QpfTxzcGFuIGNsYXNzPSJ1Ij7rqoU8L3NwYW4+PC9kaXY+JHtkZWx0YVNwYW4oaW5zdCxpbnN0UCl9PC9kaXY+YAogICAgK2A8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO3BhZGRpbmc6MCAyMHB4O21pbi13aWR0aDoxMDRweDtib3JkZXItbGVmdDoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJpZ2h0OjFweCBzb2xpZCB2YXIoLS1saW5lKSI+PGRpdiBzdHlsZT0iZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tc3ViKTtmb250LXdlaWdodDo2MDA7bWFyZ2luLWJvdHRvbToycHgiPuyEpOy5mCDihpIg6rCA7J6FPC9kaXY+PGRpdiBzdHlsZT0iZm9udC1zaXplOjIwcHg7Zm9udC13ZWlnaHQ6ODAwO2NvbG9yOnZhcigtLWFjY2VudCk7bGV0dGVyLXNwYWNpbmc6LTAuNXB4Ij4ke2kycy50b0ZpeGVkKDEpfSU8L2Rpdj48L2Rpdj5gCiAgICArYDxkaXYgc3R5bGU9ImZsZXg6MTttaW4td2lkdGg6MTUwcHg7cGFkZGluZzoxN3B4IDIwcHg7YmFja2dyb3VuZDp2YXIoLS1hY2NlbnQtc29mdCkiPjxkaXYgY2xhc3M9InQiPu2ajOybkOqwgOyehTwvZGl2PjxkaXYgY2xhc3M9ImJpZyI+JHtmKHN1KX08c3BhbiBjbGFzcz0idSI+66qFPC9zcGFuPjwvZGl2PiR7ZGVsdGFTcGFuKHN1LHN1UCl9PC9kaXY+YAogICAgK2A8L2Rpdj48L2Rpdj5gOwogIHJldHVybiBgPGRpdiBzdHlsZT0iZm9udC1zaXplOjE4cHg7Zm9udC13ZWlnaHQ6ODAwO2xldHRlci1zcGFjaW5nOi0wLjRweDtjb2xvcjp2YXIoLS1pbmspO21hcmdpbjoycHggMCAxNHB4Ij7si6Dqt5zqs6DqsJ0g64u57J28IOyLpOyggTwvZGl2PmAKICAgICtmdW5uZWwrb25iQmxvY2soVykrcmVuZGVyRmlyc3RCdXkoVyk7Cn0KY29uc3QgU1ZDTD17dGF4aToi7YOd7IucIixydnRheGk6IuyYiOyVve2DneyLnCIsZXhwYnVzOiLqs6Dsho3Ct+yLnOyZuOuyhOyKpCIsYWlyYnVzOiLqs7Xtla3rsoTsiqQiLGtvcmFpbDoi6riw7LCoIixzY29vdDoi7YKl67O065OcIixiaWtlOiLsnpDsoITqsbAiLGFpcjoi7ZWt6rO1IixwYXNzX3Njb290OiLtgqXrs7Trk5wg7KCV6riw6raMIix0cmF2ZWw6IuyXrO2WiSIscmVuY2FyOiLroIzthLDsubQiLHBrZzoi7Yyo7YKk7KeAIn07CmZ1bmN0aW9uIHJlbmRlckZpcnN0QnV5KFcpewogIGNvbnN0IEZCPURBVEEuZmlyc3RidXl8fHt9LCBhY3RzPU9iamVjdC5rZXlzKEZCKTsKICBpZighYWN0cy5sZW5ndGgpIHJldHVybiAnJzsKICBjb25zdCByb3dzPWFjdHMubWFwKGE9Pih7bGFiZWw6U1ZDTFthXXx8YSx2OnN1bShGQlthXXx8W10sVy5jdXIpfSkpLmZpbHRlcihyPT5yLnY+MCkuc29ydCgoeCx5KT0+eS52LXgudik7CiAgaWYoIXJvd3MubGVuZ3RoKSByZXR1cm4gJyc7CiAgY29uc3QgdG90PXJvd3MucmVkdWNlKChzLHIpPT5zK3IudiwwKXx8MSwgbXg9cm93c1swXS52fHwxOwogIGNvbnN0IHN1PXN1bShUT1RBTFMuc3UsVy5jdXIpOwogIGNvbnN0IG9uYkZpcnN0PShEQVRBLm9uYiYmREFUQS5vbmJbMF0pP3N1bShEQVRBLm9uYlswXVsxXXx8W10sVy5jdXIpOjA7CiAgY29uc3QgY29udj1zdT9vbmJGaXJzdC9zdSoxMDA6MDsKICBjb25zdCBiYXJzPXJvd3MubWFwKHI9PmA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMHB4O3BhZGRpbmc6NXB4IDAiPmAKICAgICtgPGRpdiBzdHlsZT0id2lkdGg6MTAwcHg7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6NzAwO2ZsZXg6bm9uZSI+JHtyLmxhYmVsfTwvZGl2PmAKICAgICtgPGRpdiBzdHlsZT0iZmxleDoxO2JhY2tncm91bmQ6dmFyKC0tYmcpO2JvcmRlci1yYWRpdXM6NnB4O2hlaWdodDoyMHB4O3Bvc2l0aW9uOnJlbGF0aXZlO292ZXJmbG93OmhpZGRlbiI+PGRpdiBzdHlsZT0icG9zaXRpb246YWJzb2x1dGU7bGVmdDowO3RvcDowO2hlaWdodDoxMDAlO3dpZHRoOiR7KHIudi9teCoxMDApLnRvRml4ZWQoMSl9JTtiYWNrZ3JvdW5kOnZhcigtLWFjY2VudC1zb2Z0KSI+PC9kaXY+PC9kaXY+YAogICAgK2A8ZGl2IHN0eWxlPSJ3aWR0aDoxNDBweDt0ZXh0LWFsaWduOnJpZ2h0O2ZvbnQtdmFyaWFudC1udW1lcmljOnRhYnVsYXItbnVtcztmb250LXNpemU6MTNweCI+PGI+JHtmKHIudil9PC9iPiA8c3BhbiBjbGFzcz0iZGltIj4vICR7KHIudi90b3QqMTAwKS50b0ZpeGVkKDEpfSU8L3NwYW4+PC9kaXY+PC9kaXY+YCkuam9pbignJyk7CiAgY29uc3QgY29udkNhcmQ9YDxkaXYgY2xhc3M9ImNhcmQiIHN0eWxlPSJtYXJnaW4tdG9wOjE2cHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmJhc2VsaW5lO2dhcDoxMnB4O2ZsZXgtd3JhcDp3cmFwIj5gCiAgICArYDxkaXYgc3R5bGU9ImZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTVweCI+7J2067KIIOqwgOyeheyekCDsoITtmZg8L2Rpdj5gCiAgICArYDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToyMnB4O2ZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1hY2NlbnQpO2xldHRlci1zcGFjaW5nOi0wLjVweCI+JHtjb252LnRvRml4ZWQoMSl9JTwvZGl2PmAKICAgICtgPGRpdiBjbGFzcz0iY3N1YiIgc3R5bGU9Im1hcmdpbjowIj7si6Dqt5wg6rCA7J6FICR7ZihzdSl966qFIOykkSAke2Yob25iRmlyc3QpfeuqheydtCDqsIDsnoUg64u57J28IOuwlOuhnCDssqsg6rKw7KCcPC9kaXY+PC9kaXY+YDsKICBjb25zdCBkaXN0Q2FyZD1gPGRpdiBjbGFzcz0iY2FyZCIgc3R5bGU9Im1hcmdpbi10b3A6MTZweCI+PGgzPuyyqyDqsrDsoJwg7ISc67mE7IqkIOu2hO2PrCA8c3BhbiBjbGFzcz0ibWluaSI+7J20IOq4sOqwhOyXkCDsspjsnYwg6rKw7KCc7ZWcIOqzoOqwnSAke2YodG90KX3rqoXsnbQg7Ja065akIOyEnOu5hOyKpOuhnCDsi5zsnpHtlojripTsp4AgKOqwgOyehSDsi5zsoJDqs7wg66y06rSAKTwvc3Bhbj48L2gzPiR7YmFyc308L2Rpdj5gOwogIHJldHVybiBjb252Q2FyZCtkaXN0Q2FyZDsKfQpmdW5jdGlvbiByZW5kZXIoKXsKICBjb25zdCBXPXdpbmRvd3MoKTsKICBjb25zdCBjbXA9IVcucHJpb3IubGVuZ3RoPyLruYTqtZDquLDqsIQg7JeG7J2MIjooVy5jdXIubGVuZ3RoPT09MT8i7KCE7KO8IOuPmeyalOydvCDrjIDruYQiOiLsoITso7wg64+Z6riw6rCEIOuMgOu5hCIpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJyYW5nZWxhYmVsIikuaW5uZXJIVE1MPWA8Yj4ke0RBWVNbUkFdfSB+ICR7REFZU1tSQl19PC9iPiAvICR7Vy5jdXIubGVuZ3RofeydvCAvICR7Y21wfWA7CiAgY29uc3QgbW9kZVNlZz1gPGRpdiBjbGFzcz0ibW9kZXNlZyIgc3R5bGU9ImFsaWduLXNlbGY6ZmxleC1zdGFydCI+PGJ1dHRvbiBjbGFzcz0iJHtNT0RFPT09J2Fkcyc/J29uJzonJ30iIGRhdGEtbW9kZT0iYWRzIj7qtJHqs6Ag7Zqo7JyoPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iJHtNT0RFPT09J2pvdXJuZXknPydvbic6Jyd9IiBkYXRhLW1vZGU9ImpvdXJuZXkiPuyLoOq3nOqzoOqwnTwvYnV0dG9uPjxidXR0b24gY2xhc3M9IiR7TU9ERT09PSdsZXZ0Jz8nb24nOicnfSIgZGF0YS1tb2RlPSJsZXZ0Ij7rnbzsmrTsp4Ag7J2067Kk7Yq4PC9idXR0b24+PC9kaXY+YDsKICBpZihNT0RFPT09J2pvdXJuZXknKXsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ0YWJzIikuaW5uZXJIVE1MPW1vZGVTZWc7CiAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgidmlldyIpLmlubmVySFRNTD1yZW5kZXJKb3VybmV5KFcpOwogICAgcmV0dXJuOwogIH0KICBpZihNT0RFPT09J2xldnQnKXsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ0YWJzIikuaW5uZXJIVE1MPW1vZGVTZWc7CiAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgidmlldyIpLmlubmVySFRNTD1yZW5kZXJMb3VuZ2VFdmVudHMoKTsKICAgIHJldHVybjsKICB9CiAgY29uc3QgYWN0aXZlPUJVQ0tFVFMuZmlsdGVyKGI9PmJ1Y2tldEFjdGl2ZShiLmlkKSk7CiAgaWYoVEFCIT09InN1bW1hcnkiJiYhYWN0aXZlLnNvbWUoYj0+Yi5pZD09PVRBQikpVEFCPSJzdW1tYXJ5IjsKICBjb25zdCB0YWJzPVtbInN1bW1hcnkiLCLsmpTslb0iXV0uY29uY2F0KGFjdGl2ZS5tYXAoYj0+W2IuaWQsYi5sYWJlbF0pKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgidGFicyIpLmlubmVySFRNTD1tb2RlU2VnK2A8ZGl2IGNsYXNzPSJzdWJzZWciIHN0eWxlPSJhbGlnbi1zZWxmOmZsZXgtc3RhcnQiPmArdGFicy5tYXAodD0+YDxidXR0b24gY2xhc3M9InRhYiAke3RbMF09PT1UQUI/J29uJzonJ30iIGRhdGEtdD0iJHt0WzBdfSI+JHt0WzFdfTwvYnV0dG9uPmApLmpvaW4oJycpK2A8L2Rpdj5gOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ2aWV3IikuaW5uZXJIVE1MPShUQUI9PT0ic3VtbWFyeSIpP3JlbmRlclN1bW1hcnkoKTpyZW5kZXJCdWNrZXQoVEFCKTsKfQooZnVuY3Rpb24gaW5pdERhdGVzKCl7CiAgY29uc3QgZmQ9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoImZyb21EYXRlIiksIHRkPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ0b0RhdGUiKTsKICBmZC5taW49dGQubWluPURBWVNbMF07IGZkLm1heD10ZC5tYXg9REFZU1tEQVlTLmxlbmd0aC0xXTsKICBmZC52YWx1ZT1EQVlTW1JBXTsgdGQudmFsdWU9REFZU1tSQl07CiAgZnVuY3Rpb24gYXBwbHkoKXsKICAgIGxldCBhPURBWVMuaW5kZXhPZihmZC52YWx1ZSksIGI9REFZUy5pbmRleE9mKHRkLnZhbHVlKTsKICAgIGlmKGE8MClhPVJBOyBpZihiPDApYj1SQjsgaWYoYT5iKXtjb25zdCB0PWE7YT1iO2I9dDt9CiAgICBSQT1hOyBSQj1iOyBmZC52YWx1ZT1EQVlTW1JBXTsgdGQudmFsdWU9REFZU1tSQl07IHJlbmRlcigpOwogIH0KICBmZC5hZGRFdmVudExpc3RlbmVyKCJjaGFuZ2UiLGFwcGx5KTsgdGQuYWRkRXZlbnRMaXN0ZW5lcigiY2hhbmdlIixhcHBseSk7CiAgLy8g67mg66W4IOyEoO2DnTog7LWc6re8IDcvMTQvMzDsnbwKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgicXVpY2twaWNrIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGU9Pntjb25zdCBidG49ZS50YXJnZXQuY2xvc2VzdCgiYnV0dG9uIik7aWYoIWJ0bilyZXR1cm47Y29uc3QgZD0rYnRuLmRhdGFzZXQuZDtSQj1EQVlTLmxlbmd0aC0xO1JBPU1hdGgubWF4KDAsREFZUy5sZW5ndGgtZCk7ZmQudmFsdWU9REFZU1tSQV07dGQudmFsdWU9REFZU1tSQl07cmVuZGVyKCk7fSk7Cn0pKCk7CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ0YWJzIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGU9Pntjb25zdCBiPWUudGFyZ2V0LmNsb3Nlc3QoIi50YWIiKTtpZighYilyZXR1cm47VEFCPWIuZGF0YXNldC50O3JlbmRlcigpO30pOwpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgidGFicyIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixlPT57Y29uc3QgYj1lLnRhcmdldC5jbG9zZXN0KCJbZGF0YS1tb2RlXSIpO2lmKCFiKXJldHVybjtNT0RFPWIuZGF0YXNldC5tb2RlO3JlbmRlcigpO30pOwpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgidmlldyIpLmFkZEV2ZW50TGlzdGVuZXIoImNoYW5nZSIsZT0+e2NvbnN0IHM9ZS50YXJnZXQuY2xvc2VzdCgiI2RyaWxsc2VsIik7aWYoIXMpcmV0dXJuO0RSSUxMTUVUUklDPXMudmFsdWU7cmVuZGVyKCk7fSk7CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ2aWV3IikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGU9Pntjb25zdCBiPWUudGFyZ2V0LmNsb3Nlc3QoIltkYXRhLWRleHBdIik7aWYoIWIpcmV0dXJuO2NvbnN0IGs9ZGVjb2RlVVJJQ29tcG9uZW50KGIuZGF0YXNldC5kZXhwKTtERVhQLmhhcyhrKT9ERVhQLmRlbGV0ZShrKTpERVhQLmFkZChrKTtyZW5kZXIoKTt9KTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInZpZXciKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsZT0+e2NvbnN0IGI9ZS50YXJnZXQuY2xvc2VzdCgiW2RhdGEta21dIik7aWYoIWIpcmV0dXJuO0RSSUxMTUVUUklDPWIuZGF0YXNldC5rbTtyZW5kZXIoKTt9KTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInZpZXciKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsZT0+e2NvbnN0IGI9ZS50YXJnZXQuY2xvc2VzdCgiW2RhdGEtbGV3XSIpO2lmKGIpe0xFV0lOPWIuZGF0YXNldC5sZXc7cmVuZGVyKCk7cmV0dXJuO31jb25zdCBzPWUudGFyZ2V0LmNsb3Nlc3QoIltkYXRhLWxlc29ydF0iKTtpZihzKXtMRVNPUlQ9cy5kYXRhc2V0Lmxlc29ydDtyZW5kZXIoKTt9fSk7CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJ2aWV3IikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGU9Pntjb25zdCBiPWUudGFyZ2V0LmNsb3Nlc3QoIltkYXRhLWJ0cmVuZF0iKTtpZighYilyZXR1cm47QlRSSz1iLmRhdGFzZXQuYnRyZW5kO3JlbmRlcigpO30pOwpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgidmlldyIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixlPT57Y29uc3QgYj1lLnRhcmdldC5jbG9zZXN0KCJbZGF0YS1sZ2tdIik7aWYoIWIpcmV0dXJuO0xHSz1iLmRhdGFzZXQubGdrO3JlbmRlcigpO30pOwpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgidmlldyIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixlPT57Y29uc3QgYj1lLnRhcmdldC5jbG9zZXN0KCJbZGF0YS10cmVuZF0iKTtpZighYilyZXR1cm47VFJFTkRLRVk9Yi5kYXRhc2V0LnRyZW5kO3JlbmRlcigpO30pOwpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgidmlldyIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixlPT57Y29uc3QgYj1lLnRhcmdldC5jbG9zZXN0KCJbZGF0YS1jcl0iKTtpZighYilyZXR1cm47Q1JTSE9XW2IuZGF0YXNldC5jcl09IUNSU0hPV1tiLmRhdGFzZXQuY3JdO3JlbmRlcigpO30pOwpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgiZm9vdCIpLmlubmVySFRNTD0iIjsKY29uc3QgX2dtPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJnZW5tZXRhIik7IGlmKF9nbSkgX2dtLnRleHRDb250ZW50PURBVEEuZ2VuZXJhdGVkfHwiIjsKcmVuZGVyKCk7Cjwvc2NyaXB0Pgo8L2JvZHk+CjwvaHRtbD4K").decode("utf-8")


if __name__=="__main__":
    main()
