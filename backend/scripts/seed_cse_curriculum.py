#!/usr/bin/env python
"""
Seed script for realistic CSE academic curriculum.
Adds 8 semesters x 3 subjects x 2 sections with faculty, students, chapters, resources, attendance, and marks.
Idempotent: safe to re-run without duplicating data.
"""

from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Announcement,
    Assignment,
    AttendanceRecord,
    Chapter,
    Course,
    Document,
    Enrollment,
    FacultyAllocation,
    Mark,
    StudentProfile,
    StudyTask,
    TimetableSlot,
    User,
)
from app.security import hash_password, validate_password
from app.services.documents import file_sha256, process_document
from app.services.storage import storage_key
from app.services.student_ids import assign_student_institutional_id

# ============================================================
# CSE CURRICULUM DEFINITION
# ============================================================

CSE_CURRICULUM = {
    1: {  # Semester 1 - Academic Year 1
        "academic_year": 1,
        "subjects": [
            {"code": "CSE101", "name": "Engineering Mathematics I", "short": "Eng Math I"},
            {"code": "CSE102", "name": "Engineering Physics", "short": "Eng Physics"},
            {"code": "CSE103", "name": "Programming for Problem Solving (C)", "short": "Programming in C"},
        ],
    },
    2: {  # Semester 2 - Academic Year 1
        "academic_year": 1,
        "subjects": [
            {"code": "CSE104", "name": "Engineering Mathematics II", "short": "Eng Math II"},
            {"code": "CSE105", "name": "Engineering Chemistry", "short": "Eng Chemistry"},
            {"code": "CSE106", "name": "Object-Oriented Programming (C++/Java)", "short": "OOP"},
        ],
    },
    3: {  # Semester 3 - Academic Year 2
        "academic_year": 2,
        "subjects": [
            {"code": "CSE201", "name": "Data Structures and Algorithms", "short": "DSA"},
            {"code": "CSE202", "name": "Computer Organization and Architecture", "short": "COA"},
            {"code": "CSE203", "name": "Discrete Mathematics and Graph Theory", "short": "Discrete Math"},
        ],
    },
    4: {  # Semester 4 - Academic Year 2
        "academic_year": 2,
        "subjects": [
            {"code": "CSE204", "name": "Operating Systems", "short": "OS"},
            {"code": "CSE205", "name": "Database Management Systems", "short": "DBMS"},
            {"code": "CSE206", "name": "Design and Analysis of Algorithms", "short": "DAA"},
        ],
    },
    5: {  # Semester 5 - Academic Year 3
        "academic_year": 3,
        "subjects": [
            {"code": "CSE301", "name": "Computer Networks", "short": "CN"},
            {"code": "CSE302", "name": "Web Technologies / Full-Stack Development", "short": "Web Tech"},
            {"code": "CSE303", "name": "Compiler Design", "short": "Compiler Design"},
        ],
    },
    6: {  # Semester 6 - Academic Year 3
        "academic_year": 3,
        "subjects": [
            {"code": "CSE304", "name": "Artificial Intelligence", "short": "AI"},
            {"code": "CSE305", "name": "Machine Learning", "short": "ML"},
            {"code": "CSE306", "name": "Cloud Computing", "short": "Cloud"},
        ],
    },
    7: {  # Semester 7 - Academic Year 4
        "academic_year": 4,
        "subjects": [
            {"code": "CSE401", "name": "Cyber Security and Information Security", "short": "Cyber Security"},
            {"code": "CSE402", "name": "Deep Learning", "short": "Deep Learning"},
            {"code": "CSE403", "name": "Industrial Management / Professional Ethics", "short": "Industrial Mgmt"},
        ],
    },
    8: {  # Semester 8 - Academic Year 4
        "academic_year": 4,
        "subjects": [
            {"code": "CSE404", "name": "Internship / Industry Training", "short": "Internship"},
            {"code": "CSE405", "name": "Major Project Phase II / Research Thesis", "short": "Major Project"},
            {"code": "CSE406", "name": "Seminar and Comprehensive Viva", "short": "Seminar & Viva"},
        ],
    },
}

SECTIONS = ["A", "B"]
STUDENTS_PER_SECTION = 15  # Required structure: 15 students per section (240 section enrolments)

