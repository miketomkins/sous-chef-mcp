# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Sous Chef MCP is a Model Context Protocol server for Claude Desktop that extracts structured recipe data from websites using schema.org JSON-LD markup. It builds verified shopping lists from exact ingredient data rather than LLM interpretation of page text. Runs over stdio transport.

## Commands

```bash
# Setup
bash setup.sh                        # Creates venv, installs deps, verifies compilation

# Run (used by Claude Desktop, not typically run manually)
./venv/bin/python server.py

# Verify server compiles
./venv/bin/python -m py_compile server.py

# Install deps manually
source venv/bin/activate && pip install -r requirements.txt
```

There are no tests, linter, or formatter configured.

## Architecture

**Single-file server** (`server.py`, ~1150 lines) built on `mcp.server.fastmcp.FastMCP`. Everything lives in one file: Pydantic input models, helper functions, tool implementations, and the server entry point.

### Key layers in server.py:
- **File I/O helpers** (`_load_json`, `_save_json`, `_load_yaml`, `_save_yaml`) — all data persistence
- **Recipe extraction** (`_extract_jsonld_recipes`, `_collect_recipes`, `_extract_recipe_data`) — parses HTML for `<script type="application/ld+json">` tags, recursively finds Recipe-typed objects including nested `@graph` structures
- **Ingredient categorization** (`CATEGORY_RULES` dict, `SPICE_KEYWORDS` list, `_categorize_ingredient`) — keyword-matching to assign ingredients to store sections (Protein, Dairy, Produce, etc.)
- **Pantry staple detection** (`_is_pantry_staple`) — fuzzy substring match against `config/pantry_staples.yaml`
- **Pydantic input models** — one per tool, with validation (e.g., `RecipeUrlInput`, `ShoppingListInput`, `MenuFormatInput`)
- **Tool implementations** — 14 tools prefixed `recipe_*`, registered via `@mcp.tool()` decorator

### Lifespan pattern:
The `app_lifespan` async context manager loads config (sites.yaml, pantry_staples.yaml) and creates a shared `httpx.AsyncClient` at startup. These are accessed in tools via `ctx.request_context.lifespan_state`.

### Data flow:
1. `recipe_get` fetches a URL, extracts JSON-LD, normalizes to internal format, logs to `data/history.json`
2. `recipe_build_shopping_list` calls `recipe_get` for multiple URLs, categorizes ingredients, separates pantry staples
3. `recipe_format_menu` orchestrates the full workflow: fetches recipes, builds shopping list, formats output for Apple Notes

### Config files:
- `config/sites.yaml` — recipe site discovery config (base URLs, search patterns, category paths). Not required to extract from a URL.
- `config/pantry_staples.yaml` — ingredients to list separately as "verify stock"

### Data files (gitignored, auto-created):
- `data/history.json` — auto-logged on every `recipe_get` call
- `data/favorites.json` — user-saved recipes with tags
- `data/exclusions.json` — blocked recipes/ingredients with reasons

## Dependencies

Python 3.10+, mcp[cli], httpx, beautifulsoup4, pydantic, pyyaml. All in `requirements.txt`.

## Claude Desktop Integration

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` with the venv python path and server.py path.
