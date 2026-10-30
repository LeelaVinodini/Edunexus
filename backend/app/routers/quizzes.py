from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.deps import get_db, require_roles
from app.models import Course, Quiz, QuizAttempt, QuizQuestion, User, utcnow
from app.services.academic import course_students, enrolled_course_ids, faculty_can_access_course, faculty_course_ids, student_can_access_course
from app.services.chapters import resolve_chapter

faculty_router = APIRouter(prefix="/api/faculty/quizzes", tags=["quizzes"])
student_router = APIRouter(prefix="/api/student/quizzes", tags=["quizzes"])

SUBMIT_GRACE = timedelta(seconds=30)


class QuizQuestionIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=1000)
    options: list[str] = Field(min_length=2, max_length=6)
    correctIndex: int = Field(ge=0, le=5)
    marks: int = Field(ge=1, le=10)


class QuizBody(BaseModel):
    courseId: str = Field(min_length=1, max_length=36)
    chapterId: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    dueAt: datetime
    timeLimitMinutes: int | None = Field(default=None, ge=1, le=300)
    questions: list[QuizQuestionIn] = Field(min_length=1, max_length=50)


class SubmitBody(BaseModel):
    answers: list[int | None] = Field(min_length=1, max_length=50)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _questions(db: Session, quiz_id: str) -> list[QuizQuestion]:
    return list(db.scalars(select(QuizQuestion).where(QuizQuestion.quiz_id == quiz_id).order_by(QuizQuestion.ordinal)).all())


def _max_score(questions: list[QuizQuestion]) -> int:
    return sum(question.marks for question in questions)


def _attempt(db: Session, quiz_id: str, student_id: str) -> QuizAttempt | None:
    return db.scalar(select(QuizAttempt).where(QuizAttempt.quiz_id == quiz_id, QuizAttempt.student_id == student_id))


def _closed_reason(quiz: Quiz, attempt: QuizAttempt | None, now: datetime) -> str | None:
    if now > _aware(quiz.due_at):
        return "The due date for this quiz has passed."
    if attempt is not None and quiz.time_limit_minutes and now > _aware(attempt.started_at) + timedelta(minutes=quiz.time_limit_minutes) + SUBMIT_GRACE:
        return "The time limit for this quiz has passed."
    return None


def _student_quiz_or_404(db: Session, user: User, quiz_id: str) -> Quiz:
    quiz = db.get(Quiz, quiz_id)
    if quiz is None or not student_can_access_course(db, user.id, quiz.course_id):
        raise HTTPException(status_code=404, detail="Quiz not found.")
    return quiz


def _quiz_row(quiz: Quiz, questions: list[QuizQuestion], enrolled: int, submitted: int) -> dict:
    return {
        "id": quiz.id,
        "courseId": quiz.course_id,
        "chapterId": quiz.chapter_id,
        "chapterTitle": quiz.chapter_title,
        "title": quiz.title,
        "dueAt": _aware(quiz.due_at).isoformat(),
        "timeLimitMinutes": quiz.time_limit_minutes,
        "questionCount": len(questions),
        "maxScore": _max_score(questions),
        "enrolledCount": enrolled,
        "submittedCount": submitted,
    }


@faculty_router.post("", status_code=201)
def create_quiz(body: QuizBody, user: User = Depends(require_roles("faculty")), db: Session = Depends(get_db)):
    course = db.get(Course, body.courseId)
    if course is None or not faculty_can_access_course(db, user.id, course.id):
        raise HTTPException(status_code=404, detail="Course not found.")
    chapter = resolve_chapter(db, course.id, body.chapterId)
    if chapter is None:
        raise HTTPException(status_code=422, detail="Choose a chapter from this course's processed materials.")
    for index, question in enumerate(body.questions, start=1):
        options = [option.strip() for option in question.options]
        if any(not option or len(option) > 300 for option in options):
            raise HTTPException(status_code=422, detail=f"Question {index} needs non-empty options of up to 300 characters.")
        if question.correctIndex >= len(options):
            raise HTTPException(status_code=422, detail=f"Question {index} marks an option that does not exist.")
    quiz = Quiz(
        course_id=course.id,
        chapter_id=body.chapterId,
        chapter_title=chapter["title"][:160],
        title=body.title.strip(),
        created_by=user.id,
        due_at=_aware(body.dueAt),
        time_limit_minutes=body.timeLimitMinutes,
    )
    db.add(quiz)
    db.flush()
    for ordinal, question in enumerate(body.questions, start=1):
        db.add(QuizQuestion(
            quiz_id=quiz.id,
            ordinal=ordinal,
            prompt=question.prompt.strip(),
            options=json.dumps([option.strip() for option in question.options]),
            correct_index=question.correctIndex,
            marks=question.marks,
        ))
    db.commit()
    db.refresh(quiz)
    return _quiz_row(quiz, _questions(db, quiz.id), len(course_students(db, course.id)), 0)