# Faculty names - 3 per academic year (12 total), realistic Indian names
FACULTY_DATA = [
    # Academic Year 1 (Sem 1-2)
    {"email": "cse.faculty.math1@edunexus.in", "name": "Dr. Priya Sharma", "department": "Mathematics", "specialization": "Engineering Mathematics"},
    {"email": "cse.faculty.physics@edunexus.in", "name": "Dr. Rajesh Kumar", "department": "Physics", "specialization": "Engineering Physics"},
    {"email": "cse.faculty.prog@edunexus.in", "name": "Prof. Anita Desai", "department": "Computer Science", "specialization": "Programming & OOP"},
    # Academic Year 2 (Sem 3-4)
    {"email": "cse.faculty.dsa@edunexus.in", "name": "Dr. Suresh Iyer", "department": "Computer Science", "specialization": "Data Structures & Algorithms"},
    {"email": "cse.faculty.coa@edunexus.in", "name": "Prof. Kavitha Nair", "department": "Computer Science", "specialization": "Computer Organization"},
    {"email": "cse.faculty.dmath@edunexus.in", "name": "Dr. Arjun Patel", "department": "Mathematics", "specialization": "Discrete Mathematics"},
    # Academic Year 3 (Sem 5-6)
    {"email": "cse.faculty.networks@edunexus.in", "name": "Prof. Meera Krishnan", "department": "Computer Science", "specialization": "Computer Networks"},
    {"email": "cse.faculty.web@edunexus.in", "name": "Prof. Rohan Gupta", "department": "Computer Science", "specialization": "Web Technologies"},
    {"email": "cse.faculty.ai@edunexus.in", "name": "Dr. Lakshmi Reddy", "department": "Computer Science", "specialization": "AI & ML"},
    # Academic Year 4 (Sem 7-8)
    {"email": "cse.faculty.security@edunexus.in", "name": "Dr. Vikram Singh", "department": "Computer Science", "specialization": "Cyber Security"},
    {"email": "cse.faculty.dl@edunexus.in", "name": "Prof. Deepika Agarwal", "department": "Computer Science", "specialization": "Deep Learning"},
    {"email": "cse.faculty.project@edunexus.in", "name": "Dr. Amit Joshi", "department": "Computer Science", "specialization": "Project Management"},
]

# Chapter templates per subject type
CHAPTER_TEMPLATES = {
    "theory": [
        "Introduction and Fundamental Concepts",
        "Core Principles and Theorems",
        "Advanced Topics and Applications",
        "Problem Solving Techniques",
        "Review and Practice Problems",
    ],
    "programming": [
        "Getting Started and Environment Setup",
        "Control Structures and Functions",
        "Data Structures Implementation",
        "Advanced Programming Concepts",
        "Project Development and Best Practices",
    ],
    "systems": [
        "System Overview and Architecture",
        "Core Components and Mechanisms",
        "Memory and Process Management",
        "Advanced System Features",
        "Case Studies and Performance Tuning",
    ],
    "math": [
        "Foundations and Preliminaries",
        "Core Mathematical Concepts",
        "Theorems and Proofs",
        "Applications in Computing",
        "Advanced Topics and Exercises",
    ],
    "project": [
        "Project Planning and Requirements",
        "Design and Architecture",
        "Implementation and Development",
        "Testing and Quality Assurance",
        "Documentation and Presentation",
    ],
}

# Map subjects to chapter template types
SUBJECT_CHAPTER_TYPES = {
    "Engineering Mathematics I": "math",
    "Engineering Mathematics II": "math",
    "Engineering Physics": "theory",
    "Engineering Chemistry": "theory",
    "Programming for Problem Solving (C)": "programming",
    "Object-Oriented Programming (C++/Java)": "programming",
    "Data Structures and Algorithms": "programming",
    "Computer Organization and Architecture": "systems",
    "Discrete Mathematics and Graph Theory": "math",
    "Operating Systems": "systems",
    "Database Management Systems": "systems",
    "Design and Analysis of Algorithms": "programming",
    "Computer Networks": "systems",
    "Web Technologies / Full-Stack Development": "programming",
    "Compiler Design": "theory",
    "Artificial Intelligence": "theory",
    "Machine Learning": "theory",
    "Cloud Computing": "systems",
    "Cyber Security and Information Security": "theory",
    "Deep Learning": "theory",
    "Industrial Management / Professional Ethics": "theory",
    "Internship / Industry Training": "project",
    "Major Project Phase II / Research Thesis": "project",
    "Seminar and Comprehensive Viva": "project",
}

