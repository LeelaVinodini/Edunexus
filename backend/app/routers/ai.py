from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import current_user, get_db
from app.models import ContentDraft, Conversation, Course, Message, User
from app.services.academic import (
    TUTOR_POLICY,
    answer_with_sources,
    attach_chapter_to_citations,
    conversation_history_text,
    enrolled_course_ids,
    exam_insights,
    faculty_can_access_course,
    faculty_course_ids,
    record_content_draft,
    retrieve_chunks,
    save_exchange,
    student_can_access_course,
)
from app.services.chapters import catalog_for_courses, resolve_chapter
from app.services.language import LanguageModel, LanguageModelError

router = APIRouter(prefix="/api/ai", tags=["ai"])
logger = logging.getLogger(__name__)


class TutorRequest(BaseModel):
    courseId: str
    message: str = Field(min_length=1, max_length=2000)
    conversationId: str | None = None
    chapterId: str | None = None


class SocraticTurn(BaseModel):
    role: str = Field(pattern="^(student|tutor)$")
    text: str = Field(min_length=1, max_length=2000)


class SocraticRequest(BaseModel):
    courseId: str
    topic: str = Field(default="", max_length=160)
    step: int = Field(ge=0, le=4)
    answer: str = Field(default="", max_length=4000)
    wantHint: bool = False
    chapterId: str | None = None
    history: list[SocraticTurn] = Field(default_factory=list, max_length=12)


# Guided-practice stages. Each stage is a seed question; the model adapts it to the student's answers.
SOCRATIC_PROMPTS = (
    "Before looking anything up, what do you already think is true about {topic}?",
    "Which point in this material best supports or challenges your last answer about {topic}?",
    "How would you explain {topic} to a classmate, using only what this material supports?",
    "Where could an understanding of {topic} go wrong in practice, and what would you check first?",
    "What real example shows {topic} in action, and why does it fit?",
)

SOCRATIC_POLICY = (
    "Ask exactly one question per turn. When the student has just answered, open with one brief, specific encouragement about that answer, "
    "then ask the next question. Never state the final answer, never give a full solution, and never do the reasoning for the student: "
    "guide them to discover it with hints and follow-up questions. "
    "Use the numbered course excerpts as the only source of subject facts, and place a [n] marker only where that exact excerpt supports the claim. "
    "Do not invent facts, examples, quotations, statistics, page numbers, chapter contents, or citations. "
    "If the excerpts do not cover the point, ask a question that checks the student's reasoning instead of asserting new facts. "
    "Keep the reply under 90 words, end with a question mark, and stay academically appropriate: no jokes, no role-play, and no praise for its own sake."
)

SOCRATIC_NO_MATCH = (
    "No passage in this material matched that yet, so nothing was invented. "
    "Try a question about a point this chapter actually covers, or choose another chapter."
)


class ContentRequest(BaseModel):
    courseId: str
    topic: str = Field(min_length=1, max_length=160)
    kind: str


class BloomLevel(str):
    pass


class AssessmentGenerateRequest(BaseModel):
    courseId: str
    chapterId: str | None = None
    topic: str = Field(min_length=1, max_length=160)
    bloomLevel: str = Field(pattern="^(remember|understand|apply|analyze|evaluate|create)$")
    questionCount: int = Field(ge=1, le=20, default=5)
    questionType: str = Field(pattern="^(mcq|short_answer|essay)$", default="mcq")


class VivaSessionRequest(BaseModel):
    courseId: str
    chapterId: str | None = None
    topic: str = Field(min_length=1, max_length=160)
    mode: str = Field(pattern="^(viva|interview)$", default="viva")


class VivaTurnRequest(BaseModel):
    sessionId: str
    answer: str = Field(min_length=1, max_length=4000)


class StudyPlanRequest(BaseModel):
    courseId: str | None = None


def _model(request: Request) -> LanguageModel:
    return request.app.state.language_model


