"""OpenAI-compatible request schemas (Pydantic models)."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class MessageItem(BaseModel):
    role:         str
    content:      str | list[dict[str, Any]] | None = None
    tool_calls:   list[dict[str, Any]] | None       = None
    tool_call_id: str | None                        = None
    name:         str | None                        = None


class ImageConfig(BaseModel):
    n:               int | None = Field(1, ge=1, le=10)
    size:            str | None = "1024x1024"
    aspect_ratio:    str | None = None
    resolution:      str | None = None
    quality:         str | None = None
    response_format: str | None = None


class VideoConfig(BaseModel):
    seconds: int | None = 6
    size: Literal["720x1280", "1280x720", "1024x1024", "1024x1792", "1792x1024"] | None = "720x1280"
    resolution_name: Literal["480p", "720p"] | None = None
    preset: Literal["fun", "normal", "spicy", "custom"] | None = None


class ChatCompletionRequest(BaseModel):
    model:               str
    messages:            list[MessageItem]
    stream:              bool | None                = None
    reasoning_effort:    str | None                 = None
    temperature:         float | None               = 0.8
    top_p:               float | None               = 0.95
    image_config:        ImageConfig | None         = None
    video_config:        VideoConfig | None         = None
    tools:               list[dict[str, Any]] | None = None
    tool_choice:         str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None                = True
    max_tokens:          int | None                 = None


class ImageGenerationRequest(BaseModel):
    model:           str
    prompt:          str
    n:               int | None = Field(1, ge=1, le=10)
    size:            str | None = "1024x1024"
    aspect_ratio:    str | None = None
    resolution:      str | None = None
    quality:         str | None = None
    response_format: str | None = "url"
    stream:          bool | None = False
    partial_images:  int | None = Field(None, ge=0, le=4)


class VideoMediaInput(BaseModel):
    """JSON media reference used by the video compatibility routes."""

    url:     str | None = None
    file_id: str | None = None


class VideoGenerationRequest(BaseModel):
    """JSON video request shared by the generation-compatible route."""

    model:            str = "grok-imagine-video"
    prompt:           str
    duration:         int | None = None
    aspect_ratio:     str | None = None
    resolution:       str | None = None
    image:            VideoMediaInput | None = None
    reference_images: list[VideoMediaInput] | None = None
    reference_audios: list[dict[str, Any]] | None = None
    video:            VideoMediaInput | None = None


class ImageEditRequest(BaseModel):
    model:           str
    prompt:          str
    image:           str | list[str] | dict[str, Any] | list[dict[str, Any]]
    mask:            str | dict[str, Any] | None = None
    n:               int | None = Field(1, ge=1, le=10)
    size:            str | None = "1024x1024"
    aspect_ratio:    str | None = None
    resolution:      str | None = None
    quality:         str | None = None
    response_format: str | None = "url"
    stream:          bool | None = False
    partial_images:  int | None = Field(None, ge=0, le=4)


class ResponsesCreateRequest(BaseModel):
    """OpenAI Responses API — /v1/responses.

    Only model/input/instructions/stream/reasoning/temperature/top_p are acted on.
    All other fields are accepted and silently discarded.
    """
    model:                str
    input:                str | list[Any]
    instructions:         str | None           = None
    stream:               bool | None          = None
    reasoning:            dict[str, Any] | None = None
    temperature:          float | None         = None
    top_p:                float | None         = None
    # silently ignored
    max_output_tokens:    int | None            = None
    tools:                list[Any] | None      = None
    tool_choice:          Any | None            = None
    previous_response_id: str | None            = None
    store:                bool | None           = None
    metadata:             dict[str, Any] | None = None
    truncation:           str | None            = None
    parallel_tool_calls:  bool | None           = None
    include:              list[str] | None      = None
    background:           bool | None           = None

    class Config:
        extra = "ignore"


__all__ = [
    "MessageItem", "ImageConfig", "VideoConfig",
    "ChatCompletionRequest", "ImageGenerationRequest", "ImageEditRequest",
    "VideoMediaInput", "VideoGenerationRequest",
    "ResponsesCreateRequest",
]