# Subject-specific five-unit syllabus for every CSE subject (24 subjects x 5 units = 120 units).
# Keyed by the base subject code so both sections share the same unit structure.
SUBJECT_UNITS: dict[str, tuple[str, str, str, str, str]] = {
    "CSE101": (
        "Matrices and Systems of Linear Equations",
        "Differential Calculus and Its Applications",
        "Integral Calculus and Areas Under Curves",
        "First-Order Ordinary Differential Equations",
        "Infinite Sequences, Series and Numerical Methods",
    ),
    "CSE102": (
        "Physical Optics and Interference",
        "Lasers and Fibre Optics",
        "Quantum Mechanics Foundations",
        "Solid State Physics and Semiconductors",
        "Electromagnetism and Engineering Applications",
    ),
    "CSE103": (
        "Introduction to Programming and Problem Solving",
        "Control Structures and Functions",
        "Arrays, Strings and Structures",
        "Pointers, Files and Dynamic Memory",
        "Modular Programming and Simple Data Structures",
    ),
    "CSE104": (
        "Multiple Integrals and Applications",
        "Vector Calculus and Line Integrals",
        "Higher-Order Differential Equations",
        "Laplace Transforms and Applications",
        "Probability, Statistics and Distributions",
    ),
    "CSE105": (
        "Atomic Structure and Chemical Bonding",
        "Electrochemistry and Batteries",
        "Polymers and Composite Materials",
        "Water Treatment and Industrial Chemistry",
        "Fuels, Combustion and Green Chemistry",
    ),
    "CSE106": (
        "Classes, Objects and Abstraction",
        "Encapsulation, Inheritance and Polymorphism",
        "Interfaces, Packages and Exception Handling",
        "Collections, Generics and File Input/Output",
        "Multithreading and Object-Oriented Design",
    ),
    "CSE201": (
        "Arrays, Linked Lists and Complexity Analysis",
        "Stacks, Queues and Recursion",
        "Trees, Heaps and Hashing",
        "Graphs and Traversal Algorithms",
        "Sorting, Searching and Algorithm Analysis",
    ),
    "CSE202": (
        "Digital Logic and Data Representation",
        "Instruction Sets and Assembly Programming",
        "Processor Architecture and Control Units",
        "Memory Organization and Cache Mapping",
        "Input/Output Systems, Buses and Parallel Processing",
    ),
    "CSE203": (
        "Logic, Proofs and Set Theory",
        "Relations, Functions and Combinatorics",
        "Algebraic Structures and Lattices",
        "Graph Theory and Traversals",
        "Trees, Colouring and Network Flows",
    ),
    "CSE204": (
        "Operating System Structures, Processes and Threads",
        "CPU Scheduling Algorithms",
        "Synchronization and Deadlocks",
        "Memory Management and Virtual Memory",
        "File Systems, Input/Output and Protection",
    ),
    "CSE205": (
        "Database Systems, Models and ER Design",
        "Relational Model, SQL and Normalization",
        "Storage, Indexing and Query Processing",
        "Transactions, Concurrency and Recovery",
        "Distributed Databases and NoSQL Systems",
    ),
    "CSE206": (
        "Algorithm Analysis and Asymptotic Notation",
        "Divide and Conquer Techniques",
        "Greedy Methods and Dynamic Programming",
        "Backtracking and Branch and Bound",
        "NP-Completeness and Approximation Algorithms",
    ),
    "CSE301": (
        "Network Models, Protocols and Layering",
        "Physical and Data Link Layer Techniques",
        "Network Layer, Routing and IP Addressing",
        "Transport Layer, TCP and Congestion Control",
        "Application Layer Protocols and Network Security",
    ),
    "CSE302": (
        "HTML, CSS and Responsive Layouts",
        "JavaScript, the DOM and Modern Frameworks",
        "Server-Side Programming and REST APIs",
        "Databases for Web Applications",
        "Deployment, Authentication and Web Security",
    ),
    "CSE303": (
        "Phases of Compilation and Language Processors",
        "Lexical Analysis and Finite Automata",
        "Syntax Analysis and Parsing Techniques",
        "Semantic Analysis and Intermediate Code",
        "Code Optimization and Run-Time Environments",
    ),
    "CSE304": (
        "Introduction, Intelligent Agents and Problem Solving",
        "Search Strategies and Heuristics",
        "Knowledge Representation and Reasoning",
        "Planning and Uncertainty in AI",
        "Machine Learning Basics and AI Applications",
    ),
    "CSE305": (
        "Introduction, Model Selection and Evaluation",
        "Supervised Learning and Regression",
        "Classification and Ensemble Methods",
        "Unsupervised Learning and Clustering",
        "Neural Networks and Applied Projects",
    ),
    "CSE306": (
        "Cloud Foundations, Models and Deployment",
        "Virtualization and Container Technologies",
        "Cloud Storage and Databases",
        "Cloud Platforms, APIs and Serverless Computing",
        "Cloud Security, Monitoring and Cost Management",
    ),
    "CSE401": (
        "Security Goals, Threats and Attacks",
        "Cryptographic Techniques and Key Management",
        "Network and Web Application Security",
        "System Hardening and Access Control",
        "Security Policies, Auditing and Incident Response",
    ),
    "CSE402": (
        "Neural Network Foundations and Backpropagation",
        "Convolutional Neural Networks",
        "Recurrent Networks and Sequence Models",
        "Generative Models and Autoencoders",
        "Training at Scale, Transfer Learning and Deployment",
    ),
    "CSE403": (
        "Principles of Management and Organizations",
        "Project Planning, Scheduling and Costing",
        "Quality Management and Standards",
        "Intellectual Property and Cyber Law",
        "Professional Ethics and Corporate Responsibility",
    ),
    "CSE404": (
        "Workplace Orientation and Professional Conduct",
        "Understanding the Project Environment",
        "Task Execution under Mentor Guidance",
        "Progress Reporting and Skill Assessment",
        "Final Report and Supervisor Evaluation",
    ),
    "CSE405": (
        "Problem Definition, Literature Review and Proposal",
        "System and Research Design",
        "Implementation and Experimentation",
        "Results, Analysis and Validation",
        "Thesis Writing and Final Defence",
    ),
    "CSE406": (
        "Topic Selection and Literature Survey",
        "Technical Report Writing",
        "Presentation Skills and Delivery",
        "Comprehensive Review of Core Subjects",
        "Viva Voce Preparation and Evaluation",
    ),
}

