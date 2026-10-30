from __future__ import annotations

import re
import secrets
import unicodedata
from datetime import date, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.deps import current_user, current_user_password_setup, enforce_login_rate, get_db, get_settings, require_roles, student_access_problem
from app.models import Announcement, Assignment, AttendanceRecord, AuditLog, Chapter, Course, EmailOutbox, Enrollment, FacultyAllocation, Mark, Notification, NotificationRead, RegistrationRequest as RegistrationRequestRecord, StudentProfile, Submission, TimetableSlot, User, utcnow
from app.security import create_token, generate_temporary_password, hash_password, validate_password, verify_password
from app.services.academic import attended_record, audit, auto_enroll_student_in_matching_courses, course_payload, faculty_analytics, student_matches_course_scope, user_public
from app.services.notifications import deliver_email_outbox, queue_email, send_temporary_credentials

router = APIRouter(prefix="/api/auth", tags=["auth"])

PROVISIONING_TTL_MINUTES = 30
# Plaintext temporary passwords exist only in this short-lived process cache for the
# duration of the admin preview. They are never written to the database or outbox.
_provisioning_cache: dict[str, tuple[str, datetime]] = {}


def _normalize_email(value: str) -> str:
    email = value.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 255:
        raise ValueError("Enter a valid email address.")
    return email


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def check_email(cls, value: str) -> str:
        if "@" in value:
            return _normalize_email(value)
        identifier = value.strip().upper()
        if not re.fullmatch(r"ENX\d{4}-[A-F0-9]{6}", identifier):
            raise ValueError("Enter your email address or institutional student ID.")
        return identifier


class PasswordChange(BaseModel):
    currentPassword: str = Field(min_length=1, max_length=128)
    newPassword: str = Field(min_length=10, max_length=128)


class CourseRequest(BaseModel):
    code: str = Field(min_length=2, max_length=32)
    name: str = Field(min_length=2, max_length=160)
    term: str = Field(min_length=1, max_length=80)
    room: str = Field(default="", max_length=40)
    facultyId: str = Field(min_length=1, max_length=36)
    semester: int | None = Field(default=None, ge=1, le=14)
    academicYear: int | None = Field(default=None, ge=1, le=7)
    section: str = Field(default="A", min_length=1, max_length=40)
    department: str = Field(default="", max_length=160)


class EnrollmentRequest(BaseModel):
    studentId: str = Field(min_length=1, max_length=36)


class ProvisionRequest(BaseModel):
    email: str
    password: str = Field(min_length=10, max_length=128)

    @field_validator("email")
    @classmethod
    def check_email(cls, value: str) -> str:
        return _normalize_email(value)
    displayName: str = Field(min_length=2, max_length=120)
    role: str
    department: str | None = Field(default=None, max_length=160)
    program: str | None = Field(default=None, max_length=160)
    term: str | None = Field(default=None, max_length=80)
    rollNumber: str | None = Field(default=None, max_length=40)


@router.post("/login")
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db), settings=Depends(get_settings)):
    enforce_login_rate(request, body.email)
    identifier = body.email
    user = db.scalar(
        select(User).outerjoin(StudentProfile, StudentProfile.user_id == User.id).where(
            or_(User.email == identifier.lower(), func.lower(StudentProfile.institutional_email) == identifier.lower(), StudentProfile.institutional_id == identifier.upper())
        )
    )
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    problem = student_access_problem(user)
    if problem:
        raise HTTPException(status_code=403, detail=problem)
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    return {"accessToken": create_token(settings, user.id, user.role, user.token_version), "tokenType": "bearer", "user": user_public(user)}


@router.get("/me")
def me(user: User = Depends(current_user_password_setup)):
    return user_public(user)


@router.post("/password")
def change_password(body: PasswordChange, request: Request, user: User = Depends(current_user_password_setup), db: Session = Depends(get_db), settings=Depends(get_settings)):
    enforce_login_rate(request, f"password-change:{user.id}")
    problem = validate_password(body.newPassword)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    if not verify_password(body.currentPassword, user.password_hash):
        raise HTTPException(status_code=400, detail="The current password is incorrect.")
    if verify_password(body.newPassword, user.password_hash):
        raise HTTPException(status_code=422, detail="Choose a password different from your current password.")
    registration = db.scalar(select(RegistrationRequestRecord).where(RegistrationRequestRecord.user_id == user.id))
    was_temporary = user.must_change_password
    user.password_hash = hash_password(body.newPassword)
    user.must_change_password = False
    user.password_changed_at = utcnow()
    user.token_version = int(user.token_version or 0) + 1
    if registration is not None and user.role == "student":
        registration.password_setup_completed_at = utcnow()
        registration.credential_status = "COMPLETED"
        registration.credential_error = None
        registration.pending_password_hash = None
        registration.provisioning_expires_at = None
    if was_temporary:
        event_key = f"password-setup:{user.id}:{secrets.token_hex(4)}"
        db.add(Notification(event_key=event_key, recipient_user_id=user.id, kind="password_setup", title="Password updated", body="Your EduNexus password was changed. The temporary password can no longer be used.", related_user_id=user.id))
    audit(db, user.id, "change_password", "user", user.id, "temporary-setup" if was_temporary else "profile")
    db.commit()
    return {"status": "saved", "accessToken": create_token(settings, user.id, user.role, user.token_version), "tokenType": "bearer", "user": user_public(user)}


class RegistrationRequest(BaseModel):
    email: str
    password: str = Field(min_length=10, max_length=128)
    displayName: str = Field(min_length=2, max_length=120)
    program: str = Field(min_length=1, max_length=160)
    term: str = Field(min_length=1, max_length=80)
    rollNumber: str = Field(min_length=1, max_length=40)

    @field_validator("email")
    @classmethod
    def check_email(cls, value: str) -> str:
        return _normalize_email(value)

    @field_validator("displayName", "program", "term", "rollNumber")
    @classmethod
    def require_nonblank_academic_fields(cls, value: str, info) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("This field cannot be blank.")
        return cleaned.upper() if info.field_name == "rollNumber" else cleaned


class RegistrationStatusCheck(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def check_email(cls, value: str) -> str:
        return _normalize_email(value)


@router.post("/registration-status")
def registration_status(body: RegistrationStatusCheck, request: Request, db: Session = Depends(get_db)):
    enforce_login_rate(request, body.email)
    user = db.scalar(select(User).where(User.email == body.email, User.role == "student"))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=404, detail="We could not verify a registration with those details.")
    request_row = db.scalar(select(RegistrationRequestRecord).where(RegistrationRequestRecord.user_id == user.id))
    profile = db.get(StudentProfile, user.id)
    status = request_row.status if request_row else ("APPROVED" if user.approval_status == "approved" else user.approval_status.upper())
    if status == "PENDING_APPROVAL":
        message = "Your registration is saved and awaiting administrator review."
    elif status == "REJECTED":
        message = "Your registration was not approved." + (f" Reason: {request_row.rejection_reason}" if request_row and request_row.rejection_reason else " Contact the institution's administrator for more information.")
    elif request_row and request_row.credential_status in {"FAILED", "CONFIGURATION_ERROR", "DELIVERY_PENDING"}:
        message = "Your registration was approved. Your institutional credentials have not reached your personal email yet; contact the institution's administrator."
    elif request_row and request_row.password_setup_completed_at:
        message = "Your registration was approved and your password has been set. Sign in with your institutional email or ID and your new password."
    else:
        message = "Your registration was approved. Sign in with your institutional email or ID and the temporary password issued to your personal email, then set a new password."
    notices = db.scalars(select(Notification).where(Notification.recipient_user_id == user.id).order_by(Notification.created_at.desc()).limit(20)).all()
    notice_rows = []
    for item in notices:
        read = db.scalar(select(NotificationRead.id).where(NotificationRead.notification_id == item.id, NotificationRead.user_id == user.id)) is not None
        notice_rows.append({"kind": item.kind, "title": item.title, "body": item.body, "createdAt": item.created_at.isoformat(), "read": read})
        if not read:
            db.add(NotificationRead(notification_id=item.id, user_id=user.id))
    db.commit()
    return {
        "status": status,
        "message": message,
        "institutionalId": profile.institutional_id if profile else None,
        "institutionalEmail": profile.institutional_email if profile else None,
        "accountActive": user.is_active,
        "passwordChangeRequired": bool(user.must_change_password),
        "credentialStatus": request_row.credential_status if request_row else None,
        "notifications": notice_rows,
    }


