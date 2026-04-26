from samvidha_profile import get_aat_subjects, get_profile_data, login


def authenticate(roll_no, password):
    return login(roll_no, password)


def fetch_profile(session):
    return get_profile_data(session)


def fetch_subjects(session):
    return get_aat_subjects(session)
