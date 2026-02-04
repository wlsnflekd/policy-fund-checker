import json
import re
import sqlite3
from datetime import datetime

import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

# =========================================================
# 기본 설정
# =========================================================
st.set_page_config(page_title="정책자금 조건 체크", page_icon="✅", layout="centered")
DB_PATH = "leads.db"

# =========================================================
# UI 가독성 개선 CSS (드롭다운/입력칸)
# =========================================================
st.markdown("""
<style>
/* 입력 박스(텍스트 / 숫자) */
div[data-baseweb="input"] > div {
    background-color: rgba(255,255,255,0.12) !important;
    border: 1px solid rgba(255,255,255,0.35) !important;
    border-radius: 8px;
}

/* selectbox(드롭다운) 본체 */
div[data-baseweb="select"] > div {
    background-color: rgba(255,255,255,0.12) !important;
    border: 1px solid rgba(255,255,255,0.35) !important;
    border-radius: 8px;
}

/* selectbox 펼쳐졌을 때 옵션 리스트 박스 */
div[data-baseweb="popover"] {
    background-color: rgba(18,18,18,0.98) !important;
    border: 1px solid rgba(255,255,255,0.25) !important;
    border-radius: 8px;
}

/* 옵션 hover */
div[data-baseweb="menu"] div[role="option"]:hover {
    background-color: rgba(255,255,255,0.12) !important;
}

/* 라벨 글씨 */
label,
.stTextInput label,
.stSelectbox label,
.stRadio label {
    color: rgba(255,255,255,0.95) !important;
    font-weight: 500;
}
</style>
""", unsafe_allow_html=True)


# =========================================================
# DB 유틸 (자동 스키마 보정)
# =========================================================
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def table_exists(conn, name: str) -> bool:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None


def get_columns(conn, table: str) -> set:
    cols = set()
    for row in conn.execute(f"PRAGMA table_info({table})"):
        cols.add(row[1])
    return cols


def ensure_db_schema():
    """
    과거에 만든 leads 테이블(예: phone NOT NULL 등)이 있어도 최대한 호환되도록 컬럼을 보정.
    SQLite는 NOT NULL 변경이 까다로워서 'phone'이 있으면 반드시 값 넣는 방향으로 처리.
    """
    with get_conn() as conn:
        if not table_exists(conn, "leads"):
            conn.execute("""
            CREATE TABLE leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,

                customer_name TEXT NOT NULL,

                phone TEXT NOT NULL,              -- 구형 스키마 호환용(필수)
                phone_digits TEXT,
                phone_formatted TEXT,

                company_name TEXT,
                biz_type TEXT,
                industry TEXT,

                business_years TEXT,
                business_months INTEGER,

                monthly_sales_raw TEXT,
                monthly_sales_formatted TEXT,

                tax_status TEXT,                  -- '완납' or '체납'

                broker_check_json TEXT,           -- 체크리스트 결과 JSON
                broker_is_risky INTEGER,          -- 1/0

                result_json TEXT NOT NULL
            )
            """)
            conn.commit()
            return

        existing = get_columns(conn, "leads")
        needed = {
            "created_at": "TEXT",
            "customer_name": "TEXT",
            "phone": "TEXT",  # 구형 호환 핵심
            "phone_digits": "TEXT",
            "phone_formatted": "TEXT",
            "company_name": "TEXT",
            "biz_type": "TEXT",
            "industry": "TEXT",
            "business_years": "TEXT",
            "business_months": "INTEGER",
            "monthly_sales_raw": "TEXT",
            "monthly_sales_formatted": "TEXT",
            "tax_status": "TEXT",
            "broker_check_json": "TEXT",
            "broker_is_risky": "INTEGER",
            "result_json": "TEXT",
        }
        for col, ctype in needed.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {ctype}")
        conn.commit()


ensure_db_schema()


# =========================================================
# 포맷/검증 유틸
# =========================================================
def only_digits(s: str) -> str:
    return re.sub(r"[^0-9]", "", s or "")


def format_phone_korea(raw: str) -> str:
    d = only_digits(raw)
    if len(d) == 11:
        return f"{d[0:3]}-{d[3:7]}-{d[7:11]}"
    if len(d) == 10:
        return f"{d[0:3]}-{d[3:6]}-{d[6:10]}"
    return raw


def is_valid_phone_korea(raw: str) -> bool:
    d = only_digits(raw)
    return len(d) in (10, 11) and d.startswith(("010", "011", "016", "017", "018", "019"))