@router.post("/register", status_code=201)
def register_student(body: RegistrationRequest, request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db), settings=Depends(get_settings)):
    problem = validate_password(body.password)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    if db.scalar(select(User).where(User.email == body.email)):
        raise HTTPException(status_code=409, detail="An account with that email already exists.")
    roll = body.rollNumber.strip().upper()
    if db.scalar(select(StudentProfile).where(StudentProfile.roll_number == roll)):
        raise HTTPException(status_code=409, detail="That roll number is already in use.")
    user = User(email=body.email, password_hash=hash_password(body.password), role="student", display_name=body.displayName.strip(), department=body.program.strip(), approval_status="pending", is_active=False)
    outbox = None
    try:
        db.add(user)
        db.flush()
        semester = _semester_number(body.term)
        db.add(StudentProfile(user_id=user.id, program=body.program.strip(), term=body.term.strip(), roll_number=roll, semester=semester, academic_year=(semester + 1) // 2, department=body.program.strip(), section="A", academic_status="PENDING_APPROVAL"))
        registration = RegistrationRequestRecord(
            user_id=user.id,
            status="PENDING_APPROVAL",
            submitted_name=user.display_name,
            submitted_email=user.email,
            submitted_program=body.program.strip(),
            submitted_term=body.term.strip(),
            submitted_roll_number=roll,
            credential_status="NOT_PROVISIONED",
        )
        db.add(registration)
        db.flush()
        event_key = f"registration:{user.id}:pending"
        notification = Notification(event_key=event_key, audience_role="administrator", kind="registration_pending", title="Student registration awaiting review", body=f"{user.display_name} submitted a student registration request.", related_user_id=user.id)
        db.add(notification)
        outbox = queue_email(db, settings, event_key, settings.admin_notification_email, "EduNexus student registration awaits review", f"A new student registration request from {user.display_name} is awaiting review. Sign in to the EduNexus administrator dashboard to review it.")
        if outbox is not None:
            notification.email_outbox_id = outbox.id
            db.flush()
        audit(db, None, "register_student", "registration_request", registration.id, "pending approval")
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="A registration already exists with these email or institutional details.") from error
    if outbox is not None:
        background_tasks.add_task(deliver_email_outbox, request.app.state.session_factory, settings, outbox.id)
    return {"status": "pending", "requestStatus": "PENDING_APPROVAL", "requestId": registration.id, "message": "Registration received and awaiting administrator approval. Once approved, we will email your institutional sign-in details to this address."}


admin_router = APIRouter(prefix="/api/admin", tags=["admin"])


def _semester_number(term: str | None) -> int:
    match = re.search(r"(?:semester|sem)\s*(\d+)", term or "", re.IGNORECASE)
    return min(max(int(match.group(1)), 1), 14) if match else 1


def _assign_institutional_id(db: Session, user: User) -> str:
    profile = db.get(StudentProfile, user.id)
    if profile is None:
        raise HTTPException(status_code=409, detail="Student academic profile is missing; complete the academic assignment before approval.")
    if profile.institutional_id:
        return profile.institutional_id
    from app.services.student_ids import assign_student_institutional_id

    assigned = assign_student_institutional_id(db, user.id)
    if not assigned:
        raise HTTPException(status_code=409, detail="Student academic profile is missing; complete the academic assignment before approval.")
    return assigned


def _email_in_use(db: Session, email: str, exclude_user_id: str | None = None, exclude_request_id: str | None = None) -> bool:
    profile_query = select(StudentProfile.user_id).where(func.lower(StudentProfile.institutional_email) == email.lower())
    if exclude_user_id:
        profile_query = profile_query.where(StudentProfile.user_id != exclude_user_id)
    if db.scalar(profile_query) is not None:
        return True
    request_query = select(RegistrationRequestRecord.id).where(func.lower(RegistrationRequestRecord.provisioning_email) == email.lower())
    if exclude_request_id:
        request_query = request_query.where(RegistrationRequestRecord.id != exclude_request_id)
    return db.scalar(request_query) is not None


def _proposed_institutional_email(db: Session, user: User, settings, registration: RegistrationRequestRecord | None = None) -> str:
    profile = db.get(StudentProfile, user.id)
    if profile is not None and profile.institutional_email:
        return profile.institutional_email
    if registration is not None and registration.provisioning_email and not _email_in_use(db, registration.provisioning_email, exclude_user_id=user.id, exclude_request_id=registration.id):
        return registration.provisioning_email
    domain = (settings.institution_email_domain or "students.example.test").strip().lower().lstrip("@")
    local = unicodedata.normalize("NFKD", user.display_name).encode("ascii", "ignore").decode("ascii").lower()
    local = re.sub(r"[^a-z0-9]+", ".", local).strip(".") or "student"
    candidate = f"{local}@{domain}"
    suffix = 1
    while _email_in_use(db, candidate, exclude_user_id=user.id, exclude_request_id=registration.id if registration else None):
        candidate = f"{local}.{suffix}@{domain}"
        suffix += 1
    return candidate


def _assign_institutional_identity(db: Session, user: User, settings) -> StudentProfile:
    profile = db.get(StudentProfile, user.id)
    if profile is None:
        raise HTTPException(status_code=409, detail="Student academic profile is missing; complete the academic assignment before approval.")
    _assign_institutional_id(db, user)
    if not profile.institutional_email:
        profile.institutional_email = _proposed_institutional_email(db, user, settings)
    return profile


@admin_router.post("/users", status_code=201)
def provision_user(body: ProvisionRequest, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db), settings=Depends(get_settings)):
    if body.role not in {"student", "faculty", "administrator"}:
        raise HTTPException(status_code=422, detail="Role must be student, faculty, or administrator.")
    problem = validate_password(body.password)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    if body.role == "student" and not (body.program and body.term and body.rollNumber):
        raise HTTPException(status_code=422, detail="Student accounts need a program, term, and roll number.")
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=409, detail="An account with that email already exists.")
    normalized_roll = (body.rollNumber or "").strip().upper()
    if normalized_roll and db.scalar(select(StudentProfile).where(StudentProfile.roll_number == normalized_roll)):
        raise HTTPException(status_code=409, detail="That roll number is already in use.")
    account_department = (body.department or body.program or "").strip()
    user = User(email=email, password_hash=hash_password(body.password), role=body.role, display_name=body.displayName.strip(), department=account_department, approval_status="approved", must_change_password=False)
    db.add(user)
    db.flush()
    if body.role == "student":
        semester = _semester_number(body.term)
        profile = StudentProfile(user_id=user.id, program=body.program or "", term=body.term or "", roll_number=normalized_roll, semester=semester, academic_year=(semester + 1) // 2, department=account_department, section="A")
        db.add(profile)
        db.flush()
        _assign_institutional_identity(db, user, settings)
        auto_enroll_student_in_matching_courses(db, user.id)
    audit(db, actor.id, "provision_user", "user", user.id, body.role)
    db.commit()
    db.refresh(user)
    return user_public(user)


@admin_router.get("/users")
def list_users(actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    users = db.scalars(select(User).order_by(User.email)).all()
    return [{**user_public(user), "department": user.department} for user in users]


@admin_router.patch("/faculty/{faculty_id}")
def update_faculty_department(faculty_id: str, body: FacultyDepartmentPatch, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    faculty = db.get(User, faculty_id)
    if faculty is None or faculty.role != "faculty":
        raise HTTPException(status_code=404, detail="Faculty account not found.")
    faculty.department = body.department.strip()
    audit(db, actor.id, "update_faculty_department", "user", faculty.id, faculty.department)
    db.commit()
    return {**user_public(faculty), "department": faculty.department}


@admin_router.get("/courses")
def list_courses(actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    courses = db.scalars(select(Course).order_by(Course.code)).all()
    rows = []
    for course in courses:
        students = db.query(Enrollment).filter(Enrollment.course_id == course.id).count()
        rows.append({**course_payload(db, course), "facultyId": course.faculty_id, "studentCount": students})
    return rows


@admin_router.post("/courses", status_code=201)
def create_course(body: CourseRequest, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    code = body.code.strip().upper()
    faculty = db.get(User, body.facultyId)
    if faculty is None or faculty.role != "faculty" or not faculty.is_active:
        raise HTTPException(status_code=422, detail="Choose an active faculty account to teach this course.")
    if db.scalar(select(Course).where(Course.code == code)):
        raise HTTPException(status_code=409, detail="A course with that code already exists.")
    semester = body.semester or _semester_number(body.term)
    academic_year = body.academicYear or (semester + 1) // 2
    if academic_year != (semester + 1) // 2:
        raise HTTPException(status_code=422, detail="Academic year must match the subject semester (two semesters per year).")
    course = Course(code=code, name=body.name.strip(), term=body.term.strip(), room=body.room.strip(), faculty_id=faculty.id, semester=semester, academic_year=academic_year, section=body.section.strip(), department=body.department.strip())
    db.add(course)
    db.flush()
    _create_allocation(db, AllocationRequest(facultyId=faculty.id, courseId=course.id, academicYear=academic_year, semester=semester, section=course.section, academicTerm=course.term))
    audit(db, actor.id, "create_course", "course", course.id, code)
    db.commit()
    db.refresh(course)
    return {**course_payload(db, course), "facultyId": course.faculty_id, "studentCount": 0}


@admin_router.get("/courses/{course_id}/chapters")
def admin_chapters(course_id: str, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    if db.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="Subject not found.")
    rows = db.scalars(select(Chapter).where(Chapter.course_id == course_id).order_by(Chapter.ordinal)).all()
    return [{"id": row.id, "title": row.title, "ordinal": row.ordinal, "syllabusCode": row.syllabus_code} for row in rows]


@admin_router.post("/courses/{course_id}/chapters", status_code=201)
def create_chapter(course_id: str, body: ChapterRequest, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    if db.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="Subject not found.")
    duplicate = db.scalar(select(Chapter.id).where(Chapter.course_id == course_id, or_(Chapter.ordinal == body.ordinal, func.lower(Chapter.title) == body.title.strip().lower())))
    if duplicate:
        raise HTTPException(status_code=409, detail="A chapter with that title or order already exists.")
    row = Chapter(course_id=course_id, title=body.title.strip(), ordinal=body.ordinal, syllabus_code=body.syllabusCode)
    db.add(row)
    db.flush()
    audit(db, actor.id, "create_chapter", "chapter", row.id, f"{course_id} {row.title}")
    db.commit()
    return {"id": row.id, "title": row.title, "ordinal": row.ordinal, "syllabusCode": row.syllabus_code}


@admin_router.post("/courses/{course_id}/enrollments", status_code=201)
def enroll_student(course_id: str, body: EnrollmentRequest, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found.")
    student = db.get(User, body.studentId)
    if student is None or student.role != "student" or not student.is_active:
        raise HTTPException(status_code=422, detail="Choose an active student account to enrol.")
    if student.approval_status != "approved":
        raise HTTPException(status_code=422, detail="Only approved students can be enrolled.")
    if not student_matches_course_scope(db.get(StudentProfile, student.id), course):
        raise HTTPException(status_code=422, detail="The subject department or class/section does not match this student's academic assignment.")
    if db.scalar(select(Enrollment.id).where(Enrollment.course_id == course.id, Enrollment.student_id == student.id)):
        raise HTTPException(status_code=409, detail="That student is already enrolled in this course.")
    enrollment = Enrollment(course_id=course.id, student_id=student.id)
    db.add(enrollment)
    try:
        db.flush()
        audit(db, actor.id, "enroll_student", "enrollment", enrollment.id, f"{course.code} {student.email}")
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="That student is already enrolled in this course.") from error
    return {"id": enrollment.id, "courseId": course.id, "studentId": student.id}


class CoursePatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    term: str | None = Field(default=None, min_length=1, max_length=80)
    room: str | None = Field(default=None, max_length=40)
    facultyId: str | None = Field(default=None, min_length=1, max_length=36)
    department: str | None = Field(default=None, max_length=160)
    semester: int | None = Field(default=None, ge=1, le=14)
    academicYear: int | None = Field(default=None, ge=1, le=7)
    section: str | None = Field(default=None, min_length=1, max_length=40)


class StudentAcademicPatch(BaseModel):
    program: str = Field(min_length=1, max_length=160)
    department: str = Field(min_length=1, max_length=160)
    semester: int = Field(ge=1, le=14)
    section: str = Field(min_length=1, max_length=40)
    admissionBatch: str = Field(default="", max_length=40)
    regulationYear: int | None = Field(default=None, ge=1900, le=2200)

    @field_validator("program", "department", "section")
    @classmethod
    def require_academic_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Academic assignment fields cannot be blank.")
        return value.strip()

    @field_validator("admissionBatch")
    @classmethod
    def trim_batch(cls, value: str) -> str:
        return value.strip()


class StudentLifecyclePatch(BaseModel):
    status: str
    graduationYear: int | None = Field(default=None, ge=1900, le=2200)
    graduationDate: date | None = None
    finalProgram: str | None = Field(default=None, max_length=160)


class FacultyDepartmentPatch(BaseModel):
    department: str = Field(min_length=1, max_length=160)

    @field_validator("department")
    @classmethod
    def trim_department(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Department cannot be blank.")
        return value.strip()


class RegistrationDecision(BaseModel):
    reason: str = Field(default="", max_length=1000)


class AllocationRequest(BaseModel):
    facultyId: str = Field(min_length=1, max_length=36)
    courseId: str = Field(min_length=1, max_length=36)
    academicYear: int = Field(ge=1, le=7)
    semester: int = Field(ge=1, le=14)
    section: str = Field(min_length=1, max_length=40)
    academicTerm: str = Field(default="", max_length=80)


class ChapterRequest(BaseModel):
    title: str = Field(min_length=2, max_length=180)
    ordinal: int = Field(ge=1, le=100)
    syllabusCode: str | None = Field(default=None, max_length=40)


def _create_allocation(db: Session, body: AllocationRequest) -> FacultyAllocation:
    faculty = db.get(User, body.facultyId)
    course = db.get(Course, body.courseId)
    if faculty is None or faculty.role != "faculty" or not faculty.is_active:
        raise HTTPException(status_code=422, detail="Choose an active faculty account.")
    if course is None:
        raise HTTPException(status_code=404, detail="Subject not found.")
    faculty_department = (faculty.department or "").casefold().strip()
    course_department = (course.department or "").casefold().strip()
    if course_department and not faculty_department:
        raise HTTPException(status_code=422, detail="Assign the faculty member to a department before allocating a departmental subject.")
    if faculty_department and course_department and faculty_department not in course_department and course_department not in faculty_department:
        raise HTTPException(status_code=422, detail="Faculty and subject departments are not compatible.")
    if (body.academicYear, body.semester, body.section.strip()) != (course.academic_year, course.semester, course.section):
        raise HTTPException(status_code=422, detail="Academic year, semester, and section must match the subject assignment.")
    duplicate = db.scalar(select(FacultyAllocation.id).where(
        FacultyAllocation.faculty_id == faculty.id,
        FacultyAllocation.academic_year == body.academicYear,
        FacultyAllocation.course_id == course.id,
        FacultyAllocation.section == body.section.strip(),
    ))
    if duplicate:
        raise HTTPException(status_code=409, detail="This faculty allocation already exists.")
    current_subjects = set(db.scalars(select(FacultyAllocation.course_id).where(
        FacultyAllocation.faculty_id == faculty.id,
        FacultyAllocation.academic_year == body.academicYear,
    ).distinct()).all())
    if course.id not in current_subjects and len(current_subjects) >= 2:
        raise HTTPException(status_code=422, detail="A faculty member may teach at most two distinct subjects in one academic year.")
    allocation = FacultyAllocation(
        faculty_id=faculty.id,
        academic_year=body.academicYear,
        course_id=course.id,
        semester=body.semester,
        section=body.section.strip(),
        academic_term=body.academicTerm.strip() or course.term,
    )
    db.add(allocation)
    db.flush()
    return allocation


def _account_state(user: User, registration: RegistrationRequestRecord | None) -> str:
    if user.approval_status == "rejected":
        return "REJECTED"
    if user.approval_status == "pending":
        return "PENDING_APPROVAL"
    if not user.must_change_password:
        return "PASSWORD_SETUP_COMPLETED"
    credential_status = (registration.credential_status if registration else "NOT_PROVISIONED") or "NOT_PROVISIONED"
    if credential_status == "SENT":
        return "CREDENTIAL_EMAIL_SENT"
    if credential_status == "FAILED":
        return "DELIVERY_FAILED"
    if credential_status in {"CONFIGURATION_ERROR", "NOT_PROVISIONED", "DELIVERY_PENDING"}:
        return "CREDENTIAL_DELIVERY_PENDING"
    if credential_status == "READY":
        return "READY_TO_SEND"
    return "CREDENTIAL_DELIVERY_PENDING"


def _student_row(db: Session, user: User) -> dict:
    profile = db.get(StudentProfile, user.id)
    registration = db.scalar(select(RegistrationRequestRecord).where(RegistrationRequestRecord.user_id == user.id))
    return {
        "id": user.id,
        "displayName": user.display_name,
        "email": user.email,
        "rollNumber": profile.roll_number if profile else None,
        "program": profile.program if profile else None,
        "term": profile.term if profile else None,
        "institutionalId": profile.institutional_id if profile else None,
        "institutionalEmail": profile.institutional_email if profile else None,
        "department": profile.department if profile else None,
        "academicYear": profile.academic_year if profile else None,
        "semester": profile.semester if profile else None,
        "section": profile.section if profile else None,
        "admissionBatch": profile.admission_batch if profile else None,
        "regulationYear": profile.regulation_year if profile else None,
        "academicStatus": profile.academic_status if profile else None,
        "graduationYear": profile.graduation_year if profile else None,
        "graduationDate": profile.graduation_date.isoformat() if profile and profile.graduation_date else None,
        "finalProgram": profile.final_program if profile else None,
        "approvalStatus": user.approval_status,
        "isActive": user.is_active,
        "mustChangePassword": bool(user.must_change_password),
        "passwordChangedAt": user.password_changed_at.isoformat() if user.password_changed_at else None,
        "accountState": _account_state(user, registration),
        "credentialStatus": registration.credential_status if registration else "NOT_PROVISIONED",
        "credentialError": registration.credential_error if registration else None,
        "credentialAttempts": registration.credential_attempts if registration else 0,
        "credentialSentAt": registration.credential_sent_at.isoformat() if registration and registration.credential_sent_at else None,
        "provisionedAt": registration.provisioned_at.isoformat() if registration and registration.provisioned_at else None,
        "passwordSetupCompletedAt": registration.password_setup_completed_at.isoformat() if registration and registration.password_setup_completed_at else None,
        "createdAt": user.created_at.isoformat(),
        "enrolmentCount": db.query(Enrollment).filter(Enrollment.student_id == user.id).count(),
        "registrationRequestId": registration.id if registration else None,
        "registrationStatus": registration.status if registration else None,
        "submittedAt": registration.submitted_at.isoformat() if registration else user.created_at.isoformat(),
        "submittedName": registration.submitted_name if registration else user.display_name,
        "submittedEmail": registration.submitted_email if registration else user.email,
        "submittedProgram": registration.submitted_program if registration else (profile.program if profile else None),
        "submittedTerm": registration.submitted_term if registration else (profile.term if profile else None),
        "submittedRollNumber": registration.submitted_roll_number if registration else (profile.roll_number if profile else None),
        "reviewedAt": registration.reviewed_at.isoformat() if registration and registration.reviewed_at else None,
        "rejectionReason": registration.rejection_reason if registration else None,
    }


def _student_or_404(db: Session, student_id: str) -> User:
    user = db.get(User, student_id)
    if user is None or user.role != "student":
        raise HTTPException(status_code=404, detail="Student not found.")
    return user


@admin_router.get("/summary")
def admin_summary(actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    students = db.scalars(select(User).where(User.role == "student")).all()
    profiles = {profile.user_id: profile for profile in db.scalars(select(StudentProfile)).all()}
    registration_notifications = db.scalars(select(Notification).where(Notification.audience_role == "administrator", Notification.kind == "registration_pending")).all()
    read_notification_ids = set(db.scalars(select(NotificationRead.notification_id).where(NotificationRead.user_id == actor.id)).all())
    return {
        "totalStudents": len(students),
        "pendingRegistrations": db.query(RegistrationRequestRecord).filter(RegistrationRequestRecord.status == "PENDING_APPROVAL").count(),
        "newRequestsSinceReview": sum(1 for item in registration_notifications if item.id not in read_notification_ids),
        "approvedStudents": sum(1 for item in students if item.approval_status == "approved"),
        "rejectedStudents": sum(1 for item in students if item.approval_status == "rejected"),
        "activeStudents": sum(1 for item in students if item.is_active and profiles.get(item.id) and profiles[item.id].academic_status == "ACTIVE"),
        "historicalStudents": sum(1 for item in students if profiles.get(item.id) and profiles[item.id].academic_status in {"GRADUATED", "DISCONTINUED", "SUSPENDED"}),
        "totalCourses": db.query(Course).count(),
    }


@admin_router.get("/students")
def list_students(query: str = "", status: str = "all", actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    if status not in {"all", "pending", "approved", "rejected"}:
        raise HTTPException(status_code=422, detail="Status must be all, pending, approved, or rejected.")
    needle = query.strip().lower()
    rows = []
    for user in db.scalars(select(User).where(User.role == "student").order_by(User.created_at.desc())).all():
        if status != "all" and user.approval_status != status:
            continue
        if needle and needle not in user.display_name.lower() and needle not in user.email.lower():
            continue
        rows.append(_student_row(db, user))
    return rows


@admin_router.get("/students/records")
def paged_students(query: str = "", status: str = "all", academicStatus: str = "all", regulationYear: int | None = None, admissionBatch: str = "", academicYear: int | None = None, semester: int | None = None, section: str = "", department: str = "", program: str = "", graduationYear: int | None = None, sort: str = "name", direction: str = "asc", page: int = 1, pageSize: int = 25, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    if status not in {"all", "pending", "approved", "rejected"}:
        raise HTTPException(status_code=422, detail="Status must be all, pending, approved, or rejected.")
    if academicStatus.upper() not in {"ALL", "PENDING_APPROVAL", "REJECTED", "ACTIVE", "GRADUATED", "DISCONTINUED", "SUSPENDED"}:
        raise HTTPException(status_code=422, detail="Academic status filter is not recognized.")
    sort_fields = {"name": "displayName", "institutionalId": "institutionalId", "regulationYear": "regulationYear", "admissionBatch": "admissionBatch", "graduationYear": "graduationYear", "semester": "semester", "academicStatus": "academicStatus", "createdAt": "createdAt"}
    if sort not in sort_fields:
        raise HTTPException(status_code=422, detail="Sort field is not recognized.")
    if direction not in {"asc", "desc"}:
        raise HTTPException(status_code=422, detail="Sort direction must be asc or desc.")
    page_size = min(max(pageSize, 1), 100)
    needle = query.strip().lower()
    records = []
    users = db.scalars(select(User).where(User.role == "student").order_by(User.created_at.desc())).all()
    for user in users:
        if status != "all" and user.approval_status != status:
            continue
        row = _student_row(db, user)
        if academicStatus.lower() != "all" and (row.get("academicStatus") or "").upper() != academicStatus.upper():
            continue
        if regulationYear is not None and row.get("regulationYear") != regulationYear:
            continue
        if admissionBatch.strip() and (row.get("admissionBatch") or "").casefold() != admissionBatch.strip().casefold():
            continue
        if graduationYear is not None and row.get("graduationYear") != graduationYear:
            continue
        if academicYear is not None and row.get("academicYear") != academicYear:
            continue
        if semester is not None and row.get("semester") != semester:
            continue
        if section.strip() and (row.get("section") or "").casefold() != section.strip().casefold():
            continue
        if department.strip() and (row.get("department") or "").casefold() != department.strip().casefold():
            continue
        if program.strip() and (row.get("program") or "").casefold() != program.strip().casefold():
            continue
        searchable = " ".join(str(row.get(key) or "") for key in ("displayName", "email", "institutionalId", "institutionalEmail", "rollNumber", "admissionBatch", "department", "program")).lower()
        if needle and needle not in searchable:
            continue
        records.append(row)
    field = sort_fields[sort]

    def sort_key(row: dict):
        value = row.get(field)
        if field in {"regulationYear", "graduationYear", "semester"}:
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0
        return str(value or "").casefold()

    with_value = [row for row in records if row.get(field) is not None]
    without_value = [row for row in records if row.get(field) is None]
    with_value.sort(key=sort_key, reverse=direction == "desc")
    records = with_value + without_value
    current_page = max(page, 1)
    start = (current_page - 1) * page_size
    return {"items": records[start:start + page_size], "page": current_page, "pageSize": page_size, "total": len(records)}


@admin_router.get("/registration-requests")
def registration_requests(status: str = "PENDING_APPROVAL", query: str = "", page: int = 1, pageSize: int = 25, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    normalized_status = status.upper()
    if normalized_status not in {"ALL", "PENDING_APPROVAL", "APPROVED", "REJECTED"}:
        raise HTTPException(status_code=422, detail="Registration request status is not recognized.")
    needle = query.strip().casefold()
    rows = db.scalars(select(RegistrationRequestRecord).order_by(RegistrationRequestRecord.submitted_at.desc())).all()
    results = []
    for row in rows:
        if normalized_status != "ALL" and row.status != normalized_status:
            continue
        search = " ".join((row.submitted_name, row.submitted_email, row.submitted_program, row.submitted_term, row.submitted_roll_number)).casefold()
        if needle and needle not in search:
            continue
        results.append({**_student_row(db, db.get(User, row.user_id)), "registrationRequestId": row.id, "registrationStatus": row.status, "submittedAt": row.submitted_at.isoformat(), "reviewedAt": row.reviewed_at.isoformat() if row.reviewed_at else None, "rejectionReason": row.rejection_reason})
    current_page = max(page, 1)
    page_size = min(max(pageSize, 1), 100)
    start = (current_page - 1) * page_size
    return {"items": results[start:start + page_size], "page": current_page, "pageSize": page_size, "total": len(results)}


def _notification_payload(db: Session, row: Notification, viewer_id: str, settings) -> dict:
    read = db.scalar(select(NotificationRead.id).where(NotificationRead.notification_id == row.id, NotificationRead.user_id == viewer_id)) is not None
    outbox = db.get(EmailOutbox, row.email_outbox_id) if row.email_outbox_id else db.scalar(select(EmailOutbox).where(EmailOutbox.event_key == row.event_key))
    if not settings.email_notifications_configured:
        email_status = "not_configured"
    else:
        email_status = outbox.status if outbox else "not_queued"
    return {
        "id": row.id,
        "kind": row.kind,
        "title": row.title,
        "body": row.body,
        "relatedUserId": row.related_user_id,
        "createdAt": row.created_at.isoformat(),
        "read": read,
        "emailStatus": email_status,
    }


@admin_router.get("/notifications")
def admin_notifications(actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db), settings=Depends(get_settings)):
    rows = db.scalars(select(Notification).where(Notification.audience_role == "administrator").order_by(Notification.created_at.desc()).limit(100)).all()
    items = [_notification_payload(db, row, actor.id, settings) for row in rows]
    unread_count = db.query(Notification).filter(
        Notification.audience_role == "administrator",
        ~Notification.id.in_(select(NotificationRead.notification_id).where(NotificationRead.user_id == actor.id)),
    ).count()
    return {"items": items, "unreadCount": unread_count, "emailConfigured": settings.email_notifications_configured}


@admin_router.post("/notifications/{notification_id}/read")
def mark_admin_notification_read(notification_id: str, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    row = db.get(Notification, notification_id)
    if row is None or row.audience_role != "administrator":
        raise HTTPException(status_code=404, detail="Notification not found.")
    exists = db.scalar(select(NotificationRead.id).where(NotificationRead.notification_id == row.id, NotificationRead.user_id == actor.id))
    if exists is None:
        db.add(NotificationRead(notification_id=row.id, user_id=actor.id))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
    return {"status": "read"}


@admin_router.post("/notifications/{notification_id}/retry-email", status_code=202)
def retry_notification_email(notification_id: str, request: Request, background_tasks: BackgroundTasks, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db), settings=Depends(get_settings)):
    if not settings.email_notifications_configured:
        raise HTTPException(status_code=503, detail="Email notifications are not configured. The in-app notification remains available.")
    notification = db.get(Notification, notification_id)
    if notification is None or notification.audience_role != "administrator":
        raise HTTPException(status_code=404, detail="Notification not found.")
    outbox = db.get(EmailOutbox, notification.email_outbox_id) if notification.email_outbox_id else db.scalar(select(EmailOutbox).where(EmailOutbox.event_key == notification.event_key))
    if outbox is None:
        raise HTTPException(status_code=404, detail="No email is queued for this notification.")
    if outbox.status == "sent":
        raise HTTPException(status_code=409, detail="This notification email has already been sent.")
    outbox.status = "pending"
    outbox.last_error = None
    db.commit()
    background_tasks.add_task(deliver_email_outbox, request.app.state.session_factory, settings, outbox.id)
    return {"status": "queued"}


@admin_router.get("/students/{student_id}")
def student_detail(student_id: str, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    user = _student_or_404(db, student_id)
    base = _student_row(db, user)
    enrollments = db.scalars(select(Enrollment).where(Enrollment.student_id == user.id)).all()
    subjects = []
    for enrollment in enrollments:
        course = db.get(Course, enrollment.course_id)
        if course:
            subjects.append({"courseId": course.id, "code": course.code, "name": course.name, "academicYear": course.academic_year, "semester": course.semester, "section": course.section})
    course_codes = {item["courseId"]: item["code"] for item in subjects}
    attendance = db.scalars(select(AttendanceRecord).where(AttendanceRecord.student_id == user.id).order_by(AttendanceRecord.session_date.desc())).all()
    marks = db.scalars(select(Mark).where(Mark.student_id == user.id)).all()
    buckets: dict[str, dict[str, int]] = {}
    for row in attendance:
        bucket = buckets.setdefault(row.course_id, {"attended": 0, "held": 0})
        bucket["held"] += 1
        if attended_record(row):
            bucket["attended"] += 1
    attendance_summary = []
    for item in subjects:
        bucket = buckets.get(item["courseId"], {"attended": 0, "held": 0})
        attendance_summary.append({**item, "attended": bucket["attended"], "held": bucket["held"], "percentage": round(bucket["attended"] / bucket["held"] * 100) if bucket["held"] else None})
    course_ids = [item["courseId"] for item in subjects]
    submissions = db.scalars(select(Submission).where(Submission.student_id == user.id).order_by(Submission.submitted_at.desc())).all()
    assignment_map = {}
    if submissions:
        assignment_map = {row.id: row for row in db.scalars(select(Assignment).where(Assignment.id.in_([item.assignment_id for item in submissions]))).all()}
    submission_rows = []
    for row in submissions:
        assignment = assignment_map.get(row.assignment_id)
        submission_rows.append({
            "assignmentId": row.assignment_id,
            "title": assignment.title if assignment else "",
            "courseCode": course_codes.get(assignment.course_id, "") if assignment else "",
            "dueAt": assignment.due_at.isoformat() if assignment else None,
            "submittedAt": row.submitted_at.isoformat(),
            "feedback": row.feedback,
            "reviewedAt": row.reviewed_at.isoformat() if row.reviewed_at else None,
            "fileName": row.file_name,
        })
    submitted_ids = {row.assignment_id for row in submissions}
    assignments = db.scalars(select(Assignment).where(Assignment.course_id.in_(course_ids), Assignment.status.in_(["published", "closed"])).order_by(Assignment.due_at.desc())).all() if course_ids else []
    assignment_rows = [{"id": row.id, "title": row.title, "courseCode": course_codes.get(row.course_id, ""), "dueAt": row.due_at.isoformat(), "status": row.status, "submitted": row.id in submitted_ids} for row in assignments]
    slots = db.scalars(select(TimetableSlot).where(TimetableSlot.course_id.in_(course_ids)).order_by(TimetableSlot.day, TimetableSlot.start_time)).all() if course_ids else []
    timetable = [{"courseCode": course_codes.get(row.course_id, ""), "day": row.day, "startTime": row.start_time, "endTime": row.end_time, "room": row.room} for row in slots]
    announcements = db.scalars(select(Announcement).where(Announcement.course_id.in_(course_ids), Announcement.status == "published").order_by(Announcement.published_at.desc())).all() if course_ids else []
    announcement_rows = [{"id": row.id, "title": row.title, "body": row.body, "courseCode": course_codes.get(row.course_id, ""), "category": row.category, "publishedAt": row.published_at.isoformat() if row.published_at else None} for row in announcements]
    notifications = db.scalars(select(Notification).where(Notification.recipient_user_id == user.id).order_by(Notification.created_at.desc()).limit(100)).all()
    notification_rows = [{"id": row.id, "kind": row.kind, "title": row.title, "body": row.body, "createdAt": row.created_at.isoformat(), "readAt": row.read_at.isoformat() if row.read_at else None} for row in notifications]
    entity_ids = [user.id]
    if base.get("registrationRequestId"):
        entity_ids.append(base["registrationRequestId"])
    history = db.scalars(select(AuditLog).where(AuditLog.entity_id.in_(entity_ids)).order_by(AuditLog.created_at.desc()).limit(100)).all()
    history_rows = [{"action": row.action, "entity": row.entity, "detail": row.detail, "at": row.created_at.isoformat()} for row in history]
    return {
        **base,
        "subjects": subjects,
        "attendanceSummary": attendance_summary,
        "attendance": [{"courseId": row.course_id, "courseCode": course_codes.get(row.course_id, ""), "date": str(row.session_date), "status": row.status, "recordedBy": row.recorded_by} for row in attendance],
        "marks": [{"courseId": row.course_id, "courseCode": course_codes.get(row.course_id, ""), "kind": row.kind, "score": row.score, "maxScore": row.max_score} for row in marks],
        "assignments": assignment_rows,
        "submissions": submission_rows,
        "timetable": timetable,
        "announcements": announcement_rows,
        "notifications": notification_rows,
        "history": history_rows,
    }


@admin_router.patch("/students/{student_id}/academic")
def update_student_academic(student_id: str, body: StudentAcademicPatch, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    user = _student_or_404(db, student_id)
    profile = db.get(StudentProfile, student_id)
    if profile is None:
        raise HTTPException(status_code=409, detail="Student academic profile is missing.")
    profile.program = body.program.strip()
    profile.department = body.department.strip()
    user.department = body.department.strip()
    profile.semester = body.semester
    profile.academic_year = (body.semester + 1) // 2
    profile.term = f"Semester {body.semester}"
    profile.section = body.section.strip()
    profile.admission_batch = body.admissionBatch.strip()
    profile.regulation_year = body.regulationYear
    audit(db, actor.id, "update_student_academic", "student_profile", user.id, f"regulation={profile.regulation_year}; batch={profile.admission_batch}; semester={profile.semester}; section={profile.section}")
    db.commit()
    db.refresh(user)
    return _student_row(db, user)


@admin_router.patch("/students/{student_id}/lifecycle")
def update_student_lifecycle(student_id: str, body: StudentLifecyclePatch, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    if body.status not in {"ACTIVE", "GRADUATED", "DISCONTINUED", "SUSPENDED"}:
        raise HTTPException(status_code=422, detail="Choose ACTIVE, GRADUATED, DISCONTINUED, or SUSPENDED.")
    user = _student_or_404(db, student_id)
    profile = db.get(StudentProfile, student_id)
    if profile is None:
        raise HTTPException(status_code=409, detail="Student academic profile is missing.")
    if user.approval_status != "approved":
        raise HTTPException(status_code=409, detail="Only approved students can receive a lifecycle status.")
    if body.status == "GRADUATED" and (body.graduationYear is None or body.graduationDate is None or not (body.finalProgram or profile.final_program or profile.program).strip()):
        raise HTTPException(status_code=422, detail="Graduation year, date, and final programme are required.")
    previous_status = profile.academic_status
    profile.academic_status = body.status
    if body.status == "GRADUATED":
        profile.graduation_year = body.graduationYear
        profile.graduation_date = body.graduationDate
        profile.final_program = (body.finalProgram or profile.program).strip()
    user.is_active = body.status == "ACTIVE"
    audit(db, actor.id, "update_student_lifecycle", "student_profile", user.id, f"{previous_status}->{body.status}; graduation_year={profile.graduation_year}; graduation_date={profile.graduation_date}; final_program={profile.final_program}")
    event_key = f"student-lifecycle:{user.id}:{body.status}:{secrets.token_hex(4)}"
    db.add(Notification(event_key=event_key, recipient_user_id=user.id, kind="student_status", title="Academic status updated", body=f"Your academic status is now {body.status}.", related_user_id=user.id))
    db.commit()
    return _student_row(db, user)


class ApprovalDecision(BaseModel):
    temporaryPassword: str | None = Field(default=None, min_length=1, max_length=128)


def _expired(value: datetime | None) -> bool:
    if value is None:
        return False
    now = utcnow()
    if value.tzinfo is None:
        now = now.replace(tzinfo=None)
    return value <= now


def _purge_expired_provisioning_cache() -> None:
    for request_id, (_, expires_at) in list(_provisioning_cache.items()):
        if _expired(expires_at):
            _provisioning_cache.pop(request_id, None)


def _credential_preview(db: Session, actor: User, student_id: str, settings, regenerate: bool = False) -> dict:
    _purge_expired_provisioning_cache()
    user = _student_or_404(db, student_id)
    profile = db.get(StudentProfile, user.id)
    registration = db.scalar(select(RegistrationRequestRecord).where(RegistrationRequestRecord.user_id == user.id))
    if user.approval_status != "pending" or registration is None or registration.status != "PENDING_APPROVAL":
        raise HTTPException(status_code=409, detail="Only a pending registration can receive a credential preview.")
    if profile is None or not all((profile.program.strip(), profile.term.strip(), profile.roll_number.strip(), profile.department.strip(), profile.section.strip())):
        raise HTTPException(status_code=422, detail="Complete the student's academic details before generating credentials.")
    institutional_email = profile.institutional_email or _proposed_institutional_email(db, user, settings, registration)
    temporary_password = generate_temporary_password()
    expires_at = utcnow() + timedelta(minutes=PROVISIONING_TTL_MINUTES)
    registration.provisioning_email = institutional_email
    registration.pending_password_hash = hash_password(temporary_password)
    registration.provisioning_expires_at = expires_at
    registration.credential_status = "READY"
    registration.credential_error = None
    _provisioning_cache[registration.id] = (temporary_password, expires_at)
    audit(db, actor.id, "regenerate_student_credentials" if regenerate else "preview_student_credentials", "registration_request", registration.id, f"institutional_email={institutional_email}")
    db.commit()
    return {
        "institutionalEmail": institutional_email,
        "temporaryPassword": temporary_password,
        "expiresAt": expires_at.isoformat(),
        "institutionEmailDomain": settings.institution_email_domain,
        "emailDomainConfigured": settings.institution_email_domain_configured,
        "smtpConfigured": settings.smtp_configured,
        "appUrlConfigured": bool(settings.app_url),
        "deliveryReady": bool(settings.smtp_configured and settings.app_url),
        "warning": "This temporary password is shown only in this provisioning response. It is not stored in plaintext and cannot be retrieved again.",
    }


@admin_router.post("/students/{student_id}/credentials/preview")
def preview_student_credentials(student_id: str, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db), settings=Depends(get_settings)):
    return _credential_preview(db, actor, student_id, settings, regenerate=False)


@admin_router.post("/students/{student_id}/credentials/regenerate")
def regenerate_student_credentials(student_id: str, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db), settings=Depends(get_settings)):
    return _credential_preview(db, actor, student_id, settings, regenerate=True)


def _approve_registration(db: Session, actor: User, student_id: str, provided_temporary_password: str | None, settings) -> dict:
    _purge_expired_provisioning_cache()
    user = _student_or_404(db, student_id)
    profile = db.get(StudentProfile, user.id)
    registration = db.scalar(select(RegistrationRequestRecord).where(RegistrationRequestRecord.user_id == user.id))
    pending_notification = db.scalar(select(Notification).where(Notification.event_key == f"registration:{user.id}:pending"))
    if pending_notification is not None and db.scalar(select(NotificationRead.id).where(NotificationRead.notification_id == pending_notification.id, NotificationRead.user_id == actor.id)) is None:
        db.add(NotificationRead(notification_id=pending_notification.id, user_id=actor.id))
    if user.approval_status != "pending" or registration is None or registration.status != "PENDING_APPROVAL":
        raise HTTPException(status_code=409, detail="This registration is no longer pending review.")
    if profile is None or not all((profile.program.strip(), profile.term.strip(), profile.roll_number.strip(), profile.department.strip(), profile.section.strip())):
        raise HTTPException(status_code=422, detail="Complete the student's academic details before approval.")

    provided = (provided_temporary_password or "").strip() or None
    pending_hash = registration.pending_password_hash
    pending_expired = _expired(registration.provisioning_expires_at)
    cached = _provisioning_cache.get(registration.id)
    if cached is not None and _expired(cached[1]):
        _provisioning_cache.pop(registration.id, None)
        cached = None

    generated_now = False
    if provided:
        if not pending_hash or pending_expired:
            raise HTTPException(status_code=409, detail="The temporary credential preview has expired. Generate a new temporary password.")
        if not verify_password(provided, pending_hash):
            raise HTTPException(status_code=409, detail="The temporary password does not match the current provisioning preview. Generate a new one.")
        credential_hash = pending_hash
        delivery_password = provided
    elif cached is not None and pending_hash and not pending_expired and verify_password(cached[0], pending_hash):
        credential_hash = pending_hash
        delivery_password = cached[0]
    else:
        delivery_password = generate_temporary_password()
        credential_hash = hash_password(delivery_password)
        generated_now = True

    claimed = db.execute(update(RegistrationRequestRecord).where(RegistrationRequestRecord.id == registration.id, RegistrationRequestRecord.status == "PENDING_APPROVAL").values(status="APPROVING"))
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="This registration is already being reviewed or has been decided.")
    try:
        institutional_email = profile.institutional_email or registration.provisioning_email or _proposed_institutional_email(db, user, settings, registration)
        if _email_in_use(db, institutional_email, exclude_user_id=user.id, exclude_request_id=registration.id):
            registration.provisioning_email = None
            institutional_email = _proposed_institutional_email(db, user, settings, registration)
        _assign_institutional_id(db, user)
        profile.institutional_email = institutional_email
        user.password_hash = credential_hash
        user.must_change_password = True
        user.password_changed_at = None
        user.token_version = int(user.token_version or 0) + 1
        user.approval_status = "approved"
        user.is_active = True
        profile.academic_status = "ACTIVE"
        auto_enroll_student_in_matching_courses(db, user.id)
        registration.status = "APPROVED"
        registration.reviewed_by = actor.id
        registration.reviewed_at = utcnow()
        registration.provisioning_email = institutional_email
        registration.pending_password_hash = None
        registration.provisioning_expires_at = None
        registration.provisioned_at = utcnow()
        registration.credential_status = "DELIVERY_PENDING"
        registration.credential_error = None
        registration.credential_attempts = 0
        registration.credential_sent_at = None
        audit(db, actor.id, "approve_student", "registration_request", registration.id, f"institutional_id={profile.institutional_id}; credential_delivery=pending")
        db.commit()
    except IntegrityError as error:
        db.rollback()
        _provisioning_cache.pop(registration.id, None)
        raise HTTPException(status_code=409, detail="Institutional identity could not be reserved. The request remains pending; review it again.") from error
    _provisioning_cache.pop(registration.id, None)

    delivered, error_code = send_temporary_credentials(
        settings,
        recipient=user.email,
        display_name=user.display_name,
        institutional_email=institutional_email,
        temporary_password=delivery_password,
    )
    registration = db.scalar(select(RegistrationRequestRecord).where(RegistrationRequestRecord.user_id == user.id))
    registration.credential_attempts = int(registration.credential_attempts or 0) + 1
    registration.credential_error = None if delivered else (error_code or "DELIVERY_FAILED")
    if delivered:
        registration.credential_status = "SENT"
        registration.credential_sent_at = utcnow()
        notification_body = (
            f"Your registration was approved. Your institutional ID is {profile.institutional_id} and your institutional email is {institutional_email}. "
            "A temporary password was sent to your registered personal email. Sign in and change it immediately."
        )
    else:
        registration.credential_status = "CONFIGURATION_ERROR" if error_code in {"SMTP_NOT_CONFIGURED", "APP_URL_NOT_CONFIGURED"} else "FAILED"
        notification_body = (
            f"Your registration was approved. Your institutional ID is {profile.institutional_id} and your institutional email is {institutional_email}. "
            "The credential email could not be delivered yet; contact the institution's administrator."
        )
    event_key = f"registration:{user.id}:approved"
    if db.scalar(select(Notification.id).where(Notification.event_key == event_key)) is None:
        db.add(Notification(event_key=event_key, recipient_user_id=user.id, kind="registration_approved", title="Student account approved", body=notification_body, related_user_id=user.id))
    if not delivered:
        db.add(Notification(
            event_key=f"credential-delivery:{registration.id}:{secrets.token_hex(4)}",
            audience_role="administrator",
            kind="credential_delivery_failed",
            title="Credential delivery needs attention",
            body=f"Credentials for {user.display_name} were provisioned but could not be emailed ({error_code}). Use secure retry to issue a fresh temporary password.",
            related_user_id=user.id,
        ))
    audit(db, actor.id, "credential_email_sent" if delivered else "credential_email_failed", "registration_request", registration.id, f"email={institutional_email}; error={error_code or 'none'}")
    db.commit()
    db.refresh(user)
    row = _student_row(db, user)
    row["credentialEmailStatus"] = registration.credential_status
    row["credentialEmailError"] = registration.credential_error
    if generated_now:
        row["temporaryPassword"] = delivery_password
    return row


def _reject_registration(db: Session, actor: User, student_id: str, reason: str, settings) -> tuple[dict, str | None]:
    user = _student_or_404(db, student_id)
    profile = db.get(StudentProfile, user.id)
    registration = db.scalar(select(RegistrationRequestRecord).where(RegistrationRequestRecord.user_id == user.id))
    pending_notification = db.scalar(select(Notification).where(Notification.event_key == f"registration:{user.id}:pending"))
    if pending_notification is not None and db.scalar(select(NotificationRead.id).where(NotificationRead.notification_id == pending_notification.id, NotificationRead.user_id == actor.id)) is None:
        db.add(NotificationRead(notification_id=pending_notification.id, user_id=actor.id))
    if user.approval_status == "rejected" or (registration and registration.status == "REJECTED"):
        raise HTTPException(status_code=409, detail="This registration has already been rejected.")
    pending_request = user.approval_status == "pending" and registration is not None and registration.status == "PENDING_APPROVAL"
    if pending_request:
        claimed = db.execute(update(RegistrationRequestRecord).where(RegistrationRequestRecord.id == registration.id, RegistrationRequestRecord.status == "PENDING_APPROVAL").values(status="REJECTING"))
        if claimed.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=409, detail="This registration is already being reviewed or has been decided.")
    user.approval_status = "rejected"
    user.is_active = False
    if profile and pending_request:
        profile.academic_status = "REJECTED"
    if registration and pending_request:
        registration.status = "REJECTED"
        registration.reviewed_by = actor.id
        registration.reviewed_at = utcnow()
        registration.rejection_reason = reason.strip() or None
        registration.credential_status = "NOT_PROVISIONED"
        registration.pending_password_hash = None
        registration.provisioning_expires_at = None
        registration.provisioning_email = None
        _provisioning_cache.pop(registration.id, None)
    decision_body = ("Your registration was not approved." if pending_request else "Your student account access was disabled by an administrator.") + (f" Reason: {reason.strip()}" if reason.strip() else " Contact the institution's administrator for more information.")
    event_key = f"registration:{user.id}:rejected:{registration.id}" if pending_request else f"account-access-revoked:{user.id}"
    outbox = queue_email(db, settings, event_key, user.email, "EduNexus student account update", decision_body)
    db.add(Notification(event_key=event_key, recipient_user_id=user.id, kind="registration_rejected" if pending_request else "student_access_revoked", title="Student registration update" if pending_request else "Student account update", body=decision_body, related_user_id=user.id, email_outbox_id=outbox.id if outbox else None))
    audit(db, actor.id, "reject_student" if pending_request else "revoke_student_access", "registration_request" if pending_request else "user", registration.id if pending_request else user.id, reason.strip()[:400])
    db.commit()
    db.refresh(user)
    return _student_row(db, user), (outbox.id if outbox else None)


@admin_router.post("/students/{student_id}/approve")
def approve_student(student_id: str, body: ApprovalDecision | None = None, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db), settings=Depends(get_settings)):
    return _approve_registration(db, actor, student_id, body.temporaryPassword if body else None, settings)


@admin_router.post("/students/{student_id}/reject")
def reject_student(student_id: str, request: Request, background_tasks: BackgroundTasks, body: RegistrationDecision | None = None, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db), settings=Depends(get_settings)):
    row, outbox_id = _reject_registration(db, actor, student_id, body.reason if body else "", settings)
    if outbox_id:
        background_tasks.add_task(deliver_email_outbox, request.app.state.session_factory, settings, outbox_id)
    return row


@admin_router.post("/registration-requests/{request_id}/approve")
def approve_registration_request(request_id: str, body: ApprovalDecision | None = None, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db), settings=Depends(get_settings)):
    registration = db.get(RegistrationRequestRecord, request_id)
    if registration is None:
        raise HTTPException(status_code=404, detail="Registration request not found.")
    return _approve_registration(db, actor, registration.user_id, body.temporaryPassword if body else None, settings)


@admin_router.post("/registration-requests/{request_id}/reject")
def reject_registration_request(request_id: str, request: Request, background_tasks: BackgroundTasks, body: RegistrationDecision | None = None, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db), settings=Depends(get_settings)):
    registration = db.get(RegistrationRequestRecord, request_id)
    if registration is None:
        raise HTTPException(status_code=404, detail="Registration request not found.")
    row, outbox_id = _reject_registration(db, actor, registration.user_id, body.reason if body else "", settings)
    if outbox_id:
        background_tasks.add_task(deliver_email_outbox, request.app.state.session_factory, settings, outbox_id)
    return row


@admin_router.post("/students/{student_id}/credentials/retry")
def retry_student_credentials(student_id: str, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db), settings=Depends(get_settings)):
    user = _student_or_404(db, student_id)
    registration = db.scalar(select(RegistrationRequestRecord).where(RegistrationRequestRecord.user_id == user.id))
    if user.approval_status != "approved" or registration is None or registration.status != "APPROVED":
        raise HTTPException(status_code=409, detail="Only an approved registration can receive credential delivery.")
    if not user.is_active:
        raise HTTPException(status_code=409, detail="This student account is inactive. Reactivate it before retrying credentials.")
    if not user.must_change_password:
        raise HTTPException(status_code=409, detail="This student has already completed password setup. Refusing to reset their password.")
    if not settings.smtp_configured or not settings.app_url:
        raise HTTPException(status_code=503, detail="Credential delivery is not configured. Set SMTP and APP_URL before retrying.")
    profile = db.get(StudentProfile, user.id)
    if profile is None or not profile.institutional_email:
        raise HTTPException(status_code=409, detail="The institutional email is missing; provision the account again.")
    temporary_password = generate_temporary_password()
    user.password_hash = hash_password(temporary_password)
    user.must_change_password = True
    user.password_changed_at = None
    user.token_version = int(user.token_version or 0) + 1
    registration.credential_status = "DELIVERY_PENDING"
    registration.credential_error = None
    registration.credential_sent_at = None
    audit(db, actor.id, "retry_student_credentials", "registration_request", registration.id, f"email={profile.institutional_email}")
    db.commit()
    delivered, error_code = send_temporary_credentials(
        settings,
        recipient=user.email,
        display_name=user.display_name,
        institutional_email=profile.institutional_email,
        temporary_password=temporary_password,
    )
    registration.credential_attempts = int(registration.credential_attempts or 0) + 1
    registration.credential_error = None if delivered else (error_code or "DELIVERY_FAILED")
    if delivered:
        registration.credential_status = "SENT"
        registration.credential_sent_at = utcnow()
    else:
        registration.credential_status = "CONFIGURATION_ERROR" if error_code in {"SMTP_NOT_CONFIGURED", "APP_URL_NOT_CONFIGURED"} else "FAILED"
        db.add(Notification(
            event_key=f"credential-delivery:{registration.id}:{secrets.token_hex(4)}",
            audience_role="administrator",
            kind="credential_delivery_failed",
            title="Credential delivery needs attention",
            body=f"A fresh temporary password for {user.display_name} could not be emailed ({error_code}). The student cannot sign in until delivery succeeds.",
            related_user_id=user.id,
        ))
    audit(db, actor.id, "credential_email_sent" if delivered else "credential_email_failed", "registration_request", registration.id, f"email={profile.institutional_email}; error={error_code or 'none'}; retry=true")
    db.commit()
    return {
        "status": "sent" if delivered else "failed",
        "institutionalEmail": profile.institutional_email,
        "credentialEmailStatus": registration.credential_status,
        "credentialEmailError": registration.credential_error,
        "message": "A fresh temporary password was emailed to the student's registered personal address." if delivered else "Delivery failed. The student cannot sign in until a secure retry succeeds.",
    }


def _set_student_active(db: Session, actor: User, student_id: str, active: bool) -> dict:
    user = _student_or_404(db, student_id)
    profile = db.get(StudentProfile, student_id)
    if active and profile is not None and profile.academic_status != "ACTIVE":
        raise HTTPException(status_code=409, detail="Only students with ACTIVE academic status can be reactivated. Update the academic lifecycle status first.")
    user.is_active = active
    audit(db, actor.id, "reactivate_student" if active else "deactivate_student", "user", user.id, user.email)
    db.commit()
    db.refresh(user)
    return _student_row(db, user)


@admin_router.post("/students/{student_id}/deactivate")
def deactivate_student(student_id: str, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    return _set_student_active(db, actor, student_id, False)


@admin_router.post("/students/{student_id}/reactivate")
def reactivate_student(student_id: str, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    return _set_student_active(db, actor, student_id, True)


@admin_router.patch("/courses/{course_id}")
def edit_course(course_id: str, body: CoursePatch, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found.")
    if body.facultyId is not None:
        faculty = db.get(User, body.facultyId)
        if faculty is None or faculty.role != "faculty" or not faculty.is_active:
            raise HTTPException(status_code=422, detail="Choose an active faculty account to teach this course.")
        if faculty.id != course.faculty_id:
            old_allocation = db.scalar(select(FacultyAllocation).where(FacultyAllocation.faculty_id == course.faculty_id, FacultyAllocation.course_id == course.id, FacultyAllocation.academic_year == course.academic_year, FacultyAllocation.section == course.section))
            if old_allocation:
                db.delete(old_allocation)
            if db.scalar(select(FacultyAllocation.id).where(FacultyAllocation.faculty_id == faculty.id, FacultyAllocation.course_id == course.id, FacultyAllocation.academic_year == course.academic_year, FacultyAllocation.section == course.section)) is None:
                _create_allocation(db, AllocationRequest(facultyId=faculty.id, courseId=course.id, academicYear=course.academic_year, semester=course.semester, section=course.section, academicTerm=course.term))
        course.faculty_id = faculty.id
    if body.name is not None:
        course.name = body.name.strip()
    if body.term is not None:
        course.term = body.term.strip()
    term_match = re.search(r"(?:semester|sem)\s*(\d+)", body.term or "", re.IGNORECASE)
    parsed_semester = min(max(int(term_match.group(1)), 1), 14) if term_match else course.semester
    new_semester = body.semester or parsed_semester
    new_year = body.academicYear or ((new_semester + 1) // 2)
    new_section = body.section.strip() if body.section is not None else course.section
    if new_year != (new_semester + 1) // 2:
        raise HTTPException(status_code=422, detail="Academic year must match the subject semester (two semesters per year).")
    if (new_semester, new_year, new_section) != (course.semester, course.academic_year, course.section):
        allocations = db.scalars(select(FacultyAllocation).where(FacultyAllocation.course_id == course.id)).all()
        for allocation in allocations:
            duplicate = db.scalar(select(FacultyAllocation.id).where(
                FacultyAllocation.id != allocation.id,
                FacultyAllocation.faculty_id == allocation.faculty_id,
                FacultyAllocation.academic_year == new_year,
                FacultyAllocation.course_id == course.id,
                FacultyAllocation.section == new_section,
            ))
            other_subjects = set(db.scalars(select(FacultyAllocation.course_id).where(
                FacultyAllocation.id != allocation.id,
                FacultyAllocation.faculty_id == allocation.faculty_id,
                FacultyAllocation.academic_year == new_year,
            ).distinct()).all())
            if duplicate or (course.id not in other_subjects and len(other_subjects) >= 2):
                raise HTTPException(status_code=422, detail="The updated academic scope would duplicate an allocation or exceed two subjects for a faculty member in that year.")
            allocation.academic_year = new_year
            allocation.semester = new_semester
            allocation.section = new_section
            allocation.academic_term = course.term
        course.semester = new_semester
        course.academic_year = new_year
        course.section = new_section
    if body.term is not None:
        for allocation in db.scalars(select(FacultyAllocation).where(FacultyAllocation.course_id == course.id)).all():
            allocation.academic_term = course.term
    if body.room is not None:
        course.room = body.room.strip()
    if body.department is not None:
        course.department = body.department.strip()
    audit(db, actor.id, "edit_course", "course", course.id, course.code)
    db.commit()
    db.refresh(course)
    return {**course_payload(db, course), "facultyId": course.faculty_id, "studentCount": db.query(Enrollment).filter(Enrollment.course_id == course.id).count()}


@admin_router.get("/courses/{course_id}/enrollments")
def course_enrollments(course_id: str, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    if db.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="Course not found.")
    rows = []
    for enrollment in db.scalars(select(Enrollment).where(Enrollment.course_id == course_id)).all():
        student = db.get(User, enrollment.student_id)
        if student is not None:
            rows.append({"studentId": student.id, "displayName": student.display_name, "email": student.email, "approvalStatus": student.approval_status})
    return sorted(rows, key=lambda item: item["displayName"])


@admin_router.get("/courses/{course_id}/attendance-summary")
def section_attendance_summary(course_id: str, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found.")
    summary = faculty_analytics(db, course)
    sessions = db.query(AttendanceRecord.session_date).filter(AttendanceRecord.course_id == course_id).distinct().count()
    return {**summary, "sessionCount": sessions}


@admin_router.delete("/courses/{course_id}/enrollments/{student_id}")
def remove_enrollment(course_id: str, student_id: str, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    enrollment = db.scalar(select(Enrollment).where(Enrollment.course_id == course_id, Enrollment.student_id == student_id))
    if enrollment is None:
        raise HTTPException(status_code=404, detail="Enrolment not found.")
    enrollment_id = enrollment.id
    db.delete(enrollment)
    audit(db, actor.id, "remove_enrollment", "enrollment", enrollment_id, f"{course_id} {student_id}")
    db.commit()
    return {"status": "removed"}


def _allocation_payload(db: Session, allocation: FacultyAllocation) -> dict:
    faculty = db.get(User, allocation.faculty_id)
    course = db.get(Course, allocation.course_id)
    return {
        "id": allocation.id,
        "facultyId": allocation.faculty_id,
        "facultyName": faculty.display_name if faculty else "Faculty",
        "courseId": allocation.course_id,
        "courseCode": course.code if course else "",
        "courseName": course.name if course else "",
        "academicYear": allocation.academic_year,
        "semester": allocation.semester,
        "section": allocation.section,
        "academicTerm": allocation.academic_term,
    }


@admin_router.get("/allocations")
def list_allocations(actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    rows = db.scalars(select(FacultyAllocation).order_by(FacultyAllocation.academic_year, FacultyAllocation.faculty_id, FacultyAllocation.course_id)).all()
    return [_allocation_payload(db, row) for row in rows]


@admin_router.post("/allocations", status_code=201)
def create_allocation(body: AllocationRequest, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    row = _create_allocation(db, body)
    audit(db, actor.id, "create_faculty_allocation", "faculty_allocation", row.id, f"year={row.academic_year} subject={row.course_id}")
    db.commit()
    db.refresh(row)
    return _allocation_payload(db, row)


@admin_router.patch("/allocations/{allocation_id}")
def edit_allocation(allocation_id: str, body: AllocationRequest, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    row = db.get(FacultyAllocation, allocation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Faculty allocation not found.")
    faculty = db.get(User, body.facultyId)
    course = db.get(Course, body.courseId)
    if faculty is None or faculty.role != "faculty" or not faculty.is_active or course is None:
        raise HTTPException(status_code=422, detail="Choose an active faculty member and an existing subject.")
    faculty_department = (faculty.department or "").casefold().strip()
    course_department = (course.department or "").casefold().strip()
    if course_department and not faculty_department:
        raise HTTPException(status_code=422, detail="Assign the faculty member to a department before allocating a departmental subject.")
    if faculty_department and course_department and faculty_department not in course_department and course_department not in faculty_department:
        raise HTTPException(status_code=422, detail="Faculty and subject departments are not compatible.")
    section = body.section.strip()
    if (body.academicYear, body.semester, section) != (course.academic_year, course.semester, course.section):
        raise HTTPException(status_code=422, detail="Academic year, semester, and section must match the subject assignment.")
    duplicate = db.scalar(select(FacultyAllocation.id).where(
        FacultyAllocation.id != row.id,
        FacultyAllocation.faculty_id == faculty.id,
        FacultyAllocation.academic_year == body.academicYear,
        FacultyAllocation.course_id == course.id,
        FacultyAllocation.section == section,
    ))
    if duplicate:
        raise HTTPException(status_code=409, detail="This faculty allocation already exists.")
    current_subjects = set(db.scalars(select(FacultyAllocation.course_id).where(
        FacultyAllocation.id != row.id,
        FacultyAllocation.faculty_id == faculty.id,
        FacultyAllocation.academic_year == body.academicYear,
    ).distinct()).all())
    if course.id not in current_subjects and len(current_subjects) >= 2:
        raise HTTPException(status_code=422, detail="A faculty member may teach at most two distinct subjects in one academic year.")
    row.faculty_id = faculty.id
    row.course_id = course.id
    row.academic_year = body.academicYear
    row.semester = body.semester
    row.section = section
    row.academic_term = body.academicTerm.strip() or course.term
    audit(db, actor.id, "edit_faculty_allocation", "faculty_allocation", row.id, f"year={row.academic_year} subject={row.course_id}")
    db.commit()
    db.refresh(row)
    return _allocation_payload(db, row)


@admin_router.delete("/allocations/{allocation_id}")
def delete_allocation(allocation_id: str, actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    row = db.get(FacultyAllocation, allocation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Faculty allocation not found.")
    audit(db, actor.id, "delete_faculty_allocation", "faculty_allocation", row.id, f"year={row.academic_year} subject={row.course_id}")
    db.delete(row)
    db.commit()
    return {"status": "removed"}


@admin_router.get("/audit")
def list_audit(actor: User = Depends(require_roles("administrator")), db: Session = Depends(get_db)):
    from app.models import AuditLog

    rows = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(100)).all()
    return [{
        "id": row.id,
        "actorId": row.actor_id,
        "action": row.action,
        "entity": row.entity,
        "entityId": row.entity_id,
        "detail": row.detail,
        "createdAt": row.created_at.isoformat() if isinstance(row.created_at, datetime) else str(row.created_at),
    } for row in rows]
