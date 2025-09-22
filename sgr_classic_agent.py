#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SGR Research Agent - Schema-Guided Reasoning with Adaptive Planning (Mistral)
Clean JSON-Schema-only implementation for Mistral with schema compatibility layer.

Изменения:
- Полностью удалён OpenAI, заменён на Mistral (`mistralai`).
- Строго один путь вывода: JSON Schema с трансформациями под Mistrал:
  * additionalProperties:false для всех object
  * anyOf → oneOf (особенно важно для поля union `function`)
  * inline $ref из $defs
  * удаление проблемных array-ключей: minItems/maxItems/uniqueItems/contains/minContains/maxContains
- Сообщения очищаются для Mistral (никаких tool_calls).
- Дальше локальная Pydantic-валидация результата.
- FIX: после Clarification — немедленный выход (REPL спрашивает ответы).
- FIX: никаких `system` после старта диалога — только один `system` в самом начале.
"""

import json
import os
import re
import yaml
from datetime import datetime
from typing import List, Union, Literal, Optional, Dict, Any, Tuple

try:
    from typing import Annotated  # Python 3.9+
except ImportError:
    from typing_extensions import Annotated  # Python 3.8+

from pydantic import BaseModel, Field
from annotated_types import MinLen, MaxLen
from tavily import TavilyClient
from rich.console import Console
from rich.panel import Panel
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import TextFormatter
import trafilatura

# =============================================================================
# CONFIGURATION
# =============================================================================

def load_config() -> Dict[str, Any]:
    """Load configuration from config.yaml and environment variables (Mistral only)."""
    config = {
        # Mistral
        'mistral_api_key': os.getenv('MISTRAL_API_KEY', ''),
        'mistral_base_url': os.getenv('MISTRAL_BASE_URL', ''),
        'mistral_model': os.getenv('MISTRAL_MODEL', 'mistral-large-latest'),
        'max_tokens': int(os.getenv('MAX_TOKENS', '8000')),
        'temperature': float(os.getenv('TEMPERATURE', '0.4')),
        # Tavily
        'tavily_api_key': os.getenv('TAVILY_API_KEY', ''),
        # Search/scraping/execution
        'max_search_results': int(os.getenv('MAX_SEARCH_RESULTS', '10')),
        'max_execution_steps': int(os.getenv('MAX_EXECUTION_STEPS', '6')),
        'reports_directory': os.getenv('REPORTS_DIRECTORY', 'reports'),
        'scraping_enabled': os.getenv('SCRAPING_ENABLED', 'false').lower() == 'true',
        'scraping_max_pages': int(os.getenv('SCRAPING_MAX_PAGES', '5')),
        'scraping_content_limit': int(os.getenv('SCRAPING_CONTENT_LIMIT', '1500')),
    }

    if os.path.exists('config.yaml.example'):
        try:
            with open('config.yaml.example', 'r', encoding='utf-8') as f:
                yaml_config = yaml.safe_load(f) or {}

            if 'mistral' in yaml_config:
                m = yaml_config['mistral'] or {}
                config['mistral_api_key'] = m.get('api_key', config['mistral_api_key'])
                config['mistral_base_url'] = m.get('base_url', config['mistral_base_url'])
                config['mistral_model'] = m.get('model', config['mistral_model'])
                config['max_tokens'] = m.get('max_tokens', config['max_tokens'])
                config['temperature'] = m.get('temperature', config['temperature'])

            if 'tavily' in yaml_config:
                config['tavily_api_key'] = (yaml_config['tavily'] or {}).get('api_key', config['tavily_api_key'])

            if 'search' in yaml_config:
                config['max_search_results'] = (yaml_config['search'] or {}).get('max_results', config['max_search_results'])

            if 'scraping' in yaml_config:
                sc = yaml_config['scraping'] or {}
                config['scraping_enabled'] = sc.get('enabled', config['scraping_enabled'])
                config['scraping_max_pages'] = sc.get('max_pages', config['scraping_max_pages'])
                config['scraping_content_limit'] = sc.get('content_limit', config['scraping_content_limit'])

            if 'execution' in yaml_config:
                ex = yaml_config['execution'] or {}
                config['max_execution_steps'] = ex.get('max_steps', config['max_execution_steps'])
                config['reports_directory'] = ex.get('reports_dir', config['reports_directory'])
        except Exception as e:
            print(f"[yellow]Warning: Could not load config.yaml: {e}[/yellow]")

    return config

CONFIG = load_config()

# Required keys
if not CONFIG['mistral_api_key']:
    print("[red]ERROR:[/red] MISTRAL_API_KEY not set in config.yaml or environment")
    raise SystemExit(1)
if not CONFIG['tavily_api_key']:
    print("[red]ERROR:[/red] TAVILY_API_KEY not set in config.yaml or environment")
    raise SystemExit(1)

# =============================================================================
# SCRAPING UTILITIES - Embedded
# =============================================================================

def extract_youtube_id(url: str) -> Optional[str]:
    youtube_patterns = [
        r'(?:youtube\.com/watch\?v=|youtu\.be/|m\.youtube\.com/watch\?v=)([a-zA-Z0-9_-]{11})',
        r'youtube\.com/embed/([a-zA-Z0-9_-]{11})',
        r'youtube\.com/v/([a-zA-Z0-9_-]{11})',
    ]
    for pattern in youtube_patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None

def fetch_youtube_transcript(url: str) -> dict:
    try:
        vid = extract_youtube_id(url)
        if not vid:
            return {'url': url, 'status': 'error', 'error': 'Invalid YouTube URL'}
        transcript_list = YouTubeTranscriptApi().list(vid)
        if not transcript_list:
            return {'url': url, 'status': 'error', 'error': 'No transcript found'}

        original = None
        first = next(iter(transcript_list), None)
        for tr in transcript_list:
            if not tr.is_generated:
                original = tr
                break
        if not original:
            original = transcript_list.find_transcript([first.language_code])

        data = original.fetch()
        text = TextFormatter().format_transcript(data)
        if text and len(text.strip()) > 100:
            return {'url': url, 'full_content': text.strip(), 'status': 'success', 'char_count': len(text)}
        return {'url': url, 'status': 'empty', 'error': 'No meaningful transcript found'}
    except Exception as e:
        return {'url': url, 'status': 'error', 'error': f"YouTube transcript: {str(e)[:200]}"}

def fetch_page_content(url: str) -> dict:
    vid = extract_youtube_id(url)
    if vid:
        return fetch_youtube_transcript(url)
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            content = trafilatura.extract(
                downloaded,
                output_format='markdown',
                favor_precision=True,
                include_tables=True,
                include_links=False,
                include_images=False,
                deduplicate=True,
                target_language=None
            )
            if content and len(content.strip()) > 100:
                return {'url': url, 'full_content': content.strip(), 'status': 'success', 'char_count': len(content)}
        return {'url': url, 'status': 'empty', 'error': 'No meaningful content extracted'}
    except Exception as e:
        return {'url': url, 'status': 'error', 'error': str(e)[:200]}

# =============================================================================
# SGR SCHEMAS - Core Schema-Guided Reasoning Definitions (Pydantic unchanged)
# =============================================================================

class Clarification(BaseModel):
    tool: Literal["clarification"]
    reasoning: str = Field(description="Why clarification is needed")
    unclear_terms: Annotated[List[str], MinLen(1), MaxLen(5)] = Field(description="List of unclear terms or concepts")
    assumptions: Annotated[List[str], MinLen(2), MaxLen(4)] = Field(description="Possible interpretations to verify")
    questions: Annotated[List[str], MinLen(3), MaxLen(5)] = Field(description="3-5 specific clarifying questions")

class GeneratePlan(BaseModel):
    tool: Literal["generate_plan"]
    reasoning: str = Field(description="Justification for research approach")
    research_goal: str = Field(description="Primary research objective")
    planned_steps: Annotated[List[str], MinLen(3), MaxLen(4)] = Field(description="List of 3-4 planned steps")
    search_strategies: Annotated[List[str], MinLen(2), MaxLen(3)] = Field(description="Information search strategies")

class WebSearch(BaseModel):
    tool: Literal["web_search"]
    reasoning: str = Field(description="Why this search is needed and what to expect")
    query: str = Field(description="Search query in same language as user request")
    max_results: int = Field(default=10, description="Maximum results (1-15)")
    plan_adapted: bool = Field(default=False, description="Is this search after plan adaptation?")
    scrape_content: bool = Field(
        default_factory=lambda: CONFIG.get('scraping_enabled', False),
        description="Fetch full page content for deeper analysis"
    )

class AdaptPlan(BaseModel):
    tool: Literal["adapt_plan"]
    reasoning: str = Field(description="Why plan needs adaptation based on new data")
    original_goal: str = Field(description="Original research goal")
    new_goal: str = Field(description="Updated research goal")
    plan_changes: Annotated[List[str], MinLen(1), MaxLen(3)] = Field(description="Specific changes made to plan")
    next_steps: Annotated[List[str], MinLen(2), MaxLen(4)] = Field(description="Updated remaining steps")

class CreateReport(BaseModel):
    tool: Literal["create_report"]
    reasoning: str = Field(description="Why ready to create report now")
    title: str = Field(description="Report title")
    user_request_language_reference: str = Field(
        description="Copy of original user request to ensure language consistency"
    )
    content: str = Field(description="""
    DETAILED technical content (800+ words) with in-text citations.

    🚨 CRITICAL LANGUAGE REQUIREMENT 🚨:
    - WRITE ENTIRELY IN THE SAME LANGUAGE AS user_request_language_reference
    - If user_request_language_reference is in Russian → WRITE IN RUSSIAN
    - If user_request_language_reference is in English → WRITE IN ENGLISH
    - DO NOT mix languages - use ONLY the language from user_request_language_reference

    OTHER REQUIREMENTS:
    - Include in-text citations for EVERY fact using [1], [2], [3] etc.
    - Citations must be integrated into sentences, not separate
    - Example Russian: "Apple M5 использует 3нм процесс [1], что улучшает производительность [2]."
    - Example English: "Apple M5 uses 3nm process [1] which improves performance [2]."

    Structure:
    1. Executive Summary / Исполнительное резюме
    2. Technical Analysis / Технический анализ (with citations)
    3. Key Findings / Ключевые выводы
    4. Conclusions / Заключения

    🚨 LANGUAGE COMPLIANCE: Text MUST match user_request_language_reference language 100%
    """)
    confidence: Literal["high", "medium", "low"] = Field(description="Confidence in findings")

class ReportCompletion(BaseModel):
    tool: Literal["report_completion"]
    reasoning: str = Field(description="Why research is now complete")
    completed_steps: Annotated[List[str], MinLen(1), MaxLen(5)] = Field(description="Summary of completed steps")
    status: Literal["completed", "failed"] = Field(description="Task completion status")

class NextStep(BaseModel):
    reasoning_steps: Annotated[List[str], MinLen(2), MaxLen(4)] = Field(
        description="Step-by-step reasoning process leading to decision"
    )
    current_situation: str = Field(description="Current research situation analysis")
    plan_status: str = Field(description="Status of current plan execution")
    searches_done: int = Field(default=0, description="Number of searches completed (MAX 3-4 searches)")
    enough_data: bool = Field(default=False, description="Sufficient data for report? (True after 2-3 searches)")
    remaining_steps: Annotated[List[str], MinLen(1), MaxLen(3)] = Field(description="1-3 remaining steps to complete task")
    task_completed: bool = Field(description="Is the research task finished?")
    function: Union[Clarification, GeneratePlan, WebSearch, AdaptPlan, CreateReport, ReportCompletion] = Field(
        description="Selected tool for the next step"
    )

# =============================================================================
# JSON SCHEMA COMPAT LAYER (Pydantic → Mistral-friendly JSON Schema)
# =============================================================================

UNSUPPORTED_ARRAY_KEYWORDS = {
    "minItems", "maxItems", "uniqueItems",
    "contains", "minContains", "maxContains",
}

def _visit(obj, fn_path_value):
    def _inner(node, path):
        fn_path_value(path, node)
        if isinstance(node, dict):
            for k, v in list(node.items()):
                _inner(v, (*path, k))
        elif isinstance(node, list):
            for i, v in enumerate(list(node)):
                _inner(v, (*path, i))
    _inner(obj, ())

def mistral_additional_properties_false(schema: dict) -> dict:
    def visit(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "additionalProperties" not in node:
                node["additionalProperties"] = False
            if "properties" in node and isinstance(node["properties"], dict):
                for v in node["properties"].values():
                    visit(v)
            if "items" in node:
                visit(node["items"])
            for branch in ("oneOf", "anyOf", "allOf"):
                if branch in node and isinstance(node[branch], list):
                    for v in node[branch]:
                        visit(v)
            for key in ("$defs", "definitions"):
                if key in node and isinstance(node[key], dict):
                    for v in node[key].values():
                        visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)
        return node
    return visit(json.loads(json.dumps(schema)))  # deep copy

def mistral_anyof_to_oneof(schema: dict) -> dict:
    sc = json.loads(json.dumps(schema))
    def _walk(node):
        if isinstance(node, dict):
            if "anyOf" in node and isinstance(node["anyOf"], list) and node["anyOf"]:
                node["oneOf"] = node.pop("anyOf")
            for _, v in list(node.items()):
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)
    _walk(sc)
    return sc

def mistral_inline_refs(schema: dict) -> Tuple[dict, int]:
    sc = json.loads(json.dumps(schema))
    defs = sc.get("$defs") or sc.get("definitions") or {}
    replaced = 0

    def resolve_ref(ref: str) -> Optional[dict]:
        if ref.startswith("#/$defs/"):
            return json.loads(json.dumps(defs.get(ref.split("/", 2)[-1])))
        if ref.startswith("#/definitions/"):
            return json.loads(json.dumps(defs.get(ref.split("/", 2)[-1])))
        return None

    def fn(path, node):
        nonlocal replaced
        if isinstance(node, dict) and "$ref" in node and isinstance(node["$ref"], str):
            target = resolve_ref(node["$ref"])
            if target is not None:
                parent = sc
                for p in path[:-1]:
                    parent = parent[p]
                parent[path[-1]] = target
                replaced += 1

    _visit(sc, fn)
    return sc, replaced

def mistral_remove_unsupported_keywords(schema: dict) -> Tuple[dict, List[str]]:
    removed_paths: List[str] = []
    sc = json.loads(json.dumps(schema))

    def fn(path, node):
        if not isinstance(node, dict):
            return
        if node.get("type") == "array":
            for key in list(node.keys()):
                if key in UNSUPPORTED_ARRAY_KEYWORDS:
                    removed_paths.append(".".join(str(p) for p in path + (key,)))
                    node.pop(key, None)

    _visit(sc, fn)
    return sc, removed_paths

def build_mistral_compatible_schema(pydantic_model: type[BaseModel],
                                    loosen_arrays: bool = True,
                                    inline_refs: bool = True) -> Tuple[dict, dict]:
    try:
        raw = pydantic_model.model_json_schema()
    except Exception:
        raw = pydantic_model.model_json_schema(mode="serialization")

    steps = []
    s1 = mistral_additional_properties_false(raw); steps.append(("additionalProperties:false", None))
    s1 = mistral_anyof_to_oneof(s1);           steps.append(("anyOf->oneOf", None))

    if inline_refs:
        s2, count = mistral_inline_refs(s1);    steps.append(("inline_$ref", f"{count} replaced"))
    else:
        s2, count = s1, 0

    if loosen_arrays:
        s3, removed = mistral_remove_unsupported_keywords(s2); steps.append(("remove_unsupported_keywords", removed))
    else:
        s3, removed = s2, []

    debug = {"transforms": steps, "removed_paths": removed}
    return s3, debug

# =============================================================================
# PROMPTS - System Instructions
# =============================================================================

def guardrail_message() -> str:
    return (
        "FORMAT CONTRACT (STRICT):\n"
        "- Produce a single JSON object matching the schema.\n"
        "- Field `function` MUST be exactly one object with property `tool` equal to one of:\n"
        "  clarification | generate_plan | web_search | adapt_plan | create_report | report_completion.\n"
        "- Do not mix properties from different tool types.\n"
        "- `reasoning_steps`: array of 2..4 concise strings.\n"
        "- `remaining_steps`: array of 1..3 concise strings.\n"
        "- For `Clarification`: `unclear_terms` 1..5, `assumptions` 2..4, `questions` 3..5 strings.\n"
        "- Output ONLY the JSON value. No prose.\n"
    )

def get_system_prompt(user_request: str) -> str:
    return f"""
