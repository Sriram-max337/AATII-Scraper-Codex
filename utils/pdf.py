import os

from samvidha_profile import build_pdf_subject, generate_full_html, safe_filename, save_pdf


ASSIGNMENTS_DIR = "assignments"


def build_student_info(profile):
    return {
        "name": profile["name"],
        "roll_no": profile["roll_number"],
        "branch_section": f"{profile['branch']} - {profile['section']}",
    }


def save_assignment_pdf(subject, student_info):
    html = generate_full_html(subject, student_info)
    filename = f"{safe_filename(subject['sub_code'])}_{safe_filename(subject['aat_type'])}.pdf"
    output_path = save_pdf(html, filename)
    if not output_path:
        return None

    return {
        "subject": subject["subject_name"],
        "file": filename,
        "path": f"/assignments/{filename}",
    }


def prepare_pdf_subject(scraped_subject, llm_result):
    return build_pdf_subject(scraped_subject, llm_result)


def resolve_assignment_path(filename):
    safe_name = safe_filename(filename)
    return os.path.abspath(os.path.join(ASSIGNMENTS_DIR, safe_name))
