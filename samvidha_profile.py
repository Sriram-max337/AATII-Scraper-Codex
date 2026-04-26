import re
import os
import json
import subprocess
import tempfile
import time
from getpass import getpass
from html import escape
from pathlib import Path

import requests
from bs4 import BeautifulSoup


def load_env_file(path=".env"):
    env_path = Path(path)
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_env_file()


BASE_URL = "https://samvidha.iare.ac.in/"
LOGIN_URL = "https://samvidha.iare.ac.in/pages/login/checkUser.php"
HOME_URL = "https://samvidha.iare.ac.in/home"
PROFILE_URL = "https://samvidha.iare.ac.in/home?action=profile"
AAT_URL = "https://samvidha.iare.ac.in/home?action=upload_aat_2"
AAT_QUESTION_URL = "https://samvidha.iare.ac.in/pages/student/ajax/aatupload.php"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS = [
    "openai/gpt-4o-mini",
    "mistralai/mixtral-8x7b-instruct",
]
NOT_FOUND = "Not Found"
NO_QUESTION = "No question available"
NO_QUESTION_ASSIGNED = "No question assigned yet"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}


def clean_text(text):
    return re.sub(r"\s+", " ", text).strip() if text else NOT_FOUND


def safe_filename(value):
    value = str(value).strip().replace(" ", "-")
    return re.sub(r"[^A-Za-z0-9_.-]", "", value)


def normalize_label(text):
    return re.sub(r"[^a-z0-9]+", "", text.lower()) if text else ""


def label_matches(text, expected):
    return normalize_label(text) == normalize_label(expected)


def is_login_page(response, soup=None):
    if "Samvidha - Campus Management Portal - IARE" in response.text:
        return True

    soup = soup or BeautifulSoup(response.text, "html.parser")
    page_text = soup.get_text(" ", strip=True).lower()

    return (
        "login" in response.url.lower()
        or soup.find("input", {"type": "password"}) is not None
        or ("login" in page_text and "password" in page_text)
    )


def login(username, password):
    session = requests.Session()
    session.headers.update(HEADERS)

    session.get(BASE_URL, timeout=15).raise_for_status()

    response = session.post(
        LOGIN_URL,
        data={"username": username, "password": password},
        headers={
            **HEADERS,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE_URL,
        },
        timeout=15,
    )
    response.raise_for_status()

    home_response = session.get(HOME_URL, timeout=15)
    home_response.raise_for_status()

    if "Dashboard" not in home_response.text:
        print("Login failed")
        return None

    return session


def extract_profile_value(soup, possible_labels):
    for label in possible_labels:
        dt = soup.find("dt", string=lambda text: label_matches(text, label))
        if dt:
            dd = dt.find_next_sibling("dd")
            if dd:
                return clean_text(dd.get_text(" ", strip=True))

        strong = soup.find("strong", string=lambda text: label_matches(text, label))
        if strong:
            paragraph = strong.find_next_sibling("p")
            if paragraph:
                return clean_text(paragraph.get_text(" ", strip=True))

    return NOT_FOUND


def get_profile_data(session):
    response = session.get(PROFILE_URL, timeout=15)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    if is_login_page(response, soup):
        return "Session expired"

    return {
        "name": extract_profile_value(soup, ["Name", "Student Name"]),
        "roll_number": extract_profile_value(soup, ["Roll Number", "Roll No", "Roll No."]),
        "branch": extract_profile_value(soup, ["Branch", "Department"]),
        "year_sem": extract_profile_value(
            soup,
            ["Year/Sem", "Year / Sem", "Year/Semester", "Year / Semester"],
        ),
        "section": extract_profile_value(soup, ["Section"]),
    }