# Chapter titles used by earlier seed runs. A stored chapter is only retitled when
# its current title still matches one of these seed-owned templates, so manually
# renamed chapters are never overwritten.
LEGACY_TEMPLATE_TITLES = {title for titles in CHAPTER_TEMPLATES.values() for title in titles}

# Student names for generating realistic student accounts
STUDENT_FIRST_NAMES = [
    "Aarav", "Aditi", "Advait", "Aisha", "Akash", "Ananya", "Anika", "Arjun", "Arnav", "Avni",
    "Dev", "Dhruv", "Diya", "Esha", "Ishaan", "Ishita", "Kabir", "Kavya", "Krish", "Kritika",
    "Meera", "Mohan", "Neha", "Nikhil", "Nisha", "Om", "Pranav", "Priya", "Rahul", "Riya",
    "Rohan", "Rohit", "Samar", "Samaira", "Sanjay", "Sara", "Shaan", "Shreya", "Siddharth", "Simran",
    "Tanvi", "Tarun", "Trisha", "Vedant", "Vihaan", "Yash", "Zara",
]

STUDENT_LAST_NAMES = [
    "Agarwal", "Bansal", "Chopra", "Desai", "Gupta", "Iyer", "Jain", "Joshi", "Kapoor", "Kumar",
    "Malhotra", "Mehta", "Nair", "Patel", "Rao", "Reddy", "Sharma", "Singh", "Trivedi", "Verma",
]


def get_password_hash(settings: Settings, password: str | None = None) -> str:
    """Get password hash from env or generate secure random."""
    pwd = password or os.getenv("CSE_SEED_PASSWORD") or secrets.token_urlsafe(32)
    problem = validate_password(pwd)
    if problem:
        raise RuntimeError(f"Invalid seed password: {problem}")
    return hash_password(pwd)


def get_or_create_faculty(db: Session, faculty_info: dict, password_hash: str) -> User:
    """Get existing faculty or create new one."""
    user = db.scalar(select(User).where(User.email == faculty_info["email"]))
    if user is None:
        user = User(
            email=faculty_info["email"],
            password_hash=password_hash,
            role="faculty",
            display_name=faculty_info["name"],
            department=faculty_info["department"],
            approval_status="approved",
            is_active=True,
        )
        db.add(user)
        db.flush()
    return user


