"""
Recipe MCP Server - Verified recipe extraction and shopping list generation.

Extracts structured recipe data from any site using schema.org JSON-LD,
aggregates ingredients into categorized shopping lists, and manages
favorites, history, and exclusions.

Transport: stdio (for Claude Desktop)
"""

import json
import re
import os
import sys
import logging
import subprocess
from datetime import datetime, date
from typing import Optional
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, ConfigDict, field_validator
from mcp.server.fastmcp import FastMCP, Context

# ---------------------------------------------------------------------------
# Logging (stderr + persistent file)
# ---------------------------------------------------------------------------
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("recipe_mcp")

# Add a file handler for persistent error logging
_LOG_DIR = Path(__file__).parent / "data"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_file_handler = logging.FileHandler(_LOG_DIR / "error.log")
_file_handler.setLevel(logging.WARNING)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_file_handler)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"

GITHUB_REPO = "mike665/sous-chef-mcp"

SITES_FILE = CONFIG_DIR / "sites.yaml"
PANTRY_FILE = CONFIG_DIR / "pantry_staples.yaml"
EXCLUSIONS_FILE = DATA_DIR / "exclusions.json"
HISTORY_FILE = DATA_DIR / "history.json"
FAVORITES_FILE = DATA_DIR / "favorites.json"

# ---------------------------------------------------------------------------
# Helpers: file I/O
# ---------------------------------------------------------------------------

def _load_json(path: Path, default=None):
    if default is None:
        default = []
    if not path.exists():
        return default
    with open(path, "r") as f:
        return json.load(f)


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _load_yaml(path: Path, default=None):
    """Load YAML, falling back to default if file missing or pyyaml not installed."""
    if default is None:
        default = {}
    if not path.exists():
        return default
    try:
        import yaml
        with open(path, "r") as f:
            return yaml.safe_load(f) or default
    except ImportError:
        logger.warning("pyyaml not installed, returning default for %s", path)
        return default


def _save_yaml(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    except ImportError:
        # Fallback: write as JSON
        with open(path, "w") as f:
            json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Helpers: recipe extraction
# ---------------------------------------------------------------------------

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _extract_jsonld_recipes(html: str) -> list[dict]:
    """Extract all Recipe objects from JSON-LD script tags."""
    soup = BeautifulSoup(html, "html.parser")
    recipes = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
        except (json.JSONDecodeError, TypeError):
            continue
        # JSON-LD can be a single object, a list, or nested in @graph
        _collect_recipes(data, recipes)
    return recipes


def _collect_recipes(data, out: list):
    """Recursively find Recipe-typed objects in JSON-LD structures."""
    if isinstance(data, list):
        for item in data:
            _collect_recipes(item, out)
    elif isinstance(data, dict):
        schema_type = data.get("@type", "")
        # @type can be a string or list
        types = schema_type if isinstance(schema_type, list) else [schema_type]
        if "Recipe" in types:
            out.append(data)
        # Check @graph
        if "@graph" in data:
            _collect_recipes(data["@graph"], out)


def _parse_iso_duration(duration_str: str) -> Optional[int]:
    """Parse ISO 8601 duration (e.g. PT30M, PT1H15M) to minutes."""
    if not duration_str:
        return None
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration_str, re.IGNORECASE)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    total = hours * 60 + minutes + (1 if seconds > 0 else 0)
    return total if total > 0 else None


def _normalize_yield(recipe_yield) -> Optional[str]:
    """Normalize recipeYield to a string."""
    if isinstance(recipe_yield, list):
        return recipe_yield[0] if recipe_yield else None
    return str(recipe_yield) if recipe_yield else None


def _extract_recipe_data(jsonld: dict, url: str) -> dict:
    """Convert a JSON-LD Recipe object into our normalized format."""
    prep = _parse_iso_duration(jsonld.get("prepTime", ""))
    cook = _parse_iso_duration(jsonld.get("cookTime", ""))
    total = _parse_iso_duration(jsonld.get("totalTime", ""))

    # If total is missing but we have prep + cook, compute it
    if total is None and prep is not None and cook is not None:
        total = prep + cook

    ingredients = jsonld.get("recipeIngredient", [])
    # Clean up ingredient strings
    ingredients = [_clean_ingredient(i) for i in ingredients if isinstance(i, str)]

    # Extract instructions as a list of strings
    instructions_raw = jsonld.get("recipeInstructions", [])
    instructions = _parse_instructions(instructions_raw)

    return {
        "name": jsonld.get("name", "Unknown Recipe"),
        "url": url,
        "description": jsonld.get("description", ""),
        "servings": _normalize_yield(jsonld.get("recipeYield")),
        "prep_time_min": prep,
        "cook_time_min": cook,
        "total_time_min": total,
        "ingredients": ingredients,
        "instructions": instructions,
        "cuisine": jsonld.get("recipeCuisine", ""),
        "category": jsonld.get("recipeCategory", ""),
        "keywords": jsonld.get("keywords", ""),
    }


def _clean_ingredient(text: str) -> str:
    """Normalize whitespace in ingredient strings."""
    return re.sub(r"\s+", " ", text).strip()