You are an expert researcher with adaptive planning and Schema-Guided Reasoning capabilities.

USER REQUEST EXAMPLE: "{user_request}"
↑ IMPORTANT: Detect the language from this request and use THE SAME LANGUAGE for all responses, searches, and reports.

CORE PRINCIPLES:
1. CLARIFICATION FIRST on any uncertainty
2. Do not assume — ask
3. Adapt plan when conflicts appear
4. Search queries in SAME language as user
5. Report ENTIRELY in SAME language
6. Every fact MUST have inline citation [1], [2], [3]

WORKFLOW:
clarification → generate_plan → web_search (2–3) → adapt_plan → create_report → report_completion

ANTI-CYCLING: max 1 clarification; max 3–4 searches before report.
""".strip()

# =============================================================================
# INITIALIZATION
# =============================================================================

console = Console()
print = console.print

# Mistral client (lazy import with friendly error)
def create_mistral_client():
    try:
        from mistralai import Mistral
    except Exception:
        print("[red]ERROR:[/red] 'mistralai' is not installed. Run: pip install mistralai")
        raise
    kwargs: Dict[str, Any] = {"api_key": CONFIG['mistral_api_key']}
    if CONFIG['mistral_base_url']:
        kwargs["server_url"] = CONFIG['mistral_base_url']
    return Mistral(**kwargs)

mistral_client = create_mistral_client()
tavily = TavilyClient(CONFIG['tavily_api_key'])

# Simple in-memory context
CONTEXT: Dict[str, Any] = {
    "plan": None,
    "searches": [],
    "sources": {},         # url -> {number, title, url}
    "citation_counter": 0,
    "clarification_used": False
}

# =============================================================================
# UTILITIES
# =============================================================================

def clean_messages_for_mistral(messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """
    Нормализация ролей под Mistral:
    - Разрешаем ровно один начальный `system` (до первого не-system).
    - Любой последующий `system` → `user`.
    - Любой `tool` → `user`.
    - Допустимые роли: system/user/assistant.
    """
    cleaned: List[Dict[str, str]] = []
    seen_non_system = False
    seen_leading_system = False

    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")

        # Любые tool → user
        if role == "tool":
            role = "user"

        if role == "system":
            if seen_non_system or seen_leading_system:
                role = "user"
            else:
                seen_leading_system = True
        else:
            seen_non_system = True

        if role not in ("system", "user", "assistant"):
            role = "user"

        cleaned.append({"role": role, "content": content})

    return cleaned

def add_citation(url: str, title: str = "") -> int:
    if url in CONTEXT["sources"]:
        return CONTEXT["sources"][url]["number"]
    CONTEXT["citation_counter"] += 1
    n = CONTEXT["citation_counter"]
    CONTEXT["sources"][url] = {"number": n, "title": title, "url": url}
    return n

def format_sources() -> str:
    if not CONTEXT["sources"]:
        return ""
    lines = ["\n\n## Sources\n"]
    for url, data in CONTEXT["sources"].items():
        n, title = data["number"], data["title"]
        lines.append(f"- [{n}] {title + ' - ' if title else ''}{url}\n")
    return "".join(lines)

# =============================================================================
# DISPATCH - Tool Execution
# =============================================================================

def dispatch(cmd: BaseModel, context: Dict[str, Any]) -> Any:
    if isinstance(cmd, Clarification):
        context["clarification_used"] = True
        print(f"\n🤔 [bold yellow]CLARIFICATION NEEDED[/bold yellow]")
        print(f"💭 Reason: {cmd.reasoning}\n")
        if cmd.unclear_terms:
            print(f"❓ [bold]Unclear terms:[/bold] {', '.join(cmd.unclear_terms)}")
        print(f"\n[bold cyan]CLARIFYING QUESTIONS:[/bold cyan]")
        for i, q in enumerate(cmd.questions, 1):
            print(f"   {i}. {q}")
        if cmd.assumptions:
            print(f"\n[bold green]Possible interpretations:[/bold green]")
            for a in cmd.assumptions:
                print(f"   • {a}")
        print(f"\n[bold yellow]⏸️  Research paused - please answer questions above[/bold yellow]")
        return {"tool": "clarification", "questions": cmd.questions, "status": "waiting_for_user"}

    elif isinstance(cmd, GeneratePlan):
        plan = {
            "research_goal": cmd.research_goal,
            "planned_steps": cmd.planned_steps,
            "search_strategies": cmd.search_strategies,
            "created_at": datetime.now().isoformat()
        }
        context["plan"] = plan
        print(f"📋 [bold]Research Plan Created[/bold]")
        print(f"🎯 Goal: {cmd.research_goal}")
        for i, step in enumerate(cmd.planned_steps, 1):
            print(f"   {i}. {step}")
        return plan

    elif isinstance(cmd, WebSearch):
        print(f"🔍 [bold cyan]Search query:[/bold cyan] [white]'{cmd.query}'[/white]")
        should_scrape = CONFIG['scraping_enabled'] and cmd.scrape_content
        if should_scrape:
            print("📄 [dim]Scraping enabled[/dim]")

        try:
            response = tavily.search(query=cmd.query, max_results=cmd.max_results)
            citation_numbers = []
            scraped_content = {}

            for i, result in enumerate(response.get('results', [])):
                url = result.get('url', '')
                title = result.get('title', '')
                if not url:
                    continue
                cnum = add_citation(url, title)
                citation_numbers.append(cnum)
                if should_scrape and i < CONFIG['scraping_max_pages']:
                    print(f"   📄 Scraping [{cnum}] {url[:50]}...")
                    scrape_result = fetch_page_content(url)
                    scraped_content[cnum] = scrape_result
                    if scrape_result['status'] == 'success':
                        print(f"   ✅ [{cnum}] {scrape_result.get('char_count', 0)} chars")
                    elif scrape_result['status'] == 'error':
                        print(f"   ❌ [{cnum}] Error: {scrape_result.get('error', 'Unknown')[:50]}")
                    else:
                        print(f"   ⚠️ [{cnum}] Empty content")

            search_result = {
                "query": cmd.query,
                "answer": response.get('answer', ''),
                "results": response.get('results', []),
                "citation_numbers": citation_numbers,
                "scraped_content": scraped_content,
                "scraping_enabled": should_scrape,
                "timestamp": datetime.now().isoformat()
            }
            context["searches"].append(search_result)

            print(f"🔍 Found {len(citation_numbers)} sources")
            for i, (result, cnum) in enumerate(zip(response.get('results', [])[:5], citation_numbers), 1):
                print(f"   {i}. [{cnum}] {result.get('url', '')}")
            if should_scrape:
                ok = len([c for c in scraped_content.values() if c['status'] == 'success'])
                print(f"📄 Scraped: {ok}/{len(scraped_content)} pages")

            return search_result

        except Exception as e:
            msg = f"Search error: {str(e)}"
            print(f"❌ {msg}")
            return {"error": msg}

    elif isinstance(cmd, AdaptPlan):
        if context.get("plan"):
            context["plan"]["research_goal"] = cmd.new_goal
            context["plan"]["planned_steps"] = cmd.next_steps
            context["plan"]["adapted"] = True
            context["plan"]["adaptations"] = context["plan"].get("adaptations", []) + [cmd.plan_changes]

        print(f"\n🔄 [bold yellow]PLAN ADAPTED[/bold yellow]")
        print(f"📝 [bold]Changes:[/bold]")
        for change in cmd.plan_changes:
            print(f"   • [yellow]{change}[/yellow]")
        print(f"🎯 [bold green]New goal:[/bold green] {cmd.new_goal}")
        return {"tool": "adapt_plan", "original_goal": cmd.original_goal, "new_goal": cmd.new_goal, "changes": cmd.plan_changes}

    elif isinstance(cmd, CreateReport):
        print(f"📝 [bold cyan]CREATE REPORT[/bold cyan]")
        print(f"   🌍 Language Reference: '{cmd.user_request_language_reference}'")
        print(f"   📊 Title: '{cmd.title}'")
        print(f"   📈 Confidence: {cmd.confidence}")

        os.makedirs(CONFIG['reports_directory'], exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_title = "".join(c for c in cmd.title if c.isalnum() or c in (' ', '-', '_'))[:50]
        filename = f"{timestamp}_{safe_title}.md"
        filepath = os.path.join(CONFIG['reports_directory'], filename)

        full = f"# {cmd.title}\n\n"
        full += f"*Created: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n\n"
        full += cmd.content
        full += format_sources()

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(full)

        report = {
            "title": cmd.title,
            "content": cmd.content,
            "confidence": cmd.confidence,
            "sources_count": len(context["sources"]),
            "word_count": len(cmd.content.split()),
            "filepath": filepath,
            "timestamp": datetime.now().isoformat()
        }
        print(f"📄 [bold blue]Report Saved:[/bold blue] {filepath}")
        return report

    elif isinstance(cmd, ReportCompletion):
        print(f"\n✅ [bold green]RESEARCH COMPLETED[/bold green]")
        print(f"📋 Status: {cmd.status}")
        if cmd.completed_steps:
            print(f"📝 [bold]Completed steps:[/bold]")
            for step in cmd.completed_steps:
                print(f"   • {step}")
        return {"tool": "report_completion", "status": cmd.status, "completed_steps": cmd.completed_steps}

    else:
        return f"Unknown command: {type(cmd)}"

# =============================================================================
# MISTRAL JSON-SCHEMA CALL
# =============================================================================

# Build compatible schema once
NEXTSTEP_JSON_SCHEMA, _SCHEMA_DEBUG = build_mistral_compatible_schema(
    NextStep, loosen_arrays=True, inline_refs=True
)

def mistral_complete_json_schema(messages: List[Dict[str, Any]]) -> NextStep:
    """Call Mistral with JSON-Schema response_format and validate via Pydantic."""
    rf = {
        "type": "json_schema",
        "json_schema": {
            "name": "nextstep",
            "schema": NEXTSTEP_JSON_SCHEMA,
            "strict": True,
        },
    }
    cleaned = clean_messages_for_mistral(messages)
    resp = mistral_client.chat.complete(
        model=CONFIG['mistral_model'],
        messages=cleaned,
        response_format=rf,
        max_tokens=CONFIG['max_tokens'],
        temperature=CONFIG['temperature'],
    )
    raw = resp.choices[0].message.content or ""
    return NextStep.model_validate_json(raw)

# =============================================================================
# MAIN EXECUTION ENGINE
# =============================================================================

def execute_research_task(task: str) -> str:
    """Execute research task using SGR with Mistral JSON-Schema only."""
    print(Panel(task, title="🔍 Research Task", title_align="left"))
    system_prompt = get_system_prompt(task)

    print(f"\n[bold green]🚀 SGR RESEARCH STARTED[/bold green]")
    print(f"[dim]🤖 Provider: Mistral[/dim]")
    print(f"[dim]🧩 Model: {CONFIG['mistral_model']}[/dim]")
    print(f"[dim]🔗 Base URL: {CONFIG['mistral_base_url'] or 'default'}[/dim]")
    print(f"[dim]🔑 API Key: {'✓ Configured' if CONFIG['mistral_api_key'] else '✗ Missing'}[/dim]")
    print(f"[dim]📊 Max tokens: {CONFIG['max_tokens']}, Temperature: {CONFIG['temperature']}[/dim]")

    # Ровно один начальный system: guardrails + system_prompt
    log: List[Dict[str, Any]] = [
        {"role": "system", "content": guardrail_message() + "\n\n" + system_prompt},
        {"role": "user",   "content": task},
    ]

    for i in range(CONFIG['max_execution_steps']):
        step_id = f"step_{i+1}"
        print(f"\n🧠 {step_id}: Planning next action...")

        # Context injection (как user, не system!)
        context_msg = []
        if CONTEXT["clarification_used"]:
            context_msg.append("IMPORTANT: Clarification already used. Do not request clarification again.")
        searches_count = len(CONTEXT.get("searches", []))
        context_msg.append(f"ORIGINAL USER REQUEST: '{task}'")
        context_msg.append(f"SEARCHES COMPLETED: {searches_count} (MAX 3-4 before creating report)")

        if CONTEXT["sources"]:
            sources_info = ["AVAILABLE SOURCES FOR CITATIONS:"]
            for url, data in CONTEXT["sources"].items():
                number = data["number"]; title = data["title"] or "Untitled"
                sources_info.append(f"[{number}] {title} - {url}")
            sources_info.append("USE THESE EXACT NUMBERS [1], [2], [3] in your report.")
            context_msg.extend(sources_info)

        if context_msg:
            ctx = "\n".join(context_msg)
            log.append({"role": "user", "content": ctx})  # ВАЖНО: user, не system
            print(f"[dim]🔧 Context: {ctx[:150]}...[/dim]")

        try:
            job = mistral_complete_json_schema(log)
            # Debug
            print(f"🤖 [bold magenta]LLM RESPONSE[/bold magenta]")
            print(f"   🧠 Reasoning Steps: {job.reasoning_steps}")
            print(f"   🔍 Searches Done: {job.searches_done}  ✅ Enough Data: {job.enough_data}")
            print(f"   📝 Remaining Steps: {job.remaining_steps}  🏁 Task Completed: {job.task_completed}")
            print(f"   🔧 Tool: {job.function.tool}")
        except Exception as e:
            print(f"[bold red]❌ Mistral/Validation error:[/bold red] {e}")
            break

        # Ранний выход на Clarification, чтобы REPL спросил пользователя
        if isinstance(job.function, Clarification):
            dispatch(job.function, CONTEXT)
            return "CLARIFICATION_NEEDED"

        if job.task_completed or isinstance(job.function, ReportCompletion):
            print(f"[bold green]✅ Task completed[/bold green]")
            dispatch(job.function, CONTEXT)
            break

        # assistant сводка шага
        next_step_text = job.remaining_steps[0] if job.remaining_steps else "Completing"
        print(f"[blue]{next_step_text}[/blue]")
        log.append({"role": "assistant", "content": next_step_text})

        # Execute tool
        result = dispatch(job.function, CONTEXT)

        # Feed back tool result как user
        if isinstance(job.function, WebSearch) and isinstance(result, dict):
            formatted = f"Search Query: {result.get('query','')}\n\n"
            if result.get('answer'):
                formatted += f"AI Answer: {result['answer']}\n\n"
            formatted += "Search Results:\n"
            scraped = result.get('scraped_content', {})
            for i2, source in enumerate(result.get('results', [])[:5], 1):
                cnums = result.get('citation_numbers', [])
                cnum = cnums[i2-1] if i2-1 < len(cnums) else i2
                title = source.get('title', 'Untitled'); url = source.get('url', '')
                if cnum in scraped and scraped[cnum]['status'] == 'success':
                    full = scraped[cnum]['full_content']
                    limit = CONFIG['scraping_content_limit']
                    content = full[:limit] + "..." if len(full) > limit else full
                    formatted += f"[{cnum}] {title}\n{url}\n**Full Content (Markdown):**\n{content}\n\n"
                else:
                    snip = source.get('content', '')[:300]
                    formatted += f"[{cnum}] {title}\n{url}\n{snip}\n\n"
            if result.get('scraping_enabled'):
                ok = len([c for c in scraped.values() if c['status'] == 'success'])
                formatted += f"Scraping Summary: {ok}/{len(scraped)} pages successfully scraped\n"
            log.append({"role": "user", "content": formatted})
        else:
            result_text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            log.append({"role": "user", "content": f"TOOL RESULT ({job.function.tool}): {result_text}"})

        # Автозавершение после отчёта
        if isinstance(job.function, CreateReport):
            print(f"\n✅ [bold green]Auto-completing after report creation[/bold green]")
            break

    return "COMPLETED"

# =============================================================================
# MAIN INTERFACE
# =============================================================================

def main():
    print("[bold]🧠 SGR Research Agent (Mistral, JSON-Schema only)[/bold]")
    print("Schema-Guided Reasoning with plan adaptation and strict JSON Schema I/O")
    print()
    print("Core features:")
    print("  🤔 Clarification-first approach")
    print("  🔄 Adaptive plan modification")
    print("  📎 Automatic citation management")
    print("  🌍 Multi-language support")
    print()

    awaiting_clarification = False
    original_task = ""

    while True:
        try:
            print("=" * 60)
            if awaiting_clarification:
                response = input("💬 Your clarification response (or 'quit'): ").strip()
                awaiting_clarification = False
                if response.lower() in ['quit', 'exit']:
                    break
                task = f"Original request: '{original_task}'\nClarification: {response}\n\nProceed with research based on clarification."
                CONTEXT["clarification_used"] = False
            else:
                task = input("🔍 Enter research task (or 'quit'): ").strip()

            if task.lower() in ['quit', 'exit']:
                print("👋 Goodbye!")
                break
            if not task:
                print("❌ Empty task. Try again.")
                continue

            if not awaiting_clarification:
                CONTEXT.clear()
                CONTEXT.update({
                    "plan": None,
                    "searches": [],
                    "sources": {},
                    "citation_counter": 0,
                    "clarification_used": False
                })
                original_task = task

            result = execute_research_task(task)
            if result == "CLARIFICATION_NEEDED":
                awaiting_clarification = True
                continue

            searches_count = len(CONTEXT.get("searches", []))
            sources_count = len(CONTEXT.get("sources", {}))
            print(f"\n📊 Session stats: 🔍 {searches_count} searches, 📎 {sources_count} sources")
            print(f"📁 Reports saved to: ./{CONFIG['reports_directory']}/")

        except KeyboardInterrupt:
            print("\n👋 Interrupted by user.")
            break
        except Exception as e:
            print(f"❌ Error: {e}")
            continue

if __name__ == "__main__":
    main()
