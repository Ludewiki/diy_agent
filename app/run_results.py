"""Build and evolve durable, versioned Agent Run result snapshots."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping
import uuid

from pydantic import ValidationError

from .models import RunStatus, utc_now
from .schemas import (
    ComponentStatus,
    ContextUsageSnapshot,
    PlanningRequestSnapshot,
    ResultComponent,
    ResultComponents,
    ResultSource,
    ResultStatus,
    ResultWarning,
    RunResultV1,
)


def _dump(result: RunResultV1) -> dict[str, Any]:
    return result.model_dump(mode="json")


def _terminal_component_status(run_status: str | None) -> ComponentStatus:
    if run_status in {
        RunStatus.SUCCEEDED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
    }:
        return ComponentStatus.UNAVAILABLE
    return ComponentStatus.PENDING


def _empty_components(run_status: str | None = None) -> ResultComponents:
    status = _terminal_component_status(run_status)
    return ResultComponents(
        weather=ResultComponent(status=status),
        guide=ResultComponent(status=status),
        route=ResultComponent(status=status),
    )


def _status_from_run(run_status: str | None) -> ResultStatus:
    if run_status in {RunStatus.FAILED.value, RunStatus.CANCELLED.value}:
        return ResultStatus.FAILED
    # A legacy successful Run has no component-level evidence, so it is partial.
    return ResultStatus.PARTIAL


def _bounded_text(value: Any, max_length: int, default: str = "") -> str:
    text = str(value if value is not None else default)
    return text[:max_length]


def _optional_bounded_text(value: Any, max_length: int) -> str | None:
    text = _bounded_text(value, max_length).strip()
    return text or None


def _legacy_sources(value: Any, fetched_at: datetime) -> list[ResultSource]:
    sources: list[ResultSource] = []
    for item in (value if isinstance(value, list) else [])[:100]:
        if not isinstance(item, Mapping):
            continue
        title = _bounded_text(item.get("title") or "历史引用", 500)
        url = _optional_bounded_text(item.get("url"), 2000)
        sources.append(
            ResultSource(
                provider="legacy",
                title=title,
                url=url,
                purpose="guide",
                fetched_at=fetched_at,
            )
        )
    return sources


def coerce_run_result(
    value: dict[str, Any] | None,
    *,
    run_status: str | None = None,
    generated_at: datetime | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    """Return a valid V1 result, upgrading legacy or incomplete JSON safely."""
    timestamp = generated_at or utc_now()
    if isinstance(value, dict) and value.get("schema_version") == "1.0":
        try:
            result = RunResultV1.model_validate(value)
            if run_status in {RunStatus.FAILED.value, RunStatus.CANCELLED.value}:
                result.result_status = ResultStatus.FAILED
            return _dump(result)
        except ValidationError:
            # Corrupt historical JSON must not break the Run query endpoint.
            pass

    legacy = value if isinstance(value, dict) else {}
    warnings: list[ResultWarning] = []
    if error_message:
        warnings.append(
            ResultWarning(
                code=_bounded_text(error_code or "RUN_FAILED", 100),
                message=_bounded_text(error_message, 2000),
                severity="error",
                scope="agent",
            )
        )
    result = RunResultV1(
        result_status=_status_from_run(run_status),
        generated_at=timestamp,
        assistant_answer=str(
            legacy.get("assistant_answer") or legacy.get("answer") or ""
        ),
        sources=_legacy_sources(
            legacy.get("sources") or legacy.get("reference"), timestamp
        ),
        warnings=warnings,
        components=_empty_components(run_status),
    )
    return _dump(result)


def build_initial_result(
    message: str,
    *,
    planning_context: Mapping[str, Any] | None,
    plan_revision: int,
    supersedes_run_id: uuid.UUID | None,
    previous_output: dict[str, Any] | None,
) -> dict[str, Any]:
    inherited: dict[str, Any] = {}
    previous_result: RunResultV1 | None = None
    if previous_output:
        previous = coerce_run_result(previous_output)
        previous_result = RunResultV1.model_validate(previous)
        request = previous.get("request")
        if isinstance(request, dict):
            inherited = {
                key: request.get(key)
                for key in (
                    "city",
                    "trip_days",
                    "interests",
                    "budget",
                    "additional_preferences",
                )
            }
    supplied = dict(planning_context or {})
    request_data = {**inherited, **{k: v for k, v in supplied.items() if v is not None}}
    request_data["message"] = message
    request = PlanningRequestSnapshot.model_validate(request_data)
    result = RunResultV1(
        generated_at=utc_now(),
        plan_revision=plan_revision,
        supersedes_run_id=supersedes_run_id,
        request=request,
    )
    if previous_result is not None and supersedes_run_id is not None:
        result.weather_window = previous_result.weather_window
        result.itinerary = previous_result.itinerary
        result.sources = previous_result.sources
        result.warnings = previous_result.warnings
        result.components = previous_result.components.model_copy(deep=True)
        for name in ("weather", "guide", "route"):
            component = getattr(result.components, name)
            if component.status in {ComponentStatus.READY, ComponentStatus.DEGRADED}:
                component.inherited_from_run_id = supersedes_run_id
                component.message = "沿用上一版结果，等待本次 Run 确认或更新。"
    return _dump(result)


def apply_context_usage(
    current: dict[str, Any] | None,
    usage: Mapping[str, Any],
) -> dict[str, Any]:
    result = RunResultV1.model_validate(coerce_run_result(current))
    result.context_usage = ContextUsageSnapshot.model_validate(dict(usage))
    result.generated_at = utc_now()
    return _dump(result)


def _replace_source(result: RunResultV1, source: ResultSource) -> None:
    key = (source.provider.lower(), source.url or "", source.purpose)
    result.sources = [
        item
        for item in result.sources
        if (item.provider.lower(), item.url or "", item.purpose) != key
    ]
    result.sources.append(source)
    result.sources = result.sources[-100:]


def _add_warning(result: RunResultV1, warning: ResultWarning) -> None:
    key = (warning.code, warning.message, warning.scope)
    if not any((item.code, item.message, item.scope) == key for item in result.warnings):
        result.warnings.append(warning)
        result.warnings = result.warnings[-100:]


def _component(
    status: ComponentStatus,
    *,
    error_code: str | None = None,
    message: str | None = None,
    timestamp: datetime,
) -> ResultComponent:
    return ResultComponent(
        status=status,
        error_code=_optional_bounded_text(error_code, 100),
        message=_optional_bounded_text(message, 1000),
        updated_at=timestamp,
    )


def _warning_scope(message: str) -> str:
    lowered = message.lower()
    if any(token in lowered for token in ("ors", "路线", "矩阵", "geocoder", "交通")):
        return "route"
    if any(token in lowered for token in ("wikivoyage", "攻略", "景点", "开放时间")):
        return "guide"
    return "guide"


def apply_tool_result(
    current: dict[str, Any] | None,
    tool_name: str,
    snapshot: dict[str, Any] | None,
    *,
    succeeded: bool,
) -> dict[str, Any]:
    result = RunResultV1.model_validate(coerce_run_result(current))
    timestamp = utc_now()
    payload = snapshot or {}
    error_code = _bounded_text(
        payload.get("error_code") or "TOOL_EXECUTION_FAILED", 100
    )
    error_message = _bounded_text(
        payload.get("message") or f"{tool_name} 执行失败。", 2000
    )

    if tool_name == "find_best_weather_window":
        if succeeded:
            result.weather_window = payload
            result.components.weather = _component(
                ComponentStatus.READY, timestamp=timestamp
            )
            source = payload.get("source")
            if isinstance(source, Mapping):
                _replace_source(
                    result,
                    ResultSource(
                        provider=_bounded_text(source.get("name") or "Open-Meteo", 100),
                        title="近期天气预报",
                        url=_optional_bounded_text(source.get("forecast_url"), 2000),
                        purpose="weather",
                        fetched_at=timestamp,
                    ),
                )
            notice = _bounded_text(payload.get("notice"), 2000).strip()
            if notice:
                _add_warning(
                    result,
                    ResultWarning(
                        code="WEATHER_FORECAST_NOTICE",
                        message=notice,
                        severity="info",
                        scope="weather",
                    ),
                )
        else:
            inherited_from = result.components.weather.inherited_from_run_id
            result.components.weather = _component(
                (
                    ComponentStatus.DEGRADED
                    if result.weather_window is not None
                    else ComponentStatus.UNAVAILABLE
                ),
                error_code=error_code,
                message=error_message,
                timestamp=timestamp,
            )
            result.components.weather.inherited_from_run_id = inherited_from
            _add_warning(
                result,
                ResultWarning(
                    code=error_code,
                    message=error_message,
                    severity="error",
                    scope="weather",
                ),
            )

    elif tool_name == "plan_wikivoyage_trip":
        if succeeded:
            result.itinerary = payload
            raw_warnings = [
                _bounded_text(item, 2000).strip()
                for item in payload.get("warnings", [])
                if str(item).strip()
            ]
            result.components.guide = _component(
                ComponentStatus.READY, timestamp=timestamp
            )
            route_degraded = any(
                any(token in warning.lower() for token in ("ors", "矩阵", "geocoder", "降级", "替代"))
                for warning in raw_warnings
            )
            result.components.route = _component(
                ComponentStatus.DEGRADED if route_degraded else ComponentStatus.READY,
                message="路线包含降级数据。" if route_degraded else None,
                timestamp=timestamp,
            )
            for page in payload.get("source_pages", []):
                if not isinstance(page, Mapping):
                    continue
                _replace_source(
                    result,
                    ResultSource(
                        provider="Wikivoyage",
                        title=_bounded_text(page.get("title") or "Wikivoyage 攻略", 500),
                        url=_optional_bounded_text(page.get("url"), 2000),
                        purpose="guide",
                        fetched_at=timestamp,
                    ),
                )
            _replace_source(
                result,
                ResultSource(
                    provider="OpenRouteService",
                    title="路线与交通矩阵",
                    url="https://openrouteservice.org/",
                    purpose="route",
                    fetched_at=timestamp,
                ),
            )
            for warning in raw_warnings:
                _add_warning(
                    result,
                    ResultWarning(
                        code="TOOL_WARNING",
                        message=warning,
                        severity="warning",
                        scope=_warning_scope(warning),
                    ),
                )
        else:
            for name in ("guide", "route"):
                previous_component = getattr(result.components, name)
                component = _component(
                    (
                        ComponentStatus.DEGRADED
                        if result.itinerary is not None
                        else ComponentStatus.UNAVAILABLE
                    ),
                    error_code=error_code,
                    message=error_message,
                    timestamp=timestamp,
                )
                component.inherited_from_run_id = (
                    previous_component.inherited_from_run_id
                )
                setattr(result.components, name, component)
            _add_warning(
                result,
                ResultWarning(
                    code=error_code,
                    message=error_message,
                    severity="error",
                    scope="route" if "ORS" in error_code else "guide",
                ),
            )

    result.result_status = ResultStatus.PARTIAL
    result.generated_at = timestamp
    return _dump(result)


def complete_result(
    current: dict[str, Any] | None,
    answer: Mapping[str, Any],
) -> dict[str, Any]:
    result = RunResultV1.model_validate(coerce_run_result(current))
    timestamp = utc_now()
    result.assistant_answer = str(
        answer.get("assistant_answer") or answer.get("answer") or ""
    )
    for item in answer.get("sources") or answer.get("reference") or []:
        if not isinstance(item, Mapping):
            continue
        _replace_source(
            result,
            ResultSource(
                provider=_bounded_text(item.get("provider") or "Agent reference", 100),
                title=_bounded_text(item.get("title") or "参考来源", 500),
                url=_optional_bounded_text(item.get("url"), 2000),
                purpose=_bounded_text(item.get("purpose") or "guide", 100),
                fetched_at=timestamp,
            ),
        )
    for name in ("weather", "guide", "route"):
        component = getattr(result.components, name)
        if component.status == ComponentStatus.PENDING:
            setattr(
                result.components,
                name,
                _component(
                    ComponentStatus.UNAVAILABLE,
                    message="本次 Run 未生成该组件。",
                    timestamp=timestamp,
                ),
            )
    component_states = [
        result.components.weather.status,
        result.components.guide.status,
        result.components.route.status,
    ]
    result.result_status = (
        ResultStatus.COMPLETE
        if result.assistant_answer.strip() and all(
            status in {ComponentStatus.READY, ComponentStatus.DEGRADED}
            for status in component_states
        )
        else ResultStatus.PARTIAL
    )
    result.generated_at = timestamp
    return _dump(result)


def fail_result(
    current: dict[str, Any] | None,
    error_code: str,
    error_message: str,
    *,
    terminal: bool,
) -> dict[str, Any]:
    result = RunResultV1.model_validate(coerce_run_result(current))
    timestamp = utc_now()
    error_code = _bounded_text(error_code, 100)
    error_message = _bounded_text(error_message, 2000)
    if terminal:
        result.result_status = ResultStatus.FAILED
        for name in ("weather", "guide", "route"):
            component = getattr(result.components, name)
            if component.status == ComponentStatus.PENDING:
                setattr(
                    result.components,
                    name,
                    _component(
                        ComponentStatus.UNAVAILABLE,
                        error_code=error_code,
                        message=error_message,
                        timestamp=timestamp,
                    ),
                )
            elif component.inherited_from_run_id is not None:
                component.status = ComponentStatus.DEGRADED
                component.error_code = error_code
                component.message = (
                    "本次 Run 未完成，当前展示内容沿用上一成功版本。"
                )
                component.updated_at = timestamp
    _add_warning(
        result,
        ResultWarning(
            code=error_code,
            message=error_message,
            severity="error" if terminal else "warning",
            scope="agent",
        ),
    )
    result.generated_at = timestamp
    return _dump(result)