def _parse_instructions(raw) -> list[str]:
    """Parse recipeInstructions from various formats into a flat list of strings."""
    if isinstance(raw, str):
        return [raw]
    steps = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                steps.append(item)
            elif isinstance(item, dict):
                if item.get("@type") == "HowToStep":
                    steps.append(item.get("text", ""))
                elif item.get("@type") == "HowToSection":
                    section_steps = item.get("itemListElement", [])
                    for s in section_steps:
                        if isinstance(s, dict):
                            steps.append(s.get("text", ""))
                        elif isinstance(s, str):
                            steps.append(s)
    return [s.strip() for s in steps if s.strip()]


# ---------------------------------------------------------------------------
# Helpers: ingredient categorization
# ---------------------------------------------------------------------------

CATEGORY_RULES = {
    "Protein 🥩": [
        "chicken", "beef", "pork", "turkey", "salmon", "shrimp", "fish",
        "steak", "sausage", "bacon", "lamb", "tilapia", "cod", "tuna",
        "ground meat", "ground beef", "ground turkey", "ground chicken",
        "prosciutto", "pancetta", "pepperoni", "chorizo", "tofu", "tempeh",
        "mahi", "halibut", "scallop", "crab", "lobster", "meatball",
        "anchov", "rotisserie",
    ],
    "Dairy 🥛 🧀": [
        "cheese", "milk", "cream", "butter", "yogurt", "sour cream",
        "mozzarella", "parmesan", "parm", "cheddar", "ricotta", "feta",
        "gouda", "gruyere", "cream cheese", "half-and-half", "half and half",
        "whipping cream", "heavy cream", "egg",
    ],
    "Produce 🥬": [
        "onion", "garlic", "tomato", "pepper", "lettuce", "spinach",
        "broccoli", "carrot", "celery", "cucumber", "zucchini", "squash",
        "mushroom", "avocado", "corn", "potato", "sweet potato",
        "green onion", "scallion", "cilantro", "parsley", "basil",
        "thyme", "rosemary", "mint", "dill", "ginger", "jalapeño",
        "jalapeno", "serrano", "lemon", "lime", "orange", "apple",
        "banana", "berry", "strawberr", "blueberr", "kale", "arugula",
        "cabbage", "bok choy", "bean sprout", "radish", "beet",
        "asparagus", "artichoke", "eggplant", "fennel", "leek",
        "shallot", "chive", "pea", "snap pea", "snow pea",
        "bell pepper", "poblano", "habanero", "fresno",
    ],
    "Frozen 🧊": [
        "frozen", "cauliflower rice",
    ],
    "Bread & Bakery 🍞": [
        "bread", "bun", "roll", "tortilla", "pita", "naan", "baguette",
        "ciabatta", "croissant", "english muffin", "flatbread",
        "hamburger bun", "hot dog bun", "pizza dough",
    ],
    "Dry Goods and Canned 🍜": [
        "pasta", "rice", "spaghetti", "penne", "noodle", "flour",
        "sugar", "baking", "broth", "stock", "canned", "tomato paste",
        "tomato sauce", "marinara", "enchilada sauce", "salsa",
        "bean", "lentil", "chickpea", "coconut milk", "soy sauce",
        "fish sauce", "oyster sauce", "hoisin", "sriracha", "vinegar",
        "olive oil", "vegetable oil", "sesame oil", "cooking spray",
        "cornstarch", "breadcrumb", "panko", "cereal", "oat",
        "quinoa", "couscous", "orzo", "farro", "barley",
        "honey", "maple syrup", "molasses", "worcestershire",
        "mustard", "ketchup", "mayo", "mayonnaise", "hot sauce",
        "teriyaki", "curry paste", "peanut butter", "almond butter",
        "jam", "jelly", "nutella", "tahini", "miso",
        "bouillon", "dressing", "marinade",
    ],
}

# Spices/seasonings that commonly show up
SPICE_KEYWORDS = [
    "salt", "pepper", "cumin", "paprika", "chili powder", "oregano",
    "cinnamon", "nutmeg", "cayenne", "turmeric", "coriander",
    "garlic powder", "onion powder", "garlic salt", "red pepper flake",
    "italian seasoning", "taco seasoning", "everything bagel",
    "bay leaf", "clove", "allspice", "cardamom", "five spice",
    "smoked paprika", "old bay",
]


def _categorize_ingredient(ingredient: str) -> str:
    """Assign an ingredient string to a store section."""
    lower = ingredient.lower()

    # Check frozen first (specific signal)
    for kw in CATEGORY_RULES["Frozen 🧊"]:
        if kw in lower:
            return "Frozen 🧊"

    # Check each category
    for category, keywords in CATEGORY_RULES.items():
        if category == "Frozen 🧊":
            continue
        for kw in keywords:
            if kw in lower:
                return category

    # Check if it's a spice/seasoning
    for kw in SPICE_KEYWORDS:
        if kw in lower:
            return "Dry Goods and Canned 🍜"

    return "Other"


# Build a flat list of all keywords sorted longest-first so "green onion"
# matches before "onion", "sweet potato" before "potato", etc.
_ALL_KEYWORDS = []
for _kws in CATEGORY_RULES.values():
    _ALL_KEYWORDS.extend(_kws)
