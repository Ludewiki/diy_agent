"""CLI entry point and Agent factory for the travel-planning workflow.

Importing this module creates neither a model client nor an Agent and never
loads local secrets. Use :func:`create_travel_agent` from a service, or run the
module as a CLI application.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
from typing import Any, Sequence

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import HumanMessage
from langchain_deepseek import ChatDeepSeek
from pydantic import BaseModel, Field

from logging_config import configure_logging
from travel_planner_tool import plan_wikivoyage_trip
from weather_tool import find_best_weather_window


class Reference(BaseModel):
    title: str = Field(description="引用来源标题")
    url: str = Field(description="引用来源 URL")


class AnswerInfo(BaseModel):
    answer: str = Field(description="给用户的最终旅行规划")
    reference: list[Reference] = Field(default_factory=list, description="Wikivoyage 引用页面")


def _system_prompt(current_date: str) -> str:
    return f"""
你是旅行规划助手，今天是 {current_date}。必须严格按以下工作流执行：

1. 先调用 find_best_weather_window 查询城市天气并选择连续日期。用户给定日期范围时，
   转为 YYYY-MM-DD 传入 target_start_date 和 target_end_date。不得自行计算或编造天气。
2. 天气工具返回 status=error 时立即停止，说明 error_code 和限制；特别是
   FORECAST_OUT_OF_RANGE 时不得继续生成依赖天气的正式行程。
3. 只有天气成功后才能调用 plan_wikivoyage_trip。传入相同 city、trip_days，天气结果
   best_window.daily_weather，用户兴趣，language=zh、daily_minutes=480、
   max_candidates=12、map_profile=foot-walking、exclude_universities=true。
4. 不得修改工具选定日期，不得自行重算景点评分、聚类、地图时间或日期匹配。
5. 攻略工具失败时如实说明 error_code 和 message，不补写虚构行程。
6. 成功回答须包含日期、逐日天气、天气恶劣程度、室内比例、景点顺序、游览与交通时间、
   推荐理由和全部风险提醒。
7. 引用只能来自攻略工具 source_pages，title/url 必须原样使用并去重；没有则返回空列表。
8. 提醒天气会变化、Wikivoyage 为社区内容、开放时间/票价/闭馆信息须到官网复核。
9. 用户提供预算时，将其作为餐饮、景点消费和节奏建议的约束；当前没有实时酒店、
   机票或门票报价，不得把预算描述成已验证价格。
"""


def create_travel_agent(*, model_name: str = "deepseek-chat") -> Any:
    """Create the Agent explicitly; suitable for dependency injection in a backend."""
    if not os.getenv("DEEPSEEK_API_KEY"):
        raise RuntimeError("缺少 DEEPSEEK_API_KEY；请通过环境变量或密钥管理服务注入。")
    model = ChatDeepSeek(model=model_name, temperature=0)
    return create_agent(
        model=model,
        tools=[find_best_weather_window, plan_wikivoyage_trip],
        system_prompt=_system_prompt(datetime.now().strftime("%Y年%m月%d日")),
        response_format=ToolStrategy(AnswerInfo),
    )


def run_prompt(
    prompt: str,
    *,
    callbacks: Sequence[Any] | None = None,
) -> AnswerInfo:
    """Run one prompt through a newly created Agent."""
    config = {"callbacks": list(callbacks)} if callbacks else None
    result = create_travel_agent().invoke(
        {"messages": [HumanMessage(content=prompt)]},
        config=config,
    )
    answer = result.get("structured_response")
    if answer is None:
        raise RuntimeError("Agent 没有生成结构化结果")
    return answer


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="天气驱动的多工具旅行规划 Agent")
    parser.add_argument("prompt", nargs="?", default="帮我规划近期去上海连续游玩3天的旅游攻略")
    parser.add_argument("--env-file", type=Path, help="可选的本地环境文件；不得提交到版本库")
    args = parser.parse_args(argv)
    if args.env_file:
        load_dotenv(args.env_file)
    else:
        load_dotenv(Path(__file__).resolve().parent / ".env")
    configure_logging()
    print(run_prompt(args.prompt).model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