def get_aat_subjects(session):
    response = session.get(AAT_URL, timeout=15)
    response.raise_for_status()

    if is_login_page(response):
        return "Session expired"

    soup = BeautifulSoup(response.text, "html.parser")
    table = soup.find("table", class_="table-bordered")
    if not table:
        return []

    subjects = []
    header_cells = table.find("tr").find_all(["th", "td"]) if table.find("tr") else []
    headers = [normalize_label(cell.get_text(" ", strip=True)) for cell in header_cells]

    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        values = [clean_text(cell.get_text(" ", strip=True)) for cell in cells]
        button = row.find("button")
        course_code = get_table_value(values, headers, ["coursecode", "code"], 0)
        course_name = get_table_value(values, headers, ["coursename", "subjectname", "name"], 1)
        semester = get_table_value(values, headers, ["semester", "sem"], 2)
        aat_type = get_table_value(values, headers, ["assignmenttype", "aattype", "type"], 3)

        if len(values) >= 5 and values[0].isdigit() and course_code == values[0]:
            course_code = values[1]
            course_name = values[2]
            semester = values[3]
            aat_type = values[4]

        subject = {
            "course_code": course_code,
            "course_name": course_name,
            "semester": semester,
            "aat_type": aat_type,
            "sub_code": button.get("data-sub_code", NOT_FOUND) if button else NOT_FOUND,
            "sem": button.get("data-sem", NOT_FOUND) if button else NOT_FOUND,
            "ay": button.get("data-ay", NOT_FOUND) if button else NOT_FOUND,
            "dept": button.get("data-dept", NOT_FOUND) if button else NOT_FOUND,
            "aat_type_param": button.get("data-aat_type", NOT_FOUND) if button else NOT_FOUND,
        }
        subject["question"] = get_aat_question(session, subject)

        subjects.append(subject)

    return subjects


def get_aat_question(session, subject):
    required_fields = ["sub_code", "sem", "ay", "dept", "aat_type_param"]
    for field in required_fields:
        value = subject.get(field, "")
        if not value or value == NOT_FOUND:
            return NO_QUESTION

    payload = {
        "sub_code": subject["sub_code"],
        "dept_id": subject["dept"],
        "sem": subject["sem"],
        "ay": subject["ay"],
        "aat_type": subject["aat_type_param"],
        "last_date": "2026-04-28",
        "action": "get_aat_question",
    }
    print("Payload:", payload)

    try:
        response = session.post(
            AAT_QUESTION_URL,
            data=payload,
            headers={
                **HEADERS,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": AAT_URL,
            },
            timeout=15,
        )
        print(response.text[:200])
        response.raise_for_status()
    except requests.RequestException:
        print(f"Invalid POST response for subject: {subject['course_code']}")
        return NO_QUESTION

    if not response.text.strip() or len(response.text.strip()) < 50:
        print(f"Invalid POST response for subject: {subject['course_code']}")
        return NO_QUESTION

    soup = BeautifulSoup(response.text, "html.parser")
    question_items = [
        clean_text(item.get_text(" ", strip=True))
        for item in soup.find_all("li")
    ]
    question_items = [item for item in question_items if item != NOT_FOUND]
    if question_items:
        return " ".join(question_items)

    if soup.find("table"):
        return NO_QUESTION_ASSIGNED

    return NO_QUESTION


def get_table_value(values, headers, possible_headers, fallback_index):
    for header in possible_headers:
        if header in headers:
            index = headers.index(header)
            if index < len(values):
                return values[index]

    return values[fallback_index] if fallback_index < len(values) else NOT_FOUND


def display_profile(profile):
    print("\nProfile Data")
    print(f"Name: {profile['name']}")
    print(f"Roll No: {profile['roll_number']}")
    print(f"Branch: {profile['branch']}")
    print(f"Year/Sem: {profile['year_sem']}")
    print(f"Section: {profile['section']}")


def display_assignments(subjects):
    print("\nAssignments")
    if not subjects:
        print("No assignments found")
        return

    for subject in subjects:
        print(
            f"{subject['course_name']} "
            f"({subject['course_code']}) - "
            f"{subject['aat_type']}\n"
            f"Q: {subject['question']}"
        )