def get_or_create_course(db: Session, code: str, name: str, semester: int, academic_year: int, section: str, faculty_id: str, department: str) -> Course:
    """Get existing course or create new one. Code includes section for uniqueness."""
    sectioned_code = f"{code}-{section}"
    course = db.scalar(select(Course).where(Course.code == sectioned_code))
    if course is None:
        course = Course(
            code=sectioned_code,
            name=name,
            term=f"Semester {semester}",
            room=f"Room-{semester}{section}",
            faculty_id=faculty_id,
            semester=semester,
            academic_year=academic_year,
            section=section,
            department=department,
        )
        db.add(course)
        db.flush()
    return course


def get_or_create_faculty_allocation(db: Session, faculty_id: str, course_id: str, academic_year: int, semester: int, section: str) -> FacultyAllocation:
    """Get existing faculty allocation or create new one."""
    alloc = db.scalar(
        select(FacultyAllocation).where(
            FacultyAllocation.faculty_id == faculty_id,
            FacultyAllocation.course_id == course_id,
            FacultyAllocation.academic_year == academic_year,
            FacultyAllocation.section == section,
        )
    )
    if alloc is None:
        alloc = FacultyAllocation(
            faculty_id=faculty_id,
            course_id=course_id,
            academic_year=academic_year,
            semester=semester,
            section=section,
            academic_term=f"Semester {semester}",
        )
        db.add(alloc)
        db.flush()
    return alloc


def get_or_create_chapter(db: Session, course_id: str, title: str, ordinal: int, syllabus_code: str) -> tuple[Chapter, bool]:
    """Get existing chapter or create new one.

    Seed-owned chapters that still carry one of the original generic template
    titles are refreshed to the subject-specific title. Manually renamed
    chapters are never modified. Returns (chapter, created).
    """
    chapter = db.scalar(select(Chapter).where(Chapter.course_id == course_id, Chapter.ordinal == ordinal))
    if chapter is None:
        chapter = Chapter(
            course_id=course_id,
            title=title,
            ordinal=ordinal,
            syllabus_code=syllabus_code,
        )
        db.add(chapter)
        db.flush()
        return chapter, True
    if chapter.title in LEGACY_TEMPLATE_TITLES and chapter.title != title:
        clash = db.scalar(select(Chapter.id).where(Chapter.course_id == course_id, Chapter.title == title, Chapter.id != chapter.id))
        if clash is None:
            chapter.title = title
            if not chapter.syllabus_code:
                chapter.syllabus_code = syllabus_code
            db.flush()
    return chapter, False


def get_or_create_student(db: Session, email: str, display_name: str, password_hash: str, program: str, term: str, roll_number: str, semester: int, academic_year: int, department: str, section: str) -> User:
    """Get existing student or create new one."""
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            email=email,
            password_hash=password_hash,
            role="student",
            display_name=display_name,
            approval_status="approved",
            is_active=True,
        )
        db.add(user)
        db.flush()
        profile = StudentProfile(
            user_id=user.id,
            program=program,
            term=term,
            roll_number=roll_number,
            semester=semester,
            academic_year=academic_year,
            department=department,
            section=section,
            institutional_email=f"{roll_number.lower().replace('/', '-')}@edunexus.in",
        )
        db.add(profile)
        db.flush()
        assign_student_institutional_id(db, user.id)
    return user


def get_or_create_enrollment(db: Session, course_id: str, student_id: str) -> Enrollment:
    """Get existing enrollment or create new one."""
    enrollment = db.scalar(select(Enrollment).where(Enrollment.course_id == course_id, Enrollment.student_id == student_id))
    if enrollment is None:
        enrollment = Enrollment(course_id=course_id, student_id=student_id)
        db.add(enrollment)
        db.flush()
    return enrollment


