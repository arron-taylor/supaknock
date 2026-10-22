"""Shared evidence limits; tool counts and network budgets are separate bounds."""

MAX_SOURCES = 20
MAX_TOOL_SOURCE_BYTES = 4096
# Twenty complete serialized sources plus array framing, never clipped excerpts.
MAX_TOOL_EVIDENCE_BYTES = 96 * 1024
MAX_TOOL_RESULT_BYTES = 256 * 1024
AI_PROGRESS_STAGES = ('thinking', 'reading', 'writing', 'checking', 'refining')