def normalize_llm_subject(subject):
    return {
        "sub_code": subject.get("sub_code", subject.get("course_code", NOT_FOUND)),
        "subject_name": subject.get("subject_name", subject.get("course_name", NOT_FOUND)),
        "aat_type": subject.get("aat_type", NOT_FOUND),
        "question": subject.get("question", ""),
    }


def clean_questions(questions):
    cleaned = []
    for question in questions:
        question = clean_text(str(question)).strip(" -:")
        if question and question != NOT_FOUND:
            cleaned.append(question)
    return cleaned[:10]


def split_assignment_questions(text):
    questions = re.split(r"(?<=[?.])\s+(?=[A-Z])", text)
    questions = [question.strip() for question in questions if question.strip()]
    return questions


def preprocess_questions(subject):
    subject = normalize_llm_subject(subject)
    raw_text = str(subject.get("question", "")).strip()
    if not raw_text or raw_text in {NO_QUESTION, NO_QUESTION_ASSIGNED, NOT_FOUND}:
        return {"topic": "", "questions": []}

    if subject["aat_type"].upper() == "CS-II":
        parts = re.split(r"Questions\s+for\s+Analysis", raw_text, maxsplit=1, flags=re.IGNORECASE)
        topic = clean_text(parts[0]) if parts else ""
        question_block = parts[1] if len(parts) > 1 else raw_text
        questions = re.split(r"\d+\.\s*", question_block)
        return {"topic": topic, "questions": clean_questions(questions)}

    questions = split_assignment_questions(raw_text)
    print(f"Detected {len(questions)} questions")

    if len(questions) < 10:
        print("Warning: Less than 10 questions detected")
        return {"topic": "", "questions": []}

    if len(questions) > 10:
        questions = questions[:10]

    print("Proceeding with 10 questions")
    return {"topic": "", "questions": questions}


def build_system_prompt(aat_type):
    if aat_type.upper() == "CS-II":
        return """You are a 2nd-year engineering student writing a case study assignment.

You are given:
- A topic
- N questions
You are given EXACTLY N questions.
You MUST return EXACTLY N answers.

Instructions:
1. Generate:
   Title
   Introduction (5-6 lines)
   Case Background (5-6 lines)
2. Then answer ALL questions.
3. Each answer must:
   - Be 7-10 lines
   - Be clear and explanatory
   - Include reasoning/examples

STRICT RULES:
- You MUST generate exactly N answers
- Do NOT skip questions
- Do NOT merge questions
- Do NOT repeat questions

Return ONLY JSON:
{
  "title": "...",
  "introduction": "...",
  "background": "...",
  "answers": ["...", "..."]
}"""

    return """You are a 2nd-year engineering student writing assignment answers.

You are given EXACTLY 10 questions.
You MUST return EXACTLY 10 answers.

Rules:
- Do NOT skip any question
- Do NOT merge questions
- Each answer must be 8-12 lines
- Keep answers clear and moderately detailed

STRICT RULES:
- Return exactly 10 answers
- Do NOT return fewer than 10 answers
- Do NOT return more than 10 answers
- Keep tone natural

Return ONLY JSON:
{
  "answers": ["A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9", "A10"]
}"""


def build_user_content(subject, processed):
    questions_text = "\n".join(
        f"{index}. {question}"
        for index, question in enumerate(processed["questions"], start=1)
    )

    if subject["aat_type"].upper() == "CS-II":
        return f"""Subject: {subject["subject_name"]} ({subject["sub_code"]})
Assignment Type: {subject["aat_type"]}
Question Count: {len(processed["questions"])}

Topic:
{processed["topic"]}

Numbered Questions:
{questions_text}"""

    return f"""Subject: {subject["subject_name"]} ({subject["sub_code"]})
Assignment Type: {subject["aat_type"]}
Question Count: 10

Numbered Questions:
{questions_text}

You MUST return exactly 10 answers."""