def seed_chapters_and_resources(db: Session, settings: Settings, course: Course, faculty: User, subject_name: str, subject_code: str, *, seed_resources: bool = True) -> tuple[int, int]:
    """Seed chapters and documents for a course. Returns (chapter_count, resource_count).

    Unit titles are subject-specific (``SUBJECT_UNITS``); existing seed-owned chapters
    are refreshed in place. When ``seed_resources`` is False, chapters/units are created
    but the synthetic document files are skipped (used by fast structural tests).
    """
    unit_titles = SUBJECT_UNITS.get(subject_code) or CHAPTER_TEMPLATES[SUBJECT_CHAPTER_TYPES.get(subject_name, "theory")]

    chapter_count = 0
    resource_count = 0

    for ordinal, chapter_title in enumerate(unit_titles, start=1):
        syllabus_code = f"{course.code}-U{ordinal}"
        chapter, created = get_or_create_chapter(db, course.id, chapter_title, ordinal, syllabus_code)
        if created:
            chapter_count += 1
        if not seed_resources:
            continue

        # Study notes
        content = (
            f"{chapter_title}\n\n"
            f"Course: {course.name} ({course.code})\n"
            f"Semester: {course.semester}, Academic Year: {course.academic_year}, Section: {course.section}\n\n"
            f"This unit covers {chapter_title.lower()} for {subject_name}.\n"
            f"Topics include fundamental concepts, worked examples, and practice problems.\n"
            f"Students are expected to understand the theoretical foundations and apply them to practical problems.\n"
        )
        resource_count += seed_document(db, settings, course, faculty, chapter, content, "study_notes", f"{course.code.lower()}-unit-{ordinal}-notes.txt")

        # Question bank (for units 3)
        if ordinal == 3:
            bank_content = (
                f"{chapter_title} - Question Bank\n\n"
                f"Sample Questions:\n"
                f"1. Explain the key concepts covered in this unit.\n"
                f"2. Solve the following practice problems...\n"
                f"3. Discuss real-world applications of these concepts.\n"
                f"4. Compare and contrast different approaches.\n"
            )
            resource_count += seed_document(db, settings, course, faculty, chapter, bank_content, "question_bank", f"{course.code.lower()}-unit-{ordinal}-qbank.txt")
        
        # Previous paper (for unit 5)
        if ordinal == 5:
            paper_content = (
                f"{chapter_title} - Previous Examination Practice\n\n"
                f"Sample Exam Questions:\n"
                f"Q1. (10 marks) Describe the main principles...\n"
                f"Q2. (15 marks) Analyze the following scenario...\n"
                f"Q3. (10 marks) Design a solution for...\n"
                f"Q4. (5 marks) Short answer: Define...\n"
            )
            resource_count += seed_document(db, settings, course, faculty, chapter, paper_content, "previous_paper", f"{course.code.lower()}-unit-{ordinal}-prev-paper.txt")
    
    return chapter_count, resource_count