@faculty_router.get("")
def list_quizzes(courseId: str | None = None, user: User = Depends(require_roles("faculty")), db: Session = Depends(get_db)):
    ids = faculty_course_ids(db, user.id)
    if courseId:
        if courseId not in ids:
            raise HTTPException(status_code=404, detail="Course not found.")
        ids = [courseId]
    quizzes = db.scalars(select(Quiz).where(Quiz.course_id.in_(ids)).order_by(Quiz.due_at)).all() if ids else []
    rows = []
    for quiz in quizzes:
        submitted = db.query(QuizAttempt).filter(QuizAttempt.quiz_id == quiz.id, QuizAttempt.submitted_at.is_not(None)).count()
        rows.append(_quiz_row(quiz, _questions(db, quiz.id), len(course_students(db, quiz.course_id)), submitted))
    return rows


@faculty_router.get("/{quiz_id}/summary")
def quiz_summary(quiz_id: str, user: User = Depends(require_roles("faculty")), db: Session = Depends(get_db)):
    quiz = db.get(Quiz, quiz_id)
    if quiz is None or not faculty_can_access_course(db, user.id, quiz.course_id):
        raise HTTPException(status_code=404, detail="Quiz not found.")
    questions = _questions(db, quiz.id)
    max_score = _max_score(questions)
    attempts = {row.student_id: row for row in db.scalars(select(QuizAttempt).where(QuizAttempt.quiz_id == quiz.id)).all()}
    students = []
    percentages = []
    stats = {question.id: {"correct": 0, "answered": 0} for question in questions}
    for student in course_students(db, quiz.course_id):
        attempt = attempts.get(student["id"])
        if attempt is None:
            status = "not started"
        elif attempt.submitted_at is None:
            status = "in progress"
        else:
            status = "submitted"
            percentages.append(round(attempt.score / max_score * 100) if max_score else 0)
            for question, answer in zip(questions, json.loads(attempt.answers or "[]")):
                if answer is not None:
                    stats[question.id]["answered"] += 1
                    stats[question.id]["correct"] += int(answer == question.correct_index)
        students.append({
            "studentId": student["id"],
            "displayName": student["displayName"],
            "status": status,
            "score": attempt.score if attempt and attempt.submitted_at else None,
            "maxScore": max_score,
        })
    return {
        "quiz": _quiz_row(quiz, questions, len(students), len(percentages)),
        "meanPercent": round(sum(percentages) / len(percentages)) if percentages else None,
        "students": students,
        "questions": [
            {"id": question.id, "prompt": question.prompt, "marks": question.marks, "answered": stats[question.id]["answered"], "correct": stats[question.id]["correct"]}
            for question in questions
        ],
    }


@student_router.get("")
def student_quizzes(user: User = Depends(require_roles("student")), db: Session = Depends(get_db)):
    now = utcnow()
    rows = []
    course_ids = enrolled_course_ids(db, user.id)
    quizzes = db.scalars(select(Quiz).where(Quiz.course_id.in_(course_ids)).order_by(Quiz.due_at)).all() if course_ids else []
    for quiz in quizzes:
        questions = _questions(db, quiz.id)
        attempt = _attempt(db, quiz.id, user.id)
        if attempt is not None and attempt.submitted_at is not None:
            state = "submitted"
        elif _closed_reason(quiz, attempt, now):
            state = "closed"
        elif attempt is not None:
            state = "in progress"
        else:
            state = "not started"
        rows.append({**_quiz_row(quiz, questions, 0, 0), "state": state, "score": attempt.score if attempt and attempt.submitted_at else None})
    return rows