def format_sales_manwon(raw: str) -> str:
    d = only_digits(raw)
    if not d:
        return ""
    try:
        n = int(d)
        return f"{n:,}만원"
    except:
        return f"{d}만원"


def parse_monthly_sales_to_manwon(raw: str) -> int:
    """입력값에서 숫자만 추출하여 '만원' 단위 정수로 변환. 예: '3,000만원' -> 3000"""
    d = only_digits(raw)
    return int(d) if d else 0


def business_years_to_months(business_years: str) -> int:
    if business_years == "1년 미만":
        return 6
    if business_years == "1~3년":
        return 24
    return 48


# =========================================================
# A/B/C 등급 + 고객용 요약
# =========================================================
def grade_label(g: str) -> str:
    return {"A": "A 적합", "B": "B 보완필요", "C": "C 불가"}.get(g, "B 보완필요")


def grade_summary(g: str) -> str:
    if g == "A":
        return "기본 요건 충족으로 접수 진행 가능합니다."
    if g == "B":
        return "일부 요건 보완이 필요해 담당자와 상담 후 진행 권장드립니다."
    return "현재 진행이 어려운 사유가 있어 담당자와 상담 후 진행 권장드립니다."


def grade_fund(eligible_status: str, business_months: int, monthly_sales_manwon: int, tax_status: str) -> str:
    """
    C: 세금체납
    A: (fund 룰 통과) + 업력 12개월 이상 + 월매출 1000만원 이상
    B: 그 외
    """
    if tax_status == "체납":
        return "C"
    if eligible_status == "가능" and business_months >= 12 and monthly_sales_manwon >= 1000:
        return "A"
    return "B"
def calc_final_grade(business_years: str, monthly_sales_manwon: int, tax_status: str) -> str:
    """
    최종 정책자금 판정 (자금명 무관, 1개 등급만)
    C: 세금체납=체납 (업력/매출 무관)
    A: 완납 + 업력(1~3년 또는 3년 이상) + 평균월매출 1000만원 '초과'
    B: 완납 + 그 외(업력 1년 미만이거나, 월매출 1000만원 이하 등)
    """
    if tax_status == "체납":
        return "C"

    if business_years in ["1~3년", "3년 이상"] and monthly_sales_manwon > 1000:
        return "A"

    return "B"



# =========================================================
# Streamlit on_change 콜백(입력 즉시 포맷)
# =========================================================
def on_phone_change():
    st.session_state["phone_input"] = format_phone_korea(st.session_state.get("phone_input", ""))


def on_sales_change():
    raw = st.session_state.get("sales_input", "")
    formatted = format_sales_manwon(raw)
    if formatted:
        st.session_state["sales_input"] = formatted


# =========================================================
# 정책자금 로드 / 판정 로직
# =========================================================
@st.cache_data
def load_funds():
    with open("funds.json", "r", encoding="utf-8") as f:
        return json.load(f)


funds = load_funds()


def check_fund(profile, fund):
    reasons = []
    el = fund.get("eligibility", {})

    if profile["business_months"] < el.get("min_business_months", 0):
        reasons.append("업력 요건 미달")

    allowed_types = el.get("allowed_business_types", [])
    if allowed_types and profile["biz_type"] not in allowed_types:
        reasons.append("사업자 유형 불일치")

    allowed_ind = el.get("allowed_industries", [])
    if allowed_ind and profile["industry"] not in allowed_ind:
        reasons.append("업종 요건 불일치")

    for ex in fund.get("exclusions", []):
        field = ex.get("field")
        value = ex.get("value")
        reason = ex.get("reason", "제외 조건")
        if profile.get(field) == value:
            reasons.append(reason)

    if reasons:
        return "불가", reasons
    return "가능", []


# =========================================================
# 세션 상태 초기화
# =========================================================
st.session_state.setdefault("step", 1)
st.session_state.setdefault("phone_input", "")
st.session_state.setdefault("sales_input", "")
st.session_state.setdefault("step1_data", {})
st.session_state.setdefault("broker_checks", {})

# =========================================================
# UI
# =========================================================
st.title("정책자금 조건 체크")
st.caption("기본진단 및 적합판정을 위해 정확히 입력해주세요. ")