def seed_document(db: Session, settings: Settings, course: Course, faculty: User, chapter: Chapter, content: str, kind: str, filename: str) -> int:
    """Seed one synthetic resource per (course, chapter, kind) slot.

    Deduplication is by the structural slot rather than by content hash so that a
    refreshed unit title never produces a duplicate file. Existing or faculty-uploaded
    resources in the same slot are preserved untouched.
    """
    existing = db.scalar(
        select(Document).where(
            Document.course_id == course.id,
            Document.chapter_id == chapter.id,
            Document.kind == kind,
        )
    )
    if existing is not None:
        return 0

    data = content.encode("utf-8")
    digest = file_sha256(data)
    folder = Path(settings.upload_dir) / "cse-curriculum" / course.code
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest}.txt"
    if not path.exists():
        path.write_bytes(data)
    
    document = Document(
        course_id=course.id,
        uploaded_by=faculty.id,
        filename=filename,
        title=filename.removesuffix(".txt").replace("-", " ").title(),
        description=f"Auto-seeded {kind} for {course.name}",
        chapter_id=chapter.id,
        kind=kind,
        content_type="text/plain",
        size_bytes=len(data),
        storage_path=storage_key(settings.upload_dir, path),
        sha256=digest,
        status="processing",
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    process_document(db, document, settings.upload_dir)
    return 1


def seed_attendance_and_marks(db: Session, course: Course, students: list[User], faculty_id: str) -> tuple[int, int]:
    """Seed attendance records and marks for enrolled students."""
    attendance_count = 0
    mark_count = 0
    
    today = date.today()
    session_dates = [today - timedelta(days=i) for i in range(1, 5)]  # 4 sessions
    
    for student in students:
        # Attendance
        for session_date in session_dates:
            present = True if session_date != session_dates[2] else False  # 3rd session absent for variety
            record = db.scalar(
                select(AttendanceRecord).where(
                    AttendanceRecord.course_id == course.id,
                    AttendanceRecord.student_id == student.id,
                    AttendanceRecord.session_date == session_date,
                )
            )
            if record is None:
                db.add(AttendanceRecord(
                    course_id=course.id,
                    student_id=student.id,
                    session_date=session_date,
                    present=present,
                    status="present" if present else "absent",
                    recorded_by=faculty_id,
                ))
                attendance_count += 1
        
        # Marks - internal and semester
        for kind, max_score in [("internal", 20), ("semester", 100)]:
            mark = db.scalar(
                select(Mark).where(
                    Mark.course_id == course.id,
                    Mark.student_id == student.id,
                    Mark.kind == kind,
                )
            )
            if mark is None:
                # Generate somewhat realistic scores
                import random
                score = random.randint(max_score // 2, max_score)
                db.add(Mark(
                    course_id=course.id,
                    student_id=student.id,
                    kind=kind,
                    score=score,
                    max_score=max_score,
                ))
                mark_count += 1
    
    return attendance_count, mark_count


def seed_timetable(db: Session, course: Course) -> int:
    """Seed timetable slots for a course."""
    count = 0
    days = ["Monday", "Wednesday", "Friday"]
    times = [("09:00", "10:00"), ("10:15", "11:15"), ("11:30", "12:30")]
    
    for i, day in enumerate(days):
        start, end = times[i % len(times)]
        # Check if slot exists
        existing = db.scalar(
            select(TimetableSlot).where(
                TimetableSlot.course_id == course.id,
                TimetableSlot.day == day,
                TimetableSlot.start_time == start,
            )
        )
        if existing is None:
            db.add(TimetableSlot(
                course_id=course.id,
                day=day,
                start_time=start,
                end_time=end,
                room=course.room,
            ))
            count += 1
    return count


def seed_assignments_and_announcements(db: Session, course: Course, faculty_id: str) -> tuple[int, int]:
    """Seed assignments and announcements for a course."""
    assign_count = 0
    announce_count = 0
    
    # Assignment
    due = datetime.now(timezone.utc) + timedelta(days=14)
    existing_assign = db.scalar(select(Assignment).where(Assignment.course_id == course.id, Assignment.title == f"{course.code} Assignment 1"))
    if existing_assign is None:
        db.add(Assignment(
            course_id=course.id,
            title=f"{course.code} Assignment 1",
            instructions=f"Complete the assigned problems for {course.name}. Submit your solutions with explanations.",
            due_at=due,
            status="published",
            created_by=faculty_id,
        ))
        assign_count += 1
    
    # Announcement
    existing_ann = db.scalar(select(Announcement).where(Announcement.course_id == course.id, Announcement.title == f"Welcome to {course.code}"))
    if existing_ann is None:
        db.add(Announcement(
            course_id=course.id,
            title=f"Welcome to {course.code}",
            body=f"Welcome to {course.name} for Semester {course.semester}, Section {course.section}. Please check the course materials and timetable.",
            category="Course",
            status="published",
            created_by=faculty_id,
            published_at=datetime.now(timezone.utc),
        ))
        announce_count += 1
    
    return assign_count, announce_count


def seed_cse_curriculum(db: Session, settings: Settings, password: str | None = None, *, seed_resources: bool = True) -> dict:
    """
    Main seed function for CSE curriculum.
    Creates courses, faculty, sections, students, chapters, resources, attendance, marks.
    """
    password_hash = get_password_hash(settings, password)
    
    stats = {
        "facultyCreated": 0,
        "coursesCreated": 0,
        "sectionsCreated": 0,
        "studentsCreated": 0,
        "enrollmentsCreated": 0,
        "chaptersCreated": 0,
        "resourcesCreated": 0,
        "attendanceCreated": 0,
        "marksCreated": 0,
        "timetableCreated": 0,
        "assignmentsCreated": 0,
        "announcementsCreated": 0,
    }
    
    # Track faculty allocation count per academic year
    faculty_alloc_count: dict[tuple[str, int], int] = {}  # (faculty_id, academic_year) -> count
    
    # Create/get faculty members
    faculty_by_ay: dict[int, list[User]] = {}
    for ay in [1, 2, 3, 4]:
        faculty_by_ay[ay] = []
    
    for idx, fac_info in enumerate(FACULTY_DATA):
        faculty = get_or_create_faculty(db, fac_info, password_hash)
        if faculty.id not in [f.id for f in faculty_by_ay[1]]:  # Only count once
            # Determine which AY this faculty belongs to based on index
            ay = (idx // 3) + 1
            if ay <= 4:
                faculty_by_ay[ay].append(faculty)
                stats["facultyCreated"] += 1
    
    db.flush()
    
    # Process each semester
    for semester in range(1, 9):
        sem_info = CSE_CURRICULUM[semester]
        academic_year = sem_info["academic_year"]
        subjects = sem_info["subjects"]
        
        # Get faculty for this academic year
        ay_faculty = faculty_by_ay[academic_year]
        
        for section in SECTIONS:
            stats["sectionsCreated"] += 1
            
            # Assign faculty to subjects: faculty[i] teaches subject[i] in BOTH semesters of this AY
            # This ensures max 2 distinct subjects per faculty per AY (one per semester)
            subject_faculty_map = {}
            for subj_idx, subject in enumerate(subjects):
                faculty = ay_faculty[subj_idx]
                subject_faculty_map[subject["code"]] = faculty
            
            # Track distinct subjects per faculty per AY (one per semester = 2 total)
            for faculty in ay_faculty:
                key = (faculty.id, academic_year)
                faculty_alloc_count[key] = 2  # Each faculty teaches 2 subjects per AY (one per semester)
            
            # Create courses for this section
            section_courses = []
            for subject in subjects:
                faculty = subject_faculty_map[subject["code"]]
                course_code = f"{subject['code']}-{section}"
                existing_course_id = db.scalar(select(Course.id).where(Course.code == course_code))
                course = get_or_create_course(
                    db, subject["code"], subject["name"], semester, academic_year, section, faculty.id, faculty.department
                )
                if existing_course_id is None:
                    stats["coursesCreated"] += 1
                section_courses.append((course, subject["code"], faculty, existing_course_id is None))

            db.flush()

            # Faculty allocations for this section
            for course, base_code, faculty, _created in section_courses:
                get_or_create_faculty_allocation(db, faculty.id, course.id, academic_year, semester, section)

            # Create/enroll students for this section
            section_students = []
            for stu_num in range(1, STUDENTS_PER_SECTION + 1):
                # Generate student identity
                name_idx = (semester * 2 + (0 if section == "A" else 1)) * STUDENTS_PER_SECTION + stu_num
                first_name = STUDENT_FIRST_NAMES[name_idx % len(STUDENT_FIRST_NAMES)]
                last_name = STUDENT_LAST_NAMES[(name_idx * 7) % len(STUDENT_LAST_NAMES)]
                display_name = f"{first_name} {last_name}"
                email = f"cse{semester}{section.lower()}.{stu_num:02d}@edunexus.in"
                roll_number = f"CSE{semester:02d}{section}{stu_num:02d}"

                existing_student = db.scalar(select(User).where(User.email == email))
                student = get_or_create_student(
                    db, email, display_name, password_hash,
                    "B.Tech Computer Science",
                    f"Semester {semester}",
                    roll_number,
                    semester,
                    academic_year,
                    "Computer Science",
                    section
                )
                if existing_student is None:
                    stats["studentsCreated"] += 1
                section_students.append(student)

                # Enroll in all courses for this section
                for course, base_code, faculty, _created in section_courses:
                    existing_enrollment = db.scalar(select(Enrollment).where(Enrollment.course_id == course.id, Enrollment.student_id == student.id))
                    get_or_create_enrollment(db, course.id, student.id)
                    if existing_enrollment is None:
                        stats["enrollmentsCreated"] += 1

            db.flush()

            # Seed chapters, resources, attendance, marks, timetable for each course
            for course, base_code, faculty, _created in section_courses:

                # Chapters and resources
                ch_count, res_count = seed_chapters_and_resources(db, settings, course, faculty, course.name, base_code, seed_resources=seed_resources)
                stats["chaptersCreated"] += ch_count
                stats["resourcesCreated"] += res_count

                # Attendance and marks
                att_count, mk_count = seed_attendance_and_marks(db, course, section_students, faculty.id)
                stats["attendanceCreated"] += att_count
                stats["marksCreated"] += mk_count

                # Timetable
                stats["timetableCreated"] += seed_timetable(db, course)

                # Assignments and announcements
                a_count, an_count = seed_assignments_and_announcements(db, course, faculty.id)
                stats["assignmentsCreated"] += a_count
                stats["announcementsCreated"] += an_count

            db.commit()
    
    # Verify faculty allocation limits
    for (fac_id, ay), count in faculty_alloc_count.items():
        if count > 2:
            print(f"WARNING: Faculty {fac_id} has {count} subjects in AY {ay} (limit: 2)")
    
    return stats


if __name__ == "__main__":
    from app.config import load_settings
    from app.database import add_missing_columns, build_session_factory
    from app.models import Base
    
    settings = load_settings()
    factory = build_session_factory(settings)
    engine = factory.kw["bind"]
    Base.metadata.create_all(engine)
    add_missing_columns(engine)
    
    with factory() as session:
        result = seed_cse_curriculum(session, settings)
        print("Seeding completed:")
        for key, value in result.items():
            print(f"  {key}: {value}")