# SGR Research Agent (Mistral)

Schema-Guided Reasoning (SGR) agent tailored for the Mistral platform with an adaptive planning loop, rich logging, and tightly controlled JSON Schema interactions. The agent orchestrates clarification, plan generation, focused web research, and report writing while maintaining full auditability of every model call.

## Features
- **Schema-guided workflow.** The agent keeps a strict JSON Schema contract when coordinating planning, searches, and reporting, ensuring deterministic tool selection and validation before each call to Mistral.
- **Clarification-first research.** Before executing a plan the agent can pause and collect extra context, preventing wasted searches and enabling user-approved assumptions.
- **Adaptive planning and search.** Each step evaluates remaining work, triggers Tavily-powered web searches, and injects citations or scraped content back into the conversation loop.
- **Automatic citation & report management.** Citations are tracked centrally, and completed reports are saved to disk alongside structured metadata for later review.
- **Robust logging & debugging.** Rotating log files, rich console output, and JSON/text artifacts for every Mistral request make it easy to audit and troubleshoot sessions.
- **Embedded scraping utilities.** Built-in helpers fetch article content via Trafilatura or extract YouTube transcripts to enrich research notes without additional setup.

## Requirements
- Python 3.9+ (the agent relies on `typing.Annotated` and other modern typing features).
- API access to Mistral and Tavily.
- Suggested Python packages: `mistralai`, `tavily-python`, `youtube-transcript-api`, `trafilatura`, `rich`, `pydantic`, `annotated-types`, `PyYAML`.

## Installation
1. Create and activate a virtual environment (optional but recommended).
2. Install dependencies:
   ```bash
   pip install mistralai tavily-python youtube-transcript-api trafilatura rich pydantic annotated-types PyYAML
   ```
3. Copy the provided template and fill in your credentials:
   ```bash
   cp config.yaml.example config.yaml
   ```

## Configuration
The agent loads configuration values from `config.yaml` and environment variables, covering model parameters, search controls, scraping limits, and logging paths. Key fields include:

- `mistral.api_key`, `mistral.model`, `mistral.max_tokens`, `mistral.temperature` – core generation settings.
- `tavily.api_key` – Tavily search token.
- `search.max_results` – default number of links per Tavily query.
- `scraping.enabled`, `scraping.max_pages`, `scraping.content_limit` – control automatic page and transcript retrieval.
- `execution.max_steps`, `execution.reports_dir` – govern planning horizon and report destination.
- `logging.dir`, `logging.file`, `logging.level`, `logging.debug_dir` via environment overrides – manage structured logs and per-request artifacts.

You may also export environment variables such as `MISTRAL_API_KEY`, `TAVILY_API_KEY`, `MAX_EXECUTION_STEPS`, or `SCRAPING_ENABLED` to run the agent without editing the YAML file.

## Running the agent
Launch the interactive session after credentials are configured:

```bash
python sgr_classic_agent.py
```

On startup the CLI prints core features, then prompts for a research request. Provide a task (in any supported language), and the agent will iterate through planning, searches, and report generation until completion. Use `quit` or `exit` to close the loop.

## Workflow overview
1. **Task intake & guardrails.** The agent constructs a guarded system prompt, records session metadata, and begins the planning loop.
2. **Context injection.** After each step, current progress, available citations, and search counts are fed back to the model to keep reasoning grounded.
3. **Tool execution.** Depending on the schema output, the agent may issue clarification questions, generate/adjust plans, query Tavily, scrape sources, or compile the final report.
4. **Reporting & completion.** Once sufficient evidence is gathered the agent saves the markdown report under `reports/` (or the configured directory) and prints a completion summary.

## Outputs and artifacts
- **Reports:** Saved as timestamped Markdown files in the configured reports directory (default `./reports`).
- **Logs:** Rotating log file under `logs/agent.log` plus optional rich console output.
- **Debug artifacts:** JSON and raw text dumps for every Mistral request live in `debug_out/` by default, allowing full replay of message payloads and parsed schema objects.

## Scraping and transcripts
When scraping is enabled the agent will fetch article content (converted to Markdown) and integrate YouTube transcripts when links are detected, expanding the evidence pool automatically. Adjust the scraping limits in the configuration to suit your bandwidth and compliance requirements.

## Troubleshooting
- Missing API keys halt startup; ensure Mistral and Tavily keys are present in the configuration or environment variables.
- Check `debug_out/req_*.json` and the log directory for detailed traces when schema validation fails or unexpected tool outputs occur.