@student_router.get("/{quiz_id}")
def student_quiz(quiz_id: str, user: User = Depends(require_roles("student")), db: Session = Depends(get_db)):
    quiz = _student_quiz_or_404(db, user, quiz_id)
    questions = _questions(db, quiz.id)
    attempt = _attempt(db, quiz.id, user.id)
    payload = _quiz_row(quiz, questions, 0, 0)
    payload["attempt"] = None
    payload["questions"] = []
    if attempt is not None:
        payload["attempt"] = {"startedAt": _aware(attempt.started_at).isoformat(), "submitted": attempt.submitted_at is not None}
        if attempt.submitted_at is None:
            payload["questions"] = [_public_question(question) for question in questions]
        else:
            payload["result"] = _result(questions, attempt)
    return payload


@student_router.post("/{quiz_id}/start")
def start_quiz(quiz_id: str, user: User = Depends(require_roles("student")), db: Session = Depends(get_db)):
    quiz = _student_quiz_or_404(db, user, quiz_id)
    attempt = _attempt(db, quiz.id, user.id)
    if attempt is None:
        reason = _closed_reason(quiz, None, utcnow())
        if reason:
            raise HTTPException(status_code=409, detail=reason)
        attempt = QuizAttempt(quiz_id=quiz.id, student_id=user.id, started_at=utcnow(), answers="[]")
        db.add(attempt)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            attempt = _attempt(db, quiz.id, user.id)
            if attempt is None:
                raise HTTPException(status_code=409, detail="Please try starting the quiz again.")
        db.refresh(attempt)
    if attempt.submitted_at is not None:
        raise HTTPException(status_code=409, detail="This quiz has already been submitted.")
    return {"startedAt": _aware(attempt.started_at).isoformat(), "timeLimitMinutes": quiz.time_limit_minutes, "questions": [_public_question(q) for q in _questions(db, quiz.id)]}


@student_router.post("/{quiz_id}/submit")
def submit_quiz(quiz_id: str, body: SubmitBody, user: User = Depends(require_roles("student")), db: Session = Depends(get_db)):
    quiz = _student_quiz_or_404(db, user, quiz_id)
    attempt = _attempt(db, quiz.id, user.id)
    if attempt is None:
        raise HTTPException(status_code=409, detail="Start the quiz before submitting answers.")
    if attempt.submitted_at is not None:
        raise HTTPException(status_code=409, detail="This quiz has already been submitted.")
    now = utcnow()
    reason = _closed_reason(quiz, attempt, now)
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    questions = _questions(db, quiz.id)
    if len(body.answers) != len(questions):
        raise HTTPException(status_code=422, detail="Answer every question in the quiz.")
    for answer, question in zip(body.answers, questions):
        if answer is not None and not 0 <= answer < len(json.loads(question.options)):
            raise HTTPException(status_code=422, detail="One of the selected answers does not exist.")
    score = sum(question.marks for answer, question in zip(body.answers, questions) if answer == question.correct_index)
    update_result = db.execute(
        update(QuizAttempt)
        .where(QuizAttempt.id == attempt.id, QuizAttempt.submitted_at.is_(None))
        .values(submitted_at=now, answers=json.dumps(body.answers), score=score, max_score=_max_score(questions))
    )
    db.commit()
    if update_result.rowcount != 1:
        raise HTTPException(status_code=409, detail="This quiz has already been submitted.")
    db.refresh(attempt)
    return _result(questions, attempt)


def _public_question(question: QuizQuestion) -> dict:
    return {"id": question.id, "ordinal": question.ordinal, "prompt": question.prompt, "options": json.loads(question.options), "marks": question.marks}


def _result(questions: list[QuizQuestion], attempt: QuizAttempt) -> dict:
    answers = json.loads(attempt.answers or "[]")
    max_score = attempt.max_score or _max_score(questions)
    return {
        "score": attempt.score,
        "maxScore": max_score,
        "percent": round(attempt.score / max_score * 100) if max_score else 0,
        "questions": [
            {
                "id": question.id,
                "prompt": question.prompt,
                "options": json.loads(question.options),
                "selected": answer,
                "correctIndex": question.correct_index,
                "correct": answer == question.correct_index,
                "marks": question.marks,
            }
            for question, answer in zip(questions, answers)
        ],
    }