def model_supports_json_object(model):
    return model == "openai/gpt-4o-mini" or model.startswith("anthropic/")


def build_payload(model, system_prompt, user_prompt):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
    }

    if model_supports_json_object(model):
        payload["response_format"] = {"type": "json_object"}

    return payload


def valid_answer_result(result, question_count):
    answers = result.get("answers", []) if isinstance(result, dict) else []
    return (
        len(answers) == question_count
        and all(str(answer).strip() for answer in answers)
    )


def call_llm_with_fallback(subject, prompt_data):
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY not set; skipping LLM answer generation.")
        return None
    if api_key == "your_openrouter_api_key_here":
        print("OPENROUTER_API_KEY is still the placeholder value in .env.")
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost",
        "X-Title": "IARE AAT Assignment Generator",
    }

    for model_name in MODELS:
        print(f"Trying model: {model_name}")
        payload = build_payload(
            model_name,
            prompt_data["system_prompt"],
            prompt_data["user_prompt"],
        )

        for attempt in range(2):
            try:
                response = requests.post(
                    OPENROUTER_URL,
                    headers=headers,
                    json=payload,
                    timeout=90,
                )

                if response.status_code == 429 and attempt == 0:
                    print(f"Rate limited on {model_name}; waiting before retry")
                    time.sleep(3)
                    continue

                if response.status_code != 200:
                    print(f"OpenRouter status: {response.status_code}")
                    print(f"OpenRouter response: {response.text[:500]}")
                    break

                print("LLM response received")
                result = parse_llm_response(response.json(), prompt_data["question_count"])
                if "raw_text" in result:
                    print("Parsing failed")
                    if attempt == 0:
                        continue
                    break

                if valid_answer_result(result, prompt_data["question_count"]):
                    print(f"Success with {model_name}")
                    print("Parsing successful")
                    return result

                if prompt_data.get("assignment_type") == "ASSIGNMENT-II":
                    print("Answer count mismatch, retrying...")
                else:
                    print("Mismatch detected, retrying...")
                if attempt == 0:
                    continue
                break
            except requests.RequestException as exc:
                print(f"API request failed with {model_name}: {exc}")
                if attempt == 0:
                    continue
                break

        print("Switching model...")

    return None


def generate_answers(subject):
    subject = normalize_llm_subject(subject)
    processed = preprocess_questions(subject)
    if not processed["questions"]:
        return None

    print(f"Generating answers for {subject['sub_code']}")
    question_count = 10 if subject["aat_type"].upper() == "ASSIGNMENT-II" else len(processed["questions"])
    prompt_data = {
        "system_prompt": build_system_prompt(subject["aat_type"]),
        "user_prompt": build_user_content(subject, processed),
        "question_count": question_count,
        "assignment_type": subject["aat_type"].upper(),
    }

    return call_llm_with_fallback(subject, prompt_data)


def parse_llm_response(response, expected_count=None):
    content = response["choices"][0]["message"]["content"]
    cleaned = content.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start:end + 1]

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {"raw_text": content, "answers": []}

    if not isinstance(data.get("answers"), list):
        data["answers"] = []

    if expected_count is not None and len(data["answers"]) != expected_count:
        return data

    return data


def format_output(subject, result):
    subject = normalize_llm_subject(subject)
    processed = preprocess_questions(subject)
    if not result or not processed["questions"]:
        return

    print(f"\nSubject: {subject['subject_name']} ({subject['sub_code']})")
    print(f"Type: {subject['aat_type']}")
    print("----------------")

    if "raw_text" in result:
        print(result["raw_text"])
        return

    if subject["aat_type"].upper() == "CS-II":
        print(f"\nTitle:\n{result.get('title', '')}")
        print(f"\nIntroduction:\n{result.get('introduction', '')}")
        print(f"\nBackground:\n{result.get('background', '')}")

    answers = result.get("answers", [])
    for index, question in enumerate(processed["questions"], start=1):
        answer = answers[index - 1] if index - 1 < len(answers) else ""
        print(f"\nQ{index}: {question}")
        print(f"A{index}: {answer}")


