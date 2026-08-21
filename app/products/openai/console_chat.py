"""Console chat completion service — routes to console.x.ai/v1/responses.

通过 console.x.ai 端点访问 grok-4.3 / grok-4 等模型，
使用 grok.com SSO token 认证，免费账号可用。

与 chat.py 的区别：
- 不走 grok.com 的 app-chat SSE 端点
- 不消耗 grok.com 配额窗口
- 响应格式是标准 OpenAI Responses API SSE 事件流
- thinking 内容以 encrypted_content 形式返回（不可读，不透传）
"""

import asyncio
from typing import Any, AsyncGenerator

import orjson

from app.platform.logging.logger import logger
from app.platform.config.snapshot import get_config
from app.platform.errors import RateLimitError, UpstreamError
from app.platform.runtime.clock import now_s
from app.platform.tokens import estimate_prompt_tokens, estimate_tokens
from app.control.account.enums import FeedbackKind
from app.control.account.invalid_credentials import feedback_kind_for_error
from app.control.proxy.models import ProxyLease
from app.control.account.runtime import get_refresh_service
from app.control.model.cooldown import (
    ModelAdmission,
    admit_model,
    mark_model_success,
    mark_rate_limited,
    release_probe,
)
from app.control.model.registry import resolve as resolve_model
from app.dataplane.account.selector import current_strategy
from app.dataplane.reverse.protocol.xai_console_chat import (
    build_console_payload,
    ConsoleStreamAdapter,
    stream_console_chat,
)
from app.products._account_selection import reserve_account, selection_max_retries
from app.products._model_fallback import (
    cooldown_seconds,
    fallback_limit,
    jitter_ratio,
    max_cooldown_seconds,
    next_fallback_candidate,
    record_fallback,
)
from app.products.openai.chat import (
    _configured_retry_codes,
    _set_request_log_routing,
    _should_retry_upstream,
)
from ._format import (
    make_response_id,
    make_stream_chunk,
    make_chat_response,
    build_usage,
)


def _log_task_exception(task: "asyncio.Task") -> None:
    exc = task.exception() if not task.cancelled() else None
    if exc:
        logger.warning("background task failed: task={} error={}", task.get_name(), exc)


async def _quota_sync(token: str, mode_id: int) -> None:
    """Fire-and-forget: 成功调用后持久化配额扣减和 usage_use_count。

    Console 配额(mode_id=5)为本地管理，不依赖上游 API，
    无论 random/quota 策略都需要执行扣减和窗口重置。
    """
    try:
        if current_strategy() != "quota" and mode_id != 5:
            return
        svc = get_refresh_service()
        if svc:
            await svc.refresh_call_async(token, mode_id)
    except Exception as exc:
        logger.warning(
            "console quota sync failed: token={}... mode_id={} error={}",
            token[:10],
            mode_id,
            exc,
        )


async def _fail_sync(token: str, mode_id: int, exc: BaseException | None = None) -> None:
    """Fire-and-forget: 失败后持久化失败计数。"""
    try:
        svc = get_refresh_service()
        if svc:
            await svc.record_failure_async(token, mode_id, exc)
    except Exception as e:
        logger.warning(
            "console fail sync error: token={}... mode_id={} error={}",
            token[:10],
            mode_id,
            e,
        )


def _reasoning_effort_from_emit_think(emit_think: bool | None) -> str:
    """将 emit_think 标志映射到 console API 的 reasoning effort。"""
    if emit_think is False:
        return "none"
    return "low"  # 默认 low，节省 token