_ALL_KEYWORDS.extend(SPICE_KEYWORDS)
_ALL_KEYWORDS.sort(key=len, reverse=True)


def _base_ingredient(ingredient: str) -> str:
    """Extract the base ingredient name for grouping similar items.

    Uses the keyword lists to find the most specific match. For example,
    '1/2 medium yellow onion, diced' -> 'onion',
    '2 cups baby bok choy' -> 'bok choy'.
    Falls back to the full ingredient string if no keyword matches.
    """
    lower = ingredient.lower()
    for kw in _ALL_KEYWORDS:
        if kw in lower:
            return kw
    return ingredient.lower().strip()


def _group_ingredients(items: list[dict]) -> list[dict]:
    """Group ingredient entries by base ingredient.

    Input:  [{"ingredient": "1/2 onion, diced", "recipe": "Lasagna"}, ...]
    Output: [{"base": "onion", "entries": [{"ingredient": ..., "recipe": ...}, ...]}, ...]

    Single-entry groups are kept as-is for clean output.
    """
    from collections import OrderedDict
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for item in items:
        base = _base_ingredient(item["ingredient"])
        if base not in groups:
            groups[base] = []
        groups[base].append(item)
    return [{"base": base, "entries": entries} for base, entries in groups.items()]


def _is_pantry_staple(ingredient: str, pantry_list: list[str]) -> bool:
    """Check if an ingredient matches a pantry staple."""
    lower = ingredient.lower()
    for staple in pantry_list:
        if staple.lower() in lower:
            return True
    return False


# ---------------------------------------------------------------------------
# Lifespan: load config at startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def app_lifespan(app):
    """Load configuration files at server startup."""
    sites = _load_yaml(SITES_FILE, default={"sites": {}})
    pantry = _load_yaml(PANTRY_FILE, default={"staples": []})

    # Ensure data dir exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(
        headers=_HTTP_HEADERS,
        follow_redirects=True,
        timeout=30.0,
    ) as client:
        yield {
            "sites": sites,
            "pantry": pantry,
            "http": client,
        }


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("Sous Chef MCP", lifespan=app_lifespan)


# ---------------------------------------------------------------------------
# Input Models
# ---------------------------------------------------------------------------

class RecipeUrlInput(BaseModel):
    """Input for extracting a recipe from a URL."""
    model_config = ConfigDict(str_strip_whitespace=True)

    url: str = Field(
        ...,
        description="Full URL of the recipe page to extract (e.g. 'https://www.skinnytaste.com/chicken-zucchini-stir-fry/')",
        min_length=10,
    )
    desired_servings: Optional[int] = Field(
        default=None,
        description="Scale ingredient quantities to this many servings. If omitted, uses the recipe's default.",
        ge=1, le=50,
    )


class ShoppingListInput(BaseModel):
    """Input for building an aggregated shopping list from multiple recipe URLs."""
    model_config = ConfigDict(str_strip_whitespace=True)

    urls: list[str] = Field(
        ...,
        description="List of recipe URLs to aggregate into one shopping list.",
        min_length=1, max_length=20,
    )
    desired_servings: Optional[int] = Field(
        default=None,
        description="Scale all recipes to this serving size. If omitted, uses each recipe's default.",
        ge=1, le=50,
    )


class MenuFormatInput(BaseModel):
    """Input for formatting a weekly menu + shopping list for Apple Notes."""
    model_config = ConfigDict(str_strip_whitespace=True)

    menu: list[dict] = Field(
        ...,
        description=(
            "List of day entries. Each entry is a dict with 'day' (str, e.g. 'Monday') "
            "and either 'url' (recipe URL to fetch) or 'label' (free text like "
            "'burger night at the country club'). Optionally include 'on_deck': true "
            "for recipes that are planned but not assigned to a specific day."
        ),
    )
    week_start: Optional[str] = Field(
        default=None,
        description="Start date for the menu header, format MM/DD. Auto-generated if omitted.",
    )
    extra_items: Optional[list[str]] = Field(
        default=None,
        description="Additional shopping list items not tied to a recipe (e.g. 'Hand soap', 'Paper towels').",
    )
    desired_servings: Optional[int] = Field(
        default=None,
        description="Scale all recipes to this serving size.",
        ge=1, le=50,
    )


class SiteConfigInput(BaseModel):
    """Input for adding a new site to the discovery configuration."""
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(..., description="Short name for the site (e.g. 'skinnytaste')", min_length=1, max_length=50)
    base_url: str = Field(..., description="Base URL (e.g. 'https://www.skinnytaste.com')")
    search_pattern: Optional[str] = Field(
        default=None,
        description="Search URL pattern with {query} placeholder (e.g. '/?s={query}')",
    )
    categories: Optional[dict[str, str]] = Field(
        default=None,
        description="Map of category name to URL path (e.g. {'vegetarian': '/category/vegetarian/'})",
    )


class FavoriteInput(BaseModel):
    """Input for adding/removing a recipe from favorites."""
    model_config = ConfigDict(str_strip_whitespace=True)

    url: str = Field(..., description="Recipe URL")
    name: Optional[str] = Field(default=None, description="Recipe name (auto-detected if omitted)")
    tags: Optional[list[str]] = Field(default=None, description="Tags like 'weeknight', 'kids-love-it', 'date-night'")