def generate_cover_html(subject, student_info):
    assignment_type = subject.get("assignment_type", subject.get("aat_type", "Assignment-II"))
    logo_path = Path("download (1).jpg").resolve().as_uri()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{escape(subject['course_code'])} {escape(assignment_type)}</title>
  <style>
    html, body {{
      min-height: 100%;
    }}
    body {{
      margin: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      font-family: "Times New Roman", Times, serif;
      color: #111;
    }}
    .page {{
      width: 100%;
      min-height: 100vh;
      padding: 45px 60px;
      border: 3px solid #1f2f46;
      box-sizing: border-box;
      text-align: center;
    }}
    .heading {{
      margin-top: 18px;
      font-size: 28px;
      font-weight: bold;
      color: #17375e;
      text-transform: uppercase;
      line-height: 1.3;
    }}
    .on {{ margin-top: 55px; font-size: 20px; font-weight: bold; }}
    .title {{ margin-top: 20px; font-size: 26px; font-weight: bold; }}
    .subject {{ margin-top: 36px; font-size: 24px; font-weight: bold; text-transform: uppercase; }}
    .code {{ margin-top: 16px; font-size: 21px; font-weight: bold; }}
    .logo {{
      display: block;
      width: 120px;
      height: 120px;
      object-fit: contain;
      margin: 62px auto 58px;
    }}
    .by {{ margin-top: 60px; font-size: 20px; font-weight: bold; }}
    .student {{ margin-top: 24px; font-size: 21px; font-weight: bold; line-height: 1.9; }}
    .footer {{ margin-top: 95px; font-size: 19px; font-weight: bold; line-height: 1.5; }}
    @page {{ size: A4; margin: 18mm; }}
  </style>
</head>
<body>
  <div class="page">
    <div class="heading">INSTITUTE OF AERONAUTICAL ENGINEERING</div>
    <div class="on">ON</div>
    <div class="title">{escape(assignment_type)}</div>
    <div class="subject">{escape(subject['course_name'])}</div>
    <div class="code">COURSE CODE - {escape(subject['course_code'])}</div>
    <img class="logo" src="{logo_path}" alt="Institute Logo">
    <div class="by">BY</div>
    <div class="student">
      <div>{escape(student_info['name'])}</div>
      <div>{escape(student_info['roll_no'])}</div>
      <div>{escape(student_info['branch_section'])}</div>
    </div>
    <div class="footer">
      INSTITUTE OF AERONAUTICAL ENGINEERING<br>
      DUNDIGAL, HYDERABAD - 500 043, TELANGANA.
    </div>
  </div>
