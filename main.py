import os
from concurrent.futures import ThreadPoolExecutor
from typing import List

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel

from services.llm import generate_subject_answers
from services.scraper import authenticate, fetch_profile, fetch_subjects
from utils.pdf import build_student_info, prepare_pdf_subject, resolve_assignment_path, save_assignment_pdf


app = FastAPI(title="IARE Assignment Generator API")


class AssignmentRequest(BaseModel):
    roll_no: str
    password: str


class GeneratedFile(BaseModel):
    subject: str
    file: str
    path: str


class AssignmentResponse(BaseModel):
    status: str
    files: List[GeneratedFile]


def process_single_subject(subject, student_info):
    print(f"Generating for subject: {subject.get('course_name', subject.get('sub_code'))}")

    llm_result = generate_subject_answers(subject)
    if not llm_result:
        return None

    pdf_subject = prepare_pdf_subject(subject, llm_result)
    if not pdf_subject:
        return None

    file_info = save_assignment_pdf(pdf_subject, student_info)
    if file_info:
        print("PDF generated")

    return file_info


def generate_assignment_pipeline(roll_no, password):
    session = authenticate(roll_no, password)
    if not session:
        raise ValueError("Login failed")

    print("Login success")

    profile = fetch_profile(session)
    if profile == "Session expired":
        raise ValueError("Session expired")

    subjects = fetch_subjects(session)
    if subjects == "Session expired":
        raise ValueError("Session expired")

    student_info = build_student_info(profile)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda subject: process_single_subject(subject, student_info),
                subjects,
            )
        )

    return [result for result in results if result is not None]


@app.post("/generate-assignment", response_model=AssignmentResponse)
async def generate_assignment(payload: AssignmentRequest):
    try:
        files = await run_in_threadpool(
            generate_assignment_pipeline,
            payload.roll_no.strip(),
            payload.password,
        )
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Assignment generation failed: {exc}") from exc

    return {
        "status": "success",
        "files": files,
    }


@app.get("/download/{filename}")
async def download_assignment(filename: str):
    file_path = resolve_assignment_path(filename)
    assignments_dir = os.path.abspath("assignments")

    if not file_path.startswith(assignments_dir):
        raise HTTPException(status_code=400, detail="Invalid filename")

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        file_path,
        media_type="application/pdf",
        filename=os.path.basename(file_path),
    )
