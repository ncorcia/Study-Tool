"""Live answer grading via the Claude API."""

from .llm_utils import get_client, unwrap_stringified_field

MODEL = "claude-sonnet-5"

GRADING_TOOL = {
    "name": "submit_grade",
    "description": "Submit the grade and feedback for a student's answer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "graded_correct": {
                "type": "string",
                "enum": ["correct", "partial", "incorrect"],
            },
            "feedback": {
                "type": "string",
                "description": "2-3 sentence explanation of the grade.",
            },
        },
        "required": ["graded_correct", "feedback"],
    },
}

GRADING_PROMPT_TEMPLATE = """A student was asked: {question_text}
Key concept(s) the answer should demonstrate: {reference_answer}
Student's answer: {student_answer}

Grade this as correct, partial, or incorrect. A partial answer shows the right
direction but misses a key mechanism or uses imprecise reasoning. Give a 2-3
sentence explanation: if correct, briefly reinforce why; if partial or incorrect,
clearly explain the gap without just restating the definition."""


def grade_answer(question_text: str, reference_answer: str, student_answer: str) -> dict:
    """Call the Claude API to grade a student's answer against the reference answer."""
    client = get_client()
    message = client.messages.create(
        model=MODEL,
        max_tokens=500,
        tools=[GRADING_TOOL],
        tool_choice={"type": "tool", "name": "submit_grade"},
        messages=[
            {
                "role": "user",
                "content": GRADING_PROMPT_TEMPLATE.format(
                    question_text=question_text,
                    reference_answer=reference_answer,
                    student_answer=student_answer,
                ),
            }
        ],
    )
    block = next(b for b in message.content if b.type == "tool_use")
    result = dict(block.input)
    for key, value in list(result.items()):
        result[key] = unwrap_stringified_field(key, value)
    return result
