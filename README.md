# Sous Chef MCP

A local MCP server for Claude Desktop that eliminates shopping list hallucinations by extracting recipe data directly from structured schema.org markup. No more phantom Thai peppers.

## The Problem

When you ask Claude to build a weekly menu and shopping list from recipe websites, it reads the page content and reconstructs the ingredient list from memory. This leads to hallucinated ingredients that don't appear in the actual recipe, missed items, and incorrect quantities. The shopping list looks right but doesn't match what the recipes actually call for.

## The Fix

Almost every major recipe website embeds structured JSON-LD data using the [schema.org Recipe](https://schema.org/Recipe) type (Google requires it for rich search results). Sous Chef MCP extracts this structured data directly, giving you the exact ingredient list the recipe author published. The shopping list is built from verified data, not Claude's interpretation.

## Features

- **Verified ingredient extraction** from any recipe site using schema.org JSON-LD (site-agnostic, no per-site scraping)
- **Categorized shopping lists** grouped by store section: Protein, Dairy, Produce, Dry Goods, Frozen, Bread & Bakery
- **Ingredient grouping** — like ingredients from multiple recipes are grouped together (e.g. all onions in one place)
- **Apple Notes checklists** — shopping list items render as tappable checkboxes, with direct export via AppleScript
- **Recipe attribution** on every ingredient so you know which recipe needs what
- **Pantry staples management** for ingredients you always have on hand (listed separately as "verify stock")
- **Cook and prep time tracking** with timing info on the menu to help plan around long-prep meals
- **Favorites and history** to track what you've made, save winners, and avoid repeats
- **Repeat detection** — warns if a recipe was used in the last 7 days, so you don't get back-to-back weeks of the same meal
- **Exclusion list** for recipes or ingredients you don't like
- **Configurable site discovery** with search patterns and category URLs for your favorite recipe sites
- **Add new sites on the fly** either by editing config or asking Claude during a conversation
- **Feedback and bug reports** — submit issues directly to GitHub from within Claude Desktop
- **Self-updating** — pull the latest version from GitHub without leaving Claude Desktop

## Quick Start

```bash
git clone https://github.com/miketomkins/sous-chef-mcp.git
cd sous-chef-mcp
bash setup.sh
```

The setup script creates the virtual environment, installs dependencies, generates a default pantry staples list, and prints the Claude Desktop config snippet.

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "sous_chef": {
      "command": "/path/to/sous-chef-mcp/venv/bin/python",
      "args": ["/path/to/sous-chef-mcp/server.py"]
    }
  }
}
```

Restart Claude Desktop.

### Optional: GitHub CLI

Install the [GitHub CLI](https://cli.github.com/) for feedback and self-update features:

```bash
brew install gh
gh auth login
```

Without it, feedback saves locally and self-update won't work.

## Tools

| Tool | Description |
|------|-------------|
| `recipe_get` | Extract structured recipe data from any URL |
| `recipe_build_shopping_list` | Aggregate multiple recipes into a categorized shopping list with grouped ingredients |
| `recipe_format_menu` | Build a weekly menu + shopping list as formatted HTML |
| `recipe_export_apple_note` | Write HTML content directly to Apple Notes with checkboxes |
| `recipe_add_site` | Add a new recipe website to the discovery config |
| `recipe_list_sites` | List all configured recipe sites |
| `recipe_add_favorite` | Save a recipe to favorites with tags |
| `recipe_remove_favorite` | Remove a recipe from favorites |
| `recipe_list_favorites` | List all favorited recipes |
| `recipe_add_exclusion` | Block a recipe or ingredient |
| `recipe_remove_exclusion` | Unblock a recipe or ingredient |
| `recipe_list_exclusions` | List all exclusions |
| `recipe_manage_pantry` | Add/remove pantry staple items |
| `recipe_list_pantry` | List current pantry staples |
| `recipe_get_history` | View recently used recipes with dates and use counts |
| `recipe_feedback` | Submit a bug report, feedback, or feature request as a GitHub issue |
| `recipe_update` | Pull the latest version from GitHub and reinstall dependencies |

## Example Workflow

```
You: I need a menu for 5 days: 1 vegetarian, 1 fish, only 1 red meat.
     Browse skinnytaste and halfbakedharvest for options.
     Check my history to avoid repeats.
     Export the menu and shopping list to Apple Notes.

Claude: [calls recipe_list_sites to get configured sources]
        [browses sites, picks recipes matching constraints]
        [calls recipe_get on each URL for verified ingredients]
        [calls recipe_format_menu to build formatted HTML]
        [calls recipe_export_apple_note to create the checklist note]
```

## Configuration

### config/sites.yaml

Recipe site configurations for discovery. Each site can have a base URL, search URL pattern, and category paths. Adding a site here enables Claude to browse it by category. You do NOT need to add a site here just to extract a recipe from a URL.

Pre-configured sites: Skinnytaste, Half Baked Harvest, Chelsea's Messy Apron, Real Food Whole Life, Damn Delicious, Cucina by Elena.

### config/pantry_staples.yaml

Ingredients you always keep stocked. These still appear on the shopping list but in a separate "Pantry Staples (verify stock)" section with checkboxes. Edit directly or use the `recipe_manage_pantry` tool. Generated with defaults on first setup.

### data/ (auto-generated, gitignored)

- `favorites.json` — Saved favorite recipes with tags
- `exclusions.json` — Blocked recipes and ingredients with reasons
- `history.json` — Auto-populated log of every recipe fetched
- `feedback.json` — Locally saved feedback (fallback when GitHub CLI is unavailable)
- `error.log` — Persistent error and warning log

## How JSON-LD Extraction Works

Recipe websites embed structured data in their HTML for Google rich search results. The server extracts this directly, which includes:

- `recipeIngredient` — exact ingredient list with quantities
- `prepTime` / `cookTime` / `totalTime` — ISO 8601 durations
- `recipeYield` — serving size
- `recipeInstructions` — step-by-step directions
- `recipeCategory`, `recipeCuisine`, `keywords` — metadata

This means the shopping list is built from exactly what the recipe author published, not from Claude's interpretation of the page text.

## Requirements

- Python 3.10+
- Claude Desktop with MCP support
- GitHub CLI (optional, for feedback and self-update)

## License

MIT
