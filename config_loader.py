import yaml
import json
import os
from typing import Any

# ── Load .env file automatically ──
try:
    from dotenv import load_dotenv
    load_dotenv()  # reads .env file from current directory
except ImportError:
    pass  # dotenv not installed, will rely on system environment variables

# ── Try to import Gemini. If not installed or no key, AI conversion is skipped ──
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

MY_FORMAT_EXAMPLE = {
    "workflow_name": "example_workflow",
    "description": "A short description",
    "steps": ["step_one", "step_two", "step_three"],
    "rules": [
        {
            "name": "rule_name",
            "field": "field_in_input",
            "operator": ">= or <= or == or != or > or <",
            "value": 100,
            "on_fail": "reject or manual_review or retry",
            "message": "Human readable reason"
        }
    ],
    "external_dependency": {
        "name": "service_name",
        "simulate": True,
        "failure_action": "retry or reject or manual_review"
    }
}

REQUIRED_KEYS = ["workflow_name", "steps", "rules"]
REQUIRED_RULE_KEYS = ["name", "field", "operator", "value", "on_fail", "message"]
VALID_OPERATORS = [">=", "<=", "==", "!=", ">", "<", "in", "not_in"]
VALID_ON_FAIL = ["reject", "manual_review", "retry"]


# ─────────────────────────────────────────────
# VALIDATOR
# ─────────────────────────────────────────────
def validate_config(config: dict) -> tuple[bool, str]:
    """Returns (is_valid, error_message)"""
    for key in REQUIRED_KEYS:
        if key not in config:
            return False, f"Missing required top-level key: '{key}'"

    if not isinstance(config["steps"], list) or len(config["steps"]) == 0:
        return False, "'steps' must be a non-empty list"

    if not isinstance(config["rules"], list) or len(config["rules"]) == 0:
        return False, "'rules' must be a non-empty list"

    for i, rule in enumerate(config["rules"]):
        for rkey in REQUIRED_RULE_KEYS:
            if rkey not in rule:
                return False, f"Rule [{i}] is missing key: '{rkey}'"
        if rule["operator"] not in VALID_OPERATORS:
            return False, f"Rule [{i}] has invalid operator '{rule['operator']}'. Valid: {VALID_OPERATORS}"
        if rule["on_fail"] not in VALID_ON_FAIL:
            return False, f"Rule [{i}] has invalid on_fail '{rule['on_fail']}'. Valid: {VALID_ON_FAIL}"

    return True, "ok"


# ─────────────────────────────────────────────
# GEMINI CONVERTER
# ─────────────────────────────────────────────
def convert_with_gemini(raw_config: str) -> dict:
    """Send unknown config to Gemini and get back our format."""
    if not GEMINI_AVAILABLE:
        raise RuntimeError("google-generativeai package is not installed.")
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set. Add it to your .env file.")

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.5-flash")

    prompt = f"""
You are a config converter. Convert the following config into EXACTLY this JSON structure.
Do not add any explanation. Return ONLY valid JSON.

TARGET FORMAT:
{json.dumps(MY_FORMAT_EXAMPLE, indent=2)}

Rules for conversion:
- workflow_name: string identifier
- steps: list of string step names
- rules: list of rule objects with keys: name, field, operator, value, on_fail, message
  - operator must be one of: >=, <=, ==, !=, >, <, in, not_in
  - on_fail must be one of: reject, manual_review, retry
- external_dependency is optional

INPUT CONFIG TO CONVERT:
{raw_config}

Return ONLY the JSON object. No markdown. No explanation.
"""
    response = model.generate_content(prompt)
    text = response.text.strip()

    # Strip markdown code fences if Gemini adds them
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    return json.loads(text)


# ─────────────────────────────────────────────
# MAIN LOADER
# ─────────────────────────────────────────────
def load_config(filepath: str) -> dict:
    """
    Load a workflow config file.
    1. Try reading and validating directly.
    2. If validation fails, try Gemini conversion.
    3. Validate again after conversion.
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Config file not found: {filepath}")

    with open(filepath, "r") as f:
        raw_text = f.read()

    # Try YAML first, then JSON
    try:
        config = yaml.safe_load(raw_text)
    except yaml.YAMLError:
        try:
            config = json.loads(raw_text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Config file is neither valid YAML nor JSON: {e}")

    # Validate directly
    is_valid, error = validate_config(config)
    if is_valid:
        print(f"✅ Config '{filepath}' loaded and validated successfully.")
        return config

    # Direct validation failed — try Gemini
    print(f"⚠️  Config validation failed: {error}")
    print("🤖 Attempting Gemini-powered config conversion...")

    try:
        converted = convert_with_gemini(raw_text)
        is_valid2, error2 = validate_config(converted)
        if is_valid2:
            print("✅ Gemini conversion successful. Config is now valid.")
            return converted
        else:
            raise ValueError(f"Gemini converted config is still invalid: {error2}")
    except Exception as gemini_err:
        raise ValueError(
            f"Config is invalid and Gemini conversion failed.\n"
            f"Original error: {error}\n"
            f"Gemini error: {gemini_err}"
        )


def load_all_configs(folder: str = "workflows") -> dict[str, dict]:
    """Load all YAML/JSON configs from the workflows folder."""
    configs = {}
    if not os.path.exists(folder):
        print(f"⚠️  Workflows folder '{folder}' not found.")
        return configs

    for filename in os.listdir(folder):
        if filename.endswith((".yaml", ".yml", ".json")):
            name = filename.rsplit(".", 1)[0]
            try:
                configs[name] = load_config(os.path.join(folder, filename))
                print(f"  📄 Loaded workflow: {name}")
            except Exception as e:
                print(f"  ❌ Failed to load '{filename}': {e}")

    return configs