</body>
</html>"""


def html_paragraphs(text):
    lines = [clean_text(line) for line in str(text).splitlines()]
    lines = [line for line in lines if line != NOT_FOUND]
    if not lines:
        return "<p></p>"
    return "".join(f"<p>{escape(line)}</p>" for line in lines)


def generate_full_html(subject, student_info):
    subject_name = subject.get("subject_name", subject.get("course_name", NOT_FOUND))
    course_code = subject.get("sub_code", subject.get("course_code", NOT_FOUND))
    assignment_type = subject.get("aat_type", subject.get("assignment_type", "Assignment-II"))
    questions = subject.get("questions", [])
    answers = subject.get("answers", [])
    logo_path = Path("download (1).jpg").resolve().as_uri()

    if len(questions) != len(answers):
        print(
            f"Warning: questions/answers mismatch for {course_code} "
            f"({len(questions)} questions, {len(answers)} answers)"
        )

    qa_blocks = []
    for index, question in enumerate(questions, start=1):
        answer = answers[index - 1] if index - 1 < len(answers) else ""
        qa_blocks.append(
            f"""
      <div class="qa-block">
        <p class="question"><strong>Q{index}:</strong> {escape(question)}</p>
        <div class="answer"><strong>A{index}:</strong>{html_paragraphs(answer)}</div>
      </div>"""
        )

    case_study_block = ""
    if assignment_type.upper() == "CS-II":
        case_study_block = f"""
      <section class="case-section">
        <h3>Title</h3>
        {html_paragraphs(subject.get("title", ""))}
        <h3>Introduction</h3>
        {html_paragraphs(subject.get("introduction", ""))}
        <h3>Background</h3>
        {html_paragraphs(subject.get("background", ""))}
      </section>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{escape(course_code)} {escape(assignment_type)}</title>
  <style>
    @page {{ size: A4; margin: 18mm; }}
    body {{
      margin: 0;
      font-family: "Times New Roman", Times, serif;
      color: #111;
      line-height: 1.6;
      font-size: 16px;
    }}
    .cover-page {{
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: center;
      height: 90vh;
      padding: 32px 56px;
      border: 3px solid #1f2f46;
      box-sizing: border-box;
      text-align: center;
      page-break-inside: avoid;
    }}
    .heading {{
      font-size: 28px;
      font-weight: bold;
      color: #17375e;
      text-transform: uppercase;
      line-height: 1.3;
    }}
    .on {{ margin-top: 34px; font-size: 20px; font-weight: bold; }}
    .title {{ margin-top: 16px; font-size: 26px; font-weight: bold; }}
    .subject {{ margin-top: 28px; font-size: 24px; font-weight: bold; text-transform: uppercase; }}
    .code {{ margin-top: 12px; font-size: 21px; font-weight: bold; }}
    .logo {{
      display: block;
      width: 120px;
      height: 120px;
      object-fit: contain;
      margin: 38px auto 34px;
    }}
    .by {{ margin-top: 26px; font-size: 20px; font-weight: bold; }}
    .student {{ margin-top: 18px; font-size: 21px; font-weight: bold; line-height: 1.8; }}
    .footer {{
      margin-top: 40px;
      text-align: center;
      font-size: 19px;
      font-weight: bold;
      line-height: 1.5;
    }}
    .answers-page {{
      margin: 40px;
      page-break-inside: auto;
    }}
    .answers-page h2 {{
      text-align: center;
      font-size: 24px;
      margin: 0 0 28px;
    }}
    .answers-page h3 {{
      font-size: 19px;
      margin: 22px 0 8px;
    }}
    .case-section {{
      margin-bottom: 28px;
    }}
    .case-section p,
    .answer p {{
      margin: 6px 0;
      text-align: justify;
    }}
    .qa-block {{
      margin: 24px 0 30px;
      page-break-inside: avoid;
    }}
    .question {{
      margin: 0 0 10px;
      font-weight: bold;
      text-align: justify;
    }}
    .answer {{
      text-align: justify;
    }}
    .page-break {{
      page-break-after: always;
      break-after: page;
      height: 0;
    }}
  </style>
</head>
<body>
  <div class="cover-page">
    <div class="heading">INSTITUTE OF AERONAUTICAL ENGINEERING</div>
    <div class="on">ON</div>
    <div class="title">{escape(assignment_type)}</div>
    <div class="subject">{escape(subject_name)}</div>
    <div class="code">COURSE CODE - {escape(course_code)}</div>
    <img class="logo" src="{logo_path}" alt="Institute Logo">
    <div class="by">BY</div>
    <div class="student">
      <div>{escape(student_info['name'])}</div>
      <div>{escape(student_info['roll_no'])}</div>
      <div>{escape(student_info['branch_section'])}</div>
    </div>
    <div class="footer">
      INSTITUTE OF AERONAUTICAL ENGINEERING<br>
      DUNDIGAL, HYDERABAD - 500 043, TELANGANA.
    </div>
  </div>

  <div class="page-break"></div>

  <div class="answers-page">
    <h2>Assignment Answers</h2>
    {case_study_block}
    {''.join(qa_blocks)}
  </div>