async def completions(
    *,
    model: str,
    messages: list[dict],
    stream: bool = True,
    emit_think: bool | None = None,
    reasoning_effort: str | None = None,
    temperature: float = 0.7,
    top_p: float = 0.95,
    proxy_lease: ProxyLease | None = None,
    force_token: str | None = None,
    model_fallbacks: tuple[str, ...] = (),
    request_log_routing: dict[str, Any] | None = None,
) -> dict | AsyncGenerator[str, None]:
    """Entry point for console.x.ai chat completions.

    Returns an async generator for streaming, or a dict for non-streaming.
    """
    cfg = get_config()
    effort = reasoning_effort or _reasoning_effort_from_emit_think(emit_think)
    timeout_s = cfg.get_float("chat.timeout", 120.0)
    max_retries = 0 if force_token else selection_max_retries()
    retry_codes = _configured_retry_codes(cfg)
    model_fallback_budget = fallback_limit(
        cfg,
        tuple(model_fallbacks),
        force_token=force_token,
    )
    max_attempts = max_retries + model_fallback_budget + 1
    response_id = make_response_id()

    logger.info(
        "console chat request: model={} stream={} messages={}",
        model, stream, len(messages),
    )

    from app.dataplane.account import _directory as _acct_dir
    if _acct_dir is None:
        raise RateLimitError("Account directory not initialised")
    directory = _acct_dir

    # ── Streaming path ────────────────────────────────────────────────────────
    if stream:
        async def _run_stream() -> AsyncGenerator[str, None]:
            excluded: list[str] = []
            account_retries = 0
            model_fallback_used = 0
            current_model = model
            for attempt in range(max_attempts):
                admission = admit_model(current_model)
                if admission is ModelAdmission.BLOCKED:
                    fallback = next_fallback_candidate(
                        tuple(model_fallbacks),
                        model_fallback_used,
                        model_fallback_budget,
                    )
                    if fallback is None:
                        raise RateLimitError(
                            f"Model {current_model!r} is cooling down and no fallback is available"
                        )
                    fallback_index, fallback_model = fallback
                    previous_model = current_model
                    current_model = fallback_model
                    model_fallback_used = fallback_index + 1
                    record_fallback(
                        request_log_routing,
                        from_model=previous_model,
                        to_model=current_model,
                        status=429,
                    )
                    logger.warning(
                        "model recovery probe busy; fallback: from={} to={}",
                        previous_model,
                        current_model,
                    )
                    continue
                current_spec = resolve_model(current_model)
                if current_spec is None:
                    if admission is ModelAdmission.PROBE:
                        release_probe(current_model)
                    raise RateLimitError(f"Model {current_model!r} is not available")
                acct, selected_mode_id = await reserve_account(
                    directory,
                    current_spec,
                    now_s_override=now_s(),
                    exclude_tokens=excluded or None,
                    only_token=force_token,
                )
                if acct is None:
                    if admission is ModelAdmission.PROBE:
                        release_probe(current_model)
                    raise RateLimitError("No available accounts for this model tier")

                token = acct.token
                _set_request_log_routing(
                    request_log_routing,
                    model=current_model,
                    token=token,
                    mode_id=selected_mode_id,
                    pool_id=acct.pool_id,
                )
                success = False
                fail_exc: BaseException | None = None
                _retry = False
                adapter = ConsoleStreamAdapter()
                stream_started = False

                try:
                    payload = build_console_payload(
                        messages=messages,
                        model=current_model,
                        temperature=temperature,
                        top_p=top_p,
                        reasoning_effort=effort,
                        stream=True,
                    )

                    try:
                        stream_kwargs = {"timeout_s": timeout_s}
                        if proxy_lease is not None:
                            stream_kwargs["proxy_lease"] = proxy_lease
                        async for event_type, data in stream_console_chat(
                            token,
                            payload,
                            **stream_kwargs,
                        ):
                            tokens = adapter.feed(event_type, data)
                            for tok in tokens:
                                chunk = make_stream_chunk(response_id, current_model, tok)
                                stream_started = True
                                yield f"data: {orjson.dumps(chunk).decode()}\n\n"

                        # 流结束，发送 final chunk
                        usage_data = adapter.usage
                        prompt_tokens = (
                            usage_data.get("input_tokens", 0) if usage_data else
                            estimate_prompt_tokens(messages)
                        )
                        completion_tokens = (
                            usage_data.get("output_tokens", 0) if usage_data else
                            estimate_tokens(adapter.full_text)
                        )
                        usage = build_usage(prompt_tokens, completion_tokens)
                        final = make_stream_chunk(
                            response_id, current_model, "", is_final=True
                        )
                        final["usage"] = usage
                        stream_started = True
                        yield f"data: {orjson.dumps(final).decode()}\n\n"
                        stream_started = True
                        yield "data: [DONE]\n\n"
                        success = True
                        logger.info(
                            "console chat stream completed: attempt={}/{} model={} tokens={}",
                            attempt + 1, max_attempts, current_model,
                            (usage_data or {}).get("total_tokens", "?"),
                        )

                    except UpstreamError as exc:
                        fail_exc = exc
                        if exc.status == 429 and model_fallback_budget > 0:
                            mark_rate_limited(
                                current_model,
                                cooldown_seconds(cfg),
                                max_cooldown_sec=max_cooldown_seconds(cfg),
                                retry_after_sec=getattr(exc, "retry_after_s", None),
                                jitter_ratio=jitter_ratio(cfg),
                            )
                        if (
                            _should_retry_upstream(exc, retry_codes)
                            or (exc.status == 429 and model_fallback_budget > 0)
                        ):
                            fallback = next_fallback_candidate(
                                tuple(model_fallbacks),
                                model_fallback_used,
                                model_fallback_budget,
                            )
                            if exc.status == 429 and fallback is not None and not stream_started:
                                fallback_index, fallback_model = fallback
                                previous_model = current_model
                                current_model = fallback_model
                                model_fallback_used = fallback_index + 1
                                record_fallback(
                                    request_log_routing,
                                    from_model=previous_model,
                                    to_model=current_model,
                                    status=exc.status,
                                )
                                _retry = True
                                logger.warning(
                                    "console chat model fallback: from={} to={} status={} token={}...",
                                    previous_model,
                                    current_model,
                                    exc.status,
                                    token[:8],
                                )
                            elif (
                                _should_retry_upstream(exc, retry_codes)
                                and account_retries < max_retries
                                and not stream_started
                            ):
                                account_retries += 1
                                _retry = True
                                logger.warning(
                                    "console chat retry: attempt={}/{} status={} token={}...",
                                    attempt + 1, max_attempts, exc.status, token[:8],
                                )
                        else:
                            logger.warning(
                                "console chat upstream failed: model={} status={} attempt={}/{}",
                                current_model, exc.status, attempt + 1, max_attempts,
                            )
                            raise
                        if not _retry:
                            raise

                finally:
                    await directory.release(acct)
                    kind = (
                        FeedbackKind.SUCCESS if success
                        else feedback_kind_for_error(fail_exc) if fail_exc
                        else FeedbackKind.SERVER_ERROR
                    )
                    await directory.feedback(token, kind, selected_mode_id, now_s_val=now_s())
                    if success:
                        asyncio.create_task(
                            _quota_sync(token, selected_mode_id)
                        ).add_done_callback(_log_task_exception)
                    else:
                        asyncio.create_task(
                            _fail_sync(token, selected_mode_id, fail_exc)
                        ).add_done_callback(_log_task_exception)
                    if success:
                        mark_model_success(current_model)
                    elif not (
                        isinstance(fail_exc, UpstreamError)
                        and fail_exc.status == 429
                    ):
                        release_probe(current_model)

                if success or not _retry:
                    return
                excluded.append(token)

        return _run_stream()

    # ── Non-streaming path ────────────────────────────────────────────────────
    excluded: list[str] = []
    account_retries = 0
    model_fallback_used = 0
    current_model = model
    for attempt in range(max_attempts):
        admission = admit_model(current_model)
        if admission is ModelAdmission.BLOCKED:
            fallback = next_fallback_candidate(
                tuple(model_fallbacks),
                model_fallback_used,
                model_fallback_budget,
            )
            if fallback is None:
                raise RateLimitError(
                    f"Model {current_model!r} is cooling down and no fallback is available"
                )
            fallback_index, fallback_model = fallback
            previous_model = current_model
            current_model = fallback_model
            model_fallback_used = fallback_index + 1
            record_fallback(
                request_log_routing,
                from_model=previous_model,
                to_model=current_model,
                status=429,
            )
            logger.warning(
                "model recovery probe busy; fallback: from={} to={}",
                previous_model,
                current_model,
            )
            continue
        current_spec = resolve_model(current_model)
        if current_spec is None:
            if admission is ModelAdmission.PROBE:
                release_probe(current_model)
            raise RateLimitError(f"Model {current_model!r} is not available")
        acct, selected_mode_id = await reserve_account(
            directory,
            current_spec,
            now_s_override=now_s(),
            exclude_tokens=excluded or None,
            only_token=force_token,
        )
        if acct is None:
            if admission is ModelAdmission.PROBE:
                release_probe(current_model)
            raise RateLimitError("No available accounts for this model tier")

        token = acct.token
        _set_request_log_routing(
            request_log_routing,
            model=current_model,
            token=token,
            mode_id=selected_mode_id,
            pool_id=acct.pool_id,
        )
        success = False
        fail_exc: BaseException | None = None
        _retry = False
        adapter = ConsoleStreamAdapter()

        try:
            payload = build_console_payload(
                messages=messages,
                model=current_model,
                temperature=temperature,
                top_p=top_p,
                reasoning_effort=effort,
                stream=True,  # 始终用流式，非流式在本地聚合
            )

            try:
                stream_kwargs = {"timeout_s": timeout_s}
                if proxy_lease is not None:
                    stream_kwargs["proxy_lease"] = proxy_lease
                async for event_type, data in stream_console_chat(
                    token,
                    payload,
                    **stream_kwargs,
                ):
                    adapter.feed(event_type, data)

                usage_data = adapter.usage
                prompt_tokens = (
                    usage_data.get("input_tokens", 0) if usage_data else
                    estimate_prompt_tokens(messages)
                )
                completion_tokens = (
                    usage_data.get("output_tokens", 0) if usage_data else
                    estimate_tokens(adapter.full_text)
                )
                usage = build_usage(prompt_tokens, completion_tokens)
                result = make_chat_response(
                    current_model,
                    adapter.full_text,
                    response_id=response_id,
                    usage=usage,
                )
                success = True
                logger.info(
                    "console chat non-stream completed: model={} tokens={}",
                    current_model, (usage_data or {}).get("total_tokens", "?"),
                )
                return result

            except UpstreamError as exc:
                fail_exc = exc
                if exc.status == 429 and model_fallback_budget > 0:
                    mark_rate_limited(
                        current_model,
                        cooldown_seconds(cfg),
                        max_cooldown_sec=max_cooldown_seconds(cfg),
                        retry_after_sec=getattr(exc, "retry_after_s", None),
                        jitter_ratio=jitter_ratio(cfg),
                    )
                if (
                    _should_retry_upstream(exc, retry_codes)
                    or (exc.status == 429 and model_fallback_budget > 0)
                ):
                    fallback = next_fallback_candidate(
                        tuple(model_fallbacks),
                        model_fallback_used,
                        model_fallback_budget,
                    )
                    if exc.status == 429 and fallback is not None:
                        fallback_index, fallback_model = fallback
                        previous_model = current_model
                        current_model = fallback_model
                        model_fallback_used = fallback_index + 1
                        record_fallback(
                            request_log_routing,
                            from_model=previous_model,
                            to_model=current_model,
                            status=exc.status,
                        )
                        _retry = True
                        logger.warning(
                            "console chat non-stream model fallback: from={} to={} status={} token={}...",
                            previous_model,
                            current_model,
                            exc.status,
                            token[:8],
                        )
                    elif _should_retry_upstream(exc, retry_codes) and account_retries < max_retries:
                        account_retries += 1
                        _retry = True
                        logger.warning(
                            "console chat non-stream retry: attempt={}/{} status={}",
                            attempt + 1,
                            max_attempts,
                            exc.status,
                        )
                    else:
                        raise
                else:
                    raise

        finally:
            await directory.release(acct)
            kind = (
                FeedbackKind.SUCCESS if success
                else feedback_kind_for_error(fail_exc) if fail_exc
                else FeedbackKind.SERVER_ERROR
            )
            await directory.feedback(token, kind, selected_mode_id, now_s_val=now_s())
            if success:
                asyncio.create_task(
                    _quota_sync(token, selected_mode_id)
                ).add_done_callback(_log_task_exception)
            else:
                asyncio.create_task(
                    _fail_sync(token, selected_mode_id, fail_exc)
                ).add_done_callback(_log_task_exception)
            if success:
                mark_model_success(current_model)
            elif not (
                isinstance(fail_exc, UpstreamError)
                and fail_exc.status == 429
            ):
                release_probe(current_model)
        if not success and _retry:
            excluded.append(token)

    raise RateLimitError("No available accounts after retries")


__all__ = ["completions"]
