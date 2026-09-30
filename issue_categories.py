"""Issue categories: loads issue_categories.toml and matches issue text to it.

Matching is fixed keyword lookup, so every recommended step comes from the
category file and can be reviewed in advance. A hazard screen runs before
routine classification so a routine keyword can never override a possible
danger signal. When an issue can't be confidently categorized, the result
says so instead of guessing.
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CATEGORIES_PATH = Path(__file__).with_name("issue_categories.toml")

PRIORITIES = ["Low", "Medium", "High", "Emergency"]
HANDLING_STEPS = {
    "safety_critical": "procedure",
    "human_review": "review_guidance",
    "routine": "standard_action",
}
OPTIONAL_KEYS = {"exclude", "min_priority", "max_priority", "vendor"}


@dataclass
class Category:
    key: str
    label: str
    handling: str
    keywords: list
    exclude: list = field(default_factory=list)
    min_priority: str = ""
    max_priority: str = ""
    vendor: str = ""
    procedure: list = field(default_factory=list)
    review_guidance: str = ""
    standard_action: str = ""

    def matches(self, text):
        return bool(matching_phrases(text, self.keywords, self.exclude))


def matching_phrases(text, phrases, exclude=()):
    """Phrases found in text as whole words, allowing a plural "s"/"es".
    Nothing matches if any exclude phrase appears anywhere in the text."""
    if any(phrase.lower() in text for phrase in exclude):
        return []
    return [p for p in phrases
            if re.search(r"(?<!\w)" + re.escape(p.lower()) + r"(?:e?s)?(?!\w)", text)]


@dataclass
class Taxonomy:
    categories: list
    hazard_indicators: list
    hazard_exclude: list


@dataclass
class Classification:
    categories: list      # the category (or categories) used to handle the issue
    also_matched: list    # other categories that matched but did not take precedence
    review_reason: str    # set when a person must review the categorization
    hazard_indicators: list = field(default_factory=list)  # danger signals found with no safety category

    @property
    def is_safety_critical(self):
        return any(c.handling == "safety_critical" for c in self.categories)

    @property
    def vendor(self):
        """The vendor type to dispatch, or "" when there isn't a single clear one."""
        if len(self.categories) == 1:
            return self.categories[0].vendor
        return ""


class CategoryError(ValueError):
    pass


def load_categories(path=DEFAULT_CATEGORIES_PATH):
    """Load and validate the hazard screen and categories (in file order)."""
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    raw = data.get("categories")
    if not isinstance(raw, dict) or not raw:
        raise CategoryError(f"{path}: no [categories.*] sections found")
    screening = data.get("hazard_screening")
    if not isinstance(screening, dict) or not screening.get("indicators"):
        raise CategoryError(f"{path}: [hazard_screening] needs a non-empty 'indicators' list")
    unknown = set(screening) - {"indicators", "exclude"}
    if unknown:
        raise CategoryError(f"{path}: unknown setting(s) in [hazard_screening]: {', '.join(sorted(unknown))}")

    categories = []
    for key, values in raw.items():
        where = f"{path}: [categories.{key}]"
        handling = values.get("handling")
        if handling not in HANDLING_STEPS:
            raise CategoryError(f"{where} handling must be one of {', '.join(HANDLING_STEPS)}")
        steps_key = HANDLING_STEPS[handling]
        allowed = {"label", "handling", "keywords", steps_key} | OPTIONAL_KEYS
        unknown = set(values) - allowed
        if unknown:
            raise CategoryError(f"{where} unknown or misplaced setting(s): {', '.join(sorted(unknown))}")
        for required in ("label", "keywords", steps_key):
            if not values.get(required):
                raise CategoryError(f"{where} missing '{required}'")
        if handling == "safety_critical" and not values.get("min_priority"):
            raise CategoryError(f"{where} safety-critical categories need 'min_priority'")
        if handling != "human_review" and not values.get("vendor"):
            raise CategoryError(f"{where} missing 'vendor'")
        for limit in ("min_priority", "max_priority"):
            if values.get(limit) and values[limit] not in PRIORITIES:
                raise CategoryError(f"{where} {limit} must be one of {', '.join(PRIORITIES)}")
        categories.append(Category(key=key, **values))
    return Taxonomy(categories, screening["indicators"], screening.get("exclude", []))


def classify(issue, taxonomy):
    """Pick the category for an issue, following the precedence rules in the file."""
    text = issue.lower()
    matched = [c for c in taxonomy.categories if c.matches(text)]
    safety = [c for c in matched if c.handling == "safety_critical"]
    review = [c for c in matched if c.handling == "human_review"]
    routine = [c for c in matched if c.handling == "routine"]

    def others(chosen):
        return [c for c in matched if c not in chosen]

    if safety:
        reason = ""
        if len(safety) > 1:
            reason = "Matches more than one safety-critical category: " + ", ".join(c.label for c in safety) + "."
        return Classification(safety, others(safety), reason)
    hazards = matching_phrases(text, taxonomy.hazard_indicators, taxonomy.hazard_exclude)
    if hazards:
        return Classification([], matched, "Possible hazard (" + ", ".join(f"'{h}'" for h in hazards)
                              + ") but no safety category could be confirmed.", hazards)
    if review:
        return Classification(review, others(review), f"{review[0].label} issues need a person to judge severity.")
    if len(routine) == 1:
        return Classification(routine, [], "")
    if routine:
        return Classification([], routine, "Matches more than one category: "
                              + ", ".join(c.label for c in routine) + ".")
    return Classification([], [], "Issue doesn't match any known category.")