class ExclusionInput(BaseModel):
    """Input for adding to the exclusion list."""
    model_config = ConfigDict(str_strip_whitespace=True)

    item: str = Field(..., description="Recipe URL or ingredient name to exclude")
    reason: Optional[str] = Field(default=None, description="Why it's excluded (e.g. 'kids hated it', 'allergy')")


class PantryInput(BaseModel):
    """Input for managing pantry staples."""
    model_config = ConfigDict(str_strip_whitespace=True)

    action: str = Field(..., description="'add' or 'remove'")
    items: list[str] = Field(..., description="Ingredient names to add/remove from pantry staples list")

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        if v.lower() not in ("add", "remove"):
            raise ValueError("action must be 'add' or 'remove'")
        return v.lower()


class AppleNoteInput(BaseModel):
    """Input for exporting formatted HTML to Apple Notes."""
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., description="Title for the Apple Note")
    html_body: str = Field(..., description="HTML content to write to the note")
    folder: Optional[str] = Field(
        default=None,
        description="Apple Notes folder name. If omitted, uses the default Notes folder.",
    )


class FeedbackInput(BaseModel):
    """Input for submitting feedback or a bug report."""
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(
        ...,
        description="Short summary of the issue or feedback",
        min_length=5, max_length=200,
    )
    description: str = Field(
        ...,
        description="Detailed description of the problem, including what happened and what was expected",
    )
    label: str = Field(
        default="feedback",
        description="Issue label: 'bug', 'feedback', or 'feature-request'",
    )

    @field_validator("label")
    @classmethod
    def validate_label(cls, v: str) -> str:
        allowed = ("bug", "feedback", "feature-request")
        if v.lower() not in allowed:
            raise ValueError(f"label must be one of: {', '.join(allowed)}")
        return v.lower()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="recipe_get",
    annotations={
        "title": "Get Recipe from URL",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def recipe_get(params: RecipeUrlInput, ctx: Context) -> str:
    """Fetch and extract structured recipe data from a URL using schema.org JSON-LD.

    Returns the recipe name, ingredients, prep/cook times, servings, and instructions.
    Ingredients are extracted directly from the site's structured data, ensuring
    accuracy with no hallucinated items.

    Args:
        params (RecipeUrlInput): Contains the recipe URL and optional desired_servings.

    Returns:
        str: JSON with recipe name, url, ingredients, times, instructions, servings.
    """
    client = ctx.request_context.lifespan_context["http"]

    try:
        resp = await client.get(params.url)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error("HTTP %s fetching %s", e.response.status_code, params.url)
        return json.dumps({"error": f"HTTP {e.response.status_code} fetching {params.url}"})
    except httpx.RequestError as e:
        logger.error("Request failed for %s: %s", params.url, e)
        return json.dumps({"error": f"Request failed for {params.url}: {str(e)}"})

    recipes = _extract_jsonld_recipes(resp.text)
    if not recipes:
        return json.dumps({
            "error": "No schema.org Recipe found on this page. The site may not use JSON-LD structured data.",
            "url": params.url,
            "suggestion": "The site may not use JSON-LD structured data. Manually verify the ingredients.",
        })

    recipe = _extract_recipe_data(recipes[0], params.url)

    # Log to history
    _log_history(recipe)

    return json.dumps(recipe, indent=2)


@mcp.tool(
    name="recipe_build_shopping_list",
    annotations={
        "title": "Build Shopping List from Recipe URLs",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def recipe_build_shopping_list(params: ShoppingListInput, ctx: Context) -> str:
    """Fetch multiple recipes and build a categorized, deduplicated shopping list.

    Each ingredient is tagged with which recipe(s) it belongs to. Pantry staples
    are listed in a separate section. Items on the exclusion list are flagged.

    Args:
        params (ShoppingListInput): List of recipe URLs and optional serving size.

    Returns:
        str: JSON with categorized shopping list, pantry staples section,
             recipe summaries with cook times, and any warnings.
    """
    client = ctx.request_context.lifespan_context["http"]
    pantry_config = ctx.request_context.lifespan_context["pantry"]
    pantry_list = pantry_config.get("staples", [])
    exclusions = _load_json(EXCLUSIONS_FILE, default=[])
    excluded_ingredients = [e["item"].lower() for e in exclusions if not e["item"].startswith("http")]

    recipes = []
    errors = []

    for url in params.urls:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            jsonld_recipes = _extract_jsonld_recipes(resp.text)
            if jsonld_recipes:
                recipe = _extract_recipe_data(jsonld_recipes[0], url)
                recipes.append(recipe)
                _log_history(recipe)
            else:
                logger.warning("No JSON-LD Recipe found: %s", url)
                errors.append(f"No JSON-LD Recipe found: {url}")
        except Exception as e:
            logger.error("Failed to fetch %s: %s", url, e)
            errors.append(f"Failed to fetch {url}: {str(e)}")

    # Build categorized list with recipe attribution
    categorized: dict[str, list[dict]] = {}
    pantry_section: list[dict] = []

    for recipe in recipes:
        for ingredient in recipe["ingredients"]:
            entry = {
                "ingredient": ingredient,
                "recipe": recipe["name"],
            }

            # Check exclusions
            if any(ex in ingredient.lower() for ex in excluded_ingredients):
                entry["excluded"] = True
                continue

            # Check pantry
            if _is_pantry_staple(ingredient, pantry_list):
                pantry_section.append(entry)
                continue

            category = _categorize_ingredient(ingredient)
            if category not in categorized:
                categorized[category] = []
            categorized[category].append(entry)

    # Recipe summaries with timing
    summaries = []
    for r in recipes:
        summary = {
            "name": r["name"],
            "url": r["url"],
            "servings": r["servings"],
            "prep_time_min": r["prep_time_min"],
            "cook_time_min": r["cook_time_min"],
            "total_time_min": r["total_time_min"],
        }
        summaries.append(summary)

    # Group like ingredients within each category
    grouped_categorized = {}
    for cat, items in categorized.items():
        grouped_categorized[cat] = _group_ingredients(items)

    grouped_pantry = _group_ingredients(pantry_section)

    result = {
        "shopping_list": grouped_categorized,
        "pantry_staples_needed": grouped_pantry,
        "recipe_summaries": summaries,
        "errors": errors,
    }

    return json.dumps(result, indent=2)


@mcp.tool(
    name="recipe_format_menu",
    annotations={
        "title": "Format Weekly Menu for Apple Notes",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def recipe_format_menu(params: MenuFormatInput, ctx: Context) -> str:
    """Build a complete weekly menu with categorized shopping list as HTML.

    Fetches all recipe URLs, extracts verified ingredients, categorizes by store
    section, and outputs formatted HTML. The returned formatted_menu is ready-to-use
    HTML — pass it directly to recipe_export_apple_note as html_body without
    modifying or reformatting it.

    Args:
        params (MenuFormatInput): Menu entries, optional week start date, extra items.

    Returns:
        str: JSON with formatted_menu (HTML string) and errors list.
    """
    client = ctx.request_context.lifespan_context["http"]
    pantry_config = ctx.request_context.lifespan_context["pantry"]
    pantry_list = pantry_config.get("staples", [])
    exclusions = _load_json(EXCLUSIONS_FILE, default=[])
    excluded_ingredients = [e["item"].lower() for e in exclusions if not e["item"].startswith("http")]

    # Determine date range header
    if params.week_start:
        header_start = params.week_start
    else:
        today = date.today()
        header_start = today.strftime("%-m/%-d")

    # Fetch recipes for entries that have URLs
    recipes_by_url: dict[str, dict] = {}
    errors = []

    for entry in params.menu:
        url = entry.get("url")
        if url and url not in recipes_by_url:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                jsonld_recipes = _extract_jsonld_recipes(resp.text)
                if jsonld_recipes:
                    recipe = _extract_recipe_data(jsonld_recipes[0], url)
                    recipes_by_url[url] = recipe
                    _log_history(recipe)
                else:
                    logger.warning("No JSON-LD Recipe found: %s", url)
                    errors.append(f"No JSON-LD Recipe found: {url}")
            except Exception as e:
                logger.error("Failed to fetch %s: %s", url, e)
                errors.append(f"Failed to fetch {url}: {str(e)}")

    # Build the menu as HTML so Apple Notes preserves formatting
    html = []
    html.append(f"<h1>Weekly Menu {header_start}</h1>")
    html.append("<h2>Weekly Overview</h2>")
    html.append("<ul>")

    for entry in params.menu:
        day = entry.get("day", "")
        url = entry.get("url")
        label = entry.get("label", "")
        is_on_deck = entry.get("on_deck", False)

        prefix = "On deck" if is_on_deck else day

        if url:
            recipe = recipes_by_url.get(url)
            if recipe:
                time_note = ""
                if recipe["total_time_min"]:
                    time_note = f" ({recipe['total_time_min']} min)"
                elif recipe["cook_time_min"]:
                    time_note = f" ({recipe['cook_time_min']} min cook)"
                html.append(f'<li><b>{prefix}:</b> <a href="{url}">{recipe["name"]}</a>{time_note}</li>')
            else:
                html.append(f'<li><b>{prefix}:</b> <a href="{url}">{url}</a></li>')
        else:
            html.append(f"<li><b>{prefix}:</b> {label}</li>")

    html.append("</ul>")

    # Aggregate ingredients into dicts for grouping
    categorized: dict[str, list[dict]] = {}
    pantry_items: list[dict] = []

    for recipe in recipes_by_url.values():
        for ingredient in recipe["ingredients"]:
            # Check exclusions
            if any(ex in ingredient.lower() for ex in excluded_ingredients):
                continue

            entry = {"ingredient": ingredient, "recipe": recipe["name"]}

            # Check pantry
            if _is_pantry_staple(ingredient, pantry_list):
                pantry_items.append(entry)
                continue

            category = _categorize_ingredient(ingredient)
            if category not in categorized:
                categorized[category] = []
            categorized[category].append(entry)

    # Group like ingredients within each category
    grouped_categorized = {}
    for cat, items in categorized.items():
        grouped_categorized[cat] = _group_ingredients(items)
    grouped_pantry = _group_ingredients(pantry_items)

    # Preferred category order
    category_order = [
        "Protein 🥩",
        "Dairy 🥛 🧀",
        "Produce 🥬",
        "Dry Goods and Canned 🍜",
        "Bread & Bakery 🍞",
        "Frozen 🧊",
        "Other",
    ]

    html.append("<hr>")
    html.append("<h2>Shopping List</h2>")

    # Apple Notes checklist styles
    CL_UL = '<ul style="-apple-note-checkbox">'
    CL_LI = '<li style="-apple-note-checkbox-unchecked">'

    def _render_grouped(groups: list[dict]):
        """Render grouped ingredients as checklist items."""
        for group in groups:
            entries = group["entries"]
            if len(entries) == 1:
                e = entries[0]
                html.append(f"{CL_LI}{e['ingredient']} ({e['recipe']})</li>")
            else:
                recipes_str = ", ".join(e["recipe"] for e in entries)
                html.append(f"{CL_LI}<b>{group['base'].title()}</b> ({recipes_str})")
                html.append(CL_UL)
                for e in entries:
                    html.append(f"{CL_LI}{e['ingredient']} ({e['recipe']})</li>")
                html.append("</ul></li>")

    for cat in category_order:
        if cat in grouped_categorized and grouped_categorized[cat]:
            html.append(f"<h3>{cat}</h3>")
            html.append(CL_UL)
            _render_grouped(grouped_categorized[cat])
            html.append("</ul>")

    # Any remaining categories not in preferred order
    for cat, groups in grouped_categorized.items():
        if cat not in category_order and groups:
            html.append(f"<h3>{cat}</h3>")
            html.append(CL_UL)
            _render_grouped(groups)
            html.append("</ul>")

    # Pantry staples section
    if grouped_pantry:
        html.append("<h3>Pantry Staples (verify stock)</h3>")
        html.append(CL_UL)
        _render_grouped(grouped_pantry)
        html.append("</ul>")

    # Extra items
    if params.extra_items:
        html.append("<h3>Other</h3>")
        html.append(CL_UL)
        for item in params.extra_items:
            html.append(f"{CL_LI}{item}</li>")
        html.append("</ul>")

    # Timing summary at the bottom
    html.append("<hr>")
    html.append("<h3>Prep &amp; Cook Times</h3>")
    html.append("<ul>")
    for recipe in recipes_by_url.values():
        time_parts = []
        if recipe["prep_time_min"]:
            time_parts.append(f"prep {recipe['prep_time_min']}min")
        if recipe["cook_time_min"]:
            time_parts.append(f"cook {recipe['cook_time_min']}min")
        if recipe["total_time_min"]:
            time_parts.append(f"total {recipe['total_time_min']}min")
        time_str = ", ".join(time_parts) if time_parts else "time not specified"
        html.append(f"<li>{recipe['name']}: {time_str}</li>")
    html.append("</ul>")

    if errors:
        html.append("<hr>")
        html.append("<h3>Warnings</h3>")
        html.append("<ul>")
        for err in errors:
            html.append(f"<li>{err}</li>")
        html.append("</ul>")

    formatted = "\n".join(html)
    return json.dumps({"formatted_menu": formatted, "errors": errors})


@mcp.tool(
    name="recipe_export_apple_note",
    annotations={
        "title": "Export to Apple Notes",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def recipe_export_apple_note(params: AppleNoteInput, ctx: Context) -> str:
    """Export HTML content directly to Apple Notes, preserving all formatting.

    Use this after recipe_format_menu to write the formatted_menu HTML to Apple
    Notes. Pass the formatted_menu value directly as html_body — do NOT rewrite
    or reformat it.

    Args:
        params (AppleNoteInput): Title, HTML body, and optional folder name.

    Returns:
        str: Confirmation with the note title.
    """
    # Escape the HTML for embedding in AppleScript
    escaped_html = params.html_body.replace("\\", "\\\\").replace('"', '\\"')
    escaped_title = params.title.replace("\\", "\\\\").replace('"', '\\"')

    if params.folder:
        escaped_folder = params.folder.replace("\\", "\\\\").replace('"', '\\"')
        applescript = f'''
tell application "Notes"
    set targetFolder to folder "{escaped_folder}"
    make new note at targetFolder with properties {{name:"{escaped_title}", body:"{escaped_html}"}}
end tell
'''
    else:
        applescript = f'''
tell application "Notes"
    make new note with properties {{name:"{escaped_title}", body:"{escaped_html}"}}
end tell
'''

    try:
        result = subprocess.run(
            ["osascript", "-e", applescript],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return json.dumps({
                "error": f"AppleScript failed: {result.stderr.strip()}",
            })
        return json.dumps({
            "status": "ok",
            "message": f"Note '{params.title}' created in Apple Notes",
        })
    except subprocess.TimeoutExpired:
        logger.error("AppleScript timed out creating note '%s'", params.title)
        return json.dumps({"error": "AppleScript timed out"})
    except Exception as e:
        logger.error("Failed to create Apple Note '%s': %s", params.title, e)
        return json.dumps({"error": f"Failed to create note: {str(e)}"})


@mcp.tool(
    name="recipe_add_site",
    annotations={
        "title": "Add Recipe Site Configuration",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def recipe_add_site(params: SiteConfigInput, ctx: Context) -> str:
    """Add a new recipe website to the discovery configuration.

    This allows Claude to search the site by category or keyword. Recipe extraction
    from URLs works without configuration (JSON-LD is site-agnostic), but adding
    a site config enables structured browsing and category filtering.

    Args:
        params (SiteConfigInput): Site name, base URL, search pattern, categories.

    Returns:
        str: Confirmation with updated site list.
    """
    sites = ctx.request_context.lifespan_context["sites"]
    if "sites" not in sites:
        sites["sites"] = {}

    sites["sites"][params.name] = {
        "base_url": params.base_url,
        "search_pattern": params.search_pattern,
        "categories": params.categories or {},
    }

    _save_yaml(SITES_FILE, sites)

    return json.dumps({
        "status": "ok",
        "message": f"Added site '{params.name}' with base URL {params.base_url}",
        "configured_sites": list(sites["sites"].keys()),
    })


@mcp.tool(
    name="recipe_list_sites",
    annotations={
        "title": "List Configured Recipe Sites",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def recipe_list_sites(ctx: Context) -> str:
    """List all configured recipe websites with their search patterns and categories.

    Returns:
        str: JSON list of configured sites.
    """
    sites = ctx.request_context.lifespan_context["sites"]
    return json.dumps(sites.get("sites", {}), indent=2)


@mcp.tool(
    name="recipe_add_favorite",
    annotations={
        "title": "Add Recipe to Favorites",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def recipe_add_favorite(params: FavoriteInput, ctx: Context) -> str:
    """Add a recipe to the favorites list for future reference.

    Args:
        params (FavoriteInput): Recipe URL, optional name and tags.

    Returns:
        str: Confirmation with current favorites count.
    """
    favorites = _load_json(FAVORITES_FILE, default=[])

    # Check for duplicate
    if any(f["url"] == params.url for f in favorites):
        return json.dumps({"status": "exists", "message": "Recipe already in favorites"})

    entry = {
        "url": params.url,
        "name": params.name or "",
        "tags": params.tags or [],
        "added": datetime.now().isoformat(),
    }
    favorites.append(entry)
    _save_json(FAVORITES_FILE, favorites)

    return json.dumps({
        "status": "ok",
        "message": f"Added to favorites: {params.name or params.url}",
        "total_favorites": len(favorites),
    })


@mcp.tool(
    name="recipe_remove_favorite",
    annotations={
        "title": "Remove Recipe from Favorites",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def recipe_remove_favorite(params: FavoriteInput, ctx: Context) -> str:
    """Remove a recipe from the favorites list.

    Args:
        params (FavoriteInput): Recipe URL to remove.

    Returns:
        str: Confirmation.
    """
    favorites = _load_json(FAVORITES_FILE, default=[])
    original_count = len(favorites)
    favorites = [f for f in favorites if f["url"] != params.url]
    _save_json(FAVORITES_FILE, favorites)

    removed = original_count - len(favorites)
    return json.dumps({
        "status": "ok",
        "removed": removed,
        "total_favorites": len(favorites),
    })


@mcp.tool(
    name="recipe_list_favorites",
    annotations={
        "title": "List Favorite Recipes",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def recipe_list_favorites(ctx: Context) -> str:
    """List all favorited recipes with their tags and dates.

    Returns:
        str: JSON list of favorite recipes.
    """
    favorites = _load_json(FAVORITES_FILE, default=[])
    return json.dumps(favorites, indent=2)


@mcp.tool(
    name="recipe_add_exclusion",
    annotations={
        "title": "Add to Exclusion List",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def recipe_add_exclusion(params: ExclusionInput, ctx: Context) -> str:
    """Add a recipe or ingredient to the exclusion list.

    Excluded recipes won't be suggested. Excluded ingredients will be flagged
    or omitted from shopping lists.

    Args:
        params (ExclusionInput): Item to exclude (URL or ingredient name) and reason.

    Returns:
        str: Confirmation with current exclusions count.
    """
    exclusions = _load_json(EXCLUSIONS_FILE, default=[])

    if any(e["item"] == params.item for e in exclusions):
        return json.dumps({"status": "exists", "message": "Already on exclusion list"})

    entry = {
        "item": params.item,
        "reason": params.reason or "",
        "added": datetime.now().isoformat(),
    }
    exclusions.append(entry)
    _save_json(EXCLUSIONS_FILE, exclusions)

    return json.dumps({
        "status": "ok",
        "message": f"Excluded: {params.item}",
        "total_exclusions": len(exclusions),
    })


@mcp.tool(
    name="recipe_remove_exclusion",
    annotations={
        "title": "Remove from Exclusion List",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def recipe_remove_exclusion(params: ExclusionInput, ctx: Context) -> str:
    """Remove an item from the exclusion list.

    Args:
        params (ExclusionInput): Item to un-exclude.

    Returns:
        str: Confirmation.
    """
    exclusions = _load_json(EXCLUSIONS_FILE, default=[])
    exclusions = [e for e in exclusions if e["item"] != params.item]
    _save_json(EXCLUSIONS_FILE, exclusions)

    return json.dumps({"status": "ok", "total_exclusions": len(exclusions)})


@mcp.tool(
    name="recipe_list_exclusions",
    annotations={
        "title": "List Exclusions",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def recipe_list_exclusions(ctx: Context) -> str:
    """List all excluded recipes and ingredients with reasons.

    Returns:
        str: JSON list of exclusions.
    """
    exclusions = _load_json(EXCLUSIONS_FILE, default=[])
    return json.dumps(exclusions, indent=2)


@mcp.tool(
    name="recipe_manage_pantry",
    annotations={
        "title": "Manage Pantry Staples List",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def recipe_manage_pantry(params: PantryInput, ctx: Context) -> str:
    """Add or remove items from the pantry staples list.

    Pantry staples are ingredients you always have on hand. They appear in a
    separate 'verify stock' section of the shopping list rather than the main list.

    Args:
        params (PantryInput): Action ('add' or 'remove') and list of items.

    Returns:
        str: Updated pantry staples list.
    """
    pantry = ctx.request_context.lifespan_context["pantry"]
    staples = pantry.get("staples", [])

    if params.action == "add":
        for item in params.items:
            if item.lower() not in [s.lower() for s in staples]:
                staples.append(item)
    elif params.action == "remove":
        lower_remove = [i.lower() for i in params.items]
        staples = [s for s in staples if s.lower() not in lower_remove]

    pantry["staples"] = staples
    _save_yaml(PANTRY_FILE, pantry)

    return json.dumps({
        "status": "ok",
        "action": params.action,
        "pantry_staples": staples,
    })


@mcp.tool(
    name="recipe_list_pantry",
    annotations={
        "title": "List Pantry Staples",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def recipe_list_pantry(ctx: Context) -> str:
    """List all items on the pantry staples list.

    Returns:
        str: JSON list of pantry staples.
    """
    pantry = ctx.request_context.lifespan_context["pantry"]
    return json.dumps({"staples": pantry.get("staples", [])}, indent=2)


@mcp.tool(
    name="recipe_get_history",
    annotations={
        "title": "Get Recipe History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def recipe_get_history(ctx: Context) -> str:
    """List recently used recipes with dates, useful for avoiding repeats.

    Returns:
        str: JSON list of recipe history entries (most recent first).
    """
    history = _load_json(HISTORY_FILE, default=[])
    # Return most recent first
    history.sort(key=lambda x: x.get("last_used", ""), reverse=True)
    return json.dumps(history[:50], indent=2)


@mcp.tool(
    name="recipe_feedback",
    annotations={
        "title": "Submit Feedback or Bug Report",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def recipe_feedback(params: FeedbackInput, ctx: Context) -> str:
    """Submit feedback, a bug report, or a feature request as a GitHub issue.

    Use this when something went wrong, a recipe didn't extract correctly,
    the shopping list had issues, or you have an idea for improvement.

    Args:
        params (FeedbackInput): Title, description, and label (bug/feedback/feature-request).

    Returns:
        str: Confirmation with the GitHub issue URL if created successfully.
    """
    import platform
    hostname = platform.node()

    body = (
        f"{params.description}\n\n"
        f"---\n"
        f"*Submitted from: {hostname}*\n"
        f"*Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}*"
    )

    try:
        result = subprocess.run(
            [
                "gh", "issue", "create",
                "--repo", GITHUB_REPO,
                "--title", params.title,
                "--body", body,
                "--label", params.label,
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            # gh CLI failed — log and fall back to local file
            logger.error("gh issue create failed: %s", result.stderr.strip())
            return _save_feedback_locally(params, hostname)

        issue_url = result.stdout.strip()
        return json.dumps({
            "status": "ok",
            "message": f"Issue created: {issue_url}",
            "url": issue_url,
        })
    except FileNotFoundError:
        # gh CLI not installed
        logger.warning("gh CLI not found, saving feedback locally")
        return _save_feedback_locally(params, hostname)
    except subprocess.TimeoutExpired:
        logger.error("gh issue create timed out")
        return _save_feedback_locally(params, hostname)


def _save_feedback_locally(params, hostname: str) -> str:
    """Save feedback to a local file when GitHub is unavailable."""
    feedback_file = DATA_DIR / "feedback.json"
    feedback = _load_json(feedback_file, default=[])
    feedback.append({
        "title": params.title,
        "description": params.description,
        "label": params.label,
        "hostname": hostname,
        "date": datetime.now().isoformat(),
    })
    _save_json(feedback_file, feedback)
    return json.dumps({
        "status": "saved_locally",
        "message": "Could not reach GitHub. Feedback saved to data/feedback.json for the developer to review.",
    })


# ---------------------------------------------------------------------------
# Helpers: history logging
# ---------------------------------------------------------------------------

def _log_history(recipe: dict):
    """Log a recipe fetch to history."""
    history = _load_json(HISTORY_FILE, default=[])

    # Update existing or add new
    existing = next((h for h in history if h["url"] == recipe["url"]), None)
    now = datetime.now().isoformat()

    if existing:
        existing["last_used"] = now
        existing["use_count"] = existing.get("use_count", 0) + 1
    else:
        history.append({
            "url": recipe["url"],
            "name": recipe["name"],
            "first_used": now,
            "last_used": now,
            "use_count": 1,
        })

    _save_json(HISTORY_FILE, history)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
