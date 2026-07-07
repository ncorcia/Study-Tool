"""Data models for the study tool, mirroring the SQLite schema."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Document:
    id: Optional[int]
    filename: str
    topic: str
    raw_text: str
    uploaded_at: str


@dataclass
class StudyGuide:
    id: Optional[int]
    document_id: int
    content: str
    generated_at: str


@dataclass
class Question:
    id: Optional[int]
    document_id: int
    topic: str
    question_text: str
    question_type: str  # 'calibration' | 'main' | 'followup_deeper' | 'followup_remedial'
    parent_question_id: Optional[int]
    reference_answer: str


@dataclass
class Session:
    id: Optional[int]
    topics: str
    started_at: str
    ended_at: Optional[str]
    total_questions: int
    score: Optional[float]


@dataclass
class Attempt:
    id: Optional[int]
    session_id: int
    question_id: int
    user_answer: str
    graded_correct: str  # 'correct' | 'partial' | 'incorrect'
    feedback: str
    answered_at: str