# =========================================================
# STEP 1 - 기본 정보 입력
# =========================================================
if st.session_state.step == 1:
    st.subheader("귀하의 정보")
    customer_name = st.text_input(
        "성함",
        placeholder="홍길동",
        value=st.session_state.step1_data.get("customer_name", ""),
    )

    phone_raw = st.text_input(
        "전화번호",
        placeholder="01012341234",
        key="phone_input",
        on_change=on_phone_change,
    )

    st.subheader("사업자 정보")
    company_name = st.text_input(
        "상호명",
        placeholder="예) OO푸드",
        value=st.session_state.step1_data.get("company_name", ""),
    )

    biz_type = st.selectbox(
        "사업자유형",
        ["선택", "개인", "법인"],
        index=0 if st.session_state.step1_data.get("biz_type", "선택") == "선택" else 1,
    )

    industry_list = ["선택", "음식점", "제조", "도소매", "서비스", "기타"]
    prev_industry = st.session_state.step1_data.get("industry", "선택")
    industry = st.selectbox(
        "업종",
        industry_list,
        index=industry_list.index(prev_industry) if prev_industry in industry_list else 0,
    )

    business_years_list = ["1년 미만", "1~3년", "3년 이상"]
    prev_years = st.session_state.step1_data.get("business_years", "1년 미만")
    business_years = st.radio(
        "업력(사업자등록 기준으로 선택)",
        business_years_list,
        index=business_years_list.index(prev_years) if prev_years in business_years_list else 0,
        horizontal=True,
    )

    monthly_sales = st.text_input(
        "평균월매출",
        placeholder="예) 3000",
        key="sales_input",
        on_change=on_sales_change,
    )

    st.markdown("### 추가 확인 (모르면 그대로 두세요)")
    tax_status_list = ["완납", "체납"]
    prev_tax = st.session_state.step1_data.get("tax_status", "완납")
    tax_status = st.radio(
        "현재 세금체납이 있으신가요?",
        tax_status_list,
        index=tax_status_list.index(prev_tax) if prev_tax in tax_status_list else 0,
        horizontal=True,
    )
    st.caption("국세 확인: 국세청/홈택스  |  지방세 확인: 위택스")

    st.divider()

    if st.button("다음 ▶", use_container_width=True):
        if not customer_name.strip():
            st.error("성함을 입력해주세요.")
            st.stop()

        if not is_valid_phone_korea(phone_raw):
            st.error("전화번호를 확인해주세요. (예: 01012341234)")
            st.stop()

        # 매출 입력 검증
        if monthly_sales.strip() and not only_digits(monthly_sales):
            if parse_monthly_sales_to_manwon(monthly_sales) == 0:
                st.error("평균월매출은 숫자만 입력해주세요. (예: 3000)")
                st.stop()

        phone_digits = only_digits(phone_raw)
        phone_formatted = format_phone_korea(phone_digits)

        sales_raw = (st.session_state.get("sales_input", "") or "").strip()
        sales_formatted = (
            format_sales_manwon(sales_raw) if parse_monthly_sales_to_manwon(sales_raw) else ""
        )

        st.session_state.step1_data = {
            "customer_name": customer_name.strip(),
            "phone_digits": phone_digits,
            "phone_formatted": phone_formatted,
            "company_name": company_name.strip(),
            "biz_type": biz_type,
            "industry": industry,
            "business_years": business_years,
            "business_months": business_years_to_months(business_years),
            "monthly_sales_raw": sales_raw,
            "monthly_sales_formatted": sales_formatted,
            "tax_status": tax_status,
        }

        st.session_state.step = 2
        st.rerun()
        st.stop()

    # ✅ STEP1 화면에서는 여기서 종료 (아래 STEP2 실행 방지)
    st.stop()


