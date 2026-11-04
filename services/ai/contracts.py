"""The domain contract contains no vendor SDK types."""
from dataclasses import dataclass, field
from pathlib import Path
import mimetypes
from typing import Any


@dataclass(frozen=True)
class Attachment:
    mime_type: str
    path: str | None = None
    data: bytes | None = field(default=None, repr=False)

    @classmethod
    def from_path(cls, path):
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return cls(mime_type=mime, path=str(path))

    @property
    def capability(self):
        if self.mime_type.startswith("image/"):
            return "image"
        if self.mime_type.startswith("audio/"):
            return "audio"
        if self.mime_type == "application/pdf":
            return "pdf"
        if self.mime_type.startswith("text/"):
            return "text"
        return "unsupported"

    def read(self):
        return Path(self.path).read_bytes() if self.path else self.data or b""


@dataclass(frozen=True)
class AIRequest:
    contents: tuple
    schema: type | None = None
    allow_image_sampling: bool = False
    # Trusted task instructions; never place uploaded student content here.
    system_instruction: str | None = None
    # Only populated by extended/super PDF batches; ordinary requests are unchanged.
    file_session: Any | None = None
    # Extended two-part PDFs start on the same configured model/credential,
    # then each request independently follows the normal cascade on failure.
    preferred_attempt: tuple[str, str, int] | None = None
    # Never upload an expanded PDF through the Gemini Files API.
    inline_only: bool = False
    # Only explicit extraction EOF may contain a valid empty questions array.
    allow_empty_questions: bool = False
    # Task policy, translated to the provider's supported wire format.
    reasoning_effort: str | None = None

    @property
    def capabilities(self):
        return {"text"} | {part.capability for part in self.contents if isinstance(part, Attachment)}


@dataclass
class AIResult:
    text: str
    parsed: Any = None
    input_tokens: int = 0
    output_tokens: int = 0
    thoughts_tokens: int = 0
    total_tokens: int = 0
    truncated: bool = False
    sampled: bool = False
    # Fixed provider completion code for diagnostics; never raw output text.
    finish_reason: str | None = None

    @property
    def usage(self):
        return {key: getattr(self, key) for key in
                ("input_tokens", "output_tokens", "thoughts_tokens", "total_tokens")}