def _can_use_course(db: Session, user: User, course_id: str) -> bool:
    if user.role == "student":
        return student_can_access_course(db, user.id, course_id)
    if user.role == "faculty":
        return faculty_can_access_course(db, user.id, course_id)
    return False


def _require_course(db: Session, user: User, course_id: str) -> Course:
    course = db.get(Course, course_id)
    if course is None or not _can_use_course(db, user, course_id):
        raise HTTPException(status_code=404, detail="Course not found.")
    return course


@router.get("/status")
def status(request: Request, user: User = Depends(current_user)):
    model = _model(request)
    return {
        "provider": model.name,
        "configured": model.configured,
        "message": "A language model is configured." if model.configured else "Language model credentials are not configured. Retrieval still runs, and answers quote matching course passages.",
    }


@router.get("/tutor/subjects")
def tutor_subjects(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role == "student":
        ids = enrolled_course_ids(db, user.id)
    elif user.role == "faculty":
        ids = faculty_course_ids(db, user.id)
    else:
        raise HTTPException(status_code=403, detail="You do not have access to this resource.")
    courses = [course for course_id in ids if (course := db.get(Course, course_id)) is not None]
    return catalog_for_courses(db, courses)


@router.post("/tutor")
def tutor(body: TutorRequest, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role not in {"student", "faculty"}:
        raise HTTPException(status_code=403, detail="You do not have access to this resource.")
    course = _require_course(db, user, body.courseId)
    model = _model(request)
    chapter = None
    if body.chapterId:
        chapter = resolve_chapter(db, course.id, body.chapterId)
        if chapter is None:
            raise HTTPException(status_code=404, detail="Chapter not found.")
        if not model.configured:
            raise HTTPException(status_code=503, detail="GEMINI_API_KEY is not configured on the API server. Add it to backend/.env, restart the API, and try again. The tutor cannot generate an explanation until then.")
    history = ""
    if body.conversationId:
        existing = db.get(Conversation, body.conversationId)
        if existing is None or existing.user_id != user.id or existing.course_id != course.id:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        history = conversation_history_text(db, existing.id)
    try:
        if chapter is None:
            chunks = retrieve_chunks(db, course.id, body.message)
            instruction = f"Question: {body.message}\n{TUTOR_POLICY}"
            if history:
                instruction = f"{instruction}\n\nPrior conversation:\n{history}"
        else:
            chunks = retrieve_chunks(db, course.id, f"{chapter['title']}\n{body.message}", document_id=chapter["documentId"], chunk_ids=chapter["chunkIds"])
            instruction = (
                f"Tutor the student on the chapter \"{chapter['title']}\" in {course.name}. Question: {body.message}. "
                "The chapter's excerpts are context for this question; answer the question itself and do not summarise the whole chapter. "
                f"{TUTOR_POLICY}"
            )
            if history:
                instruction = f"{instruction}\nPrior conversation:\n{history}"
        if chapter is not None and not chunks and not model.configured:
            result = {
                "message": "No passage in this chapter matched the question. No explanation was generated and no source was invented.",
                "citations": [],
                "provider": model.name,
                "draft": True,
                "grounded": False,
            }
        else:
            result = answer_with_sources(model, instruction, chunks)
            if chapter is not None and result["citations"]:
                result = {**result, "citations": attach_chapter_to_citations(result["citations"], chapter)}
    except HTTPException:
        raise
    except LanguageModelError as error:
        logger.warning("Gemini tutor request failed: %s", error)
        raise HTTPException(status_code=503, detail=f"{error} Your saved records were not changed.") from error
    except Exception:
        logger.exception("AI tutor failed unexpectedly")
        raise HTTPException(
            status_code=503,
            detail="The language model is unavailable. Your saved records were not changed.",
        )
    conversation_id = save_exchange(db, user.id, course.id, "tutor", body.message, result, body.conversationId)
    return {**result, "conversationId": conversation_id, "chapterId": body.chapterId}


@router.get("/conversations")
def conversations(courseId: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = select(Conversation).where(Conversation.user_id == user.id)
    if courseId:
        query = query.where(Conversation.course_id == courseId)
    rows = db.scalars(query.order_by(Conversation.created_at.desc())).all()
    return [{"id": row.id, "title": row.title, "mode": row.mode, "courseId": row.course_id, "createdAt": row.created_at.isoformat()} for row in rows]


@router.get("/conversations/{conversation_id}")
def conversation(conversation_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(Conversation, conversation_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    messages = db.scalars(select(Message).where(Message.conversation_id == row.id).order_by(Message.created_at)).all()
    return {
        "id": row.id,
        "title": row.title,
        "messages": [{"role": item.role, "content": item.content, "citations": json.loads(item.citations or "[]")} for item in messages],
    }


def _socratic_instruction(course: Course, chapter: dict | None, topic: str, body: SocraticRequest, step: int) -> str:
    lines = [
        f"Socratic practice session with a university student on \"{topic}\" in {course.name}"
        + (f", using only the chapter \"{chapter['title']}\"." if chapter else "."),
        f"This turn asks question {step + 1} of {len(SOCRATIC_PROMPTS)}; pitch the challenge at that stage.",
        f"Seed question for this stage: {SOCRATIC_PROMPTS[step].format(topic=topic)}",
    ]
    if body.wantHint:
        lines.append("The student asked for a hint, so add one short nudge toward the next step without revealing the answer.")
    if body.answer.strip():
        lines.append(f"The student's latest answer: {body.answer.strip()}")
    else:
        lines.append("The student has not answered yet: ask your opening question only.")
    if body.history:
        transcript = [f"{'Student' if turn.role == 'student' else 'Tutor'}: {turn.text.strip()[:400]}" for turn in body.history]
        lines.append("Session so far:\n" + "\n".join(transcript))
    lines.append(f"Policy: {SOCRATIC_POLICY}")
    return "\n".join(lines)


@router.post("/socratic")
def socratic(body: SocraticRequest, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role != "student":
        raise HTTPException(status_code=403, detail="You do not have access to this resource.")
    course = _require_course(db, user, body.courseId)
    model = _model(request)
    chapter = None
    if body.chapterId:
        chapter = resolve_chapter(db, course.id, body.chapterId)
        if chapter is None:
            raise HTTPException(status_code=404, detail="Chapter not found.")
    topic = body.topic.strip() or (chapter["title"] if chapter else "")
    if not topic:
        raise HTTPException(status_code=422, detail="Choose a chapter or enter a topic to practise.")
    step = min(body.step, len(SOCRATIC_PROMPTS) - 1)
    if chapter:
        chunks = retrieve_chunks(db, course.id, f"{chapter['title']}\n{topic}\n{body.answer}", document_id=chapter["documentId"], chunk_ids=chapter["chunkIds"])
    else:
        chunks = retrieve_chunks(db, course.id, f"{topic}\n{body.answer}")
    hint = None
    if body.wantHint:
        hint = chunks[0]["excerpt"].split("\n", 1)[0][:280] if chunks else "No course passage is available to hint from. Do not guess a fact that is not in the materials."
    question = SOCRATIC_PROMPTS[step].format(topic=topic)
    reflection = None
    citations: list[dict] = []
    grounded = bool(chunks)
    if not chunks:
        question = SOCRATIC_NO_MATCH if chapter else question
        grounded = False
    elif model.configured:
        try:
            result = answer_with_sources(model, _socratic_instruction(course, chapter, topic, body, step), chunks)
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(status_code=503, detail="The language model is unavailable. Your saved records were not changed.") from error
        question = result["message"]
        reflection = result
        citations = attach_chapter_to_citations(result["citations"], chapter) if chapter else result["citations"]
        grounded = result["grounded"]
    elif body.answer.strip():
        try:
            reflection = answer_with_sources(
                model,
                f"The student is studying {topic}. Their answer: {body.answer}. Respond with one guiding question and do not give the full solution.",
                chunks,
            )
        except Exception as error:
            raise HTTPException(status_code=503, detail="The language model is unavailable. Your saved records were not changed.") from error
        citations = attach_chapter_to_citations(reflection["citations"], chapter) if chapter else reflection["citations"]
    return {
        "step": step,
        "totalSteps": len(SOCRATIC_PROMPTS),
        "finished": step >= len(SOCRATIC_PROMPTS) - 1,
        "question": question,
        "hint": hint,
        "reflection": reflection,
        "citations": citations,
        "provider": model.name,
        "draft": True,
        "grounded": grounded,
        "groundedHint": bool(chunks) and body.wantHint,
        "chapterId": body.chapterId,
    }


@router.post("/content")
def content(body: ContentRequest, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role != "faculty":
        raise HTTPException(status_code=403, detail="You do not have access to this resource.")
    if body.kind not in {"lesson_outline", "practice_questions", "study_notes", "question_bank"}:
        raise HTTPException(status_code=422, detail="Choose a supported content type.")
    _require_course(db, user, body.courseId)
    chunks = retrieve_chunks(db, body.courseId, body.topic)
    instruction = (
        f"Create a {body.kind.replace('_', ' ')} about {body.topic}. "
        "Use only the excerpts. If they are insufficient, say what is missing. Label the result as a draft."
    )
    try:
        result = answer_with_sources(_model(request), instruction, chunks)
    except Exception as error:
        raise HTTPException(status_code=503, detail="The language model is unavailable. Your saved records were not changed.") from error
    draft = record_content_draft(db, user.id, body.courseId, body.topic.strip(), body.kind, result["message"], result["provider"])
    return {**result, "draftId": draft.id}


@router.get("/content")
def content_drafts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role != "faculty":
        raise HTTPException(status_code=403, detail="You do not have access to this resource.")
    rows = db.scalars(select(ContentDraft).where(ContentDraft.author_id == user.id).order_by(ContentDraft.created_at.desc())).all()
    return [{"id": row.id, "courseId": row.course_id, "topic": row.topic, "kind": row.kind, "body": row.body, "provider": row.provider, "createdAt": row.created_at.isoformat()} for row in rows]


@router.get("/exam-insights")
def insights(courseId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role not in {"student", "faculty"}:
        raise HTTPException(status_code=403, detail="You do not have access to this resource.")
    _require_course(db, user, courseId)
    return exam_insights(db, courseId)


BLOOM_LEVELS = (
    "remember",
    "understand",
    "apply",
    "analyze",
    "evaluate",
    "create",
)

BLOOM_DESCRIPTIONS = {
    "remember": "Recall facts, terms, basic concepts, and definitions. Keywords: define, list, identify, name, state.",
    "understand": "Explain ideas, concepts, or processes. Keywords: describe, explain, paraphrase, summarize, classify.",
    "apply": "Use knowledge in new situations. Keywords: solve, apply, demonstrate, compute, construct, use.",
    "analyze": "Break down information into parts, examine relationships. Keywords: analyze, compare, contrast, differentiate, organize.",
    "evaluate": "Justify a decision or course of action. Keywords: evaluate, assess, critique, judge, defend, justify.",
    "create": "Produce new or original work. Keywords: design, create, develop, formulate, propose, generate.",
}

BLOOM_POLICY = (
    "You are an assessment designer creating questions aligned to a specific level of Bloom's Taxonomy. "
    "The level is: {level}. {description} "
    "Create {count} {type} question(s) about \"{topic}\" using ONLY the provided course excerpts. "
    "For MCQs: provide 4 options (A-D) with exactly one correct answer. "
    "For short answer: specify expected key points in the answer. "
    "For essay: specify the expected structure and evaluation criteria. "
    "Each question must cite the supporting excerpt using [n] markers where that excerpt supports the question content. "
    "If excerpts are insufficient for the requested level, state what is missing and suggest a lower level. "
    "Output format: JSON array of questions. Do not include explanations outside the JSON."
)

VIVA_POLICY = (
    "You are conducting a {mode} for a university student on \"{topic}\". "
    "Use ONLY the provided course excerpts as the source of subject facts. "
    "Ask ONE question at a time. Wait for the student's answer before proceeding. "
    "After each answer: evaluate correctness, identify missing points, explain mistakes, suggest improvements. "
    "Cite supporting excerpts with [n] markers. Do not invent facts. "
    "If excerpts don't cover a point, say so and ask about what IS covered. "
    "After 5-7 questions, provide a summary: strengths, weaknesses, specific topics to review, overall readiness. "
    "Keep questions academic and rigorous. No jokes or role-play."
)

VIVA_INSTRUCTION_START = (
    "This is the start of a {mode} session on \"{topic}\". "
    "Welcome the student, state the topic scope, and ask your FIRST question. "
    "Do not provide the answer. Just ask the question."
)

VIVA_INSTRUCTION_CONTINUE = (
    "The student answered: \"{answer}\". "
    "Evaluate this answer against the course excerpts. "
    "Identify what is correct, what is missing, and any misconceptions. "
    "Then ask your NEXT question, or if this was the final question, provide the session summary."
)

VIVA_SUMMARY_TRIGGER = "If this appears to be the final question (you have asked 5-7 questions), provide the session summary instead of another question."


def _get_bloom_description(level: str) -> str:
    return BLOOM_DESCRIPTIONS.get(level, "Recall and basic understanding.")


def _validate_bloom_level(level: str) -> None:
    if level not in BLOOM_LEVELS:
        raise HTTPException(status_code=422, detail=f"Invalid Bloom level. Choose from: {', '.join(BLOOM_LEVELS)}")


def _build_bloom_instruction(level: str, topic: str, count: int, qtype: str) -> str:
    return BLOOM_POLICY.format(
        level=level,
        description=_get_bloom_description(level),
        count=count,
        type=qtype,
        topic=topic,
    )


def _build_viva_instruction(mode: str, topic: str, history: list[dict], chunks: list) -> str:
    is_start = len(history) == 0
    if is_start:
        base = VIVA_INSTRUCTION_START.format(mode=mode, topic=topic)
    else:
        last_answer = history[-1].get("answer", "")
        base = VIVA_INSTRUCTION_CONTINUE.format(mode=mode, topic=topic, answer=last_answer)
    base += f"\n{VIVA_SUMMARY_TRIGGER}"
    base += f"\n\nPolicy: {VIVA_POLICY.format(mode=mode, topic=topic)}"
    return base


@router.post("/assessment/generate")
def generate_assessment(body: AssessmentGenerateRequest, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Generate assessment questions aligned to a specific Bloom's Taxonomy level."""
    if user.role != "faculty":
        raise HTTPException(status_code=403, detail="You do not have access to this resource.")
    _validate_bloom_level(body.bloomLevel)
    _require_course(db, user, body.courseId)
    
    chapter = None
    if body.chapterId:
        chapter = resolve_chapter(db, body.courseId, body.chapterId)
        if chapter is None:
            raise HTTPException(status_code=404, detail="Chapter not found.")
    
    chunks = []
    if chapter:
        chunks = retrieve_chunks(
            db, body.courseId, body.topic, document_id=chapter["documentId"], chunk_ids=chapter["chunkIds"]
        )
    else:
        chunks = retrieve_chunks(db, body.courseId, body.topic)
    
    if not chunks:
        return {
            "questions": [],
            "message": "No relevant course passages were found for this topic. Cannot generate assessment without source material.",
            "grounded": False,
            "provider": _model(request).name,
        }
    
    if not _model(request).configured:
        raise HTTPException(status_code=503, detail="Language model not configured. Add GEMINI_API_KEY to backend/.env and restart.")
    
    try:
        instruction = _build_bloom_instruction(body.bloomLevel, body.topic, body.questionCount, body.questionType)
        result = answer_with_sources(_model(request), instruction, chunks)
        
        questions = []
        try:
            parsed = json.loads(result["message"])
            if isinstance(parsed, list):
                questions = parsed
            elif isinstance(parsed, dict) and "questions" in parsed:
                questions = parsed["questions"]
        except Exception:
            questions = [{"raw": result["message"], "note": "Could not parse as structured JSON; see raw output."}]
        
        return {
            "bloomLevel": body.bloomLevel,
            "topic": body.topic,
            "questionType": body.questionType,
            "questionCount": len(questions),
            "questions": questions,
            "citations": result["citations"],
            "grounded": result["grounded"],
            "provider": result["provider"],
        }
    except LanguageModelError as error:
        raise HTTPException(status_code=503, detail=f"{error} Your saved records were not changed.") from error
    except Exception:
        logger.exception("Assessment generation failed")
        raise HTTPException(status_code=503, detail="The language model is unavailable. Your saved records were not changed.")


@router.post("/viva/start")
def start_viva(body: VivaSessionRequest, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Start a new AI viva/interview practice session."""
    if user.role != "student":
        raise HTTPException(status_code=403, detail="Viva practice is for students only.")
    _require_course(db, user, body.courseId)
    
    chapter = None
    if body.chapterId:
        chapter = resolve_chapter(db, body.courseId, body.chapterId)
        if chapter is None:
            raise HTTPException(status_code=404, detail="Chapter not found.")
    
    chunks = []
    if chapter:
        chunks = retrieve_chunks(
            db, body.courseId, body.topic, document_id=chapter["documentId"], chunk_ids=chapter["chunkIds"]
        )
    else:
        chunks = retrieve_chunks(db, body.courseId, body.topic)
    
    if not chunks:
        return {
            "sessionId": "",
            "message": "No relevant course passages were found for this topic. Cannot start viva without source material.",
            "grounded": False,
            "provider": _model(request).name,
        }
    
    if not _model(request).configured:
        raise HTTPException(status_code=503, detail="Language model not configured. Add GEMINI_API_KEY to backend/.env and restart.")
    
    try:
        instruction = _build_viva_instruction(body.mode, body.topic, [], chunks)
        result = answer_with_sources(_model(request), instruction, chunks)
        
        session_id = save_exchange(db, user.id, body.courseId, f"viva_{body.mode}", f"Start viva on {body.topic}", result, None)
        
        return {
            "sessionId": session_id,
            "question": result["message"],
            "citations": result["citations"],
            "grounded": result["grounded"],
            "provider": result["provider"],
            "topic": body.topic,
            "mode": body.mode,
        }
    except LanguageModelError as error:
        raise HTTPException(status_code=503, detail=f"{error} Your saved records were not changed.") from error
    except Exception:
        logger.exception("Viva start failed")
        raise HTTPException(status_code=503, detail="The language model is unavailable. Your saved records were not changed.")


@router.post("/viva/continue")
def continue_viva(body: VivaTurnRequest, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Continue a viva session with the student's answer."""
    if user.role != "student":
        raise HTTPException(status_code=403, detail="Viva practice is for students only.")
    
    session = db.get(Conversation, body.sessionId)
    if session is None or session.user_id != user.id or not session.mode.startswith("viva_"):
        raise HTTPException(status_code=404, detail="Viva session not found.")
    
    course = db.get(Course, session.course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found.")
    
    messages = db.scalars(select(Message).where(Message.conversation_id == session.id).order_by(Message.created_at)).all()
    history = [{"role": m.role, "text": m.content} for m in messages]
    
    chunks = retrieve_chunks(db, course.id, f"{session.mode} {session.title}")
    
    if not _model(request).configured:
        raise HTTPException(status_code=503, detail="Language model not configured. Add GEMINI_API_KEY to backend/.env and restart.")
    
    try:
        instruction = _build_viva_instruction(session.mode.replace("viva_", ""), session.title.replace("Start viva on ", ""), history, chunks)
        result = answer_with_sources(_model(request), instruction, chunks)
        
        save_exchange(db, user.id, course.id, session.mode, body.answer, result, session.id)
        
        is_summary = "summary" in result["message"].lower() or "strengths" in result["message"].lower() or "weaknesses" in result["message"].lower()
        
        return {
            "sessionId": session.id,
            "response": result["message"],
            "citations": result["citations"],
            "grounded": result["grounded"],
            "provider": result["provider"],
            "isSummary": is_summary,
        }
    except LanguageModelError as error:
        raise HTTPException(status_code=503, detail=f"{error} Your saved records were not changed.") from error
    except Exception:
        logger.exception("Viva continue failed")
        raise HTTPException(status_code=503, detail="The language model is unavailable. Your saved records were not changed.")


@router.get("/viva/sessions")
def list_viva_sessions(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """List student's viva/interview practice sessions."""
    if user.role != "student":
        raise HTTPException(status_code=403, detail="Viva practice is for students only.")
    rows = db.scalars(select(Conversation).where(
        Conversation.user_id == user.id,
        Conversation.mode.like("viva_%")
    ).order_by(Conversation.created_at.desc())).all()
    return [{"id": row.id, "title": row.title, "mode": row.mode, "courseId": row.course_id, "createdAt": row.created_at.isoformat()} for row in rows]


@router.get("/viva/sessions/{session_id}")
def get_viva_session(session_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Get a specific viva session with full transcript."""
    if user.role != "student":
        raise HTTPException(status_code=403, detail="Viva practice is for students only.")
    session = db.get(Conversation, session_id)
    if session is None or session.user_id != user.id or not session.mode.startswith("viva_"):
        raise HTTPException(status_code=404, detail="Viva session not found.")
    messages = db.scalars(select(Message).where(Message.conversation_id == session.id).order_by(Message.created_at)).all()
    return {
        "id": session.id,
        "title": session.title,
        "mode": session.mode,
        "courseId": session.course_id,
        "createdAt": session.created_at.isoformat(),
        "messages": [{"role": m.role, "content": m.content, "citations": json.loads(m.citations or "[]")} for m in messages],
    }


@router.post("/study-plan")
def generate_study_plan(body: StudyPlanRequest, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Generate a personalized study plan based on attendance, marks, and available materials."""
    if user.role != "student":
        raise HTTPException(status_code=403, detail="Study plans are for students only.")
    
    if body.courseId:
        _require_course(db, user, body.courseId)
        course_ids = [body.courseId]
    else:
        course_ids = enrolled_course_ids(db, user.id)
    
    if not course_ids:
        return {
            "status": "no_enrolments",
            "message": "You are not enrolled in any courses.",
            "plan": [],
        }
    
    # Gather evidence: attendance, marks, topic coverage from materials
    evidence = []
    weak_areas = []
    strong_areas = []
    
    for cid in course_ids:
        course = db.get(Course, cid)
        if not course:
            continue
        
        # Attendance
        attendance = attendance_summary(db, user.id)
        for subj in attendance.get("subjects", []):
            if subj["courseId"] == cid:
                if subj["percentage"] < 75:
                    weak_areas.append({"course": course.name, "area": "attendance", "detail": f"Attendance at {subj['percentage']}% ({subj['attended']}/{subj['held']} sessions)"})
                elif subj["percentage"] >= 90:
                    strong_areas.append({"course": course.name, "area": "attendance", "detail": f"Strong attendance at {subj['percentage']}%"})
        
        # Marks
        marks = marks_summary(db, user.id)
        for subj in marks.get("subjects", []):
            if subj["courseId"] == cid:
                for kind in ("internal", "semester"):
                    if subj[kind] and subj[kind]["score"] is not None:
                        pct = round(subj[kind]["score"] / subj[kind]["maxScore"] * 100)
                        if pct < 60:
                            weak_areas.append({"course": course.name, "area": kind, "detail": f"{kind.capitalize()} score {pct}% ({subj[kind]['score']}/{subj[kind]['maxScore']})"})
                        elif pct >= 85:
                            strong_areas.append({"course": course.name, "area": kind, "detail": f"Strong {kind} score at {pct}%"})
        
        # Topic coverage from documents
        docs = db.scalars(select(Document).where(Document.course_id == cid, Document.status == "ready")).all()
        if docs:
            topics_covered = len(docs)
            evidence.append({"course": course.name, "type": "materials", "detail": f"{topics_covered} processed documents available"})
        else:
            weak_areas.append({"course": course.name, "area": "materials", "detail": "No processed study materials available"})
    
    if not _model(request).configured:
        # Return evidence-based plan without AI synthesis
        plan_items = []
        for w in weak_areas[:5]:
            plan_items.append({
                "priority": "high",
                "course": w["course"],
                "focus": w["area"],
                "reason": w["detail"],
                "action": f"Review {w['area']} materials and attend supplementary sessions for {w['course']}",
            })
        for s in strong_areas[:3]:
            plan_items.append({
                "priority": "maintain",
                "course": s["course"],
                "focus": s["area"],
                "reason": s["detail"],
                "action": f"Continue current approach; consider peer tutoring in {s['course']}",
            })
        
        return {
            "status": "evidence_only",
            "message": "Language model not configured; plan based on recorded evidence only.",
            "evidence": evidence,
            "weakAreas": weak_areas,
            "strongAreas": strong_areas,
            "plan": plan_items,
            "provider": _model(request).name,
        }
    
    # AI-enhanced plan
    try:
        instruction = (
            f"Create a personalized study plan for a student enrolled in: {', '.join(db.get(Course, cid).name for cid in course_ids if db.get(Course, cid))}. "
            f"Weak areas (evidence): {json.dumps(weak_areas[:5])}. "
            f"Strong areas: {json.dumps(strong_areas[:3])}. "
            f"Available materials: {json.dumps(evidence)}. "
            "Output a JSON array of plan items with fields: priority (high/medium/low/maintain), course, focus, reason, action, suggestedDuration. "
            "Be specific and actionable. Reference the evidence. Do not invent data."
        )
        result = answer_with_sources(_model(request), instruction, [])
        
        plan = []
        try:
            parsed = json.loads(result["message"])
            if isinstance(parsed, list):
                plan = parsed
        except Exception:
            plan = [{"raw": result["message"], "note": "Could not parse as structured JSON"}]
        
        return {
            "status": "ai_generated",
            "evidence": evidence,
            "weakAreas": weak_areas,
            "strongAreas": strong_areas,
            "plan": plan,
            "provider": result["provider"],
            "grounded": result["grounded"],
        }
    except LanguageModelError as error:
        raise HTTPException(status_code=503, detail=f"{error} Your saved records were not changed.") from error
    except Exception:
        logger.exception("Study plan generation failed")
        raise HTTPException(status_code=503, detail="The language model is unavailable. Your saved records were not changed.")


@router.get("/progress/{course_id}")
def get_course_progress(course_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Get progress tracking for a specific course over time."""
    if user.role != "student":
        raise HTTPException(status_code=403, detail="Progress tracking is for students only.")
    _require_course(db, user, course_id)
    
    attendance = attendance_summary(db, user.id)
    marks = marks_summary(db, user.id)
    
    course_attendance = next((s for s in attendance.get("subjects", []) if s["courseId"] == course_id), None)
    course_marks = next((s for s in marks.get("subjects", []) if s["courseId"] == course_id), None)
    
    # Get assessment history
    from app.models import Mark
    mark_history = db.scalars(select(Mark).where(Mark.course_id == course_id, Mark.student_id == user.id).order_by(Mark.id)).all()
    
    return {
        "courseId": course_id,
        "attendance": course_attendance,
        "marks": course_marks,
        "markHistory": [
            {"kind": m.kind, "score": m.score, "maxScore": m.max_score, "percentage": round(m.score / m.max_score * 100) if m.score is not None else None}
            for m in mark_history
        ],
        "hasData": course_attendance is not None or course_marks is not None,
    }