</body>
</html>"""


def build_pdf_subject(subject, result):
    if not result or "raw_text" in result:
        return None

    normalized = normalize_llm_subject(subject)
    processed = preprocess_questions(normalized)
    questions = processed["questions"]
    answers = result.get("answers", [])
    if not answers:
        return None

    if len(questions) != len(answers):
        print(
            f"Warning: questions/answers mismatch for {normalized['sub_code']} "
            f"({len(questions)} questions, {len(answers)} answers)"
        )

    return {
        "sub_code": normalized["sub_code"],
        "subject_name": normalized["subject_name"],
        "aat_type": normalized["aat_type"],
        "questions": questions,
        "answers": answers,
        "title": result.get("title", ""),
        "introduction": result.get("introduction", ""),
        "background": result.get("background", ""),
    }


def save_pdf(html, filename):
    os.makedirs("assignments", exist_ok=True)

    filename = safe_filename(filename)
    output_path = os.path.join("assignments", filename)
    absolute_output_path = os.path.abspath(output_path)

    options = {
        "page-size": "A4",
        "encoding": "UTF-8",
        "enable-local-file-access": None,
        "quiet": "",
    }

    try:
        import pdfkit

        pdfkit.from_string(html, output_path, options=options)
        return output_path
    except (ImportError, OSError):
        browser_path = find_pdf_browser()
        if not browser_path:
            print("PDF generation failed: install wkhtmltopdf or Google Chrome/Microsoft Edge.")
            return None

    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as temp_file:
        temp_file.write(html)
        temp_html_path = temp_file.name

    try:
        subprocess.run(
            [
                browser_path,
                "--headless",
                "--disable-gpu",
                f"--print-to-pdf={absolute_output_path}",
                Path(temp_html_path).as_uri(),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    finally:
        os.remove(temp_html_path)

    return output_path


def find_pdf_browser():
    browser_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]

    for browser_path in browser_paths:
        if os.path.exists(browser_path):
            return browser_path

    return None


def generate_assignment_pdfs(subjects, student_info):
    for subject in subjects:
        if not subject.get("answers"):
            continue

        if len(subject.get("questions", [])) != len(subject.get("answers", [])):
            print(
                f"Warning: questions/answers mismatch for {subject['sub_code']} "
                f"({len(subject.get('questions', []))} questions, "
                f"{len(subject.get('answers', []))} answers)"
            )

        html = generate_full_html(subject, student_info)
        filename = f"{safe_filename(subject['sub_code'])}_{safe_filename(subject['aat_type'])}.pdf"
        if save_pdf(html, filename):
            print(f"Generated: {filename}")


def generate_assignment_cover_pdfs(subjects, student_info):
    generate_assignment_pdfs(subjects, student_info)


if __name__ == "__main__":
    roll_no = input("Enter Roll No: ").strip()
    password = getpass("Enter Password: ").strip()

    logged_in_session = login(roll_no, password)
    if logged_in_session:
        profile_data = get_profile_data(logged_in_session)
        if profile_data == "Session expired":
            print("Session expired, re-login required")
        else:
            display_profile(profile_data)

        assignment_data = get_aat_subjects(logged_in_session)
        if assignment_data == "Session expired":
            print("Session expired, re-login required")
        else:
            display_assignments(assignment_data)
            pdf_subjects = []
            for subject in assignment_data:
                generated_answers = generate_answers(subject)
                format_output(subject, generated_answers)
                pdf_subject = build_pdf_subject(subject, generated_answers)
                if pdf_subject:
                    pdf_subjects.append(pdf_subject)

            student_info = {
                "name": profile_data["name"],
                "roll_no": profile_data["roll_number"],
                "branch_section": f"{profile_data['branch']} - {profile_data['section']}",
            }
            generate_assignment_pdfs(pdf_subjects, student_info)