# =========================================================
# STEP 2 - 불법브로커 자가진단 체크리스트 + 최종 판정/접수
# =========================================================
if st.session_state.step == 2:
    # STEP2 진입 시 상단으로 스크롤
    st.markdown("<div id='top'></div>", unsafe_allow_html=True)
    st.markdown(
        """
        <script>
          setTimeout(function() {
            const el = document.getElementById('top');
            if (el) el.scrollIntoView({behavior: 'auto'});
          }, 50);
        </script>
        """,
        unsafe_allow_html=True,
    )

    st.subheader("불법 브로커(제3자 부당개입) 자가진단 체크리스트")
    st.caption("아래 항목 중 경험했거나 권유받은 적이 있다면 [예]를 선택해주세요.")

    default_checks = {"q1": "아니오", "q2": "아니오", "q3": "아니오", "q4": "아니오", "q5": "아니오", "q6": "아니오"}
    for k, v in default_checks.items():
        st.session_state.broker_checks.setdefault(k, v)

    def card_question(num: int, key: str, text: str):
        with st.container(border=True):
            st.markdown(f"**{num}. {text}**")
            st.session_state.broker_checks[key] = st.radio(
                "선택",
                ["아니오", "예"],
                index=0 if st.session_state.broker_checks[key] == "아니오" else 1,
                horizontal=True,
                key=f"{key}_radio",
                label_visibility="collapsed",
            )

    card_question(1, "q1", "보험계약을 조건으로 정책자금 신청 대행을 약속한 경우")
    card_question(2, "q2", "재무제표 분식 · 허위 사업계획으로 대출을 진행한 경우")
    card_question(3, "q3", "자격 미달 기업에 대출을 사전 약속하고 대가를 요구한 경우")
    card_question(4, "q4", "정부 · 공공기관 직원 명함 또는 신분을 사칭한 경우")
    card_question(5, "q5", "인맥 · 청탁으로 정책자금이 가능하다며 착수금을 요구한 경우")
    card_question(6, "q6", "성공 조건 계약 후 대출 실패에도 수수료를 반환하지 않은 경우")

    checked_yes = [k for k, v in st.session_state.broker_checks.items() if v == "예"]

    # =========================
    # 자가진단 결과 (원래 위치)
    # =========================
    st.subheader("자가진단 결과")

    if checked_yes:
        st.error("⚠️ 체크된 항목이 있습니다.")
        st.write("• 제3자 부당개입(불법 브로커) 유형에 해당할 수 있습니다.")
        st.write("• 정책자금 진행 시 각별한 주의가 필요합니다.")
    else:
        st.success("✅ 체크된 항목이 없습니다.")
        st.write("• 정상적인 컨설팅 범위에 해당합니다.")
        st.write("• (신청 방법 안내 · 자금 적합성 상담)")

    st.divider()

    # =========================
    # STEP2 버튼
    # =========================
    col1, col2 = st.columns(2)
    with col1:
        if st.button("◀ 이전", use_container_width=True):
            st.session_state.step = 1
            st.rerun()
            st.stop()

    with col2:
        do_submit = st.button("최종 판정 & 접수", use_container_width=True)

    # =========================
    # 최종 판정 결과 + DB 저장
    # =========================
    if do_submit:
        if not st.session_state.step1_data:
            st.error("STEP 1 정보가 없습니다. 이전으로 돌아가 다시 입력해주세요.")
            st.stop()

        s1 = st.session_state.step1_data

        business_years = s1.get("business_years", "1년 미만")
        tax_status = s1.get("tax_status", "완납")

        sales_raw = (s1.get("monthly_sales_raw") or "").strip()
        monthly_sales_manwon = parse_monthly_sales_to_manwon(sales_raw)

        final_grade = calc_final_grade(business_years, monthly_sales_manwon, tax_status)

        st.divider()
        st.subheader("정책자금 판정 결과")
        st.write(f"정책자금 판정 : {grade_label(final_grade)}")
        st.info(f"요약: {grade_summary(final_grade)}")

        # DB 저장(접수)
        broker_payload = {"answers": st.session_state.broker_checks, "checked_yes": checked_yes}
        broker_is_risky = 1 if checked_yes else 0

        # ✅ result_json은 '최종등급' 중심으로 저장 (results 변수 없어서 에러 방지)
        result_payload = {
            "final_grade": final_grade,
            "final_label": grade_label(final_grade),
            "summary": grade_summary(final_grade),
        }

        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO leads (
                    created_at, customer_name, phone, phone_digits, phone_formatted,
                    company_name, biz_type, industry, business_years, business_months,
                    monthly_sales_raw, monthly_sales_formatted, tax_status,
                    broker_check_json, broker_is_risky, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    s1.get("customer_name", ""),
                    s1.get("phone_formatted", ""),  # phone NOT NULL 호환
                    s1.get("phone_digits", ""),
                    s1.get("phone_formatted", ""),
                    s1.get("company_name", ""),
                    s1.get("biz_type", ""),
                    s1.get("industry", ""),
                    s1.get("business_years", ""),
                    int(s1.get("business_months", 0)),
                    s1.get("monthly_sales_raw", ""),
                    s1.get("monthly_sales_formatted", ""),
                    s1.get("tax_status", ""),
                    json.dumps(broker_payload, ensure_ascii=False),
                    broker_is_risky,
                    json.dumps(result_payload, ensure_ascii=False),
                ),
            )
            conn.commit()

        st.success("접수 기록이 저장되었습니다.")

    # ✅ STEP2 끝나면 아래 실행 방지
    st.stop()

